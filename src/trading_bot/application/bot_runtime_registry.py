"""Registry runtime PAPER theo bot trong một process (MB05.5, ruling 1, 2, 26).

Registry giữ trạng thái *trong bộ nhớ* của từng bot mà process này quản lý: readiness, fence
đang giữ, cờ control/activity đã quan sát. Registry chỉ phục vụ readiness tổng hợp và điều phối
supervisor; quyết định entry luôn đọc lại control/state trong transaction (ruling 27), không dùng
cờ ở đây làm căn cứ. Readiness không chứa workspace/bot/account ID (SEC-35).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any
from uuid import UUID

from trading_bot.domain.execution.runtime_control import BotReadiness, RuntimeFence

# Trạng thái bootstrap cũ (MB-01) ánh xạ sang readiness registry.
_STATUS_MAP = {
    "READY": BotReadiness.READY,
    "RECOVERY_REQUIRED": BotReadiness.RECOVERY_REQUIRED,
    "STANDBY": BotReadiness.STANDBY,
    "STARTING": BotReadiness.STARTING,
}
BLOCKED_BY_WORKSPACE = "BLOCKED_BY_WORKSPACE"


@dataclass(frozen=True, slots=True)
class BotRuntimeEntry:
    """Trạng thái một bot trong process; thay instance khi đổi (không sửa tại chỗ)."""

    account_id: UUID
    bot_id: UUID | None
    readiness: BotReadiness | None
    reason_code: str | None = None
    fence: RuntimeFence | None = None
    bot_paused: bool = False
    workspace_killed: bool = False
    activity_blocked: bool = False
    blocked_by_workspace: bool = False
    last_error_code: str | None = None

    @property
    def holds_lease(self) -> bool:
        return self.fence is not None and self.readiness in (
            BotReadiness.READY,
            BotReadiness.RECOVERY_REQUIRED,
        )


class BotRuntimeRegistry:
    """Bản đồ account -> trạng thái bot của process; readiness tổng hợp không lộ ID."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        owner_id: str | None = None,
        workspace_ready: bool = True,
        error_code: str | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.owner_id = owner_id
        self.workspace_ready = workspace_ready
        self.error_code = error_code
        self._entries: dict[UUID, BotRuntimeEntry] = {}

    @classmethod
    def from_bootstrap(cls, result: Any) -> BotRuntimeRegistry:
        """Dựng registry từ kết quả bootstrap (kể cả kết quả giả của test cũ)."""
        registry = cls(
            workspace_id=result.workspace_id,
            owner_id=getattr(result, "owner_id", None),
            workspace_ready=bool(getattr(result, "ready", False)),
            error_code=getattr(result, "error_code", None),
        )
        bots: Mapping[UUID, UUID] = getattr(result, "bot_ids", {}) or {}
        active = getattr(result, "active_accounts", {}) or {}
        for account_id, state in getattr(result, "account_states", {}).items():
            status = getattr(state, "status", "")
            status_value = str(getattr(status, "value", status))
            runtime = active.get(account_id)
            fence = getattr(runtime, "fence", None)
            registry._entries[account_id] = BotRuntimeEntry(
                account_id=account_id,
                bot_id=bots.get(account_id) or getattr(runtime, "bot_id", None),
                readiness=_STATUS_MAP.get(status_value),
                reason_code=getattr(state, "reason_code", None),
                fence=fence if isinstance(fence, RuntimeFence) else None,
                blocked_by_workspace=status_value == BLOCKED_BY_WORKSPACE,
            )
        return registry

    # --- truy vấn ------------------------------------------------------------------------
    def entry(self, account_id: UUID) -> BotRuntimeEntry:
        return self._entries[account_id]

    def entries(self) -> Mapping[UUID, BotRuntimeEntry]:
        return MappingProxyType(dict(self._entries))

    def account_ids(self) -> tuple[UUID, ...]:
        return tuple(sorted(self._entries, key=str))

    # --- cập nhật ------------------------------------------------------------------------
    def register(
        self,
        account_id: UUID,
        bot_id: UUID,
        readiness: BotReadiness,
        *,
        fence: RuntimeFence | None = None,
        reason_code: str | None = None,
    ) -> None:
        self._entries[account_id] = BotRuntimeEntry(
            account_id=account_id,
            bot_id=bot_id,
            readiness=readiness,
            reason_code=reason_code,
            fence=fence,
        )

    def _update(self, account_id: UUID, **changes: Any) -> None:
        current = self._entries.get(account_id)
        if current is None:
            current = BotRuntimeEntry(account_id=account_id, bot_id=None, readiness=None)
        self._entries[account_id] = replace(current, **changes)

    def mark_ready(self, account_id: UUID, fence: RuntimeFence, bot_id: UUID | None = None) -> None:
        changes: dict[str, Any] = {
            "readiness": BotReadiness.READY,
            "reason_code": None,
            "fence": fence,
            "last_error_code": None,
        }
        if bot_id is not None:
            changes["bot_id"] = bot_id
        self._update(account_id, **changes)

    def mark_standby(self, account_id: UUID, reason_code: str) -> None:
        """Mất lease: bot không còn là writer trong process này, không ảnh hưởng bot khác."""
        self._update(
            account_id, readiness=BotReadiness.STANDBY, reason_code=reason_code, fence=None
        )

    def mark_recovery(self, account_id: UUID, reason_code: str) -> None:
        self._update(account_id, readiness=BotReadiness.RECOVERY_REQUIRED, reason_code=reason_code)

    def set_control_flags(
        self, account_id: UUID, *, bot_paused: bool, workspace_killed: bool
    ) -> None:
        self._update(account_id, bot_paused=bot_paused, workspace_killed=workspace_killed)

    def set_activity_blocked(self, account_id: UUID, blocked: bool) -> None:
        self._update(account_id, activity_blocked=blocked)

    def record_error(self, account_id: UUID, code: str | None) -> None:
        """Lỗi tick của một bot chỉ đánh dấu bot đó (degraded), không gỡ bot khác."""
        self._update(account_id, last_error_code=code)

    # --- readiness tổng hợp --------------------------------------------------------------
    def summary(self) -> dict[str, int]:
        counts = {
            "total": len(self._entries),
            "ready": 0,
            "recovery_required": 0,
            "standby": 0,
            "paused": 0,
            "activity_blocked": 0,
            "blocked_by_workspace": 0,
        }
        for entry in self._entries.values():
            if entry.blocked_by_workspace:
                counts["blocked_by_workspace"] += 1
            elif entry.readiness is BotReadiness.READY:
                counts["ready"] += 1
            elif entry.readiness is BotReadiness.RECOVERY_REQUIRED:
                counts["recovery_required"] += 1
            elif entry.readiness is BotReadiness.STANDBY:
                counts["standby"] += 1
            if entry.bot_paused:
                counts["paused"] += 1
            if entry.activity_blocked:
                counts["activity_blocked"] += 1
        return counts

    def workspace_control(self) -> str:
        killed = any(entry.workspace_killed for entry in self._entries.values())
        return "killed" if killed else "active"

    def status(self) -> str:
        """`ready` khi mọi bot READY và không bị chặn; ngược lại `degraded` (ruling 26)."""
        counts = self.summary()
        healthy = (
            counts["ready"] == counts["total"]
            and counts["paused"] == 0
            and counts["activity_blocked"] == 0
            and self.workspace_control() == "active"
            and all(entry.last_error_code is None for entry in self._entries.values())
        )
        return "ready" if healthy else "degraded"
