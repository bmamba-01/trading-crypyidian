"""MB05.6 (ruling 20, 21, 25, 30): service điều khiển runtime có kiểm quyền, MFA và audit."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from trading_bot.application.bot_runtime_control import (
    BotRuntimeControlError,
    BotRuntimeControlService,
)
from trading_bot.auth.bot_policy import (
    WORKSPACE_BOT_ROLES,
    BotPermission,
    role_allows_bot_permission,
)
from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.domain.execution.runtime_control import (
    BotControlResult,
    BotControlState,
    BotEntryState,
    RuntimeControlError,
    WorkspaceControlResult,
    WorkspaceControlState,
    WorkspaceKillState,
    plan_bot_control_change,
    plan_workspace_control_change,
)

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
WORKSPACE = uuid4()
BOT_A, BOT_B, FOREIGN_BOT = uuid4(), uuid4(), uuid4()


class Store:
    def __init__(self) -> None:
        self.bot_rows: dict[UUID, list[BotEntryState]] = {}
        self.workspace_rows: list[WorkspaceKillState] = []
        self.audits: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.inserts: list[tuple[str, Any]] = []
        self.grants: dict[tuple[UUID, UUID], set[BotPermission]] = {}
        self.visible: dict[UUID, set[UUID]] = {}


class FakeUow:
    def __init__(self, store: Store, _sessions: object, **kwargs: Any) -> None:
        self.store = store
        self.workspace_id = kwargs["workspace_id"]
        self.actor_membership_id = kwargs.get("actor_membership_id")
        self.request_id = kwargs.get("request_id") or uuid4()
        self.session = SimpleNamespace(store=store, actor=self.actor_membership_id)
        self.pending: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeUow:
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        if exc_type is None:
            self.store.audits.extend(self.pending)

    def record_audit(self, **values: Any) -> None:
        self.pending.append(values | {"request_id": self.request_id})


class FakeControls:
    def __init__(self, session: Any) -> None:
        self.store: Store = session.store

    async def append_bot_control(self, **kwargs: Any) -> BotControlResult:
        self.store.events.append("L1")
        rows = self.store.bot_rows.setdefault(kwargs["bot_id"], [])
        current = BotControlState(len(rows), rows[-1]) if rows else None
        change = plan_bot_control_change(
            current, kwargs["target"], expected_revision=kwargs["expected_revision"]
        )
        if change.kind.value == "NOOP":
            return BotControlResult(
                kwargs["bot_id"],
                current.entry_state if current else BotEntryState.ENABLED,
                change.next_revision,
                False,
            )
        rows.append(kwargs["target"])
        self.store.inserts.append(("bot", kwargs))
        return BotControlResult(kwargs["bot_id"], kwargs["target"], change.next_revision, True)

    async def append_workspace_control(self, **kwargs: Any) -> WorkspaceControlResult:
        self.store.events.append("L1")
        rows = self.store.workspace_rows
        current = WorkspaceControlState(len(rows), rows[-1]) if rows else None
        change = plan_workspace_control_change(
            current, kwargs["target"], expected_revision=kwargs["expected_revision"]
        )
        if change.kind.value == "NOOP":
            return WorkspaceControlResult(
                kwargs["workspace_id"],
                current.kill_state if current else WorkspaceKillState.ACTIVE,
                change.next_revision,
                False,
            )
        rows.append(kwargs["target"])
        self.store.inserts.append(("workspace", kwargs))
        return WorkspaceControlResult(
            kwargs["workspace_id"], kwargs["target"], change.next_revision, True
        )


class FakeGrants:
    """Cùng ngữ nghĩa `BotGrantRepository.require`: trần role rồi grant theo bot."""

    def __init__(self, session: Any) -> None:
        self.store: Store = session.store

    async def require(self, principal: Principal, *, bot_id: UUID, permission: BotPermission):
        if not role_allows_bot_permission(principal.roles, permission):
            raise PermissionError("role")
        if role_allows_bot_permission(principal.roles & WORKSPACE_BOT_ROLES, permission):
            return
        if permission not in self.store.grants.get((principal.membership_id, bot_id), set()):
            raise PermissionError("grant")


def _principal(*roles: Role, mfa_age: timedelta = timedelta(minutes=1)) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=WORKSPACE,
        roles=frozenset(roles),
        mfa_verified_at=NOW - mfa_age,
    )


def _service(store: Store) -> BotRuntimeControlService:
    async def visible(session: Any, *, workspace_id: UUID, bot_id: UUID) -> bool:
        return workspace_id == WORKSPACE and bot_id in store.visible.get(session.actor, set())

    return BotRuntimeControlService(
        object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        unit_of_work_factory=lambda sessions, **kw: FakeUow(store, sessions, **kw),
        control_repository_factory=FakeControls,
        grant_repository_factory=FakeGrants,
        bot_visibility=visible,
    )


def _operator(store: Store, *permissions: BotPermission) -> Principal:
    principal = _principal(Role.OPERATOR)
    store.visible[principal.membership_id] = {BOT_A}
    store.grants[(principal.membership_id, BOT_A)] = set(permissions)
    return principal


def _wide(store: Store, role: Role, **kwargs: Any) -> Principal:
    principal = _principal(role, **kwargs)
    store.visible[principal.membership_id] = {BOT_A, BOT_B}
    return principal


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_operator_with_pause_grant_pauses_bot_a_but_bot_b_is_not_found() -> None:
    store = Store()
    operator = _operator(store, BotPermission.READ, BotPermission.RUNTIME_PAUSE)
    control = _service(store).for_principal(operator)

    result = _run(control.pause_bot(BOT_A, "Bảo trì"))
    with pytest.raises(BotRuntimeControlError) as error:
        _run(control.pause_bot(BOT_B, "Bảo trì"))

    assert (result.revision, result.changed, result.entry_state) == (1, True, BotEntryState.PAUSED)
    assert error.value.reason_code == "BOT_NOT_FOUND"
    assert [audit["action"] for audit in store.audits] == [
        "bot.runtime.paused",
        "bot.runtime.control_denied",
    ]
    denied = store.audits[1]
    assert denied["reason"] == "BOT_NOT_FOUND"


def test_unseen_foreign_bot_and_ungranted_bot_return_same_not_found() -> None:
    store = Store()
    operator = _operator(store, BotPermission.READ)
    control = _service(store).for_principal(operator)

    codes = []
    for bot in (BOT_A, FOREIGN_BOT):
        with pytest.raises(BotRuntimeControlError) as error:
            _run(control.pause_bot(bot, "x"))
        codes.append(error.value.reason_code)

    assert codes == ["BOT_NOT_FOUND", "BOT_NOT_FOUND"]
    assert store.inserts == []


def test_resume_unseen_or_ungranted_bot_returns_not_found_before_noop_planning() -> None:
    """M5: bot chưa từng pause + resume không được trả noop mà phải BOT_NOT_FOUND."""
    store = Store()
    operator = _operator(store, BotPermission.READ, BotPermission.RUNTIME_PAUSE)
    control = _service(store).for_principal(operator)

    for bot in (BOT_A, FOREIGN_BOT):  # A: thiếu grant resume; foreign: không thấy
        with pytest.raises(BotRuntimeControlError) as error:
            _run(control.resume_bot(bot, "mở lại", expected_revision=0))
        assert error.value.reason_code == "BOT_NOT_FOUND"
    assert store.events == []


def test_pause_and_resume_permissions_are_split_at_service_level() -> None:
    store = Store()
    pauser = _operator(store, BotPermission.READ, BotPermission.RUNTIME_PAUSE)
    resumer = _principal(Role.OPERATOR)
    store.visible[resumer.membership_id] = {BOT_A}
    store.grants[(resumer.membership_id, BOT_A)] = {
        BotPermission.READ,
        BotPermission.RUNTIME_RESUME,
    }
    service = _service(store)

    _run(service.for_principal(pauser).pause_bot(BOT_A, "pause"))
    with pytest.raises(BotRuntimeControlError) as pauser_resume:
        _run(service.for_principal(pauser).resume_bot(BOT_A, "resume", expected_revision=1))
    with pytest.raises(BotRuntimeControlError) as resumer_pause:
        _run(service.for_principal(resumer).pause_bot(BOT_A, "pause"))
    resumed = _run(service.for_principal(resumer).resume_bot(BOT_A, "resume", expected_revision=1))

    assert pauser_resume.value.reason_code == "BOT_NOT_FOUND"
    assert resumer_pause.value.reason_code == "BOT_NOT_FOUND"
    assert (resumed.revision, resumed.entry_state) == (2, BotEntryState.ENABLED)


def test_resume_requires_recent_mfa_reason_and_matching_revision() -> None:
    store = Store()
    stale = _wide(store, Role.RISK_ADMIN, mfa_age=timedelta(minutes=30))
    fresh = _wide(store, Role.RISK_ADMIN)
    service = _service(store)
    _run(service.for_principal(fresh).pause_bot(BOT_A, "pause"))

    cases = {
        "RECENT_MFA_REQUIRED": service.for_principal(stale).resume_bot(BOT_A, "r", 1),
        "CONTROL_REASON_REQUIRED": service.for_principal(fresh).resume_bot(BOT_A, "  ", 1),
        "CONTROL_REVISION_CONFLICT": service.for_principal(fresh).resume_bot(BOT_A, "r", 7),
        "CONTROL_REVISION_REQUIRED": service.for_principal(fresh).resume_bot(BOT_A, "r", None),
    }
    for code, call in cases.items():
        with pytest.raises(BotRuntimeControlError) as error:
            _run(call)
        assert error.value.reason_code == code

    assert store.bot_rows[BOT_A] == [BotEntryState.PAUSED]
    assert sum(audit["action"] == "bot.runtime.control_denied" for audit in store.audits) == 4


def test_workspace_kill_and_release_only_owner_or_risk_admin() -> None:
    assert authorize(_principal(Role.OWNER), Permission.WORKSPACE_KILL)
    assert authorize(_principal(Role.RISK_ADMIN), Permission.WORKSPACE_KILL)
    store = Store()
    service = _service(store)
    for role in (Role.OPERATOR, Role.STRATEGY_ADMIN, Role.ANALYST_REVIEWER):
        principal = _wide(store, role)
        for call in (
            service.for_principal(principal).pause_workspace("kill"),
            service.for_principal(principal).release_workspace("release", 0),
        ):
            with pytest.raises(BotRuntimeControlError) as error:
                _run(call)
            assert error.value.reason_code == "CONTROL_PERMISSION_DENIED"
    assert store.workspace_rows == []

    risk_admin = _wide(store, Role.RISK_ADMIN)
    killed = _run(service.for_principal(risk_admin).pause_workspace("Sự cố thị trường"))
    again = _run(service.for_principal(risk_admin).pause_workspace("Sự cố thị trường"))
    assert (killed.revision, killed.changed, killed.kill_state) == (
        1,
        True,
        WorkspaceKillState.KILLED,
    )
    assert (again.revision, again.changed) == (1, False)
    assert store.audits[-1]["action"] == "workspace.runtime.killed.noop"


def test_release_workspace_requires_recent_mfa_reason_and_revision() -> None:
    store = Store()
    owner = _wide(store, Role.OWNER)
    stale_owner = _wide(store, Role.OWNER, mfa_age=timedelta(hours=1))
    service = _service(store)
    _run(service.for_principal(owner).pause_workspace("kill"))

    for call, code in (
        (service.for_principal(stale_owner).release_workspace("ok", 1), "RECENT_MFA_REQUIRED"),
        (service.for_principal(owner).release_workspace("", 1), "CONTROL_REASON_REQUIRED"),
        (service.for_principal(owner).release_workspace("ok", 3), "CONTROL_REVISION_CONFLICT"),
    ):
        with pytest.raises(BotRuntimeControlError) as error:
            _run(call)
        assert error.value.reason_code == code
    released = _run(service.for_principal(owner).release_workspace("ok", 1))

    assert (released.revision, released.kill_state) == (2, WorkspaceKillState.ACTIVE)
    assert store.audits[-1]["action"] == "workspace.runtime.released"


def test_pause_noop_is_idempotent_and_audited_without_new_revision() -> None:
    store = Store()
    risk_admin = _wide(store, Role.RISK_ADMIN)
    control = _service(store).for_principal(risk_admin)

    first = _run(control.pause_bot(BOT_B, "pause"))
    second = _run(control.pause_bot(BOT_B, "pause"))

    assert (first.revision, first.changed) == (1, True)
    assert (second.revision, second.changed) == (1, False)
    assert len(store.inserts) == 1
    assert [audit["action"] for audit in store.audits] == [
        "bot.runtime.paused",
        "bot.runtime.paused.noop",
    ]


def test_every_outcome_audit_has_actor_reason_correlation_and_no_secret() -> None:
    store = Store()
    risk_admin = _wide(store, Role.RISK_ADMIN)
    control = _service(store).for_principal(risk_admin)

    _run(control.pause_bot(BOT_A, "Lý do vận hành"))
    with pytest.raises(BotRuntimeControlError):
        _run(control.resume_bot(BOT_A, "", 1))

    for audit in store.audits:
        assert audit["reason"]
        assert isinstance(audit["request_id"], UUID)
        assert "token" not in str(audit).lower()
        assert "password" not in str(audit).lower()
    insert = store.inserts[0][1]
    assert insert["changed_by"] == risk_admin.membership_id
    assert insert["reason_text"] == "Lý do vận hành"
    assert isinstance(insert["correlation_id"], UUID)


def test_control_mutation_goes_through_l1_repository_and_only_inserts() -> None:
    import inspect

    from trading_bot.adapters.database.repositories.bot_runtime_controls import (
        BotRuntimeControlRepository,
    )

    methods = {
        name
        for name, _ in inspect.getmembers(BotRuntimeControlRepository, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert not any("update" in name or "delete" in name for name in methods)
    store = Store()
    control = _service(store).for_principal(_wide(store, Role.OWNER))
    _run(control.pause_bot(BOT_A, "pause"))
    assert store.events == ["L1"]


def test_repository_resume_of_invisible_bot_is_not_found_not_noop() -> None:
    """M5 ở tầng repository: resolve bot trước khi lập kế hoạch noop."""
    from tests.unit.runtime_fakes import RecordingSession

    from trading_bot.adapters.database.repositories.bot_runtime_controls import (
        BotRuntimeControlRepository,
    )

    # Thứ tự scalar: (lock L1 là execute) -> account của bot (None = không thấy).
    session = RecordingSession(scalar_results=[None])
    with pytest.raises(RuntimeControlError) as error:
        asyncio.run(
            BotRuntimeControlRepository(session).append_bot_control(  # type: ignore[arg-type]
                workspace_id=WORKSPACE,
                bot_id=FOREIGN_BOT,
                target=BotEntryState.ENABLED,
                expected_revision=0,
                changed_by=uuid4(),
                reason_code="OPERATOR_RESUME",
                reason_text="resume",
                correlation_id=uuid4(),
            )
        )
    assert error.value.reason_code == "BOT_NOT_FOUND"
    assert session.added == []
