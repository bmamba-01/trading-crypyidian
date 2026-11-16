"""Probe RT-1 lát 2: CANDIDATE thật → PAPER intent → khớp nến kế → đóng bằng stop (Compose).

Dữ liệu chỉ có nguồn `BINANCE_USDM_REST_BACKFILL` (như collector SIG-0). Pha chạy đúng role:

- `seed` (migrator): workspace, account/bot, root settings có khung `1m`, risk profile, release
  `h01-v1` + bot config đã publish, instrument, seed vốn PAPER, nến 1h/4h và vài nến 1m.
- `genesis` (trading-runtime): checkpoint PAPER đầu tiên (chưa có lease).
- `produce` (market-signal-worker): producer thật tạo evidence + BotDecision CANDIDATE.
- `runtime` (trading-runtime): `create_app` thật với `runtime_market_dispatch_enabled=True`
  (provider + event loop production); chặn mọi kết nối socket Python ra ngoài để chứng minh
  không gọi sàn (libpq của PostgreSQL không đi qua socket Python).
- `mark_check` (trading-runtime): vốn account có vị thế mở phải `known` chỉ với mark REST.
- `add_stop_bar` (migrator): nến 1m chạm stop.
- `verify` (migrator): đối soát ledger.
- `tg_seed` (migrator): binding Telegram đã xác minh + subscription bật cho bot (TG-3).
- `tg_results` (telegram-signal-worker): worker kết quả PAPER thật dưới role signal-worker gửi
  qua sender ghi nhớ; chạy lại và khởi động lại không gửi trùng; kiểm quyền cột/RLS.
- `tg_verify` (migrator): trạng thái delivery ledger TG-3.
"""

import asyncio
import hashlib
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

STATE = json.loads(os.environ["RT1_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
NOW = datetime.fromisoformat(STATE["now"]).astimezone(UTC)
SOURCE = "BINANCE_USDM_REST_BACKFILL"
# Nến 1h/4h cuối đóng 20 phút trước NOW; nến 1m được thêm sau khi có candidate.
LAST_CLOSE = NOW - timedelta(minutes=20)


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"rt1-paper-market:{WORKSPACE}:{name}")


ACCOUNT = _id("account")
MEMBERSHIP = _id("membership")
ROOT_SETTINGS = _id("root-settings")
RISK_PROFILE = _id("risk-profile")
BOT_CONFIG = _id("bot-config")
INSTRUMENT = _id("instrument")
TELEGRAM_BINDING = _id("telegram-binding")
TELEGRAM_SUBSCRIPTION = _id("telegram-subscription")
TELEGRAM_CHAT = 777000111


def _password() -> str:
    return Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=_password(),
        autocommit=True,
    )


def _sessions() -> tuple[Any, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    return engine, create_session_factory(engine)


def _insert(connection: psycopg.Connection[Any], table: str, **values: Any) -> None:
    values.setdefault("id", uuid4())
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ),
        tuple(values.values()),
    )


