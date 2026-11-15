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
from sqlalchemy import text

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


async def web_read() -> dict[str, Any]:
    """CK-7: admin-web (role thật, RLS thật) đọc vị thế mở và quyết định tại nến cho chart."""
    from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
    from trading_bot.application.bot_reporting import build_latest_signal_view
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.portfolio.position_overlay import position_overlay

    bot_id = UUID(STATE["bot_id"])
    engine, sessions = _sessions()
    try:
        # Như BotReportingService: RLS của admin-web yêu cầu membership đang hoạt động có quyền.
        async with UnitOfWork(
            sessions,
            workspace_id=WORKSPACE,
            bot_id=bot_id,
            actor_membership_id=MEMBERSHIP,
            snapshot_read_only=True,
        ) as uow:
            repository = BotReportRepository(uow.session)
            positions = await repository.open_positions(
                workspace_id=WORKSPACE, bot_id=bot_id, symbol="BTCUSDT"
            )
            decisions = await repository.bot_decisions_between(
                workspace_id=WORKSPACE,
                bot_id=bot_id,
                start=LAST_CLOSE - timedelta(hours=2),
                end=NOW + timedelta(minutes=30),
            )
            role = await uow.session.scalar(text("SELECT current_user"))
        async with UnitOfWork(
            sessions, workspace_id=WORKSPACE, bot_id=bot_id, snapshot_read_only=True
        ) as uow:
            hidden = await BotReportRepository(uow.session).open_positions(
                workspace_id=WORKSPACE, bot_id=bot_id, symbol="BTCUSDT"
            )
    finally:
        await engine.dispose()
    latest = build_latest_signal_view(decisions)["latest_decision"]
    views = []
    for position in positions:
        entry = position["entry_fills"][0]["price"]
        views.append(position_overlay(position, mark_price=entry + 1, mark_source="mark"))
    return {
        "role": role,
        "positions": [
            {
                "side": view["side"],
                "fills": len(position["entry_fills"]),
                "has_initial_stop": view["initial_stop"] is not None,
                "stop_below_entry": view["side"] != "LONG" or view["stop"] < view["entry_price"],
                "upnl_matches_quantity": abs(view["upnl"] - view["quantity"]) < 1e-9,
            }
            for position, view in zip(positions, views, strict=True)
        ],
        "decision_status": None if latest is None else latest["status"],
        "decision_rows": len(decisions),
        "hidden_without_membership": len(hidden),
    }


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
elif phase == "web_read":
    output = asyncio.run(web_read())
elif phase == "add_minutes":
    output = add_minutes()
elif phase == "add_stop_bar":
    output = add_stop_bar()
elif phase == "verify":
    output = verify()
else:
    raise AssertionError(phase)
print(json.dumps(output, default=str))
