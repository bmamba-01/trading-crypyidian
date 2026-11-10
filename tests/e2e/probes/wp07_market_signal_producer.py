"""Fixture offline cho market producer, BotDecision và delivery Telegram giả."""

import json
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

STATE = json.loads(os.environ["WP07M_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
MEMBERSHIP = UUID(STATE["membership_id"])
BINDING = UUID(STATE["binding_id"])
SUBSCRIPTION = UUID(STATE["subscription_id"])
ACCOUNT = UUID(STATE["account_id"])
ROOT_SETTINGS = UUID(STATE["root_settings_id"])
RISK_PROFILE = UUID(STATE["risk_profile_id"])
BOT_CONFIG = UUID(STATE["bot_config_id"])
INSTRUMENT = UUID(STATE["instrument_id"])
NOW = datetime.fromisoformat(STATE["now"]).astimezone(UTC)


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"wp07-market-signal:{WORKSPACE}:{name}")


FILTERED_MEMBERSHIP = _id("filter-membership")
FILTERED_USER = _id("filter-user")
FILTERED_BINDING = _id("filter-binding")
FILTERED_SUBSCRIPTION = _id("filter-subscription")


def _connect() -> psycopg.Connection[Any]:
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        autocommit=True,
    )


def _insert(connection: psycopg.Connection[Any], table: str, **values: Any) -> None:
    values.setdefault("id", uuid4())
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, values)),
        sql.SQL(",").join(sql.Placeholder() for _ in values),
    )
    connection.execute(query, tuple(values.values()))


