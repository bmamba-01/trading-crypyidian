from __future__ import annotations

import importlib.util
import re
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from trading_bot.adapters.database.models.base import Base


def test_scorer_deployment_is_tenant_scoped_immutable_metadata_only() -> None:
    events = Base.metadata.tables["learning_scoring_deployment_event"]
    pointer = Base.metadata.tables["learning_scoring_deployment_pointer"]
    forbidden = {
        "feature_vector",
        "feature_values",
        "labels",
        "sample_ids",
        "test_payload",
        "raw_source",
    }

    assert not forbidden & set(events.columns.keys())
    assert {
        "event_hash",
        "intent_hash",
        "canonical_event",
        "current_rights_receipt_hash",
    } <= set(events.columns.keys())
    assert "event_id" in pointer.columns
    assert any(
        isinstance(item, UniqueConstraint)
        and tuple(column.name for column in item.columns) == ("workspace_id", "idempotency_key")
        for item in events.constraints
    )
    assert any(
        isinstance(item, ForeignKeyConstraint)
        and tuple(column.name for column in item.columns) == ("workspace_id", "actor_membership_id")
        and item.referred_table.name == "user_membership"
        for item in events.foreign_key_constraints
    )


def test_session_schema_keeps_unverified_legacy_mfa_method_unknown() -> None:
    session = Base.metadata.tables["session"]
    assert session.c.mfa_method.nullable is True


def test_deployment_migration_scopes_event_insert_and_drops_jsonb_function_signature() -> None:
    root = Path(__file__).parents[2]
    migration = (
        root / "migrations" / "versions" / "c5e7a9b1d3f5_wp06_scorer_deployment.py"
    ).read_text(encoding="utf-8")

    assert "FOR INSERT TO {ADMIN} WITH CHECK ({WORKSPACE})" in migration
    assert "DROP FUNCTION public.wp06_scoring_deployment_payload_has_forbidden(jsonb)" in migration
    assert "length(btrim(reason)) BETWEEN 12 AND 2000" in migration


