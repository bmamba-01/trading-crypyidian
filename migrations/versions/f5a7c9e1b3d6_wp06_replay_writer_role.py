"""WP06: tách quyền ghi kết quả replay khỏi trainer."""

from collections.abc import Sequence

from alembic import op

revision: str = "f5a7c9e1b3d6"
down_revision: str | None = "e6c8a0b2d4f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRAINER = "crypidian_trainer_worker"
ADMIN = "crypidian_admin_web"
REPLAY_WRITER = "crypidian_replay_writer"
PERSIST_TABLES = (
    "backtest_run",
    "backtest_trial",
    "backtest_report",
    "backtest_scenario",
    "backtest_trade_outcome",
    "backtest_run_dataset_rights",
)
RIGHTS_METADATA_TABLES = ("source_policy_revision", "source_policy_observation")
BEGIN_SIGNATURE = "public.begin_backtest_job_persist(uuid, uuid, text, integer)"
COMPLETE_SIGNATURE = "public.complete_backtest_job_persist(uuid, uuid, text, integer, uuid[])"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    # Role được provisioning cấp riêng secret; migration chỉ bổ sung quyền dữ liệu cần thiết.
    op.execute(
        f"""DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles r
                WHERE r.rolname = '{REPLAY_WRITER}' AND r.rolcanlogin
                    AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolinherit
                        OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication)
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members m
                        WHERE m.roleid = r.oid OR m.member = r.oid
                    )
            ) THEN
                RAISE EXCEPTION 'WP06_REPLAY_WRITER_ROLE_UNSAFE';
            END IF;
        END $$"""
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {REPLAY_WRITER}")

    # Recheck source rights trong persist transaction chỉ cần policy/receipt metadata.
    for table in RIGHTS_METADATA_TABLES:
        op.execute(
            f"CREATE POLICY replay_writer_select ON public.{table} FOR SELECT "
            f"TO {REPLAY_WRITER} USING ({WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {REPLAY_WRITER}")

    # Rights receipt ban đầu chỉ cho admin/trainer; mở policy cho writer đúng workspace.
    op.execute("DROP POLICY workspace_isolation ON public.backtest_run_dataset_rights")
    op.execute(
        f"CREATE POLICY workspace_isolation ON public.backtest_run_dataset_rights "
        f"TO {ADMIN}, {TRAINER}, {REPLAY_WRITER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    for table in PERSIST_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO {REPLAY_WRITER}")
        op.execute(f"REVOKE INSERT ON TABLE public.{table} FROM {TRAINER}")

    for signature in (BEGIN_SIGNATURE, COMPLETE_SIGNATURE):
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM {TRAINER}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {REPLAY_WRITER}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_entry_features_valid(jsonb) TO {REPLAY_WRITER}"
    )


def downgrade() -> None:
    # Quay về quyền cũ chỉ cho phép khi không có thay đổi dữ liệu; schema không bị xóa.
    op.execute(
        "REVOKE ALL PRIVILEGES ON FUNCTION public.wp06_entry_features_valid(jsonb) "
        f"FROM {REPLAY_WRITER}"
    )
    for signature in (COMPLETE_SIGNATURE, BEGIN_SIGNATURE):
        op.execute(f"REVOKE ALL PRIVILEGES ON FUNCTION {signature} FROM {REPLAY_WRITER}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {TRAINER}")
    for table in reversed(PERSIST_TABLES):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM {REPLAY_WRITER}")
        op.execute(f"GRANT INSERT ON TABLE public.{table} TO {TRAINER}")
    for table in RIGHTS_METADATA_TABLES:
        op.execute(f"DROP POLICY replay_writer_select ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {REPLAY_WRITER}")
    op.execute("DROP POLICY workspace_isolation ON public.backtest_run_dataset_rights")
    op.execute(
        f"CREATE POLICY workspace_isolation ON public.backtest_run_dataset_rights "
        f"TO {ADMIN}, {TRAINER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {REPLAY_WRITER}")
