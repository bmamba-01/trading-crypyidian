"""Fake dùng chung cho test MB-07a: control plane thật (MB-05 + MB-07a) trên kho trong bộ nhớ.

`BotRuntimeControlService` và `BotOperationsService` là code thật; chỉ UoW, repository control,
repository grant, repository đọc trạng thái và guard phẳng được thay bằng fake cùng ngữ nghĩa
(trần role rồi grant theo bot; bot ngoài scope = không thấy; L1 trước khi ghi).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from trading_bot.adapters.database.repositories.bot_operations import (
    BotIdentityRow,
    BotRuntimeRow,
    WorkspaceControlRow,
)
from trading_bot.application.bot_lifecycle_guard import BotNotFlatError
from trading_bot.application.bot_operations import BotOperationsService
from trading_bot.application.bot_runtime_control import BotRuntimeControlService
from trading_bot.auth.bot_policy import (
    WORKSPACE_BOT_ROLES,
    BotPermission,
    role_allows_bot_permission,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.execution.runtime_control import (
    BotControlResult,
    BotControlState,
    BotEntryState,
    WorkspaceControlResult,
    WorkspaceControlState,
    WorkspaceKillState,
    plan_bot_control_change,
    plan_workspace_control_change,
)

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
OTHER_WORKSPACE = UUID("22222222-2222-4222-8222-222222222222")
BOT_A = UUID("aaaaaaaa-0000-4000-8000-00000000000a")
BOT_B = UUID("bbbbbbbb-0000-4000-8000-00000000000b")
FOREIGN_BOT = UUID("ffffffff-0000-4000-8000-00000000000f")


@dataclass
class Store:
    """Trạng thái giả của một workspace; `visible` mô phỏng RLS admin-web theo membership."""

    bot_rows: dict[UUID, list[BotEntryState]] = field(default_factory=dict)
    workspace_rows: list[WorkspaceKillState] = field(default_factory=list)
    audits: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    grants: dict[tuple[UUID, UUID], set[BotPermission]] = field(default_factory=dict)
    visible: dict[UUID, set[UUID]] = field(default_factory=dict)
    bots: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    not_flat: dict[UUID, tuple[int, int, int]] = field(default_factory=dict)
    readiness: dict[UUID, tuple[str, str | None]] = field(default_factory=dict)

    def add_bot(self, bot_id: UUID, name: str, status: str = "ENABLED") -> None:
        self.bots[bot_id] = {"name": name, "status": status, "account_id": uuid4()}


class FakeUow:
    def __init__(self, store: Store, _sessions: object, **kwargs: Any) -> None:
        self.store = store
        self.workspace_id = kwargs["workspace_id"]
        self.bot_id = kwargs.get("bot_id")
        self.actor_membership_id = kwargs.get("actor_membership_id")
        self.request_id = kwargs.get("request_id") or uuid4()
        self.session = SimpleNamespace(
            store=store, actor=self.actor_membership_id, workspace=self.workspace_id
        )
        self.pending: list[dict[str, Any]] = []
        self.pending_status: dict[UUID, str] = {}

    async def __aenter__(self) -> FakeUow:
        self.session.uow = self
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        if exc_type is None:
            self.store.audits.extend(self.pending)
            for bot_id, status in self.pending_status.items():
                self.store.bots[bot_id]["status"] = status

    def record_audit(self, **values: Any) -> None:
        self.pending.append(
            values
            | {
                "request_id": self.request_id,
                "actor": self.actor_membership_id,
                "workspace_id": self.workspace_id,
            }
        )


def _visible(session: Any, bot_id: UUID) -> bool:
    store: Store = session.store
    return (
        session.workspace == WORKSPACE
        and bot_id in store.bots
        and bot_id in store.visible.get(session.actor, set())
    )


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
        self.store.events.append(f"bot:{kwargs['target'].value}")
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
        self.store.events.append(f"workspace:{kwargs['target'].value}")
        return WorkspaceControlResult(
            kwargs["workspace_id"], kwargs["target"], change.next_revision, True
        )


class FakeGrants:
    """Cùng ngữ nghĩa `BotGrantRepository.require`: trần role rồi grant theo bot."""

    def __init__(self, session: Any) -> None:
        self.store: Store = session.store

    async def require(
        self, principal: Principal, *, bot_id: UUID, permission: BotPermission
    ) -> None:
        if not role_allows_bot_permission(principal.roles, permission):
            raise PermissionError("role")
        if role_allows_bot_permission(principal.roles & WORKSPACE_BOT_ROLES, permission):
            return
        if permission not in self.store.grants.get((principal.membership_id, bot_id), set()):
            raise PermissionError("grant")


class FakeOperationsRepository:
    """Đọc bot/control/readiness dưới "RLS" giả; `disable_account` ghi vào UoW đang mở."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.store: Store = session.store

    def _row(self, bot_id: UUID) -> BotIdentityRow:
        bot = self.store.bots[bot_id]
        return BotIdentityRow(
            bot_id=bot_id,
            name=bot["name"],
            status=bot["status"],
            account_id=bot["account_id"],
        )

    async def list_bots(self, workspace_id: UUID) -> list[BotIdentityRow]:
        del workspace_id
        return [
            self._row(bot_id)
            for bot_id in sorted(self.store.bots, key=str)
            if _visible(self.session, bot_id)
        ]

    async def get_bot(self, workspace_id: UUID, bot_id: UUID) -> BotIdentityRow | None:
        del workspace_id
        return self._row(bot_id) if _visible(self.session, bot_id) else None

    async def bot_runtime(self, workspace_id: UUID, bot_id: UUID) -> BotRuntimeRow:
        del workspace_id
        rows = self.store.bot_rows.get(bot_id, [])
        readiness = self.store.readiness.get(bot_id)
        return BotRuntimeRow(
            entry_state=rows[-1] if rows else BotEntryState.ENABLED,
            control_revision=len(rows),
            readiness=readiness[0] if readiness else None,
            readiness_reason_code=readiness[1] if readiness else None,
        )

    async def workspace_control(self, workspace_id: UUID) -> WorkspaceControlRow:
        del workspace_id
        rows = self.store.workspace_rows
        return WorkspaceControlRow(
            kill_state=rows[-1] if rows else WorkspaceKillState.ACTIVE, revision=len(rows)
        )

    async def disable_account(self, workspace_id: UUID, account_id: UUID) -> bool:
        del workspace_id
        self.store.events.append("account:lock")
        for bot_id, bot in self.store.bots.items():
            if bot["account_id"] == account_id:
                if bot["status"] == "DISABLED":
                    return False
                self.session.uow.pending_status[bot_id] = "DISABLED"
                self.store.events.append("account:disabled")
                return True
        raise AssertionError("account không tồn tại")


