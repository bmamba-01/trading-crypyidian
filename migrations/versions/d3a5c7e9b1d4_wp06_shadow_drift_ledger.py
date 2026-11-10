"""WP06 Task7: lưu observation, assessment và retrain proposal metadata-only."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3a5c7e9b1d4"
down_revision: str | None = "d2b4c6e8f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OBSERVER = "crypidian_learning_shadow_observer"
ADMIN = "crypidian_admin_web"
APP = "crypidian_app"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
TABLES = (
    "learning_shadow_observation",
    "learning_drift_assessment",
    "learning_drift_assessment_member",
    "learning_retrain_proposal",
)
SOURCE_TABLES = (
    "bot_decision",
    "shared_signal_evidence",
    "feature_snapshot",
    "learning_model_registry",
    "learning_model_registry_event",
    "learning_model_active_pointer",
    "learning_training_artifact",
)
PROCESS_ROLES = (
    "crypidian_trading_runtime",
    "crypidian_execution_gateway",
    "crypidian_ingestion_worker",
    "crypidian_signal_worker",
    "crypidian_market_signal_worker",
    "crypidian_candidate_builder",
    "crypidian_replay_writer",
    "crypidian_trainer_worker",
    "crypidian_learning_evaluator",
    "crypidian_backtest_worker",
)
OBSERVER_COLUMNS = {
    "bot_decision": (
        "workspace_id",
        "id",
        "evidence_id",
        "evidence_hash",
        "strategy_release_id",
        "strategy_release_hash",
        "evaluated_at",
        "status",
        "decision_hash",
    ),
    "shared_signal_evidence": (
        "workspace_id",
        "id",
        "feature_snapshot_id",
        "available_at",
        "evidence_hash",
        "schema_version",
    ),
    "feature_snapshot": (
        "workspace_id",
        "snapshot_id",
        "content_hash",
        "symbol",
        "timeframe",
        "schema_hash",
        "as_of",
        "computed_at",
        "quality_flags",
        "data_freshness",
        "data_coverage",
        "is_usable",
    ),
    "learning_model_registry": (
        "workspace_id",
        "id",
        "artifact_id",
        "artifact_hash",
        "manifest_hash",
        "fold_plan_hash",
        "approval_hash",
        "state",
        "revision",
    ),
    "learning_model_registry_event": (
        "workspace_id",
        "registry_id",
        "id",
        "sequence",
        "action",
        "to_state",
        "mfa_verified_at",
    ),
    "learning_model_active_pointer": ("workspace_id", "registry_id", "version", "changed_at"),
    "learning_training_artifact": ("workspace_id", "id", "feature_schema_hash"),
}


def upgrade() -> None:
    _ensure_observer_role()
    op.create_unique_constraint(
        "uq_learning_model_registry_event_exact_entry",
        "learning_model_registry_event",
        ["workspace_id", "registry_id", "id"],
    )
    _create_tables()
    _create_guards()
    _create_policies_and_grants()


def _ensure_observer_role() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{OBSERVER}'
        ) THEN
            CREATE ROLE {OBSERVER} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB
                NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        ELSIF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
             WHERE role.rolname = '{OBSERVER}'
               AND (role.rolsuper OR role.rolbypassrls OR role.rolinherit
                    OR role.rolcreatedb OR role.rolcreaterole OR role.rolreplication)
        ) THEN
            RAISE EXCEPTION 'WP06_SHADOW_OBSERVER_ROLE_UNSAFE';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
             WHERE membership.roleid = (
                       SELECT oid FROM pg_catalog.pg_roles WHERE rolname = '{OBSERVER}'
                   )
                OR membership.member = (
                       SELECT oid FROM pg_catalog.pg_roles WHERE rolname = '{OBSERVER}'
                   )
        ) THEN
            RAISE EXCEPTION 'WP06_SHADOW_OBSERVER_ROLE_MUST_NOT_INHERIT_GROUP_GRANTS';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {OBSERVER}")


def _create_tables() -> None:
    jsonb_type = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "learning_shadow_observation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("incumbent_decision_id", sa.Uuid(), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("strategy_release_id", sa.String(80), nullable=False),
        sa.Column("strategy_release_hash", sa.String(64), nullable=False),
        sa.Column("incumbent_status", sa.String(16), nullable=False),
        sa.Column("decision_evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_schema_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("challenger_registry_id", sa.Uuid(), nullable=False),
        sa.Column("active_registry_id", sa.Uuid(), nullable=False),
        sa.Column("active_registry_revision", sa.Integer(), nullable=False),
        sa.Column("active_artifact_hash", sa.String(64), nullable=False),
        sa.Column("challenger_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("challenger_artifact_hash", sa.String(64), nullable=False),
        sa.Column("challenger_manifest_hash", sa.String(64), nullable=False),
        sa.Column("challenger_fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("training_approval_hash", sa.String(64), nullable=False),
        sa.Column("challenger_revision", sa.Integer(), nullable=False),
        sa.Column("challenger_state", sa.String(24), nullable=False),
        sa.Column("registry_approval_event_id", sa.Uuid(), nullable=False),
        sa.Column("registry_approval_event_hash", sa.String(64), nullable=False),
        sa.Column("registry_approval_event_sequence", sa.Integer(), nullable=False),
        sa.Column("active_pointer_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_coverage", sa.Float(), nullable=False),
        sa.Column("snapshot_quality_ok", sa.Boolean(), nullable=False),
        sa.Column("schema_compatible", sa.Boolean(), nullable=False),
        sa.Column("challenger_decision", sa.String(16), nullable=False),
        sa.Column("challenger_reason", sa.String(40), nullable=False),
        sa.Column("rights_eligible", sa.Boolean(), nullable=False),
        sa.Column("rights_fingerprint", sa.String(64), nullable=True),
        sa.Column("rights_source_id", sa.String(128), nullable=True),
        sa.Column("rights_policy_id", sa.String(160), nullable=True),
        sa.Column("rights_policy_sequence", sa.Integer(), nullable=True),
        sa.Column("rights_known_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "decision_hash ~ '^[0-9a-f]{64}$' AND strategy_release_hash ~ '^[0-9a-f]{64}$' "
            "AND snapshot_hash ~ '^[0-9a-f]{64}$' AND snapshot_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND active_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND challenger_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND challenger_manifest_hash ~ '^[0-9a-f]{64}$' "
            "AND challenger_fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND training_approval_hash ~ '^[0-9a-f]{64}$' "
            "AND registry_approval_event_hash ~ '^[0-9a-f]{64}$' "
            "AND idempotency_key ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_shadow_observation_hashes_valid",
        ),
        sa.CheckConstraint(
            "incumbent_status IN ('CANDIDATE', 'ABSTAIN', 'VETO')",
            name="learning_shadow_observation_incumbent_status_valid",
        ),
        sa.CheckConstraint(
            "challenger_state = 'evaluated' AND challenger_decision = 'abstain'",
            name="learning_shadow_observation_fail_closed",
        ),
        sa.CheckConstraint(
            "challenger_reason IN ('UNCALIBRATED_SCORE', 'FEATURE_SCHEMA_MISMATCH', "
            "'SOURCE_RIGHTS_INELIGIBLE', 'MODEL_UNAVAILABLE', 'MISSING_EVIDENCE')",
            name="learning_shadow_observation_reason_valid",
        ),
        sa.CheckConstraint(
            "length(btrim(strategy_release_id)) BETWEEN 1 AND 80 "
            "AND challenger_revision >= 1 AND registry_approval_event_sequence >= 1 "
            "AND active_registry_revision >= 1 AND active_pointer_version >= 1 "
            "AND active_registry_id <> challenger_registry_id",
            name="learning_shadow_observation_reference_valid",
        ),
        sa.CheckConstraint(
            "snapshot_coverage BETWEEN 0 AND 1",
            name="learning_shadow_observation_snapshot_metrics_valid",
        ),
        sa.CheckConstraint(
            "(rights_eligible AND rights_fingerprint ~ '^[0-9a-f]{64}$') OR "
            "(NOT rights_eligible AND rights_fingerprint IS NULL)",
            name="learning_shadow_observation_rights_valid",
        ),
        sa.CheckConstraint(
            "(rights_eligible AND rights_source_id IS NOT NULL AND rights_policy_id IS NOT NULL "
            "AND rights_policy_sequence IS NOT NULL AND rights_known_at IS NOT NULL) OR "
            "(NOT rights_eligible AND rights_source_id IS NULL AND rights_policy_id IS NULL "
            "AND rights_policy_sequence IS NULL AND rights_known_at IS NULL)",
            name="learning_shadow_observation_rights_receipt_valid",
        ),
        sa.CheckConstraint(
            "rights_known_at IS NULL OR rights_known_at <= observed_at",
            name="learning_shadow_observation_rights_time_valid",
        ),
        sa.CheckConstraint(
            "snapshot_as_of <= decision_evaluated_at AND snapshot_as_of <= observed_at "
            "AND decision_evaluated_at <= observed_at",
            name="learning_shadow_observation_time_order",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "incumbent_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_decision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "snapshot_id"],
            ["feature_snapshot.workspace_id", "feature_snapshot.snapshot_id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "challenger_registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_registry",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "active_registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_active_registry",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "rights_source_id", "rights_policy_sequence"],
            [
                "source_policy_revision.workspace_id",
                "source_policy_revision.source_id",
                "source_policy_revision.sequence",
            ],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_rights_policy_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "rights_source_id", "rights_policy_sequence"],
            [
                "source_policy_observation.workspace_id",
                "source_policy_observation.source_id",
                "source_policy_observation.sequence",
            ],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_rights_receipt_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "rights_policy_id"],
            ["source_policy_revision.workspace_id", "source_policy_revision.policy_id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_rights_policy_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "rights_policy_id"],
            ["source_policy_observation.workspace_id", "source_policy_observation.policy_id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_rights_receipt_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "challenger_registry_id", "registry_approval_event_id"],
            [
                "learning_model_registry_event.workspace_id",
                "learning_model_registry_event.registry_id",
                "learning_model_registry_event.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_registry_approval",
        ),
        sa.ForeignKeyConstraint(
            ["challenger_artifact_id"],
            ["learning_training_artifact.id"],
            ondelete="RESTRICT",
            name="fk_learning_shadow_observation_artifact",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_shadow_observation")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_learning_shadow_observation_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_shadow_observation_idempotency"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "incumbent_decision_id",
            "challenger_registry_id",
            "challenger_revision",
            "snapshot_hash",
            name="uq_learning_shadow_observation_logical_identity",
        ),
    )
    op.create_index(
        "ix_learning_shadow_observation_workspace_observed",
        "learning_shadow_observation",
        ["workspace_id", "observed_at", "id"],
    )
    op.create_index(
        "ix_learning_shadow_observation_registry_revision",
        "learning_shadow_observation",
        ["workspace_id", "challenger_registry_id", "challenger_revision", "observed_at"],
    )

    op.create_table(
        "learning_drift_assessment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.String(120), nullable=False),
        sa.Column("cohort_registry_id", sa.Uuid(), nullable=True),
        sa.Column("cohort_revision", sa.Integer(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("minimum_observations", sa.Integer(), nullable=False),
        sa.Column("minimum_snapshot_coverage", sa.Float(), nullable=False),
        sa.Column("minimum_quality_rate", sa.Float(), nullable=False),
        sa.Column("snapshot_coverage_rate", sa.Float(), nullable=True),
        sa.Column("quality_rate", sa.Float(), nullable=True),
        sa.Column("abstention_rate", sa.Float(), nullable=True),
        sa.Column("data_quality_status", sa.String(16), nullable=False),
        sa.Column("performance_status", sa.String(16), nullable=False),
        sa.Column("reason_codes", jsonb_type, nullable=False),
        sa.Column("membership_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "length(btrim(policy_version)) BETWEEN 1 AND 120",
            name="learning_drift_assessment_policy_version_valid",
        ),
        sa.CheckConstraint(
            "length(membership_digest) = 64 AND membership_digest ~ '^[0-9a-f]{64}$' "
            "AND length(idempotency_key) = 64 AND idempotency_key ~ '^[0-9a-f]{64}$' "
            "AND length(content_hash) = 64 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_drift_assessment_hashes_valid",
        ),
        sa.CheckConstraint(
            "member_count >= 0 AND eligible_count >= 0 AND eligible_count <= member_count",
            name="learning_drift_assessment_counts_valid",
        ),
        sa.CheckConstraint(
            "(member_count = 0 AND cohort_registry_id IS NULL AND cohort_revision IS NULL "
            "AND snapshot_coverage_rate IS NULL AND quality_rate IS NULL "
            "AND abstention_rate IS NULL AND data_quality_status = 'inconclusive' "
            "AND reason_codes @> '[\"low_sample\"]'::jsonb) OR "
            "(member_count > 0 AND cohort_registry_id IS NOT NULL "
            "AND cohort_revision IS NOT NULL AND cohort_revision >= 1)",
            name=("ck_learning_drift_assessment_learning_drift_assessment_cohort_scope_valid"),
        ),
        sa.CheckConstraint(
            "minimum_observations >= 1 AND minimum_snapshot_coverage BETWEEN 0 AND 1 "
            "AND minimum_quality_rate BETWEEN 0 AND 1",
            name="learning_drift_assessment_policy_thresholds_valid",
        ),
        sa.CheckConstraint(
            "(member_count = 0 AND window_start IS NULL AND window_end IS NULL) OR "
            "(member_count > 0 AND window_start IS NOT NULL AND window_end IS NOT NULL "
            "AND window_start <= window_end)",
            name="learning_drift_assessment_window_valid",
        ),
        sa.CheckConstraint(
            "(snapshot_coverage_rate IS NULL OR snapshot_coverage_rate BETWEEN 0 AND 1) AND "
            "(quality_rate IS NULL OR quality_rate BETWEEN 0 AND 1) AND "
            "(abstention_rate IS NULL OR abstention_rate BETWEEN 0 AND 1)",
            name="learning_drift_assessment_rates_valid",
        ),
        sa.CheckConstraint(
            "data_quality_status IN ('detected', 'no_drift', 'inconclusive') "
            "AND performance_status = 'inconclusive'",
            name="learning_drift_assessment_status_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND "
            'reason_codes <@ \'["baseline_missing", "coverage_below_threshold", '
            '"low_sample", "performance_not_evaluable", "quality_below_threshold", '
            '"rights_unverified", "schema_incompatible"]\'::jsonb',
            name="learning_drift_assessment_reason_codes_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "cohort_registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            ondelete="RESTRICT",
            name="fk_learning_drift_assessment_cohort_registry",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_drift_assessment")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_drift_assessment_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_drift_assessment_idempotency"
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", "content_hash", name="uq_learning_drift_assessment_exact_content"
        ),
    )
    op.create_index(
        "ix_learning_drift_assessment_workspace_created",
        "learning_drift_assessment",
        ["workspace_id", "created_at", "id"],
    )

    op.create_table(
        "learning_drift_assessment_member",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_coverage", sa.Float(), nullable=False),
        sa.Column("snapshot_quality_ok", sa.Boolean(), nullable=False),
        sa.Column("baseline_available", sa.Boolean(), nullable=False),
        sa.Column("schema_compatible", sa.Boolean(), nullable=False),
        sa.Column("rights_eligible", sa.Boolean(), nullable=False),
        sa.Column("rights_fingerprint", sa.String(64), nullable=True),
        sa.Column("challenger_decision", sa.String(16), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="learning_drift_assessment_member_ordinal_valid"),
        sa.CheckConstraint(
            "snapshot_coverage BETWEEN 0 AND 1",
            name="learning_drift_assessment_member_coverage_valid",
        ),
        sa.CheckConstraint(
            "length(snapshot_hash) = 64 AND snapshot_hash ~ '^[0-9a-f]{64}$' "
            "AND (rights_fingerprint IS NULL OR (length(rights_fingerprint) = 64 "
            "AND rights_fingerprint ~ '^[0-9a-f]{64}$'))",
            name="learning_drift_assessment_member_hashes_valid",
        ),
        sa.CheckConstraint(
            "challenger_decision IN ('approve', 'veto', 'abstain')",
            name="learning_drift_assessment_member_decision_valid",
        ),
        sa.CheckConstraint(
            "eligible = (baseline_available AND schema_compatible AND rights_eligible)",
            name="learning_drift_assessment_member_eligibility_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "assessment_id"],
            ["learning_drift_assessment.workspace_id", "learning_drift_assessment.id"],
            ondelete="RESTRICT",
            name="fk_learning_drift_assessment_member_assessment",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "observation_id"],
            ["learning_shadow_observation.workspace_id", "learning_shadow_observation.id"],
            ondelete="RESTRICT",
            name="fk_learning_drift_assessment_member_observation",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "assessment_id",
            "observation_id",
            name=op.f("pk_learning_drift_assessment_member"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "assessment_id",
            "ordinal",
            name="uq_learning_drift_assessment_member_ordinal",
        ),
    )
    op.create_index(
        "ix_learning_drift_assessment_member_observation",
        "learning_drift_assessment_member",
        ["workspace_id", "observation_id"],
    )

    op.create_table(
        "learning_retrain_proposal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(120), nullable=False),
        sa.Column("membership_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("reason_codes", jsonb_type, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "length(btrim(policy_version)) BETWEEN 1 AND 120",
            name="learning_retrain_proposal_policy_version_valid",
        ),
        sa.CheckConstraint(
            "length(assessment_hash) = 64 AND assessment_hash ~ '^[0-9a-f]{64}$' "
            "AND length(membership_digest) = 64 AND membership_digest ~ '^[0-9a-f]{64}$' "
            "AND length(idempotency_key) = 64 AND idempotency_key ~ '^[0-9a-f]{64}$' "
            "AND length(content_hash) = 64 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="learning_retrain_proposal_hashes_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) > 0 "
            'AND reason_codes <@ \'["coverage_below_threshold", '
            '"quality_below_threshold"]\'::jsonb',
            name="learning_retrain_proposal_data_reasons_only",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "assessment_id", "assessment_hash"],
            [
                "learning_drift_assessment.workspace_id",
                "learning_drift_assessment.id",
                "learning_drift_assessment.content_hash",
            ],
            ondelete="RESTRICT",
            name="fk_learning_retrain_proposal_assessment_exact",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_retrain_proposal")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_retrain_proposal_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_retrain_proposal_idempotency"
        ),
    )
    op.create_index(
        "ix_learning_retrain_proposal_workspace_created",
        "learning_retrain_proposal",
        ["workspace_id", "created_at", "id"],
    )


def _create_policies_and_grants() -> None:
    for table in TABLES:
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY {table}_observer_select ON public."{table}" '
            f"FOR SELECT TO {OBSERVER} USING ({WORKSPACE})"
        )
        op.execute(
            f'CREATE POLICY {table}_observer_insert ON public."{table}" '
            f"FOR INSERT TO {OBSERVER} WITH CHECK ({WORKSPACE})"
        )
        op.execute(
            f'CREATE POLICY {table}_admin_select ON public."{table}" '
            f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
        )
        op.execute(
            f'REVOKE ALL PRIVILEGES ON TABLE public."{table}" FROM PUBLIC, {APP}, {ADMIN}, '
            f"{OBSERVER}, {', '.join(PROCESS_ROLES)}"
        )
        op.execute(f'GRANT SELECT ON TABLE public."{table}" TO {ADMIN}')
        op.execute(f'GRANT SELECT, INSERT ON TABLE public."{table}" TO {OBSERVER}')

    for table in SOURCE_TABLES:
        op.execute(
            f'CREATE POLICY {table}_learning_shadow_observer_select ON public."{table}" '
            f"FOR SELECT TO {OBSERVER} USING ({WORKSPACE})"
        )
    for table, columns in OBSERVER_COLUMNS.items():
        selected = ", ".join(columns)
        op.execute(f"GRANT SELECT ({selected}) ON TABLE public.{table} TO {OBSERVER}")

    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_learning_registry_approval_event_hash"
        f"(uuid, uuid, uuid) TO {OBSERVER}"
    )
    op.execute(f"""
        GRANT EXECUTE ON FUNCTION public.wp06_shadow_current_paper_rights_receipt(uuid)
            TO {OBSERVER}
    """)
    op.execute(f"""
        GRANT EXECUTE ON FUNCTION public.wp06_shadow_decision_source_rights_supported(uuid, uuid)
            TO {OBSERVER}
    """)
    op.execute(f"""
        GRANT EXECUTE ON FUNCTION public.wp06_shadow_market_source_rights_matches(text, text)
            TO {OBSERVER}
    """)
    op.execute(f"""
        GRANT EXECUTE ON FUNCTION public.wp06_canonical_source_cost(numeric)
            TO {OBSERVER}
    """)
    op.execute(f"""
        GRANT EXECUTE ON FUNCTION public.wp06_lock_learning_shadow_cohort(uuid, uuid, integer)
            TO {OBSERVER}
    """)


def _create_guards() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_compact_canonical_json(document json)
        RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            source_text text := document::text;
            character_parts text[] := ARRAY[]::text[];
            current_character text;
            in_string boolean := false;
            escaped boolean := false;
            position integer;
        BEGIN
            FOR position IN 1..pg_catalog.char_length(source_text) LOOP
                current_character := pg_catalog.substr(source_text, position, 1);
                IF in_string THEN
                    character_parts := pg_catalog.array_append(character_parts, current_character);
                    IF escaped THEN
                        escaped := false;
                    ELSIF current_character = pg_catalog.chr(92) THEN
                        escaped := true;
                    ELSIF current_character = '"' THEN
                        in_string := false;
                    END IF;
                ELSIF current_character = '"' THEN
                    in_string := true;
                    character_parts := pg_catalog.array_append(character_parts, current_character);
                ELSIF current_character NOT IN (' ', E'\\t', E'\\n', E'\\r') THEN
                    character_parts := pg_catalog.array_append(character_parts, current_character);
                END IF;
            END LOOP;
            RETURN pg_catalog.array_to_string(character_parts, '');
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_compact_canonical_json(json) FROM PUBLIC")
    op.execute("""
        CREATE FUNCTION public.wp06_canonical_float_json(value double precision)
        RETURNS json LANGUAGE plpgsql IMMUTABLE
        SET search_path = pg_catalog, pg_temp
        SET extra_float_digits = 1 AS $$
        DECLARE
            rendered text;
        BEGIN
            IF value IS NULL THEN
                RETURN 'null'::json;
            END IF;
            IF value::text IN ('NaN', 'Infinity', '-Infinity') THEN
                RAISE EXCEPTION 'WP06_CANONICAL_FLOAT_NOT_FINITE';
            END IF;
            rendered := value::text;
            IF rendered !~ '[.eE]' THEN
                rendered := rendered || '.0';
            END IF;
            RETURN rendered::json;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_canonical_float_json(double precision) FROM PUBLIC"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_canonical_utc_timestamp_json(value timestamptz)
        RETURNS json LANGUAGE plpgsql STABLE
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            utc_value timestamp without time zone;
            rendered text;
        BEGIN
            IF value IS NULL THEN
                RETURN 'null'::json;
            END IF;
            utc_value := value AT TIME ZONE 'UTC';
            IF pg_catalog.date_trunc('second', utc_value) = utc_value THEN
                rendered := pg_catalog.to_char(
                    utc_value,
                    'YYYY-MM-DD"T"HH24:MI:SS'
                );
            ELSE
                rendered := pg_catalog.to_char(
                    utc_value,
                    'YYYY-MM-DD"T"HH24:MI:SS.US'
                );
            END IF;
            RETURN pg_catalog.to_json(rendered || '+00:00');
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_canonical_utc_timestamp_json(timestamptz) FROM PUBLIC"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_sha256_canonical_json(document json)
        RETURNS text LANGUAGE sql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_compact_canonical_json(document), 'UTF8'
                )),
                'hex'
            )
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_sha256_canonical_json(json) FROM PUBLIC")
    op.execute("""
        CREATE FUNCTION public.wp06_shadow_observation_idempotency_key(
            row_value public.learning_shadow_observation
        ) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                'active_artifact_hash', row_value.active_artifact_hash,
                'active_pointer_version', row_value.active_pointer_version,
                'active_registry_id', row_value.active_registry_id::text,
                'active_registry_revision', row_value.active_registry_revision,
                'challenger_reason', row_value.challenger_reason,
                'challenger_registry_id', row_value.challenger_registry_id::text,
                'challenger_revision', row_value.challenger_revision,
                'incumbent_decision_hash', row_value.decision_hash,
                'incumbent_decision_id', row_value.incumbent_decision_id::text,
                'incumbent_strategy_release_hash', row_value.strategy_release_hash,
                'registry_approval_event_hash', row_value.registry_approval_event_hash,
                'registry_approval_event_id', row_value.registry_approval_event_id::text,
                'registry_approval_event_sequence', row_value.registry_approval_event_sequence,
                'rights_eligible', row_value.rights_eligible,
                'rights_fingerprint', row_value.rights_fingerprint,
                'rights_known_at', public.wp06_canonical_utc_timestamp_json(
                    row_value.rights_known_at
                ),
                'rights_policy_id', row_value.rights_policy_id,
                'rights_policy_sequence', row_value.rights_policy_sequence,
                'rights_source_id', row_value.rights_source_id,
                'schema_compatible', row_value.schema_compatible,
                'snapshot_coverage', public.wp06_canonical_float_json(
                    row_value.snapshot_coverage
                ),
                'snapshot_hash', row_value.snapshot_hash,
                'snapshot_quality_ok', row_value.snapshot_quality_ok,
                'workspace_id', row_value.workspace_id::text
            ))
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_shadow_observation_canonical_document(
            row_value public.learning_shadow_observation
        ) RETURNS json LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT pg_catalog.json_build_object(
                'active_artifact_hash', row_value.active_artifact_hash,
                'active_pointer_version', row_value.active_pointer_version,
                'active_registry_id', row_value.active_registry_id::text,
                'active_registry_revision', row_value.active_registry_revision,
                'challenger_model', pg_catalog.json_build_object(
                    'artifact_hash', row_value.challenger_artifact_hash,
                    'artifact_id', row_value.challenger_artifact_id::text,
                    'fold_plan_hash', row_value.challenger_fold_plan_hash,
                    'manifest_hash', row_value.challenger_manifest_hash,
                    'registry_approval_event_hash', row_value.registry_approval_event_hash,
                    'registry_approval_event_id', row_value.registry_approval_event_id::text,
                    'registry_approval_event_sequence',
                        row_value.registry_approval_event_sequence,
                    'registry_id', row_value.challenger_registry_id::text,
                    'revision', row_value.challenger_revision,
                    'state', row_value.challenger_state,
                    'training_approval_hash', row_value.training_approval_hash,
                    'workspace_id', row_value.workspace_id::text
                ),
                'challenger_reason', row_value.challenger_reason,
                'challenger_snapshot_state', pg_catalog.json_build_object(
                    'coverage', public.wp06_canonical_float_json(row_value.snapshot_coverage),
                    'quality_ok', row_value.snapshot_quality_ok,
                    'schema_compatible', row_value.schema_compatible
                ),
                'incumbent_decision', pg_catalog.json_build_object(
                    'decision_hash', row_value.decision_hash,
                    'decision_id', row_value.incumbent_decision_id::text,
                    'evaluated_at', public.wp06_canonical_utc_timestamp_json(
                        row_value.decision_evaluated_at
                    ),
                    'status', row_value.incumbent_status,
                    'strategy_release_hash', row_value.strategy_release_hash,
                    'strategy_release_id', row_value.strategy_release_id
                ),
                'observation_id', row_value.id::text,
                'observed_at', public.wp06_canonical_utc_timestamp_json(row_value.observed_at),
                'snapshot', pg_catalog.json_build_object(
                    'as_of', public.wp06_canonical_utc_timestamp_json(row_value.snapshot_as_of),
                    'schema_hash', row_value.snapshot_schema_hash,
                    'snapshot_hash', row_value.snapshot_hash,
                    'snapshot_id', row_value.snapshot_id
                ),
                'source_rights', pg_catalog.json_build_object(
                    'eligible', row_value.rights_eligible,
                    'fingerprint', row_value.rights_fingerprint,
                    'known_at', public.wp06_canonical_utc_timestamp_json(
                        row_value.rights_known_at
                    ),
                    'policy_id', row_value.rights_policy_id,
                    'policy_sequence', row_value.rights_policy_sequence,
                    'source_id', row_value.rights_source_id
                ),
                'workspace_id', row_value.workspace_id::text
            )
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_shadow_observation_content_hash(
            row_value public.learning_shadow_observation
        ) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT public.wp06_sha256_canonical_json(
                public.wp06_shadow_observation_canonical_document(row_value)
            )
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_drift_assessment_idempotency_key(
            row_value public.learning_drift_assessment
        ) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                'membership_digest', row_value.membership_digest,
                'policy_version', row_value.policy_version,
                'registry_id', row_value.cohort_registry_id::text,
                'revision', row_value.cohort_revision,
                'workspace_id', row_value.workspace_id::text
            ))
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_drift_assessment_content_hash(
            row_value public.learning_drift_assessment,
            member_ids json
        ) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                'abstention_rate', public.wp06_canonical_float_json(
                    row_value.abstention_rate
                ),
                'assessment_id', row_value.id::text,
                'cohort_registry_id', row_value.cohort_registry_id::text,
                'cohort_revision', row_value.cohort_revision,
                'created_at', public.wp06_canonical_utc_timestamp_json(row_value.created_at),
                'data_quality_status', row_value.data_quality_status,
                'eligible_count', row_value.eligible_count,
                'member_count', row_value.member_count,
                'member_ids', member_ids,
                'membership_digest', row_value.membership_digest,
                'performance_status', row_value.performance_status,
                'policy_version', row_value.policy_version,
                'quality_rate', public.wp06_canonical_float_json(row_value.quality_rate),
                'reason_codes', row_value.reason_codes::json,
                'snapshot_coverage_rate', public.wp06_canonical_float_json(
                    row_value.snapshot_coverage_rate
                ),
                'window_end', public.wp06_canonical_utc_timestamp_json(row_value.window_end),
                'window_start', public.wp06_canonical_utc_timestamp_json(row_value.window_start),
                'workspace_id', row_value.workspace_id::text
            ))
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_retrain_proposal_idempotency_key(
            row_value public.learning_retrain_proposal
        ) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                'assessment_hash', row_value.assessment_hash,
                'membership_digest', row_value.membership_digest,
                'policy_version', row_value.policy_version,
                'workspace_id', row_value.workspace_id::text
            ))
        $$
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_retrain_proposal_content_hash(
            row_value public.learning_retrain_proposal
        ) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT public.wp06_sha256_canonical_json(pg_catalog.json_build_object(
                'assessment_hash', row_value.assessment_hash,
                'assessment_id', row_value.assessment_id::text,
                'created_at', public.wp06_canonical_utc_timestamp_json(row_value.created_at),
                'idempotency_key', row_value.idempotency_key,
                'membership_digest', row_value.membership_digest,
                'policy_version', row_value.policy_version,
                'proposal_id', row_value.id::text,
                'reason_codes', row_value.reason_codes::json,
                'workspace_id', row_value.workspace_id::text
            ))
        $$
    """)
    for signature in (
        "public.wp06_shadow_observation_idempotency_key(public.learning_shadow_observation)",
        "public.wp06_shadow_observation_canonical_document(public.learning_shadow_observation)",
        "public.wp06_shadow_observation_content_hash(public.learning_shadow_observation)",
        "public.wp06_drift_assessment_idempotency_key(public.learning_drift_assessment)",
        "public.wp06_drift_assessment_content_hash(public.learning_drift_assessment, json)",
        "public.wp06_retrain_proposal_idempotency_key(public.learning_retrain_proposal)",
        "public.wp06_retrain_proposal_content_hash(public.learning_retrain_proposal)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")

    op.execute("""
        CREATE FUNCTION public.wp06_learning_registry_approval_event_hash(
            target_workspace_id uuid, target_registry_id uuid, target_event_id uuid
        ) RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            event_row record;
            event_document jsonb;
        BEGIN
            IF target_workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SHADOW_REGISTRY_APPROVAL_WORKSPACE_INVALID';
            END IF;
            SELECT event.id, event.workspace_id, event.registry_id, event.sequence,
                   event.action, event.from_state, event.to_state,
                   event.actor_membership_id, event.policy_version, event.reason_code,
                   event.reason, event.idempotency_key, event.mfa_verified_at,
                   event.expected_revision, event.expected_pointer_version,
                   event.evaluation_report_id, event.evaluation_report_hash, event.created_at
              INTO event_row
              FROM public.learning_model_registry_event AS event
             WHERE event.workspace_id = target_workspace_id
               AND event.registry_id = target_registry_id
               AND event.id = target_event_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            IF event_row.action <> 'approve'
                OR event_row.to_state <> 'evaluated'
                OR event_row.mfa_verified_at IS NULL THEN
                RETURN NULL;
            END IF;
            event_document := pg_catalog.jsonb_build_object(
                'action', event_row.action,
                'actor_membership_id', event_row.actor_membership_id::text,
                'created_at', pg_catalog.to_char(
                    event_row.created_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ),
                'event_id', event_row.id::text,
                'evaluation_report_hash', event_row.evaluation_report_hash,
                'evaluation_report_id', event_row.evaluation_report_id::text,
                'expected_pointer_version', event_row.expected_pointer_version,
                'expected_revision', event_row.expected_revision,
                'from_state', event_row.from_state,
                'idempotency_key', event_row.idempotency_key::text,
                'mfa_verified_at', pg_catalog.to_char(
                    event_row.mfa_verified_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                ),
                'policy_version', event_row.policy_version,
                'reason', event_row.reason,
                'reason_code', event_row.reason_code,
                'registry_id', event_row.registry_id::text,
                'sequence', event_row.sequence,
                'to_state', event_row.to_state,
                'workspace_id', event_row.workspace_id::text
            );
            RETURN pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(event_document::text, 'UTF8')),
                'hex'
            );
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_learning_registry_approval_event_hash"
        "(uuid, uuid, uuid) FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_canonical_source_cost(target_cost numeric)
        RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            plain_value text;
            digits text;
            decimal_position integer;
            first_nonzero integer;
            normalized_digits text;
            exponent_value integer;
            adjusted_exponent integer;
            point_position integer;
            mantissa text;
        BEGIN
            IF target_cost = 0 THEN
                RETURN '0';
            END IF;
            plain_value := pg_catalog.trim_scale(pg_catalog.abs(target_cost))::text;
            decimal_position := pg_catalog.strpos(plain_value, '.') - 1;
            IF decimal_position < 0 THEN
                decimal_position := pg_catalog.length(plain_value);
            END IF;
            digits := pg_catalog.replace(plain_value, '.', '');
            first_nonzero := pg_catalog.length(digits)
                - pg_catalog.length(pg_catalog.ltrim(digits, '0')) + 1;
            normalized_digits := pg_catalog.substr(digits, first_nonzero);
            normalized_digits := pg_catalog.regexp_replace(normalized_digits, '0+$', '');
            exponent_value := decimal_position - first_nonzero + 1
                - pg_catalog.length(normalized_digits);
            adjusted_exponent := pg_catalog.length(normalized_digits) + exponent_value - 1;
            IF exponent_value <= 0 AND adjusted_exponent >= -6 THEN
                point_position := pg_catalog.length(normalized_digits) + exponent_value;
                IF point_position <= 0 THEN
                    RETURN CASE WHEN target_cost < 0 THEN '-' ELSE '' END
                        || '0.' || pg_catalog.repeat('0', -point_position) || normalized_digits;
                ELSIF point_position = pg_catalog.length(normalized_digits) THEN
                    RETURN CASE WHEN target_cost < 0 THEN '-' ELSE '' END
                        || normalized_digits;
                END IF;
                RETURN CASE WHEN target_cost < 0 THEN '-' ELSE '' END
                    || pg_catalog.substr(normalized_digits, 1, point_position)
                    || '.' || pg_catalog.substr(normalized_digits, point_position + 1);
            END IF;
            mantissa := pg_catalog.substr(normalized_digits, 1, 1)
                || CASE WHEN pg_catalog.length(normalized_digits) > 1
                    THEN '.' || pg_catalog.substr(normalized_digits, 2)
                    ELSE '' END;
            RETURN CASE WHEN target_cost < 0 THEN '-' ELSE '' END || mantissa || 'E'
                || CASE WHEN adjusted_exponent >= 0
                    THEN '+' || adjusted_exponent::text
                    ELSE adjusted_exponent::text END;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_canonical_source_cost(numeric) FROM PUBLIC")

    op.execute("""
        CREATE FUNCTION public.wp06_shadow_current_paper_rights_hash(
            target_workspace_id uuid,
            target_source_id text,
            target_policy_id text,
            target_sequence integer,
            target_known_at timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            policy_row record;
            latest_policy record;
            receipt_row record;
            latest_known_at timestamptz;
            previous_known_at timestamptz;
            expected_sequence integer := 1;
            policy_seen boolean := false;
            instant timestamptz := pg_catalog.clock_timestamp();
            allowed_purposes_json text;
            reviewed_at_json text;
            valid_until_json text;
            policy_document text;
        BEGIN
            IF target_workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
                OR target_source_id <> 'BINANCE_USDM_REST' THEN
                RETURN NULL;
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    'research-source:' || target_workspace_id::text || ':' || target_source_id,
                    0
                )
            );
            FOR policy_row IN
                SELECT policy.workspace_id, policy.source_id, policy.sequence, policy.policy_id,
                       policy.state, policy.allowed_purposes, policy.evidence_ref,
                       policy.reviewed_at, policy.valid_until, policy.publisher,
                       policy.endpoint_url, policy.access_method, policy.auth_scope,
                       policy.attribution, policy.retention_ref, policy.quota_window_seconds,
                       policy.quota_max_requests, policy.quota_max_bytes, policy.quota_max_cost,
                       policy.quota_cost_unit, policy.approved_by, policy.created_at
                  FROM public.source_policy_revision AS policy
                 WHERE policy.workspace_id = target_workspace_id
                   AND policy.source_id = target_source_id
                 ORDER BY policy.sequence
            LOOP
                IF policy_row.sequence <> expected_sequence THEN
                    RETURN NULL;
                END IF;
                SELECT receipt.policy_id, receipt.known_at INTO receipt_row
                  FROM public.source_policy_observation AS receipt
                 WHERE receipt.workspace_id = target_workspace_id
                   AND receipt.source_id = target_source_id
                   AND receipt.sequence = policy_row.sequence;
                IF NOT FOUND THEN
                    RETURN NULL;
                END IF;
                IF receipt_row.policy_id <> policy_row.policy_id
                    OR receipt_row.known_at < policy_row.created_at
                    OR (policy_row.reviewed_at IS NOT NULL
                        AND receipt_row.known_at < policy_row.reviewed_at)
                    OR (previous_known_at IS NOT NULL
                        AND receipt_row.known_at < previous_known_at) THEN
                    RETURN NULL;
                END IF;
                latest_policy := policy_row;
                latest_known_at := receipt_row.known_at;
                previous_known_at := receipt_row.known_at;
                policy_seen := true;
                expected_sequence := expected_sequence + 1;
            END LOOP;
            IF NOT policy_seen THEN
                RETURN NULL;
            END IF;
            IF latest_policy.sequence <> target_sequence
                OR latest_policy.policy_id <> target_policy_id
                OR latest_known_at IS DISTINCT FROM target_known_at
                OR latest_known_at >= instant
                OR latest_policy.state <> 'verified'
                OR latest_policy.reviewed_at IS NULL
                OR latest_policy.reviewed_at > instant
                OR (latest_policy.valid_until IS NOT NULL AND instant >= latest_policy.valid_until)
                OR NOT (latest_policy.allowed_purposes @> '["paper_demo"]'::jsonb) THEN
                RETURN NULL;
            END IF;

            SELECT '[' || COALESCE(
                       pg_catalog.string_agg(pg_catalog.to_jsonb(value.purpose)::text, ','
                                             ORDER BY value.purpose),
                       ''
                   ) || ']'
              INTO allowed_purposes_json
              FROM pg_catalog.jsonb_array_elements_text(latest_policy.allowed_purposes)
                   AS value(purpose);
            reviewed_at_json := CASE
                WHEN latest_policy.reviewed_at IS NULL THEN 'null'
                WHEN date_trunc('second', latest_policy.reviewed_at) = latest_policy.reviewed_at
                    THEN pg_catalog.to_jsonb(
                        pg_catalog.to_char(latest_policy.reviewed_at AT TIME ZONE 'UTC',
                                           'YYYY-MM-DD"T"HH24:MI:SS') || '+00:00'
                    )::text
                ELSE pg_catalog.to_jsonb(
                    pg_catalog.to_char(latest_policy.reviewed_at AT TIME ZONE 'UTC',
                                       'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
                )::text
            END;
            valid_until_json := CASE
                WHEN latest_policy.valid_until IS NULL THEN 'null'
                WHEN date_trunc('second', latest_policy.valid_until) = latest_policy.valid_until
                    THEN pg_catalog.to_jsonb(
                        pg_catalog.to_char(latest_policy.valid_until AT TIME ZONE 'UTC',
                                           'YYYY-MM-DD"T"HH24:MI:SS') || '+00:00'
                    )::text
                ELSE pg_catalog.to_jsonb(
                    pg_catalog.to_char(latest_policy.valid_until AT TIME ZONE 'UTC',
                                       'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
                )::text
            END;
            policy_document := '{"access_method":'
                || pg_catalog.to_jsonb(latest_policy.access_method)::text
                || ',"allowed_purposes":' || allowed_purposes_json
                || ',"approved_by":' || pg_catalog.to_jsonb(latest_policy.approved_by)::text
                || ',"attribution":' || pg_catalog.to_jsonb(latest_policy.attribution)::text
                || ',"auth_scope":' || pg_catalog.to_jsonb(latest_policy.auth_scope)::text
                || ',"budget":{"cost_unit":'
                || pg_catalog.to_jsonb(latest_policy.quota_cost_unit)::text
                || ',"max_bytes":' || pg_catalog.to_jsonb(latest_policy.quota_max_bytes)::text
                || ',"max_cost":'
                || pg_catalog.to_jsonb(
                    public.wp06_canonical_source_cost(latest_policy.quota_max_cost)
                )::text
                || ',"max_requests":' || pg_catalog.to_jsonb(latest_policy.quota_max_requests)::text
                || ',"window":[' || (latest_policy.quota_window_seconds / 86400)::text
                || ',' || (latest_policy.quota_window_seconds % 86400)::text || ',0]}'
                || ',"endpoint_url":' || pg_catalog.to_jsonb(latest_policy.endpoint_url)::text
                || ',"evidence_ref":' || pg_catalog.to_jsonb(latest_policy.evidence_ref)::text
                || ',"policy_id":' || pg_catalog.to_jsonb(latest_policy.policy_id)::text
                || ',"publisher":' || pg_catalog.to_jsonb(latest_policy.publisher)::text
                || ',"retention_ref":' || pg_catalog.to_jsonb(latest_policy.retention_ref)::text
                || ',"reviewed_at":' || reviewed_at_json
                || ',"sequence":' || pg_catalog.to_jsonb(latest_policy.sequence)::text
                || ',"source_id":' || pg_catalog.to_jsonb(latest_policy.source_id)::text
                || ',"state":' || pg_catalog.to_jsonb(latest_policy.state)::text
                || ',"valid_until":' || valid_until_json || '}';
            RETURN pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(policy_document, 'UTF8')),
                'hex'
            );
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_shadow_current_paper_rights_hash("
        "uuid, text, text, integer, timestamptz) FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_shadow_current_paper_rights_receipt(
            target_workspace_id uuid
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            current_policy record;
            receipt_known_at timestamptz;
            current_fingerprint text;
        BEGIN
            IF target_workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RETURN NULL;
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    'research-source:' || target_workspace_id::text || pg_catalog.chr(58)
                        || 'BINANCE_USDM_REST',
                    0
                )
            );
            SELECT policy.policy_id, policy.sequence
              INTO current_policy
              FROM public.source_policy_revision AS policy
             WHERE policy.workspace_id = target_workspace_id
               AND policy.source_id = 'BINANCE_USDM_REST'
             ORDER BY policy.sequence DESC
             LIMIT 1;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            SELECT receipt.known_at
              INTO receipt_known_at
              FROM public.source_policy_observation AS receipt
             WHERE receipt.workspace_id = target_workspace_id
               AND receipt.source_id = 'BINANCE_USDM_REST'
               AND receipt.sequence = current_policy.sequence
               AND receipt.policy_id = current_policy.policy_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            current_fingerprint := public.wp06_shadow_current_paper_rights_hash(
                target_workspace_id,
                'BINANCE_USDM_REST',
                current_policy.policy_id,
                current_policy.sequence,
                receipt_known_at
            );
            IF current_fingerprint IS NULL THEN
                RETURN NULL;
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'fingerprint', current_fingerprint,
                'known_at', receipt_known_at,
                'policy_id', current_policy.policy_id,
                'policy_sequence', current_policy.sequence,
                'source_id', 'BINANCE_USDM_REST'
            );
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_shadow_current_paper_rights_receipt(uuid) FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_shadow_market_source_rights_matches(
            target_market_source text,
            target_rights_source text
        ) RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
        SET search_path = pg_catalog AS $$
            SELECT CASE target_market_source
                WHEN 'BINANCE_USDM' THEN target_rights_source = 'BINANCE_USDM_REST'
                WHEN 'BINANCE_USDM_REST_BACKFILL' THEN target_rights_source = 'BINANCE_USDM_REST'
                ELSE false
            END
        $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_shadow_market_source_rights_matches(text, text) "
        "FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_shadow_decision_source_rights_supported(
            target_workspace_id uuid,
            target_decision_id uuid
        ) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            evidence_refs jsonb;
            snapshot_event_ids jsonb;
            matched_source_count integer;
            supported_source_count integer;
        BEGIN
            IF target_workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RETURN false;
            END IF;
            SELECT evidence.source_refs, snapshot.input_event_ids
              INTO evidence_refs, snapshot_event_ids
              FROM public.bot_decision AS decision
              JOIN public.shared_signal_evidence AS evidence
                ON evidence.workspace_id = decision.workspace_id
               AND evidence.id = decision.evidence_id
               AND evidence.evidence_hash = decision.evidence_hash
              JOIN public.feature_snapshot AS snapshot
                ON snapshot.workspace_id = evidence.workspace_id
               AND snapshot.snapshot_id = evidence.feature_snapshot_id
             WHERE decision.workspace_id = target_workspace_id
               AND decision.id = target_decision_id;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            IF pg_catalog.jsonb_typeof(evidence_refs) IS DISTINCT FROM 'array' THEN
                RETURN false;
            END IF;
            IF pg_catalog.jsonb_array_length(evidence_refs) = 0 THEN
                RETURN false;
            END IF;
            IF pg_catalog.jsonb_typeof(snapshot_event_ids) IS DISTINCT FROM 'array' THEN
                RETURN false;
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM pg_catalog.jsonb_array_elements_text(evidence_refs) AS ref
                 WHERE ref.value !~
                    '^market-bar-event:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                    '[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            ) THEN
                RETURN false;
            END IF;
            SELECT pg_catalog.count(DISTINCT ref.value)::integer,
                   pg_catalog.count(DISTINCT ref.value) FILTER (
                       WHERE ref.value = 'market-bar-event:' || event_id.value
                         AND public.wp06_shadow_market_source_rights_matches(
                             bar.source, 'BINANCE_USDM_REST'
                         )
                   )::integer
              INTO matched_source_count, supported_source_count
              FROM pg_catalog.jsonb_array_elements_text(evidence_refs) AS ref
              LEFT JOIN pg_catalog.jsonb_array_elements_text(snapshot_event_ids) AS event_id
                ON ref.value = 'market-bar-event:' || event_id.value
              LEFT JOIN public.market_bar AS bar
                ON bar.workspace_id = target_workspace_id
               AND bar.id = substring(ref.value FROM 18)::uuid;
            RETURN matched_source_count = pg_catalog.jsonb_array_length(evidence_refs)
               AND supported_source_count = matched_source_count;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_shadow_decision_source_rights_supported(uuid, uuid) "
        "FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_lock_learning_shadow_cohort(
            target_workspace_id uuid,
            target_registry_id uuid,
            target_revision integer
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF target_workspace_id IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
                OR target_registry_id IS NULL
                OR target_revision IS NULL OR target_revision < 1 THEN
                RAISE EXCEPTION 'WP06_SHADOW_COHORT_SCOPE_INVALID';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    'learning-shadow-cohort:' || target_workspace_id::text || ':'
                    || target_registry_id::text || ':' || target_revision::text,
                    0
                )
            );
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_lock_learning_shadow_cohort(uuid, uuid, integer) "
        "FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_lock_learning_model_active_pointer()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(
                    'learning-model-registry:' || NEW.workspace_id::text
                    || pg_catalog.chr(58) || 'active-pointer',
                    0
                )
            );
            RETURN NEW;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_lock_learning_model_active_pointer() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER learning_model_active_pointer_shadow_lock "
        "BEFORE INSERT OR UPDATE ON public.learning_model_active_pointer "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_lock_learning_model_active_pointer()"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_require_learning_shadow_lineage()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            decision_row record;
            evidence_row record;
            snapshot_row record;
            challenger_row record;
            challenger_artifact_row record;
            active_row record;
            pointer_row record;
            expected_event_hash text;
            expected_rights_hash text;
            expected_idempotency_key text;
            expected_content_hash text;
            has_non_market_source boolean;
            matched_source_count integer;
            supported_bar_count integer;
        BEGIN
            IF NEW.observed_at IS DISTINCT FROM transaction_timestamp()
                OR NEW.created_at IS DISTINCT FROM transaction_timestamp() THEN
                RAISE EXCEPTION 'WP06_SHADOW_DATABASE_TIME_REQUIRED';
            END IF;
            expected_idempotency_key := public.wp06_shadow_observation_idempotency_key(NEW);
            IF NEW.idempotency_key IS DISTINCT FROM expected_idempotency_key THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_IDEMPOTENCY_INVALID';
            END IF;
            expected_content_hash := public.wp06_shadow_observation_content_hash(NEW);
            IF NEW.content_hash IS DISTINCT FROM expected_content_hash THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_CONTENT_HASH_INVALID';
            END IF;
            PERFORM public.wp06_lock_learning_shadow_cohort(
                NEW.workspace_id,
                NEW.challenger_registry_id,
                NEW.challenger_revision
            );
            SELECT decision.id, decision.workspace_id, decision.evidence_id,
                   decision.evidence_hash, decision.strategy_release_id,
                   decision.strategy_release_hash, decision.evaluated_at,
                   decision.status, decision.decision_hash
              INTO decision_row
              FROM public.bot_decision AS decision
             WHERE decision.workspace_id = NEW.workspace_id
               AND decision.id = NEW.incumbent_decision_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;

            SELECT evidence.workspace_id, evidence.id, evidence.feature_snapshot_id,
                   evidence.evidence_hash, evidence.source_refs
              INTO evidence_row
              FROM public.shared_signal_evidence AS evidence
             WHERE evidence.workspace_id = decision_row.workspace_id
               AND evidence.id = decision_row.evidence_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;
            SELECT snapshot.workspace_id, snapshot.snapshot_id, snapshot.content_hash,
                   snapshot.schema_hash, snapshot.as_of, snapshot.input_event_ids,
                   snapshot.data_coverage, snapshot.data_freshness, snapshot.quality_flags,
                   snapshot.is_usable
              INTO snapshot_row
              FROM public.feature_snapshot AS snapshot
             WHERE snapshot.workspace_id = NEW.workspace_id
               AND snapshot.snapshot_id = NEW.snapshot_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;
            IF evidence_row.feature_snapshot_id <> NEW.snapshot_id
                OR evidence_row.evidence_hash <> decision_row.evidence_hash
                OR evidence_row.workspace_id <> NEW.workspace_id
                OR snapshot_row.content_hash <> NEW.snapshot_hash
                OR snapshot_row.snapshot_id <> NEW.snapshot_id
                OR snapshot_row.schema_hash <> NEW.snapshot_schema_hash
                OR snapshot_row.as_of <> NEW.snapshot_as_of
                OR snapshot_row.data_coverage <> NEW.snapshot_coverage
                OR NEW.snapshot_quality_ok <> (
                    snapshot_row.is_usable
                    AND snapshot_row.data_freshness = 'FRESH'
                    AND snapshot_row.quality_flags = '[]'::jsonb
                )
                OR decision_row.decision_hash <> NEW.decision_hash
                OR decision_row.strategy_release_id <> NEW.strategy_release_id
                OR decision_row.strategy_release_hash IS DISTINCT FROM NEW.strategy_release_hash
                OR decision_row.status <> NEW.incumbent_status
                OR decision_row.evaluated_at <> NEW.decision_evaluated_at
                OR decision_row.evaluated_at < snapshot_row.as_of THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;

            IF NOT NEW.rights_eligible
                AND NEW.challenger_reason <> 'SOURCE_RIGHTS_INELIGIBLE' THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_SOURCE_RIGHTS_UNVERIFIED';
            END IF;
            IF NEW.rights_eligible THEN
                IF pg_catalog.jsonb_typeof(evidence_row.source_refs)
                    IS DISTINCT FROM 'array' THEN
                    RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_SOURCE_RIGHTS_UNVERIFIED';
                END IF;
                IF pg_catalog.jsonb_array_length(evidence_row.source_refs) = 0 THEN
                    RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_SOURCE_RIGHTS_UNVERIFIED';
                END IF;
                SELECT EXISTS (
                    SELECT 1
                      FROM pg_catalog.jsonb_array_elements_text(evidence_row.source_refs) AS ref
                     WHERE ref.value !~
                        '^market-bar-event:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                        '[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
                ) INTO has_non_market_source;
                SELECT pg_catalog.count(DISTINCT ref.value)::integer,
                       pg_catalog.count(DISTINCT ref.value) FILTER (
                           WHERE ref.value = 'market-bar-event:' || event_id.value
                             AND public.wp06_shadow_market_source_rights_matches(
                                 bar.source, NEW.rights_source_id
                             )
                       )::integer
                  INTO matched_source_count, supported_bar_count
                  FROM pg_catalog.jsonb_array_elements_text(evidence_row.source_refs) AS ref
                  LEFT JOIN pg_catalog.jsonb_array_elements_text(snapshot_row.input_event_ids)
                       AS event_id ON ref.value = 'market-bar-event:' || event_id.value
                  LEFT JOIN public.market_bar AS bar
                    ON bar.workspace_id = NEW.workspace_id
                   AND bar.id = substring(ref.value FROM 18)::uuid;
                IF has_non_market_source
                    OR matched_source_count <>
                        pg_catalog.jsonb_array_length(evidence_row.source_refs)
                    OR supported_bar_count <> matched_source_count
                    OR NEW.rights_source_id <> 'BINANCE_USDM_REST'
                    OR NEW.rights_policy_id IS NULL
                    OR NEW.rights_policy_sequence IS NULL
                    OR NEW.rights_known_at IS NULL
                    OR NEW.rights_fingerprint IS NULL
                    OR NEW.rights_fingerprint !~ '^[0-9a-f]{64}$' THEN
                    RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_SOURCE_RIGHTS_UNVERIFIED';
                END IF;
                expected_rights_hash := public.wp06_shadow_current_paper_rights_hash(
                    NEW.workspace_id,
                    NEW.rights_source_id,
                    NEW.rights_policy_id,
                    NEW.rights_policy_sequence,
                    NEW.rights_known_at
                );
                IF expected_rights_hash IS NULL
                    OR expected_rights_hash <> NEW.rights_fingerprint THEN
                    RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_SOURCE_RIGHTS_UNVERIFIED';
                END IF;
            END IF;

            SELECT registry.workspace_id, registry.id, registry.artifact_id,
                   registry.artifact_hash, registry.manifest_hash, registry.fold_plan_hash,
                   registry.approval_hash, registry.state, registry.revision
              INTO challenger_row
              FROM public.learning_model_registry AS registry
             WHERE registry.workspace_id = NEW.workspace_id
               AND registry.id = NEW.challenger_registry_id
             FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;
            SELECT artifact.workspace_id, artifact.id, artifact.artifact_hash,
                   artifact.manifest_hash, artifact.fold_plan_hash, artifact.approval_hash,
                   artifact.feature_schema_hash
              INTO challenger_artifact_row
              FROM public.learning_training_artifact AS artifact
             WHERE artifact.workspace_id = NEW.workspace_id
               AND artifact.id = NEW.challenger_artifact_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;
            SELECT registry.revision, registry.artifact_hash
              INTO active_row
              FROM public.learning_model_registry AS registry
             WHERE registry.workspace_id = NEW.workspace_id
               AND registry.id = NEW.active_registry_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;
            SELECT pointer.registry_id, pointer.version
              INTO pointer_row
              FROM public.learning_model_active_pointer AS pointer
             WHERE pointer.workspace_id = NEW.workspace_id
             FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;
            IF pointer_row.registry_id IS DISTINCT FROM NEW.active_registry_id
                OR pointer_row.version <> NEW.active_pointer_version
                OR challenger_row.state <> 'evaluated'
                OR challenger_row.revision <> NEW.challenger_revision
                OR challenger_row.artifact_id <> NEW.challenger_artifact_id
                OR challenger_row.artifact_hash <> NEW.challenger_artifact_hash
                OR challenger_row.manifest_hash <> NEW.challenger_manifest_hash
                OR challenger_row.fold_plan_hash <> NEW.challenger_fold_plan_hash
                OR challenger_row.approval_hash <> NEW.training_approval_hash
                OR challenger_artifact_row.artifact_hash <> NEW.challenger_artifact_hash
                OR challenger_artifact_row.manifest_hash <> NEW.challenger_manifest_hash
                OR challenger_artifact_row.fold_plan_hash <> NEW.challenger_fold_plan_hash
                OR challenger_artifact_row.approval_hash <> NEW.training_approval_hash
                OR active_row.revision <> NEW.active_registry_revision
                OR active_row.artifact_hash <> NEW.active_artifact_hash
                OR NEW.active_registry_id = NEW.challenger_registry_id THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_LINEAGE_INVALID';
            END IF;

            expected_event_hash := public.wp06_learning_registry_approval_event_hash(
                NEW.workspace_id, NEW.challenger_registry_id, NEW.registry_approval_event_id
            );
            IF expected_event_hash IS NULL
                OR expected_event_hash <> NEW.registry_approval_event_hash
                OR NOT EXISTS (
                    SELECT 1 FROM public.learning_model_registry_event AS event
                     WHERE event.workspace_id = NEW.workspace_id
                       AND event.registry_id = NEW.challenger_registry_id
                       AND event.id = NEW.registry_approval_event_id
                       AND event.action = 'approve'
                       AND event.to_state = 'evaluated'
                       AND event.mfa_verified_at IS NOT NULL
                       AND event.sequence = NEW.registry_approval_event_sequence
                       AND event.sequence <= challenger_row.revision
                ) THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_REGISTRY_APPROVAL_INVALID';
            END IF;
            IF NEW.schema_compatible <>
                (snapshot_row.schema_hash = challenger_artifact_row.feature_schema_hash) THEN
                RAISE EXCEPTION 'WP06_SHADOW_OBSERVATION_SCHEMA_COMPATIBILITY_INVALID';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_require_learning_shadow_lineage() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER learning_shadow_observation_lineage_guard "
        "BEFORE INSERT ON public.learning_shadow_observation FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_require_learning_shadow_lineage()"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_lock_learning_drift_assessment_cohort()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.created_at IS DISTINCT FROM transaction_timestamp() THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_DATABASE_TIME_REQUIRED';
            END IF;
            IF NEW.policy_version IS DISTINCT FROM 'coverage-v1'
                OR NEW.minimum_observations <> 3
                OR NEW.minimum_snapshot_coverage <> 0.8
                OR NEW.minimum_quality_rate <> 0.9 THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_POLICY_INVALID';
            END IF;
            IF NEW.member_count = 0 THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_EMPTY_COHORT';
            END IF;
            IF NEW.cohort_registry_id IS NOT NULL THEN
                PERFORM public.wp06_lock_learning_shadow_cohort(
                    NEW.workspace_id,
                    NEW.cohort_registry_id,
                    NEW.cohort_revision
                );
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_lock_learning_drift_assessment_cohort() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER learning_drift_assessment_cohort_lock "
        "BEFORE INSERT ON public.learning_drift_assessment FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_lock_learning_drift_assessment_cohort()"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_validate_learning_drift_assessment_membership()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            target_workspace uuid;
            target_assessment uuid;
            assessment_row record;
            actual_member_ids json;
            actual_count integer;
            actual_eligible_count integer;
            actual_window_start timestamptz;
            actual_window_end timestamptz;
            actual_digest text;
            actual_coverage float8;
            actual_quality float8;
            actual_abstention float8;
            required_complete boolean;
            expected_status text;
            expected_reasons jsonb;
            reason_list text[] := ARRAY['performance_not_evaluable']::text[];
            has_low_coverage boolean := false;
            has_low_quality boolean := false;
            invalid_ordinal boolean;
            actual_cohort_count integer;
            actual_cohort_digest text;
            actual_cohort_ids text;
            expected_idempotency_key text;
            expected_content_hash text;
        BEGIN
            IF TG_TABLE_NAME = 'learning_drift_assessment' THEN
                target_workspace := NEW.workspace_id;
                target_assessment := NEW.id;
            ELSE
                target_workspace := NEW.workspace_id;
                target_assessment := NEW.assessment_id;
            END IF;
            SELECT assessment.* INTO assessment_row
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            SELECT pg_catalog.count(*)::integer,
                   pg_catalog.count(*) FILTER (WHERE member.eligible)::integer,
                   pg_catalog.min(member.observed_at),
                   pg_catalog.max(member.observed_at),
                   pg_catalog.round(
                       pg_catalog.sum(member.snapshot_coverage::text::numeric)
                           FILTER (WHERE member.eligible)
                       / NULLIF(pg_catalog.count(*) FILTER (WHERE member.eligible), 0),
                       12
                   )::float8,
                   pg_catalog.round(
                       (pg_catalog.count(*) FILTER (
                           WHERE member.eligible AND member.snapshot_quality_ok
                       ))::numeric
                       / NULLIF(pg_catalog.count(*) FILTER (WHERE member.eligible), 0),
                       12
                   )::float8,
                   pg_catalog.round(
                       (pg_catalog.count(*) FILTER (
                           WHERE member.challenger_decision = 'abstain'
                       ))::numeric / NULLIF(pg_catalog.count(*), 0),
                       12
                   )::float8,
                   COALESCE(
                       pg_catalog.json_agg(
                           member.observation_id::text
                           ORDER BY member.observation_id::text
                       ),
                       '[]'::json
                   ),
                   '[' || COALESCE(
                       pg_catalog.string_agg(
                           '"' || member.observation_id::text || '"', ','
                           ORDER BY member.observation_id::text
                       ),
                       ''
                   ) || ']'
              INTO actual_count, actual_eligible_count, actual_window_start, actual_window_end,
                   actual_coverage, actual_quality, actual_abstention, actual_member_ids,
                   actual_digest
              FROM public.learning_drift_assessment_member AS member
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = target_assessment;

            SELECT EXISTS (
                SELECT 1 FROM (
                    SELECT member.ordinal,
                           pg_catalog.row_number() OVER (ORDER BY member.observation_id::text) - 1
                               AS expected_ordinal
                      FROM public.learning_drift_assessment_member AS member
                     WHERE member.workspace_id = target_workspace
                       AND member.assessment_id = target_assessment
                ) AS ordered
                 WHERE ordered.ordinal <> ordered.expected_ordinal
            ) INTO invalid_ordinal;

            actual_digest := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(actual_digest, 'UTF8')),
                'hex'
            );
            SELECT public.wp06_drift_assessment_idempotency_key(assessment)
              INTO expected_idempotency_key
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF assessment_row.idempotency_key IS DISTINCT FROM expected_idempotency_key THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_IDEMPOTENCY_INVALID';
            END IF;
            SELECT public.wp06_drift_assessment_content_hash(assessment, actual_member_ids)
              INTO expected_content_hash
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF assessment_row.content_hash IS DISTINCT FROM expected_content_hash THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_CONTENT_HASH_INVALID';
            END IF;
            IF assessment_row.cohort_registry_id IS NULL THEN
                actual_cohort_count := 0;
                actual_cohort_digest := pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to('[]', 'UTF8')),
                    'hex'
                );
            ELSE
                SELECT pg_catalog.count(*)::integer,
                       '[' || COALESCE(
                           pg_catalog.string_agg(
                               '"' || observation.id::text || '"', ','
                               ORDER BY observation.id::text
                           ),
                           ''
                       ) || ']'
                  INTO actual_cohort_count, actual_cohort_ids
                  FROM public.learning_shadow_observation AS observation
                 WHERE observation.workspace_id = target_workspace
                   AND observation.challenger_registry_id = assessment_row.cohort_registry_id
                   AND observation.challenger_revision = assessment_row.cohort_revision;
                actual_cohort_digest := pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to(actual_cohort_ids, 'UTF8')),
                    'hex'
                );
            END IF;
            required_complete := actual_count >= assessment_row.minimum_observations
                AND actual_count = actual_eligible_count;
            IF actual_count < assessment_row.minimum_observations THEN
                reason_list := pg_catalog.array_append(reason_list, 'low_sample');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.baseline_available
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'baseline_missing');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.schema_compatible
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'schema_incompatible');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.rights_eligible
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'rights_unverified');
            END IF;

            IF required_complete THEN
                has_low_coverage := actual_coverage < assessment_row.minimum_snapshot_coverage;
                has_low_quality := actual_quality < assessment_row.minimum_quality_rate;
                IF has_low_coverage THEN
                    reason_list := pg_catalog.array_append(reason_list, 'coverage_below_threshold');
                END IF;
                IF has_low_quality THEN
                    reason_list := pg_catalog.array_append(reason_list, 'quality_below_threshold');
                END IF;
                expected_status := CASE
                    WHEN has_low_coverage OR has_low_quality THEN 'detected'
                    ELSE 'no_drift'
                END;
            ELSE
                actual_coverage := NULL;
                actual_quality := NULL;
                expected_status := 'inconclusive';
            END IF;
            SELECT pg_catalog.jsonb_agg(reason ORDER BY reason)
              INTO expected_reasons
              FROM pg_catalog.unnest(reason_list) AS reason;

            IF actual_count <> assessment_row.member_count
                OR actual_count <> actual_cohort_count
                OR actual_eligible_count <> assessment_row.eligible_count
                OR assessment_row.window_start IS DISTINCT FROM actual_window_start
                OR assessment_row.window_end IS DISTINCT FROM actual_window_end
                OR assessment_row.created_at < actual_window_end
                OR assessment_row.membership_digest <> actual_digest
                OR actual_digest <> actual_cohort_digest
                OR invalid_ordinal
                OR assessment_row.performance_status <> 'inconclusive'
                OR assessment_row.data_quality_status <> expected_status
                OR assessment_row.reason_codes <> expected_reasons
                OR assessment_row.snapshot_coverage_rate IS DISTINCT FROM actual_coverage
                OR assessment_row.quality_rate IS DISTINCT FROM actual_quality
                OR assessment_row.abstention_rate IS DISTINCT FROM actual_abstention THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBERSHIP_INVALID';
            END IF;
            RETURN NULL;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_validate_learning_drift_assessment_membership() "
        "FROM PUBLIC"
    )
    op.execute("""
        CREATE CONSTRAINT TRIGGER learning_drift_assessment_membership_complete
        AFTER INSERT ON public.learning_drift_assessment
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.wp06_validate_learning_drift_assessment_membership()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER learning_drift_assessment_member_complete
        AFTER INSERT ON public.learning_drift_assessment_member
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.wp06_validate_learning_drift_assessment_membership()
    """)

    op.execute("""
        CREATE FUNCTION public.wp06_require_learning_drift_member_lineage()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            observation_row record;
            snapshot_row record;
            assessment_row record;
            baseline_found boolean;
        BEGIN
            SELECT observation.id, observation.workspace_id, observation.incumbent_decision_id,
                   observation.challenger_registry_id, observation.challenger_revision,
                   observation.decision_hash, observation.snapshot_id, observation.snapshot_hash,
                   observation.observed_at, observation.snapshot_coverage,
                   observation.snapshot_quality_ok, observation.schema_compatible,
                   observation.rights_eligible, observation.rights_fingerprint,
                   observation.challenger_decision
              INTO observation_row
              FROM public.learning_shadow_observation AS observation
             WHERE observation.workspace_id = NEW.workspace_id
               AND observation.id = NEW.observation_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBER_LINEAGE_INVALID';
            END IF;
            SELECT snapshot.workspace_id, snapshot.snapshot_id, snapshot.data_coverage,
                   snapshot.data_freshness, snapshot.quality_flags, snapshot.is_usable
              INTO snapshot_row
              FROM public.feature_snapshot AS snapshot
             WHERE snapshot.workspace_id = NEW.workspace_id
               AND snapshot.snapshot_id = observation_row.snapshot_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBER_LINEAGE_INVALID';
            END IF;
            SELECT assessment.cohort_registry_id, assessment.cohort_revision
              INTO assessment_row
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = NEW.workspace_id
               AND assessment.id = NEW.assessment_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBER_LINEAGE_INVALID';
            END IF;
            SELECT EXISTS (
                SELECT 1 FROM public.bot_decision AS decision
                 WHERE decision.workspace_id = observation_row.workspace_id
                   AND decision.id = observation_row.incumbent_decision_id
                   AND decision.decision_hash = observation_row.decision_hash
            ) INTO baseline_found;
            IF observation_row.observed_at <> NEW.observed_at
                OR observation_row.challenger_registry_id
                    IS DISTINCT FROM assessment_row.cohort_registry_id
                OR observation_row.challenger_revision
                    IS DISTINCT FROM assessment_row.cohort_revision
                OR observation_row.snapshot_hash <> NEW.snapshot_hash
                OR snapshot_row.data_coverage <> NEW.snapshot_coverage
                OR observation_row.snapshot_coverage <> NEW.snapshot_coverage
                OR observation_row.snapshot_quality_ok <> (
                    snapshot_row.is_usable
                    AND snapshot_row.data_freshness = 'FRESH'
                    AND snapshot_row.quality_flags = '[]'::jsonb
                )
                OR observation_row.snapshot_quality_ok <> NEW.snapshot_quality_ok
                OR observation_row.schema_compatible <> NEW.schema_compatible
                OR observation_row.rights_eligible <> NEW.rights_eligible
                OR observation_row.rights_fingerprint IS DISTINCT FROM NEW.rights_fingerprint
                OR baseline_found <> NEW.baseline_available
                OR observation_row.challenger_decision <> 'abstain'
                OR NEW.challenger_decision <> 'abstain'
                OR NEW.eligible <> (
                    NEW.baseline_available AND NEW.schema_compatible AND NEW.rights_eligible
                ) THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBER_LINEAGE_INVALID';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_require_learning_drift_member_lineage() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER learning_drift_member_lineage_guard "
        "BEFORE INSERT ON public.learning_drift_assessment_member FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_require_learning_drift_member_lineage()"
    )

    op.execute("""
        CREATE FUNCTION public.wp06_require_learning_retrain_proposal_lineage()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            assessment_row record;
            expected_idempotency_key text;
            expected_content_hash text;
            canonical_reason_codes jsonb;
            reason_count integer;
            unique_reason_count integer;
        BEGIN
            IF NEW.created_at IS DISTINCT FROM transaction_timestamp() THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_DATABASE_TIME_REQUIRED';
            END IF;
            SELECT COALESCE(pg_catalog.jsonb_agg(reason.value ORDER BY reason.value), '[]'::jsonb),
                   pg_catalog.count(*)::integer,
                   pg_catalog.count(DISTINCT reason.value)::integer
              INTO canonical_reason_codes, reason_count, unique_reason_count
              FROM pg_catalog.jsonb_array_elements_text(NEW.reason_codes) AS reason(value);
            IF NEW.reason_codes IS DISTINCT FROM canonical_reason_codes
                OR reason_count <> unique_reason_count THEN
                RAISE EXCEPTION 'WP06_RETRAIN_PROPOSAL_REASONS_INVALID';
            END IF;
            expected_idempotency_key := public.wp06_retrain_proposal_idempotency_key(NEW);
            IF NEW.idempotency_key IS DISTINCT FROM expected_idempotency_key THEN
                RAISE EXCEPTION 'WP06_RETRAIN_PROPOSAL_IDEMPOTENCY_INVALID';
            END IF;
            expected_content_hash := public.wp06_retrain_proposal_content_hash(NEW);
            IF NEW.content_hash IS DISTINCT FROM expected_content_hash THEN
                RAISE EXCEPTION 'WP06_RETRAIN_PROPOSAL_CONTENT_HASH_INVALID';
            END IF;
            SELECT assessment.* INTO assessment_row
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = NEW.workspace_id
               AND assessment.id = NEW.assessment_id
               AND assessment.content_hash = NEW.assessment_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SHADOW_RETRAIN_PROPOSAL_ASSESSMENT_INVALID';
            END IF;
            IF assessment_row.performance_status <> 'inconclusive'
                OR assessment_row.data_quality_status <> 'detected'
                OR assessment_row.policy_version <> NEW.policy_version
                OR assessment_row.membership_digest <> NEW.membership_digest
                OR NOT (assessment_row.reason_codes @> NEW.reason_codes) THEN
                RAISE EXCEPTION 'WP06_RETRAIN_PROPOSAL_ASSESSMENT_INVALID';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_require_learning_retrain_proposal_lineage() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER learning_retrain_proposal_lineage_guard "
        "BEFORE INSERT ON public.learning_retrain_proposal FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_require_learning_retrain_proposal_lineage()"
    )

    op.execute("""
        CREATE FUNCTION public.reject_wp06_shadow_drift_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'WP06_SHADOW_DRIFT_IMMUTABLE';
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.reject_wp06_shadow_drift_mutation() FROM PUBLIC")
    for table in TABLES:
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON public."{table}" '
            "FOR EACH ROW EXECUTE FUNCTION public.reject_wp06_shadow_drift_mutation()"
        )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public."learning_shadow_observation")
            OR EXISTS (SELECT 1 FROM public."learning_drift_assessment")
            OR EXISTS (SELECT 1 FROM public."learning_drift_assessment_member")
            OR EXISTS (SELECT 1 FROM public."learning_retrain_proposal") THEN
            RAISE EXCEPTION 'WP06_SHADOW_DRIFT_DOWNGRADE_REQUIRES_EMPTY_TABLES';
        END IF;
    END $$""")
    for table in TABLES:
        op.execute(f'DROP TRIGGER {table}_immutable ON public."{table}"')
    op.execute(
        "DROP TRIGGER learning_retrain_proposal_lineage_guard ON public.learning_retrain_proposal"
    )
    op.execute(
        "DROP TRIGGER learning_drift_assessment_cohort_lock ON public.learning_drift_assessment"
    )
    op.execute(
        "DROP TRIGGER learning_model_active_pointer_shadow_lock "
        "ON public.learning_model_active_pointer"
    )
    op.execute(
        "DROP TRIGGER learning_drift_member_lineage_guard "
        "ON public.learning_drift_assessment_member"
    )
    op.execute(
        "DROP TRIGGER learning_drift_assessment_member_complete "
        "ON public.learning_drift_assessment_member"
    )
    op.execute(
        "DROP TRIGGER learning_drift_assessment_membership_complete "
        "ON public.learning_drift_assessment"
    )
    op.execute(
        "DROP TRIGGER learning_shadow_observation_lineage_guard "
        "ON public.learning_shadow_observation"
    )
    for function in (
        "public.reject_wp06_shadow_drift_mutation()",
        "public.wp06_require_learning_retrain_proposal_lineage()",
        "public.wp06_require_learning_drift_member_lineage()",
        "public.wp06_validate_learning_drift_assessment_membership()",
        "public.wp06_lock_learning_drift_assessment_cohort()",
        "public.wp06_lock_learning_model_active_pointer()",
        "public.wp06_lock_learning_shadow_cohort(uuid, uuid, integer)",
        "public.wp06_require_learning_shadow_lineage()",
        "public.wp06_learning_registry_approval_event_hash(uuid, uuid, uuid)",
        "public.wp06_shadow_decision_source_rights_supported(uuid, uuid)",
        "public.wp06_shadow_market_source_rights_matches(text, text)",
        "public.wp06_shadow_current_paper_rights_receipt(uuid)",
        "public.wp06_shadow_current_paper_rights_hash(uuid, text, text, integer, timestamptz)",
        "public.wp06_canonical_source_cost(numeric)",
        "public.wp06_shadow_observation_idempotency_key(public.learning_shadow_observation)",
        "public.wp06_shadow_observation_content_hash(public.learning_shadow_observation)",
        "public.wp06_shadow_observation_canonical_document(public.learning_shadow_observation)",
        "public.wp06_drift_assessment_idempotency_key(public.learning_drift_assessment)",
        "public.wp06_drift_assessment_content_hash(public.learning_drift_assessment, json)",
        "public.wp06_retrain_proposal_idempotency_key(public.learning_retrain_proposal)",
        "public.wp06_retrain_proposal_content_hash(public.learning_retrain_proposal)",
        "public.wp06_sha256_canonical_json(json)",
        "public.wp06_canonical_utc_timestamp_json(timestamptz)",
        "public.wp06_canonical_float_json(double precision)",
        "public.wp06_compact_canonical_json(json)",
    ):
        op.execute(f"DROP FUNCTION {function}")
    op.execute(
        "REVOKE SELECT (workspace_id, id, feature_schema_hash) "
        "ON TABLE public.learning_training_artifact FROM crypidian_learning_shadow_observer"
    )
    op.execute(
        "REVOKE SELECT (workspace_id, registry_id, id, sequence, action, to_state, "
        "mfa_verified_at) "
        "ON TABLE public.learning_model_registry_event FROM crypidian_learning_shadow_observer"
    )
    for table in TABLES:
        op.execute(f'DROP POLICY {table}_admin_select ON public."{table}"')
        op.execute(f'DROP POLICY {table}_observer_insert ON public."{table}"')
        op.execute(f'DROP POLICY {table}_observer_select ON public."{table}"')
    for table in SOURCE_TABLES:
        op.execute(f'DROP POLICY {table}_learning_shadow_observer_select ON public."{table}"')
    for table in reversed(TABLES):
        op.drop_table(table)
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {OBSERVER}")
    op.drop_constraint(
        "uq_learning_model_registry_event_exact_entry",
        "learning_model_registry_event",
        type_="unique",
    )


__all__ = ["OBSERVER_COLUMNS"]
