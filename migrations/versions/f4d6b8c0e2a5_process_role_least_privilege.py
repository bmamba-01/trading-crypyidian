"""SEC-01: quyền DB tối thiểu theo process trên bảng identity/auth/config.

Group `crypidian_app` không còn quyền trên bảng identity/config; admin-web và trading-runtime
nhận grant trực tiếp đúng đường code; execution-gateway, ingestion-worker, trainer-worker rời
group (chỉ còn grant trực tiếp sẵn có). Policy `workspace_isolation` cũ (mặc định PUBLIC) được
giới hạn `TO` role dự định, giữ nguyên biểu thức. Xem
`docs/superpowers/plans/2026-10-24-sec01-sec02-db-hardening-wbs.md`.

Revision ID: f4d6b8c0e2a5
Revises: e3c5a7b9d2f4
"""

from alembic import op

revision = "f4d6b8c0e2a5"
down_revision = "e3c5a7b9d2f4"
branch_labels = None
depends_on = None

GROUP_ROLE = "crypidian_app"
ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
WORKER_ROLES = (
    "crypidian_execution_gateway",
    "crypidian_ingestion_worker",
    "crypidian_trainer_worker",
)

# Quyền của group trước SEC-01 (48a80bae456d, d0b7e9a2416c); downgrade cấp lại đúng như cũ.
LEGACY_GROUP_PRIVILEGES: dict[str, str] = {
    **dict.fromkeys(
        (
            "user_membership",
            "role_binding",
            "session",
            "mfa_factor",
            "telegram_binding",
            "trading_account",
            "secret_reference",
            "config_version",
            "risk_profile_version",
            "approval_request",
            "watchlist_version",
            "watchlist_item",
            "strategy_release",
        ),
        "SELECT, INSERT, UPDATE, DELETE",
    ),
    "workspace": "SELECT",
    "workspace_settings_version": "SELECT, INSERT, UPDATE",
}

# Grant trực tiếp MỚI (chưa có trước SEC-01). Grant trực tiếp cũ được giữ nguyên:
# admin-web `user_account` S/I/U, `workspace` I/U, `workspace_settings_version` S/I/U;
# runtime `workspace_settings_version` S.
DIRECT_GRANTS: dict[str, dict[str, tuple[str, ...]]] = {
    ADMIN: {
        # Đọc control-plane (đã chịu RLS); ghi chỉ ở bảng và cột mà code admin thật sự ghi.
        "workspace": ("SELECT",),
        # BotGrantRepository khóa membership FOR UPDATE; UPDATE(created_at) chỉ là cột lock-only
        # (như runtime R4). Không code nào sửa status — cột này nằm trong predicate RLS MB-01.
        "user_membership": ("SELECT", "INSERT", "UPDATE (created_at)"),
        "role_binding": ("SELECT", "INSERT"),
        "session": ("SELECT", "INSERT", "UPDATE (csrf_token_hash, mfa_verified_at, revoked_at)"),
        "mfa_factor": ("SELECT", "INSERT", "UPDATE (last_used_counter, verified_at)"),
        "telegram_binding": ("SELECT",),
        "secret_reference": ("SELECT", "INSERT"),
        # MB-02 tạo bot bằng INSERT account PAPER và khóa account FOR UPDATE; không code nào
        # sửa cột account (max_notional là cap rủi ro), nên UPDATE chỉ cột lock-only updated_at.
        "trading_account": ("SELECT", "INSERT", "UPDATE (updated_at)"),
        "config_version": ("SELECT",),
        "risk_profile_version": ("SELECT", "INSERT"),
        "approval_request": ("SELECT",),
        "watchlist_version": ("SELECT",),
        "watchlist_item": ("SELECT",),
        "strategy_release": ("SELECT",),
    },
    RUNTIME: {
        "workspace": ("SELECT",),
        # Khởi tạo account PAPER; UPDATE(updated_at) chỉ để SELECT ... FOR UPDATE khóa hàng
        # (PostgreSQL yêu cầu UPDATE trên ít nhất một cột), không sửa được cap/trạng thái.
        "trading_account": ("SELECT", "INSERT", "UPDATE (updated_at)"),
        "config_version": ("SELECT",),
        "risk_profile_version": ("SELECT",),
        "approval_request": ("SELECT",),
        "strategy_release": ("SELECT",),
    },
}

# Policy workspace_isolation cũ áp cho PUBLIC; chỉ role dự định còn dùng được nó.
POLICY_ROLES: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(
        (
            "user_membership",
            "role_binding",
            "session",
            "mfa_factor",
            "telegram_binding",
            "secret_reference",
            "watchlist_version",
            "watchlist_item",
        ),
        (ADMIN,),
    ),
    **dict.fromkeys(
        (
            "trading_account",
            "risk_profile_version",
            "strategy_release",
            "workspace_settings_version",
        ),
        (ADMIN, RUNTIME),
    ),
}


def _assert_no_residual_access() -> None:
    """Dừng nếu worker/group vẫn còn quyền identity/config (ví dụ do PUBLIC hoặc grant lạ)."""
    tables = ", ".join(f"'{table}'" for table in LEGACY_GROUP_PRIVILEGES)
    roles = ", ".join(f"'{role}'" for role in (GROUP_ROLE, *WORKER_ROLES))
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM unnest(ARRAY[{roles}]) AS r(role_name)
            CROSS JOIN unnest(ARRAY[{tables}]) AS t(table_name)
            CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',
                'REFERENCES','TRIGGER']) AS p(privilege)
            WHERE has_table_privilege(r.role_name, t.table_name, p.privilege)
               OR (p.privilege IN ('SELECT','INSERT','UPDATE','REFERENCES')
                   AND has_any_column_privilege(r.role_name, t.table_name, p.privilege))
        ) THEN
            RAISE EXCEPTION 'SEC01_RESIDUAL_IDENTITY_PRIVILEGE';
        END IF;
    END $$""")


def upgrade() -> None:
    for table in LEGACY_GROUP_PRIVILEGES:
        op.execute(f'REVOKE ALL ON TABLE "{table}" FROM {GROUP_ROLE}')
    for role, tables in DIRECT_GRANTS.items():
        for table, privileges in tables.items():
            for privilege in privileges:
                op.execute(f'GRANT {privilege} ON TABLE "{table}" TO {role}')
    for role in WORKER_ROLES:
        # Membership là đối tượng cấp cluster; schema usage cấp trực tiếp, không dựa PUBLIC.
        op.execute(f"REVOKE {GROUP_ROLE} FROM {role}")
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    for table, roles in POLICY_ROLES.items():
        op.execute(f'ALTER POLICY workspace_isolation ON "{table}" TO {", ".join(roles)}')
    _assert_no_residual_access()


def downgrade() -> None:
    for table in POLICY_ROLES:
        op.execute(f'ALTER POLICY workspace_isolation ON "{table}" TO PUBLIC')
    for role in WORKER_ROLES:
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")
        op.execute(f"GRANT {GROUP_ROLE} TO {role}")
    for role, tables in DIRECT_GRANTS.items():
        for table, privileges in tables.items():
            for privilege in privileges:
                op.execute(f'REVOKE {privilege} ON TABLE "{table}" FROM {role}')
    for table, privileges in LEGACY_GROUP_PRIVILEGES.items():
        op.execute(f'GRANT {privileges} ON TABLE "{table}" TO {GROUP_ROLE}')