def _seed(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    owner_user = _id("owner-user")
    if not STATE.get("workspace_exists", False):
        connection.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'WP07 market signal E2E', %s, 'PAPER', 'ACTIVE')",
            (WORKSPACE, "wp07-market-signal-" + WORKSPACE.hex[:12]),
        )
    _insert(
        connection,
        "user_account",
        id=owner_user,
        email_normalized=f"wp07-market-{WORKSPACE.hex[:12]}@example.invalid",
        password_hash="e2e-unused",
        status="ACTIVE",
    )
    _insert(
        connection,
        "user_membership",
        id=MEMBERSHIP,
        workspace_id=WORKSPACE,
        user_id=owner_user,
        status="ACTIVE",
    )
    _insert(
        connection,
        "role_binding",
        id=_id("owner-role"),
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        role="owner",
    )
    _insert(
        connection,
        "trading_account",
        id=ACCOUNT,
        workspace_id=WORKSPACE,
        name="WP07 signal-only PAPER account",
        venue="binance_usdm",
        mode="PAPER",
        environment="local",
        status="ENABLED",
        max_notional=Decimal("10000"),
    )
    bot_id = connection.execute(
        "SELECT id FROM bot_instance WHERE workspace_id=%s AND trading_account_id=%s",
        (WORKSPACE, ACCOUNT),
    ).fetchone()[0]
    connection.execute(
        "UPDATE bot_instance SET status='ENABLED' WHERE workspace_id=%s AND id=%s",
        (WORKSPACE, bot_id),
    )
    hard_caps = {
        "schema_version": 1,
        "hard_caps": {
            "max_aggregate_open_risk": "250",
            "max_notional": "10000",
            "max_leverage": "5",
            "max_positions": 2,
            "max_symbol_notional": "2000",
            "max_concentration_fraction": "0.2",
            "max_daily_loss": "200",
            "max_rolling_loss": "500",
            "max_account_age_seconds": 86400,
            "max_data_age_seconds": 7200,
        },
    }
    _insert(
        connection,
        "risk_profile_version",
        id=RISK_PROFILE,
        workspace_id=WORKSPACE,
        name="WP07 signal E2E risk",
        version=1,
        max_risk_fraction=Decimal("0.01"),
        max_open_risk_fraction=Decimal("1.0"),
        settings=Jsonb(hard_caps),
        is_active=True,
    )
    from trading_bot.adapters.database.repositories.workspace_settings import (
        canonical_settings,
        settings_hash,
    )
    from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
    from trading_bot.config.workspace_settings import WorkspaceSettings
    from trading_bot.domain.strategy.registry import StrategyRegistry

    settings = WorkspaceSettings.model_validate(
        {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT"],
                "timeframes": ["1h", "4h"],
                "max_age_seconds": 7200,
            },
            "features": {
                "formula_version": "crypidian-features-v1",
                "sessions_timezone": "UTC",
            },
            "strategy": {
                "default_release": "h01-v1",
                "parameters": {},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
            "risk": {"profile_version_id": str(RISK_PROFILE)},
            "paper_execution": {
                "quote_asset": "USDT",
                "starting_equity": "10000",
                "fee_schedule_version": "paper-fees-v1",
                "fill_model": "next_bar_with_adverse_costs",
            },
        }
    )
    settings_payload = canonical_settings(settings)
    root_hash = settings_hash(settings_payload)
    effective_at = NOW - timedelta(days=1)
    _insert(
        connection,
        "workspace_settings_version",
        id=ROOT_SETTINGS,
        workspace_id=WORKSPACE,
        version=1,
        schema_version=1,
        settings_json=Jsonb(settings_payload),
        settings_hash=root_hash,
        status="PUBLISHED",
        revision=1,
        created_by=MEMBERSHIP,
        approved_by=MEMBERSHIP,
        published_at=effective_at,
        effective_at=effective_at,
    )
    release = StrategyRegistry().get_release("h01-v1")
    _insert(
        connection,
        "strategy_release",
        id=_id("h01-release"),
        workspace_id=WORKSPACE,
        name="h01-v1",
        version=1,
        status="PUBLISHED",
        recipe=Jsonb({}),
        content_hash=release.content_hash,
    )
    overlay = canonical_overlay(
        BotConfigOverlay.model_validate(
            {
                "schema_version": 1,
                "strategy": {"release_id": "h01-v1"},
                "universe": {"symbols": ["BTCUSDT"]},
            }
        )
    )
    _insert(
        connection,
        "bot_config_version",
        id=BOT_CONFIG,
        workspace_id=WORKSPACE,
        bot_id=bot_id,
        account_id=ACCOUNT,
        version=1,
        schema_version=1,
        overlay_json=Jsonb(overlay),
        overlay_hash=overlay_hash(overlay),
        base_settings_version_id=ROOT_SETTINGS,
        base_settings_hash=root_hash,
        status="DRAFT",
        revision=1,
        created_by=MEMBERSHIP,
        updated_by=MEMBERSHIP,
    )
    connection.execute(
        "UPDATE bot_config_version SET status='PUBLISHED', approved_by=%s, "
        "published_at=%s, effective_at=%s WHERE workspace_id=%s AND id=%s",
        (MEMBERSHIP, effective_at, effective_at, WORKSPACE, BOT_CONFIG),
    )
    _insert(
        connection,
        "telegram_binding",
        id=BINDING,
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        telegram_user_id="987654321",
        verified_at=NOW - timedelta(minutes=1),
        revoked_at=None,
    )
    _insert(
        connection,
        "telegram_signal_subscription",
        id=SUBSCRIPTION,
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        bot_id=bot_id,
        telegram_binding_id=BINDING,
        telegram_chat_id=987654321,
        enabled=True,
    )
    _insert(
        connection,
        "user_account",
        id=FILTERED_USER,
        email_normalized=f"wp07-market-filter-{WORKSPACE.hex[:12]}@example.invalid",
        password_hash="e2e-unused",
        status="ACTIVE",
    )
    _insert(
        connection,
        "user_membership",
        id=FILTERED_MEMBERSHIP,
        workspace_id=WORKSPACE,
        user_id=FILTERED_USER,
        status="ACTIVE",
    )
    _insert(
        connection,
        "telegram_binding",
        id=FILTERED_BINDING,
        workspace_id=WORKSPACE,
        membership_id=FILTERED_MEMBERSHIP,
        telegram_user_id="987654322",
        verified_at=NOW - timedelta(minutes=1),
        revoked_at=None,
    )
    _insert(
        connection,
        "telegram_signal_subscription",
        id=FILTERED_SUBSCRIPTION,
        workspace_id=WORKSPACE,
        membership_id=FILTERED_MEMBERSHIP,
        bot_id=bot_id,
        telegram_binding_id=FILTERED_BINDING,
        telegram_chat_id=987654322,
        enabled=True,
    )
    _insert(
        connection,
        "market_instrument",
        id=INSTRUMENT,
        workspace_id=WORKSPACE,
        source="BINANCE_USDM",
        idempotency_key="wp07-market-signal-instrument",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_notional=Decimal("5"),
        status="TRADING",
        revision=1,
        checksum="a" * 64,
        supersedes_id=None,
        event_at=NOW - timedelta(days=1),
        received_at=NOW - timedelta(days=1),
        available_at=NOW - timedelta(days=1),
    )
    if STATE.get("skip_bars", False):
        backfill_shadow_id = None
    else:
        backfill_shadow_id = _seed_bars(connection, "1h", timedelta(hours=1), 206, h1=True)
        _seed_bars(connection, "4h", timedelta(hours=4), 205, h1=False)
    return {
        "bot_id": str(bot_id),
        "instrument_id": str(INSTRUMENT),
        "backfill_shadow_id": str(backfill_shadow_id),
        "feature_snapshot_count_before": 0,
    }


