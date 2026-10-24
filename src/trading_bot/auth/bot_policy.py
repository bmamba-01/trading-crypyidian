"""Quyền thao tác bot; grant theo bot không được mở rộng quyền của role."""

from enum import StrEnum

from trading_bot.auth.policy import Role


class BotPermission(StrEnum):
    READ = "bot:read"
    CONFIG_WRITE = "bot:config:write"
    CONFIG_PUBLISH = "bot:config:publish"
    RUNTIME_PAUSE = "bot:runtime:pause"
    RUNTIME_RESUME = "bot:runtime:resume"
    REPORT_READ = "bot:report:read"


WORKSPACE_BOT_ROLES = frozenset({Role.OWNER, Role.STRATEGY_ADMIN, Role.RISK_ADMIN})
_READ = frozenset({BotPermission.READ, BotPermission.REPORT_READ})
BOT_ROLE_PERMISSIONS = {
    Role.OWNER: frozenset(BotPermission),
    Role.STRATEGY_ADMIN: _READ | {BotPermission.CONFIG_WRITE, BotPermission.CONFIG_PUBLISH},
    Role.RISK_ADMIN: _READ | {BotPermission.RUNTIME_PAUSE, BotPermission.RUNTIME_RESUME},
    Role.OPERATOR: _READ | {BotPermission.RUNTIME_PAUSE, BotPermission.RUNTIME_RESUME},
    Role.ANALYST_REVIEWER: _READ,
}


def role_allows_bot_permission(roles: frozenset[Role], permission: BotPermission) -> bool:
    """Kiểm tra trần quyền role trước khi xét grant theo từng bot."""
    return any(permission in BOT_ROLE_PERMISSIONS[role] for role in roles)
