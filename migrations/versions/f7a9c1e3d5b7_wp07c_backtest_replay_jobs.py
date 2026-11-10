"""WP-07C durable replay queue and least-privilege worker reads."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a9c1e3d5b7"
down_revision: str | None = "e7c9a1b3d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
WORKER = "crypidian_backtest_worker"
TRAINER = "crypidian_trainer_worker"
INGESTION = "crypidian_ingestion_worker"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{WORKER}') THEN
                RAISE EXCEPTION 'WP07C_BACKTEST_WORKER_ROLE_MISSING';
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = '{WORKER}'
                AND (r.rolsuper OR r.rolbypassrls OR r.rolreplication OR r.rolcreaterole
                     OR r.rolcreatedb OR r.rolinherit OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members m
                        WHERE m.roleid = r.oid OR m.member = r.oid
                     ))
            ) THEN RAISE EXCEPTION 'WP07C_BACKTEST_WORKER_ROLE_UNSAFE'; END IF;
        END $$"""
    )

    op.create_table(
        "backtest_replay_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("strategy_release_id", sa.String(120), nullable=False),
        sa.Column("execution_interval", sa.String(12), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_request", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), server_default="QUEUED", nullable=False),
        sa.Column("stage", sa.String(64), server_default="queued", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_by", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "result_run_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="backtest_replay_job_status_valid",
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="backtest_replay_job_progress_valid"),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3", name="backtest_replay_job_attempts_valid"
        ),
        sa.CheckConstraint("lease_generation >= 0", name="backtest_replay_job_generation_valid"),
        sa.CheckConstraint(
            "bundle_hash ~ '^[0-9a-f]{64}$'", name="backtest_replay_job_bundle_sha256"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(request_payload) = 'object'", name="backtest_replay_job_payload_object"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_run_ids) = 'array'", name="backtest_replay_job_results_array"
        ),
        sa.CheckConstraint(
            "(cancel_requested AND cancel_requested_at IS NOT NULL "
            "AND cancel_requested_by IS NOT NULL) "
            "OR (NOT cancel_requested AND cancel_requested_at IS NULL "
            "AND cancel_requested_by IS NULL)",
            name="backtest_replay_job_cancel_evidence_consistent",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) "
            "OR (status <> 'RUNNING' AND lease_owner IS NULL AND lease_until IS NULL)",
            name="backtest_replay_job_lease_consistent",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "requested_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_backtest_replay_job_requested_by_membership",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "cancel_requested_by"],
            ["user_membership.workspace_id", "user_membership.id"],
            name="fk_backtest_replay_job_cancel_requested_by_membership",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_replay_job")),
    )
    op.create_index(
        "ix_backtest_replay_job_workspace_created",
        "backtest_replay_job",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_backtest_replay_job_claim",
        "backtest_replay_job",
        ["workspace_id", "status", "created_at"],
    )
    op.execute('ALTER TABLE public."backtest_replay_job" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE public."backtest_replay_job" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY backtest_replay_job_select ON public."backtest_replay_job" '
        f"FOR SELECT TO {ADMIN}, {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY backtest_replay_job_insert ON public."backtest_replay_job" '
        f"FOR INSERT TO {ADMIN} WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY backtest_replay_job_update ON public."backtest_replay_job" '
        f"FOR UPDATE TO {ADMIN}, {WORKER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        """
        CREATE FUNCTION public.guard_backtest_replay_job_update() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR
               (OLD.id, OLD.workspace_id, OLD.requested_by, OLD.bundle_hash,
                OLD.strategy_release_id, OLD.execution_interval, OLD.request_payload,
                OLD.canonical_request, OLD.created_at)
               IS DISTINCT FROM
               (NEW.id, NEW.workspace_id, NEW.requested_by, NEW.bundle_hash,
                NEW.strategy_release_id, NEW.execution_interval, NEW.request_payload,
                NEW.canonical_request, NEW.created_at) THEN
                RAISE EXCEPTION 'WP07C_REPLAY_JOB_REQUEST_IMMUTABLE';
            END IF;
            IF OLD.cancel_requested AND
               (OLD.cancel_requested, OLD.cancel_requested_at, OLD.cancel_requested_by)
               IS DISTINCT FROM
               (NEW.cancel_requested, NEW.cancel_requested_at, NEW.cancel_requested_by) THEN
                RAISE EXCEPTION 'WP07C_REPLAY_JOB_CANCEL_EVIDENCE_IMMUTABLE';
            END IF;
            IF OLD.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED') THEN
                RAISE EXCEPTION 'WP07C_REPLAY_JOB_TERMINAL_IMMUTABLE';
            END IF;
            IF NOT (
                (OLD.status = 'QUEUED' AND NEW.status IN (
                    'QUEUED', 'RUNNING', 'CANCELLED', 'FAILED'
                ))
                OR (OLD.status = 'RUNNING' AND NEW.status IN (
                    'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'
                ))
            ) THEN
                RAISE EXCEPTION 'WP07C_REPLAY_JOB_TRANSITION_INVALID';
            END IF;
            IF NEW.progress < OLD.progress OR NEW.lease_generation < OLD.lease_generation
               OR NEW.attempt_count < OLD.attempt_count THEN
                RAISE EXCEPTION 'WP07C_REPLAY_JOB_PROGRESS_REGRESSION';
            END IF;
            NEW.updated_at := pg_catalog.clock_timestamp();
            RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER backtest_replay_job_guard BEFORE UPDATE OR DELETE "
        'ON public."backtest_replay_job" FOR EACH ROW '
        "EXECUTE FUNCTION public.guard_backtest_replay_job_update()"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.backtest_replay_job FROM PUBLIC, {ADMIN}, {WORKER}"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.backtest_replay_job TO {ADMIN}")
    op.execute(
        f"GRANT UPDATE (cancel_requested, cancel_requested_at, cancel_requested_by, updated_at) "
        f"ON TABLE public.backtest_replay_job TO {ADMIN}"
    )
    op.execute(f"GRANT SELECT ON TABLE public.backtest_replay_job TO {WORKER}")
    op.execute(
        f"GRANT UPDATE (status, stage, progress, attempt_count, lease_owner, "
        f"lease_generation, lease_until, result_run_ids, error_code, updated_at) "
        f"ON TABLE public.backtest_replay_job TO {WORKER}"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO " + WORKER)

    # Worker đọc dữ liệu theo workspace; ghi outcomes chỉ qua trainer_worker riêng.
    op.execute('DROP POLICY workspace_isolation ON public."backtest_dataset_manifest"')
    op.execute(
        f'CREATE POLICY workspace_isolation ON public."backtest_dataset_manifest" '
        f"TO {ADMIN} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY backtest_worker_select ON public."backtest_dataset_manifest" '
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.backtest_dataset_manifest TO {WORKER}")
    op.execute('DROP POLICY workspace_isolation ON public."backtest_dataset_replay_bundle"')
    op.execute(
        f'CREATE POLICY workspace_isolation ON public."backtest_dataset_replay_bundle" '
        f"TO {ADMIN} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f'CREATE POLICY backtest_worker_select ON public."backtest_dataset_replay_bundle" '
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.backtest_dataset_replay_bundle TO {WORKER}")
    op.execute("DROP POLICY workspace_isolation ON public.source_policy_revision")
    op.execute(
        f"CREATE POLICY workspace_isolation ON public.source_policy_revision "
        f"TO {ADMIN}, {INGESTION}, {TRAINER}, {WORKER} USING ({_WORKSPACE}) "
        f"WITH CHECK ({_WORKSPACE})"
    )
    op.execute("DROP POLICY workspace_select ON public.source_policy_observation")
    op.execute(
        f"CREATE POLICY workspace_select ON public.source_policy_observation "
        f"FOR SELECT TO {ADMIN}, {INGESTION}, {TRAINER}, {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        f"GRANT SELECT ON TABLE public.source_policy_revision, "
        f"public.source_policy_observation TO {WORKER}"
    )
    op.execute(f"GRANT SELECT ON TABLE public.market_instrument, public.market_bar TO {WORKER}")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.backtest_replay_job) "
        "THEN RAISE EXCEPTION 'WP07C_REPLAY_JOB_DOWNGRADE_REQUIRES_EMPTY_TABLE'; "
        "END IF; END $$"
    )
    op.execute("DROP TRIGGER backtest_replay_job_guard ON public.backtest_replay_job")
    op.execute("DROP FUNCTION public.guard_backtest_replay_job_update()")
    op.execute('DROP POLICY backtest_replay_job_update ON public."backtest_replay_job"')
    op.execute('DROP POLICY backtest_replay_job_insert ON public."backtest_replay_job"')
    op.execute('DROP POLICY backtest_replay_job_select ON public."backtest_replay_job"')
    op.execute('ALTER TABLE public."backtest_replay_job" DISABLE ROW LEVEL SECURITY')
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.backtest_replay_job FROM {ADMIN}, {WORKER}")
    op.drop_index("ix_backtest_replay_job_claim", table_name="backtest_replay_job")
    op.drop_index("ix_backtest_replay_job_workspace_created", table_name="backtest_replay_job")
    op.drop_table("backtest_replay_job")

    op.execute(f"REVOKE SELECT ON TABLE public.backtest_dataset_manifest FROM {WORKER}")
    op.execute('DROP POLICY backtest_worker_select ON public."backtest_dataset_manifest"')
    op.execute('DROP POLICY workspace_isolation ON public."backtest_dataset_manifest"')
    op.execute(
        f'CREATE POLICY workspace_isolation ON public."backtest_dataset_manifest" '
        f"FOR SELECT TO {ADMIN} USING ({_WORKSPACE})"
    )
    op.execute(f"REVOKE SELECT ON TABLE public.backtest_dataset_replay_bundle FROM {WORKER}")
    op.execute('DROP POLICY backtest_worker_select ON public."backtest_dataset_replay_bundle"')
    op.execute('DROP POLICY workspace_isolation ON public."backtest_dataset_replay_bundle"')
    op.execute(
        f'CREATE POLICY workspace_isolation ON public."backtest_dataset_replay_bundle" '
        f"TO {ADMIN} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute(
        f"REVOKE SELECT ON TABLE public.source_policy_revision, "
        f"public.source_policy_observation FROM {WORKER}"
    )
    op.execute("DROP POLICY workspace_isolation ON public.source_policy_revision")
    op.execute(
        f"CREATE POLICY workspace_isolation ON public.source_policy_revision "
        f"TO {ADMIN}, {INGESTION}, {TRAINER} USING ({_WORKSPACE}) WITH CHECK ({_WORKSPACE})"
    )
    op.execute("DROP POLICY workspace_select ON public.source_policy_observation")
    op.execute(
        f"CREATE POLICY workspace_select ON public.source_policy_observation "
        f"FOR SELECT TO {ADMIN}, {INGESTION}, {TRAINER} USING ({_WORKSPACE})"
    )
    op.execute(f"REVOKE SELECT ON TABLE public.market_instrument, public.market_bar FROM {WORKER}")
