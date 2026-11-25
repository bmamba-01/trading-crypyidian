from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import ProgrammingError

from trading_bot.adapters.database.repositories.operational_telemetry import (
    AdminTelemetryScopeDenied,
    OperationalTelemetryRepository,
)
from trading_bot.application.operational_telemetry import OperationalHealthService
from trading_bot.domain.risk.circuit_breaker import (
    BreakerState,
    BreakerTriggerKind,
    CircuitBreakerSnapshot,
    OperationalTelemetry,
)
from trading_bot.domain.risk.risk_windows import LossWindowSnapshot

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / (
    "migrations/versions/f0a2c4e6b8d5_wp06_admin_operational_telemetry_projection.py"
)


def _migration():
    assert MIGRATION.is_file(), "Thiếu migration projection telemetry admin an toàn."
    spec = importlib.util.spec_from_file_location("wp06_admin_operational_telemetry", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    statements: list[str] = []
    migration = _migration()
    migration.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    migration.upgrade()
    return "\n".join(statements)


def test_admin_telemetry_migration_chains_after_whole_admin_role_split() -> None:
    migration = _migration()

    assert migration.revision == "f0a2c4e6b8d5"
    assert migration.down_revision == "f0a2c4e6b8d4"


def test_admin_telemetry_gateway_is_aggregate_scoped_and_owned_by_isolated_role() -> None:
    sql = _upgrade_sql()
    function_start = sql.index("CREATE FUNCTION public.wp06_admin_operational_telemetry_snapshot(")
    function_end = sql.index(
        "ALTER FUNCTION public.wp06_admin_operational_telemetry_snapshot(", function_start
    )
    function_sql = sql[function_start:function_end]

    assert "SECURITY DEFINER" in function_sql
    assert "SET search_path = pg_catalog, pg_temp" in function_sql
    assert "SET row_security = on" in function_sql
    assert "session_user <> 'crypidian_admin_web'" in function_sql
    assert "app.workspace_id" in function_sql
    assert "app.membership_id" in function_sql
    assert "health_read" in function_sql
    assert "breaker_resume" in function_sql
    assert "user_membership" in function_sql
    assert "status = 'ACTIVE'" in function_sql
    assert "role_binding" in function_sql
    assert "bot_grant" in function_sql

    returns = function_sql.split(") RETURNS TABLE (", maxsplit=1)[1].split(
        ") LANGUAGE", maxsplit=1
    )[0]
    assert "trade_id" not in returns
    assert "net_pnl" not in returns
    assert "order_id" not in returns

    assert "OWNER TO crypidian_admin_telemetry_reader" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "TO crypidian_admin_web_safe" in sql
    assert "GRANT SELECT (" in sql
    assert "TO crypidian_admin_telemetry_reader" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "FROM PUBLIC" in sql


@pytest.mark.asyncio
async def test_admin_projection_maps_only_aggregate_fields_to_domain_snapshots() -> None:
    as_of = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    daily_start = as_of.replace(hour=0)
    expected = {
        "feed_age_seconds": 12,
        "daily_loss": Decimal("4.25"),
        "drawdown_pct": Decimal("2.10"),
        "consecutive_api_errors": 4,
        "outbox_lag_seconds": 0,
        "clock_skew_ms": 2,
        "unprotected_exposure_count": 1,
        "reconciliation_discrepancy_count": 0,
        "unknown_order_count": 2,
        "daily_window_start": daily_start,
        "daily_window_end": daily_start + timedelta(days=1),
        "rolling_window_start": as_of - timedelta(hours=24),
        "realized_loss_today": Decimal("4.25"),
        "realized_loss_rolling": Decimal("5.50"),
        "gross_loss_today": Decimal("6.00"),
        "gross_loss_rolling": Decimal("7.25"),
        "trade_count_today": 3,
        "trade_count_rolling": 5,
        "timezone_name": "UTC",
    }

    class Result:
        def mappings(self):
            return self

        def one(self):
            return expected

    class Session:
        def __init__(self) -> None:
            self.statement = None
            self.parameters = None

        async def execute(self, statement, parameters):
            self.statement = str(statement)
            self.parameters = parameters
            return Result()

    session = Session()
    repository = OperationalTelemetryRepository(session)  # type: ignore[arg-type]
    workspace_id = uuid4()
    membership_id = uuid4()
    telemetry, loss = await repository.collect_admin_projection(
        workspace_id=workspace_id,
        membership_id=membership_id,
        bot_id=None,
        as_of=as_of,
        consecutive_api_errors=4,
        operation="health_read",
    )

    assert "wp06_admin_operational_telemetry_snapshot" in session.statement
    assert session.parameters == {
        "workspace_id": workspace_id,
        "membership_id": membership_id,
        "bot_id": None,
        "as_of": as_of,
        "consecutive_api_errors": 4,
        "operation": "health_read",
    }
    assert telemetry.feed_age_seconds == 12
    assert telemetry.daily_loss == Decimal("4.25")
    assert telemetry.drawdown_pct == Decimal("2.10")
    assert telemetry.consecutive_api_errors == 4
    assert telemetry.unknown_order_count == 2
    assert isinstance(loss, LossWindowSnapshot)
    assert loss.trade_count_today == 3
    assert loss.trade_count_rolling == 5
    assert loss.gross_loss_rolling == Decimal("7.25")


@pytest.mark.asyncio
async def test_admin_projection_maps_scope_denial_and_propagates_other_database_errors() -> None:
    class ScopeDeniedError(Exception):
        diag = SimpleNamespace(message_primary="WP06_ADMIN_TELEMETRY_BOT_SCOPE_DENIED")

    session = SimpleNamespace(
        execute=AsyncMock(side_effect=ProgrammingError("SELECT telemetry", {}, ScopeDeniedError()))
    )
    repository = OperationalTelemetryRepository(session)  # type: ignore[arg-type]
    arguments = {
        "workspace_id": uuid4(),
        "membership_id": uuid4(),
        "bot_id": None,
        "as_of": datetime(2026, 11, 24, 10, 0, tzinfo=UTC),
        "consecutive_api_errors": 0,
        "operation": "health_read",
    }

    with pytest.raises(AdminTelemetryScopeDenied):
        await repository.collect_admin_projection(**arguments)

    session.execute.side_effect = ProgrammingError(
        "SELECT telemetry", {}, RuntimeError("database connection failed")
    )
    with pytest.raises(ProgrammingError, match="database connection failed"):
        await repository.collect_admin_projection(**arguments)


def _safe_projection(as_of: datetime) -> tuple[OperationalTelemetry, LossWindowSnapshot]:
    return (
        OperationalTelemetry(
            evaluated_at=as_of,
            feed_age_seconds=0,
            daily_loss=Decimal(0),
            drawdown_pct=Decimal(0),
            consecutive_api_errors=0,
            outbox_lag_seconds=0,
            clock_skew_ms=0,
            unprotected_exposure_count=0,
            reconciliation_discrepancy_count=0,
            unknown_order_count=0,
        ),
        LossWindowSnapshot(
            as_of=as_of,
            daily_window_start=as_of.replace(hour=0, minute=0, second=0, microsecond=0),
            daily_window_end=as_of.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1),
            rolling_window_start=as_of - timedelta(hours=24),
            realized_loss_today=Decimal(0),
            realized_loss_rolling=Decimal(0),
            gross_loss_today=Decimal(0),
            gross_loss_rolling=Decimal(0),
            trade_count_today=0,
            trade_count_rolling=0,
            timezone_name="UTC",
        ),
    )


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.execute = AsyncMock()
        self.begin = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.close = AsyncMock()
        self.add = lambda _item: None


