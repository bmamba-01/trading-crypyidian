from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/a9c7e5f3d1b2_merge_wp06_wp07_heads.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration bảo vệ incident eligibility gateway."
    spec = importlib.util.spec_from_file_location("wp06_incident_eligibility_owner", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(*, downgrade: bool = False) -> str:
    statements: list[str] = []
    migration = _migration()
    migration.op = SimpleNamespace(execute=lambda statement: statements.append(str(statement)))
    getattr(migration, "downgrade" if downgrade else "upgrade")()
    return "\n".join(statements)


def test_merge_revision_moves_incident_eligibility_off_superuser_owner() -> None:
    migration = _migration()
    assert migration.revision == "a9c7e5f3d1b2"
    assert migration.down_revision == ("e2f4a6c8b0d7", "b4d6f8a0c2e5")
    statements = _statements()

    assert "ALTER FUNCTION public.wp06_learning_incident_eligible_target(uuid,uuid)" in statements
    assert f"OWNER TO {migration.RIGHTS_ROLE}" in statements
    assert (
        f"GRANT EXECUTE ON FUNCTION {migration.LOCK_FUNCTION} TO {migration.RIGHTS_ROLE}"
        in statements
    )
    assert (
        f"GRANT EXECUTE ON FUNCTION {migration.COHORT_LOCK_FUNCTION} TO {migration.RIGHTS_ROLE}"
        in statements
    )
    assert (
        "GRANT SELECT (active_registry_id,active_registry_revision,active_artifact_hash) "
        f"ON public.learning_shadow_observation TO {migration.RIGHTS_ROLE}"
    ) in statements
    assert (
        "GRANT SELECT (membership_digest) ON public.learning_drift_assessment "
        f"TO {migration.RIGHTS_ROLE}"
    ) in statements
    assert "role_state.rolbypassrls" in statements
    assert "role_state.rolinherit" in statements
    assert "role_state.has_membership" in statements
    assert "WP06_INCIDENT_ELIGIBILITY_PROJECTION_NOT_FOUND" in statements
    assert "proposal public.learning_retrain_proposal' || pg_catalog.chr(37)" in statements
    assert "SELECT * INTO proposal FROM public.learning_retrain_proposal p" in statements
    assert "SELECT * INTO assessment FROM public.learning_drift_assessment AS a" in statements
    assert "a.data_quality_status, a.reason_codes, a.member_count" in statements
    assert "SELECT r.workspace_id, r.id, r.artifact_hash INTO registry" in statements


def test_merge_revision_accepts_an_already_projected_eligibility_function() -> None:
    statements = _statements()

    assert "already_projected" in statements
    assert "proposal record;" in statements
    assert "SELECT p.workspace_id, p.id, p.assessment_id, p.assessment_hash" in statements
    assert "SELECT a.workspace_id, a.id, a.content_hash, a.membership_digest" in statements
    assert "SELECT r.workspace_id, r.id, r.artifact_hash" in statements


def test_projection_guard_binds_exact_table_alias_and_single_into_target() -> None:
    statements = _statements()

    for target, alias in (("proposal", "p"), ("assessment", "a"), ("registry", "r")):
        assert f"{target}_projection || ' AS {alias}'" in statements
        assert f"{target}_projection || ' {alias}'" in statements
        assert f"INTO {target}[[:space:];]" in statements


def test_projection_guard_normalizes_multiline_sql_before_rejecting_extra_targets() -> None:
    statements = _statements()
    normalized_statements = " ".join(statements.split())

    assert "function_ddl_normalized text;" in statements
    assert (
        "pg_catalog.regexp_replace( function_ddl, '[[:space:]]+', ' ', 'g')"
        in normalized_statements
    )
    assert "function_ddl_normalized, 'SELECT * INTO proposal'" in normalized_statements
    assert "INTO proposal[[:space:];]" in normalized_statements


def test_projection_guard_counts_targets_followed_by_statement_terminators() -> None:
    statements = _statements()
    normalized_statements = " ".join(statements.split())

    for target in ("proposal", "assessment", "registry"):
        assert f"INTO {target}[[:space:];]" in normalized_statements


def test_merge_revision_moves_incident_trigger_definers_to_scoped_roles() -> None:
    migration = _migration()
    statements = _statements()
    normalized = " ".join(statements.split())

    assert (
        "ALTER FUNCTION public.wp06_lock_learning_incident_scope(uuid,uuid) "
        f"OWNER TO {migration.RIGHTS_ROLE}"
    ) in statements
    assert (
        "ALTER FUNCTION public.wp06_require_shadow_exact_source_scope() "
        f"OWNER TO {migration.RIGHTS_ROLE}"
    ) in statements
    assert (
        "ALTER FUNCTION public.wp06_learning_incident_guard() "
        f"OWNER TO {migration.INCIDENT_GUARD_ROLE}"
    ) in statements
    assert migration.INCIDENT_GUARD_ROLE in statements
    assert "role_state.rolcanlogin" in statements
    assert "role_state.rolbypassrls" in statements
    assert "role_state.has_membership" in statements
    assert (
        f"GRANT SELECT ON TABLE public.learning_incident_entry_hold_event "
        f"TO {migration.INCIDENT_GUARD_ROLE}"
    ) in statements
    assert (
        f"CREATE POLICY wp06_incident_guard_read ON public.learning_incident_entry_hold_event "
        f"FOR SELECT TO {migration.INCIDENT_GUARD_ROLE}"
    ) in normalized
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_learning_incident_eligible_target(uuid,uuid) "
        f"TO {migration.INCIDENT_GUARD_ROLE}"
    ) in statements
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_shadow_inference_source_scope"
        "(uuid,uuid,timestamptz) "
        f"TO {migration.RIGHTS_ROLE}"
    ) in statements


def test_merge_revision_downgrade_restores_incident_trigger_owners_and_grants() -> None:
    migration = _migration()
    statements = _statements(downgrade=True)

    for signature in (
        "public.wp06_lock_learning_incident_scope(uuid,uuid)",
        "public.wp06_require_shadow_exact_source_scope()",
    ):
        assert f"ALTER FUNCTION {signature} OWNER TO crypidian_admin" in statements
    assert (
        "ALTER FUNCTION public.wp06_learning_incident_guard() OWNER TO crypidian_admin"
        in statements
    )
    assert (
        f"REVOKE SELECT ON TABLE public.learning_incident_entry_hold_event "
        f"FROM {migration.INCIDENT_GUARD_ROLE}"
    ) in statements
    assert f"DROP ROLE {migration.INCIDENT_GUARD_ROLE}" in statements


def test_downgrade_restores_previous_owner_and_revokes_only_added_access() -> None:
    migration = _migration()
    statements = _statements(downgrade=True)

    assert (
        "ALTER FUNCTION public.wp06_learning_incident_eligible_target(uuid,uuid) "
        "OWNER TO crypidian_admin"
    ) in statements
    assert (
        f"REVOKE EXECUTE ON FUNCTION {migration.LOCK_FUNCTION} FROM {migration.RIGHTS_ROLE}"
        in statements
    )
    assert (
        f"REVOKE EXECUTE ON FUNCTION {migration.COHORT_LOCK_FUNCTION} FROM {migration.RIGHTS_ROLE}"
        in statements
    )
    assert (
        "REVOKE SELECT (active_registry_id,active_registry_revision,active_artifact_hash) "
        f"ON public.learning_shadow_observation FROM {migration.RIGHTS_ROLE}"
    ) in statements
    assert (
        "REVOKE SELECT (membership_digest) ON public.learning_drift_assessment "
        f"FROM {migration.RIGHTS_ROLE}"
    ) in statements
