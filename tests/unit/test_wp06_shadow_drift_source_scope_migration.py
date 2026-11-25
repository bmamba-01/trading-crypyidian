from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from trading_bot.adapters.database.models.learning_shadow_drift_entities import (
    LearningDriftAssessmentMemberRecord,
    LearningDriftAssessmentRecord,
    LearningShadowObservationRecord,
)

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f8e0a2c4d6bb_wp06_shadow_drift_source_scope.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration source-use cho Shadow/Drift."
    spec = importlib.util.spec_from_file_location("wp06_shadow_drift_source_scope", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(
        execute=lambda sql: statements.append(str(sql)),
        add_column=lambda table, column: statements.append(f"ALTER {table} ADD {column.name}"),
        create_check_constraint=lambda name, table, expression: statements.append(
            f"CHECK {table}.{name}: {expression}"
        ),
        f=lambda name: name,
    )
    module.upgrade()
    return "\n".join(statements)


def test_shadow_drift_migration_chains_after_paper_source_scope() -> None:
    migration = _migration()

    assert migration.revision == "f8e0a2c4d6bb"
    assert migration.down_revision == "f8e0a2c4d6ba"


def test_shadow_gate_is_fixed_inference_and_uses_exact_persisted_bar_lineage() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_authorize_shadow_inference_source_scope(")
    end = sql.index("ALTER FUNCTION public.wp06_authorize_shadow_inference_source_scope(", start)
    gate = sql[start:end]

    for expected in (
        "wp06_verify_bot_decision_source_scope",
        "bot_decision_source_bar",
        "market_bar_id",
        "source_event_id",
        "paper_demo",
        "paper_inference",
        "research-source:",
        "source_license_evidence",
        "max_retention_days",
        "CRYPIDIAN_FIRST_PARTY_QA",
        "IS DISTINCT FROM 'null'::jsonb",
        "'retention_days', NULL",
    ):
        assert expected in gate
    assert "target_purpose" not in gate
    assert "target_outputs" not in gate
    assert "SELECT policy.*" not in gate


def test_shadow_source_scope_sql_checks_every_license_use_dimension() -> None:
    """Giữ regression guard cho các chiều rights trước khi ghi fingerprint."""
    sql = _upgrade_sql()
    gate_start = sql.index("CREATE FUNCTION public.wp06_authorize_shadow_inference_source_scope(")
    scope_start = sql.index("IF policy_row.license_scope->>'source_id'", gate_start)
    scope_end = sql.index("source_license_evidence AS evidence", scope_start)
    scope_contract = sql[scope_start:scope_end]

    for predicate in (
        "policy_row.license_scope->>'source_id' IS DISTINCT FROM group_row.source_id",
        "policy_row.license_scope->>'provider' IS DISTINCT FROM policy_row.provider",
        "policy_row.license_scope->>'product' IS DISTINCT FROM policy_row.product",
        "policy_row.license_scope->>'venue' IS DISTINCT FROM policy_row.venue",
        "policy_row.license_scope->>'contract' IS DISTINCT FROM policy_row.contract",
        "policy_row.license_scope->'symbols'",
        "policy_row.license_scope->'intervals'",
        "policy_row.license_scope->>'data_start'",
        "policy_row.license_scope->>'data_end'",
        "policy_row.license_scope->'allowed_purposes'",
        "policy_row.license_scope->'allowed_outputs'",
        "policy_row.license_scope->'derived_use'",
        "policy_row.license_scope->'max_retention_days'",
    ):
        assert predicate in scope_contract


def test_shadow_inference_hash_pattern_survives_migration_f_string_rendering() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_authorize_shadow_inference_source_scope(")
    end = sql.index("ALTER FUNCTION public.wp06_authorize_shadow_inference_source_scope(", start)
    gate = sql[start:end]

    for predicate in (
        "source_scope_row.decision_scope_fingerprint !~ '^[0-9a-f]{64}$'",
        "policy_row.license_digest !~ '^[0-9a-f]{64}$'",
        "policy_row.license_scope_hash !~ '^[0-9a-f]{64}$'",
        "policy_row.policy_fingerprint !~ '^[0-9a-f]{64}$'",
    ):
        assert predicate in gate


def test_scoped_shadow_hash_documents_preserve_python_sorted_json_order() -> None:
    sql = _upgrade_sql()
    idempotency_start = sql.index(
        "CREATE OR REPLACE FUNCTION public.wp06_shadow_observation_idempotency_key("
    )
    canonical_start = sql.index(
        "CREATE OR REPLACE FUNCTION public.wp06_shadow_observation_canonical_document(",
        idempotency_start,
    )
    idempotency = sql[idempotency_start:canonical_start]
    idempotency_scope_start = idempotency.index(
        "END IF;", idempotency.index("IF row_value.source_scope_fingerprint IS NULL")
    )
    idempotency_scope = idempotency[idempotency_scope_start:]
    assert "pg_catalog.json_build_object" in idempotency_scope
    assert "pg_catalog.jsonb_build_object" not in idempotency_scope
    idempotency_keys = (
        "'active_artifact_hash'",
        "'active_pointer_version'",
        "'active_registry_id'",
        "'active_registry_revision'",
        "'challenger_reason'",
        "'challenger_registry_id'",
        "'challenger_revision'",
        "'incumbent_decision_hash'",
        "'incumbent_decision_id'",
        "'incumbent_strategy_release_hash'",
        "'registry_approval_event_hash'",
        "'registry_approval_event_id'",
        "'registry_approval_event_sequence'",
        "'rights_eligible'",
        "'rights_fingerprint'",
        "'rights_known_at'",
        "'rights_policy_id'",
        "'rights_policy_sequence'",
        "'rights_source_id'",
        "'schema_compatible'",
        "'snapshot_coverage'",
        "'snapshot_hash'",
        "'snapshot_quality_ok'",
        "'source_scope_fingerprint'",
        "'workspace_id'",
    )
    assert [idempotency_scope.index(key) for key in idempotency_keys] == sorted(
        idempotency_scope.index(key) for key in idempotency_keys
    )

    canonical_end = sql.index(
        "CREATE OR REPLACE FUNCTION public.wp06_lock_learning_shadow_cohort(", canonical_start
    )
    canonical = sql[canonical_start:canonical_end]
    canonical_scope_start = canonical.index(
        "END IF;", canonical.index("IF row_value.source_scope_fingerprint IS NULL")
    )
    canonical_scope = canonical[canonical_scope_start:]
    assert "pg_catalog.jsonb_build_object" not in canonical_scope
    assert "'source_scope_fingerprint'" in canonical_scope
    assert canonical_scope.index("'source_scope_fingerprint'") < canonical_scope.rindex(
        "'workspace_id'"
    )


def test_shadow_scope_gates_are_executable_by_dedicated_observer_only() -> None:
    sql = _upgrade_sql()
    source_ids_start = sql.index("CREATE FUNCTION public.wp06_shadow_drift_decision_source_ids(")
    source_ids_end = sql.index(
        "CREATE FUNCTION public.wp06_authorize_shadow_inference_source_scope(", source_ids_start
    )
    source_ids = sql[source_ids_start:source_ids_end]
    inference_start = source_ids_end
    inference_end = sql.index(
        "ALTER FUNCTION public.wp06_authorize_shadow_inference_source_scope(", inference_start
    )
    inference_gate = sql[inference_start:inference_end]

    for gate in (source_ids, inference_gate):
        assert "session_user NOT IN ('crypidian_admin_web', " in gate
        assert "'crypidian_learning_shadow_observer')" in gate
    assert "wp06_paper_performance_decision_source_ids" not in source_ids
    assert "wp06_verify_bot_decision_source_scope" in source_ids
    assert "GRANT EXECUTE ON FUNCTION public.wp06_shadow_drift_decision_source_ids" in sql
    assert "GRANT EXECUTE ON FUNCTION public.wp06_authorize_shadow_inference_source_scope" in sql
    assert "TO crypidian_admin_web, crypidian_learning_shadow_observer" in sql


def test_shadow_drift_trigger_owner_can_execute_source_lineage_helper() -> None:
    sql = _upgrade_sql()

    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_shadow_drift_decision_source_ids(uuid,uuid) "
        "TO crypidian_learning_shadow_integrity_guard"
    ) in sql


def test_rights_rechecks_are_volatile_and_run_under_least_privilege_reader_role() -> None:
    sql = _upgrade_sql()
    digest_start = sql.index("CREATE FUNCTION public.wp06_learning_drift_current_rights_digest(")
    digest_end = sql.index(
        "REVOKE ALL ON FUNCTION public.wp06_learning_drift_current_rights_digest(",
        digest_start,
    )
    digest = sql[digest_start:digest_end]
    assert "RETURNS text LANGUAGE plpgsql SECURITY DEFINER" in digest
    assert "STABLE" not in digest

    for signature in (
        "wp06_learning_drift_current_rights_digest(uuid,uuid,timestamptz)",
        "wp06_recheck_learning_drift_assessment_source_rights(uuid,uuid)",
        "wp06_recheck_learning_shadow_observation_source_rights(uuid,uuid)",
    ):
        create_name = signature.split("(", maxsplit=1)[0]
        create_at = sql.index(f"CREATE FUNCTION public.{create_name}(")
        owner_at = sql.index(f"ALTER FUNCTION public.{signature}")
        assert create_at < owner_at
        assert "OWNER TO crypidian_scorer_deployment_rights_reader" in sql[owner_at:]

    drift_recheck_start = sql.index(
        "CREATE FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights("
    )
    drift_recheck_end = sql.index(
        "ALTER FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights(",
        drift_recheck_start,
    )
    drift_recheck = sql[drift_recheck_start:drift_recheck_end]
    assert "SELECT assessment.*" not in drift_recheck
    assert "assessment.source_rights_digest" in drift_recheck

    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
    ):
        assert f"CREATE POLICY wp06_shadow_drift_rights_{table}_select" in sql
        assert f"ON public.{table} FOR SELECT TO crypidian_scorer_deployment_rights_reader" in sql


def test_shadow_writer_takes_exact_source_locks_before_shared_fence_after_lineage_guard() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE FUNCTION public.wp06_require_shadow_exact_source_scope()")
    end = sql.index("REVOKE ALL ON FUNCTION public.wp06_require_shadow_exact_source_scope()", start)
    writer = sql[start:end]

    assert writer.index("research-source:") < writer.index("wp06-learning-observation-member-set:")
    assert "CREATE TRIGGER zz_wp06_shadow_observation_exact_source_scope" in sql
    assert "pg_get_functiondef" in MIGRATION.read_text()


def test_drift_writes_exact_rights_digest_and_deny_metrics_when_any_member_is_uncovered() -> None:
    sql = _upgrade_sql()

    assert "source_rights_digest" in sql
    assert "current_rights_fingerprint" in sql
    assert "current_source_ids" in sql
    assert "wp06_authorize_learning_drift_source_scope" in sql
    assert "current_eligible" in sql
    assert "current member set changed" in sql.lower()


def test_drift_assessment_gate_has_combined_policy_and_transaction_time_error() -> None:
    sql = _upgrade_sql()
    start = sql.index(
        "CREATE OR REPLACE FUNCTION public.wp06_lock_learning_drift_assessment_cohort()"
    )
    end = sql.index(
        "CREATE OR REPLACE FUNCTION public.wp06_require_learning_drift_member_lineage()",
        start,
    )
    gate = sql[start:end]

    assert "NEW.created_at IS DISTINCT FROM transaction_timestamp()" in gate
    assert "NEW.policy_version IS DISTINCT FROM 'coverage-v2-30d'" in gate
    assert "NEW.observation_window_days IS DISTINCT FROM 30" in gate
    assert "NEW.member_count = 0" in gate
    assert "WP06_SHADOW_DRIFT_POLICY_OR_TIME_INVALID" in gate


def test_admin_feed_filters_assessment_and_proposal_through_current_rights_gateway() -> None:
    sql = _upgrade_sql()

    assert "wp06_recheck_learning_drift_assessment_source_rights" in sql
    assert "CREATE TRIGGER" in sql
    assert "BEFORE INSERT" in sql
    assert "REVOKE ALL ON FUNCTION" in sql


def test_workspace_fence_follows_sorted_source_locks_and_downgrade_guards_data() -> None:
    sql = _upgrade_sql()
    source_at = sql.index("research-source:")
    workspace_fence_at = sql.index("wp06-learning-observation-member-set:")

    assert source_at < workspace_fence_at
    assert "WP06_SHADOW_DRIFT_SOURCE_SCOPE_DOWNGRADE_POPULATED_DENIED" in MIGRATION.read_text()


def test_hold_rechecks_exact_drift_rights_after_sorted_target_and_assessment_locks() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE OR REPLACE FUNCTION public.wp06_learning_incident_eligible_target(")
    hold_gate = sql[start:]
    target_scope_start = sql.index(
        "CREATE FUNCTION public.wp06_learning_incident_target_source_ids("
    )
    target_scope_end = sql.index(
        "ALTER FUNCTION public.wp06_learning_incident_target_source_ids(uuid)",
        target_scope_start,
    )
    target_source_scope = sql[target_scope_start:target_scope_end]

    for expected in (
        "wp06_learning_incident_target_source_ids",
        "wp06_shadow_drift_decision_source_ids",
        "wp06_learning_drift_current_rights_digest",
        "source_rights_digest IS NULL",
        "WP06_INCIDENT_SOURCE_RIGHTS_STALE",
        "research-source:",
        "wp06-learning-observation-member-set:",
    ):
        assert expected in hold_gate

    source_lock_at = hold_gate.index("research-source:")
    workspace_fence_at = hold_gate.index("wp06-learning-observation-member-set:")
    after_fence = hold_gate[workspace_fence_at:]
    initial_target_at = hold_gate.index("wp06_resolve_scorer_runtime_target")
    current_target_at = hold_gate.rindex("wp06_resolve_scorer_runtime_target")
    current_rights_at = hold_gate.index("wp06_learning_drift_current_rights_digest")
    current_membership_at = workspace_fence_at + after_fence.index("assessment.membership_digest")
    assert "wp06_scorer_manifest_source_refs" in target_source_scope
    assert "learning_scorer_test_opportunity_proof" in target_source_scope
    assert source_lock_at < workspace_fence_at < current_membership_at
    assert workspace_fence_at < current_rights_at < current_target_at
    assert initial_target_at < hold_gate.index("WP06_INCIDENT_SOURCE_LINEAGE_MISSING")
    assert current_target_at > initial_target_at
    assert "ORDER BY source_id" in hold_gate
    assert "pg_catalog.char_length(source_id) > 128" in hold_gate
    assert "source_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]*$'" in hold_gate


def test_deferred_source_bar_lineage_guard_fences_late_complete_sets() -> None:
    sql = _upgrade_sql()
    start = sql.index(
        "CREATE OR REPLACE FUNCTION public.wp06_assert_complete_bot_decision_source_bar_set()"
    )
    end = sql.index(
        "CREATE OR REPLACE FUNCTION public.wp06_learning_incident_eligible_target(", start
    )
    lineage_guard = sql[start:end]

    assert "bot_decision_source_bar" in lineage_guard
    assert "JOIN public.market_bar" in lineage_guard
    assert "ORDER BY provider.source_id" in lineage_guard
    assert "research-source:" in lineage_guard
    assert "wp06-learning-observation-member-set:" in lineage_guard
    assert (
        lineage_guard.index("research-source:")
        < lineage_guard.index("wp06-learning-observation-member-set:")
        < lineage_guard.index("FOR group_row IN")
    )


def test_upgrade_captures_legacy_functions_before_replacement_and_denies_app_access() -> None:
    sql = _upgrade_sql()
    capture_at = sql.index("pg_catalog.pg_get_functiondef")
    replacement_at = sql.index("CREATE OR REPLACE FUNCTION")

    assert capture_at < replacement_at
    for signature in (
        "wp06_shadow_observation_idempotency_key",
        "wp06_shadow_observation_canonical_document",
        "wp06_lock_learning_shadow_cohort",
        "wp06_drift_assessment_idempotency_key",
        "wp06_drift_assessment_content_hash",
        "wp06_lock_learning_drift_assessment_cohort",
        "wp06_require_learning_drift_member_lineage",
        "wp06_validate_learning_drift_assessment_membership",
        "wp06_require_learning_shadow_lineage",
        "wp06_learning_incident_eligible_target",
        "wp06_assert_complete_bot_decision_source_bar_set",
    ):
        assert signature in sql[capture_at:replacement_at]
    assert "REVOKE ALL ON SCHEMA wp06_shadow_drift_source_scope FROM PUBLIC" in sql
    assert "REVOKE ALL ON TABLE wp06_shadow_drift_source_scope.function_restore" in sql
    assert "backup_count <> 11" in MIGRATION.read_text()


def test_downgrade_restores_legacy_functions_acl_trigger_and_constraint_before_columns() -> None:
    actions: list[str] = []
    migration = _migration()
    migration.op = SimpleNamespace(
        execute=lambda sql: actions.append(str(sql)),
        drop_column=lambda table, column: actions.append(f"DROP COLUMN {table}.{column}"),
    )

    migration.downgrade()

    upgrade_sql = _upgrade_sql()
    script = "\n".join(actions)
    guard_at = script.index("WP06_SHADOW_DRIFT_SOURCE_SCOPE_DOWNGRADE_POPULATED_DENIED")
    restore_at = script.index("EXECUTE backup_row.function_definition")
    drop_trigger_at = script.index("DROP TRIGGER zz_wp06_shadow_observation_exact_source_scope")
    restore_constraint_at = script.index(
        "ADD CONSTRAINT ck_learning_drift_assessment_member_learning_drift_asse_5fd5"
    )
    drop_column_at = script.index(
        "DROP COLUMN learning_shadow_observation.source_scope_fingerprint"
    )

    assert script.index("LOCK TABLE") < guard_at < restore_at
    assert restore_at < drop_trigger_at < restore_constraint_at < drop_column_at
    assert "CREATE TRIGGER zz_wp06_shadow_observation_exact_source_scope" in upgrade_sql
    assert "DROP TRIGGER learning_shadow_observation_lineage_guard" not in script
    assert "DROP TRIGGER learning_drift_assessment_cohort_lock" not in script
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_paper_performance_decision_source_scope"
        in upgrade_sql
    )
    assert "DROP FUNCTION public.wp06_recheck_learning_drift_assessment_source_rights" in script
    assert "DROP FUNCTION public.wp06_shadow_drift_decision_source_ids" in script
    assert "REVOKE EXECUTE ON FUNCTION public.wp06_paper_performance_decision_source_ids" in script
    assert "DROP TABLE wp06_shadow_drift_source_scope.function_restore" in script
    assert "DROP SCHEMA wp06_shadow_drift_source_scope" in script
    assert "wp06_authorize_paper_performance_decision_source_scope(uuid,uuid,timestamptz) " in (
        script
    )
    assert "ALTER FUNCTION public.wp06_shadow_observation_idempotency_key" not in script


def test_source_scope_constraints_match_orm_metadata() -> None:
    sql = " ".join(_upgrade_sql().replace("pg_catalog.", "").split()).lower()
    shadow_observation = LearningShadowObservationRecord.__table__
    drift_assessment = LearningDriftAssessmentRecord.__table__
    drift_member = LearningDriftAssessmentMemberRecord.__table__
    expected = {
        "learning_shadow_observation_source_scope_hash_valid": shadow_observation,
        "learning_shadow_observation_exact_rights_valid": shadow_observation,
        "learning_drift_assessment_source_rights_digest_valid": drift_assessment,
        "learning_drift_assessment_member_current_scope_valid": drift_member,
        "learning_drift_assessment_member_current_eligible_valid": drift_member,
    }
    for name, table in expected.items():
        constraint = next(
            (item for item in table.constraints if getattr(item, "name", None) == name),
            None,
        )
        assert constraint is not None, f"ORM thiếu constraint {name}"
        expression = " ".join(str(constraint.sqltext).split()).lower()
        assert name in sql
        assert expression in sql


def test_upgrade_and_downgrade_use_the_postgres_truncated_eligibility_constraint() -> None:
    constraint = "ck_learning_drift_assessment_member_learning_drift_asse_5fd5"
    assert f"DROP CONSTRAINT {constraint}" in _upgrade_sql()

    actions: list[str] = []
    migration = _migration()
    migration.op = SimpleNamespace(
        execute=lambda sql: actions.append(str(sql)),
        drop_column=lambda table, column: actions.append(f"DROP COLUMN {table}.{column}"),
    )
    migration.downgrade()
    assert f"ADD CONSTRAINT {constraint}" in "\n".join(actions)


def test_downgrade_removes_rights_reader_policies_and_column_grants() -> None:
    actions: list[str] = []
    migration = _migration()
    migration.op = SimpleNamespace(
        execute=lambda statement: actions.append(str(statement)),
        drop_column=lambda table, column: actions.append(f"DROP COLUMN {table}.{column}"),
    )

    migration.downgrade()

    script = "\n".join(actions)
    policies = {
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
    }
    for table in policies:
        assert f"DROP POLICY wp06_shadow_drift_rights_{table}_select ON public.{table}" in script
    grants = (
        "REVOKE SELECT (workspace_id,id,incumbent_decision_id,rights_eligible,"
        "source_scope_fingerprint,challenger_registry_id,challenger_revision,observed_at) "
        "ON TABLE public.learning_shadow_observation "
        "FROM crypidian_scorer_deployment_rights_reader",
        "REVOKE SELECT (workspace_id,id,source_rights_digest,cohort_registry_id,"
        "cohort_revision,window_start,window_end,member_count,membership_digest) "
        "ON TABLE public.learning_drift_assessment FROM crypidian_scorer_deployment_rights_reader",
        "REVOKE SELECT (workspace_id,assessment_id,observation_id,current_rights_eligible,"
        "current_rights_fingerprint,current_source_ids,current_eligible,baseline_available,"
        "schema_compatible) ON TABLE public.learning_drift_assessment_member "
        "FROM crypidian_scorer_deployment_rights_reader",
    )
    for grant in grants:
        assert grant in script
        assert script.index(grant) < script.index("DROP COLUMN learning_shadow_observation")
