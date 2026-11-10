"""WP06: tách quyền scorer guard và pointer trigger khỏi role quản trị."""

from collections.abc import Sequence

from alembic import op

revision: str = "d9f1c3a5b7e9"
down_revision: str | None = "d8f0a2c4e6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD_ROLE = "crypidian_scorer_deployment_guard"
POINTER_ROLE = "crypidian_scorer_deployment_pointer_writer"
REGISTRY_ROLE = "crypidian_learning_model_registry_guard"
SHADOW_GUARD_ROLE = "crypidian_learning_shadow_integrity_guard"
POINTER = "learning_scoring_deployment_pointer"
GUARD_FUNCTION = "public.wp06_scoring_deployment_guard()"
APPLY_FUNCTION = "public.wp06_scoring_deployment_apply()"
SEED_FUNCTION = "public.wp06_scoring_deployment_workspace_seed()"
PAYLOAD_FUNCTION = "public.wp06_scoring_deployment_payload_has_forbidden(jsonb)"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
GUARD_TABLES = (
    "learning_scoring_deployment_pointer",
    "learning_model_active_pointer",
    "learning_model_registry",
    "learning_scorer_artifact",
    "learning_scorer_test_evaluation_report",
    "learning_scorer_test_opportunity_proof",
)
GUARD_POLICY_TABLES = (
    *GUARD_TABLES,
    "user_membership",
    "role_binding",
    "learning_scoring_deployment_event",
)
GUARD_COLUMN_GRANTS = {
    "user_membership": "workspace_id, id, status",
    "role_binding": "workspace_id, membership_id, role",
    "learning_scoring_deployment_event": "workspace_id, id, event_hash",
}
GUARD_LOCK_COLUMNS = {
    "learning_scoring_deployment_pointer": "changed_at",
    "learning_model_active_pointer": "version",
    "learning_model_registry": "revision",
    "learning_scorer_artifact": "state",
    "learning_scorer_test_evaluation_report": "evaluation_status",
    "learning_scorer_test_opportunity_proof": "completeness_verified",
}
REGISTRY_READ_TABLES = (
    "learning_model_registry",
    "learning_model_registry_event",
    "learning_model_active_pointer",
    "learning_evaluation_report",
)
REGISTRY_COLUMN_GRANTS = {
    "learning_training_artifact": (
        "workspace_id, id, artifact_hash, manifest_hash, fold_plan_hash, approval_hash, artifact"
    ),
    "user_membership": "workspace_id, id, status",
}
REGISTRY_UPDATE_COLUMNS = "state, revision, evaluation_report_id, evaluation_report_hash"
REGISTRY_GUARD_FUNCTIONS = (
    "public.wp06_learning_model_registry_entry_guard()",
    "public.wp06_learning_model_registry_event_guard()",
    "public.wp06_learning_model_registry_event_apply()",
    "public.wp06_learning_model_registry_initial_event_check()",
    "public.wp06_learning_model_pointer_guard()",
)
SHADOW_GUARD_FUNCTIONS = (
    "public.wp06_lock_learning_model_active_pointer()",
    "public.wp06_require_learning_shadow_lineage()",
    "public.wp06_shadow_observation_idempotency_key(public.learning_shadow_observation)",
    "public.wp06_shadow_observation_canonical_document(public.learning_shadow_observation)",
    "public.wp06_shadow_observation_content_hash(public.learning_shadow_observation)",
    "public.wp06_drift_assessment_idempotency_key(public.learning_drift_assessment)",
    "public.wp06_retrain_proposal_idempotency_key(public.learning_retrain_proposal)",
    "public.wp06_shadow_current_paper_rights_hash(uuid, text, text, integer, timestamptz)",
    "public.wp06_drift_assessment_content_hash(public.learning_drift_assessment, json)",
    "public.wp06_retrain_proposal_content_hash(public.learning_retrain_proposal)",
    "public.wp06_lock_learning_drift_assessment_cohort()",
    "public.wp06_validate_learning_drift_assessment_membership()",
    "public.wp06_require_learning_drift_member_lineage()",
    "public.wp06_require_learning_retrain_proposal_lineage()",
    "public.wp06_learning_registry_approval_event_hash(uuid, uuid, uuid)",
    "public.wp06_shadow_current_paper_rights_receipt(uuid)",
    "public.wp06_shadow_decision_source_rights_supported(uuid, uuid)",
    "public.wp06_lock_learning_shadow_cohort(uuid, uuid, integer)",
)
SHADOW_HELPER_FUNCTIONS = (
    "public.wp06_canonical_source_cost(numeric)",
    "public.wp06_shadow_market_source_rights_matches(text, text)",
    "public.wp06_canonical_float_json(double precision)",
    "public.wp06_canonical_utc_timestamp_json(timestamptz)",
    "public.wp06_sha256_canonical_json(json)",
    "public.wp06_compact_canonical_json(json)",
)
SHADOW_RIGHTS_TABLES = ("source_policy_revision", "source_policy_observation")
SHADOW_DATA_TABLES = (
    "learning_shadow_observation",
    "learning_drift_assessment",
    "learning_drift_assessment_member",
    "learning_retrain_proposal",
)
SHADOW_SOURCE_COLUMNS = {
    "bot_decision": (
        "workspace_id, id, evidence_id, evidence_hash, strategy_release_id, "
        "strategy_release_hash, evaluated_at, status, decision_hash"
    ),
    "shared_signal_evidence": (
        "workspace_id, id, feature_snapshot_id, available_at, evidence_hash, schema_version, "
        "source_refs"
    ),
    "feature_snapshot": (
        "workspace_id, snapshot_id, content_hash, symbol, timeframe, schema_hash, as_of, "
        "computed_at, quality_flags, data_freshness, data_coverage, is_usable, input_event_ids"
    ),
    "learning_model_registry": (
        "workspace_id, id, artifact_id, artifact_hash, manifest_hash, fold_plan_hash, "
        "approval_hash, state, revision"
    ),
    "learning_model_active_pointer": "workspace_id, registry_id, version, changed_at",
    "learning_training_artifact": (
        "workspace_id, id, artifact_hash, manifest_hash, fold_plan_hash, approval_hash, "
        "feature_schema_hash"
    ),
}
SHADOW_LOCK_COLUMNS = {
    "learning_model_registry": "revision",
    "learning_model_active_pointer": "version",
}
SHADOW_FULL_READ_TABLES = (
    *SHADOW_DATA_TABLES,
    *SHADOW_RIGHTS_TABLES,
    "learning_model_registry_event",
)
POINTER_UPDATE_COLUMNS = "event_id, revision, active_pointer_version, mode, changed_at"


