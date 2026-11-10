"""WP06 C.3: receipt payload TEST dùng một lần và report chỉ lưu chỉ số tổng hợp."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8f0a2c4d6b8"
down_revision: str | None = "e6f8a0c2d4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVALUATOR = "crypidian_learning_evaluator"
READER = "crypidian_learning_evaluation_reader"
ADMIN = "crypidian_admin_web"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
RIGHTS_GATE = "public.wp06_assert_training_source_rights(uuid, text, jsonb, uuid, text, text, uuid)"
LOCK_FUNCTION = "public.wp06_lock_scorer_test_sources"
OPEN_FUNCTION = "public.wp06_open_scorer_test_access_receipt"
OPEN_SIGNATURE = f"{OPEN_FUNCTION}(uuid, uuid, uuid, uuid, uuid, text, text, text, text)"
CONSUME_FUNCTION = "public.wp06_consume_scorer_test_payload"
FORBIDDEN_FUNCTION = "public.wp06_scorer_test_report_has_forbidden_payload"


def upgrade() -> None:
    # Python rights gate chỉ đọc bundle, manifest và policy đã lưu khi đang giữ source lock.
    for table in (
        "backtest_dataset_replay_bundle",
        "backtest_dataset_manifest",
        "source_policy_revision",
        "source_policy_observation",
    ):
        op.execute(
            f"CREATE POLICY wp06_scorer_test_evaluator_metadata_read ON public.{table} "
            f"FOR SELECT TO {EVALUATOR} USING ({WORKSPACE})"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {EVALUATOR}")

    op.execute(
        f"CREATE POLICY wp06_scorer_test_source_rights_reader_select "
        "ON public.learning_run_source_rights_receipt "
        f"FOR SELECT TO {READER} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.learning_run_source_rights_receipt TO {READER}")

    # Definer cần các hàng này để lấy source lock từ manifest đã lưu.
    # Python gate của evaluator chỉ được đọc metadata của cùng bảng.
    op.execute(
        f"CREATE POLICY wp06_scorer_test_evaluation_reader_attempt "
        "ON public.learning_training_attempt "
        f"FOR SELECT TO {READER} USING ({WORKSPACE})"
    )
    op.execute(
        "GRANT SELECT (workspace_id, id, normalized_dataset_id, manifest_hash, "
        "fold_plan_hash, fold_id, approval_hash, split_policy_version) "
        f"ON public.learning_training_attempt TO {READER}"
    )
    op.execute(
        "CREATE POLICY wp06_scorer_test_evaluation_reader_scorer "
        "ON public.learning_scorer_artifact "
        f"FOR SELECT TO {READER} USING ({WORKSPACE})"
    )
    op.execute(
        "GRANT SELECT (workspace_id, attempt_id, base_artifact_id, normalized_dataset_id, "
        "manifest_hash, fold_plan_hash, fold_id, approval_hash, base_artifact_hash, "
        "schema_version, state, vector_schema_version, vector_schema_hash, artifact_hash) "
        f"ON public.learning_scorer_artifact TO {READER}"
    )
    op.execute(
        "CREATE POLICY wp06_scorer_test_evaluation_reader_base_artifact "
        "ON public.learning_training_artifact "
        f"FOR SELECT TO {READER} USING ({WORKSPACE})"
    )
    op.execute(
        "GRANT SELECT (workspace_id, id, attempt_id, normalized_dataset_id, artifact_hash, "
        "manifest_hash, fold_plan_hash, fold_id, approval_hash) "
        f"ON public.learning_training_artifact TO {READER}"
    )
    op.execute(
        "CREATE POLICY wp06_scorer_test_evaluation_reader_proof "
        "ON public.learning_scorer_test_opportunity_proof "
        f"FOR SELECT TO {READER} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT ON TABLE public.learning_scorer_test_opportunity_proof TO {READER}")

    op.create_table(
        "learning_scorer_test_access_receipt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("proof_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("fold_id", sa.String(64), nullable=False),
        sa.Column("approval_hash", sa.String(64), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("vector_schema_version", sa.String(128), nullable=False),
        sa.Column("vector_schema_hash", sa.String(64), nullable=False),
        sa.Column("test_membership_digest", sa.String(64), nullable=False),
        sa.Column("source_scope_hash", sa.String(64), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("scorer_artifact_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("current_rights_receipt_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("actor_principal", sa.String(80), nullable=False),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND fold_id ~ '^[0-9a-f]{64}$' AND approval_hash ~ '^[0-9a-f]{64}$' "
            "AND bundle_hash ~ '^[0-9a-f]{64}$' AND vector_schema_hash ~ '^[0-9a-f]{64}$' "
            "AND test_membership_digest ~ '^[0-9a-f]{64}$' "
            "AND source_scope_hash ~ '^[0-9a-f]{64}$' AND proof_hash ~ '^[0-9a-f]{64}$' "
            "AND scorer_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_hash ~ '^[0-9a-f]{64}$' "
            "AND current_rights_receipt_hash ~ '^[0-9a-f]{64}$' "
            "AND request_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND receipt_hash ~ '^[0-9a-f]{64}$'",
            name="learning_scorer_test_access_hashes_valid",
        ),
        sa.CheckConstraint(
            "purpose = 'scorer-test' AND "
            "actor_principal = 'crypidian_learning_evaluator' AND "
            "length(vector_schema_version) BETWEEN 1 AND 128",
            name="learning_scorer_test_access_actor_purpose_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_training_attempt.workspace_id", "learning_training_attempt.id"],
            name="fk_learning_scorer_test_access_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["learning_scorer_artifact.workspace_id", "learning_scorer_artifact.attempt_id"],
            name="fk_learning_scorer_test_access_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "normalized_dataset_id"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
                "learning_normalized_dataset.id",
            ],
            name="fk_learning_scorer_test_access_dataset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash"],
            ["learning_split_approval.workspace_id", "learning_split_approval.manifest_hash"],
            name="fk_learning_scorer_test_access_approval",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            [
                "learning_scorer_test_opportunity_proof.workspace_id",
                "learning_scorer_test_opportunity_proof.id",
            ],
            name="fk_learning_scorer_test_access_proof",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_scorer_test_access_receipt")),
        sa.UniqueConstraint(
            "workspace_id",
            "attempt_id",
            "fold_id",
            name="uq_learning_scorer_test_access_attempt_fold",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            name="uq_learning_scorer_test_access_evaluation_run",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            "id",
            "proof_id",
            name="uq_learning_scorer_test_access_run_receipt_proof",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_learning_scorer_test_access_workspace_id"
        ),
    )
    op.create_index(
        "ix_learning_scorer_test_access_workspace_created",
        "learning_scorer_test_access_receipt",
        ["workspace_id", "created_at"],
    )

    op.create_table(
        "learning_scorer_test_payload_consumption",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("payload_count", sa.Integer(), nullable=False),
        sa.Column("membership_digest", sa.String(64), nullable=False),
        sa.Column("payload_commitment_hash", sa.String(64), nullable=False),
        sa.Column("consumption_hash", sa.String(64), nullable=False),
        sa.Column("current_rights_receipt_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "payload_count > 0", name="learning_scorer_test_consumption_count_valid"
        ),
        sa.CheckConstraint(
            "membership_digest ~ '^[0-9a-f]{64}$' "
            "AND payload_commitment_hash ~ '^[0-9a-f]{64}$' "
            "AND consumption_hash ~ '^[0-9a-f]{64}$' "
            "AND current_rights_receipt_hash ~ '^[0-9a-f]{64}$' "
            "AND request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="learning_scorer_test_consumption_hashes_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "receipt_id"],
            [
                "learning_scorer_test_access_receipt.workspace_id",
                "learning_scorer_test_access_receipt.id",
            ],
            name="fk_learning_scorer_test_consumption_receipt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_scorer_test_payload_consumption")),
        sa.UniqueConstraint(
            "workspace_id",
            "receipt_id",
            "id",
            name="uq_learning_scorer_test_consumption_receipt_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "receipt_id",
            name="uq_learning_scorer_test_consumption_receipt",
        ),
    )
    op.create_index(
        "ix_learning_scorer_test_consumption_workspace_created",
        "learning_scorer_test_payload_consumption",
        ["workspace_id", "created_at"],
    )

    _create_forbidden_payload_helper()
    op.create_table(
        "learning_scorer_test_evaluation_report",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("access_receipt_id", sa.Uuid(), nullable=False),
        sa.Column("payload_consumption_id", sa.Uuid(), nullable=False),
        sa.Column("proof_id", sa.Uuid(), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("access_receipt_hash", sa.String(64), nullable=False),
        sa.Column("current_rights_receipt_hash", sa.String(64), nullable=False),
        sa.Column("payload_commitment_hash", sa.String(64), nullable=False),
        sa.Column("payload_consumption_hash", sa.String(64), nullable=False),
        sa.Column("payload_consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("scorer_artifact_hash", sa.String(64), nullable=False),
        sa.Column("base_artifact_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(120), nullable=False),
        sa.Column("evaluation_status", sa.String(40), nullable=False),
        sa.Column("candidate_completeness_verified", sa.Boolean(), nullable=False),
        sa.Column("scored_candidate_coverage_verified", sa.Boolean(), nullable=False),
        sa.Column("test_sample_count", sa.Integer(), nullable=False),
        sa.Column("opportunity_count", sa.Integer(), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_report", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "evaluation_status IN ('INCONCLUSIVE','ACCEPTED_SHADOW_ONLY',"
            "'ACCEPTED_PAPER_GATE_ELIGIBLE')",
            name="learning_scorer_test_report_status_valid",
        ),
        sa.CheckConstraint(
            "report_hash ~ '^[0-9a-f]{64}$' AND request_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND proof_hash ~ '^[0-9a-f]{64}$' "
            "AND scorer_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND base_artifact_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_hash ~ '^[0-9a-f]{64}$' "
            "AND access_receipt_hash ~ '^[0-9a-f]{64}$' "
            "AND current_rights_receipt_hash ~ '^[0-9a-f]{64}$' "
            "AND payload_commitment_hash ~ '^[0-9a-f]{64}$' "
            "AND payload_consumption_hash ~ '^[0-9a-f]{64}$'",
            name="learning_scorer_test_report_hashes_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(report) = 'object' AND length(canonical_report) > 0 "
            f"AND NOT {FORBIDDEN_FUNCTION}(report)",
            name="learning_scorer_test_report_sanitized_valid",
        ),
        sa.CheckConstraint(
            "evaluation_status <> 'ACCEPTED_PAPER_GATE_ELIGIBLE' OR "
            "(candidate_completeness_verified AND scored_candidate_coverage_verified)",
            name="learning_scorer_test_report_paper_gate_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evaluation_run_id", "access_receipt_id", "proof_id"],
            [
                "learning_scorer_test_access_receipt.workspace_id",
                "learning_scorer_test_access_receipt.evaluation_run_id",
                "learning_scorer_test_access_receipt.id",
                "learning_scorer_test_access_receipt.proof_id",
            ],
            name="fk_learning_scorer_test_report_exact_receipt_proof",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "access_receipt_id"],
            [
                "learning_scorer_test_access_receipt.workspace_id",
                "learning_scorer_test_access_receipt.id",
            ],
            name="fk_learning_scorer_test_report_receipt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "access_receipt_id", "payload_consumption_id"],
            [
                "learning_scorer_test_payload_consumption.workspace_id",
                "learning_scorer_test_payload_consumption.receipt_id",
                "learning_scorer_test_payload_consumption.id",
            ],
            name="fk_learning_scorer_test_report_consumption",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "proof_id"],
            [
                "learning_scorer_test_opportunity_proof.workspace_id",
                "learning_scorer_test_opportunity_proof.id",
            ],
            name="fk_learning_scorer_test_report_proof",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_scorer_test_evaluation_report")),
        sa.UniqueConstraint(
            "workspace_id",
            "evaluation_run_id",
            name="uq_learning_scorer_test_report_evaluation_run",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_learning_scorer_test_report_workspace_id"
        ),
    )
    op.create_index(
        "ix_learning_scorer_test_report_workspace_created",
        "learning_scorer_test_evaluation_report",
        ["workspace_id", "created_at"],
    )

    for table in (
        "learning_scorer_test_access_receipt",
        "learning_scorer_test_payload_consumption",
        "learning_scorer_test_evaluation_report",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, crypidian_app, "
            "crypidian_trading_runtime, crypidian_ingestion_worker, "
            "crypidian_execution_gateway, crypidian_backtest_worker, "
            "crypidian_signal_worker, crypidian_market_signal_worker, "
            "crypidian_replay_writer, crypidian_trainer_worker, "
            "crypidian_learning_batch_reader, crypidian_learning_shadow_observer, "
            f"{EVALUATOR}, {ADMIN}, {READER}"
        )
        op.execute(
            f"CREATE POLICY {table}_reader_select ON public.{table} "
            f"FOR SELECT TO {READER} USING ({WORKSPACE})"
        )
        op.execute(
            f"CREATE POLICY {table}_reader_insert ON public.{table} "
            f"FOR INSERT TO {READER} WITH CHECK ({WORKSPACE})"
        )
        op.execute(
            f"CREATE POLICY {table}_evaluator_select ON public.{table} "
            f"FOR SELECT TO {EVALUATOR} USING ({WORKSPACE})"
        )
        op.execute(
            f"CREATE POLICY {table}_admin_select ON public.{table} "
            f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
        )

    op.execute(
        "CREATE POLICY learning_scorer_test_report_evaluator_insert "
        "ON public.learning_scorer_test_evaluation_report "
        f"FOR INSERT TO {EVALUATOR} WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE public.learning_scorer_test_access_receipt, "
        "public.learning_scorer_test_payload_consumption, "
        f"public.learning_scorer_test_evaluation_report TO {READER}"
    )
    op.execute(
        "GRANT SELECT ON TABLE public.learning_scorer_test_access_receipt, "
        "public.learning_scorer_test_payload_consumption, "
        f"public.learning_scorer_test_evaluation_report TO {EVALUATOR}"
    )
    op.execute(
        f"GRANT INSERT ON TABLE public.learning_scorer_test_evaluation_report TO {EVALUATOR}"
    )
    op.execute(
        "GRANT SELECT ON TABLE public.learning_scorer_test_access_receipt, "
        f"public.learning_scorer_test_evaluation_report TO {ADMIN}"
    )

    op.execute("""CREATE FUNCTION public.reject_wp06_scorer_test_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'WP06_SCORER_TEST_LEDGER_APPEND_ONLY';
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION public.reject_wp06_scorer_test_mutation() FROM PUBLIC")
    for table in (
        "learning_scorer_test_access_receipt",
        "learning_scorer_test_payload_consumption",
        "learning_scorer_test_evaluation_report",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.reject_wp06_scorer_test_mutation()"
        )

    op.execute("""CREATE FUNCTION public.wp06_set_scorer_test_receipt_hash()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            payload jsonb;
        BEGIN
            IF current_user <> 'crypidian_learning_evaluation_reader' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_RECEIPT_CALLER_DENIED';
            END IF;
            payload := pg_catalog.jsonb_build_object(
                'actor_principal', NEW.actor_principal,
                'approval_hash', NEW.approval_hash,
                'attempt_id', NEW.attempt_id,
                'bundle_hash', NEW.bundle_hash,
                'created_at', NEW.created_at,
                'current_rights_receipt_hash', NEW.current_rights_receipt_hash,
                'evaluation_run_id', NEW.evaluation_run_id,
                'fold_id', NEW.fold_id,
                'fold_plan_hash', NEW.fold_plan_hash,
                'receipt_id', NEW.id,
                'manifest_hash', NEW.manifest_hash,
                'normalized_dataset_id', NEW.normalized_dataset_id,
                'policy_hash', NEW.policy_hash,
                'proof_hash', NEW.proof_hash,
                'proof_id', NEW.proof_id,
                'purpose', NEW.purpose,
                'request_fingerprint', NEW.request_fingerprint,
                'scorer_artifact_hash', NEW.scorer_artifact_hash,
                'source_scope_hash', NEW.source_scope_hash,
                'test_membership_digest', NEW.test_membership_digest,
                'vector_schema_hash', NEW.vector_schema_hash,
                'vector_schema_version', NEW.vector_schema_version,
                'workspace_id', NEW.workspace_id
            );
            NEW.receipt_hash := pg_catalog.encode(pg_catalog.sha256(
                pg_catalog.convert_to(payload::text, 'UTF8')), 'hex');
            RETURN NEW;
        END $$""")
    op.execute("""CREATE FUNCTION public.wp06_set_scorer_test_consumption_hash()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            payload jsonb;
        BEGIN
            IF current_user <> 'crypidian_learning_evaluation_reader' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_CONSUMPTION_CALLER_DENIED';
            END IF;
            payload := pg_catalog.jsonb_build_object(
                'consumed_at', NEW.consumed_at,
                'consumption_id', NEW.id,
                'current_rights_receipt_hash', NEW.current_rights_receipt_hash,
                'membership_digest', NEW.membership_digest,
                'payload_commitment_hash', NEW.payload_commitment_hash,
                'payload_count', NEW.payload_count,
                'receipt_id', NEW.receipt_id,
                'request_fingerprint', NEW.request_fingerprint,
                'workspace_id', NEW.workspace_id
            );
            NEW.consumption_hash := pg_catalog.encode(pg_catalog.sha256(
                pg_catalog.convert_to(payload::text, 'UTF8')), 'hex');
            RETURN NEW;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_set_scorer_test_receipt_hash() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_set_scorer_test_consumption_hash() FROM PUBLIC")
    for function in (
        "public.wp06_set_scorer_test_receipt_hash()",
        "public.wp06_set_scorer_test_consumption_hash()",
    ):
        op.execute(f"ALTER FUNCTION {function} OWNER TO {READER}")
    op.execute(
        "CREATE TRIGGER learning_scorer_test_access_receipt_hash "
        "BEFORE INSERT ON public.learning_scorer_test_access_receipt "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_set_scorer_test_receipt_hash()"
    )
    op.execute(
        "CREATE TRIGGER learning_scorer_test_consumption_hash "
        "BEFORE INSERT ON public.learning_scorer_test_payload_consumption "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_set_scorer_test_consumption_hash()"
    )

    _create_access_functions()
    _create_report_guard()


def _create_forbidden_payload_helper() -> None:
    """Tạo kiểm tra khóa đệ quy trước khi khai báo CHECK của bảng report."""
    op.execute(f"""CREATE FUNCTION {FORBIDDEN_FUNCTION}(value jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            item record;
            blocked_keys text[] := ARRAY[
                'sample_ids','sample_id','features','labels','outcomes','trade_rows',
                'trade_id','test_payload'
            ];
        BEGIN
            IF pg_catalog.jsonb_typeof(value) = 'object' THEN
                FOR item IN SELECT key, val FROM pg_catalog.jsonb_each(value) AS pair(key, val)
                LOOP
                    IF item.key = ANY(blocked_keys)
                       OR public.wp06_scorer_test_report_has_forbidden_payload(item.val) THEN
                        RETURN true;
                    END IF;
                END LOOP;
            ELSIF pg_catalog.jsonb_typeof(value) = 'array' THEN
                FOR item IN SELECT val FROM pg_catalog.jsonb_array_elements(value) AS pair(val)
                LOOP
                    IF public.wp06_scorer_test_report_has_forbidden_payload(item.val) THEN
                        RETURN true;
                    END IF;
                END LOOP;
            END IF;
            RETURN false;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION {FORBIDDEN_FUNCTION}(jsonb) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FORBIDDEN_FUNCTION}(jsonb) TO {EVALUATOR}, {READER}")


def _create_access_functions() -> None:
    op.execute(f"""CREATE FUNCTION {LOCK_FUNCTION}(
        target_workspace uuid,
        target_normalized_dataset_id uuid
    ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        manifest_payload jsonb;
        source_key text;
    BEGIN
        IF session_user <> '{EVALUATOR}' THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_CALLER_DENIED';
        END IF;
        IF target_workspace IS DISTINCT FROM
            NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_WORKSPACE_DENIED';
        END IF;
        SELECT manifest.manifest INTO manifest_payload
          FROM public.learning_normalized_dataset AS dataset
          JOIN public.dataset_manifest AS manifest
            ON manifest.workspace_id = dataset.workspace_id
           AND manifest.manifest_hash = dataset.manifest_hash
         WHERE dataset.workspace_id = target_workspace
           AND dataset.id = target_normalized_dataset_id;
        IF NOT FOUND OR pg_catalog.jsonb_typeof(
            manifest_payload->'samples'
        ) IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_DATASET_NOT_FOUND';
        END IF;
        FOR source_key IN
            SELECT DISTINCT reference.value->>'source_id'
              FROM pg_catalog.jsonb_array_elements(manifest_payload->'samples') AS sample(value)
              CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                  COALESCE(sample.value->'source_references', '[]'::jsonb)
              ) AS reference(value)
             WHERE COALESCE((sample.value->>'source_dependent')::boolean, false)
            UNION
            SELECT 'BINANCE_USDM_REST'
             ORDER BY 1
        LOOP
            IF source_key IS NULL OR source_key !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,127}}$' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_SOURCE_LINEAGE_INVALID';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'research-source:' || target_workspace::text || ':' || source_key, 0));
        END LOOP;
    END $$""")
    op.execute(f"ALTER FUNCTION {LOCK_FUNCTION}(uuid, uuid) OWNER TO {READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {LOCK_FUNCTION}(uuid, uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {LOCK_FUNCTION}(uuid, uuid) TO {EVALUATOR}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {RIGHTS_GATE} TO {READER}")

    op.execute(f"""CREATE FUNCTION {OPEN_FUNCTION}(
        target_workspace uuid,
        target_evaluation_run_id uuid,
        target_attempt_id uuid,
        target_normalized_dataset_id uuid,
        target_proof_id uuid,
        target_scorer_artifact_hash text,
        target_policy_hash text,
        target_request_fingerprint text,
        target_training_scope_digest text
    ) RETURNS TABLE(
        receipt_id uuid,
        workspace_id uuid,
        receipt_hash text,
        request_fingerprint text,
        scorer_artifact_hash text,
        policy_hash text,
        proof_id uuid,
        proof_hash text,
        current_rights_receipt_hash text,
        committed_at timestamptz
    ) LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        proof public.learning_scorer_test_opportunity_proof%ROWTYPE;
        receipt public.learning_scorer_test_access_receipt%ROWTYPE;
        dataset_manifest jsonb;
        dataset_canonical text;
        source_rights jsonb;
        expected_membership text;
        actual_membership text;
        expected_count integer;
        actual_count integer;
        valid_count integer;
        attempt_dataset uuid;
        attempt_manifest text;
        attempt_fold_plan text;
        attempt_fold text;
        attempt_approval text;
        attempt_policy text;
        scorer_base_id uuid;
        scorer_dataset uuid;
        scorer_manifest text;
        scorer_fold_plan text;
        scorer_fold text;
        scorer_approval text;
        scorer_base_hash text;
        scorer_version text;
        scorer_state text;
        scorer_vector_version text;
        scorer_vector_hash text;
        base_hash text;
        rights_hash text;
        new_receipt_id uuid;
    BEGIN
        IF session_user <> '{EVALUATOR}' THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_CALLER_DENIED';
        END IF;
        IF target_workspace IS DISTINCT FROM
            NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_WORKSPACE_DENIED';
        END IF;
        IF target_scorer_artifact_hash IS NULL
           OR target_scorer_artifact_hash !~ '^[0-9a-f]{{64}}$'
           OR target_policy_hash IS NULL
           OR target_policy_hash !~ '^[0-9a-f]{{64}}$'
           OR target_request_fingerprint IS NULL
           OR target_request_fingerprint !~ '^[0-9a-f]{{64}}$'
           OR target_training_scope_digest IS NULL
           OR target_training_scope_digest !~ '^[0-9a-f]{{64}}$' THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_HASH_INVALID';
        END IF;
        PERFORM public.wp06_lock_scorer_test_sources(
            target_workspace, target_normalized_dataset_id);
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
            'wp06-scorer-test-access:' || target_workspace::text || ':' ||
            target_evaluation_run_id::text, 0));

        SELECT * INTO proof
          FROM public.learning_scorer_test_opportunity_proof AS p
         WHERE p.workspace_id = target_workspace
           AND p.id = target_proof_id
           AND p.evaluation_run_id = target_evaluation_run_id
           AND p.attempt_id = target_attempt_id
           AND p.normalized_dataset_id = target_normalized_dataset_id;
        IF NOT FOUND OR pg_catalog.encode(pg_catalog.sha256(
                pg_catalog.convert_to(proof.canonical_proof, 'UTF8')), 'hex')
                IS DISTINCT FROM proof.proof_hash THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_NOT_AUTHORITATIVE';
        END IF;

        SELECT attempt.normalized_dataset_id, attempt.manifest_hash,
               attempt.fold_plan_hash, attempt.fold_id, attempt.approval_hash,
               attempt.split_policy_version
          INTO attempt_dataset, attempt_manifest, attempt_fold_plan,
               attempt_fold, attempt_approval, attempt_policy
          FROM public.learning_training_attempt AS attempt
         WHERE attempt.workspace_id = target_workspace AND attempt.id = target_attempt_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_ATTEMPT_NOT_FOUND';
        END IF;

        SELECT scorer.base_artifact_id, scorer.normalized_dataset_id, scorer.manifest_hash,
               scorer.fold_plan_hash, scorer.fold_id, scorer.approval_hash,
               scorer.base_artifact_hash, scorer.schema_version, scorer.state,
               scorer.vector_schema_version, scorer.vector_schema_hash
          INTO scorer_base_id, scorer_dataset, scorer_manifest, scorer_fold_plan,
               scorer_fold, scorer_approval, scorer_base_hash, scorer_version, scorer_state,
               scorer_vector_version, scorer_vector_hash
          FROM public.learning_scorer_artifact AS scorer
         WHERE scorer.workspace_id = target_workspace
           AND scorer.attempt_id = target_attempt_id
           AND scorer.artifact_hash = target_scorer_artifact_hash;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_ARTIFACT_NOT_FOUND';
        END IF;
        SELECT base.artifact_hash INTO base_hash
          FROM public.learning_training_artifact AS base
         WHERE base.workspace_id = target_workspace
           AND base.id = scorer_base_id
           AND base.attempt_id = target_attempt_id;
        IF NOT FOUND OR base_hash IS DISTINCT FROM scorer_base_hash THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_BASE_ARTIFACT_MISMATCH';
        END IF;

        IF scorer_state IS DISTINCT FROM 'ready_for_evaluation'
           OR scorer_version IS DISTINCT FROM 'crypidian.learning.scorer-temperature.v2'
           OR scorer_vector_version IS NULL
           OR (proof.workspace_id, proof.evaluation_run_id, proof.attempt_id,
               proof.normalized_dataset_id, proof.manifest_hash, proof.fold_plan_hash,
               proof.fold_id, proof.approval_hash, proof.vector_schema_version,
               proof.vector_schema_hash)
              IS DISTINCT FROM
              (target_workspace, target_evaluation_run_id, target_attempt_id,
               target_normalized_dataset_id, attempt_manifest, attempt_fold_plan,
               attempt_fold, attempt_approval, scorer_vector_version, scorer_vector_hash)
           OR (attempt_dataset, attempt_manifest, attempt_fold_plan, attempt_fold,
               attempt_approval)
              IS DISTINCT FROM
              (scorer_dataset, scorer_manifest, scorer_fold_plan, scorer_fold,
               scorer_approval)
           OR (attempt_dataset, attempt_manifest, attempt_fold_plan, attempt_fold,
               attempt_approval)
              IS DISTINCT FROM
              (proof.normalized_dataset_id, proof.manifest_hash, proof.fold_plan_hash,
               proof.fold_id, proof.approval_hash) THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_SCOPE_MISMATCH';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.learning_normalized_dataset AS dataset
             WHERE dataset.workspace_id = target_workspace
               AND dataset.id = target_normalized_dataset_id
               AND dataset.manifest_hash = proof.manifest_hash
               AND dataset.fold_plan_hash = proof.fold_plan_hash
               AND dataset.approval_hash = proof.approval_hash
               AND dataset.split_policy_version = attempt_policy
        ) OR NOT EXISTS (
            SELECT 1 FROM public.learning_split_approval AS approval
             WHERE approval.workspace_id = target_workspace
               AND approval.manifest_hash = proof.manifest_hash
               AND approval.fold_plan_hash = proof.fold_plan_hash
               AND approval.approval_hash = proof.approval_hash
               AND approval.split_policy_version = attempt_policy
        ) THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_APPROVAL_MISMATCH';
        END IF;

        SELECT manifest.manifest, manifest.canonical_manifest
          INTO dataset_manifest, dataset_canonical
          FROM public.dataset_manifest AS manifest
         WHERE manifest.workspace_id = target_workspace
           AND manifest.manifest_hash = proof.manifest_hash;
        IF NOT FOUND
           OR dataset_manifest->>'workspace_id' IS DISTINCT FROM target_workspace::text
           OR dataset_canonical::jsonb IS DISTINCT FROM dataset_manifest THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_MANIFEST_NOT_FOUND';
        END IF;

        SELECT count(*)::integer,
               count(outcome.id)::integer,
               pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                   public.wp06_scorer_test_proof_canonical_json(
                       COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                           'sample_id', sample.sample_id,
                           'sample_hash', sample.sample_hash,
                           'vector_hash', outcome.entry_vector_hash
                       ) ORDER BY sample.sample_id COLLATE "C"), '[]'::jsonb)
                   ), 'UTF8')), 'hex')
          INTO expected_count, valid_count, actual_membership
          FROM public.learning_fold_assignment AS assignment
          JOIN public.learning_normalized_sample AS sample
            ON sample.workspace_id = assignment.workspace_id
           AND sample.manifest_hash = assignment.manifest_hash
           AND sample.fold_plan_hash = assignment.fold_plan_hash
           AND sample.sample_id = assignment.sample_id
          LEFT JOIN public.backtest_trade_outcome AS outcome
            ON outcome.workspace_id = sample.workspace_id
           AND outcome.id::text = sample.outcome_id
           AND outcome.outcome_hash = sample.outcome_hash
         WHERE assignment.workspace_id = target_workspace
           AND assignment.manifest_hash = proof.manifest_hash
           AND assignment.fold_plan_hash = proof.fold_plan_hash
           AND assignment.fold_id = proof.fold_id
           AND assignment.role = 'test';
        IF expected_count = 0 OR valid_count <> expected_count
           OR actual_membership IS DISTINCT FROM proof.test_membership_digest THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_MEMBERSHIP_MISMATCH';
        END IF;

        SELECT access.* INTO receipt
          FROM public.learning_scorer_test_access_receipt AS access
         WHERE access.workspace_id = target_workspace
           AND access.evaluation_run_id = target_evaluation_run_id;
        IF FOUND THEN
            IF receipt.request_fingerprint IS DISTINCT FROM target_request_fingerprint
               OR receipt.attempt_id IS DISTINCT FROM target_attempt_id
               OR receipt.normalized_dataset_id IS DISTINCT FROM target_normalized_dataset_id
               OR receipt.proof_id IS DISTINCT FROM target_proof_id
               OR receipt.scorer_artifact_hash IS DISTINCT FROM target_scorer_artifact_hash
               OR receipt.policy_hash IS DISTINCT FROM target_policy_hash
               OR receipt.manifest_hash IS DISTINCT FROM proof.manifest_hash
               OR receipt.fold_plan_hash IS DISTINCT FROM proof.fold_plan_hash
               OR receipt.fold_id IS DISTINCT FROM proof.fold_id
               OR receipt.approval_hash IS DISTINCT FROM proof.approval_hash
               OR receipt.bundle_hash IS DISTINCT FROM proof.bundle_hash
               OR receipt.vector_schema_version IS DISTINCT FROM proof.vector_schema_version
               OR receipt.vector_schema_hash IS DISTINCT FROM proof.vector_schema_hash
               OR receipt.test_membership_digest IS DISTINCT FROM proof.test_membership_digest
               OR receipt.source_scope_hash IS DISTINCT FROM proof.source_scope_hash
               OR receipt.proof_hash IS DISTINCT FROM proof.proof_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_RECEIPT_RETRY_MISMATCH';
            END IF;
            new_receipt_id := pg_catalog.gen_random_uuid();
            PERFORM public.wp06_assert_training_source_rights(
                target_workspace, proof.manifest_hash, dataset_manifest,
                target_normalized_dataset_id, proof.fold_plan_hash, 'evaluation',
                new_receipt_id);
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                       'item_id', rights.item_id,
                       'policy_id', rights.policy_id,
                       'policy_sequence', rights.policy_sequence,
                       'purpose', rights.purpose,
                       'rights_hash', rights.rights_hash,
                       'revision_id', rights.revision_id,
                       'sample_id', rights.sample_id,
                       'source_id', rights.source_id
                   ) ORDER BY rights.sample_id COLLATE "C", rights.source_id COLLATE "C",
                              rights.item_id COLLATE "C", rights.revision_id COLLATE "C",
                              rights.policy_id COLLATE "C"), '[]'::jsonb)
              INTO source_rights
              FROM public.learning_run_source_rights_receipt AS rights
             WHERE rights.workspace_id = target_workspace
               AND rights.access_kind = 'evaluation'
               AND rights.access_id = new_receipt_id
               AND rights.normalized_dataset_id = target_normalized_dataset_id
               AND rights.manifest_hash = proof.manifest_hash
               AND rights.fold_plan_hash = proof.fold_plan_hash;
            rights_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                pg_catalog.jsonb_build_object(
                    'training_scope_digest', target_training_scope_digest,
                    'manifest_hash', proof.manifest_hash,
                    'fold_plan_hash', proof.fold_plan_hash,
                    'normalized_dataset_id', target_normalized_dataset_id,
                    'source_rights', source_rights
                )::text, 'UTF8')), 'hex');
            IF rights_hash IS DISTINCT FROM receipt.current_rights_receipt_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_RIGHTS_CHANGED';
            END IF;
            RETURN QUERY SELECT receipt.id, receipt.workspace_id, receipt.receipt_hash,
                                receipt.request_fingerprint, receipt.scorer_artifact_hash,
                                receipt.policy_hash, receipt.proof_id, receipt.proof_hash,
                                receipt.current_rights_receipt_hash, receipt.created_at;
            RETURN;
        END IF;

        new_receipt_id := pg_catalog.gen_random_uuid();
        PERFORM public.wp06_assert_training_source_rights(
            target_workspace, proof.manifest_hash, dataset_manifest,
            target_normalized_dataset_id, proof.fold_plan_hash, 'evaluation', new_receipt_id);
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                   'item_id', rights.item_id,
                   'policy_id', rights.policy_id,
                   'policy_sequence', rights.policy_sequence,
                   'purpose', rights.purpose,
                   'rights_hash', rights.rights_hash,
                   'revision_id', rights.revision_id,
                   'sample_id', rights.sample_id,
                   'source_id', rights.source_id
               ) ORDER BY rights.sample_id COLLATE "C", rights.source_id COLLATE "C",
                          rights.item_id COLLATE "C", rights.revision_id COLLATE "C",
                          rights.policy_id COLLATE "C"), '[]'::jsonb)
          INTO source_rights
          FROM public.learning_run_source_rights_receipt AS rights
         WHERE rights.workspace_id = target_workspace
           AND rights.access_kind = 'evaluation'
           AND rights.access_id = new_receipt_id
           AND rights.normalized_dataset_id = target_normalized_dataset_id
           AND rights.manifest_hash = proof.manifest_hash
           AND rights.fold_plan_hash = proof.fold_plan_hash;
        rights_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            pg_catalog.jsonb_build_object(
                'training_scope_digest', target_training_scope_digest,
                'manifest_hash', proof.manifest_hash,
                'fold_plan_hash', proof.fold_plan_hash,
                'normalized_dataset_id', target_normalized_dataset_id,
                'source_rights', source_rights
            )::text, 'UTF8')), 'hex');
        INSERT INTO public.learning_scorer_test_access_receipt(
            id, workspace_id, evaluation_run_id, attempt_id, normalized_dataset_id,
            proof_id, manifest_hash, fold_plan_hash, fold_id, approval_hash, bundle_hash,
            vector_schema_version, vector_schema_hash, test_membership_digest,
            source_scope_hash, proof_hash, scorer_artifact_hash, policy_hash,
            current_rights_receipt_hash, request_fingerprint, receipt_hash,
            purpose, actor_principal
        ) VALUES (
            new_receipt_id, target_workspace, target_evaluation_run_id, target_attempt_id,
            target_normalized_dataset_id, proof.id, proof.manifest_hash, proof.fold_plan_hash,
            proof.fold_id, proof.approval_hash, proof.bundle_hash, proof.vector_schema_version,
            proof.vector_schema_hash, proof.test_membership_digest, proof.source_scope_hash,
            proof.proof_hash, target_scorer_artifact_hash, target_policy_hash, rights_hash,
            target_request_fingerprint, repeat('0', 64), 'scorer-test', '{EVALUATOR}')
        RETURNING * INTO receipt;
        RETURN QUERY SELECT receipt.id, receipt.workspace_id, receipt.receipt_hash,
                            receipt.request_fingerprint, receipt.scorer_artifact_hash,
                            receipt.policy_hash, receipt.proof_id, receipt.proof_hash,
                            receipt.current_rights_receipt_hash, receipt.created_at;
    END $$""")
    op.execute(f"ALTER FUNCTION {OPEN_SIGNATURE} OWNER TO {READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {OPEN_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {OPEN_SIGNATURE} TO {EVALUATOR}")

    op.execute(f"""CREATE FUNCTION {CONSUME_FUNCTION}(
        target_workspace uuid,
        target_receipt_id uuid,
        target_training_scope_digest text
    ) RETURNS TABLE(
        consumption_id uuid,
        receipt_id uuid,
        workspace_id uuid,
        request_fingerprint text,
        payload_count integer,
        membership_digest text,
        payload_commitment_hash text,
        consumption_hash text,
        current_rights_receipt_hash text,
        consumed_at timestamptz,
        sample_id text,
        sample_outcome_id uuid,
        sample_outcome_hash text,
        sample_hash text,
        feature_snapshot_hash text,
        feature_schema_hash text,
        label_hash text,
        features jsonb,
        label_value integer,
        outcome_id uuid,
        outcome_hash text,
        outcome_payload jsonb,
        outcome_payload_canonical text,
        trade_id text,
        vector_hash text,
        candidate_id_hash text
    ) LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        receipt public.learning_scorer_test_access_receipt%ROWTYPE;
        proof public.learning_scorer_test_opportunity_proof%ROWTYPE;
        manifest_payload jsonb;
        source_rights jsonb;
        member_payload jsonb;
        commitment_payload jsonb;
        member_hash text;
        commitment_hash text;
        rights_hash text;
        count_rows integer;
        assignment_count integer;
        consumption public.learning_scorer_test_payload_consumption%ROWTYPE;
        new_consumption_id uuid;
        consumed_at_value timestamptz;
    BEGIN
        IF session_user <> '{EVALUATOR}' THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_CALLER_DENIED';
        END IF;
        IF target_workspace IS DISTINCT FROM
            NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_WORKSPACE_DENIED';
        END IF;
        IF target_training_scope_digest IS NULL
           OR target_training_scope_digest !~ '^[0-9a-f]{{64}}$' THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_RIGHTS_DIGEST_INVALID';
        END IF;
        SELECT access.* INTO receipt
          FROM public.learning_scorer_test_access_receipt AS access
         WHERE access.workspace_id = target_workspace
           AND access.id = target_receipt_id
           AND access.actor_principal = '{EVALUATOR}'
           AND access.purpose = 'scorer-test';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_RECEIPT_NOT_FOUND';
        END IF;
        PERFORM public.wp06_lock_scorer_test_sources(
            target_workspace, receipt.normalized_dataset_id);

        SELECT p.* INTO proof
          FROM public.learning_scorer_test_opportunity_proof AS p
         WHERE p.workspace_id = target_workspace AND p.id = receipt.proof_id
           AND p.evaluation_run_id = receipt.evaluation_run_id
           AND p.attempt_id = receipt.attempt_id
           AND p.normalized_dataset_id = receipt.normalized_dataset_id;
        IF NOT FOUND OR proof.proof_hash IS DISTINCT FROM receipt.proof_hash
           OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
               proof.canonical_proof, 'UTF8')), 'hex') IS DISTINCT FROM proof.proof_hash
           OR proof.test_membership_digest IS DISTINCT FROM receipt.test_membership_digest THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_PROOF_RECEIPT_MISMATCH';
        END IF;
        SELECT manifest.manifest INTO manifest_payload
          FROM public.dataset_manifest AS manifest
         WHERE manifest.workspace_id = target_workspace
           AND manifest.manifest_hash = receipt.manifest_hash;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_MANIFEST_NOT_FOUND';
        END IF;

        IF EXISTS (
            SELECT 1 FROM public.learning_scorer_test_payload_consumption AS prior
             WHERE prior.workspace_id = target_workspace AND prior.receipt_id = receipt.id
        ) THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_PAYLOAD_ALREADY_CONSUMED';
        END IF;
        new_consumption_id := pg_catalog.gen_random_uuid();
        PERFORM public.wp06_assert_training_source_rights(
            target_workspace, receipt.manifest_hash, manifest_payload,
            receipt.normalized_dataset_id, receipt.fold_plan_hash, 'evaluation',
            new_consumption_id);
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                   'item_id', rights.item_id,
                   'policy_id', rights.policy_id,
                   'policy_sequence', rights.policy_sequence,
                   'purpose', rights.purpose,
                   'rights_hash', rights.rights_hash,
                   'revision_id', rights.revision_id,
                   'sample_id', rights.sample_id,
                   'source_id', rights.source_id
               ) ORDER BY rights.sample_id COLLATE "C", rights.source_id COLLATE "C",
                          rights.item_id COLLATE "C", rights.revision_id COLLATE "C",
                          rights.policy_id COLLATE "C"), '[]'::jsonb)
          INTO source_rights
          FROM public.learning_run_source_rights_receipt AS rights
         WHERE rights.workspace_id = target_workspace
           AND rights.access_kind = 'evaluation'
           AND rights.access_id = new_consumption_id
           AND rights.normalized_dataset_id = receipt.normalized_dataset_id
           AND rights.manifest_hash = receipt.manifest_hash
           AND rights.fold_plan_hash = receipt.fold_plan_hash;
        rights_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            pg_catalog.jsonb_build_object(
                'training_scope_digest', target_training_scope_digest,
                'manifest_hash', receipt.manifest_hash,
                'fold_plan_hash', receipt.fold_plan_hash,
                'normalized_dataset_id', receipt.normalized_dataset_id,
                'source_rights', source_rights
            )::text, 'UTF8')), 'hex');
        IF rights_hash IS DISTINCT FROM receipt.current_rights_receipt_hash THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_RIGHTS_CHANGED';
        END IF;

        SELECT count(*)::integer,
               count(DISTINCT assignment.sample_id)::integer,
               COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                   'sample_id', sample.sample_id,
                   'sample_hash', sample.sample_hash,
                   'vector_hash', outcome.entry_vector_hash
               ) ORDER BY sample.sample_id COLLATE "C"), '[]'::jsonb),
               COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                   'candidate_id_hash', outcome.entry_candidate_id,
                   'outcome_hash', outcome.outcome_hash,
                   'outcome_id', outcome.id::text,
                   'sample_hash', sample.sample_hash,
                   'sample_id', sample.sample_id,
                   'vector_hash', outcome.entry_vector_hash
               ) ORDER BY sample.sample_id COLLATE "C"), '[]'::jsonb)
          INTO count_rows, assignment_count, member_payload, commitment_payload
          FROM public.learning_fold_assignment AS assignment
          JOIN public.learning_normalized_sample AS sample
            ON sample.workspace_id = assignment.workspace_id
           AND sample.manifest_hash = assignment.manifest_hash
           AND sample.fold_plan_hash = assignment.fold_plan_hash
           AND sample.sample_id = assignment.sample_id
          JOIN public.backtest_trade_outcome AS outcome
            ON outcome.workspace_id = sample.workspace_id
           AND outcome.id::text = sample.outcome_id
           AND outcome.outcome_hash = sample.outcome_hash
          JOIN LATERAL pg_catalog.jsonb_array_elements(proof.canonical_proof::jsonb->'entries')
               AS entry(value) ON entry.value->>'status' = 'candidate'
              AND entry.value->>'candidate_id_hash' = outcome.entry_candidate_id
              AND entry.value->>'outcome_id_hash' = pg_catalog.encode(pg_catalog.sha256(
                  pg_catalog.convert_to(outcome.id::text, 'UTF8')), 'hex')
              AND entry.value->>'outcome_hash' = outcome.outcome_hash
              AND entry.value->>'sample_id_hash' = pg_catalog.encode(pg_catalog.sha256(
                  pg_catalog.convert_to(sample.sample_id, 'UTF8')), 'hex')
              AND entry.value->>'sample_hash' = sample.sample_hash
              AND entry.value->>'vector_hash' = outcome.entry_vector_hash
         WHERE assignment.workspace_id = target_workspace
           AND assignment.manifest_hash = receipt.manifest_hash
           AND assignment.fold_plan_hash = receipt.fold_plan_hash
           AND assignment.fold_id = receipt.fold_id
           AND assignment.role = 'test'
           AND outcome.entry_candidate_id IS NOT NULL
           AND outcome.entry_vector_hash IS NOT NULL
           AND outcome.entry_decision_snapshot_id IS NOT NULL
           AND outcome.entry_features_hash = sample.feature_snapshot_hash
           AND outcome.entry_feature_schema_hash = sample.feature_schema_hash
           AND outcome.canonical_json::jsonb = outcome.outcome
           AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
               outcome.canonical_json, 'UTF8')), 'hex') = outcome.outcome_hash;
        IF count_rows = 0 OR count_rows <> assignment_count THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_PAYLOAD_MEMBERSHIP_MISMATCH';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.learning_fold_assignment AS assignment
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = receipt.manifest_hash
               AND assignment.fold_plan_hash = receipt.fold_plan_hash
               AND assignment.fold_id = receipt.fold_id
               AND assignment.role = 'test'
               AND NOT EXISTS (
                   SELECT 1 FROM public.learning_normalized_sample AS sample
                   JOIN public.backtest_trade_outcome AS outcome
                     ON outcome.workspace_id = sample.workspace_id
                    AND outcome.id::text = sample.outcome_id
                    AND outcome.outcome_hash = sample.outcome_hash
                   JOIN LATERAL pg_catalog.jsonb_array_elements(
                       proof.canonical_proof::jsonb->'entries') AS entry(value)
                     ON entry.value->>'status' = 'candidate'
                    AND entry.value->>'candidate_id_hash' = outcome.entry_candidate_id
                    AND entry.value->>'outcome_id_hash' = pg_catalog.encode(
                        pg_catalog.sha256(pg_catalog.convert_to(outcome.id::text, 'UTF8')), 'hex')
                    AND entry.value->>'outcome_hash' = outcome.outcome_hash
                    AND entry.value->>'sample_id_hash' = pg_catalog.encode(
                        pg_catalog.sha256(pg_catalog.convert_to(sample.sample_id, 'UTF8')), 'hex')
                    AND entry.value->>'sample_hash' = sample.sample_hash
                    AND entry.value->>'vector_hash' = outcome.entry_vector_hash
                    WHERE sample.workspace_id = assignment.workspace_id
                      AND sample.manifest_hash = assignment.manifest_hash
                      AND sample.fold_plan_hash = assignment.fold_plan_hash
                      AND sample.sample_id = assignment.sample_id
                      AND outcome.entry_features_hash = sample.feature_snapshot_hash
                      AND outcome.entry_feature_schema_hash = sample.feature_schema_hash
                      AND outcome.canonical_json::jsonb = outcome.outcome
                      AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                          outcome.canonical_json, 'UTF8')), 'hex') = outcome.outcome_hash
               )
        ) THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_PAYLOAD_MEMBERSHIP_MISMATCH';
        END IF;

        member_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            public.wp06_scorer_test_proof_canonical_json(member_payload), 'UTF8')), 'hex');
        commitment_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            public.wp06_scorer_test_proof_canonical_json(commitment_payload), 'UTF8')), 'hex');
        IF member_hash IS DISTINCT FROM receipt.test_membership_digest THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_PAYLOAD_MEMBERSHIP_MISMATCH';
        END IF;
        consumed_at_value := pg_catalog.clock_timestamp();
        INSERT INTO public.learning_scorer_test_payload_consumption(
            id, workspace_id, receipt_id, payload_count, membership_digest,
            payload_commitment_hash, consumption_hash, current_rights_receipt_hash,
            request_fingerprint, consumed_at
        ) VALUES (
            new_consumption_id, target_workspace, receipt.id, count_rows, member_hash,
            commitment_hash, repeat('0', 64), rights_hash, receipt.request_fingerprint,
            consumed_at_value
        ) RETURNING * INTO consumption;

        RETURN QUERY
            SELECT consumption.id, consumption.receipt_id, consumption.workspace_id,
                   consumption.request_fingerprint, consumption.payload_count,
                   consumption.membership_digest, consumption.payload_commitment_hash,
                   consumption.consumption_hash, consumption.current_rights_receipt_hash,
                   consumption.consumed_at, sample.sample_id,
                   sample.outcome_id::uuid, sample.outcome_hash, sample.sample_hash,
                   sample.feature_snapshot_hash, sample.feature_schema_hash, sample.label_hash,
                   sample.features, sample.label_value, outcome.id, outcome.outcome_hash,
                   outcome.outcome, outcome.canonical_json, outcome.trade_id,
                   outcome.entry_vector_hash, outcome.entry_candidate_id
              FROM public.learning_fold_assignment AS assignment
              JOIN public.learning_normalized_sample AS sample
                ON sample.workspace_id = assignment.workspace_id
               AND sample.manifest_hash = assignment.manifest_hash
               AND sample.fold_plan_hash = assignment.fold_plan_hash
               AND sample.sample_id = assignment.sample_id
              JOIN public.backtest_trade_outcome AS outcome
                ON outcome.workspace_id = sample.workspace_id
               AND outcome.id::text = sample.outcome_id
               AND outcome.outcome_hash = sample.outcome_hash
              JOIN LATERAL pg_catalog.jsonb_array_elements(
                   proof.canonical_proof::jsonb->'entries') AS entry(value)
                ON entry.value->>'status' = 'candidate'
               AND entry.value->>'candidate_id_hash' = outcome.entry_candidate_id
               AND entry.value->>'outcome_id_hash' = pg_catalog.encode(pg_catalog.sha256(
                   pg_catalog.convert_to(outcome.id::text, 'UTF8')), 'hex')
               AND entry.value->>'outcome_hash' = outcome.outcome_hash
               AND entry.value->>'sample_id_hash' = pg_catalog.encode(pg_catalog.sha256(
                   pg_catalog.convert_to(sample.sample_id, 'UTF8')), 'hex')
               AND entry.value->>'sample_hash' = sample.sample_hash
               AND entry.value->>'vector_hash' = outcome.entry_vector_hash
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = receipt.manifest_hash
               AND assignment.fold_plan_hash = receipt.fold_plan_hash
               AND assignment.fold_id = receipt.fold_id
               AND assignment.role = 'test'
               AND outcome.entry_candidate_id IS NOT NULL
               AND outcome.entry_vector_hash IS NOT NULL
               AND outcome.entry_decision_snapshot_id IS NOT NULL
               AND outcome.entry_features_hash = sample.feature_snapshot_hash
               AND outcome.entry_feature_schema_hash = sample.feature_schema_hash
               AND outcome.canonical_json::jsonb = outcome.outcome
               AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                   outcome.canonical_json, 'UTF8')), 'hex') = outcome.outcome_hash
             ORDER BY sample.sample_id COLLATE "C";
    END $$""")
    op.execute(f"ALTER FUNCTION {CONSUME_FUNCTION}(uuid, uuid, text) OWNER TO {READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {CONSUME_FUNCTION}(uuid, uuid, text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {CONSUME_FUNCTION}(uuid, uuid, text) TO {EVALUATOR}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_scorer_test_proof_canonical_json(jsonb) TO {READER}"
    )


def _create_report_guard() -> None:
    op.execute("""CREATE FUNCTION public.wp06_validate_scorer_test_evaluation_report()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            access public.learning_scorer_test_access_receipt%ROWTYPE;
            consumption public.learning_scorer_test_payload_consumption%ROWTYPE;
            proof public.learning_scorer_test_opportunity_proof%ROWTYPE;
            scorer record;
            base record;
            payload jsonb;
            report_keys text[] := ARRAY[
                'status','workspace_id','evaluation_run_id','attempt_id',
                'normalized_dataset_id','manifest_hash','fold_plan_hash','fold_id',
                'approval_hash','scorer_artifact_hash','base_artifact_hash',
                'vector_schema_version','vector_schema_hash','bundle_hash',
                'test_membership_digest','source_scope_hash','opportunity_proof_id',
                'proof_hash','candidate_completeness_verified',
                'scored_candidate_coverage_verified','policy_version','policy_hash',
                'evaluator_version','evaluated_at','test_sample_count','class_support',
                'coverage','abstain_count','log_loss','brier_score',
                'prior_baseline_log_loss','identity_temperature_log_loss',
                'expected_calibration_error','reliability_bins','confusion_matrix',
                'opportunity_count','opportunity_candidate_count','no_candidate_count',
                'input_excluded_count','candidate_count','scored_candidate_count',
                'approved_candidate_count','vetoed_candidate_count','false_veto_count',
                'false_veto_rate','scored_candidate_retention','candidate_retention',
                'unfiltered_portfolio','filtered_portfolio','test_access_receipt_id',
                'test_access_receipt_hash','test_current_rights_receipt_hash',
                'test_payload_consumption_id','test_payload_commitment_hash',
                'test_payload_consumption_hash','test_payload_consumed_at'
            ];
        BEGIN
            IF session_user <> 'crypidian_learning_evaluator' THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_CALLER_DENIED';
            END IF;
            payload := NEW.canonical_report::jsonb;
            IF pg_catalog.jsonb_typeof(payload) IS DISTINCT FROM 'object'
               OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(payload)) <> 56
               OR NOT (payload ?& report_keys)
               OR payload <> NEW.report
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                   NEW.canonical_report, 'UTF8')), 'hex') IS DISTINCT FROM NEW.report_hash
               OR public.wp06_scorer_test_report_has_forbidden_payload(payload) THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_CANONICAL_OR_SANITIZED_INVALID';
            END IF;

            SELECT * INTO access
              FROM public.learning_scorer_test_access_receipt AS row
             WHERE row.workspace_id = NEW.workspace_id
               AND row.id = NEW.access_receipt_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_RECEIPT_NOT_FOUND';
            END IF;
            SELECT * INTO consumption
              FROM public.learning_scorer_test_payload_consumption AS row
             WHERE row.workspace_id = NEW.workspace_id
               AND row.receipt_id = NEW.access_receipt_id
               AND row.id = NEW.payload_consumption_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_CONSUMPTION_NOT_FOUND';
            END IF;
            SELECT * INTO proof
              FROM public.learning_scorer_test_opportunity_proof AS row
             WHERE row.workspace_id = NEW.workspace_id AND row.id = NEW.proof_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_PROOF_NOT_FOUND';
            END IF;
            IF pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                   proof.canonical_proof, 'UTF8')), 'hex') IS DISTINCT FROM proof.proof_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_PROOF_CANONICAL_INVALID';
            END IF;
            SELECT artifact_hash, base_artifact_id, base_artifact_hash,
                   normalized_dataset_id, manifest_hash, fold_plan_hash, fold_id,
                   approval_hash, vector_schema_version, vector_schema_hash, state
              INTO scorer
              FROM public.learning_scorer_artifact AS row
             WHERE row.workspace_id = NEW.workspace_id
               AND row.attempt_id = access.attempt_id
               AND row.artifact_hash = NEW.scorer_artifact_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_SCORER_NOT_FOUND';
            END IF;
            SELECT id, artifact_hash, normalized_dataset_id, manifest_hash,
                   fold_plan_hash, fold_id, approval_hash
              INTO base
              FROM public.learning_training_artifact AS row
             WHERE row.workspace_id = NEW.workspace_id
               AND row.attempt_id = access.attempt_id
               AND row.artifact_hash = NEW.base_artifact_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_BASE_NOT_FOUND';
            END IF;

            IF NEW.evaluation_run_id IS DISTINCT FROM access.evaluation_run_id
               OR NEW.proof_id IS DISTINCT FROM access.proof_id
               OR NEW.proof_id IS DISTINCT FROM proof.id
               OR proof.proof_hash IS DISTINCT FROM NEW.proof_hash
               OR proof.proof_hash IS DISTINCT FROM access.proof_hash
               OR NEW.request_fingerprint IS DISTINCT FROM access.request_fingerprint
               OR NEW.request_fingerprint IS DISTINCT FROM consumption.request_fingerprint
               OR NEW.access_receipt_hash IS DISTINCT FROM access.receipt_hash
               OR NEW.current_rights_receipt_hash IS DISTINCT FROM
                   consumption.current_rights_receipt_hash
               OR NEW.payload_commitment_hash IS DISTINCT FROM consumption.payload_commitment_hash
               OR NEW.payload_consumption_hash IS DISTINCT FROM consumption.consumption_hash
               OR NEW.payload_consumed_at IS DISTINCT FROM consumption.consumed_at
               OR NEW.test_sample_count IS DISTINCT FROM consumption.payload_count
               OR NEW.opportunity_count IS DISTINCT FROM proof.expected_opportunity_count
               OR NEW.scorer_artifact_hash IS DISTINCT FROM access.scorer_artifact_hash
               OR NEW.policy_hash IS DISTINCT FROM access.policy_hash
               OR NEW.base_artifact_hash IS DISTINCT FROM scorer.base_artifact_hash
               OR scorer.base_artifact_hash IS DISTINCT FROM base.artifact_hash
               OR scorer.base_artifact_id IS DISTINCT FROM base.id
               OR scorer.state IS DISTINCT FROM 'ready_for_evaluation'
               OR scorer.normalized_dataset_id IS DISTINCT FROM access.normalized_dataset_id
               OR scorer.manifest_hash IS DISTINCT FROM access.manifest_hash
               OR scorer.fold_plan_hash IS DISTINCT FROM access.fold_plan_hash
               OR scorer.fold_id IS DISTINCT FROM access.fold_id
               OR scorer.approval_hash IS DISTINCT FROM access.approval_hash
               OR base.normalized_dataset_id IS DISTINCT FROM access.normalized_dataset_id
               OR base.manifest_hash IS DISTINCT FROM access.manifest_hash
               OR base.fold_plan_hash IS DISTINCT FROM access.fold_plan_hash
               OR base.fold_id IS DISTINCT FROM access.fold_id
               OR base.approval_hash IS DISTINCT FROM access.approval_hash
               OR payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
               OR payload->>'evaluation_run_id' IS DISTINCT FROM NEW.evaluation_run_id::text
               OR payload->>'attempt_id' IS DISTINCT FROM access.attempt_id::text
               OR payload->>'normalized_dataset_id' IS DISTINCT FROM
                   access.normalized_dataset_id::text
               OR payload->>'manifest_hash' IS DISTINCT FROM access.manifest_hash
               OR payload->>'fold_plan_hash' IS DISTINCT FROM access.fold_plan_hash
               OR payload->>'fold_id' IS DISTINCT FROM access.fold_id
               OR payload->>'approval_hash' IS DISTINCT FROM access.approval_hash
               OR payload->>'bundle_hash' IS DISTINCT FROM access.bundle_hash
               OR payload->>'vector_schema_version' IS DISTINCT FROM
                   access.vector_schema_version
               OR payload->>'vector_schema_hash' IS DISTINCT FROM access.vector_schema_hash
               OR payload->>'test_membership_digest' IS DISTINCT FROM
                   access.test_membership_digest
               OR payload->>'source_scope_hash' IS DISTINCT FROM access.source_scope_hash
               OR payload->>'opportunity_proof_id' IS DISTINCT FROM access.proof_id::text
               OR payload->>'proof_hash' IS DISTINCT FROM access.proof_hash
               OR payload->>'scorer_artifact_hash' IS DISTINCT FROM access.scorer_artifact_hash
               OR payload->>'base_artifact_hash' IS DISTINCT FROM NEW.base_artifact_hash
               OR payload->>'policy_hash' IS DISTINCT FROM access.policy_hash
               OR payload->>'test_access_receipt_id' IS DISTINCT FROM access.id::text
               OR payload->>'test_access_receipt_hash' IS DISTINCT FROM access.receipt_hash
               OR payload->>'test_current_rights_receipt_hash' IS DISTINCT FROM
                   consumption.current_rights_receipt_hash
               OR payload->>'test_payload_consumption_id' IS DISTINCT FROM consumption.id::text
               OR payload->>'test_payload_commitment_hash' IS DISTINCT FROM
                   consumption.payload_commitment_hash
               OR payload->>'test_payload_consumption_hash' IS DISTINCT FROM
                   consumption.consumption_hash
               OR NULLIF(payload->>'test_payload_consumed_at', '')::timestamptz
                    IS DISTINCT FROM consumption.consumed_at
               OR payload->>'evaluator_version' IS DISTINCT FROM
                   'wp06-scorer-test-evaluator-v1'
               OR payload->>'policy_version' IS NULL
               OR pg_catalog.btrim(payload->>'policy_version') = ''
               OR payload->>'status' IS DISTINCT FROM NEW.evaluation_status
               OR (payload->>'test_sample_count')::integer IS DISTINCT FROM NEW.test_sample_count
               OR (payload->>'opportunity_count')::integer IS DISTINCT FROM NEW.opportunity_count
               OR (payload->>'candidate_completeness_verified')::boolean
                    IS DISTINCT FROM proof.completeness_verified
               OR NEW.candidate_completeness_verified IS DISTINCT FROM proof.completeness_verified
               OR (payload->>'scored_candidate_coverage_verified')::boolean
                    IS DISTINCT FROM NEW.scored_candidate_coverage_verified
               OR (payload->>'opportunity_candidate_count')::integer IS DISTINCT FROM
                    proof.candidate_count
               OR (payload->>'no_candidate_count')::integer IS DISTINCT FROM
                    proof.no_candidate_count
               OR (payload->>'input_excluded_count')::integer IS DISTINCT FROM
                    proof.input_excluded_count
               OR (payload->>'candidate_count')::integer IS DISTINCT FROM proof.candidate_count
               OR NEW.evaluation_status = 'ACCEPTED_PAPER_GATE_ELIGIBLE'
                    AND (NOT proof.completeness_verified
                         OR NOT NEW.scored_candidate_coverage_verified
                         OR (payload->>'scored_candidate_count')::integer
                            IS DISTINCT FROM proof.candidate_count) THEN
                RAISE EXCEPTION 'WP06_SCORER_TEST_REPORT_LINEAGE_MISMATCH';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(
        f"ALTER FUNCTION public.wp06_validate_scorer_test_evaluation_report() OWNER TO {READER}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_validate_scorer_test_evaluation_report() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER learning_scorer_test_evaluation_report_validate "
        "BEFORE INSERT ON public.learning_scorer_test_evaluation_report "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_validate_scorer_test_evaluation_report()"
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.learning_scorer_test_evaluation_report, "
        "public.learning_scorer_test_payload_consumption, "
        "public.learning_scorer_test_access_receipt IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("SET LOCAL row_security = off")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.learning_scorer_test_evaluation_report)
           OR EXISTS (SELECT 1 FROM public.learning_scorer_test_payload_consumption)
           OR EXISTS (SELECT 1 FROM public.learning_scorer_test_access_receipt) THEN
            RAISE EXCEPTION 'WP06_SCORER_TEST_DOWNGRADE_REQUIRES_EMPTY_TABLES';
        END IF;
    END $$""")

    op.execute(
        "DROP TRIGGER learning_scorer_test_evaluation_report_validate "
        "ON public.learning_scorer_test_evaluation_report"
    )
    op.execute(
        "DROP TRIGGER learning_scorer_test_consumption_hash "
        "ON public.learning_scorer_test_payload_consumption"
    )
    op.execute(
        "DROP TRIGGER learning_scorer_test_access_receipt_hash "
        "ON public.learning_scorer_test_access_receipt"
    )
    for table in (
        "learning_scorer_test_access_receipt",
        "learning_scorer_test_payload_consumption",
        "learning_scorer_test_evaluation_report",
    ):
        op.execute(f"DROP TRIGGER {table}_immutable ON public.{table}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {OPEN_SIGNATURE} FROM {EVALUATOR}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {CONSUME_FUNCTION}(uuid, uuid, text) FROM {EVALUATOR}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {LOCK_FUNCTION}(uuid, uuid) FROM {EVALUATOR}")
    op.execute(f"DROP FUNCTION {CONSUME_FUNCTION}(uuid, uuid, text)")
    op.execute(f"DROP FUNCTION {OPEN_SIGNATURE}")
    op.execute(f"DROP FUNCTION {LOCK_FUNCTION}(uuid, uuid)")
    op.execute("DROP FUNCTION public.wp06_validate_scorer_test_evaluation_report()")
    op.execute("DROP FUNCTION public.wp06_set_scorer_test_consumption_hash()")
    op.execute("DROP FUNCTION public.wp06_set_scorer_test_receipt_hash()")
    op.execute("DROP FUNCTION public.reject_wp06_scorer_test_mutation()")

    for table in (
        "learning_scorer_test_access_receipt",
        "learning_scorer_test_payload_consumption",
        "learning_scorer_test_evaluation_report",
    ):
        op.execute(f"DROP POLICY {table}_admin_select ON public.{table}")
        op.execute(f"DROP POLICY {table}_evaluator_select ON public.{table}")
        op.execute(f"DROP POLICY {table}_reader_insert ON public.{table}")
        op.execute(f"DROP POLICY {table}_reader_select ON public.{table}")
        if table == "learning_scorer_test_evaluation_report":
            op.execute(
                f"DROP POLICY learning_scorer_test_report_evaluator_insert ON public.{table}"
            )
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {EVALUATOR}, {ADMIN}, {READER}")

    op.drop_index(
        "ix_learning_scorer_test_report_workspace_created",
        table_name="learning_scorer_test_evaluation_report",
    )
    op.drop_index(
        "ix_learning_scorer_test_consumption_workspace_created",
        table_name="learning_scorer_test_payload_consumption",
    )
    op.drop_index(
        "ix_learning_scorer_test_access_workspace_created",
        table_name="learning_scorer_test_access_receipt",
    )
    op.drop_table("learning_scorer_test_evaluation_report")
    op.drop_table("learning_scorer_test_payload_consumption")
    op.drop_table("learning_scorer_test_access_receipt")
    op.execute(f"DROP FUNCTION {FORBIDDEN_FUNCTION}(jsonb)")

    op.execute(
        "DROP POLICY wp06_scorer_test_evaluation_reader_proof "
        "ON public.learning_scorer_test_opportunity_proof"
    )
    op.execute(
        f"REVOKE SELECT ON TABLE public.learning_scorer_test_opportunity_proof FROM {READER}"
    )
    op.execute(
        "DROP POLICY wp06_scorer_test_evaluation_reader_base_artifact "
        "ON public.learning_training_artifact"
    )
    op.execute(
        "REVOKE SELECT (workspace_id, id, attempt_id, normalized_dataset_id, artifact_hash, "
        "manifest_hash, fold_plan_hash, fold_id, approval_hash) "
        "ON TABLE public.learning_training_artifact FROM "
        f"{READER}"
    )
    op.execute(
        "DROP POLICY wp06_scorer_test_evaluation_reader_scorer ON public.learning_scorer_artifact"
    )
    op.execute(
        "REVOKE SELECT (workspace_id, attempt_id, base_artifact_id, normalized_dataset_id, "
        "manifest_hash, fold_plan_hash, fold_id, approval_hash, base_artifact_hash, "
        "schema_version, state, vector_schema_version, vector_schema_hash, artifact_hash) "
        "ON TABLE public.learning_scorer_artifact FROM "
        f"{READER}"
    )
    op.execute(
        "DROP POLICY wp06_scorer_test_evaluation_reader_attempt ON public.learning_training_attempt"
    )
    op.execute(
        "REVOKE SELECT (workspace_id, id, normalized_dataset_id, manifest_hash, "
        "fold_plan_hash, fold_id, approval_hash, split_policy_version) "
        "ON TABLE public.learning_training_attempt FROM "
        f"{READER}"
    )
    op.execute(
        "DROP POLICY wp06_scorer_test_source_rights_reader_select "
        "ON public.learning_run_source_rights_receipt"
    )
    op.execute(f"REVOKE SELECT ON TABLE public.learning_run_source_rights_receipt FROM {READER}")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_scorer_test_proof_canonical_json(jsonb) "
        f"FROM {READER}"
    )

    for table in (
        "backtest_dataset_replay_bundle",
        "backtest_dataset_manifest",
        "source_policy_revision",
        "source_policy_observation",
    ):
        op.execute(f"DROP POLICY wp06_scorer_test_evaluator_metadata_read ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {EVALUATOR}")