class FakeGuard:
    def __init__(self, session: Any) -> None:
        self.store: Store = session.store

    async def ensure_flat_for_disable(self, workspace_id: UUID, bot_id: UUID) -> None:
        del workspace_id
        self.store.events.append("guard:L1")
        counts = self.store.not_flat.get(bot_id)
        if counts:
            raise BotNotFlatError(
                "BOT_NOT_FLAT",
                open_positions=counts[0],
                open_orders=counts[1],
                held_reservations=counts[2],
            )


def build_operations(store: Store, *, clock: Any = None) -> BotOperationsService:
    """Service vận hành thật nối với control service MB-05 thật, cùng kho fake."""

    async def visible(session: Any, *, workspace_id: UUID, bot_id: UUID) -> bool:
        return workspace_id == WORKSPACE and _visible(session, bot_id)

    def uow_factory(sessions: object, **kwargs: Any) -> FakeUow:
        return FakeUow(store, sessions, **kwargs)

    now = clock or (lambda: NOW)
    control = BotRuntimeControlService(
        object(),  # type: ignore[arg-type]
        clock=now,
        unit_of_work_factory=uow_factory,
        control_repository_factory=FakeControls,
        grant_repository_factory=FakeGrants,
        bot_visibility=visible,
    )
    return BotOperationsService(
        object(),  # type: ignore[arg-type]
        control_service=control,
        clock=now,
        unit_of_work_factory=uow_factory,
        operations_repository_factory=FakeOperationsRepository,
        grant_repository_factory=FakeGrants,
        lifecycle_guard_factory=FakeGuard,
    )


def principal(
    *roles: Role,
    mfa_age: timedelta = timedelta(minutes=1),
    workspace_id: UUID = WORKSPACE,
    membership_id: UUID | None = None,
) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=membership_id or uuid4(),
        workspace_id=workspace_id,
        roles=frozenset(roles),
        mfa_verified_at=NOW - mfa_age,
    )


def seeded_store() -> Store:
    """Workspace QA thu nhỏ: bot A, B; principal được thêm bằng `member`."""
    store = Store()
    store.add_bot(BOT_A, "paper-a")
    store.add_bot(BOT_B, "paper-b")
    return store


def member(store: Store, kind: str, **kwargs: Any) -> Principal:
    """Principal theo bộ dữ liệu QA (`docs/testing/qa-test-data.md` mục 2.2)."""
    roles = {
        "owner": Role.OWNER,
        "strategy_admin": Role.STRATEGY_ADMIN,
        "risk_admin": Role.RISK_ADMIN,
        "operator": Role.OPERATOR,
        "operator_ungranted": Role.OPERATOR,
        "analyst_reviewer": Role.ANALYST_REVIEWER,
        "other_workspace_owner": Role.OWNER,
    }
    if kind == "other_workspace_owner":
        kwargs.setdefault("workspace_id", OTHER_WORKSPACE)
    actor = principal(roles[kind], **kwargs)
    if kind in {"owner", "strategy_admin", "risk_admin"}:
        store.visible[actor.membership_id] = {BOT_A, BOT_B}
    elif kind == "operator":
        store.visible[actor.membership_id] = {BOT_A}
        store.grants[(actor.membership_id, BOT_A)] = {
            BotPermission.READ,
            BotPermission.RUNTIME_PAUSE,
            BotPermission.RUNTIME_RESUME,
        }
    elif kind == "analyst_reviewer":
        store.visible[actor.membership_id] = {BOT_A}
        store.grants[(actor.membership_id, BOT_A)] = {
            BotPermission.READ,
            BotPermission.REPORT_READ,
        }
    else:
        store.visible[actor.membership_id] = set()
    return actor