def _bar(
    connection: psycopg.Connection[Any],
    *,
    timeframe: str,
    index: int,
    opened: datetime,
    interval: timedelta,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> None:
    closed = opened + interval - timedelta(microseconds=1)
    _insert(
        connection,
        "market_bar",
        id=_id(f"bar-{timeframe}-{index}"),
        workspace_id=WORKSPACE,
        instrument_id=INSTRUMENT,
        symbol="BTCUSDT",
        timeframe=timeframe,
        source=SOURCE,
        logical_key=f"REST_BACKFILL:BTCUSDT:{timeframe}:{opened.isoformat()}",
        idempotency_key=f"rt1-paper:{timeframe}:{index}",
        revision=1,
        checksum=hashlib.sha256(f"{timeframe}:{index}".encode()).hexdigest(),
        supersedes_id=None,
        raw_reference_id=None,
        source_cursor=f"rt1:{timeframe}:{index}",
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed + timedelta(milliseconds=500),
        available_at=closed + timedelta(seconds=1),
        open=Decimal(str(open_price)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("100"),
        is_closed=True,
    )


def _history(connection: psycopg.Connection[Any], timeframe: str, count: int) -> float:
    interval = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}[timeframe]
    h1 = timeframe == "1h"
    final_open = LAST_CLOSE - interval + timedelta(microseconds=1)
    first_open = final_open - (count - 1) * interval
    closes = (
        [300.0 - index * 0.1 for index in range(count - 1)] + [300.0 - (count - 2) * 0.1 + 50.0]
        if h1
        else [200.0 + index * 0.5 for index in range(count)]
    )
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index > 0 else close
        open_price = previous if h1 and index > 0 else close
        _bar(
            connection,
            timeframe=timeframe,
            index=index,
            opened=first_open + index * interval,
            interval=interval,
            open_price=open_price,
            high=max(open_price, close) + 1.0,
            low=min(open_price, close) - 1.0,
            close=close,
        )
    return closes[-1]


def _minute_open(index: int) -> datetime:
    """Nến 1m nối sau thời điểm submit (gateway chỉ khớp từ `evaluated_at + latency`)."""
    return datetime.fromisoformat(STATE["minute_base"]) + index * timedelta(minutes=1)


def _minute(connection: psycopg.Connection[Any], index: int, *, low: float = 329.0) -> None:
    price = 329.6
    _bar(
        connection,
        timeframe="1m",
        index=index,
        opened=_minute_open(index),
        interval=timedelta(minutes=1),
        open_price=price,
        high=price + 0.9,
        low=min(low, price - 0.6),
        close=price if low >= 329.0 else low + 1.0,
    )


def seed() -> dict[str, Any]:
    from trading_bot.adapters.database.repositories.workspace_settings import (
        canonical_settings,
        settings_hash,
    )
    from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
    from trading_bot.config.workspace_settings import WorkspaceSettings
    from trading_bot.domain.strategy.registry import StrategyRegistry

    with _connect() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, 'RT1 PAPER market loop E2E', %s, 'PAPER', 'ACTIVE')",
            (WORKSPACE, "rt1-paper-" + WORKSPACE.hex[:12]),
        )
        owner = _id("owner")
        _insert(
            connection,
            "user_account",
            id=owner,
            email_normalized=f"rt1-{WORKSPACE.hex[:12]}@example.invalid",
            password_hash="e2e-unused",
            status="ACTIVE",
        )
        _insert(
            connection,
            "user_membership",
            id=MEMBERSHIP,
            workspace_id=WORKSPACE,
            user_id=owner,
            status="ACTIVE",
        )
        _insert(
            connection,
            "role_binding",
            workspace_id=WORKSPACE,
            membership_id=MEMBERSHIP,
            role="owner",
        )
        _insert(
            connection,
            "trading_account",
            id=ACCOUNT,
            workspace_id=WORKSPACE,
            name="RT1 PAPER account",
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
        _insert(
            connection,
            "risk_profile_version",
            id=RISK_PROFILE,
            workspace_id=WORKSPACE,
            name="RT1 risk",
            version=1,
            max_risk_fraction=Decimal("0.01"),
            max_open_risk_fraction=Decimal("1.0"),
            settings=Jsonb(
                {
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
            ),
            is_active=True,
        )
        settings = WorkspaceSettings.model_validate(
            {
                "schema_version": 1,
                "runtime": {"mode": "PAPER", "timezone": "UTC"},
                "market_data": {
                    "venue": "BINANCE_USDM",
                    "symbols": ["BTCUSDT"],
                    "timeframes": ["1m", "1h", "4h"],
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
                "risk": {
                    "profile_version_id": str(RISK_PROFILE),
                    "max_equity_age_seconds": 3600,
                    "max_mark_age_seconds": 3600,
                },
                "paper_execution": {
                    "quote_asset": "USDT",
                    "starting_equity": "10000",
                    "fee_schedule_version": "paper-fees-v1",
                    "fill_model": "next_bar_with_adverse_costs",
                },
            }
        )
        payload = canonical_settings(settings)
        root_hash = settings_hash(payload)
        effective_at = NOW - timedelta(days=1)
        _insert(
            connection,
            "workspace_settings_version",
            id=ROOT_SETTINGS,
            workspace_id=WORKSPACE,
            version=1,
            schema_version=1,
            settings_json=Jsonb(payload),
            settings_hash=root_hash,
            status="PUBLISHED",
            revision=1,
            created_by=MEMBERSHIP,
            approved_by=MEMBERSHIP,
            published_at=effective_at,
            effective_at=effective_at,
        )
        _insert(
            connection,
            "strategy_release",
            workspace_id=WORKSPACE,
            name="h01-v1",
            version=1,
            status="PUBLISHED",
            recipe=Jsonb({}),
            content_hash=StrategyRegistry().get_release("h01-v1").content_hash,
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
            "paper_account_seed",
            bot_id=bot_id,
            workspace_id=WORKSPACE,
            trading_account_id=ACCOUNT,
            quote_asset="USDT",
            initial_equity=Decimal("10000"),
            source_settings_version_id=ROOT_SETTINGS,
            source_settings_hash=root_hash,
            idempotency_key=f"paper-seed:{ACCOUNT}",
        )
        listed = NOW - timedelta(days=30)
        _insert(
            connection,
            "market_instrument",
            id=INSTRUMENT,
            workspace_id=WORKSPACE,
            source="BINANCE_USDM",
            idempotency_key="rt1-paper-instrument",
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
            event_at=listed,
            received_at=listed,
            available_at=listed,
        )
        last_close = _history(connection, "1h", 206)
        _history(connection, "4h", 205)
    return {"bot_id": str(bot_id), "last_h1_close": last_close}


async def genesis() -> dict[str, Any]:
    from trading_bot.adapters.database.repositories.paper_checkpoints import (
        PaperCheckpointRepository,
    )
    from trading_bot.adapters.database.repositories.workspace_settings import (
        WorkspaceSettingsRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_execution_profile import (
        default_paper_execution_profile_catalog,
        resolve_paper_execution_profile,
    )
    from trading_bot.domain.execution.paper import PaperExecutionGateway

    engine, sessions = _sessions()
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await uow.bind_account(ACCOUNT)
            published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
                workspace_id=WORKSPACE
            )
            profile = resolve_paper_execution_profile(
                published, default_paper_execution_profile_catalog()
            )
            key = f"rt1-genesis:{ACCOUNT}"
            await PaperCheckpointRepository().append(
                uow,
                account_id=ACCOUNT,
                event_key=key,
                event_hash=hashlib.sha256(key.encode()).hexdigest(),
                snapshot=PaperExecutionGateway(profile.fill_policy).export_state(),
                occurred_at=NOW - timedelta(days=1),
            )
    finally:
        await engine.dispose()
    return {"genesis": True}


def produce() -> dict[str, Any]:
    import time

    os.environ["TRADING_BOT_MARKET_SIGNAL_WORKSPACE_ID"] = str(WORKSPACE)
    from fastapi.testclient import TestClient

    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    application = create_app(AppSettings())
    with TestClient(application):
        deadline = time.monotonic() + 60
        worker = application.state.market_signal_worker
        while not worker.last_tick_succeeded and time.monotonic() < deadline:
            time.sleep(0.05)
        assert worker.last_tick_succeeded, worker.last_error_code
        result = worker.last_scan_result
        return {
            "decision_count": result.decision_count,
            "dispatched_count": result.dispatched_count,
            "stale_count": result.stale_count,
            "unusable_count": result.unusable_count,
        }


async def runtime() -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    attempted: list[str] = []
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        # Driver PostgreSQL (libpq) không dùng socket Python; mọi kết nối ở đây là ra ngoài.
        attempted.append(str(address))
        raise OSError("RT1_E2E_NETWORK_BLOCKED")

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    engine, _ = _sessions()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        app = create_app(
            AppSettings(
                process_role="trading-runtime",
                runtime_workspace_id=WORKSPACE,
                runtime_market_dispatch_enabled=True,
                runtime_lease_ttl_seconds=30,
                runtime_supervisor_tick_seconds=20,
            ),
            session_factory=sessions,
        )
        async with app.router.lifespan_context(app):
            # Tick đầu đã chạy đồng bộ khi wiring; thêm một tick để chắc vòng nến đã ổn định.
            await app.state.paper_supervisor.tick_once()
            route = next(
                item for item in app.routes if getattr(item, "path", "") == "/health/ready"
            )
            ready = await route.endpoint()
            body = json.loads(ready.body) if hasattr(ready, "body") else ready
            provider = type(app.state.paper_application._context_provider).__name__
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        await engine.dispose()
    return {
        "readiness": body,
        "provider": provider,
        "network_attempts": attempted,
        "ledger": _ledger_counts_via_runtime(),
    }


def _ledger_counts_via_runtime() -> dict[str, Any]:
    with _connect() as connection:
        connection.execute("SELECT set_config('app.workspace_id', %s, false)", (str(WORKSPACE),))
        connection.execute("SELECT set_config('app.bot_id', %s, false)", (STATE["bot_id"],))
        intents = connection.execute(
            "SELECT status FROM order_intent WHERE workspace_id=%s", (WORKSPACE,)
        ).fetchall()
        trades = connection.execute(
            "SELECT status FROM trade WHERE workspace_id=%s", (WORKSPACE,)
        ).fetchall()
        fills = connection.execute(
            "SELECT fill_role FROM fill WHERE workspace_id=%s ORDER BY occurred_at", (WORKSPACE,)
        ).fetchall()
        return {
            "intents": [row[0] for row in intents],
            "trades": [row[0] for row in trades],
            "fills": [row[0] for row in fills],
        }


async def mark_check() -> dict[str, Any]:
    from sqlalchemy import func, select

    from trading_bot.adapters.database.repositories.account_equity import AccountEquityRepository
    from trading_bot.adapters.database.repositories.workspace_settings import (
        WorkspaceSettingsRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.workspace_risk import mark_policy_from

    engine, sessions = _sessions()
    try:
        async with UnitOfWork(
            sessions, workspace_id=WORKSPACE, bot_id=UUID(STATE["bot_id"])
        ) as uow:
            now = await uow.session.scalar(select(func.clock_timestamp()))
            published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
                workspace_id=WORKSPACE
            )
            snapshot = await AccountEquityRepository(uow.session).load_paper_equity_snapshot(
                workspace_id=WORKSPACE,
                bot_id=UUID(STATE["bot_id"]),
                account_id=ACCOUNT,
                as_of=now.astimezone(UTC),
                max_equity_age=timedelta(seconds=3600),
                max_mark_age=timedelta(seconds=3600),
                mark_policy=mark_policy_from(published.settings),
            )
            mark_refs = [ref for ref in snapshot.basis.source_refs if ref.startswith("mark:")]
    finally:
        await engine.dispose()
    return {
        "known": snapshot.known,
        "reason": snapshot.unknown_reason,
        "open_positions": snapshot.basis.open_position_count,
        "mark_refs": len(mark_refs),
    }


async def breaker() -> dict[str, Any]:
    """FX-2: đo drawdown bằng role trading-runtime thật (đọc trade + bot_runtime_control)."""
    from decimal import Decimal
    from types import SimpleNamespace

    from sqlalchemy import func, select

    from trading_bot.adapters.database.repositories.account_equity import AccountEquityRepository
    from trading_bot.adapters.database.repositories.workspace_settings import (
        WorkspaceSettingsRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_market_context import PostgresPaperDecisionContextProvider
    from trading_bot.application.workspace_risk import mark_policy_from

    engine, sessions = _sessions()
    bot_id = UUID(STATE["bot_id"])
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE, bot_id=bot_id) as uow:
            now = (await uow.session.scalar(select(func.clock_timestamp()))).astimezone(UTC)
            published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
                workspace_id=WORKSPACE
            )
            equity = await AccountEquityRepository(uow.session).load_paper_equity_snapshot(
                workspace_id=WORKSPACE,
                bot_id=bot_id,
                account_id=ACCOUNT,
                as_of=now,
                max_equity_age=timedelta(seconds=3600),
                max_mark_age=timedelta(seconds=3600),
                mark_policy=mark_policy_from(published.settings),
            )
            assert equity.known, equity.unknown_reason
            assert equity.seed_capital is not None
            provider = PostgresPaperDecisionContextProvider(sessions, workspace_id=WORKSPACE)
            account = SimpleNamespace(account_id=ACCOUNT, bot_id=bot_id)
            actual = await provider._drawdown_breaker(
                uow.session,
                account,
                as_of=now,
                equity=equity.equity_amount,
                seed=equity.seed_capital,
            )
            stressed = await provider._drawdown_breaker(
                uow.session,
                account,
                as_of=now,
                equity=actual.peak_equity * Decimal("0.89"),
                seed=equity.seed_capital,
            )
    finally:
        await engine.dispose()
    return {
        "seed": str(equity.seed_capital),
        "realized": str(equity.cumulative_realized_pnl),
        "actual_peak": str(actual.peak_equity),
        "actual_tripped": actual.tripped,
        "stressed_tripped": stressed.tripped,
        "baseline_at": actual.baseline_at.isoformat() if actual.baseline_at else None,
    }


async def run_report() -> dict[str, Any]:
    """AI-1: Run Report tất định từ ledger thật của bot RT-1 (role admin của migrator)."""
    from trading_bot.application.run_report import load_run_report

    engine, sessions = _sessions()
    try:
        first = await load_run_report(
            sessions,
            workspace_id=WORKSPACE,
            start=NOW - timedelta(days=3),
            end=NOW + timedelta(days=1),
            run_label="rt1-e2e",
        )
        second = await load_run_report(
            sessions,
            workspace_id=WORKSPACE,
            start=NOW - timedelta(days=3),
            end=NOW + timedelta(days=1),
            run_label="rt1-e2e",
        )
    finally:
        await engine.dispose()
    [bot] = [item for item in first.payload["bots"] if item["bot_id"] == STATE["bot_id"]]
    return {
        "deterministic": first.report_hash == second.report_hash,
        "closed_trades": bot["performance"]["closed_trades"],
        "r_mean": bot["performance"]["r_multiple"]["mean"],
        "mae_mean": bot["performance"]["mae_r"]["mean"],
        "candidates": bot["funnel"]["candidates"],
        "candidates_with_intent": bot["funnel"]["candidates_with_intent"],
        "exit_reasons": bot["performance"]["exit_reasons"],
    }


async def analyst() -> dict[str, Any]:
    """AI-2: report → engine giả → validator → lưu analyst_run/session (role admin migrator)."""
    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError

    from trading_bot.adapters.database.models import (
        AnalystRunRecord,
        AnalystSessionRecord,
        ProposalBacktestRecord,
    )
    from trading_bot.application.ai_analyst import (
        ANALYST_OUTPUT_VERSION,
        PROMPT_VERSION,
        build_prompt,
        input_hash,
        topic_report,
        validate_analysis,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.proposal_backtest import (
        PROPOSAL_BACKTEST_VERSION,
        BacktestArm,
        ProposalBacktestResult,
        aggregate,
    )
    from trading_bot.application.run_report import load_run_report

    engine, sessions = _sessions()
    try:
        report = await load_run_report(
            sessions,
            workspace_id=WORKSPACE,
            start=NOW - timedelta(days=3),
            end=NOW + timedelta(days=1),
            run_label="rt1-e2e",
        )
        family = report.payload["bots"][0]["config"]["strategy_family"]
        topic = topic_report(report.payload, family)
        closed = topic["bots"][0]["performance"]["closed_trades"]
        output = {
            "summary": "Kiểm luồng E2E.",
            "findings": [
                {
                    "statement": f"Có {closed} lệnh đóng.",
                    "evidence_refs": ["/bots/0/performance/closed_trades"],
                }
            ],
            "hypotheses": [],
            "proposals": [],
            "confidence": "low",
        }
        bad = dict(output, summary="Lãi 987654 USDT.")
        prompt = build_prompt(topic)
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            for candidate in (output, bad):
                check = validate_analysis(candidate, topic)
                uow.session.add(
                    AnalystRunRecord(
                        workspace_id=WORKSPACE,
                        topic_key=f"family:{family}",
                        engine="fake",
                        session_id="e2e-session",
                        resumed=False,
                        run_report_hash=report.report_hash,
                        input_sha256=input_hash(prompt),
                        prompt_version=PROMPT_VERSION,
                        output_version=ANALYST_OUTPUT_VERSION,
                        usage={},
                        exit_code=0,
                        raw_stdout="{}",
                        structured_output=candidate,
                        validation_errors=list(check.errors),
                        valid=check.ok,
                    )
                )
            uow.session.add(
                AnalystSessionRecord(
                    workspace_id=WORKSPACE,
                    topic_key=f"family:{family}",
                    engine="fake",
                    session_id="e2e-session",
                    status="ACTIVE",
                    run_count=2,
                )
            )
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            valid = await uow.session.scalar(
                select(func.count()).where(
                    AnalystRunRecord.workspace_id == WORKSPACE, AnalystRunRecord.valid.is_(True)
                )
            )
            total = await uow.session.scalar(
                select(func.count()).where(AnalystRunRecord.workspace_id == WORKSPACE)
            )
            valid_run_id = await uow.session.scalar(
                select(AnalystRunRecord.id).where(
                    AnalystRunRecord.workspace_id == WORKSPACE, AnalystRunRecord.valid.is_(True)
                )
            )
        # AI-3: lưu kết quả backtest đề xuất gắn với lượt phân tích hợp lệ; trùng đề xuất bị chặn.
        result = ProposalBacktestResult(
            bot_name="e2e",
            baseline_arm=BacktestArm("h03-v3-1h", (("donchian_lookback", 20),), Decimal("0.005")),
            variant_arm=BacktestArm("h03-v3-1h", (("donchian_lookback", 40),), Decimal("0.005")),
            baseline=aggregate([]),
            variant=aggregate([]),
            verdict="INCONCLUSIVE",
            notes=("e2e",),
            unsupported_fields=(),
            skipped_symbols=(),
            window=None,
        )
        record = {
            "workspace_id": WORKSPACE,
            "analyst_run_id": valid_run_id,
            "proposal_index": 0,
            "topic_key": f"family:{family}",
            "bot_name": "e2e",
            "proposal": {"changes": []},
            "result": result.payload(),
            "verdict": result.verdict,
            "version": PROPOSAL_BACKTEST_VERSION,
        }
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            uow.session.add(ProposalBacktestRecord(**record))
        duplicate_rejected = False
        try:
            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
                uow.session.add(ProposalBacktestRecord(**record))
        except IntegrityError:
            duplicate_rejected = True
    finally:
        await engine.dispose()
    with _connect() as connection:
        admin_web_can_select = connection.execute(
            "SELECT has_table_privilege('crypidian_admin_web', 'analyst_run', 'SELECT'), "
            "has_table_privilege('crypidian_admin_web', 'analyst_run', 'INSERT'), "
            "(SELECT relforcerowsecurity FROM pg_class WHERE relname = 'analyst_run'), "
            "has_table_privilege('crypidian_admin_web', 'proposal_backtest', 'SELECT'), "
            "has_table_privilege('crypidian_admin_web', 'proposal_backtest', 'INSERT'), "
            "has_table_privilege('crypidian_trading_runtime', 'proposal_backtest', 'SELECT'), "
            "(SELECT relforcerowsecurity FROM pg_class WHERE relname = 'proposal_backtest'), "
            "(SELECT count(*) FROM proposal_backtest)"
        ).fetchone()
    return {
        "runs": int(total or 0),
        "valid_runs": int(valid or 0),
        "admin_web_select": bool(admin_web_can_select[0]),
        "admin_web_insert": bool(admin_web_can_select[1]),
        "forced_rls": bool(admin_web_can_select[2]),
        "backtest_admin_web_select": bool(admin_web_can_select[3]),
        "backtest_admin_web_insert": bool(admin_web_can_select[4]),
        "backtest_runtime_select": bool(admin_web_can_select[5]),
        "backtest_forced_rls": bool(admin_web_can_select[6]),
        "backtests": int(admin_web_can_select[7]),
        "backtest_duplicate_rejected": duplicate_rejected,
    }


MACRO_EVENT_AT = datetime(2026, 11, 25, 13, 30, tzinfo=UTC)


async def macro_write() -> dict[str, Any]:
    """AI-4: ingestion-worker (role thật) ghi lần tải lịch vĩ mô; không được UPDATE/DELETE."""
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    from trading_bot.adapters.database.models import MacroCalendarFetchRecord
    from trading_bot.domain.research.macro_calendar import (
        BEA_SOURCE_ID,
        FOMC_SOURCE_ID,
        PFEI_SOURCE_ID,
        MacroEvent,
        events_hash,
    )
    from trading_bot.workers.macro_calendar_worker import database_macro_store_scope

    engine, sessions = _sessions()
    fetched_at = MACRO_EVENT_AT - timedelta(hours=6)
    try:
        scope = database_macro_store_scope(sessions, WORKSPACE)
        far = MACRO_EVENT_AT + timedelta(days=30)
        for source, events in (
            (
                BEA_SOURCE_ID,
                [
                    MacroEvent(BEA_SOURCE_ID, "gdp", "GDP", MACRO_EVENT_AT, True),
                    MacroEvent(BEA_SOURCE_ID, "gdp2", "GDP", far, True),
                ],
            ),
            (FOMC_SOURCE_ID, [MacroEvent(FOMC_SOURCE_ID, "fomc", "FOMC", far, True)]),
            (PFEI_SOURCE_ID, [MacroEvent(PFEI_SOURCE_ID, "cpi", "CPI", far, True)]),
        ):
            async with scope() as store:
                await store.insert(
                    {
                        "id": uuid4(),
                        "workspace_id": WORKSPACE,
                        "source_id": source,
                        "fetched_at": fetched_at,
                        "source_url": "https://example.invalid/macro",
                        "policy_fingerprint": "d" * 64,
                        "events_sha256": events_hash(events),
                        "event_count": len(events),
                        "coverage_end": far,
                        "events": [event.payload() for event in events],
                    }
                )
        update_denied = False
        try:
            async with scope() as store:
                await store._session.execute(update(MacroCalendarFetchRecord).values(event_count=0))
        except DBAPIError:
            update_denied = True
    finally:
        await engine.dispose()
    return {"written": 3, "update_denied": update_denied}


async def macro_read() -> dict[str, Any]:
    """AI-4: runtime (role thật) đọc trạng thái macro point-in-time."""
    from trading_bot.application.macro_calendar_state import load_macro_state
    from trading_bot.application.outbox import UnitOfWork

    engine, sessions = _sessions()
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            during = await load_macro_state(
                uow.session, workspace_id=WORKSPACE, decision_time=MACRO_EVENT_AT
            )
            clear = await load_macro_state(
                uow.session,
                workspace_id=WORKSPACE,
                decision_time=MACRO_EVENT_AT + timedelta(hours=2),
            )
            before_fetch = await load_macro_state(
                uow.session,
                workspace_id=WORKSPACE,
                decision_time=MACRO_EVENT_AT - timedelta(hours=7),
            )
    finally:
        await engine.dispose()
    with _connect() as connection:
        admin_insert = connection.execute(
            "SELECT has_table_privilege('crypidian_admin_web', 'macro_calendar_fetch', 'INSERT')"
        ).fetchone()[0]
    return {
        "during_blackout": during.blackout,
        "during_fresh": during.is_fresh,
        "evidence": len(during.evidence_ids),
        "after_blackout": clear.blackout,
        "before_fetch_unknown": before_fetch.blackout is None,
        "admin_web_insert": bool(admin_insert),
    }


DERIV_AT = datetime(2026, 11, 16, 8, 6, 40, tzinfo=UTC)


def _deriv_raw(symbol: str) -> dict[str, Any]:
    hour = 3_600_000
    t08 = 1794816000000
    return {
        "premium": {
            "symbol": symbol,
            "markPrice": "64772",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1794844800000,
            "time": 1794816400000,
        },
        "oi_hist": [
            {"symbol": symbol, "sumOpenInterest": "90", "sumOpenInterestValue": "1000",
             "timestamp": t08 - 24 * hour},
            {"symbol": symbol, "sumOpenInterest": "93", "sumOpenInterestValue": "1100",
             "timestamp": t08},
        ],
        "global_ls": [{"symbol": symbol, "longShortRatio": "2.5", "timestamp": t08}],
        "top_ls": [{"symbol": symbol, "longShortRatio": "1.5", "timestamp": t08}],
        "taker": [{"buySellRatio": "1.1", "timestamp": t08 - hour}],
    }  # fmt: skip


async def derivatives_write() -> dict[str, Any]:
    """AI-4 lát 2: ingestion-worker (role thật) ghi ảnh chụp phái sinh; không UPDATE/DELETE."""
    from uuid import uuid4

    from sqlalchemy import delete, update
    from sqlalchemy.exc import DBAPIError

    from trading_bot.adapters.database.models import DerivativesSnapshotRecord
    from trading_bot.domain.research.derivatives import build_snapshot, payload_hash
    from trading_bot.workers.derivatives_worker import database_derivatives_store_scope

    engine, sessions = _sessions()
    try:
        scope = database_derivatives_store_scope(sessions, WORKSPACE)
        async with scope() as store:
            for symbol in ("BTCUSDT", "ETHUSDT"):
                raw = _deriv_raw(symbol)
                await store.insert(
                    {
                        "id": uuid4(),
                        "workspace_id": WORKSPACE,
                        "policy_fingerprint": "f" * 64,
                        "payload_sha256": payload_hash(raw),
                        **build_snapshot(symbol, DERIV_AT, raw).record(),
                    }
                )
            last = await store.last_collected_at()
        denied = []
        for statement in (
            update(DerivativesSnapshotRecord).values(funding_rate=0),
            delete(DerivativesSnapshotRecord),
        ):
            try:
                async with scope() as store:
                    await store._session.execute(statement)
                denied.append(False)
            except DBAPIError:
                denied.append(True)
    finally:
        await engine.dispose()
    return {"written": 2, "last": last.isoformat(), "update_delete_denied": denied}


async def derivatives_read() -> dict[str, Any]:
    """AI-4 lát 2: runtime (role thật) đọc ảnh chụp point-in-time; admin-web không được ghi."""
    from trading_bot.application.derivatives_state import load_derivatives_snapshots
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.research.derivatives import derivatives_digest

    engine, sessions = _sessions()
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            now = await load_derivatives_snapshots(
                uow.session, workspace_id=WORKSPACE, as_of=DERIV_AT + timedelta(minutes=5)
            )
            before = await load_derivatives_snapshots(
                uow.session, workspace_id=WORKSPACE, as_of=DERIV_AT - timedelta(seconds=1)
            )
            other = await load_derivatives_snapshots(
                uow.session, workspace_id=uuid4(), as_of=DERIV_AT + timedelta(minutes=5)
            )
    finally:
        await engine.dispose()
    digest = derivatives_digest(now, DERIV_AT + timedelta(minutes=5), priority_symbols=["ETHUSDT"])
    with _connect() as connection:
        admin_insert = connection.execute(
            "SELECT has_table_privilege('crypidian_admin_web', 'derivatives_snapshot', 'INSERT')"
        ).fetchone()[0]
    return {
        "symbols": [row["symbol"] for row in digest["symbols"]],
        "oi_change": digest["symbols"][0]["oi_change_24h_pct"],
        "before_collect": len(before),
        "other_workspace": len(other),
        "admin_web_insert": bool(admin_insert),
    }


def _listing_symbols(zec_delivery: int) -> list[dict[str, Any]]:
    far = 4133404800000
    rows = [
        {
            "symbol": f"C{i}USDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "deliveryDate": far,
            "onboardDate": 1569398400000,
        }
        for i in range(10)
    ]
    rows.append(
        {
            "symbol": "ZECUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "deliveryDate": zec_delivery,
            "onboardDate": 1569398400000,
        }
    )
    return rows  # fmt: skip


LISTING_AT = datetime(2026, 11, 16, 9, tzinfo=UTC)


async def listings_write() -> dict[str, Any]:
    """AI-4 lát 3: ingestion-worker (role thật) ghi 2 lần kiểm exchangeInfo; không UPDATE/DELETE."""
    from sqlalchemy import delete, update
    from sqlalchemy.exc import DBAPIError

    from trading_bot.adapters.database.models import ListingSnapshotRecord
    from trading_bot.domain.research.listings import entries_hash, parse_exchange_info
    from trading_bot.workers.listings_worker import database_listings_store_scope

    engine, sessions = _sessions()
    try:
        scope = database_listings_store_scope(sessions, WORKSPACE)
        for offset, delivery in ((0, 4133404800000), (1, 1795161600000)):
            entries = parse_exchange_info({"symbols": _listing_symbols(delivery)})
            async with scope() as store:
                await store.insert(
                    {
                        "id": uuid4(),
                        "workspace_id": WORKSPACE,
                        "fetched_at": LISTING_AT + timedelta(hours=offset),
                        "policy_fingerprint": "c" * 64,
                        "entries_sha256": entries_hash(entries),
                        "entry_count": len(entries),
                        "changed": True,
                        "entries": [e.payload() for e in entries],
                    }
                )
        async with scope() as store:
            last = await store.last_check()
        denied = []
        for statement in (
            update(ListingSnapshotRecord).values(entry_count=1),
            delete(ListingSnapshotRecord),
        ):
            try:
                async with scope() as store:
                    await store._session.execute(statement)
                denied.append(False)
            except DBAPIError:
                denied.append(True)
    finally:
        await engine.dispose()
    return {"last": last[0].isoformat() if last else None, "update_delete_denied": denied}


async def listings_read() -> dict[str, Any]:
    """AI-4 lát 3: runtime (role thật) đọc lần kiểm point-in-time và phát hiện lịch gỡ ZEC."""
    from trading_bot.application.listings_state import load_listing_fetches, symbol_entry_block
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.research.listings import listings_digest

    engine, sessions = _sessions()
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            after = LISTING_AT + timedelta(hours=2)
            fetches, checked = await load_listing_fetches(
                uow.session, workspace_id=WORKSPACE, as_of=after
            )
            early, early_checked = await load_listing_fetches(
                uow.session, workspace_id=WORKSPACE, as_of=LISTING_AT + timedelta(minutes=30)
            )
            # Chặn vào lệnh trong runtime (role thật, RLS): ZEC bị lên lịch gỡ sau lần kiểm thứ hai.
            blocks = {}
            for label, symbol, at in (
                ("zec_after", "ZECUSDT", after),
                ("zec_before", "ZECUSDT", LISTING_AT + timedelta(minutes=30)),
                ("c0_after", "C0USDT", after),
                ("unlisted_after", "NOPEUSDT", after),
                ("no_data", "ZECUSDT", LISTING_AT - timedelta(hours=1)),
            ):
                block = await symbol_entry_block(
                    uow.session, workspace_id=WORKSPACE, symbol=symbol, as_of=at
                )
                blocks[label] = block.reason_code if block else None
    finally:
        await engine.dispose()
    digest = listings_digest(fetches, after, last_checked_at=checked, watch_symbols=["ZECUSDT"])
    before = listings_digest(
        early,
        LISTING_AT + timedelta(minutes=30),
        last_checked_at=early_checked,
        watch_symbols=["ZECUSDT"],
    )
    with _connect() as connection:
        admin_insert = connection.execute(
            "SELECT has_table_privilege('crypidian_admin_web', 'listing_snapshot', 'INSERT')"
        ).fetchone()[0]
    return {
        "at_risk": [(r["symbol"], r["delist_at_vn"]) for r in digest["watched_at_risk"]],
        "changes": [(c["kind"], c["symbol"]) for c in digest["recent_changes"]],
        "before_at_risk": before["watched_at_risk"],
        "admin_web_insert": bool(admin_insert),
        "blocks": blocks,
    }


async def outcome() -> dict[str, Any]:
    """16/11/2026: runtime (role thật) lưu lý do candidate, idempotent, RLS theo bot."""
    from sqlalchemy import func, select, text
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.exc import DBAPIError

    from trading_bot.adapters.database.models import BotDecisionRecord, PaperCandidateOutcomeRecord
    from trading_bot.application.outbox import UnitOfWork

    engine, sessions = _sessions()
    bot_id = UUID(STATE["bot_id"])
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE, bot_id=bot_id) as uow:
            decision_id = await uow.session.scalar(
                select(BotDecisionRecord.id).where(
                    BotDecisionRecord.workspace_id == WORKSPACE,
                    BotDecisionRecord.status == "CANDIDATE",
                )
            )
        statement = (
            insert(PaperCandidateOutcomeRecord)
            .values(
                id=uuid4(),
                workspace_id=WORKSPACE,
                bot_id=bot_id,
                account_id=ACCOUNT,
                decision_id=decision_id,
                stage="PLAN",
                reason_code="MARKET_DATA_STALE",
            )
            .on_conflict_do_nothing(constraint="uq_paper_candidate_outcome")
        )
        for _ in range(2):
            async with UnitOfWork(sessions, workspace_id=WORKSPACE, bot_id=bot_id) as uow:
                await uow.session.execute(statement)
        async with UnitOfWork(sessions, workspace_id=WORKSPACE, bot_id=bot_id) as uow:
            rows = await uow.session.scalar(
                select(func.count()).where(PaperCandidateOutcomeRecord.workspace_id == WORKSPACE)
            )
            user = await uow.session.scalar(text("SELECT current_user"))
        other_denied = False
        try:
            async with UnitOfWork(sessions, workspace_id=WORKSPACE, bot_id=uuid4()) as uow:
                await uow.session.execute(
                    insert(PaperCandidateOutcomeRecord).values(
                        id=uuid4(),
                        workspace_id=WORKSPACE,
                        bot_id=bot_id,
                        account_id=ACCOUNT,
                        decision_id=decision_id,
                        stage="RISK",
                        reason_code="OTHER_BOT_SCOPE",
                    )
                )
        except DBAPIError:
            other_denied = True
    finally:
        await engine.dispose()
    return {"rows": int(rows or 0), "user": str(user), "other_bot_denied": other_denied}


def pause_resume() -> dict[str, Any]:
    """Owner tạm dừng rồi tiếp tục bot (bản ghi append-only như luồng resume có MFA)."""
    with _connect() as connection, connection.transaction():
        bot_id = UUID(STATE["bot_id"])
        latest = connection.execute(
            "SELECT coalesce(max(revision), 0) FROM bot_runtime_control "
            "WHERE workspace_id=%s AND bot_id=%s",
            (WORKSPACE, bot_id),
        ).fetchone()[0]
        for offset, state_name, code in (
            (1, "PAUSED", "OPERATOR_PAUSE"),
            (2, "ENABLED", "OPERATOR_RESUME"),
        ):
            _insert(
                connection,
                "bot_runtime_control",
                workspace_id=WORKSPACE,
                bot_id=bot_id,
                account_id=ACCOUNT,
                revision=latest + offset,
                entry_state=state_name,
                reason_code=code,
                reason_text="E2E FX-2: mở lại sau ngắt mạch drawdown",
                changed_by=MEMBERSHIP,
                correlation_id=uuid4(),
            )
        resumed_at = connection.execute(
            "SELECT changed_at FROM bot_runtime_control WHERE workspace_id=%s AND bot_id=%s "
            "AND revision=%s",
            (WORKSPACE, bot_id, latest + 2),
        ).fetchone()[0]
    return {"resumed_at": resumed_at.astimezone(UTC).isoformat()}


def add_minutes() -> dict[str, Any]:
    """Ba nến 1m bình thường bắt đầu ở phút tròn đầu tiên sau `evaluated_at + 5s` của candidate."""
    with _connect() as connection, connection.transaction():
        evaluated_at = connection.execute(
            "SELECT evaluated_at FROM bot_decision WHERE workspace_id=%s AND status='CANDIDATE'",
            (WORKSPACE,),
        ).fetchone()[0]
        ready = evaluated_at.astimezone(UTC) + timedelta(seconds=5)
        base = ready.replace(second=0, microsecond=0) + timedelta(minutes=1)
        STATE["minute_base"] = base.isoformat()
        # Nến 1m lịch sử liền trước (trước eligible_at, không dùng để khớp): chốt liên tục của
        # vòng sự kiện cần nến liền trước để kiểm khoảng trống và bước nhảy giá.
        for index in range(-1, 3):
            _minute(connection, index)
    return {"minute_base": base.isoformat()}


def add_stop_bar() -> dict[str, Any]:
    with _connect() as connection, connection.transaction():
        _minute(connection, 3, low=250.0)
        # Nến sau stop (còn trong tương lai khi runtime chạy): không được dùng.
        _minute(connection, 4)
    return {"added": 2}


def verify() -> dict[str, Any]:
    with _connect() as connection:

        def rows(query: str) -> list[tuple[Any, ...]]:
            return connection.execute(query, (WORKSPACE,)).fetchall()

        decisions = rows("SELECT status FROM bot_decision WHERE workspace_id=%s")
        intents = rows(
            "SELECT id, status, quantity, filled_quantity, feature_snapshot_id, "
            "evidence->>'context_provider', evidence->>'bot_decision_id' "
            "FROM order_intent WHERE workspace_id=%s"
        )
        trades = rows(
            "SELECT status, opened_at, closed_at, net_pnl, gross_pnl, total_cost "
            "FROM trade WHERE workspace_id=%s"
        )
        fills = rows(
            "SELECT fill_role, quantity, price, occurred_at, bar_key "
            "FROM fill WHERE workspace_id=%s ORDER BY occurred_at"
        )
        exit_reasons = rows(
            "SELECT payload->>'exit_reason' FROM position_event "
            "WHERE workspace_id=%s AND event_key LIKE 'exit-fill:%%'"
        )
        reservations = rows("SELECT status FROM risk_reservation WHERE workspace_id=%s")
        venue_rows = {
            table: int(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE workspace_id=%s").format(
                        sql.Identifier(table)
                    ),
                    (WORKSPACE,),
                ).fetchone()[0]
            )
            for table in ("exchange_order", "venue_order_intent")
        }
        # `exchange_order` có một hàng gương PAPER do ledger tự ghi khi tạo intent; không có ID
        # lệnh sàn nào (chứng minh không có lệnh thật được gửi).
        venue_rows["exchange_order_with_venue_id"] = int(
            connection.execute(
                "SELECT count(*) FROM exchange_order WHERE workspace_id=%s "
                "AND (mode <> 'PAPER' OR exchange_order_id IS NOT NULL "
                "OR client_order_id NOT LIKE 'paper-%%')",
                (WORKSPACE,),
            ).fetchone()[0]
        )
        checkpoints = rows("SELECT count(*) FROM paper_execution_checkpoint WHERE workspace_id=%s")[
            0
        ][0]
        minute_open = {index: _minute_open(index) for index in range(5)}
    return {
        "decisions": sorted(row[0] for row in decisions),
        "intents": [
            {
                "status": row[1],
                "full": row[2] == row[3],
                "feature_snapshot_id_len": len(row[4]),
                "context_provider": row[5],
                "has_bot_decision": row[6] is not None,
            }
            for row in intents
        ],
        "trades": [
            {
                "status": row[0],
                "opened_at": row[1].isoformat(),
                "closed_at": None if row[2] is None else row[2].isoformat(),
                "pnl_consistent": row[3] == row[4] - row[5],
                "loss": row[3] < 0,
            }
            for row in trades
        ],
        "fills": [
            {
                "role": row[0],
                "price": str(row[2]),
                "occurred_at": row[3].isoformat(),
                "bar_source": str(row[4]).split(":")[0],
            }
            for row in fills
        ],
        "exit_reasons": [row[0] for row in exit_reasons],
        "reservations": sorted(row[0] for row in reservations),
        "venue_rows": venue_rows,
        "checkpoints": int(checkpoints),
        "minute_open": {key: value.isoformat() for key, value in minute_open.items()},
    }