def _seed_bars(
    connection: psycopg.Connection[Any],
    timeframe: str,
    interval: timedelta,
    count: int,
    *,
    h1: bool,
) -> UUID | None:
    final_close_time = NOW - timedelta(seconds=3)
    final_open_time = final_close_time - interval + timedelta(microseconds=1)
    first_open_time = final_open_time - (count - 1) * interval
    closes = (
        [300.0 - index * 0.1 for index in range(count - 1)] + [300.0 - (count - 2) * 0.1 + 50.0]
        if h1
        else [200.0 + index * 0.5 for index in range(count)]
    )
    latest: tuple[UUID, datetime, datetime, float] | None = None
    for index, close in enumerate(closes):
        opened = first_open_time + index * interval
        closed = opened + interval - timedelta(microseconds=1)
        previous = closes[index - 1] if index > 0 else close
        open_price = previous if h1 and index > 0 else close
        high = max(open_price, close) + 1.0
        low = min(open_price, close) - 1.0
        if low <= 0:
            raise AssertionError("Fixture giá phải dương.")
        available = closed + timedelta(seconds=1)
        bar_id = _id(f"bar-{timeframe}-{index}")
        _insert(
            connection,
            "market_bar",
            id=bar_id,
            workspace_id=WORKSPACE,
            instrument_id=INSTRUMENT,
            symbol="BTCUSDT",
            timeframe=timeframe,
            source="BINANCE_USDM",
            logical_key=f"BTCUSDT:{timeframe}:{opened.isoformat()}",
            idempotency_key=f"wp07-market-signal:{timeframe}:{index}",
            revision=1,
            checksum=f"{index:064x}",
            supersedes_id=None,
            raw_reference_id=None,
            source_cursor=f"fixture:{timeframe}:{index}",
            open_time=opened,
            close_time=closed,
            event_at=closed,
            received_at=closed + timedelta(milliseconds=500),
            available_at=available,
            open=Decimal(str(open_price)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
            volume=Decimal("100"),
            is_closed=True,
        )
        if index == count - 1:
            latest = (bar_id, opened, closed, close)
    if h1:
        assert latest is not None
        _live_id, opened, closed, live_close = latest
        # Низший приоритет, nhưng available_at và revision cao hơn để kiểm đúng source preference.
        shadow_id = _id("backfill-shadow-latest")
        shadow_close = live_close - 100
        _insert(
            connection,
            "market_bar",
            id=shadow_id,
            workspace_id=WORKSPACE,
            instrument_id=INSTRUMENT,
            symbol="BTCUSDT",
            timeframe=timeframe,
            source="BINANCE_USDM_REST_BACKFILL",
            logical_key=f"REST_BACKFILL:BTCUSDT:{timeframe}:{opened.isoformat()}",
            idempotency_key=f"wp07-market-signal-backfill-shadow:{timeframe}",
            revision=99,
            checksum="b" * 64,
            supersedes_id=None,
            raw_reference_id=None,
            source_cursor=f"backfill-shadow:{timeframe}",
            open_time=opened,
            close_time=closed,
            event_at=closed,
            received_at=NOW - timedelta(milliseconds=750),
            available_at=NOW - timedelta(milliseconds=500),
            open=Decimal(str(shadow_close - 1)),
            high=Decimal(str(shadow_close + 1)),
            low=Decimal(str(shadow_close - 2)),
            close=Decimal(str(shadow_close)),
            volume=Decimal("100"),
            is_closed=True,
        )
        return shadow_id
    return None


def _produce() -> dict[str, Any]:
    import asyncio

    # app tạo FastAPI instance ở import-time; fixture cần override UUID Compose trước import.
    os.environ["TRADING_BOT_MARKET_SIGNAL_WORKSPACE_ID"] = str(WORKSPACE)

    from fastapi.testclient import TestClient

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.app import create_app
    from trading_bot.application.bot_decision_contexts import EffectiveBotDecisionContextProvider
    from trading_bot.application.market_signal_composition import (
        SignalExclusionRecorder,
        SignalRuntimeGate,
    )
    from trading_bot.config.settings import AppSettings

    settings = AppSettings()
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    provider = EffectiveBotDecisionContextProvider(
        sessions,
        runtime_gate=SignalRuntimeGate(),
        exclusion_recorder=SignalExclusionRecorder(),
    )
    eligibility = asyncio.run(provider.eligibility(workspace_id=WORKSPACE, as_of=datetime.now(UTC)))
    asyncio.run(engine.dispose())

    application = create_app(settings)
    with TestClient(application) as client:
        deadline = time.monotonic() + 30
        worker = application.state.market_signal_worker
        while not worker.last_tick_succeeded and time.monotonic() < deadline:
            time.sleep(0.05)
        assert worker.last_tick_succeeded, worker.last_error_code
        readiness = client.get("/health/ready")
        assert readiness.status_code == 200, readiness.text
        result = worker.last_scan_result
        assert result is not None
        return {
            "decision_count": result.decision_count,
            "dispatched_count": result.dispatched_count,
            "stale_count": result.stale_count,
            "unusable_count": result.unusable_count,
            "expired_count": result.expired_count,
            "no_context_count": result.no_context_count,
            "duplicate_count": result.duplicate_count,
            "workspace_id": str(WORKSPACE),
            "eligible_context_count": len(eligibility.contexts),
            "exclusions": [item.reason_code for item in eligibility.exclusions],
        }


def _deliver() -> dict[str, Any]:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        database_url_from_secret,
    )
    from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
    from trading_bot.workers.telegram_signal_worker import TelegramSignalDeliveryWorker

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sender = RecordingTelegramSender()
    worker = TelegramSignalDeliveryWorker(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        workspace_id=WORKSPACE,
        sender=sender,
        worker_id="wp07-market-signal-e2e",
        max_candidate_age=timedelta(hours=2),
    )
    try:
        sent = asyncio.run(worker.tick())
        messages = sender.messages
        return {
            "sent": sent,
            "messages": len(messages),
            "recipient_chat_ids": [chat_id for chat_id, _ in messages],
            "message_contains_reference_only": all(
                "KHÔNG PHẢI LỆNH" in content for _, content in messages
            ),
        }
    finally:
        asyncio.run(engine.dispose())


