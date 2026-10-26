"""Domain thuần cho registry runtime PAPER nhiều bot (MB05.1).

Gồm fencing token, quyết định lease, trạng thái control vận hành (pause bot, kill workspace)
và cổng entry. Module không đọc đồng hồ hay DB: mọi mốc thời gian do caller truyền vào (lấy từ
`clock_timestamp()` của PostgreSQL ở tầng repository) để đồng hồ DB là nguồn sự thật duy nhất.
Hợp đồng: `docs/architecture/decisions/2026-10-24-multibot-contracts.md` §7 và ADR addendum
`docs/architecture/decisions/2026-10-26-runtime-control-addendum.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID

# TTL tối đa của lease; trigger DB dùng cùng giá trị (migration MB-05) nên đổi phải đổi cả hai.
MAX_LEASE_TTL = timedelta(minutes=5)


class RuntimeControlError(RuntimeError):
    """Lỗi domain runtime mang reason code an toàn, không chứa dữ liệu nhạy cảm."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require_utc(*values: datetime) -> None:
    for value in values:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise RuntimeControlError("LEASE_TIME_NOT_UTC")


def _require_owner(owner_id: str) -> None:
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 120:
        raise RuntimeControlError("LEASE_OWNER_INVALID")


@dataclass(frozen=True, slots=True, order=True)
class FencingToken:
    """Số thế hệ lease đơn điệu tăng trong scope bot/account; luôn là int dương."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or self.value < 1:
            raise RuntimeControlError("FENCING_TOKEN_INVALID")

    def next(self) -> FencingToken:
        """Token kế tiếp khi lease đổi chủ hoặc được lấy lại sau khi hết hạn."""
        return FencingToken(self.value + 1)

    def __int__(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True)
class RuntimeFence:
    """Bằng chứng giữ lease mà mọi command thay đổi state PAPER phải trình ra."""

    owner_id: str
    fencing_token: FencingToken

    def __post_init__(self) -> None:
        _require_owner(self.owner_id)
        if not isinstance(self.fencing_token, FencingToken):
            raise RuntimeControlError("FENCING_TOKEN_INVALID")


@dataclass(frozen=True, slots=True)
class LeaseSnapshot:
    """Hàng lease hiện hành đã đọc trong transaction (owner, token, hạn theo đồng hồ DB)."""

    owner_id: str
    fencing_token: FencingToken
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_owner(self.owner_id)
        _require_utc(self.expires_at)

    def is_live(self, now: datetime) -> bool:
        _require_utc(now)
        return self.expires_at > now


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """Lease đã được cấp/gia hạn; `rehydrate_required` khi token mới khác token đang giữ."""

    owner_id: str
    fencing_token: FencingToken
    granted_at: datetime
    expires_at: datetime
    rehydrate_required: bool
    max_ttl: timedelta = MAX_LEASE_TTL

    def __post_init__(self) -> None:
        _require_owner(self.owner_id)
        if not isinstance(self.fencing_token, FencingToken):
            raise RuntimeControlError("FENCING_TOKEN_INVALID")
        _require_utc(self.granted_at, self.expires_at)
        if self.expires_at <= self.granted_at:
            raise RuntimeControlError("LEASE_EXPIRY_NOT_FUTURE")
        if self.max_ttl <= timedelta(0) or self.max_ttl > MAX_LEASE_TTL:
            raise RuntimeControlError("LEASE_TTL_INVALID")
        if self.expires_at - self.granted_at > self.max_ttl:
            raise RuntimeControlError("LEASE_TTL_TOO_LONG")

    @property
    def ttl(self) -> timedelta:
        return self.expires_at - self.granted_at

    @property
    def fence(self) -> RuntimeFence:
        return RuntimeFence(self.owner_id, self.fencing_token)


def decide_lease_acquire(
    current: LeaseSnapshot | None,
    *,
    owner_id: str,
    now: datetime,
    expires_at: datetime,
    max_ttl: timedelta = MAX_LEASE_TTL,
) -> LeaseGrant:
    """Compare-and-set lease (ruling 8): chỉ một chủ thắng, token không bao giờ giảm.

    - chưa có hàng: token 1, phải hydrate;
    - cùng chủ, chưa hết hạn: giữ token (tương đương renew);
    - hết hạn (chủ mới hoặc chính chủ cũ): token + 1 và phải hydrate lại;
    - chủ khác còn hạn: `LEASE_HELD_ELSEWHERE`.
    """
    _require_owner(owner_id)
    _require_utc(now, expires_at)
    if current is None:
        return LeaseGrant(owner_id, FencingToken(1), now, expires_at, True, max_ttl)
    if current.is_live(now):
        if current.owner_id != owner_id:
            raise RuntimeControlError("LEASE_HELD_ELSEWHERE")
        return LeaseGrant(owner_id, current.fencing_token, now, expires_at, False, max_ttl)
    return LeaseGrant(owner_id, current.fencing_token.next(), now, expires_at, True, max_ttl)


def verify_fence(
    current: LeaseSnapshot | None,
    *,
    owner_id: str,
    fencing_token: FencingToken,
    now: datetime,
) -> None:
    """Fence tầng application: đúng chủ, đúng token hiện hành và lease còn hạn."""
    if current is None:
        raise RuntimeControlError("LEASE_NOT_HELD")
    if current.owner_id != owner_id or current.fencing_token != fencing_token:
        raise RuntimeControlError("STALE_FENCING_TOKEN")
    if not current.is_live(now):
        raise RuntimeControlError("LEASE_EXPIRED")


def decide_lease_renew(
    current: LeaseSnapshot | None,
    *,
    owner_id: str,
    fencing_token: FencingToken,
    now: datetime,
    expires_at: datetime,
    max_ttl: timedelta = MAX_LEASE_TTL,
) -> LeaseGrant:
    """Gia hạn chỉ hợp lệ khi cùng chủ, cùng token và chưa hết hạn; hết hạn phải acquire lại."""
    verify_fence(current, owner_id=owner_id, fencing_token=fencing_token, now=now)
    return LeaseGrant(owner_id, fencing_token, now, expires_at, False, max_ttl)


class WorkspaceKillState(StrEnum):
    """Kill switch workspace là trạng thái vận hành (ruling 5, 25), không phải settings."""

    ACTIVE = "ACTIVE"
    KILLED = "KILLED"


class BotEntryState(StrEnum):
    """Pause bot chỉ chặn entry mới; exits bảo vệ vẫn chạy (ruling 14)."""

    ENABLED = "ENABLED"
    PAUSED = "PAUSED"


class BotReadiness(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    STANDBY = "STANDBY"


class RuntimeActivityStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class RuntimeOperation(StrEnum):
    """Loại thao tác runtime; giá trị trùng `BudgetOperation` của MB-04."""

    NEW_ENTRY = "NEW_ENTRY"
    PROTECTIVE_EXIT = "PROTECTIVE_EXIT"
    PROTECTIVE_MONITORING = "PROTECTIVE_MONITORING"


class EntryBlockReason(StrEnum):
    """Lý do chặn entry theo thứ tự cố định; thứ tự khai báo chính là thứ tự đánh giá."""

    BOT_DISABLED = "BOT_DISABLED"
    WORKSPACE_KILLED = "WORKSPACE_KILLED"
    BOT_PAUSED = "BOT_PAUSED"
    BOT_RECOVERY_REQUIRED = "BOT_RECOVERY_REQUIRED"
    LEASE_NOT_HELD = "LEASE_NOT_HELD"
    EXTERNAL_ACTIVITY_UNRECONCILED = "EXTERNAL_ACTIVITY_UNRECONCILED"
    ACCOUNT_ACTIVITY_UNVERIFIED = "ACCOUNT_ACTIVITY_UNVERIFIED"
    RUNTIME_CONTROL_UNAVAILABLE = "RUNTIME_CONTROL_UNAVAILABLE"


_PROTECTIVE = frozenset(
    {RuntimeOperation.PROTECTIVE_EXIT.value, RuntimeOperation.PROTECTIVE_MONITORING.value}
)


@dataclass(frozen=True, slots=True)
class EntryGateInput:
    """Toàn bộ điều kiện entry của ruling 4, đã đọc trong transaction hiện hành.

    `workspace_kill_state`/`bot_entry_state` là None khi không đọc được control (lỗi DB/quyền);
    khi đó `control_available` phải False và gate fail-closed.
    """

    account_enabled: bool
    bot_enabled: bool
    workspace_kill_state: WorkspaceKillState | None
    bot_entry_state: BotEntryState | None
    readiness: BotReadiness
    lease_held: bool
    open_incident: bool
    activity_status: RuntimeActivityStatus | None
    control_available: bool


@dataclass(frozen=True, slots=True)
class EntryGateDecision:
    allowed: bool
    reason: EntryBlockReason | None = None


class EntryGate:
    """Cổng entry thuần; không bao giờ chặn thao tác bảo vệ vị thế đã biết."""

    @staticmethod
    def evaluate(gate: EntryGateInput, operation: StrEnum) -> EntryGateDecision:
        if str(operation.value) in _PROTECTIVE:
            return EntryGateDecision(True)
        if str(operation.value) != RuntimeOperation.NEW_ENTRY.value:
            raise RuntimeControlError("RUNTIME_OPERATION_INVALID")
        checks = (
            (not (gate.account_enabled and gate.bot_enabled), EntryBlockReason.BOT_DISABLED),
            (
                gate.workspace_kill_state is WorkspaceKillState.KILLED,
                EntryBlockReason.WORKSPACE_KILLED,
            ),
            (gate.bot_entry_state is BotEntryState.PAUSED, EntryBlockReason.BOT_PAUSED),
            (
                gate.readiness in (BotReadiness.RECOVERY_REQUIRED, BotReadiness.STARTING),
                EntryBlockReason.BOT_RECOVERY_REQUIRED,
            ),
            (
                not gate.lease_held or gate.readiness is BotReadiness.STANDBY,
                EntryBlockReason.LEASE_NOT_HELD,
            ),
            (gate.open_incident, EntryBlockReason.EXTERNAL_ACTIVITY_UNRECONCILED),
            (
                gate.activity_status is not RuntimeActivityStatus.VERIFIED,
                EntryBlockReason.ACCOUNT_ACTIVITY_UNVERIFIED,
            ),
            (
                not gate.control_available
                or gate.workspace_kill_state is None
                or gate.bot_entry_state is None,
                EntryBlockReason.RUNTIME_CONTROL_UNAVAILABLE,
            ),
        )
        for blocked, reason in checks:
            if blocked:
                return EntryGateDecision(False, reason)
        return EntryGateDecision(True)


@dataclass(frozen=True, slots=True)
class WorkspaceControlState:
    """Revision control workspace hiện hành (hàng có revision lớn nhất)."""

    revision: int
    kill_state: WorkspaceKillState

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            raise RuntimeControlError("CONTROL_REVISION_INVALID")


@dataclass(frozen=True, slots=True)
class BotControlState:
    """Revision control bot hiện hành (hàng có revision lớn nhất)."""

    revision: int
    entry_state: BotEntryState

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            raise RuntimeControlError("CONTROL_REVISION_INVALID")


@dataclass(frozen=True, slots=True)
class RuntimeControlSnapshot:
    """Control workspace + bot đọc lại trong chính transaction entry/reservation (ruling 27).

    Chưa có hàng nghĩa là chưa từng pause/kill (ruling 22): revision 0, ACTIVE/ENABLED.
    """

    workspace: WorkspaceControlState | None
    bot: BotControlState | None

    @property
    def workspace_kill_state(self) -> WorkspaceKillState:
        return self.workspace.kill_state if self.workspace else WorkspaceKillState.ACTIVE

    @property
    def bot_entry_state(self) -> BotEntryState:
        return self.bot.entry_state if self.bot else BotEntryState.ENABLED

    def revisions(self) -> dict[str, int]:
        """Revision đã đọc để ghi vào evidence intent/reservation (`runtime_control_revisions`)."""
        return {
            "workspace": self.workspace.revision if self.workspace else 0,
            "bot": self.bot.revision if self.bot else 0,
        }


class ControlChangeKind(StrEnum):
    INSERT = "INSERT"
    NOOP = "NOOP"


@dataclass(frozen=True, slots=True)
class ControlChange:
    """Kế hoạch thay đổi control: INSERT revision kế tiếp hoặc NOOP (idempotent theo đích)."""

    kind: ControlChangeKind
    next_revision: int

    @classmethod
    def noop(cls, revision: int) -> Self:
        return cls(ControlChangeKind.NOOP, revision)


def _plan_change(
    current_revision: int,
    *,
    at_target: bool,
    risk_increasing: bool,
    expected_revision: int | None,
) -> ControlChange:
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision != current_revision
    ):
        raise RuntimeControlError("CONTROL_REVISION_CONFLICT")
    if at_target:
        return ControlChange.noop(current_revision)
    if risk_increasing and expected_revision is None:
        # Resume/release là hành động tăng rủi ro: bắt buộc expected_revision (ruling 21).
        raise RuntimeControlError("CONTROL_REVISION_REQUIRED")
    return ControlChange(ControlChangeKind.INSERT, current_revision + 1)


def plan_bot_control_change(
    current: BotControlState | None,
    target: BotEntryState,
    *,
    expected_revision: int | None,
) -> ControlChange:
    """Pause idempotent theo trạng thái đích; resume cần `expected_revision` khớp."""
    state = current.entry_state if current else BotEntryState.ENABLED
    return _plan_change(
        current.revision if current else 0,
        at_target=state is target,
        risk_increasing=target is BotEntryState.ENABLED,
        expected_revision=expected_revision,
    )


def plan_workspace_control_change(
    current: WorkspaceControlState | None,
    target: WorkspaceKillState,
    *,
    expected_revision: int | None,
) -> ControlChange:
    """Kill idempotent theo trạng thái đích; release cần `expected_revision` khớp."""
    state = current.kill_state if current else WorkspaceKillState.ACTIVE
    return _plan_change(
        current.revision if current else 0,
        at_target=state is target,
        risk_increasing=target is WorkspaceKillState.ACTIVE,
        expected_revision=expected_revision,
    )


@dataclass(frozen=True, slots=True)
class BotControlResult:
    """Kết quả pause/resume bot; `changed=False` là noop (không thêm revision)."""

    bot_id: UUID
    entry_state: BotEntryState
    revision: int
    changed: bool
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceControlResult:
    """Kết quả kill/release workspace; `changed=False` là noop (không thêm revision)."""

    workspace_id: UUID
    kill_state: WorkspaceKillState
    revision: int
    changed: bool
    reason_code: str | None = None
