import ast
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from trading_bot.adapters.database.models import (
    Base,
    LearningDriftAssessmentMemberRecord,
    LearningDriftAssessmentRecord,
    LearningRetrainProposalRecord,
    LearningShadowObservationRecord,
)

ROOT = Path(__file__).resolve().parents[2]


def test_shadow_drift_tables_are_metadata_only_and_workspace_scoped() -> None:
    assert {
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
        "learning_retrain_proposal",
    } <= set(Base.metadata.tables)

    for record in (
        LearningShadowObservationRecord,
        LearningDriftAssessmentRecord,
        LearningDriftAssessmentMemberRecord,
        LearningRetrainProposalRecord,
    ):
        table = record.__table__
        assert table.c.workspace_id.foreign_keys or any(
            "workspace.id" in {element.target_fullname for element in constraint.elements}
            for constraint in table.foreign_key_constraints
        )
        assert not {
            "features",
            "warmup",
            "canonical_json",
            "source_refs",
            "payload",
            "job_id",
            "order_id",
        } & set(table.c.keys())


def test_shadow_drift_membership_and_idempotency_are_exact() -> None:
    observation = LearningShadowObservationRecord.__table__
    assessment = LearningDriftAssessmentRecord.__table__
    member = LearningDriftAssessmentMemberRecord.__table__
    proposal = LearningRetrainProposalRecord.__table__

    assert any(
        isinstance(item, UniqueConstraint)
        and {column.name for column in item.columns} == {"workspace_id", "idempotency_key"}
        for item in observation.constraints
    )
    assert any(
        isinstance(item, UniqueConstraint)
        and item.name == "uq_learning_shadow_observation_logical_identity"
        and {column.name for column in item.columns}
        == {
            "workspace_id",
            "incumbent_decision_id",
            "challenger_registry_id",
            "challenger_revision",
            "snapshot_hash",
        }
        for item in observation.constraints
    )
    assert any(
        isinstance(item, UniqueConstraint)
        and {column.name for column in item.columns} == {"workspace_id", "idempotency_key"}
        for item in assessment.constraints
    )
    assert any(
        isinstance(item, UniqueConstraint)
        and {column.name for column in item.columns} == {"workspace_id", "idempotency_key"}
        for item in proposal.constraints
    )
    assert any(
        isinstance(item, ForeignKeyConstraint)
        and {element.target_fullname for element in item.elements}
        >= {
            "learning_drift_assessment.workspace_id",
            "learning_drift_assessment.id",
        }
        for item in member.foreign_key_constraints
    )
    assert any(
        isinstance(item, ForeignKeyConstraint)
        and {
            "learning_drift_assessment.workspace_id",
            "learning_drift_assessment.id",
            "learning_drift_assessment.content_hash",
        }
        <= {element.target_fullname for element in item.elements}
        for item in proposal.foreign_key_constraints
    )
    assert "performance_status" in assessment.c
    assert "membership_digest" in assessment.c


def test_drift_assessment_pins_one_challenger_cohort_or_empty_scope() -> None:
    assessment = LearningDriftAssessmentRecord.__table__

    assert assessment.c.cohort_registry_id.nullable
    assert assessment.c.cohort_revision.nullable
    assert any(
        isinstance(item, ForeignKeyConstraint)
        and {
            "workspace_id",
            "cohort_registry_id",
        }
        == {element.parent.name for element in item.elements}
        and {
            "learning_model_registry.workspace_id",
            "learning_model_registry.id",
        }
        == {element.target_fullname for element in item.elements}
        for item in assessment.foreign_key_constraints
    )
    scope_guard = next(
        item
        for item in assessment.constraints
        if isinstance(item, CheckConstraint)
        and item.name.endswith("_learning_drift_assessment_cohort_scope_valid")
    )
    assert "member_count = 0" in str(scope_guard.sqltext)
    assert "cohort_registry_id IS NULL" in str(scope_guard.sqltext)
    assert "cohort_revision IS NULL" in str(scope_guard.sqltext)
    assert "cohort_registry_id IS NOT NULL" in str(scope_guard.sqltext)
    assert "cohort_revision >= 1" in str(scope_guard.sqltext)


