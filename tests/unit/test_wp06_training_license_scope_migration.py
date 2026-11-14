"""Kiểm schema và ranh giới quyền của receipt TRAINING theo replay bundle."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from trading_bot.adapters.database import models

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/b7d9f1a3c5e8_wp06_training_license_scope.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration exact-scope TRAINING cho replay bundle"
    spec = importlib.util.spec_from_file_location("wp06_training_license_scope", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    statements: list[str] = []
    module = _migration()

    def capture_create_table(*args, **kwargs) -> None:
        statements.extend(str(getattr(item, "sqltext", item)) for item in args)

    module.op = SimpleNamespace(
        execute=lambda sql: statements.append(str(sql)),
        create_table=capture_create_table,
        create_index=lambda *args, **kwargs: None,
        drop_index=lambda *args, **kwargs: None,
        drop_table=lambda *args, **kwargs: None,
        f=lambda name: name,
    )
    module.upgrade()
    return "\n".join(statements)


def _downgrade_sql() -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(
        execute=lambda sql: statements.append(str(sql)),
        get_bind=lambda: SimpleNamespace(
            execute=lambda _statement: SimpleNamespace(scalar_one=lambda: False)
        ),
        drop_index=lambda *args, **kwargs: statements.append("DROP INDEX"),
        drop_table=lambda *args, **kwargs: statements.append("DROP TABLE"),
    )
    module.downgrade()
    return "\n".join(statements)


def test_receipt_model_records_only_exact_metadata_per_sample_child_and_access() -> None:
    assert hasattr(models, "LearningReplayTrainingReceiptRecord")
    table = models.LearningReplayTrainingReceiptRecord.__table__
    assert table.name == "learning_replay_training_receipt"
    assert not {
        "features",
        "labels",
        "samples",
        "canonical_evidence",
        "raw_source",
        "license_document",
    } & set(table.c.keys())
    for columns in (
        {
            "workspace_id",
            "access_kind",
            "access_id",
            "normalized_dataset_id",
            "sample_id",
            "child_dataset_hash",
        },
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
    assert {
        "learning_normalized_dataset.workspace_id",
        "learning_normalized_dataset.manifest_hash",
        "learning_normalized_dataset.fold_plan_hash",
        "learning_normalized_dataset.id",
    } in references
    checks = " ".join(
        str(item.sqltext) for item in table.constraints if isinstance(item, CheckConstraint)
    )
    for required in (
        "access_kind IN ('normalization', 'evaluation', 'training')",
        "source_id",
        "provider",
        "symbol",
        "interval",
        "data_start < data_end",
        "scope_hash",
        "current_policy_fingerprint",
        "evidence_digest",
    ):
        assert required in checks


def test_migration_chains_after_license_digest_and_installs_narrow_append_only_gate() -> None:
    migration = _migration()
    sql = _upgrade_sql()
    assert migration.down_revision == "a4c6e8f0b2d5"
    assert sql.count("'BACKTEST_RESEARCH'") == 8
    assert "'backtest_research'" not in sql
    for required in (
        "FORCE ROW LEVEL SECURITY",
        "BEFORE UPDATE OR DELETE",
        "learning_replay_training_receipt",
        "wp06_assert_training_source_rights",
        "wp06_claim_learning_training_batch",
        "wp06_authorize_replay_training_bundle",
        "source_use_lineage",
        "dataset-manifest-v5",
        "source_use_lineage_set_hash",
        "wp06_sha256_canonical_json",
        "public.wp06_license_canonical_json(",
        "child_row.manifest_json - 'dataset_hash'",
        "child_manifest.manifest_json - 'dataset_hash'",
        "WP06_TRAINING_REPLAY_LEGACY_LINEAGE_DENIED",
        "WP06_TRAINING_REPLAY_SOURCE_FREE_FORGED",
        "WP06_TRAINING_REPLAY_MARKET_BAR_SET_MISMATCH",
        "WP06_TRAINING_REPLAY_CHILD_COUNT_MISMATCH",
        "substring(ids.value FROM 19)",
        "source_license_evidence",
        "license_scope_hash",
        "current_policy_fingerprint ~ '^[0-9a-f]{64}$'",
        "target_operation text",
        "target_access_kind text",
        "persist_receipts boolean",
        "OWNER TO crypidian_learning_source_rights_reader",
        "GRANT EXECUTE ON FUNCTION public.wp06_claim_learning_training_batch(uuid,uuid) "
        "TO crypidian_trainer_worker",
        "FOR SELECT TO crypidian_learning_source_rights_reader USING (workspace_id = NULLIF",
        "event_type = 'research.rights_changed.v1'",
        "consumer_name = 'wp06.dataset-rights-eligibility.v1'",
        "receipt.evidence_json IS NOT DISTINCT FROM receipt.canonical_evidence::jsonb",
        "lineage.value->>'interval' = component.value->>'interval'",
        "policy.evidence_ref, policy.license_scope",
        "pending.policy_id = evidence.policy_id",
        "pending.state = 'rights_pending'",
        "wp06_candidate_builder_rights_receipt_read",
        "FOR SELECT\n        TO crypidian_candidate_builder",
        "GRANT SELECT ON TABLE public.backtest_run_dataset_rights TO crypidian_candidate_builder",
        "child_row.manifest_json->>'purpose' IS DISTINCT FROM 'BACKTEST_RESEARCH'",
        "child_manifest.manifest_json->>'purpose' IS DISTINCT FROM 'BACKTEST_RESEARCH'",
        'child_dataset_hash text, "interval" text',
    ):
        assert required in sql
    assert "receipt.canonical_evidence = public.wp06_compact_canonical_json(" in sql
    assert "receipt.evidence_json::json)" not in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "expected_receipt_count := expected_receipt_count + 1" in sql
    assert (
        "GRANT SELECT ON TABLE public.source_license_evidence TO crypidian_trainer_worker"
        not in sql
    )
    assert (
        "GRANT SELECT ON TABLE public.source_license_evidence TO crypidian_learning_evaluator"
        not in sql
    )
    assert (
        "GRANT SELECT ON TABLE public.source_license_evidence TO crypidian_candidate_builder"
        not in sql
    )


def test_downgrade_restores_previous_functions_and_revokes_only_new_scope_grants() -> None:
    sql = _downgrade_sql()
    assert "ALTER FUNCTION public.wp06_assert_training_source_rights_pre_license_scope" in sql
    assert "RENAME TO wp06_assert_training_source_rights" in sql
    assert "wp06_claim_learning_training_batch_pre_license_scope" in sql
    assert "RENAME TO wp06_claim_learning_training_batch" in sql
    assert (
        "REVOKE SELECT (workspace_id, id, manifest_hash, manifest, canonical_manifest, "
        "split_policy_version) ON public.dataset_manifest FROM "
        "crypidian_learning_source_rights_reader"
    ) in sql
    assert (
        "REVOKE SELECT ON public.source_policy_revision FROM "
        "crypidian_learning_source_rights_reader" in sql
    )
    assert "ALTER FUNCTION public.wp06_license_canonical_json(jsonb) OWNER TO" not in sql
    assert "REVOKE EXECUTE ON FUNCTION public.wp06_license_source_policy_fingerprint" in sql
    assert "DROP POLICY wp06_candidate_builder_rights_receipt_read" in sql
    assert (
        "REVOKE SELECT ON TABLE public.backtest_run_dataset_rights FROM crypidian_candidate_builder"
    ) in sql
