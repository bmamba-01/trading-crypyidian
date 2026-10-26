"""Probe MB05.5–05.9: runtime PAPER nhiều bot trên PostgreSQL thật (Compose).

Mã probe gửi qua stdin (giới hạn dòng lệnh Windows); pha chọn bằng `MB05B_PHASE`, state giữa các
pha qua `MB05B_STATE` (JSON). Mỗi pha chạy đúng container/role của nó:

- `seed` (migrator, superuser): workspace (bot A, B ENABLED; C DISABLED), workspace khác, thành
  viên/grant, root settings + risk profile, release, seed vốn, feature snapshot, instrument và nến
  mark mỗi phút (để định giá vị thế mở cho mẫu số equity MB-04).
- `genesis` (trading-runtime): checkpoint genesis NULL token (revision 1) cho A, B; NULL ở revision
  sau bị trigger `genesis_null` từ chối.
- các pha `runtime_*` (trading-runtime): dựng app thật (`create_app`, registry, supervisor) với
  context provider + decision cycle tổng hợp (strategy stub; planner, RiskEngine, coordinator
  MB-04, ledger, checkpoint, fencing là code thật).
- các pha `admin_*` (admin-web): `BotRuntimeControlService`, `BotLifecycleGuard`, resolution
  incident dưới role `crypidian_admin_web` thật.
- `race` (migrator, `-c role=` theo từng engine): entry/fill của runtime đua với pause/kill của
  admin trong cùng process để có barrier (ruling 27).
- `inspect` (migrator): đọc DB bỏ qua RLS để kiểm audit/lease/state/ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

PHASE = os.environ.get("MB05B_PHASE", "")
STATE: dict[str, Any] = json.loads(os.environ.get("MB05B_STATE", "{}"))
HARD_CAPS = {
    "schema_version": 1,
    "hard_caps": {
        "max_aggregate_open_risk": "1000",
        "max_notional": "10000",
        "max_leverage": "5",
        "max_positions": 6,
        "max_symbol_notional": "10000",
        "max_concentration_fraction": "1",
        "max_daily_loss": "1000",
        "max_rolling_loss": "2000",
        "max_account_age_seconds": 30,
        "max_data_age_seconds": 300,
    },
}
RELEASE = "h03-v1"
MARK_MINUTES = 240


# --- kết nối ---------------------------------------------------------------------------------
def _password() -> str:
    return Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()


def _url(user: str | None = None) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=user or os.environ["TRADING_BOT_DATABASE_USER"],
        password=_password(),
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )


def _engine(role: str | None = None, pool: int = 5) -> AsyncEngine:
    connect_args = {"options": f"-c role={role}"} if role else {}
    return create_async_engine(_url(), pool_size=pool, connect_args=connect_args)


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=_password(),
        autocommit=True,
    )


def _insert(connection: psycopg.Connection, table: str, **values: object) -> None:
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ),
        tuple(values.values()),
    )


def _code(error: BaseException) -> str:
    code = getattr(error, "reason_code", None)
    if isinstance(code, str):
        return code
    if isinstance(error, DBAPIError):
        message = str(error.orig)
        for marker in (
            "MB05_STALE_FENCING_TOKEN",
            "MB05_FENCING_TOKEN_REQUIRED",
            "MB05_LEASE_EXPIRED",
        ):
            if marker in message:
                return marker
        return f"SQLSTATE_{getattr(error.orig, 'sqlstate', '?')}"
    return type(error).__name__


async def _outcome(coroutine: Any) -> Any:
    try:
        return await coroutine
    except Exception as error:
        return {"error": _code(error)}


def _at(minutes: int, seconds: int = 5) -> datetime:
    base = datetime.fromisoformat(STATE["t0"])
    return base + timedelta(minutes=minutes, seconds=seconds)


# --- seed ------------------------------------------------------------------------------------
def _seed_workspace(connection: psycopg.Connection, name: str, fraction: str) -> dict[str, Any]:
    from trading_bot.adapters.database.repositories.workspace_settings import (
        canonical_settings,
        settings_hash,
    )
    from trading_bot.config.workspace_settings import WorkspaceSettings
    from trading_bot.domain.strategy.registry import StrategyRegistry

    t0 = datetime.fromisoformat(STATE["t0"])
    workspace = uuid4()
    _insert(
        connection,
        "workspace",
        id=workspace,
        name=f"MB05B {name}",
        slug=f"mb05b-{name}-{workspace.hex[:8]}",
        mode="PAPER",
        status="ACTIVE",
    )
    members: dict[str, str] = {}
    for role in ("owner", "risk_admin", "operator", "analyst_reviewer", "strategy_admin"):
        user, member = uuid4(), uuid4()
        _insert(
            connection,
            "user_account",
            id=user,
            email_normalized=f"mb05b-{name}-{role}-{user.hex[:6]}@example.test",
            password_hash="unused",
            status="ACTIVE",
        )
        _insert(
            connection,
            "user_membership",
            id=member,
            workspace_id=workspace,
            user_id=user,
            status="ACTIVE",
        )
        _insert(
            connection,
            "role_binding",
            id=uuid4(),
            workspace_id=workspace,
            membership_id=member,
            role=role,
        )
        members[role] = str(member)
    profile = uuid4()
    _insert(
        connection,
        "risk_profile_version",
        id=profile,
        workspace_id=workspace,
        name="MB05B risk",
        version=1,
        max_risk_fraction=Decimal("0.01"),
        settings=Jsonb(HARD_CAPS),
        is_active=True,
    )
    settings = WorkspaceSettings.model_validate(
        {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT"],
                "timeframes": ["1h", "4h"],
                "max_age_seconds": 30,
            },
            "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
            "strategy": {
                "default_release": RELEASE,
                "parameters": {},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
            "risk": {
                "profile_version_id": str(profile),
                "workspace_max_open_risk_fraction": fraction,
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
    settings_id = uuid4()
    effective_at = t0 - timedelta(days=1)
    owner = UUID(members["owner"])
    _insert(
        connection,
        "workspace_settings_version",
        id=settings_id,
        workspace_id=workspace,
        version=1,
        schema_version=settings.schema_version,
        settings_json=Jsonb(payload),
        settings_hash=root_hash,
        status="PUBLISHED",
        revision=1,
        created_by=owner,
        approved_by=owner,
        published_at=effective_at,
        effective_at=effective_at,
    )
    _insert(
        connection,
        "strategy_release",
        id=uuid4(),
        workspace_id=workspace,
        name=RELEASE,
        version=1,
        status="PUBLISHED",
        recipe=Jsonb({}),
        content_hash=StrategyRegistry().get_release(RELEASE).content_hash,
    )
    accounts: dict[str, str] = {}
    bots: dict[str, str] = {}
    for key, status in (("a", "ENABLED"), ("b", "ENABLED"), ("c", "DISABLED")):
        account = uuid4()
        _insert(
            connection,
            "trading_account",
            id=account,
            workspace_id=workspace,
            name=f"mb05b-{name}-{key}",
            venue="binance_usdm",
            mode="PAPER",
            environment="local",
            status=status,
            max_notional=10000,
        )
        bot = connection.execute(
            "SELECT id FROM bot_instance WHERE trading_account_id=%s", (account,)
        ).fetchone()[0]
        _insert(
            connection,
            "paper_account_seed",
            id=uuid4(),
            bot_id=bot,
            workspace_id=workspace,
            trading_account_id=account,
            quote_asset="USDT",
            initial_equity=Decimal("10000"),
            source_settings_version_id=settings_id,
            source_settings_hash=root_hash,
            idempotency_key=f"paper-seed:{account}",
        )
        accounts[key] = str(account)
        bots[key] = str(bot)
    # Operator: pause + resume bot A (ruling 20); analyst: chỉ đọc bot A.
    for permission in ("bot:read", "bot:runtime:pause", "bot:runtime:resume"):
        _insert(
            connection,
            "bot_grant",
            id=uuid4(),
            workspace_id=workspace,
            bot_id=UUID(bots["a"]),
            membership_id=UUID(members["operator"]),
            permission=permission,
            granted_by=owner,
        )
    _insert(
        connection,
        "bot_grant",
        id=uuid4(),
        workspace_id=workspace,
        bot_id=UUID(bots["a"]),
        membership_id=UUID(members["analyst_reviewer"]),
        permission="bot:read",
        granted_by=owner,
    )
    snapshots = [_seed_feature_snapshot(connection, workspace, index) for index in range(40)]
    _seed_marks(connection, workspace, t0)
    return {
        "workspace": str(workspace),
        "members": members,
        "accounts": accounts,
        "bots": bots,
        "settings_version_id": str(settings_id),
        "snapshots": snapshots,
    }


def _seed_feature_snapshot(connection: psycopg.Connection, workspace: UUID, index: int) -> str:
    from dataclasses import replace

    from trading_bot.domain.market.feature_snapshot import (
        DataFreshness,
        FeatureSnapshot,
        MarketRegime,
        RegimeAssessment,
    )

    t0 = datetime.fromisoformat(STATE["t0"])
    provisional = FeatureSnapshot(
        snapshot_id="0" * 64,
        content_hash="0" * 64,
        workspace_id=workspace,
        symbol="BTCUSDT",
        timeframe="1h",
        as_of=t0 - timedelta(hours=1, minutes=index),
        computed_at=t0 - timedelta(hours=1, minutes=index) + timedelta(seconds=1),
        schema_hash="a" * 64,
        formula_hash="b" * 64,
        code_hash="c" * 64,
        input_bar_ids=(f"mb05b-bar-{index}",),
        input_event_ids=(f"mb05b-event-{index}",),
        features={"atr_14": 2.0},
        warmup={"atr_14": True},
        quality_flags=(),
        data_freshness=DataFreshness.FRESH,
        data_coverage=1.0,
        regime=RegimeAssessment(MarketRegime.TREND, 0.8, 1.0, "MB05B_FIXTURE", "Dữ liệu E2E."),
        is_usable=True,
    )
    digest = hashlib.sha256(provisional.canonical_json().encode()).hexdigest()
    snapshot = replace(provisional, snapshot_id=digest, content_hash=digest)
    payload = snapshot.canonical_payload()
    _insert(
        connection,
        "feature_snapshot",
        id=uuid4(),
        workspace_id=workspace,
        snapshot_id=snapshot.snapshot_id,
        content_hash=snapshot.content_hash,
        symbol=snapshot.symbol,
        timeframe=snapshot.timeframe,
        as_of=snapshot.as_of,
        computed_at=snapshot.computed_at,
        schema_hash=snapshot.schema_hash,
        formula_hash=snapshot.formula_hash,
        code_hash=snapshot.code_hash,
        input_bar_ids=Jsonb(list(snapshot.input_bar_ids)),
        input_event_ids=Jsonb(list(snapshot.input_event_ids)),
        features=Jsonb(dict(snapshot.features)),
        warmup=Jsonb(dict(snapshot.warmup)),
        quality_flags=Jsonb(list(snapshot.quality_flags)),
        data_freshness=snapshot.data_freshness.value,
        data_coverage=snapshot.data_coverage,
        regime=Jsonb(payload["regime"]),
        is_usable=True,
        canonical_json=snapshot.canonical_json(),
    )
    return snapshot.snapshot_id


def _seed_marks(connection: psycopg.Connection, workspace: UUID, t0: datetime) -> None:
    """Instrument + nến 1m mỗi phút: mark cho vị thế mở trong cửa sổ `max_equity_age`."""
    instrument = uuid4()
    listed = t0 - timedelta(days=2)
    _insert(
        connection,
        "market_instrument",
        id=instrument,
        workspace_id=workspace,
        source="BINANCE_USDM",
        idempotency_key=f"mb05b-instrument-{instrument}",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_notional=Decimal("5"),
        status="TRADING",
        revision=1,
        checksum="d" * 64,
        event_at=listed,
        received_at=listed,
        available_at=listed,
    )
    with connection.cursor() as cursor:
        rows = []
        for minute in range(-5, MARK_MINUTES):
            closed = t0 + timedelta(minutes=minute)
            rows.append(
                (
                    uuid4(),
                    workspace,
                    instrument,
                    "BTCUSDT",
                    "1m",
                    "MB05B_E2E",
                    f"mb05b:{minute}",
                    f"mb05b-bar:{instrument}:{minute}",
                    1,
                    "e" * 64,
                    closed - timedelta(minutes=1),
                    closed,
                    closed,
                    closed,
                    closed,
                    Decimal("100"),
                    Decimal("100"),
                    Decimal("100"),
                    Decimal("100"),
                    Decimal("1"),
                    True,
                )
            )
        cursor.executemany(
            "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, source, "
            "logical_key, idempotency_key, revision, checksum, open_time, close_time, event_at, "
            "received_at, available_at, open, high, low, close, volume, is_closed) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )


def seed() -> dict[str, Any]:
    t0 = datetime.now(UTC).replace(second=0, microsecond=0)
    STATE["t0"] = t0.isoformat()
    variants = STATE.get("variants") or {"main": "1"}
    with _connect() as connection, connection.transaction():
        workspaces = {
            name: _seed_workspace(connection, name, fraction) for name, fraction in variants.items()
        }
    return {"t0": t0.isoformat(), "workspaces": workspaces}


# --- runtime: context/decision tổng hợp ------------------------------------------------------
class _StubStrategy:
    def __init__(self, release_id: str) -> None:
        self.release_id = release_id

    def evaluate(self, snapshot: Any, selection: Any) -> Any:
        from trading_bot.domain.strategy.contracts import StrategyCandidate
        from trading_bot.domain.strategy.recipes import ExitRecipe
        from trading_bot.domain.strategy.registry import StrategyRegistry

        decided = snapshot.decision_time
        return StrategyCandidate(
            strategy_id="H03",
            strategy_release_id=self.release_id,
            release_content_hash=StrategyRegistry().get_release(self.release_id).content_hash,
            settings_version_id=selection.settings_version_id,
            snapshot_id=snapshot.snapshot_id,
            symbol=snapshot.symbol,
            direction="long",
            reference_price=Decimal("100"),
            atr_value=2.0,
            signal_at=decided,
            eligible_after=decided + timedelta(seconds=1),
            expires_at=decided + timedelta(hours=6),
            recipe=ExitRecipe("exit-1", 2),
            evidence_feature_ids=(f"{snapshot.snapshot_id}:atr",),
            evidence_bar_keys=("BTCUSDT:1h:mb05b",),
            macro_evidence_ids=(),
            geometry_version=None,
            reason_code="H03_LONG_ENTRY",
            detail_vi="Ứng viên tổng hợp cho E2E MB-05.",
        )


class _StubRegistry:
    def resolve(self, release_id: str, selection: Any) -> _StubStrategy:
        del selection
        return _StubStrategy(release_id)


def _cycle(gateway: Any) -> Any:
    from trading_bot.application.paper_decision_cycle import PaperDecisionCycle
    from trading_bot.domain.risk.engine import RiskEngine
    from trading_bot.domain.risk.planner import TradePlanner

    return PaperDecisionCycle(_StubRegistry(), TradePlanner(), RiskEngine(), gateway)  # type: ignore[arg-type]


class SyntheticContextProvider:
    """Context point-in-time nhất quán với ledger thật (reservation/vị thế của account)."""

    async def load_context(
        self,
        *,
        uow: Any,
        command: Any,
        published_settings: Any,
        execution_profile: Any,
        account: Any,
    ) -> Any:
        from trading_bot.adapters.database.models import TradeRecord
        from trading_bot.adapters.database.models.risk_entities import RiskReservationRecord
        from trading_bot.adapters.database.repositories.risk_reservations import (
            RiskReservationRepository,
        )
        from trading_bot.application.paper_runtime_application import PaperDecisionContext
        from trading_bot.application.runtime_risk_profile import load_risk_profile
        from trading_bot.domain.risk.engine import RiskState
        from trading_bot.domain.risk.planner import (
            AccountSnapshot,
            ExchangeRules,
            OpenExposure,
            PlannerSettings,
        )
        from trading_bot.domain.strategy.contracts import DecisionSnapshot, MarketSeries
        from trading_bot.domain.strategy.registry import StrategyRegistry

        published = published_settings
        workspace = str(published.workspace_id)
        version = str(published.settings_version_id)
        decided = command.evaluated_at - timedelta(seconds=1)
        profile = await load_risk_profile(
            uow.session,
            workspace_id=published.workspace_id,
            profile_id=published.settings.risk.profile_version_id,
        )
        release = published.settings.strategy.default_release
        planner = PlannerSettings(
            workspace_id=workspace,
            settings_version_id=version,
            risk_profile_version_id=str(published.settings.risk.profile_version_id),
            published=True,
            active_release_ids=(release,),
            leverage=Decimal("5"),
            exchange=ExchangeRules(
                Decimal("0.1"),
                Decimal("0.001"),
                Decimal("1"),
                Decimal("5"),
                Decimal("10000"),
                Decimal("1000"),
            ),
            limits=profile.limits,
            release_content_hashes=(
                (release, StrategyRegistry().get_release(release).content_hash),
            ),
            settings_hash=published.settings_hash,
        )
        snapshot_account = AccountSnapshot(
            workspace,
            str(account.account_id),
            version,
            decided,
            Decimal("10000"),
            Decimal("10000"),
            True,
        )
        totals = await RiskReservationRepository(uow.session)._active_totals(
            workspace_id=published.workspace_id, account_id=account.account_id, symbol="BTCUSDT"
        )
        trades = (
            await uow.session.execute(
                select(
                    TradeRecord.symbol,
                    RiskReservationRecord.reserved_notional,
                    RiskReservationRecord.reserved_risk,
                )
                .join(
                    RiskReservationRecord,
                    (RiskReservationRecord.workspace_id == TradeRecord.workspace_id)
                    & (RiskReservationRecord.id == TradeRecord.risk_reservation_id),
                )
                .where(
                    TradeRecord.workspace_id == published.workspace_id,
                    TradeRecord.account_id == account.account_id,
                    TradeRecord.status == "OPEN",
                )
            )
        ).all()
        exposures = tuple(
            OpenExposure(
                workspace, str(account.account_id), symbol, Decimal(notional), Decimal(risk)
            )
            for symbol, notional, risk in trades
        )
        state = RiskState(
            workspace_id=workspace,
            account_id=str(account.account_id),
            settings_version_id=version,
            risk_profile_version_id=str(published.settings.risk.profile_version_id),
            evaluated_at=command.evaluated_at,
            account_captured_at=decided,
            market_data_as_of=decided - timedelta(seconds=1),
            equity=Decimal("10000"),
            available_margin=Decimal("10000"),
            open_exposures=exposures,
            realized_loss_today=Decimal("0"),
            realized_loss_rolling=Decimal("0"),
            open_position_count=len(exposures),
            is_known=True,
            pending_reservation_initial_margin=totals.initial_margin,
            pending_reservation_risk=totals.risk,
            pending_reservation_count=totals.count,
            pending_reservation_notional=totals.notional,
            pending_symbol_reservation_notional=totals.symbol_notional,
        )
        return PaperDecisionContext(
            snapshot=DecisionSnapshot(
                symbol="BTCUSDT",
                decision_time=decided,
                snapshot_id=command.feature_snapshot_id,
                series=(MarketSeries("1h", ()),),
                features=(),
            ),
            planner_settings=planner,
            account=snapshot_account,
            risk_state=state,
            account_quote_asset="USDT",
            open_exposures=exposures,
            evidence={"feature_snapshot_id": command.feature_snapshot_id, "probe": "mb05b"},
        )

    async def load_monitor_evidence(
        self,
        *,
        command: Any,
        published_settings: Any,
        account: Any,
        pinned_strategy_release_name: Any,
        pinned_strategy_release_hash: Any,
        pinned_strategy_settings_version_id: Any,
        pinned_strategy_parameters: Any,
        **_: Any,
    ) -> Any:
        from trading_bot.application.paper_runtime_application import PaperMonitorEvidence
        from trading_bot.domain.strategy.registry import StrategyRegistry

        release = pinned_strategy_release_name or RELEASE
        return PaperMonitorEvidence(
            workspace_id=published_settings.workspace_id,
            account_id=account.account_id,
            feature_snapshot_id=command.feature_snapshot_id,
            symbol=command.bar.symbol,
            available_at=command.bar.opened_at - timedelta(seconds=1),
            strategy_release_name=release,
            strategy_release_hash=pinned_strategy_release_hash
            or StrategyRegistry().get_release(release).content_hash,
            opposing_signal=False,
            atr=Decimal("2"),
            strategy_settings_version_id=pinned_strategy_settings_version_id,
            strategy_parameters=pinned_strategy_parameters,
        )


def _ws(name: str = "main") -> dict[str, Any]:
    return STATE["workspaces"][name]


@contextlib.asynccontextmanager
async def runtime_app(
    name: str = "main",
    *,
    ttl: int = 30,
    tick: float = 20.0,
    sessions: Any = None,
    supervisor_factory: Any = None,
) -> AsyncIterator[Any]:
    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    engine = None
    if sessions is None:
        engine = _engine(pool=8)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(
        AppSettings(
            process_role="trading-runtime",
            runtime_workspace_id=UUID(_ws(name)["workspace"]),
            runtime_lease_ttl_seconds=ttl,
            runtime_supervisor_tick_seconds=tick,
        ),
        session_factory=sessions,
        paper_decision_context_provider_factory=lambda **_: SyntheticContextProvider(),
        paper_runtime_supervisor_factory=supervisor_factory,
    )
    try:
        async with app.router.lifespan_context(app):
            if app.state.paper_application is not None:
                app.state.paper_application._decision_cycle_factory = _cycle
                _trace_coordinators(app)
            yield app
    finally:
        if engine is not None:
            await engine.dispose()


def _trace_coordinators(app: Any) -> None:
    """Chẩn đoán: in lý do lỗi lifecycle (bị nuốt thành PAPER_COMMAND_REJECTED) ra stderr."""
    import sys

    for account in app.state.paper_application._active_accounts.values():
        coordinator = account.coordinator
        for method in ("submit_approved", "apply_entry_market_event", "monitor_market_event"):
            original = getattr(coordinator, method)

            async def traced(*args: Any, _original: Any = original, _name: str = method, **kw: Any):
                try:
                    return await _original(*args, **kw)
                except Exception as error:
                    print(
                        f"[trace] {_name}: {type(error).__name__}: {error} | cause: "
                        f"{type(error.__cause__).__name__}: {error.__cause__}",
                        file=sys.stderr,
                    )
                    raise

            setattr(coordinator, method, traced)


async def entry(app: Any, key: str, snapshot_index: int, minute: int, name: str = "main") -> Any:
    from trading_bot.application.paper_runtime_application import PaperDecisionCommand

    ws = _ws(name)
    result = await app.state.paper_application.evaluate_and_submit(
        PaperDecisionCommand(
            account_id=UUID(ws["accounts"][key]),
            feature_snapshot_id=ws["snapshots"][snapshot_index],
            evaluated_at=_at(minute),
        )
    )
    if result.submission is None:
        return {"blocked": result.entry_block_reason}
    order = result.submission.order
    return {
        "intent_id": str(result.submission.ledger_intent_id),
        "paper_intent_id": order.intent_id,
        "eligible_at": order.eligible_at.isoformat(),
        "stop": str(order.stop_price),
        "reservation_id": str(result.risk_reservation_id),
        "revisions": dict(result.runtime_control_revisions or {}),
    }


def _bar(key: str, opened: datetime, *, low: Decimal = Decimal("99.5")) -> Any:
    from trading_bot.domain.execution.paper import PaperBar

    close = Decimal("100") if low >= Decimal("99.5") else low + Decimal("0.5")
    return PaperBar(
        key,
        "BTCUSDT",
        opened,
        opened + timedelta(minutes=1),
        opened + timedelta(minutes=1),
        Decimal("100"),
        Decimal("100.5"),
        low,
        close,
    )


async def fill(app: Any, key: str, submitted: dict[str, Any], name: str = "main") -> Any:
    from trading_bot.application.paper_runtime_application import PaperEntryEventCommand

    opened = datetime.fromisoformat(submitted["eligible_at"])
    result = await app.state.paper_application.apply_entry_event(
        PaperEntryEventCommand(
            account_id=UUID(_ws(name)["accounts"][key]),
            ledger_intent_id=UUID(submitted["intent_id"]),
            paper_intent_id=submitted["paper_intent_id"],
            bar=_bar(f"fill-{submitted['paper_intent_id'][-8:]}", opened),
        )
    )
    return {
        "status": result.order.status.value,
        "fills": result.persisted_fill_count,
        "trade_id": str(result.trade_id) if result.trade_id else None,
    }


async def stop_out(
    app: Any,
    key: str,
    submitted: dict[str, Any],
    trade_id: str,
    minutes_after: int = 5,
    name: str = "main",
) -> Any:
    from trading_bot.application.paper_runtime_application import PaperMonitorCommand

    opened = datetime.fromisoformat(submitted["eligible_at"]) + timedelta(minutes=minutes_after)
    stop = Decimal(submitted["stop"])
    result = await app.state.paper_application.monitor_position(
        PaperMonitorCommand(
            account_id=UUID(_ws(name)["accounts"][key]),
            ledger_intent_id=UUID(submitted["intent_id"]),
            trade_id=UUID(trade_id),
            paper_position_id=submitted["paper_intent_id"],
            bar=_bar(
                f"stop-{submitted['paper_intent_id'][-8:]}-{minutes_after}",
                opened,
                low=stop - Decimal("1"),
            ),
            feature_snapshot_id=_ws(name)["snapshots"][0],
        )
    )
    return {
        "exit_reason": result.monitor_result.exit_reason,
        "closed": result.trade_closed,
    }


def _registry_view(app: Any) -> dict[str, Any]:
    registry = app.state.paper_registry
    ws = _ws(app_name(app))
    names = {UUID(value): key for key, value in ws["accounts"].items()}
    entries = {
        names[account]: {
            "readiness": entry.readiness.value if entry.readiness else None,
            "reason": entry.reason_code,
            "token": int(entry.fence.fencing_token) if entry.fence else None,
        }
        for account, entry in registry.entries().items()
    }
    return {
        "summary": registry.summary(),
        "status": registry.status(),
        "workspace_control": registry.workspace_control(),
        "entries": entries,
    }


def _ready(app: Any) -> tuple[int, Any]:
    """Gọi đúng handler `/health/ready` của app (không cần HTTP client trong image)."""
    route = next(item for item in app.routes if getattr(item, "path", "") == "/health/ready")
    response = route.endpoint()
    if hasattr(response, "status_code"):
        return int(response.status_code), json.loads(response.body)
    return 200, response


def app_name(app: Any) -> str:
    workspace = str(app.state.settings.runtime_workspace_id)
    return next(
        name for name, item in STATE["workspaces"].items() if item["workspace"] == workspace
    )


# --- pha: genesis + runtime ------------------------------------------------------------------
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

    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, Any] = {}
    try:
        for name, ws in STATE["workspaces"].items():
            workspace = UUID(ws["workspace"])
            for key in ("a", "b"):
                account = UUID(ws["accounts"][key])
                async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                    await uow.bind_account(account)
                    published = await WorkspaceSettingsRepository(
                        uow.session
                    ).load_effective_config(workspace_id=workspace)
                    profile = resolve_paper_execution_profile(
                        published, default_paper_execution_profile_catalog()
                    )
                    snapshot = PaperExecutionGateway(profile.fill_policy).export_state()
                    key_name = f"mb05b-genesis:{account}"
                    await PaperCheckpointRepository().append(
                        uow,
                        account_id=account,
                        event_key=key_name,
                        event_hash=hashlib.sha256(key_name.encode()).hexdigest(),
                        snapshot=snapshot,
                        occurred_at=_at(-1),
                    )

                async def null_revision_two(
                    account: UUID = account, snapshot: Any = snapshot, workspace: UUID = workspace
                ) -> str:
                    async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                        await uow.bind_account(account)
                        await PaperCheckpointRepository().append(
                            uow,
                            account_id=account,
                            event_key=f"mb05b-null-r2:{account}",
                            event_hash="f" * 64,
                            snapshot=snapshot,
                            occurred_at=_at(-1, 30),
                        )
                    return "OK"

                # M3 (DB): NULL token chỉ hợp lệ cho genesis revision 1, kể cả khi chưa có lease.
                out[f"{name}_{key}_null_revision_2"] = await _outcome(null_revision_two())
    finally:
        await engine.dispose()
    return out


async def runtime_restart_setup() -> dict[str, Any]:
    async with runtime_app() as app:
        view_start = _registry_view(app)
        a1 = await entry(app, "a", 0, 1)
        b1 = await entry(app, "b", 1, 2)
        filled = await fill(app, "a", a1)
        view = _registry_view(app)
    return {"start": view_start, "a1": a1, "b1": b1, "a_fill": filled, "view": view}


async def runtime_restart_check() -> dict[str, Any]:
    async with runtime_app() as app:
        view = _registry_view(app)
        ws = _ws()
        accounts = app.state.paper_accounts
        a_gateway = accounts[UUID(ws["accounts"]["a"])].gateway
        b_gateway = accounts[UUID(ws["accounts"]["b"])].gateway
        a1 = STATE["a1"]
        position = a_gateway.position(a1["paper_intent_id"])
        b_order = b_gateway.order(STATE["b1"]["paper_intent_id"])
        a_config = accounts[UUID(ws["accounts"]["a"])].bot_config_version_id
    return {
        "view": view,
        "a_position_stop": str(position.stop_price) if position else None,
        "a_position_open": bool(position and not position.is_closed),
        "b_order_status": b_order.status.value,
        "a_bot_config_version_id": a_config,
    }


async def runtime_corrupt_b() -> dict[str, Any]:
    """Writer hợp lệ (lease riêng) ghi checkpoint hỏng cho B: snapshot rỗng lệch ledger."""
    from trading_bot.adapters.database.repositories.bot_runtime_leases import BotRuntimeLease
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

    ws = _ws()
    workspace, account = UUID(ws["workspace"]), UUID(ws["accounts"]["b"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with UnitOfWork(sessions, workspace_id=workspace) as uow:
            bot = await uow.bind_account(account)
            lease = BotRuntimeLease(uow.session)
            now = await lease.database_now()
            token = await lease.acquire(
                workspace, bot, "e2e-corruptor", now + timedelta(seconds=60)
            )
            published = await WorkspaceSettingsRepository(uow.session).load_effective_config(
                workspace_id=workspace
            )
            profile = resolve_paper_execution_profile(
                published, default_paper_execution_profile_catalog()
            )
            await PaperCheckpointRepository().append(
                uow,
                account_id=account,
                event_key="mb05b-corrupt-b",
                event_hash=hashlib.sha256(b"mb05b-corrupt-b").hexdigest(),
                snapshot=PaperExecutionGateway(profile.fill_policy).export_state(),
                occurred_at=_at(30),
                fencing_token=token,
            )
            released = await lease.release_if_owner(workspace, bot, "e2e-corruptor", token)
    finally:
        await engine.dispose()
    return {"corruptor_token": int(token), "released": released}


async def runtime_after_corruption() -> dict[str, Any]:
    async with runtime_app() as app:
        view = _registry_view(app)
        health_status, health_body = _ready(app)
        a1 = STATE["a1"]
        stop = await stop_out(app, "a", a1, STATE["a_fill"]["trade_id"])
        a2 = await _outcome(entry(app, "a", 2, 40))
        b_entry = await _outcome(entry(app, "b", 3, 41))
    return {
        "view": view,
        "health_status": health_status,
        "health": health_body,
        "a_stop": stop,
        "a2": a2,
        "b_entry": b_entry,
    }


async def runtime_caps() -> dict[str, Any]:
    """Ngân sách workspace MB-04 tại runtime: A reserve, B bị WORKSPACE_CAP_EXCEEDED."""
    async with runtime_app("caps") as app:
        a1 = await entry(app, "a", 0, 1, name="caps")
        b1 = await _outcome(entry(app, "b", 1, 2, name="caps"))
    return {"a1": a1, "b1": b1}


# --- control: runtime ------------------------------------------------------------------------
async def runtime_control_setup() -> dict[str, Any]:
    async with runtime_app() as app:
        a1 = await entry(app, "a", 0, 1)
        a2 = await entry(app, "a", 1, 2)
        a_fill = await fill(app, "a", a1)
        b1 = await entry(app, "b", 2, 3)
    return {"a1": a1, "a2": a2, "a_fill": a_fill, "b1": b1}


async def runtime_after_pause() -> dict[str, Any]:
    async with runtime_app() as app:
        view = _registry_view(app)
        ws = _ws()
        a_gateway = app.state.paper_accounts[UUID(ws["accounts"]["a"])].gateway
        a2_status = a_gateway.order(STATE["a2"]["paper_intent_id"]).status.value
        blocked = await _outcome(entry(app, "a", 3, 10))
        stop = await stop_out(app, "a", STATE["a1"], STATE["a_fill"]["trade_id"])
        b_fill = await fill(app, "b", STATE["b1"])
        b_entry = await _outcome(entry(app, "b", 4, 12))
    return {
        "view": view,
        "a2_status": a2_status,
        "a_entry": blocked,
        "a_stop": stop,
        "b_fill": b_fill,
        "b_entry": b_entry,
    }


async def runtime_after_kill() -> dict[str, Any]:
    async with runtime_app() as app:
        view = _registry_view(app)
        a_entry = await _outcome(entry(app, "a", 5, 20))
        b_entry = await _outcome(entry(app, "b", 6, 21))
        b_stop = await _outcome(
            stop_out(app, "b", STATE["b1"], STATE["b_fill"]["trade_id"], minutes_after=8)
        )
    return {"view": view, "a_entry": a_entry, "b_entry": b_entry, "b_stop": b_stop}


# --- control: admin --------------------------------------------------------------------------
def _principal(role: str, name: str = "main", *, stale_mfa: bool = False) -> Any:
    from trading_bot.auth.policy import Principal, Role

    ws = _ws(name)
    age = timedelta(hours=1) if stale_mfa else timedelta(minutes=1)
    return Principal(
        user_id=uuid4(),
        membership_id=UUID(ws["members"][role]),
        workspace_id=UUID(ws["workspace"]),
        roles=frozenset({Role(role)}),
        mfa_verified_at=datetime.now(UTC) - age,
    )


def _control_result(result: Any) -> Any:
    return [result.revision, result.changed]


async def admin_pause() -> dict[str, Any]:
    from trading_bot.application.bot_lifecycle_guard import BotLifecycleGuard
    from trading_bot.application.bot_runtime_control import BotRuntimeControlService
    from trading_bot.application.outbox import UnitOfWork

    ws = _ws()
    bot_a, bot_b = UUID(ws["bots"]["a"]), UUID(ws["bots"]["b"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = BotRuntimeControlService(sessions)
    out: dict[str, Any] = {}
    try:
        operator = service.for_principal(_principal("operator"))
        out["operator_pause_a"] = _control_result(await operator.pause_bot(bot_a, "Bảo trì A"))
        out["operator_pause_b"] = await _outcome(operator.pause_bot(bot_b, "Không có grant"))
        out["operator_kill"] = await _outcome(operator.pause_workspace("operator kill"))
        out["analyst_pause_a"] = await _outcome(
            service.for_principal(_principal("analyst_reviewer")).pause_bot(bot_a, "x")
        )
        out["strategy_admin_kill"] = await _outcome(
            service.for_principal(_principal("strategy_admin")).pause_workspace("x")
        )
        owner = _principal("owner")
        async with UnitOfWork(
            sessions, workspace_id=owner.workspace_id, actor_membership_id=owner.membership_id
        ) as uow:
            try:
                await BotLifecycleGuard(uow.session).ensure_flat_for_disable(
                    owner.workspace_id, bot_a
                )
                out["guard_a"] = "FLAT"
            except Exception as error:
                out["guard_a"] = [
                    _code(error),
                    getattr(error, "open_positions", None),
                    getattr(error, "open_orders", None),
                    getattr(error, "held_reservations", None),
                ]
    finally:
        await engine.dispose()
    return out


async def admin_kill() -> dict[str, Any]:
    from trading_bot.application.bot_runtime_control import BotRuntimeControlService

    ws = _ws()
    bot_a = UUID(ws["bots"]["a"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = BotRuntimeControlService(sessions)
    out: dict[str, Any] = {}
    try:
        risk = service.for_principal(_principal("risk_admin"))
        out["risk_admin_kill"] = _control_result(await risk.pause_workspace("Sự cố thị trường"))
        out["risk_admin_kill_again"] = _control_result(await risk.pause_workspace("lặp"))
        stale = service.for_principal(_principal("risk_admin", stale_mfa=True))
        out["release_stale_mfa"] = await _outcome(stale.release_workspace("mở lại", 1))
        out["operator_release"] = await _outcome(
            service.for_principal(_principal("operator")).release_workspace("mở lại", 1)
        )
        out["operator_resume_stale_mfa"] = await _outcome(
            service.for_principal(_principal("operator", stale_mfa=True)).resume_bot(
                bot_a, "mở lại", 1
            )
        )
    finally:
        await engine.dispose()
    return out


# --- ruling 27: race entry/fill vs pause/kill trong cùng process -----------------------------
async def _race_round(
    app: Any,
    owner: Any,
    bot_a: UUID,
    *,
    index: int,
    kind: str,
    mode: str,
    order_first: bool,
    snapshot: int,
    minute: int,
    pending: dict[str, Any] | None,
) -> tuple[Any, Any, dict[str, float]]:
    """Một vòng đua có barrier: runtime entry/fill và admin pause/kill xuất phát cùng lúc."""
    start = asyncio.Event()
    marks: dict[str, float] = {}

    async def runtime_side() -> Any:
        await start.wait()
        if not order_first:
            await asyncio.sleep(0.01)
        try:
            if mode == "entry":
                outcome = await entry(app, "a", snapshot, minute, name="race")
            else:
                assert pending is not None
                outcome = await fill(app, "a", pending, name="race")
        except Exception as error:
            outcome = {"error": _code(error)}
        marks["runtime"] = time.monotonic()
        return outcome

    async def admin_side() -> Any:
        await start.wait()
        if order_first:
            await asyncio.sleep(0.01)
        if kind == "bot":
            result = await owner.pause_bot(bot_a, f"race {index}")
        else:
            result = await owner.pause_workspace(f"race {index}")
        marks["admin"] = time.monotonic()
        return _control_result(result)

    tasks = [asyncio.create_task(runtime_side()), asyncio.create_task(admin_side())]
    start.set()
    runtime_outcome, admin_outcome = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    return runtime_outcome, admin_outcome, marks


async def race() -> dict[str, Any]:
    """Barrier PostgreSQL thật: runtime role entry/fill đua với admin role pause/kill."""
    from trading_bot.application.bot_runtime_control import BotRuntimeControlService

    ws = _ws("race")
    bot_a = UUID(ws["bots"]["a"])
    runtime_engine = _engine("crypidian_trading_runtime", pool=10)
    admin_engine = _engine("crypidian_admin_web", pool=4)
    runtime_sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    control = BotRuntimeControlService(admin_sessions)
    owner = control.for_principal(_principal("owner", "race"))
    rounds: list[dict[str, Any]] = []
    try:
        async with runtime_app("race", sessions=runtime_sessions, ttl=120, tick=60) as app:
            service = app.state.paper_application
            account = UUID(ws["accounts"]["a"])
            snapshot = 0
            minute = 1
            for index in range(8):
                kind = "bot" if index % 2 == 0 else "workspace"
                mode = "entry" if index < 4 else "fill"
                order_first = index % 4 < 2  # đổi thứ tự xuất phát để có cả hai thứ tự commit
                pending = None
                if mode == "fill":
                    pending = await entry(app, "a", snapshot, minute, name="race")
                    snapshot, minute = snapshot + 1, minute + 1
                runtime_outcome, admin_outcome, marks = await _race_round(
                    app,
                    owner,
                    bot_a,
                    index=index,
                    kind=kind,
                    mode=mode,
                    order_first=order_first,
                    snapshot=snapshot,
                    minute=minute,
                    pending=pending,
                )
                applied = await service.apply_runtime_control(account)
                rounds.append(
                    {
                        "kind": kind,
                        "mode": mode,
                        "runtime": runtime_outcome,
                        "admin": admin_outcome,
                        "runtime_before_admin": marks["runtime"] < marks["admin"],
                        "applied_cancelled": len(applied.cancelled_intent_ids),
                    }
                )
                snapshot, minute = snapshot + 1, minute + 1
                # Mở lại cho vòng sau (resume/release cần expected_revision + MFA gần đây).
                if kind == "bot":
                    current = admin_outcome[0]
                    await owner.resume_bot(bot_a, "race resume", current)
                else:
                    current = admin_outcome[0]
                    await owner.release_workspace("race release", current)
                await service.apply_runtime_control(account)
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()
    return {"rounds": rounds}


# --- activity ------------------------------------------------------------------------------
async def runtime_activity_setup() -> dict[str, Any]:
    async with runtime_app() as app:
        a1 = await entry(app, "a", 0, 1)
        a_fill = await fill(app, "a", a1)
    return {"a1": a1, "a_fill": a_fill}


def _scripted_supervisor(foreign: bool) -> Any:
    """Supervisor thật với guard dùng adapter kịch bản cho A (B dùng adapter PAPER thật)."""
    from trading_bot.adapters.paper.account_activity import (
        PaperLedgerAccountActivityAdapter,
        ScriptedAccountActivityAdapter,
    )
    from trading_bot.application.account_activity_guard import AccountActivityGuard
    from trading_bot.application.bot_runtime_supervisor import BotRuntimeSupervisor
    from trading_bot.ports.account_activity import (
        AccountActivitySnapshot,
        ObservedOrder,
        ObservedPosition,
    )

    ws = _ws()
    account_a = UUID(ws["accounts"]["a"])

    class Composite:
        def __init__(self, application: Any) -> None:
            self.paper = PaperLedgerAccountActivityAdapter(
                workspace_id=UUID(ws["workspace"]),
                gateways=application.gateway_view(),
                clock=lambda: datetime.now(UTC),
            )
            self.scripted = ScriptedAccountActivityAdapter()

        async def inspect(self, account_id: UUID, cursor: Any) -> Any:
            if account_id != account_a:
                return await self.paper.inspect(account_id, cursor)
            known = await self.paper.inspect(account_id, cursor)
            orders = tuple(known.orders)
            positions = tuple(known.positions)
            if foreign:
                orders += (ObservedOrder("manual-web-1", "BTCUSDT", "LONG", Decimal("0.5"), "NEW"),)
                positions += (ObservedPosition("BTCUSDT", "SHORT", Decimal("3")),)
            self.scripted.script(
                AccountActivitySnapshot(
                    workspace_id=UUID(ws["workspace"]),
                    account_id=account_id,
                    source="SCRIPTED",
                    cursor="mb05b-1",
                    observed_at=datetime.now(UTC),
                    complete=True,
                    orders=orders,
                    positions=positions,
                )
            )
            return await self.scripted.inspect(account_id, cursor)

    def factory(**kwargs: Any) -> Any:
        application = kwargs["application"]
        guard = AccountActivityGuard(
            sessions=kwargs["sessions"],
            workspace_id=kwargs["workspace_id"],
            accounts=application.runtime_account,
            adapter=Composite(application),
        )
        return BotRuntimeSupervisor(activity_guard=guard, **kwargs)

    return factory


async def runtime_activity_foreign() -> dict[str, Any]:
    from trading_bot.adapters.paper.account_activity import venue_account_activity_adapter

    async with runtime_app(supervisor_factory=_scripted_supervisor(True)) as app:
        view = _registry_view(app)
        a_entry = await _outcome(entry(app, "a", 1, 10))
        a_stop = await stop_out(app, "a", STATE["a1"], STATE["a_fill"]["trade_id"])
        b_entry = await _outcome(entry(app, "b", 2, 12))
    venue = await _outcome(asyncio.to_thread(venue_account_activity_adapter))
    return {"view": view, "a_entry": a_entry, "a_stop": a_stop, "b_entry": b_entry, "venue": venue}


async def admin_activity_resolve() -> dict[str, Any]:
    from trading_bot.adapters.database.models import AccountActivityIncidentRecord
    from trading_bot.application.account_activity_guard import AccountActivityResolutionService
    from trading_bot.application.bot_runtime_control import BotRuntimeControlService
    from trading_bot.application.outbox import UnitOfWork

    ws = _ws()
    bot_a = UUID(ws["bots"]["a"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, Any] = {}
    try:
        control = BotRuntimeControlService(sessions)
        out["operator_pause_a"] = _control_result(
            await control.for_principal(_principal("operator")).pause_bot(bot_a, "Chờ đối soát")
        )
        owner = _principal("owner")
        async with UnitOfWork(
            sessions, workspace_id=owner.workspace_id, actor_membership_id=owner.membership_id
        ) as uow:
            incident_id = await uow.session.scalar(
                select(AccountActivityIncidentRecord.id).where(
                    AccountActivityIncidentRecord.workspace_id == owner.workspace_id,
                    AccountActivityIncidentRecord.bot_id == bot_a,
                    AccountActivityIncidentRecord.status == "OPEN",
                )
            )
        resolution = AccountActivityResolutionService(sessions)
        out["operator_resolve"] = await _outcome(
            resolution.resolve_incident(
                _principal("operator"),
                bot_id=bot_a,
                incident_id=incident_id,
                resolution_code="EXTERNAL_STATE_REMOVED_BY_OPERATOR",
                reason="x",
            )
        )
        resolved = await resolution.resolve_incident(
            owner,
            bot_id=bot_a,
            incident_id=incident_id,
            resolution_code="EXTERNAL_STATE_REMOVED_BY_OPERATOR",
            reason="Đã hủy lệnh tay và đóng vị thế trên sàn",
        )
        out["owner_resolve"] = resolved.status
        out["operator_resume"] = _control_result(
            await control.for_principal(_principal("operator")).resume_bot(
                bot_a, "Đã đối soát xong", 1
            )
        )
    finally:
        await engine.dispose()
    return out


async def runtime_activity_clean() -> dict[str, Any]:
    async with runtime_app(supervisor_factory=_scripted_supervisor(False)) as app:
        view = _registry_view(app)
        a_entry = await _outcome(entry(app, "a", 3, 20))
    return {"view": view, "a_entry": a_entry}


# --- inspect (superuser) ---------------------------------------------------------------------
def inspect() -> dict[str, Any]:
    ws = _ws(STATE.get("inspect_workspace", "main"))
    workspace = ws["workspace"]
    with _connect() as connection:

        def rows(query: str, *params: object) -> list[Any]:
            return [list(row) for row in connection.execute(query, params).fetchall()]

        bot_names = {value: key for key, value in ws["bots"].items()}

        def by_bot(query: str) -> dict[str, Any]:
            return {
                bot_names[str(bot)]: value
                for bot, value in connection.execute(query, (workspace,)).fetchall()
                if str(bot) in bot_names
            }

        audits = rows(
            "SELECT action, target_id::text, reason FROM audit_event WHERE workspace_id=%s "
            "ORDER BY occurred_at, id",
            workspace,
        )
        return {
            "leases": by_bot(
                "SELECT bot_id, json_build_array(owner_id, fencing_token) FROM bot_runtime_lease "
                "WHERE workspace_id=%s"
            ),
            "states": by_bot(
                "SELECT bot_id, json_build_array(readiness, reason_code, "
                "applied_bot_control_revision, applied_workspace_control_revision, "
                "activity_status, fanout_exclusion_code) FROM bot_runtime_state "
                "WHERE workspace_id=%s"
            ),
            "audits": [
                [action, bot_names.get(target, target), reason] for action, target, reason in audits
            ],
            "intents": by_bot(
                "SELECT bot_id, json_agg(json_build_array(status, evidence->"
                "'runtime_control_revisions', settings_version_id) ORDER BY created_at) "
                "FROM order_intent WHERE workspace_id=%s GROUP BY bot_id"
            ),
            "reservations": by_bot(
                "SELECT bot_id, json_agg(json_build_array(status, resolution_reason) "
                "ORDER BY created_at) FROM risk_reservation WHERE workspace_id=%s GROUP BY bot_id"
            ),
            "trades": by_bot(
                "SELECT bot_id, json_agg(status ORDER BY opened_at) FROM trade "
                "WHERE workspace_id=%s GROUP BY bot_id"
            ),
            "checkpoints": by_bot(
                "SELECT bot_id, count(*) FROM paper_execution_checkpoint WHERE workspace_id=%s "
                "GROUP BY bot_id"
            ),
            "incidents": by_bot(
                "SELECT bot_id, json_agg(status ORDER BY detected_at) FROM "
                "account_activity_incident WHERE workspace_id=%s GROUP BY bot_id"
            ),
            "foreign_orders_in_ledger": connection.execute(
                "SELECT count(*) FROM exchange_order WHERE workspace_id=%s "
                "AND client_order_id LIKE 'manual-%%'",
                (workspace,),
            ).fetchone()[0],
            "budget_decisions": rows(
                "SELECT approved, reason_code, payload->'evidence'->'runtime_control_revisions' "
                "FROM risk_decision WHERE workspace_id=%s AND payload->>'decision_kind' = "
                "'WORKSPACE_BUDGET' ORDER BY created_at",
                workspace,
            ),
        }


async def lease_stale_writes() -> dict[str, Any]:
    """Worker cũ (token cũ) bị từ chối ở fence application và ở trigger DB khi bỏ qua app."""
    from trading_bot.adapters.database.repositories.bot_runtime_leases import RuntimeFenceGuard
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.execution.runtime_control import FencingToken, RuntimeFence

    ws = _ws()
    workspace = UUID(ws["workspace"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    out: dict[str, Any] = {}
    try:
        for key in ("a", "b"):
            bot, account = UUID(ws["bots"][key]), UUID(ws["accounts"][key])
            owner, token = STATE["old_leases"][key]

            async def application_fence(
                bot: UUID = bot, account: UUID = account, owner: str = owner, token: int = token
            ) -> str:
                async with UnitOfWork(sessions, workspace_id=workspace, bot_id=bot) as uow:
                    await RuntimeFenceGuard(uow.session).require(
                        workspace_id=workspace,
                        bot_id=bot,
                        account_id=account,
                        fence=RuntimeFence(owner, FencingToken(token)),
                    )
                return "OK"

            async def raw_insert(
                bot: UUID = bot, account: UUID = account, token: int = token
            ) -> str:
                async with UnitOfWork(sessions, workspace_id=workspace, bot_id=bot) as uow:
                    revision = await uow.session.scalar(
                        text(
                            "SELECT coalesce(max(revision), 0) + 1 FROM paper_execution_checkpoint "
                            "WHERE workspace_id = :ws AND account_id = :account"
                        ),
                        {"ws": workspace, "account": account},
                    )
                    await uow.session.execute(
                        text(
                            "INSERT INTO paper_execution_checkpoint (id, workspace_id, bot_id, "
                            "account_id, revision, event_key, event_hash, event_at, "
                            "schema_version, policy_hash, snapshot_hash, snapshot_payload, "
                            "fencing_token) VALUES (gen_random_uuid(), :ws, :bot, :account, "
                            ":revision, :key, repeat('a', 64), clock_timestamp(), 1, "
                            "repeat('b', 64), repeat('c', 64), '{}', :token)"
                        ),
                        {
                            "ws": workspace,
                            "bot": bot,
                            "account": account,
                            "revision": revision,
                            "key": f"stale-{uuid4()}",
                            "token": token,
                        },
                    )
                return "OK"

            out[f"{key}_application"] = await _outcome(application_fence())
            out[f"{key}_raw_insert"] = await _outcome(raw_insert())
    finally:
        await engine.dispose()
    return out


def diagnose_fills() -> dict[str, Any]:
    ws = _ws()
    with _connect() as connection:
        fills = connection.execute(
            "SELECT fill_role, fill_key, bar_key, fill_policy_version, quantity::text, "
            "price::text, fee_amount::text, slippage_amount::text FROM fill WHERE workspace_id=%s "
            "AND account_id=%s ORDER BY occurred_at",
            (ws["workspace"], ws["accounts"]["a"]),
        ).fetchall()
        payload = connection.execute(
            "SELECT snapshot_payload FROM paper_execution_checkpoint WHERE workspace_id=%s "
            "AND account_id=%s ORDER BY revision DESC LIMIT 1",
            (ws["workspace"], ws["accounts"]["a"]),
        ).fetchone()[0]
    body = json.loads(payload) if isinstance(payload, str) else payload
    snapshot_fills = [order.get("fills") for order in body.get("orders", [])]
    close_fills = [receipt.get("fill") for receipt in body.get("close_keys", {}).values()]
    return {"ledger": [list(row) for row in fills], "orders": snapshot_fills, "closes": close_fills}


PHASES = {
    "diagnose_fills": diagnose_fills,
    "lease_stale_writes": lease_stale_writes,
    "seed": seed,
    "genesis": genesis,
    "runtime_restart_setup": runtime_restart_setup,
    "runtime_restart_check": runtime_restart_check,
    "runtime_corrupt_b": runtime_corrupt_b,
    "runtime_after_corruption": runtime_after_corruption,
    "runtime_caps": runtime_caps,
    "runtime_control_setup": runtime_control_setup,
    "runtime_after_pause": runtime_after_pause,
    "runtime_after_kill": runtime_after_kill,
    "admin_pause": admin_pause,
    "admin_kill": admin_kill,
    "race": race,
    "runtime_activity_setup": runtime_activity_setup,
    "runtime_activity_foreign": runtime_activity_foreign,
    "admin_activity_resolve": admin_activity_resolve,
    "runtime_activity_clean": runtime_activity_clean,
    "inspect": inspect,
}


def _main() -> None:
    import inspect as _inspect

    function = PHASES[PHASE]
    result = asyncio.run(function()) if _inspect.iscoroutinefunction(function) else function()
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    _main()
