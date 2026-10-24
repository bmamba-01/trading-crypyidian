"""Lớp danh tính và quyền: workspace, thành viên, PAPER account và grant theo bot (MB-01).

Bot không được mô tả ở đây vì trigger DB tạo `bot_instance` khi insert account; grant tham
chiếu bot qua khóa account. Thông tin đăng nhập không nằm trong module này: seed sinh mật
khẩu/TOTP lúc chạy và chỉ ghi vào tệp trong `.secrets/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from tests.fixtures.qa_dataset.identity import QA_OTHER_WORKSPACE_KEY, QA_WORKSPACE_KEY, qa_id

from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Role


@dataclass(frozen=True, slots=True)
class QaWorkspace:
    """Workspace PAPER của dataset; slug dùng cho đăng nhập Admin API."""

    key: str
    id: UUID
    name: str
    slug: str
    mode: str
    status: str


@dataclass(frozen=True, slots=True)
class QaMember:
    """Người dùng + membership + role + MFA; không chứa mật khẩu hay TOTP seed."""

    key: str
    workspace_key: str
    user_id: UUID
    membership_id: UUID
    email: str
    roles: tuple[Role, ...]
    membership_status: str
    user_status: str
    mfa_factor_id: UUID
    secret_reference_id: UUID

    def role_binding_id(self, role: Role) -> UUID:
        return qa_id("role_binding", f"{self.key}:{role.value}")

    @property
    def is_active(self) -> bool:
        return self.membership_status == "ACTIVE" and self.user_status == "ACTIVE"


@dataclass(frozen=True, slots=True)
class QaTradingAccount:
    """PAPER account; bot tương ứng do trigger `trading_account_provision_bot` tạo."""

    key: str
    workspace_key: str
    id: UUID
    name: str
    status: str
    max_notional: Decimal
    venue: str
    mode: str
    environment: str


@dataclass(frozen=True, slots=True)
class QaBotGrant:
    """Grant còn hiệu lực trên bot của account `account_key`, do owner cấp."""

    id: UUID
    workspace_key: str
    account_key: str
    member_key: str
    permission: BotPermission
    granted_by_member_key: str


def build_workspaces() -> tuple[QaWorkspace, ...]:
    return (
        QaWorkspace(
            key=QA_WORKSPACE_KEY,
            id=qa_id("workspace", QA_WORKSPACE_KEY),
            name="Crypidian QA",
            slug="crypidian-qa",
            mode="PAPER",
            status="ACTIVE",
        ),
        QaWorkspace(
            key=QA_OTHER_WORKSPACE_KEY,
            id=qa_id("workspace", QA_OTHER_WORKSPACE_KEY),
            name="Crypidian QA Other",
            slug="crypidian-qa-other",
            mode="PAPER",
            status="ACTIVE",
        ),
    )


def _member(
    key: str,
    workspace_key: str,
    email_local: str,
    role: Role,
    *,
    membership_status: str = "ACTIVE",
) -> QaMember:
    return QaMember(
        key=key,
        workspace_key=workspace_key,
        user_id=qa_id("user_account", key),
        membership_id=qa_id("user_membership", key),
        email=f"{email_local}@example.test",
        roles=(role,),
        membership_status=membership_status,
        user_status="ACTIVE",
        mfa_factor_id=qa_id("mfa_factor", key),
        secret_reference_id=qa_id("secret_reference", key),
    )


def build_members() -> tuple[QaMember, ...]:
    """Mỗi role một thành viên ACTIVE; thêm một thành viên bị đình chỉ và owner workspace khác."""
    return (
        _member("qa.owner", QA_WORKSPACE_KEY, "qa.owner", Role.OWNER),
        _member("qa.strategy_admin", QA_WORKSPACE_KEY, "qa.strategy-admin", Role.STRATEGY_ADMIN),
        _member("qa.risk_admin", QA_WORKSPACE_KEY, "qa.risk-admin", Role.RISK_ADMIN),
        _member("qa.operator", QA_WORKSPACE_KEY, "qa.operator", Role.OPERATOR),
        _member(
            "qa.analyst_reviewer",
            QA_WORKSPACE_KEY,
            "qa.analyst-reviewer",
            Role.ANALYST_REVIEWER,
        ),
        # Giữ role workspace-wide và grant: phép thử từ chối chỉ đến từ trạng thái membership.
        _member(
            "qa.suspended",
            QA_WORKSPACE_KEY,
            "qa.suspended",
            Role.RISK_ADMIN,
            membership_status="SUSPENDED",
        ),
        _member("qa_other.owner", QA_OTHER_WORKSPACE_KEY, "qa-other.owner", Role.OWNER),
    )


def _account(
    key: str, workspace_key: str, name: str, status: str, max_notional: str
) -> QaTradingAccount:
    return QaTradingAccount(
        key=key,
        workspace_key=workspace_key,
        id=qa_id("trading_account", key),
        name=name,
        status=status,
        max_notional=Decimal(max_notional),
        venue="binance_usdm",
        mode="PAPER",
        environment="local",
    )


def build_accounts() -> tuple[QaTradingAccount, ...]:
    return (
        _account("A", QA_WORKSPACE_KEY, "qa-paper-a", "ENABLED", "10000"),
        _account("B", QA_WORKSPACE_KEY, "qa-paper-b", "ENABLED", "10000"),
        _account("C", QA_WORKSPACE_KEY, "qa-paper-c", "DISABLED", "5000"),
        _account("OTHER", QA_OTHER_WORKSPACE_KEY, "qa-other-paper", "ENABLED", "5000"),
    )


def _grant(account_key: str, member_key: str, permission: BotPermission) -> QaBotGrant:
    return QaBotGrant(
        id=qa_id("bot_grant", f"{account_key}:{member_key}:{permission.value}"),
        workspace_key=QA_WORKSPACE_KEY,
        account_key=account_key,
        member_key=member_key,
        permission=permission,
        granted_by_member_key="qa.owner",
    )


def build_grants() -> tuple[QaBotGrant, ...]:
    """Operator/analyst chỉ có quyền trên bot A; role workspace-wide không cần grant."""
    return (
        _grant("A", "qa.operator", BotPermission.READ),
        _grant("A", "qa.operator", BotPermission.RUNTIME_PAUSE),
        _grant("A", "qa.operator", BotPermission.RUNTIME_RESUME),
        _grant("A", "qa.analyst_reviewer", BotPermission.READ),
        _grant("A", "qa.analyst_reviewer", BotPermission.REPORT_READ),
        _grant("A", "qa.suspended", BotPermission.READ),
    )