def _prepare_filter_change() -> dict[str, Any]:
    with _connect() as connection:
        decision_id = connection.execute(
            "SELECT id FROM bot_decision WHERE workspace_id=%s AND status='CANDIDATE'",
            (WORKSPACE,),
        ).fetchone()
        assert decision_id is not None
        _insert(
            connection,
            "telegram_signal_delivery",
            id=_id("pending-filtered-delivery"),
            workspace_id=WORKSPACE,
            bot_id=UUID(STATE["bot_id"]),
            subscription_id=FILTERED_SUBSCRIPTION,
            decision_id=decision_id[0],
            status="PENDING",
            attempts=0,
            available_at=NOW - timedelta(seconds=1),
        )
        connection.execute(
            "UPDATE telegram_signal_subscription SET symbols=%s WHERE workspace_id=%s AND id=%s",
            (Jsonb(["ETHUSDT"]), WORKSPACE, FILTERED_SUBSCRIPTION),
        )
    return {"pending_receipt_seeded": True, "filter_changed": True}


def _verify() -> dict[str, Any]:
    with _connect() as connection:

        def count(table: str, where: str) -> int:
            return int(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE {}").format(
                        sql.Identifier(table), sql.SQL(where)
                    ),
                    (WORKSPACE,),
                ).fetchone()[0]
            )

        candidate_count = count("bot_decision", "workspace_id = %s AND status = 'CANDIDATE'")
        sent_count = count("telegram_signal_delivery", "workspace_id = %s AND status = 'SENT'")
        filtered_count = count(
            "telegram_signal_delivery", "workspace_id = %s AND status = 'FILTERED'"
        )
        pending_evidence_count = count(
            "outbox_event",
            "workspace_id = %s AND event_type = 'signal.evidence.recorded.v1' "
            "AND status = 'PENDING'",
        )
        order_counts = {
            table: count(table, "workspace_id = %s")
            for table in ("order_intent", "fill", "trade", "risk_reservation")
        }
        for role in ("crypidian_market_signal_worker", "crypidian_signal_worker"):
            for table in (
                "order_intent",
                "exchange_order",
                "fill",
                "position_event",
                "trade",
                "trade_cost",
                "risk_reservation",
                "venue_order_intent",
            ):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    assert not connection.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        (role, table, privilege),
                    ).fetchone()[0], (role, table, privilege)
                    if privilege in ("SELECT", "INSERT", "UPDATE"):
                        assert not connection.execute(
                            "SELECT has_any_column_privilege(%s, %s, %s)",
                            (role, table, privilege),
                        ).fetchone()[0], (role, table, privilege)
        assert not connection.execute(
            "SELECT has_table_privilege('crypidian_signal_worker', 'market_bar', 'SELECT')"
        ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_table_privilege('crypidian_signal_worker', "
            "'shared_signal_evidence', 'INSERT')"
        ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_table_privilege('crypidian_market_signal_worker', "
            "'telegram_signal_delivery', 'INSERT')"
        ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_table_privilege('crypidian_market_signal_worker', "
            "'telegram_signal_subscription', 'SELECT')"
        ).fetchone()[0]
        assert candidate_count == 1, candidate_count
        assert sent_count == 1, sent_count
        assert filtered_count == 1, filtered_count
        filtered_receipt = connection.execute(
            "SELECT status, attempts, last_error_code FROM telegram_signal_delivery "
            "WHERE workspace_id=%s AND subscription_id=%s",
            (WORKSPACE, FILTERED_SUBSCRIPTION),
        ).fetchone()
        assert filtered_receipt == ("FILTERED", 0, "SUBSCRIPTION_FILTER_MISMATCH")
        assert pending_evidence_count == 0, pending_evidence_count
        assert set(order_counts.values()) == {0}, order_counts
        decision_snapshot = connection.execute(
            "SELECT f.input_event_ids FROM bot_decision d "
            "JOIN shared_signal_evidence e ON e.workspace_id=d.workspace_id "
            "AND e.id=d.evidence_id JOIN feature_snapshot f "
            "ON f.workspace_id=e.workspace_id AND f.snapshot_id=e.feature_snapshot_id "
            "WHERE d.workspace_id=%s AND d.status='CANDIDATE'",
            (WORKSPACE,),
        ).fetchone()
        assert decision_snapshot is not None
        assert STATE["backfill_shadow_id"] not in decision_snapshot[0]
        return {
            "candidates": candidate_count,
            "sent_receipts": sent_count,
            "filtered_receipts": filtered_count,
            "pending_mismatch_was_filtered_without_attempt": True,
            "pending_evidence": pending_evidence_count,
            "order_rows": order_counts,
            "live_source_preferred_over_backfill": True,
            "producer_has_no_execution_or_delivery_privilege": True,
            "delivery_has_no_market_or_evidence_write_privilege": True,
        }


with _connect() as connection:
    phase = os.environ["WP07M_PHASE"]
    if phase == "seed":
        result = _seed(connection)
    elif phase == "produce":
        # TestClient runs the actual app lifespan and independent producer task.
        result = _produce()
    elif phase == "prepare_filter_change":
        result = _prepare_filter_change()
    elif phase == "deliver":
        result = _deliver()
    elif phase == "verify":
        result = _verify()
    else:
        raise AssertionError(phase)
    print(json.dumps(result, default=str))