def tg_seed() -> dict[str, Any]:
    """TG-3: chủ workspace liên kết Telegram và bật nhận tin cho bot RT1 (lọc BTCUSDT)."""
    bot_id = UUID(STATE["bot_id"])
    with _connect() as connection, connection.transaction():
        _insert(
            connection,
            "telegram_binding",
            id=TELEGRAM_BINDING,
            workspace_id=WORKSPACE,
            membership_id=MEMBERSHIP,
            telegram_user_id=str(TELEGRAM_CHAT),
            verified_at=datetime.now(UTC),
            revoked_at=None,
        )
        _insert(
            connection,
            "telegram_signal_subscription",
            id=TELEGRAM_SUBSCRIPTION,
            workspace_id=WORKSPACE,
            membership_id=MEMBERSHIP,
            bot_id=bot_id,
            telegram_binding_id=TELEGRAM_BINDING,
            telegram_chat_id=TELEGRAM_CHAT,
            enabled=True,
            symbols=Jsonb(["BTCUSDT"]),
        )
    return {"tg_seeded": True}


async def tg_results() -> dict[str, Any]:
    """TG-3: worker kết quả PAPER thật dưới role signal-worker, gửi tới sender ghi nhớ."""
    from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
    from trading_bot.workers.telegram_trade_result_worker import TelegramTradeResultWorker

    engine, sessions = _sessions()

    def worker(name: str, sender: Any) -> Any:
        return TelegramTradeResultWorker(
            session_factory=sessions,
            workspace_id=WORKSPACE,
            sender=sender,
            worker_id=name,
            max_event_age=timedelta(minutes=60),
            report_interval=timedelta(hours=4),
        )

    try:
        first_sender = RecordingTelegramSender()
        first = worker("rt1-tg3-a", first_sender)
        sent_first = await first.tick()
        sent_again = await first.tick()
        restart_sender = RecordingTelegramSender()
        sent_restart = await worker("rt1-tg3-b", restart_sender).tick()
    finally:
        await engine.dispose()
    texts = [text for _, text in first_sender.messages]
    closes = [text for text in texts if "Đóng lệnh PAPER" in text]
    opens = [text for text in texts if "Mở vị thế PAPER" in text]
    summaries = [text for text in texts if text.startswith("TỔNG HỢP KẾT QUẢ PAPER")]
    close_text = closes[0] if len(closes) == 1 else ""
    bot_id = UUID(STATE["bot_id"])
    with _connect() as connection:

        def scalar(query: str, params: tuple[Any, ...] = ()) -> Any:
            row = connection.execute(query, params).fetchone()
            assert row is not None
            return row[0]

        privileges = {
            "trade_table_select": scalar(
                "SELECT has_table_privilege(current_user, 'trade', 'SELECT')"
            ),
            "trade_net_pnl_select": scalar(
                "SELECT has_column_privilege(current_user, 'trade', 'net_pnl', 'SELECT')"
            ),
            "trade_lineage_select": scalar(
                "SELECT has_column_privilege(current_user, 'trade', 'lineage', 'SELECT')"
            ),
            "trade_any_update": scalar(
                "SELECT has_any_column_privilege(current_user, 'trade', 'UPDATE')"
            ),
            "fill_any_insert": scalar(
                "SELECT has_any_column_privilege(current_user, 'fill', 'INSERT')"
            ),
            "order_intent_any_select": scalar(
                "SELECT has_any_column_privilege(current_user, 'order_intent', 'SELECT')"
            ),
        }
        connection.execute(
            "SELECT set_config('app.workspace_id', %s, false), set_config('app.bot_id', %s, false)",
            (str(WORKSPACE), str(bot_id)),
        )
        visible_own = scalar("SELECT count(id) FROM trade WHERE workspace_id=%s", (WORKSPACE,))
        connection.execute("SELECT set_config('app.bot_id', %s, false)", (str(uuid4()),))
        visible_other = scalar("SELECT count(id) FROM trade WHERE workspace_id=%s", (WORKSPACE,))
        try:
            connection.execute("SELECT lineage FROM trade LIMIT 1")
            lineage_denied = False
        except psycopg.errors.InsufficientPrivilege:
            lineage_denied = True
    return {
        "sent": [sent_first, sent_again, sent_restart],
        "recipients": sorted({chat for chat, _ in first_sender.messages}),
        "restart_messages": len(restart_sender.messages),
        "opens": len(opens),
        "closes": len(closes),
        "summaries": len(summaries),
        "close_header": close_text.splitlines()[0] if close_text else None,
        "close_reason": "Lý do thoát: Chạm dừng lỗ (SL) (STOP_LOSS)" in close_text,
        "close_loss_pnl": "PnL ròng (sau phí): -" in close_text,
        "close_r_negative": "Bội số R: -" in close_text,
        "summary_lists_bot": bool(summaries) and "1. " in summaries[0],
        "max_length": max((len(text) for text in texts), default=0),
        "privileges": privileges,
        "rls": {"own_bot": int(visible_own), "other_bot": int(visible_other)},
        "lineage_denied": lineage_denied,
    }