@pytest.mark.asyncio
async def test_admin_health_uses_scoped_aggregate_in_read_only_transaction(monkeypatch) -> None:
    from trading_bot.adapters.database.repositories.operational_telemetry import (
        OperationalTelemetryRepository,
    )

    as_of = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    workspace_id = uuid4()
    membership_id = uuid4()
    session = _Session()
    projection = AsyncMock(return_value=_safe_projection(as_of))
    monkeypatch.setattr(
        OperationalTelemetryRepository,
        "collect_admin_projection",
        projection,
    )
    monkeypatch.setattr(
        OperationalTelemetryRepository,
        "latest_breaker_snapshot",
        AsyncMock(return_value=None),
    )

    service = OperationalHealthService(lambda: session, clock=lambda: as_of)  # type: ignore[arg-type]
    await service.load_health_snapshot(
        workspace_id=workspace_id,
        actor_membership_id=membership_id,
        runtime=[{"error_count": 2}],
        workspace={"kill_state": "ACTIVE"},
        correlation_id="qa-correlation",
        now_utc=as_of,
    )

    assert projection.await_args is not None
    assert projection.await_args.kwargs["workspace_id"] == workspace_id
    assert projection.await_args.kwargs["membership_id"] == membership_id
    assert projection.await_args.kwargs["operation"] == "health_read"
    assert projection.await_args.kwargs["consecutive_api_errors"] == 2
    assert any(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY" in str(call.args[0])
        for call in session.execute.await_args_list
    )
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dashboard_omits_health_on_scope_denial_and_api_returns_forbidden() -> None:
    from fastapi import HTTPException

    from trading_bot.admin.web_router import _build_operations_health_data

    service = SimpleNamespace(
        load_health_snapshot=AsyncMock(side_effect=AdminTelemetryScopeDenied())
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(operational_health_service=service))
    )
    arguments = {
        "workspace_id": str(uuid4()),
        "runtime": [],
        "workspace": {"kill_state": "ACTIVE"},
        "correlation_id": "wp06-denied-scope",
        "now_utc": datetime(2026, 11, 24, 10, 0, tzinfo=UTC),
        "request": request,
        "actor_membership_id": uuid4(),
    }

    assert await _build_operations_health_data(**arguments, hide_scope_denied=True) == {}

    with pytest.raises(HTTPException) as error:
        await _build_operations_health_data(**arguments)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_breaker_resume_rechecks_projection_and_appends_in_same_transaction(
    monkeypatch,
) -> None:
    from trading_bot.adapters.database.repositories.operational_telemetry import (
        OperationalTelemetryRepository,
    )

    as_of = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    workspace_id = uuid4()
    membership_id = uuid4()
    bot_id = uuid4()
    session = _Session()
    projection = AsyncMock(return_value=_safe_projection(as_of))
    append = AsyncMock()
    last_snapshot = CircuitBreakerSnapshot(
        state=BreakerState.COOLDOWN_PASSED,
        entry_allowed=False,
        protective_exit_allowed=True,
        tripped_reasons=(BreakerTriggerKind.FEED_STALE,),
        detail_vi="Đang chờ MFA mở lại.",
        tripped_at=as_of - timedelta(minutes=30),
        cooldown_until=as_of - timedelta(minutes=5),
        revision=4,
        evidence={},
    )
    latest = AsyncMock(return_value=last_snapshot)
    latest_sessions: list[object] = []
    append_sessions: list[object] = []

    async def latest_with_session(repository, **kwargs):
        latest_sessions.append(repository._session)
        return await latest(**kwargs)

    async def append_with_session(repository, **kwargs):
        append_sessions.append(repository._session)
        return await append(**kwargs)

    async def projection_with_session(repository, **kwargs):
        latest_sessions.append(repository._session)
        return await projection(**kwargs)

    monkeypatch.setattr(
        OperationalTelemetryRepository, "latest_breaker_snapshot", latest_with_session
    )
    monkeypatch.setattr(
        OperationalTelemetryRepository, "collect_admin_projection", projection_with_session
    )
    monkeypatch.setattr(OperationalTelemetryRepository, "append_breaker_event", append_with_session)

    service = OperationalHealthService(lambda: session, clock=lambda: as_of)  # type: ignore[arg-type]
    resumed = await service.resume_breaker(
        workspace_id=workspace_id,
        bot_id=bot_id,
        operator_membership_id=membership_id,
        operator_id="owner-qa",
        mfa_verified=True,
        approval_reason="Đã đối soát trạng thái PAPER.",
    )

    assert resumed.state is BreakerState.CLOSED
    assert projection.await_args is not None
    assert projection.await_args.kwargs["operation"] == "breaker_resume"
    assert projection.await_args.kwargs["membership_id"] == membership_id
    assert latest_sessions == [session, session]
    assert append_sessions == [session]
    append.assert_awaited_once()
    session.commit.assert_awaited_once()
