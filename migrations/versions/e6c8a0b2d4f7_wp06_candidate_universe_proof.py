"""WP06: proof tập trade BASE và receipt TRAINING bất biến, tenant scoped."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6c8a0b2d4f7"
down_revision: str | None = "e5b7c9d1f3a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRAINER = "crypidian_trainer_worker"
ADMIN = "crypidian_admin_web"
BUILDER = "crypidian_candidate_builder"
SOURCE_TABLES = (
    "backtest_run",
    "backtest_trial",
    "backtest_report",
    "backtest_scenario",
    "backtest_trade_outcome",
)
LOCKER = "crypidian_candidate_locker"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
TABLES = ("candidate_universe_proof", "candidate_training_rights")
READ_TABLES = (
    "source_policy_revision",
    "source_policy_observation",
    "backtest_dataset_replay_bundle",
    "backtest_dataset_manifest",
)


def _identity_columns():
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "candidate_universe_proof",
        *_identity_columns(),
        *(
            sa.Column(field, sa.Uuid(), nullable=False)
            for field in ("run_id", "trial_record_id", "report_id", "scenario_id")
        ),
        sa.Column("query_version", sa.String(64), nullable=False),
        *(
            sa.Column(field, sa.String(64), nullable=False)
            for field in ("manifest_hash", "proof_hash", "membership_digest")
        ),
        *(
            sa.Column(field, sa.Integer(), nullable=False)
            for field in ("candidate_count", "eligible_count", "exclusion_count")
        ),
        sa.Column("canonical_proof", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "query_version",
            name="uq_candidate_universe_proof_trial_query",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_candidate_universe_proof_workspace"),
        *(
            sa.ForeignKeyConstraint(
                ["workspace_id", field],
                [f"{table}.workspace_id", f"{table}.id"],
                ondelete="RESTRICT",
            )
            for field, table in (
                ("run_id", "backtest_run"),
                ("trial_record_id", "backtest_trial"),
                ("report_id", "backtest_report"),
                ("scenario_id", "backtest_scenario"),
            )
        ),
        sa.CheckConstraint(
            "candidate_count >= 0 AND eligible_count >= 0 AND exclusion_count >= 0 AND "
            "candidate_count = eligible_count + exclusion_count",
            name="candidate_universe_proof_counts_valid",
        ),
        sa.CheckConstraint(
            "proof_hash ~ '^[0-9a-f]{64}$' AND membership_digest ~ '^[0-9a-f]{64}$' AND "
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="candidate_universe_proof_hashes_valid",
        ),
        sa.CheckConstraint(
            "query_version = 'base-closed-trade-universe-v1'",
            name="candidate_universe_proof_query_valid",
        ),
    )
    op.create_table(
        "candidate_training_rights",
        *_identity_columns(),
        sa.Column("proof_id", sa.Uuid(), nullable=False),
        *(
            sa.Column(field, sa.String(64), nullable=False)
            for field in ("child_dataset_hash", "policy_fingerprint", "evidence_hash")
        ),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("canonical_evidence", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "proof_id",
            "child_dataset_hash",
            name="uq_candidate_training_rights_child",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            ["candidate_universe_proof.workspace_id", "candidate_universe_proof.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("purpose = 'training'", name="candidate_training_rights_purpose_valid"),
        sa.CheckConstraint(
            "child_dataset_hash ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ "
            "'^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'",
            name="candidate_training_rights_hashes_valid",
        ),
    )
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{LOCKER}') THEN
            CREATE ROLE {LOCKER} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
            NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE rolname='{LOCKER}' AND
            (rolcanlogin OR rolsuper OR rolbypassrls OR rolinherit OR rolcreatedb OR
            rolcreaterole OR rolreplication OR EXISTS (SELECT 1 FROM
            pg_catalog.pg_auth_members m WHERE m.roleid=r.oid OR m.member=r.oid)))
            THEN
            RAISE EXCEPTION 'WP06_CANDIDATE_LOCKER_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {LOCKER}, {BUILDER}")
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE rolname='{BUILDER}'
            AND rolcanlogin AND NOT (rolsuper OR rolbypassrls OR rolinherit OR rolcreatedb
            OR rolcreaterole OR rolreplication)
            AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                WHERE m.roleid=r.oid OR m.member=r.oid)) THEN
            RAISE EXCEPTION 'WP06_CANDIDATE_BUILDER_ROLE_UNSAFE';
        END IF;
    END $$""")
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY candidate_select ON public.{table} FOR SELECT TO {ADMIN}, "
            f"{TRAINER}, {BUILDER} USING ({WORKSPACE})"
        )
        op.execute(
            f"CREATE POLICY candidate_insert ON public.{table} FOR INSERT TO {BUILDER} "
            f"WITH CHECK ({WORKSPACE})"
        )
        op.execute(
            f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, crypidian_app, "
            f"crypidian_trading_runtime, crypidian_ingestion_worker, "
            f"crypidian_execution_gateway, crypidian_backtest_worker, "
            f"crypidian_signal_worker, crypidian_market_signal_worker, "
            f"{ADMIN}, {TRAINER}, {BUILDER}"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {ADMIN}")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {TRAINER}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO {BUILDER}")
    for table in (*READ_TABLES, *SOURCE_TABLES):
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {BUILDER}")
        op.execute(
            f"CREATE POLICY candidate_builder_select ON public.{table} FOR SELECT TO "
            f"{BUILDER} USING ({WORKSPACE})"
        )
    op.execute(
        f"GRANT SELECT (workspace_id, id), UPDATE (id) ON TABLE public.backtest_trial TO {LOCKER}"
    )
    op.execute(
        f"CREATE POLICY candidate_locker_select ON public.backtest_trial FOR SELECT "
        f"TO {LOCKER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY candidate_locker_update ON public.backtest_trial FOR UPDATE "
        f"TO {LOCKER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"GRANT SELECT (workspace_id, trial_record_id) ON TABLE "
        f"public.candidate_universe_proof TO {LOCKER}"
    )
    op.execute(
        f"CREATE POLICY candidate_locker_select ON public.candidate_universe_proof "
        f"FOR SELECT TO {LOCKER} USING ({WORKSPACE})"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_lock_candidate_trial(target_workspace uuid, target_trial uuid)
        RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$ BEGIN
            IF target_workspace IS DISTINCT FROM
            NULLIF(current_setting('app.workspace_id', true), '')::uuid THEN
                RETURN false;
            END IF;
            PERFORM id FROM public.backtest_trial WHERE
            workspace_id=target_workspace AND id=target_trial FOR UPDATE;
            RETURN FOUND;
        END $$""")
    op.execute(f"ALTER FUNCTION public.wp06_lock_candidate_trial(uuid, uuid) OWNER TO {LOCKER}")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_lock_candidate_trial(uuid, uuid) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_lock_candidate_trial(uuid, uuid) TO {BUILDER}"
    )
    op.execute("""CREATE FUNCTION public.wp06_guard_outcome_insert() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$ BEGIN
            -- Không khóa tenant khác; INSERT của product role phải qua RLS WITH CHECK.
            IF NEW.workspace_id IS DISTINCT FROM
            NULLIF(current_setting('app.workspace_id', true), '')::uuid THEN
                RETURN NEW;
            END IF;
            PERFORM id FROM public.backtest_trial WHERE
            workspace_id=NEW.workspace_id AND id=NEW.trial_record_id FOR UPDATE;
            IF EXISTS (SELECT 1 FROM public.candidate_universe_proof WHERE
            workspace_id=NEW.workspace_id AND trial_record_id=NEW.trial_record_id)
            THEN
                RAISE EXCEPTION 'WP06_OUTCOME_TRIAL_SEALED_BY_PROOF';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(f"ALTER FUNCTION public.wp06_guard_outcome_insert() OWNER TO {LOCKER}")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_guard_outcome_insert() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER wp06_outcome_insert_fence BEFORE INSERT ON "
        "public.backtest_trade_outcome FOR EACH ROW EXECUTE FUNCTION "
        "public.wp06_guard_outcome_insert()"
    )
    op.execute("""CREATE FUNCTION public.reject_candidate_proof_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ BEGIN
            RAISE EXCEPTION 'WP06_CANDIDATE_PROOF_IMMUTABLE';
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION public.reject_candidate_proof_mutation() FROM PUBLIC")
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER candidate_immutable BEFORE UPDATE OR DELETE ON "
            f"public.{table} FOR EACH ROW EXECUTE FUNCTION "
            f"public.reject_candidate_proof_mutation()"
        )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.candidate_universe_proof, "
        "public.candidate_training_rights, public.backtest_trade_outcome IN ACCESS "
        "EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.candidate_universe_proof)
        OR EXISTS (SELECT 1 FROM public.candidate_training_rights) THEN
        RAISE EXCEPTION 'WP06_CANDIDATE_PROOF_DOWNGRADE_REQUIRES_EMPTY_TABLES'; END IF; END $$""")
    op.execute("DROP TRIGGER wp06_outcome_insert_fence ON public.backtest_trade_outcome")
    op.execute("DROP FUNCTION public.wp06_guard_outcome_insert()")
    op.execute("DROP FUNCTION public.wp06_lock_candidate_trial(uuid, uuid)")
    for table in TABLES:
        op.execute(f"DROP TRIGGER candidate_immutable ON public.{table}")
    op.execute("DROP FUNCTION public.reject_candidate_proof_mutation()")
    op.execute("DROP POLICY candidate_locker_select ON public.backtest_trial")
    op.execute("DROP POLICY candidate_locker_update ON public.backtest_trial")
    op.execute(
        f"REVOKE SELECT (workspace_id, id), UPDATE (id) ON TABLE "
        f"public.backtest_trial FROM {LOCKER}"
    )
    op.execute(f"REVOKE ALL ON TABLE public.backtest_trial FROM {LOCKER}")
    for table in (*READ_TABLES, *SOURCE_TABLES):
        op.execute(f"DROP POLICY candidate_builder_select ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {BUILDER}")
    for table in reversed(TABLES):
        op.drop_table(table)
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {LOCKER}")
    op.execute(f"DROP ROLE {LOCKER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {BUILDER}")
