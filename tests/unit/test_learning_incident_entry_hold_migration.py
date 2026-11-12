"""Kiểm tra DDL ledger incident và ranh giới quyền projection runtime."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from trading_bot.adapters.database import models

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f5b7d9e1a3c6_wp06_learning_incident_entry_hold.py"


def _migration():
    assert MIGRATION.exists(), "Chưa có migration ledger incident"
    spec = importlib.util.spec_from_file_location("incident_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements():
    migration = _migration()
    statements = []
    operations = SimpleNamespace(
        execute=lambda sql: statements.append(str(sql)),
        create_table=lambda *args, **kwargs: None,
        create_index=lambda *args, **kwargs: None,
    )
    migration.op = operations
    migration.upgrade()
    return "\n".join(statements)


def test_incident_model_registers_only_metadata_and_exact_workspace_lineage() -> None:
    assert hasattr(models, "LearningIncidentHoldEventRecord"), "Chưa đăng ký model ledger incident"
    table = models.LearningIncidentHoldEventRecord.__table__
    assert table.name == "learning_incident_entry_hold_event"
    assert not {"features", "labels", "samples", "test_payload", "raw_source"} & set(table.c.keys())
    for columns in (
        {"workspace_id", "proposal_id", "revision"},
        {"workspace_id", "idempotency_key"},
        {"workspace_id", "previous_event_id"},
    ):
        assert any(
            isinstance(item, UniqueConstraint)
            and {column.name for column in item.columns} == columns
            for item in table.constraints
        )
    references = [
        {element.target_fullname for element in item.elements}
        for item in table.constraints
        if isinstance(item, ForeignKeyConstraint)
    ]
    assert {"learning_retrain_proposal.workspace_id", "learning_retrain_proposal.id"} in references
    assert {
        "learning_drift_assessment.workspace_id",
        "learning_drift_assessment.id",
        "learning_drift_assessment.content_hash",
    } in references
    assert {"user_membership.workspace_id", "user_membership.id"} in references
    checks = " ".join(
        str(item.sqltext) for item in table.constraints if isinstance(item, CheckConstraint)
    )
    assert "revision = 1" in checks
    assert "revision = 2" in checks
    assert "previous_event_id IS NULL" in checks
    assert "previous_event_id IS NOT NULL" in checks


def test_migration_builds_forced_rls_and_runtime_execute_only_projection() -> None:
    sql = _statements()
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT" in sql
    assert "OWNER TO crypidian_learning_incident_reader" in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_learning_incident_hold_active(uuid) TO "
        "crypidian_trading_runtime" in sql
    )
    assert (
        "GRANT SELECT ON TABLE public.learning_incident_entry_hold_event TO "
        "crypidian_trading_runtime" not in sql
    )
    assert "pg_catalog.current_setting('app.workspace_id', true)" in sql
    projection = sql.split("CREATE FUNCTION public.wp06_learning_incident_hold_active", 1)[1]
    assert "pg_catalog.current_setting('transaction_isolation')" in projection
    assert "WP06_INCIDENT_READ_COMMITTED_REQUIRED" in projection


def test_guard_requires_canonical_hash_mfa_actor_and_terminal_chain() -> None:
    sql = _statements()
    for marker in (
        "WP06_INCIDENT_CANONICAL_INVALID",
        "WP06_INCIDENT_MFA_REQUIRED",
        "WP06_INCIDENT_ACTOR_INVALID",
        "WP06_INCIDENT_PREDECESSOR_MISMATCH",
        "WP06_INCIDENT_STALE_REVISION",
        "WP06_INCIDENT_IMMUTABLE",
    ):
        assert marker in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "BEFORE INSERT" in sql
    assert "binding.role IN ('owner','risk_admin')" in sql


def test_trigger_checks_totp_freshness_after_locks_and_release_takes_only_l1() -> None:
    sql = _statements()
    guard = sql.split("CREATE FUNCTION public.wp06_learning_incident_guard()", 1)[1].split(
        "CREATE FUNCTION public.wp06_learning_incident_immutable()", 1
    )[0]

    assert "now_at timestamptz := pg_catalog.clock_timestamp()" not in guard
    assert guard.index("IF NEW.action = 'HOLD' THEN") < guard.index(
        "wp06_lock_learning_incident_scope"
    )
    release_start = guard.index("ELSIF NEW.action = 'RELEASE' THEN")
    release_end = guard.index(
        "\n            ELSE\n                RAISE EXCEPTION 'WP06_INCIDENT_ACTION_INVALID'",
        release_start,
    )
    release_branch = guard[release_start:release_end]
    assert "workspace-settings:" in release_branch
    assert "wp06_lock_learning_incident_scope" not in release_branch
    assert (
        guard.index("wp06_learning_incident_eligible_target")
        < guard.rindex("pg_catalog.clock_timestamp()")
        < guard.rindex("WP06_INCIDENT_MFA_REQUIRED")
    )


def test_reason_database_checks_strip_python_whitespace_not_only_ascii_space() -> None:
    sql = _statements()
    table_check = sql.split(
        "CONSTRAINT ck_learning_incident_entry_hold_event_learning_incident_109a", 1
    )[1].split("\n)", 1)[0]
    guard = sql.split("CREATE FUNCTION public.wp06_learning_incident_guard()", 1)[1].split(
        "CREATE FUNCTION public.wp06_learning_incident_immutable()", 1
    )[0]

    assert "pg_catalog.chr(9)" in table_check
    assert "pg_catalog.chr(133)" in table_check
    assert "pg_catalog.chr(12288)" in table_check
    assert table_check.count("pg_catalog.chr(9)") == 2
    assert table_check.count("pg_catalog.chr(133)") == 2
    assert guard.count("pg_catalog.chr(9)") == 2
    assert guard.count("pg_catalog.chr(12288)") == 2
    model_check = next(
        item.sqltext
        for item in models.LearningIncidentHoldEventRecord.__table__.constraints
        if isinstance(item, CheckConstraint)
        and item.name
        and item.name.endswith("learning_incident_audit_valid")
    )
    model_sql = str(model_check)
    assert model_sql.count("pg_catalog.chr(9)") == 2
    assert model_sql.count("pg_catalog.chr(133)") == 2


def test_hold_target_checks_all_members_and_current_authoritative_gateway() -> None:
    sql = _statements()
    for required in (
        "learning_drift_assessment_member",
        "learning_shadow_observation",
        "active_registry_id",
        "active_registry_revision",
        "active_artifact_hash",
        "active_pointer_version",
        "wp06_resolve_scorer_runtime_target",
        "registry.artifact_hash",
        "WP06_INCIDENT_MIXED_INCUMBENT",
        "WP06_INCIDENT_LATEST_ASSESSMENT_REQUIRED",
        "WP06_INCIDENT_TARGET_STALE",
    ):
        assert required in sql
    assert "scorer_artifact_hash = incumbent_artifact_hash" not in sql
    assert sql.index("workspace-settings:") < sql.index("learning-scoring-deployment:")
    assert sql.index("learning-scoring-deployment:") < sql.index("learning-model-registry:")


def test_migration_extends_current_head_and_refuses_populated_downgrade() -> None:
    migration = _migration()
    assert migration.down_revision == "f4a6c8e0b2d5"
    migration.op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(
            execute=lambda statement: SimpleNamespace(scalar_one=lambda: 1)
        )
    )
    with pytest.raises(RuntimeError, match="WP06_INCIDENT_POPULATED_DOWNGRADE_DENIED"):
        migration.downgrade()