def test_shadow_drift_migration_enforces_rls_rights_immutability_and_safe_downgrade() -> None:
    migration_path = ROOT / "migrations/versions/d3a5c7e9b1d4_wp06_shadow_drift_ledger.py"
    source = migration_path.read_text(encoding="utf-8")

    assert 'revision: str = "d3a5c7e9b1d4"' in source
    assert 'down_revision: str | None = "d2b4c6e8f0a1"' in source
    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
        "learning_retrain_proposal",
    ):
        assert f'EXISTS (SELECT 1 FROM public."{table}")' in source
    assert 'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY' in source
    assert 'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY' in source
    assert "CREATE CONSTRAINT TRIGGER" in source
    assert "DEFERRABLE INITIALLY DEFERRED" in source
    assert "membership_digest" in source
    assert "WP06_SHADOW_DRIFT_IMMUTABLE" in source
    assert "WP06_SHADOW_DRIFT_MEMBERSHIP_INVALID" in source
    assert "WP06_SHADOW_OBSERVATION_LINEAGE_INVALID" in source
    assert "market-bar-event:" in source
    assert "wp06_shadow_current_paper_rights_hash" in source
    assert "wp06_shadow_decision_source_rights_supported" in source
    assert "wp06_shadow_current_paper_rights_receipt" in source
    assert "wp06_canonical_source_cost" in source
    assert "IF NEW.rights_eligible THEN" in source
    assert "paper_demo" in source
    assert "rights_policy_sequence" in source
    assert "rights_known_at" in source
    assert "performance_status = 'inconclusive'" in source
    assert "actual_cohort_count" in source
    assert "actual_digest <> actual_cohort_digest" in source
    assert "WP06_SHADOW_DRIFT_POLICY_INVALID" in source
    assert "WP06_SHADOW_DATABASE_TIME_REQUIRED" in source
    assert "learning_model_active_pointer_shadow_lock" in source
    assert "'active-pointer'" in source
    assert "pg_catalog.chr(58)" in source
    assert 'GRANT SELECT, INSERT ON TABLE public."{table}" TO {OBSERVER}' in source
    assert 'GRANT SELECT ON TABLE public."{table}" TO {ADMIN}' in source
    assert "GRANT INSERT ON TABLE public.bot_decision" not in source
    assert "GRANT INSERT ON TABLE public.learning_training_job" not in source
    assert "GRANT UPDATE ON TABLE public.learning_model_active_pointer" not in source


def test_shadow_rights_accept_only_live_usdm_or_verified_backfill_bars() -> None:
    migration_path = ROOT / "migrations/versions/d3a5c7e9b1d4_wp06_shadow_drift_ledger.py"
    source = migration_path.read_text(encoding="utf-8")

    normalized_source = " ".join(source.split())
    assert "wp06_shadow_market_source_rights_matches" in normalized_source
    assert "bar.source LIKE 'BINANCE_RB_%'" not in normalized_source
    assert "WHEN 'BINANCE_USDM' THEN target_rights_source = 'BINANCE_USDM_REST'" in (
        normalized_source
    )
    assert (
        "WHEN 'BINANCE_USDM_REST_BACKFILL' THEN target_rights_source = 'BINANCE_USDM_REST'"
        in normalized_source
    )


def test_shadow_observer_has_independent_nologin_role_secret_and_column_grants() -> None:
    migration_path = ROOT / "migrations/versions/d3a5c7e9b1d4_wp06_shadow_drift_ledger.py"
    migration_source = migration_path.read_text(encoding="utf-8")
    role_source = (ROOT / "scripts/provision_database_roles.py").read_text(encoding="utf-8")
    secret_source = (ROOT / "scripts/prepare_local_secrets.py").read_text(encoding="utf-8")
    compose_source = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "crypidian_learning_shadow_observer" in migration_source
    assert "GRANT SELECT ({selected}) ON TABLE public.{table} TO {OBSERVER}" in migration_source
    assert (
        "GRANT EXECUTE ON FUNCTION public.wp06_shadow_current_paper_rights_receipt(uuid)"
        in migration_source
    )
    assert "crypidian_learning_shadow_observer" in role_source
    assert "database_learning_shadow_observer_password" in role_source
    assert '"learning_shadow_observer"' in secret_source
    assert "database_learning_shadow_observer_password:" in compose_source
    assert "- database_learning_shadow_observer_password" in compose_source
    assert "learning-shadow-observer:" in compose_source
    assert "TRADING_BOT_PROCESS_ROLE: learning-shadow-observer" in compose_source
    assert 'command: ["python", "-m", "trading_bot.cli.learning_shadow", "run"]' in compose_source


def test_shadow_drift_migration_declares_safe_observer_columns() -> None:
    migration_path = ROOT / "migrations/versions/d3a5c7e9b1d4_wp06_shadow_drift_ledger.py"
    module = ast.parse(migration_path.read_text(encoding="utf-8"))
    column_map = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "OBSERVER_COLUMNS"
            for target in node.targets
        )
    )

    assert column_map["feature_snapshot"] == (
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
    )
    assert "features" not in column_map["feature_snapshot"]
    assert "warmup" not in column_map["feature_snapshot"]
    assert "payload" not in column_map["bot_decision"]
    assert "source_refs" not in column_map["shared_signal_evidence"]
    assert "reason" not in column_map["learning_model_registry_event"]
    assert "actor_membership_id" not in column_map["learning_model_registry_event"]
    assert "source_policy_revision" not in column_map
    assert "source_policy_observation" not in column_map
    assert column_map["learning_training_artifact"] == (
        "workspace_id",
        "id",
        "feature_schema_hash",
    )
