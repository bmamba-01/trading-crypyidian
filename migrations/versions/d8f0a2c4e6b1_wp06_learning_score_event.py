# Các câu SQL được giữ nguyên để có thể đối chiếu trigger và quyền PostgreSQL.
# ruff: noqa: E501
"""WP06 C4: persist metadata-only scorer results with the bot decision."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8f0a2c4e6b1"
down_revision: str | None = "d7f9a1b3c5e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER = "crypidian_market_signal_worker"
ADMIN = "crypidian_admin_web"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
BOT_SCOPE = (
    f"{WORKSPACE} AND bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid"
)
TABLE = "learning_score_event"


def upgrade() -> None:
    _add_bot_decision_scope_key()
    _create_score_event_table()
    _grant_rights_reader_metadata()
    _create_scope_policies()
    _grant_worker_runtime_metadata()
    _create_integrity_helpers()
    _create_guard()
    _grant_worker_access()
    _grant_admin_read_access()


def _add_bot_decision_scope_key() -> None:
    op.create_unique_constraint(
        "uq_bot_decision_workspace_bot_id",
        "bot_decision",
        ["workspace_id", "bot_id", "id"],
    )


def _create_score_event_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("bot_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("bot_decision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("bot_decision_hash", sa.String(64), nullable=False),
        sa.Column("incumbent_decision_hash", sa.String(64), nullable=False),
        sa.Column("incumbent_status", sa.String(16), nullable=False),
        sa.Column("final_status", sa.String(16), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("decision_snapshot_id", sa.String(64), nullable=False),
        sa.Column("vector_hash", sa.String(64), nullable=False),
        sa.Column("feature_schema_version", sa.String(80), nullable=False),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column(
            "timeframe_snapshot_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("source_event_digests", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_rights_fingerprint", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("deployment_event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("deployment_revision", sa.Integer(), nullable=False),
        sa.Column("registry_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("registry_revision", sa.Integer(), nullable=False),
        sa.Column("active_pointer_version", sa.Integer(), nullable=False),
        sa.Column("scorer_artifact_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scorer_artifact_hash", sa.String(64), nullable=False),
        sa.Column("base_artifact_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_report_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("evaluation_report_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_status", sa.String(40), nullable=False),
        sa.Column("report_scope_hash", sa.String(64), nullable=False),
        sa.Column("proof_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("proof_scope_hash", sa.String(64), nullable=False),
        sa.Column("candidate_completeness_verified", sa.Boolean(), nullable=False),
        sa.Column("policy_id", sa.String(128), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("class_probabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("probability", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("uncertainty", sa.Float(), nullable=True),
        sa.Column("model_latency_ms", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("canonical_event", sa.Text(), nullable=False),
        sa.Column("event_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_learning_score_event_workspace_id_workspace"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            name="fk_learning_score_event_bot_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "registry_id"],
            ["learning_model_registry.workspace_id", "learning_model_registry.id"],
            name="fk_learning_score_event_registry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scorer_artifact_id"],
            ["learning_scorer_artifact.workspace_id", "learning_scorer_artifact.id"],
            name="fk_learning_score_event_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evaluation_report_id"],
            [
                "learning_scorer_test_evaluation_report.workspace_id",
                "learning_scorer_test_evaluation_report.id",
            ],
            name="fk_learning_score_event_report",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            [
                "learning_scorer_test_opportunity_proof.workspace_id",
                "learning_scorer_test_opportunity_proof.id",
            ],
            name="fk_learning_score_event_proof",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "deployment_event_id"],
            [
                "learning_scoring_deployment_event.workspace_id",
                "learning_scoring_deployment_event.id",
            ],
            name="fk_learning_score_event_deployment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_score_event")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_learning_score_event_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "bot_decision_id", name="uq_learning_score_event_decision"
        ),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_learning_score_event_idempotency"
        ),
        sa.CheckConstraint(
            "mode IN ('SHADOW','PAPER_GATE') AND decision IN ('APPROVE','VETO','ABSTAIN') "
            "AND incumbent_status = 'CANDIDATE' AND incumbent_status IN ('CANDIDATE','ABSTAIN','VETO') "
            "AND final_status IN ('CANDIDATE','ABSTAIN','VETO') "
            "AND (mode <> 'SHADOW' OR bot_decision_hash = incumbent_decision_hash)",
            name=op.f("ck_learning_score_event_learning_score_event_state_valid"),
        ),
        sa.CheckConstraint(
            "evaluation_status IN ('ACCEPTED_SHADOW_ONLY','ACCEPTED_PAPER_GATE_ELIGIBLE') "
            "AND (mode <> 'PAPER_GATE' OR (evaluation_status = 'ACCEPTED_PAPER_GATE_ELIGIBLE' "
            "AND candidate_completeness_verified IS TRUE))",
            name=op.f("ck_learning_score_event_learning_score_event_eligibility_valid"),
        ),
        sa.CheckConstraint(
            "deployment_revision >= 1 AND registry_revision >= 1 AND active_pointer_version >= 0 "
            "AND model_latency_ms >= 0",
            name=op.f("ck_learning_score_event_learning_score_event_versions_valid"),
        ),
        sa.CheckConstraint(
            "bot_decision_hash ~ '^[0-9a-f]{64}$' AND incumbent_decision_hash ~ '^[0-9a-f]{64}$' "
            "AND candidate_id ~ '^[0-9a-f]{64}$' AND candidate_hash ~ '^[0-9a-f]{64}$' "
            "AND decision_snapshot_id ~ '^[0-9a-f]{64}$' AND vector_hash ~ '^[0-9a-f]{64}$' "
            "AND feature_schema_hash ~ '^[0-9a-f]{64}$' AND source_rights_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND scorer_artifact_hash ~ '^[0-9a-f]{64}$' AND base_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND evaluation_report_hash ~ '^[0-9a-f]{64}$' AND report_scope_hash ~ '^[0-9a-f]{64}$' "
            "AND proof_hash ~ '^[0-9a-f]{64}$' AND proof_scope_hash = report_scope_hash "
            "AND policy_hash ~ '^[0-9a-f]{64}$' AND event_hash ~ '^[0-9a-f]{64}$' "
            "AND idempotency_key ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_learning_score_event_learning_score_event_hashes_valid"),
        ),
        sa.CheckConstraint(
            "length(btrim(feature_schema_version)) > 0 AND length(btrim(policy_id)) > 0 "
            "AND model_latency_ms >= 0 AND jsonb_typeof(timeframe_snapshot_ids) = 'array' "
            "AND jsonb_array_length(timeframe_snapshot_ids) > 0 "
            "AND jsonb_typeof(source_event_digests) = 'array' "
            "AND jsonb_array_length(source_event_digests) > 0 AND jsonb_typeof(class_probabilities) = 'array'",
            name=op.f("ck_learning_score_event_learning_score_event_metadata_valid"),
        ),
        sa.CheckConstraint(
            "(decision = 'ABSTAIN' AND reason_code IS NOT NULL AND class_probabilities = '[]'::jsonb "
            "AND probability IS NULL AND confidence IS NULL AND uncertainty IS NULL) OR "
            "(decision IN ('APPROVE','VETO') AND (decision = 'APPROVE' AND reason_code IS NULL OR "
            "decision = 'VETO' AND reason_code IS NOT NULL) AND jsonb_array_length(class_probabilities) = 3 "
            "AND probability BETWEEN 0 AND 1 AND confidence BETWEEN 0 AND 1 AND uncertainty BETWEEN 0 AND 1)",
            name=op.f("ck_learning_score_event_learning_score_event_result_valid"),
        ),
        sa.CheckConstraint(
            "event_payload->>'schema_version' = 'crypidian.learning.score-event.v1' "
            "AND NOT (event_payload ?| ARRAY['features','feature_values','labels','sample_ids',"
            "'test_payload','raw_source','candidate_json'])",
            name=op.f("ck_learning_score_event_learning_score_event_payload_me_de7b"),
        ),
    )
    op.create_index(
        "ix_learning_score_event_workspace_created", TABLE, ["workspace_id", "created_at"]
    )


def _grant_rights_reader_metadata() -> None:
    # Trigger owner is the same NOLOGIN role as the current-rights gateway.
    scopes = (
        ("bot_decision", "workspace_id,bot_id,id,status,candidate_id,decision_hash"),
        ("learning_model_registry", "workspace_id,id,revision,artifact_id,artifact_hash,state"),
        ("learning_model_active_pointer", "workspace_id,registry_id,version"),
        (
            "learning_scoring_deployment_pointer",
            "workspace_id,event_id,revision,active_pointer_version,mode",
        ),
        (
            "learning_scoring_deployment_event",
            "workspace_id,id,revision,mode,registry_id,registry_revision,active_pointer_version,"
            "scorer_artifact_id,scorer_artifact_hash,evaluation_report_id,evaluation_report_hash,"
            "evaluation_status,proof_id,proof_hash,candidate_completeness_verified,report_scope_hash,"
            "proof_scope_hash,policy_id,policy_hash",
        ),
    )
    for table, columns in scopes:
        op.execute(
            f"CREATE POLICY wp06_score_event_rights_reader ON public.{table} FOR SELECT "
            f"TO {RIGHTS_ROLE} USING ({WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ({columns}) ON public.{table} TO {RIGHTS_ROLE}")


def _create_scope_policies() -> None:
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY learning_score_event_worker_select ON public.{TABLE} "
        f"FOR SELECT TO {WORKER} USING ({BOT_SCOPE})"
    )


def _grant_worker_runtime_metadata() -> None:
    """Cấp projection runtime theo workspace, không cấp quyền đọc report/artifact."""
    projections = (
        (
            "learning_scoring_deployment_pointer",
            "wp06_score_event_runtime_deployment_pointer_select",
            "workspace_id,event_id,revision,active_pointer_version,mode",
        ),
        (
            "learning_scoring_deployment_event",
            "wp06_score_event_runtime_deployment_event_select",
            "workspace_id,id,mode,revision,scorer_artifact_id,evaluation_report_id",
        ),
        (
            "learning_model_active_pointer",
            "wp06_score_event_runtime_model_pointer_select",
            "workspace_id,version",
        ),
    )
    for table, policy, columns in projections:
        op.execute(
            f"CREATE POLICY {policy} ON public.{table} FOR SELECT TO {WORKER} USING ({WORKSPACE})"
        )
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {WORKER}")
        op.execute(f"GRANT SELECT ({columns}) ON public.{table} TO {WORKER}")
    op.execute(
        f"CREATE POLICY learning_score_event_worker_insert ON public.{TABLE} "
        f"FOR INSERT TO {WORKER} WITH CHECK ({BOT_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY learning_score_event_admin_select ON public.{TABLE} "
        f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
    )


def _grant_admin_read_access() -> None:
    """Cho admin đọc cột hiển thị; payload canonical luôn bị cấm."""
    op.execute(
        f"GRANT SELECT (workspace_id,id,created_at,mode,decision,reason_code,probability,"
        f"confidence,uncertainty) ON TABLE public.{TABLE} TO {ADMIN}"
    )


def _create_integrity_helpers() -> None:
    op.execute("""
        CREATE FUNCTION public.wp06_score_event_payload_has_exact_schema(value jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            expected_keys text[] := ARRAY[
                'active_pointer_version','base_artifact_hash','bot_decision_hash',
                'bot_decision_id','bot_id','candidate_completeness_verified','candidate_hash',
                'candidate_id','class_probabilities','confidence','decision',
                'decision_snapshot_id','deployment_event_id','deployment_revision',
                'evaluation_report_hash','evaluation_report_id','evaluation_status',
                'feature_schema_hash','feature_schema_version','final_status','idempotency_key',
                'incumbent_decision_hash','incumbent_status','mode','model_latency_ms',
                'policy_hash','policy_id','probability','proof_hash','proof_id','proof_scope_hash',
                'reason_code','registry_id','registry_revision','report_scope_hash','schema_version',
                'scorer_artifact_hash','scorer_artifact_id','source_event_digests',
                'source_rights_fingerprint','timeframe_snapshot_ids','uncertainty','vector_hash',
                'workspace_id'
            ];
            item jsonb;
            actual_key_count integer;
        BEGIN
            IF pg_catalog.jsonb_typeof(value) <> 'object' THEN
                RETURN false;
            END IF;
            SELECT pg_catalog.count(*) INTO actual_key_count
            FROM pg_catalog.jsonb_object_keys(value);
            IF actual_key_count <> pg_catalog.cardinality(expected_keys)
               OR value - expected_keys <> '{}'::jsonb THEN
                RETURN false;
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_catalog.unnest(ARRAY[
                    'base_artifact_hash','bot_decision_hash','bot_decision_id','bot_id',
                    'candidate_hash','candidate_id','decision','decision_snapshot_id',
                    'deployment_event_id','evaluation_report_hash','evaluation_report_id',
                    'evaluation_status','feature_schema_hash','feature_schema_version',
                    'final_status','idempotency_key','incumbent_decision_hash','incumbent_status',
                    'mode','policy_hash','policy_id','proof_hash','proof_id','proof_scope_hash',
                    'registry_id','report_scope_hash','schema_version','scorer_artifact_hash',
                    'scorer_artifact_id','source_rights_fingerprint','vector_hash','workspace_id'
                ]) AS keys(key)
                WHERE pg_catalog.jsonb_typeof(value->key) <> 'string'
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.unnest(ARRAY[
                    'active_pointer_version','deployment_revision','model_latency_ms','registry_revision'
                ]) AS keys(key)
                WHERE pg_catalog.jsonb_typeof(value->key) <> 'number'
                   OR (value->>key) !~ '^-?[0-9]+$'
            ) OR pg_catalog.jsonb_typeof(value->'candidate_completeness_verified') <> 'boolean'
              OR pg_catalog.jsonb_typeof(value->'reason_code') NOT IN ('string','null')
              OR pg_catalog.jsonb_typeof(value->'probability') NOT IN ('number','null')
              OR pg_catalog.jsonb_typeof(value->'confidence') NOT IN ('number','null')
              OR pg_catalog.jsonb_typeof(value->'uncertainty') NOT IN ('number','null') THEN
                RETURN false;
            END IF;
            IF pg_catalog.jsonb_typeof(value->'timeframe_snapshot_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(value->'source_event_digests') <> 'array'
               OR pg_catalog.jsonb_typeof(value->'class_probabilities') <> 'array' THEN
                RETURN false;
            END IF;
            FOR item IN SELECT entry FROM pg_catalog.jsonb_array_elements(
                value->'timeframe_snapshot_ids'
            ) AS rows(entry)
            LOOP
                IF pg_catalog.jsonb_typeof(item) <> 'array'
                   OR pg_catalog.jsonb_array_length(item) <> 2
                   OR pg_catalog.jsonb_typeof(item->0) <> 'string'
                   OR pg_catalog.jsonb_typeof(item->1) <> 'string' THEN
                    RETURN false;
                END IF;
            END LOOP;
            FOR item IN SELECT entry FROM pg_catalog.jsonb_array_elements(
                value->'source_event_digests'
            ) AS rows(entry)
            LOOP
                IF pg_catalog.jsonb_typeof(item) <> 'array'
                   OR pg_catalog.jsonb_array_length(item) <> 2
                   OR pg_catalog.jsonb_typeof(item->0) <> 'string'
                   OR pg_catalog.jsonb_typeof(item->1) <> 'string' THEN
                    RETURN false;
                END IF;
            END LOOP;
            FOR item IN SELECT entry FROM pg_catalog.jsonb_array_elements(
                value->'class_probabilities'
            ) AS rows(entry)
            LOOP
                IF pg_catalog.jsonb_typeof(item) <> 'array'
                   OR pg_catalog.jsonb_array_length(item) <> 2
                   OR pg_catalog.jsonb_typeof(item->0) <> 'number'
                   OR pg_catalog.jsonb_typeof(item->1) <> 'number' THEN
                    RETURN false;
                END IF;
            END LOOP;
            RETURN true;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_score_event_payload_has_exact_schema(jsonb) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_score_event_payload_has_exact_schema(jsonb) "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_score_event_has_forbidden_payload(value jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE item record;
        DECLARE forbidden text[] := ARRAY[
            'feature','features','feature_values','vector','vector_values','labels','sample_id',
            'sample_ids','test_payload','raw_source','outcomes','trade_rows','candidate_json',
            'raw_features','payload_rows'
        ];
        BEGIN
            IF pg_catalog.jsonb_typeof(value) = 'object' THEN
                FOR item IN SELECT key, val FROM pg_catalog.jsonb_each(value) AS pair(key,val)
                LOOP
                    IF item.key = ANY(forbidden)
                       OR public.wp06_score_event_has_forbidden_payload(item.val) THEN
                        RETURN true;
                    END IF;
                END LOOP;
            ELSIF pg_catalog.jsonb_typeof(value) = 'array' THEN
                FOR item IN SELECT val FROM pg_catalog.jsonb_array_elements(value) AS pair(val)
                LOOP
                    IF public.wp06_score_event_has_forbidden_payload(item.val) THEN
                        RETURN true;
                    END IF;
                END LOOP;
            END IF;
            RETURN false;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_score_event_has_forbidden_payload(jsonb) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_score_event_has_forbidden_payload(jsonb) "
        f"TO {RIGHTS_ROLE}"
    )