def test_deployment_definers_use_dedicated_minimum_privilege_roles() -> None:
    migration_path = (
        Path(__file__).parents[2]
        / "migrations/versions/d9f1c3a5b7e9_wp06_scoring_seed_function_hardening.py"
    )
    assert migration_path.is_file(), "Thiếu migration role riêng cho trigger seed deployment."
    spec = importlib.util.spec_from_file_location("wp06_scoring_seed_hardening", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()

    sql = output.getvalue()
    assert migration.revision == "d9f1c3a5b7e9"
    assert migration.down_revision == "d8f0a2c4e6b1"
    assert "ALTER ROLE crypidian_bot_provisioner NOINHERIT" in sql
    assert "CREATE ROLE crypidian_scorer_deployment_guard NOLOGIN" in sql
    assert "CREATE ROLE crypidian_scorer_deployment_pointer_writer NOLOGIN" in sql
    assert "CREATE ROLE crypidian_learning_model_registry_guard NOLOGIN" in sql
    assert "CREATE ROLE crypidian_learning_shadow_integrity_guard NOLOGIN" in sql
    assert "ALTER FUNCTION public.wp06_scoring_deployment_workspace_seed()" in sql
    assert "OWNER TO crypidian_scorer_deployment_pointer_writer" in sql
    assert "ALTER FUNCTION public.wp06_scoring_deployment_guard()" in sql
    assert "OWNER TO crypidian_scorer_deployment_guard" in sql
    assert "ALTER FUNCTION public.wp06_scoring_deployment_apply()" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "GRANT INSERT ON TABLE public.learning_scoring_deployment_pointer" in sql
    assert "GRANT SELECT (workspace_id) ON TABLE public.learning_scoring_deployment_pointer" in sql
    assert "GRANT UPDATE (event_id, revision, active_pointer_version, mode, changed_at)" in sql
    assert "FOR INSERT TO crypidian_scorer_deployment_pointer_writer" in sql
    assert "FOR SELECT TO crypidian_scorer_deployment_pointer_writer" in sql
    assert "FOR UPDATE TO crypidian_scorer_deployment_pointer_writer" in sql
    assert "FOR SELECT TO crypidian_scorer_deployment_guard" in sql
    assert (
        "ALTER FUNCTION public.wp06_learning_model_registry_event_guard() SET search_path "
        "= pg_catalog, pg_temp"
    ) in sql
    assert (
        "ALTER FUNCTION public.wp06_learning_model_registry_event_guard() "
        "OWNER TO crypidian_learning_model_registry_guard"
    ) in sql
    assert (
        "ALTER FUNCTION public.wp06_require_learning_shadow_lineage() "
        "OWNER TO crypidian_learning_shadow_integrity_guard"
    ) in sql
    assert "GRANT SELECT ON TABLE public.source_policy_revision" in sql
    assert "GRANT SELECT (workspace_id, id, source) ON TABLE public.market_bar" in sql
    assert (
        "GRANT SELECT (workspace_id, id, feature_snapshot_id, available_at, evidence_hash, "
        "schema_version, source_refs) ON TABLE public.shared_signal_evidence" in sql
    )
    assert "is_usable, input_event_ids) ON TABLE public.feature_snapshot" in sql
    assert (
        "GRANT SELECT (workspace_id, id, artifact_hash, manifest_hash, fold_plan_hash, "
        "approval_hash, feature_schema_hash) ON TABLE public.learning_training_artifact"
    ) in sql
    assert (
        "GRANT UPDATE (revision) ON TABLE public.learning_model_registry "
        "TO crypidian_learning_shadow_integrity_guard"
    ) in sql
    assert (
        "GRANT UPDATE (version) ON TABLE public.learning_model_active_pointer "
        "TO crypidian_learning_shadow_integrity_guard"
    ) in sql
    assert (
        "CREATE POLICY wp06_shadow_guard_lock ON public.learning_model_registry "
        "FOR UPDATE TO crypidian_learning_shadow_integrity_guard"
    ) in sql
    assert (
        "CREATE POLICY wp06_shadow_guard_lock ON public.learning_model_active_pointer "
        "FOR UPDATE TO crypidian_learning_shadow_integrity_guard"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_canonical_source_cost(numeric) "
        "TO crypidian_learning_shadow_integrity_guard"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_shadow_market_source_rights_matches(text, text) "
        "TO crypidian_learning_shadow_integrity_guard"
    ) in sql
    for signature in (
        "wp06_canonical_float_json(double precision)",
        "wp06_canonical_utc_timestamp_json(timestamptz)",
        "wp06_sha256_canonical_json(json)",
        "wp06_compact_canonical_json(json)",
    ):
        assert (
            f"GRANT EXECUTE ON FUNCTION public.{signature} "
            "TO crypidian_learning_shadow_integrity_guard"
        ) in sql
    assert (
        "GRANT SELECT (workspace_id, id, status) ON TABLE public.user_membership "
        "TO crypidian_scorer_deployment_guard"
    ) in sql
    assert (
        "GRANT SELECT (workspace_id, membership_id, role) ON TABLE public.role_binding "
        "TO crypidian_scorer_deployment_guard"
    ) in sql
    assert (
        "GRANT SELECT (workspace_id, id, event_hash) "
        "ON TABLE public.learning_scoring_deployment_event "
        "TO crypidian_scorer_deployment_guard"
    ) in sql
    assert "GRANT SELECT ON TABLE public.user_membership" not in sql
    assert (
        "GRANT UPDATE (changed_at) ON TABLE "
        "public.learning_scoring_deployment_pointer TO crypidian_scorer_deployment_guard"
    ) in sql
    assert (
        "GRANT UPDATE (version) ON TABLE public.learning_model_active_pointer "
        "TO crypidian_scorer_deployment_guard"
    ) in sql
    normalized_sql = re.sub(r"\s+", " ", sql)
    assert (
        "SELECT a.workspace_id, a.id, a.artifact_hash, a.manifest_hash, "
        "a.fold_plan_hash, a.approval_hash, a.artifact INTO artifact_row"
    ) in normalized_sql
    assert "SELECT * INTO artifact_row" not in normalized_sql

    downgrade_output = StringIO()
    downgrade_context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": downgrade_output}
    )
    with Operations.context(downgrade_context):
        migration.downgrade()
    normalized_downgrade = re.sub(r"\s+", " ", downgrade_output.getvalue())
    assert "SELECT * INTO artifact_row" in normalized_downgrade
    assert "SET search_path = pg_catalog, public AS $$" in normalized_downgrade
    assert (
        "REVOKE UPDATE (revision) ON TABLE public.learning_model_registry "
        "FROM crypidian_learning_shadow_integrity_guard"
    ) in normalized_downgrade
    assert (
        "REVOKE UPDATE (version) ON TABLE public.learning_model_active_pointer "
        "FROM crypidian_learning_shadow_integrity_guard"
    ) in normalized_downgrade
    assert (
        "REVOKE EXECUTE ON FUNCTION public.wp06_canonical_source_cost(numeric) "
        "FROM crypidian_learning_shadow_integrity_guard"
    ) in normalized_downgrade
    for signature in (
        "wp06_shadow_market_source_rights_matches(text, text)",
        "wp06_canonical_float_json(double precision)",
        "wp06_canonical_utc_timestamp_json(timestamptz)",
        "wp06_sha256_canonical_json(json)",
        "wp06_compact_canonical_json(json)",
    ):
        assert (
            f"REVOKE EXECUTE ON FUNCTION public.{signature} "
            "FROM crypidian_learning_shadow_integrity_guard"
        ) in normalized_downgrade
    assert "FOR UPDATE TO crypidian_scorer_deployment_guard" in sql
