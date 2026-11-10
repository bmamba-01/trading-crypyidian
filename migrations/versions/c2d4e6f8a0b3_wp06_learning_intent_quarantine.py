"""WP06: cho candidate builder xác minh intent gắn trước replay."""

from collections.abc import Sequence

from alembic import op

revision: str = "c2d4e6f8a0b3"
down_revision: str | None = "f1b3d5f7a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUILDER = "crypidian_candidate_builder"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname='{BUILDER}'
                AND r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolinherit
                OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication)
                AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                    WHERE m.roleid=r.oid OR m.member=r.oid)) THEN
                RAISE EXCEPTION 'WP06_CANDIDATE_BUILDER_ROLE_UNSAFE';
            END IF;
        END $$"""
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {BUILDER}")
    op.execute(
        f"CREATE POLICY wp06_candidate_learning_job_select ON public.backtest_replay_job "
        f"FOR SELECT TO {BUILDER} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.backtest_replay_job TO {BUILDER}")


def downgrade() -> None:
    op.execute("DROP POLICY wp06_candidate_learning_job_select ON public.backtest_replay_job")
    op.execute(f"REVOKE SELECT ON TABLE public.backtest_replay_job FROM {BUILDER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {BUILDER}")