def tg_verify() -> dict[str, Any]:
    with _connect() as connection:
        notifications = connection.execute(
            "SELECT kind, status, attempts, telegram_chat_id = %s FROM telegram_trade_notification "
            "JOIN telegram_signal_subscription s ON s.workspace_id = "
            "telegram_trade_notification.workspace_id AND s.id = subscription_id "
            "WHERE telegram_trade_notification.workspace_id=%s ORDER BY kind",
            (TELEGRAM_CHAT, WORKSPACE),
        ).fetchall()
        reports = connection.execute(
            "SELECT status, attempts, telegram_chat_id, window_end - window_start "
            "FROM telegram_paper_report_delivery WHERE workspace_id=%s",
            (WORKSPACE,),
        ).fetchall()
    return {
        "notifications": [list(row) for row in notifications],
        "reports": [[row[0], row[1], row[2] == TELEGRAM_CHAT, str(row[3])] for row in reports],
    }


phase = os.environ["RT1_PHASE"]
if phase == "seed":
    output = seed()
elif phase == "genesis":
    output = asyncio.run(genesis())
elif phase == "produce":
    output = produce()
elif phase == "runtime":
    output = asyncio.run(runtime())
elif phase == "mark_check":
    output = asyncio.run(mark_check())
elif phase == "breaker":
    output = asyncio.run(breaker())
elif phase == "analyst":
    output = asyncio.run(analyst())
elif phase == "macro_write":
    output = asyncio.run(macro_write())
elif phase == "macro_read":
    output = asyncio.run(macro_read())
elif phase == "derivatives_write":
    output = asyncio.run(derivatives_write())
elif phase == "derivatives_read":
    output = asyncio.run(derivatives_read())
elif phase == "listings_write":
    output = asyncio.run(listings_write())
elif phase == "listings_read":
    output = asyncio.run(listings_read())
elif phase == "outcome":
    output = asyncio.run(outcome())
elif phase == "run_report":
    output = asyncio.run(run_report())
elif phase == "pause_resume":
    output = pause_resume()
elif phase == "add_minutes":
    output = add_minutes()
elif phase == "add_stop_bar":
    output = add_stop_bar()
elif phase == "verify":
    output = verify()
elif phase == "tg_seed":
    output = tg_seed()
elif phase == "tg_results":
    output = asyncio.run(tg_results())
elif phase == "tg_verify":
    output = tg_verify()
else:
    raise AssertionError(phase)
print(json.dumps(output, default=str))