def upgrade() -> None:
    # Role owner legacy chưa khai báo NOINHERIT trong migration ban đầu.
    op.execute("ALTER ROLE crypidian_bot_provisioner NOINHERIT")
    for role in (GUARD_ROLE, POINTER_ROLE, REGISTRY_ROLE, SHADOW_GUARD_ROLE):
        op.execute(
            f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOBYPASSRLS NOINHERIT NOREPLICATION"
        )
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")

    _grant_registry_guard_rights()
    _grant_shadow_source_metadata()
    _move_wp06_definers_to_scoped_roles()
    _replace_registry_entry_guard_projection(minimum_columns=True)

    for table in GUARD_TABLES:
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {GUARD_ROLE}")
    for table, columns in GUARD_COLUMN_GRANTS.items():
        op.execute(f"GRANT SELECT ({columns}) ON TABLE public.{table} TO {GUARD_ROLE}")
    for table, column in GUARD_LOCK_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({column}) ON TABLE public.{table} TO {GUARD_ROLE}")
    for table in GUARD_POLICY_TABLES:
        op.execute(
            f"CREATE POLICY wp06_deployment_guard_read ON public.{table} "
            f"FOR SELECT TO {GUARD_ROLE} USING ({WORKSPACE})"
        )
    for table in GUARD_LOCK_COLUMNS:
        op.execute(
            f"CREATE POLICY wp06_deployment_guard_lock ON public.{table} "
            f"FOR UPDATE TO {GUARD_ROLE} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
        )
    op.execute(f"GRANT EXECUTE ON FUNCTION {PAYLOAD_FUNCTION} TO {GUARD_ROLE}")
    op.execute(f"ALTER FUNCTION {GUARD_FUNCTION} SET search_path = pg_catalog, pg_temp")
    op.execute(f"ALTER FUNCTION {GUARD_FUNCTION} OWNER TO {GUARD_ROLE}")

    op.execute(f"GRANT INSERT ON TABLE public.{POINTER} TO {POINTER_ROLE}")
    op.execute(f"GRANT SELECT (workspace_id) ON TABLE public.{POINTER} TO {POINTER_ROLE}")
    op.execute(
        f"GRANT UPDATE ({POINTER_UPDATE_COLUMNS}) ON TABLE public.{POINTER} TO {POINTER_ROLE}"
    )
    op.execute(
        f"CREATE POLICY scoring_deployment_pointer_trigger_insert ON public.{POINTER} "
        f"FOR INSERT TO {POINTER_ROLE} WITH CHECK (workspace_id IS NOT NULL)"
    )
    op.execute(
        f"CREATE POLICY scoring_deployment_pointer_trigger_identity_read ON public.{POINTER} "
        f"FOR SELECT TO {POINTER_ROLE} USING (workspace_id IS NOT NULL)"
    )
    op.execute(
        f"CREATE POLICY scoring_deployment_pointer_trigger_update ON public.{POINTER} "
        f"FOR UPDATE TO {POINTER_ROLE} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    for signature in (APPLY_FUNCTION, SEED_FUNCTION):
        op.execute(f"ALTER FUNCTION {signature} SET search_path = pg_catalog, pg_temp")
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {POINTER_ROLE}")


def _grant_registry_guard_rights() -> None:
    for table in REGISTRY_READ_TABLES:
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {REGISTRY_ROLE}")
    for table, columns in REGISTRY_COLUMN_GRANTS.items():
        op.execute(f"GRANT SELECT ({columns}) ON TABLE public.{table} TO {REGISTRY_ROLE}")
    op.execute(
        f"GRANT UPDATE ({REGISTRY_UPDATE_COLUMNS}) ON TABLE public.learning_model_registry "
        f"TO {REGISTRY_ROLE}"
    )
    for table in (*REGISTRY_READ_TABLES, *REGISTRY_COLUMN_GRANTS):
        op.execute(
            f"CREATE POLICY wp06_model_registry_guard_read ON public.{table} "
            f"FOR SELECT TO {REGISTRY_ROLE} USING ({WORKSPACE})"
        )
    op.execute(
        "CREATE POLICY wp06_model_registry_guard_update ON public.learning_model_registry "
        f"FOR UPDATE TO {REGISTRY_ROLE} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )


def _grant_shadow_source_metadata() -> None:
    # SECURITY DEFINER của shadow gọi các helper đã bị revoke EXECUTE khỏi PUBLIC.
    for signature in SHADOW_HELPER_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {SHADOW_GUARD_ROLE}")
    for table in SHADOW_FULL_READ_TABLES:
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {SHADOW_GUARD_ROLE}")
        op.execute(
            f"CREATE POLICY wp06_shadow_guard_read ON public.{table} "
            f"FOR SELECT TO {SHADOW_GUARD_ROLE} USING ({WORKSPACE})"
        )
    for table, columns in SHADOW_SOURCE_COLUMNS.items():
        op.execute(f"GRANT SELECT ({columns}) ON TABLE public.{table} TO {SHADOW_GUARD_ROLE}")
        op.execute(
            f"CREATE POLICY wp06_shadow_guard_read ON public.{table} "
            f"FOR SELECT TO {SHADOW_GUARD_ROLE} USING ({WORKSPACE})"
        )
    # PostgreSQL yêu cầu UPDATE trên ít nhất một cột cho SELECT ... FOR SHARE.
    # Policy workspace + trigger lineage buộc mọi UPDATE thực tế phải có event hợp lệ.
    for table, column in SHADOW_LOCK_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({column}) ON TABLE public.{table} TO {SHADOW_GUARD_ROLE}")
        op.execute(
            f"CREATE POLICY wp06_shadow_guard_lock ON public.{table} "
            f"FOR UPDATE TO {SHADOW_GUARD_ROLE} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
        )
    op.execute(
        f"GRANT SELECT (workspace_id, id, source) ON TABLE public.market_bar TO {SHADOW_GUARD_ROLE}"
    )
    op.execute(
        "CREATE POLICY wp06_shadow_guard_read ON public.market_bar "
        f"FOR SELECT TO {SHADOW_GUARD_ROLE} USING ({WORKSPACE})"
    )


def _move_wp06_definers_to_scoped_roles() -> None:
    for signature in REGISTRY_GUARD_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {signature} SET search_path = pg_catalog, pg_temp")
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {REGISTRY_ROLE}")
    for signature in SHADOW_GUARD_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {SHADOW_GUARD_ROLE}")


def _replace_registry_entry_guard_projection(*, minimum_columns: bool) -> None:
    """Điều chỉnh projection tương ứng với quyền của owner sau upgrade/downgrade."""
    projection = (
        "a.workspace_id, a.id, a.artifact_hash, a.manifest_hash, "
        "a.fold_plan_hash, a.approval_hash, a.artifact"
        if minimum_columns
        else "*"
    )
    search_path = "pg_catalog, pg_temp" if minimum_columns else "pg_catalog, public"
    op.execute(
        f"""CREATE OR REPLACE FUNCTION public.wp06_learning_model_registry_entry_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = {search_path} AS $$
        DECLARE artifact_row record;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_IMMUTABLE';
          END IF;
          IF TG_OP = 'UPDATE' THEN
            IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
              OR NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
              OR NEW.artifact_hash IS DISTINCT FROM OLD.artifact_hash
              OR NEW.manifest_hash IS DISTINCT FROM OLD.manifest_hash
              OR NEW.fold_plan_hash IS DISTINCT FROM OLD.fold_plan_hash
              OR NEW.approval_hash IS DISTINCT FROM OLD.approval_hash
              OR NEW.created_by_membership_id IS DISTINCT FROM OLD.created_by_membership_id
              OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_LINEAGE_IMMUTABLE';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_REVISION_INVALID';
            END IF;
            IF NOT EXISTS (
              SELECT 1 FROM public.learning_model_registry_event e
              WHERE e.workspace_id = OLD.workspace_id AND e.registry_id = OLD.id
                AND e.sequence = NEW.revision AND e.expected_revision = OLD.revision
                AND e.from_state = OLD.state AND e.to_state = NEW.state
            ) THEN
              RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_TRANSITION_WITHOUT_EVENT';
            END IF;
            RETURN NEW;
          END IF;

          IF NEW.state <> 'draft' OR NEW.revision <> 1
            OR NEW.evaluation_report_id IS NOT NULL
            OR NEW.evaluation_report_hash IS NOT NULL THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_MUST_START_DRAFT';
          END IF;
          SELECT {projection}
            INTO artifact_row
            FROM public.learning_training_artifact a
           WHERE a.id = NEW.artifact_id AND a.workspace_id = NEW.workspace_id;
          IF NOT FOUND OR artifact_row.artifact_hash IS DISTINCT FROM NEW.artifact_hash
            OR artifact_row.manifest_hash IS DISTINCT FROM NEW.manifest_hash
            OR artifact_row.fold_plan_hash IS DISTINCT FROM NEW.fold_plan_hash
            OR artifact_row.approval_hash IS DISTINCT FROM NEW.approval_hash
            OR artifact_row.artifact->>'scorer_state' IS DISTINCT FROM 'abstain'
            OR artifact_row.artifact->>'calibrated' IS DISTINCT FROM 'false' THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_ARTIFACT_MISMATCH';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.user_membership m
            WHERE m.workspace_id = NEW.workspace_id AND m.id = NEW.created_by_membership_id
              AND m.status = 'ACTIVE'
          ) THEN
            RAISE EXCEPTION 'WP06_LEARNING_MODEL_REGISTRY_CREATOR_INVALID';
          END IF;
          RETURN NEW;
        END $$"""
    )


def downgrade() -> None:
    for signature in REGISTRY_GUARD_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO crypidian_admin")
        op.execute(f"ALTER FUNCTION {signature} SET search_path = pg_catalog, public")
    for signature in SHADOW_GUARD_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO crypidian_admin")
    op.execute(f"ALTER FUNCTION {GUARD_FUNCTION} OWNER TO crypidian_admin")
    op.execute(f"ALTER FUNCTION {GUARD_FUNCTION} SET search_path = pg_catalog, public")
    for signature in (APPLY_FUNCTION, SEED_FUNCTION):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO crypidian_admin")
        op.execute(f"ALTER FUNCTION {signature} SET search_path = pg_catalog, public")
    _replace_registry_entry_guard_projection(minimum_columns=False)

    op.execute(f"DROP POLICY scoring_deployment_pointer_trigger_update ON public.{POINTER}")
    op.execute(f"DROP POLICY scoring_deployment_pointer_trigger_identity_read ON public.{POINTER}")
    op.execute(f"DROP POLICY scoring_deployment_pointer_trigger_insert ON public.{POINTER}")
    for table in GUARD_POLICY_TABLES:
        op.execute(f"DROP POLICY wp06_deployment_guard_read ON public.{table}")
    for table in GUARD_LOCK_COLUMNS:
        op.execute(f"DROP POLICY wp06_deployment_guard_lock ON public.{table}")

    op.execute(f"REVOKE EXECUTE ON FUNCTION {PAYLOAD_FUNCTION} FROM {GUARD_ROLE}")
    for table in GUARD_TABLES:
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {GUARD_ROLE}")
    for table, columns in GUARD_COLUMN_GRANTS.items():
        op.execute(f"REVOKE SELECT ({columns}) ON TABLE public.{table} FROM {GUARD_ROLE}")
    for table, column in GUARD_LOCK_COLUMNS.items():
        op.execute(f"REVOKE UPDATE ({column}) ON TABLE public.{table} FROM {GUARD_ROLE}")
    op.execute(
        f"REVOKE UPDATE ({POINTER_UPDATE_COLUMNS}) ON TABLE public.{POINTER} FROM {POINTER_ROLE}"
    )
    op.execute(f"REVOKE SELECT (workspace_id) ON TABLE public.{POINTER} FROM {POINTER_ROLE}")
    op.execute(f"REVOKE INSERT ON TABLE public.{POINTER} FROM {POINTER_ROLE}")
    for table in SHADOW_FULL_READ_TABLES:
        op.execute(f"DROP POLICY wp06_shadow_guard_read ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {SHADOW_GUARD_ROLE}")
    for table in SHADOW_SOURCE_COLUMNS:
        op.execute(f"DROP POLICY wp06_shadow_guard_read ON public.{table}")
        columns = SHADOW_SOURCE_COLUMNS[table]
        op.execute(f"REVOKE SELECT ({columns}) ON TABLE public.{table} FROM {SHADOW_GUARD_ROLE}")
    for table, column in SHADOW_LOCK_COLUMNS.items():
        op.execute(f"DROP POLICY wp06_shadow_guard_lock ON public.{table}")
        op.execute(f"REVOKE UPDATE ({column}) ON TABLE public.{table} FROM {SHADOW_GUARD_ROLE}")
    op.execute("DROP POLICY wp06_shadow_guard_read ON public.market_bar")
    op.execute(
        "REVOKE SELECT (workspace_id, id, source) ON TABLE public.market_bar "
        f"FROM {SHADOW_GUARD_ROLE}"
    )
    for signature in SHADOW_HELPER_FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM {SHADOW_GUARD_ROLE}")
    for table in (*REGISTRY_READ_TABLES, *REGISTRY_COLUMN_GRANTS):
        op.execute(f"DROP POLICY wp06_model_registry_guard_read ON public.{table}")
    op.execute("DROP POLICY wp06_model_registry_guard_update ON public.learning_model_registry")
    for table in REGISTRY_READ_TABLES:
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {REGISTRY_ROLE}")
    for table, columns in REGISTRY_COLUMN_GRANTS.items():
        op.execute(f"REVOKE SELECT ({columns}) ON TABLE public.{table} FROM {REGISTRY_ROLE}")
    op.execute(
        f"REVOKE UPDATE ({REGISTRY_UPDATE_COLUMNS}) ON TABLE public.learning_model_registry "
        f"FROM {REGISTRY_ROLE}"
    )
    for role in (SHADOW_GUARD_ROLE, REGISTRY_ROLE, POINTER_ROLE, GUARD_ROLE):
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")
        op.execute(f"DROP ROLE {role}")
    op.execute("ALTER ROLE crypidian_bot_provisioner INHERIT")
