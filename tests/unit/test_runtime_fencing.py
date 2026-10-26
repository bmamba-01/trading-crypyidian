"""MB05.3: fence tầng application (`FOR SHARE` hàng lease) và kiểm kê đường ghi PAPER."""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.runtime_fakes import FakeUow, RecordingSession

from trading_bot.adapters.database.models import BotRuntimeLeaseRecord
from trading_bot.adapters.database.repositories.account_activity_incidents import (
    AccountActivityIncidentRepository,
)
from trading_bot.adapters.database.repositories.bot_runtime_leases import (
    RuntimeFenceGuard,
    require_current_fence,
    require_unleased,
)
from trading_bot.adapters.database.repositories.bot_runtime_states import (
    BotRuntimeStateRepository,
)
from trading_bot.adapters.database.repositories.paper_checkpoints import (
    PaperCheckpointRepository,
)
from trading_bot.adapters.database.repositories.paper_ledger import PaperLedgerRepository
from trading_bot.adapters.database.repositories.risk_reservations import (
    RiskReservationRepository,
)
from trading_bot.application.workspace_risk import WorkspaceRiskCoordinator
from trading_bot.domain.execution.runtime_control import (
    FencingToken,
    RuntimeControlError,
    RuntimeFence,
)

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
WORKSPACE, BOT, ACCOUNT = uuid4(), uuid4(), uuid4()
SRC = Path(__file__).parents[2] / "src" / "trading_bot"


def _lease(owner: str = "runtime-a", token: int = 3, *, expires_in: int = 30) -> object:
    return BotRuntimeLeaseRecord(
        id=uuid4(),
        workspace_id=WORKSPACE,
        bot_id=BOT,
        account_id=ACCOUNT,
        owner_id=owner,
        fencing_token=token,
        acquired_at=NOW - timedelta(minutes=1),
        renewed_at=NOW,
        expires_at=NOW + timedelta(seconds=expires_in),
    )


def _fence(session: RecordingSession, *, owner: str = "runtime-a", token: int = 3) -> None:
    asyncio.run(
        require_current_fence(
            FakeUow(WORKSPACE, session),
            bot_id=BOT,
            account_id=ACCOUNT,
            owner_id=owner,
            token=FencingToken(token),
        )
    )


def test_current_fence_locks_lease_row_for_share_then_reads_db_clock() -> None:
    session = RecordingSession(scalar_results=[_lease(), NOW])

    _fence(session)

    assert "FOR SHARE" in session.statements[0]
    assert "bot_runtime_lease" in session.statements[0]
    assert "clock_timestamp()" in session.statements[1]


@pytest.mark.parametrize(
    ("lease", "owner", "token", "code"),
    [
        (_lease(token=4), "runtime-a", 3, "STALE_FENCING_TOKEN"),
        (_lease(owner="runtime-b"), "runtime-a", 3, "STALE_FENCING_TOKEN"),
        (_lease(expires_in=0), "runtime-a", 3, "LEASE_EXPIRED"),
        (None, "runtime-a", 3, "LEASE_NOT_HELD"),
    ],
    ids=["stale-token", "other-owner", "expired", "missing"],
)
def test_current_fence_rejects_stale_owner_expired_and_missing_lease(
    lease: object, owner: str, token: int, code: str
) -> None:
    session = RecordingSession(scalar_results=[lease, NOW])

    with pytest.raises(RuntimeControlError) as error:
        _fence(session, owner=owner, token=token)

    assert error.value.reason_code == code


def test_current_fence_rejects_lease_for_other_account() -> None:
    lease = _lease()
    lease.account_id = uuid4()  # type: ignore[attr-defined]
    session = RecordingSession(scalar_results=[lease, NOW])

    with pytest.raises(RuntimeControlError) as error:
        _fence(session)

    assert error.value.reason_code == "LEASE_SCOPE_MISMATCH"


def test_unleased_transitional_mode_only_when_bot_never_had_lease() -> None:
    asyncio.run(require_unleased(FakeUow(WORKSPACE, RecordingSession([None])), bot_id=BOT))

    with pytest.raises(RuntimeControlError) as error:
        asyncio.run(require_unleased(FakeUow(WORKSPACE, RecordingSession([_lease()])), bot_id=BOT))
    assert error.value.reason_code == "FENCING_TOKEN_REQUIRED"


def test_fence_guard_dispatches_on_fence_presence() -> None:
    session = RecordingSession(scalar_results=[_lease(), NOW])
    asyncio.run(
        RuntimeFenceGuard(session).require(
            workspace_id=WORKSPACE,
            bot_id=BOT,
            account_id=ACCOUNT,
            fence=RuntimeFence("runtime-a", FencingToken(3)),
        )
    )
    assert "FOR SHARE" in session.statements[0]

    session = RecordingSession(scalar_results=[None])
    asyncio.run(
        RuntimeFenceGuard(session).require(
            workspace_id=WORKSPACE, bot_id=BOT, account_id=ACCOUNT, fence=None
        )
    )
    assert len(session.statements) == 1


def test_application_calls_fence_right_after_l1_and_before_l2_l4() -> None:
    from tests.unit.test_paper_runtime_application import _application_with_fakes

    events: list[str] = []

    class _Guard:
        def __init__(self, _session: object) -> None:
            pass

        async def require(self, **_: object) -> None:
            events.append("fence")

    service, command, *_ = _application_with_fakes(
        fail_submission=False, fence_guard_factory=_Guard, lock_events=events
    )

    asyncio.run(service.evaluate_and_submit(command))

    assert events[:2] == ["L1", "fence"]
    assert events.index("fence") < events.index("L2-L4:reserve")
    assert events.index("fence") < events.index("L4:submit")


