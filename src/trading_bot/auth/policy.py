from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    OWNER = "owner"
    OPERATOR = "operator"
    STRATEGY_ADMIN = "strategy_admin"
    RISK_ADMIN = "risk_admin"
    ANALYST_REVIEWER = "analyst_reviewer"


class Permission(StrEnum):
    WORKSPACE_READ = "workspace:read"
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    TRADE_READ = "trade:read"
    TRADE_REVIEW = "trade:review"
    STRATEGY_WRITE = "strategy:write"
    WATCHLIST_WRITE = "watchlist:write"
    RISK_PROFILE_WRITE = "risk_profile:write"
    RUNTIME_PAUSE = "runtime:pause"
    RUNTIME_RESUME = "runtime:resume"
    # Kill/release toàn workspace (MB-05 ruling 30): chỉ owner, risk_admin.
    WORKSPACE_KILL = "workspace:kill"
    MEMBER_WRITE = "member:write"
    SECRET_WRITE = "secret:write"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.OPERATOR: frozenset(
        {
            Permission.WORKSPACE_READ,
            Permission.TRADE_READ,
            Permission.RUNTIME_PAUSE,
            Permission.RUNTIME_RESUME,
        }
    ),
    Role.STRATEGY_ADMIN: frozenset(
        {
            Permission.WORKSPACE_READ,
            Permission.SETTINGS_READ,
            Permission.SETTINGS_WRITE,
            Permission.STRATEGY_WRITE,
            Permission.WATCHLIST_WRITE,
        }
    ),
    Role.RISK_ADMIN: frozenset(
        {
            Permission.WORKSPACE_READ,
            Permission.TRADE_READ,
            Permission.RISK_PROFILE_WRITE,
            Permission.RUNTIME_PAUSE,
            Permission.RUNTIME_RESUME,
            Permission.WORKSPACE_KILL,
        }
    ),
    Role.ANALYST_REVIEWER: frozenset(
        {Permission.WORKSPACE_READ, Permission.TRADE_READ, Permission.TRADE_REVIEW}
    ),
}


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    membership_id: UUID
    workspace_id: UUID
    roles: frozenset[Role]
    mfa_verified_at: datetime


def authorize(principal: Principal, permission: Permission, *, raise_error: bool = False) -> bool:
    """Kiểm tra quyền theo role ở backend; giao diện không thay cho bước này."""
    allowed = any(permission in ROLE_PERMISSIONS[role] for role in principal.roles)
    if not allowed and raise_error:
        raise PermissionError("Không đủ quyền cho thao tác này")
    return allowed