def _create_guard() -> None:
    op.execute(f"""
        CREATE FUNCTION public.wp06_learning_score_event_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE decision_row record;
        DECLARE deployment_row record;
        DECLARE pointer_row record;
        DECLARE registry_row record;
        DECLARE artifact_row record;
        DECLARE report_row record;
        DECLARE proof_row record;
        DECLARE rights_row record;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_APPEND_ONLY';
            END IF;
            IF session_user <> '{WORKER}' THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_CALLER_DENIED';
            END IF;
            IF NEW.workspace_id IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR NEW.bot_id IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_SCOPE_DENIED';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-scoring-deployment:' || NEW.workspace_id::text ||
                chr(58) || 'current-pointer', 0
            ));
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-model-registry:' || NEW.workspace_id::text ||
                chr(58) || 'active-pointer', 0
            ));
            IF NEW.event_payload IS DISTINCT FROM NEW.canonical_event::jsonb
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    NEW.canonical_event, 'UTF8')), 'hex') IS DISTINCT FROM NEW.event_hash
               OR public.wp06_score_event_has_forbidden_payload(NEW.event_payload)
               OR NOT public.wp06_score_event_payload_has_exact_schema(NEW.event_payload)
               OR NEW.event_payload->>'schema_version' IS DISTINCT FROM
                    'crypidian.learning.score-event.v1'
               OR NEW.event_payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
               OR NEW.event_payload->>'bot_id' IS DISTINCT FROM NEW.bot_id::text
               OR NEW.event_payload->>'bot_decision_id' IS DISTINCT FROM NEW.bot_decision_id::text
               OR NEW.event_payload->>'bot_decision_hash' IS DISTINCT FROM NEW.bot_decision_hash
               OR NEW.event_payload->>'incumbent_decision_hash' IS DISTINCT FROM NEW.incumbent_decision_hash
               OR NEW.event_payload->>'incumbent_status' IS DISTINCT FROM NEW.incumbent_status
               OR NEW.event_payload->>'final_status' IS DISTINCT FROM NEW.final_status
               OR NEW.event_payload->>'candidate_id' IS DISTINCT FROM NEW.candidate_id
               OR NEW.event_payload->>'candidate_hash' IS DISTINCT FROM NEW.candidate_hash
               OR NEW.event_payload->>'decision_snapshot_id' IS DISTINCT FROM NEW.decision_snapshot_id
               OR NEW.event_payload->>'vector_hash' IS DISTINCT FROM NEW.vector_hash
               OR NEW.event_payload->>'feature_schema_version' IS DISTINCT FROM NEW.feature_schema_version
               OR NEW.event_payload->>'feature_schema_hash' IS DISTINCT FROM NEW.feature_schema_hash
               OR NEW.event_payload->'timeframe_snapshot_ids' IS DISTINCT FROM NEW.timeframe_snapshot_ids
               OR NEW.event_payload->'source_event_digests' IS DISTINCT FROM NEW.source_event_digests
               OR NEW.event_payload->>'source_rights_fingerprint' IS DISTINCT FROM NEW.source_rights_fingerprint
               OR NEW.event_payload->>'mode' IS DISTINCT FROM NEW.mode
               OR NEW.event_payload->>'deployment_event_id' IS DISTINCT FROM NEW.deployment_event_id::text
               OR (NEW.event_payload->>'deployment_revision')::integer IS DISTINCT FROM NEW.deployment_revision
               OR NEW.event_payload->>'registry_id' IS DISTINCT FROM NEW.registry_id::text
               OR (NEW.event_payload->>'registry_revision')::integer IS DISTINCT FROM NEW.registry_revision
               OR (NEW.event_payload->>'active_pointer_version')::integer IS DISTINCT FROM NEW.active_pointer_version
               OR NEW.event_payload->>'scorer_artifact_id' IS DISTINCT FROM NEW.scorer_artifact_id::text
               OR NEW.event_payload->>'scorer_artifact_hash' IS DISTINCT FROM NEW.scorer_artifact_hash
               OR NEW.event_payload->>'base_artifact_hash' IS DISTINCT FROM NEW.base_artifact_hash
               OR NEW.event_payload->>'evaluation_report_id' IS DISTINCT FROM NEW.evaluation_report_id::text
               OR NEW.event_payload->>'evaluation_report_hash' IS DISTINCT FROM NEW.evaluation_report_hash
               OR NEW.event_payload->>'evaluation_status' IS DISTINCT FROM NEW.evaluation_status
               OR NEW.event_payload->>'report_scope_hash' IS DISTINCT FROM NEW.report_scope_hash
               OR NEW.event_payload->>'proof_id' IS DISTINCT FROM NEW.proof_id::text
               OR NEW.event_payload->>'proof_hash' IS DISTINCT FROM NEW.proof_hash
               OR NEW.event_payload->>'proof_scope_hash' IS DISTINCT FROM NEW.proof_scope_hash
               OR (NEW.event_payload->>'candidate_completeness_verified')::boolean
                    IS DISTINCT FROM NEW.candidate_completeness_verified
               OR NEW.event_payload->>'policy_id' IS DISTINCT FROM NEW.policy_id
               OR NEW.event_payload->>'policy_hash' IS DISTINCT FROM NEW.policy_hash
               OR NEW.event_payload->>'decision' IS DISTINCT FROM NEW.decision
               OR NEW.event_payload->>'reason_code' IS DISTINCT FROM NEW.reason_code
               OR NEW.event_payload->'class_probabilities' IS DISTINCT FROM NEW.class_probabilities
               OR (NEW.event_payload->>'probability')::double precision IS DISTINCT FROM NEW.probability
               OR (NEW.event_payload->>'confidence')::double precision IS DISTINCT FROM NEW.confidence
               OR (NEW.event_payload->>'uncertainty')::double precision IS DISTINCT FROM NEW.uncertainty
               OR (NEW.event_payload->>'model_latency_ms')::integer IS DISTINCT FROM NEW.model_latency_ms
               OR NEW.event_payload->>'idempotency_key' IS DISTINCT FROM NEW.idempotency_key THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_CANONICAL_INVALID';
            END IF;
            IF NEW.mode = 'SHADOW' AND NEW.bot_decision_hash IS DISTINCT FROM NEW.incumbent_decision_hash THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_SHADOW_MUTATION_DENIED';
            END IF;

            SELECT row.bot_id, row.status, row.candidate_id, row.decision_hash
              INTO decision_row
              FROM public.bot_decision AS row
             WHERE row.workspace_id = NEW.workspace_id AND row.id = NEW.bot_decision_id;
            IF NOT FOUND OR decision_row.bot_id IS DISTINCT FROM NEW.bot_id
               OR decision_row.status IS DISTINCT FROM NEW.final_status
               OR decision_row.decision_hash IS DISTINCT FROM NEW.bot_decision_hash
               OR (NEW.final_status = 'CANDIDATE' AND decision_row.candidate_id IS DISTINCT FROM NEW.candidate_id)
               OR (NEW.final_status <> 'CANDIDATE' AND decision_row.candidate_id IS NOT NULL)
               OR (NEW.mode = 'SHADOW' AND NEW.bot_decision_hash IS DISTINCT FROM NEW.incumbent_decision_hash) THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DECISION_MISMATCH';
            END IF;

            SELECT row.id, row.revision, row.mode, row.registry_id, row.registry_revision,
                   row.active_pointer_version, row.scorer_artifact_id, row.scorer_artifact_hash,
                   row.evaluation_report_id, row.evaluation_report_hash, row.evaluation_status,
                   row.proof_id, row.proof_hash, row.candidate_completeness_verified,
                   row.report_scope_hash, row.proof_scope_hash, row.policy_id, row.policy_hash
              INTO deployment_row
              FROM public.learning_scoring_deployment_event AS row
             WHERE row.workspace_id = NEW.workspace_id AND row.id = NEW.deployment_event_id;
            IF NOT FOUND OR deployment_row.revision IS DISTINCT FROM NEW.deployment_revision
               OR deployment_row.mode IS DISTINCT FROM NEW.mode
               OR deployment_row.registry_id IS DISTINCT FROM NEW.registry_id
               OR deployment_row.registry_revision IS DISTINCT FROM NEW.registry_revision
               OR deployment_row.active_pointer_version IS DISTINCT FROM NEW.active_pointer_version
               OR deployment_row.scorer_artifact_id IS DISTINCT FROM NEW.scorer_artifact_id
               OR deployment_row.scorer_artifact_hash IS DISTINCT FROM NEW.scorer_artifact_hash
               OR deployment_row.evaluation_report_id IS DISTINCT FROM NEW.evaluation_report_id
               OR deployment_row.evaluation_report_hash IS DISTINCT FROM NEW.evaluation_report_hash
               OR deployment_row.evaluation_status IS DISTINCT FROM NEW.evaluation_status
               OR deployment_row.proof_id IS DISTINCT FROM NEW.proof_id
               OR deployment_row.proof_hash IS DISTINCT FROM NEW.proof_hash
               OR deployment_row.candidate_completeness_verified IS DISTINCT FROM NEW.candidate_completeness_verified
               OR deployment_row.report_scope_hash IS DISTINCT FROM NEW.report_scope_hash
               OR deployment_row.proof_scope_hash IS DISTINCT FROM NEW.proof_scope_hash
               OR deployment_row.policy_id IS DISTINCT FROM NEW.policy_id
               OR deployment_row.policy_hash IS DISTINCT FROM NEW.policy_hash THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DEPLOYMENT_MISMATCH';
            END IF;
            SELECT row.event_id, row.revision, row.active_pointer_version, row.mode
              INTO pointer_row
              FROM public.learning_scoring_deployment_pointer AS row
             WHERE row.workspace_id = NEW.workspace_id;
            IF NOT FOUND OR pointer_row.event_id IS DISTINCT FROM NEW.deployment_event_id
               OR pointer_row.revision IS DISTINCT FROM NEW.deployment_revision
               OR pointer_row.active_pointer_version IS DISTINCT FROM NEW.active_pointer_version
               OR pointer_row.mode IS DISTINCT FROM NEW.mode THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DEPLOYMENT_STALE';
            END IF;

            SELECT row.revision, row.artifact_id, row.artifact_hash, row.state
              INTO registry_row
              FROM public.learning_model_registry AS row
             WHERE row.workspace_id = NEW.workspace_id AND row.id = NEW.registry_id;
            IF NOT FOUND OR registry_row.revision IS DISTINCT FROM NEW.registry_revision
               OR registry_row.state IS DISTINCT FROM 'paper_active' THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_REGISTRY_MISMATCH';
            END IF;
            SELECT row.registry_id, row.version
              INTO pointer_row
              FROM public.learning_model_active_pointer AS row
             WHERE row.workspace_id = NEW.workspace_id;
            IF NOT FOUND OR pointer_row.registry_id IS DISTINCT FROM NEW.registry_id
               OR pointer_row.version IS DISTINCT FROM NEW.active_pointer_version THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_ACTIVE_POINTER_STALE';
            END IF;

            SELECT row.base_artifact_id, row.base_artifact_hash, row.artifact_hash, row.state
              INTO artifact_row
              FROM public.learning_scorer_artifact AS row
             WHERE row.workspace_id = NEW.workspace_id AND row.id = NEW.scorer_artifact_id;
            IF NOT FOUND OR artifact_row.artifact_hash IS DISTINCT FROM NEW.scorer_artifact_hash
               OR artifact_row.base_artifact_hash IS DISTINCT FROM NEW.base_artifact_hash
               OR artifact_row.state IS DISTINCT FROM 'ready_for_evaluation'
               OR registry_row.artifact_id IS DISTINCT FROM artifact_row.base_artifact_id
               OR registry_row.artifact_hash IS DISTINCT FROM artifact_row.base_artifact_hash THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_ARTIFACT_MISMATCH';
            END IF;

            SELECT row.report_hash, row.proof_id, row.proof_hash, row.scorer_artifact_hash,
                   row.base_artifact_hash, row.policy_version, row.policy_hash, row.evaluation_status,
                   row.candidate_completeness_verified, row.report, row.canonical_report
              INTO report_row
              FROM public.learning_scorer_test_evaluation_report AS row
             WHERE row.workspace_id = NEW.workspace_id AND row.id = NEW.evaluation_report_id;
            IF NOT FOUND OR report_row.report_hash IS DISTINCT FROM NEW.evaluation_report_hash
               OR report_row.proof_id IS DISTINCT FROM NEW.proof_id
               OR report_row.proof_hash IS DISTINCT FROM NEW.proof_hash
               OR report_row.scorer_artifact_hash IS DISTINCT FROM NEW.scorer_artifact_hash
               OR report_row.base_artifact_hash IS DISTINCT FROM NEW.base_artifact_hash
               OR report_row.policy_version IS DISTINCT FROM NEW.policy_id
               OR report_row.policy_hash IS DISTINCT FROM NEW.policy_hash
               OR report_row.evaluation_status IS DISTINCT FROM NEW.evaluation_status
               OR report_row.candidate_completeness_verified IS DISTINCT FROM NEW.candidate_completeness_verified
               OR report_row.report IS DISTINCT FROM report_row.canonical_report::jsonb
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    report_row.canonical_report, 'UTF8')), 'hex') IS DISTINCT FROM report_row.report_hash
               OR report_row.report->>'source_scope_hash' IS DISTINCT FROM NEW.report_scope_hash
               OR report_row.report->>'evaluation_status' IS DISTINCT FROM NEW.evaluation_status
               OR report_row.report->>'scorer_artifact_hash' IS DISTINCT FROM NEW.scorer_artifact_hash
               OR report_row.report->>'policy_hash' IS DISTINCT FROM NEW.policy_hash
               OR public.wp06_scorer_test_report_has_forbidden_payload(report_row.report) THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_REPORT_MISMATCH';
            END IF;

            SELECT row.proof_hash, row.source_scope_hash, row.bundle_hash, row.canonical_proof
              INTO proof_row
              FROM public.learning_scorer_test_opportunity_proof AS row
             WHERE row.workspace_id = NEW.workspace_id AND row.id = NEW.proof_id;
            IF NOT FOUND OR proof_row.proof_hash IS DISTINCT FROM NEW.proof_hash
               OR proof_row.source_scope_hash IS DISTINCT FROM NEW.proof_scope_hash
               OR NEW.proof_scope_hash IS DISTINCT FROM NEW.report_scope_hash
               OR report_row.report->>'source_scope_hash' IS DISTINCT FROM proof_row.source_scope_hash
               OR report_row.report->>'bundle_hash' IS DISTINCT FROM proof_row.bundle_hash
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    proof_row.canonical_proof, 'UTF8')), 'hex') IS DISTINCT FROM proof_row.proof_hash THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_PROOF_SCOPE_MISMATCH';
            END IF;

            SELECT rights.* INTO rights_row
              FROM public.wp06_verify_scorer_deployment_rights(
                  NEW.workspace_id, NEW.scorer_artifact_id, NEW.evaluation_report_id
              ) AS rights;
            IF NOT FOUND
               OR rights_row.artifact_hash IS DISTINCT FROM NEW.scorer_artifact_hash
               OR rights_row.report_hash IS DISTINCT FROM NEW.evaluation_report_hash
               OR rights_row.proof_hash IS DISTINCT FROM NEW.proof_hash
               OR rights_row.policy_hash IS DISTINCT FROM NEW.policy_hash
               OR rights_row.fingerprint IS DISTINCT FROM NEW.source_rights_fingerprint
               OR (NEW.mode = 'SHADOW' AND rights_row.shadow_eligible IS NOT TRUE)
               OR (NEW.mode = 'PAPER_GATE' AND rights_row.paper_gate_eligible IS NOT TRUE) THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_CURRENT_RIGHTS_DENIED';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"ALTER FUNCTION public.wp06_learning_score_event_guard() OWNER TO {RIGHTS_ROLE}")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_learning_score_event_guard() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {TABLE}_guard BEFORE INSERT OR UPDATE OR DELETE ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_learning_score_event_guard()"
    )


def _grant_worker_access() -> None:
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE public.{TABLE} FROM "
        f"PUBLIC, crypidian_app, crypidian_trading_runtime, {WORKER}, {ADMIN}"
    )
    op.execute(f"GRANT INSERT ON TABLE public.{TABLE} TO {WORKER}")
    op.execute(
        f"GRANT SELECT (workspace_id,bot_id,bot_decision_id,idempotency_key,event_hash,"
        f"canonical_event) ON TABLE public.{TABLE} TO {WORKER}"
    )


def downgrade() -> None:
    op.execute(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN
            RAISE EXCEPTION 'WP06_SCORE_EVENT_POPULATED_DOWNGRADE_DENIED';
        END IF;
    END $$""")
    op.execute(f"DROP TRIGGER {TABLE}_guard ON public.{TABLE}")
    op.execute("DROP FUNCTION public.wp06_learning_score_event_guard()")
    op.execute("DROP FUNCTION public.wp06_score_event_payload_has_exact_schema(jsonb)")
    op.execute("DROP FUNCTION public.wp06_score_event_has_forbidden_payload(jsonb)")
    op.execute(f"DROP POLICY learning_score_event_worker_insert ON public.{TABLE}")
    op.execute(f"DROP POLICY learning_score_event_worker_select ON public.{TABLE}")
    op.execute(f"DROP POLICY learning_score_event_admin_select ON public.{TABLE}")
    op.execute(
        f"REVOKE SELECT (workspace_id,id,created_at,mode,decision,reason_code,probability,"
        f"confidence,uncertainty) ON TABLE public.{TABLE} FROM {ADMIN}"
    )
    op.execute(
        f"REVOKE SELECT (workspace_id,bot_id,bot_decision_id,idempotency_key,event_hash,"
        f"canonical_event) ON TABLE public.{TABLE} FROM {WORKER}"
    )
    op.drop_index("ix_learning_score_event_workspace_created", table_name=TABLE)
    op.drop_table(TABLE)
    runtime_projections = (
        (
            "learning_scoring_deployment_pointer",
            "wp06_score_event_runtime_deployment_pointer_select",
            "workspace_id,event_id,revision,active_pointer_version,mode",
        ),
        (
            "learning_scoring_deployment_event",
            "wp06_score_event_runtime_deployment_event_select",
            "workspace_id,id,mode,revision,scorer_artifact_id,evaluation_report_id",
        ),
        (
            "learning_model_active_pointer",
            "wp06_score_event_runtime_model_pointer_select",
            "workspace_id,version",
        ),
    )
    for table, policy, columns in runtime_projections:
        op.execute(f"DROP POLICY {policy} ON public.{table}")
        op.execute(f"REVOKE SELECT ({columns}) ON TABLE public.{table} FROM {WORKER}")
    rights_metadata = (
        ("bot_decision", "workspace_id,bot_id,id,status,candidate_id,decision_hash"),
        ("learning_model_registry", "workspace_id,id,revision,artifact_id,artifact_hash,state"),
        ("learning_model_active_pointer", "workspace_id,registry_id,version"),
        (
            "learning_scoring_deployment_pointer",
            "workspace_id,event_id,revision,active_pointer_version,mode",
        ),
        (
            "learning_scoring_deployment_event",
            "workspace_id,id,revision,mode,registry_id,registry_revision,active_pointer_version,"
            "scorer_artifact_id,scorer_artifact_hash,evaluation_report_id,evaluation_report_hash,"
            "evaluation_status,proof_id,proof_hash,candidate_completeness_verified,report_scope_hash,"
            "proof_scope_hash,policy_id,policy_hash",
        ),
    )
    for table, columns in rights_metadata:
        op.execute(f"DROP POLICY wp06_score_event_rights_reader ON public.{table}")
        op.execute(f"REVOKE SELECT ({columns}) ON TABLE public.{table} FROM {RIGHTS_ROLE}")
    op.drop_constraint("uq_bot_decision_workspace_bot_id", "bot_decision", type_="unique")