# --- Kiểm kê tĩnh mọi method repository ghi PAPER/reservation/incident/state (ruling 10) ----
# Phân loại: CHECKPOINT = append checkpoint cùng transaction (trigger DB fence);
# APPLICATION_FENCE = chỉ gọi trong command đã qua `RuntimeFenceGuard` sau L1;
# FENCE_ARGUMENT = method tự gọi fence với `RuntimeFence` bắt buộc; ADMIN = admin-web ghi ý định
# (runtime không có grant); UNWIRED = chưa có call site runtime, MB-05 Part B phải nối trong
# command có fence trước khi thêm module gọi.
LEDGER_WRITES = {
    "create_intent",
    "cancel_remainder",
    "record_order_transition",
    "transfer_reservation_to_open_trade",
    "release_reservation_for_terminal_no_fill",
    "mark_reservation_unknown",
    "append_fill",
    "append_exit_fill",
    "append_position_event",
    "open_trade",
    "append_trade_cost",
    "close_trade",
}
RESERVATION_WRITES = {
    "record_evaluation",
    "reserve",
    "mark_unknown",
    "reconcile",
    "transfer_to_open_trade",
    "release_terminal_no_fill",
    "release_before_submit",
    "expire_overdue",
    "expire_due",
}
RESERVATION_READS = {
    "lock_workspace_risk",
    "lock_account_risk",
    "lock_accounts_risk",
    "get_by_idempotency_key",
    "get_decision_by_key",
    "get_decision_by_idempotency_key",
    "list_active_reservations",
    "get_workspace_risk_aggregate",
}
COORDINATOR_WRITES = {
    "reserve",
    "transfer_to_open",
    "release_reservation",
    "mark_unknown",
    "expire_overdue",
}
COORDINATOR_READS = {"acquire_locks", "build_account_scope", "to_budget_reservation"}
# Module được phép gọi từng method ghi; module mới phải được phân loại ở đây.
ALLOWED_CALLERS = {
    **dict.fromkeys(LEDGER_WRITES, {"application/paper_lifecycle.py"}),
    "transfer_to_open_trade": {"adapters/database/repositories/paper_ledger.py"},
    "release_terminal_no_fill": {"adapters/database/repositories/paper_ledger.py"},
    "mark_unknown": {"adapters/database/repositories/paper_ledger.py"},
    "record_evaluation": {"application/paper_runtime_application.py"},
    "reserve": {"application/paper_runtime_application.py"},
    "reconcile": set(),
    "release_before_submit": set(),
    "expire_overdue": set(),
    "expire_due": set(),
    "transfer_to_open": set(),
    "release_reservation": set(),
    "append": {"application/paper_lifecycle.py"},
}


def _public_coroutines(cls: type) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }


def test_every_write_method_of_paper_risk_repositories_is_classified() -> None:
    assert _public_coroutines(PaperLedgerRepository) == LEDGER_WRITES
    assert _public_coroutines(RiskReservationRepository) == RESERVATION_WRITES | RESERVATION_READS
    assert (
        _public_coroutines(WorkspaceRiskCoordinator)
        | {
            "build_account_scope",
            "to_budget_reservation",
        }
        == COORDINATOR_WRITES | COORDINATOR_READS
    )
    assert "append" in _public_coroutines(PaperCheckpointRepository)


def test_state_and_incident_writes_require_runtime_fence_argument() -> None:
    for method in (
        BotRuntimeStateRepository.upsert,
        AccountActivityIncidentRepository.open_incident,
    ):
        parameter = inspect.signature(method).parameters["fence"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
        assert "RuntimeFence" in str(parameter.annotation)


# File định nghĩa chỉ ủy quyền nội bộ giữa các method của chính repository/coordinator.
DEFINING_MODULES = {
    "adapters/database/repositories/paper_ledger.py",
    "adapters/database/repositories/risk_reservations.py",
    "adapters/database/repositories/paper_checkpoints.py",
    "application/workspace_risk.py",
}


def _call_sites() -> dict[str, set[str]]:
    """Tên method -> module gọi; `append` chỉ tính khi receiver là checkpoint repository."""
    sites: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("domain/") or relative in DEFINING_MODULES:
            continue  # domain thuần thao tác state trong bộ nhớ, không ghi DB.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            name = node.func.attr
            if name == "append" and "checkpoint" not in ast.unparse(node.func.value).lower():
                continue
            sites.setdefault(name, set()).add(relative)
    return sites


def test_write_methods_are_only_called_from_fenced_boundaries() -> None:
    sites = _call_sites()
    problems = []
    for name, allowed in ALLOWED_CALLERS.items():
        actual = sites.get(name, set())
        if not actual <= allowed | DEFINING_MODULES:
            problems.append((name, sorted(actual - allowed)))
    assert not problems, problems


def test_fenced_commands_cover_every_runtime_write_call_site() -> None:
    """Mọi call site ghi trong application service nằm trong command đã gọi fence."""
    path = SRC / "application" / "paper_runtime_application.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PaperRuntimeApplicationService"
    )
    writes = {"record_evaluation", "reserve", "submit_approved"} | {
        "apply_entry_market_event",
        "monitor_market_event",
    }
    for function in service.body:
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        calls = {
            node.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        if calls & writes:
            assert "_require_fence" in calls, function.name
