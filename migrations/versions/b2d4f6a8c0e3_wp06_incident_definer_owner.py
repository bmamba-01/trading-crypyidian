"""WP06: chuyển owner 3 hàm SECURITY DEFINER của incident entry hold sang role NOLOGIN riêng.

Migration `f5b7d9e1a3c6` tạo `wp06_lock_learning_incident_scope`,
`wp06_learning_incident_eligible_target` và trigger `wp06_learning_incident_guard` với owner mặc
định là user migration `crypidian_admin` (role đăng nhập, bỏ qua RLS). Hàm SECURITY DEFINER khi
đó chạy với toàn quyền admin — trái quy tắc MB-04/SEC-01 "chỉ role NOLOGIN riêng sở hữu, grant
tối thiểu" mà `tests/e2e/test_db_role_least_privilege.py` kiểm.

Role mới `crypidian_learning_incident_guard` (NOLOGIN, NOBYPASSRLS) chỉ được SELECT theo
`app.workspace_id` trên đúng các bảng ba hàm đọc và EXECUTE các hàm chúng gọi. Kiểm caller
(`session_user`) trong hàm giữ nguyên.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b2d4f6a8c0e3"
down_revision: str | None = "f1b3d5e7a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD = "crypidian_learning_incident_guard"
PREVIOUS_OWNER = "crypidian_admin"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
POLICY = "wp06_incident_guard_read"
TABLES = (
    "learning_model_active_pointer",
    "learning_retrain_proposal",
    "learning_drift_assessment",
    "learning_drift_assessment_member",
    "learning_shadow_observation",
    "learning_model_registry",
    "learning_incident_entry_hold_event",
    "user_membership",
    "role_binding",
)
OWNED_FUNCTIONS = (
    "wp06_lock_learning_incident_scope(uuid,uuid)",
    "wp06_learning_incident_eligible_target(uuid,uuid)",
    "wp06_learning_incident_guard()",
)
CALLED_FUNCTIONS = (
    "wp06_resolve_scorer_runtime_target(uuid,text,jsonb,uuid,integer,boolean)",
    "wp06_lock_learning_shadow_cohort(uuid,uuid,integer)",
    "wp06_canonical_utc_timestamp_json(timestamp with time zone)",
    "wp06_learning_incident_canonical(jsonb)",
    # `wp06_learning_incident_canonical` (SQL, không definer) gọi tiếp hàm này dưới quyền owner.
    "wp06_compact_canonical_json(json)",
)


def upgrade() -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{GUARD}') THEN
                CREATE ROLE {GUARD} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                    NOCREATEDB NOCREATEROLE NOREPLICATION;
            END IF;
        END $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {GUARD}")
    for table in TABLES:
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {GUARD}")
        op.execute(
            f'CREATE POLICY {POLICY} ON public."{table}" FOR SELECT TO {GUARD} USING ({WORKSPACE})'
        )
    for function in CALLED_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function} TO {GUARD}")
    for function in OWNED_FUNCTIONS:
        op.execute(f"ALTER FUNCTION public.{function} OWNER TO {GUARD}")
    # Đổi owner làm mất grant cũ do owner cấp; cấp lại đúng ma trận gốc (chỉ admin-web).
    for function in OWNED_FUNCTIONS[:2]:
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function} TO crypidian_admin_web")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_learning_incident_guard() FROM PUBLIC")


def downgrade() -> None:
    for function in OWNED_FUNCTIONS:
        op.execute(f"ALTER FUNCTION public.{function} OWNER TO {PREVIOUS_OWNER}")
    for function in OWNED_FUNCTIONS[:2]:
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function} TO crypidian_admin_web")
    for function in CALLED_FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION public.{function} FROM {GUARD}")
    for table in TABLES:
        op.execute(f'DROP POLICY IF EXISTS {POLICY} ON public."{table}"')
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {GUARD}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {GUARD}")
    op.execute(f"DROP ROLE IF EXISTS {GUARD}")
