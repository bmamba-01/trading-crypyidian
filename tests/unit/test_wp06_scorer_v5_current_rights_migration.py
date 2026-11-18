"""Kiểm migration v5 cho recheck current-rights scorer."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/e2f4a6c8b0d2_wp06_scorer_v5_current_rights.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration v5 current-rights scorer."
    spec = importlib.util.spec_from_file_location("wp06_scorer_v5_current_rights", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_reader_adds_v5_without_changing_v1_to_v3_and_reverses() -> None:
    migration = _migration()
    legacy = migration._LEGACY_MANIFEST_VERSION_GATE + "\n" + migration._LEGACY_MANIFEST_BRANCH

    current = migration._rewrite_manifest_source_refs(legacy, forward=True)

    assert migration._LICENSED_MANIFEST_VERSION_GATE in current
    assert migration._LICENSED_MANIFEST_BRANCH in current
    assert "dataset-manifest-v1" in current
    assert "dataset-manifest-v2" in current
    assert "dataset-manifest-v3" in current
    assert "dataset-manifest-v5" in current
    assert migration._rewrite_manifest_source_refs(current, forward=False) == legacy


def test_scope_check_limits_deployment_recheck_to_internal_deployment_callers() -> None:
    migration = _migration()
    legacy = migration._LEGACY_SCOPE_CALLER_GUARD + "\n" + migration._TRAINING_SCOPE_FALLBACK
    updated = migration._rewrite_scope_check(legacy, forward=True)

    assert "OR (target_access_kind = 'deployment_recheck' AND session_user NOT IN (" in updated
    assert "'report_read', 'deployment_recheck') THEN" in updated
    assert migration._TRAINING_SCOPE_FALLBACK in updated
    assert "target_access_kind = 'report_read'" in updated
    mapping = updated.split("use_outputs := CASE", 1)[1].split("use_scope :=", 1)[0]
    assert "target_access_kind IN ('evaluation', 'report_read')" in mapping
    assert '"aggregate_report"' in mapping
    assert '"aggregate_reporting"' in mapping
    assert '"trained_model"' in mapping
    assert '"model_training"' in mapping
    assert (
        "target_access_kind IN ('evaluation', 'report_read', 'deployment_recheck')" not in mapping
    )
    assert "crypidian_admin_web" in updated
    assert "crypidian_trading_runtime" in updated
    assert "crypidian_market_signal_worker" in updated
    assert "crypidian_migrator" not in updated
    assert "crypidian_app" not in updated
    deployment_callers = updated.split("target_access_kind = 'deployment_recheck'", 1)[1]
    assert "crypidian_learning_evaluator" not in deployment_callers.split(") THEN", 1)[0]
    assert migration._rewrite_scope_check(updated, forward=False) == legacy


def test_scope_check_guard_rewrite_preserves_database_whitespace_exactly() -> None:
    migration = _migration()
    legacy = """            IF true THEN
               OR target_access_kind NOT IN (
                    'normalization', 'evaluation',
                    'training', 'report_read') THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CALLER_DENIED';
            END IF;"""

    updated = migration._extend_scope_caller_guard(legacy)

    assert (
        "OR target_access_kind NOT IN (\n                    'normalization', 'evaluation',\n"
        "                    'training', 'report_read', 'deployment_recheck') THEN"
    ) in updated
    assert migration._revert_scope_caller_guard(updated) == legacy


def test_deployment_recheck_cannot_be_mapped_to_aggregate_report_scope() -> None:
    migration = _migration()
    legacy = migration._LEGACY_SCOPE_CALLER_GUARD + "\n" + migration._TRAINING_SCOPE_FALLBACK
    aggregate_only = legacy.replace(
        "target_access_kind IN ('evaluation', 'report_read')",
        "target_access_kind IN ('evaluation', 'report_read', 'deployment_recheck')",
        1,
    )

    with pytest.raises(RuntimeError, match="MAPPING_UNEXPECTED"):
        migration._rewrite_scope_check(aggregate_only, forward=True)


def test_scope_check_rewrite_uses_regprocedure_signature_in_upgrade_and_downgrade() -> None:
    migration = _migration()
    syntax = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(syntax)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_rewrite_function"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "SCOPE_CHECK_SIGNATURE"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Name)
        and node.args[1].id == "_rewrite_scope_check"
    ]

    assert migration.SCOPE_CHECK_SIGNATURE.endswith("(uuid,text,jsonb,uuid,text,text,uuid,boolean)")
    assert len(calls) == 2


def test_current_rights_recheck_locks_v5_providers_and_runs_b7_scope_check() -> None:
    migration = _migration()
    legacy = migration._LEGACY_SOURCE_LOCK_LOOP + "\n" + migration._LEGACY_INDEX_CHECK

    current = migration._rewrite_current_rights(legacy, forward=True)

    assert migration._V5_SOURCE_LOCK_LOOP in current
    assert migration._V5_SCOPE_CHECK in current
    assert "source_use_lineage" in current
    assert "wp06_check_replay_training_scope" in current
    assert "deployment_recheck" in current
    assert "CRYPIDIAN_FIRST_PARTY_QA" in current
    assert "BINANCE_USDM_REST" in current
    assert "persist_receipts" not in current
    assert migration._rewrite_current_rights(current, forward=False) == legacy


def test_v5_migration_is_forward_only_from_license_fingerprint_head() -> None:
    migration = _migration()

    assert migration.revision == "e2f4a6c8b0d2"
    assert migration.down_revision == "fb4d6e8a0c2f"


def test_v5_current_rights_owner_can_read_manifest_lineage_then_revoke_on_downgrade() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]
    downgrade = source.split("def downgrade() -> None:", 1)[1]

    assert "GRANT SELECT (manifest) ON public.dataset_manifest TO {RIGHTS_ROLE}" in upgrade
    assert "REVOKE SELECT (manifest) ON public.dataset_manifest FROM {RIGHTS_ROLE}" in downgrade


def test_manifest_comparator_can_read_only_index_identity_under_workspace_rls() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]
    downgrade = source.split("def downgrade() -> None:", 1)[1]

    assert "dataset_manifest_source_ref_comparator_select" in upgrade
    assert "TO {COMPARATOR_ROLE} USING (workspace_id = " in upgrade
    assert "GRANT SELECT (workspace_id,manifest_id) " in upgrade
    assert "REVOKE SELECT (workspace_id,manifest_id) " in downgrade


def test_migration_rejects_unexpected_preimages() -> None:
    migration = _migration()

    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        migration._rewrite_manifest_source_refs("unexpected", forward=True)
    with pytest.raises(RuntimeError, match="UNEXPECTED"):
        migration._rewrite_scope_check("unexpected", forward=True)
    with pytest.raises(RuntimeError, match="PREIMAGE_UNEXPECTED"):
        migration._rewrite_current_rights("unexpected", forward=True)


@pytest.mark.parametrize(
    ("forward", "source", "transform"),
    [
        (
            True,
            "legacy source",
            lambda value, *, forward: "current source" if forward else "legacy source",
        ),
        (
            False,
            "current source",
            lambda value, *, forward: "current source" if forward else "legacy source",
        ),
    ],
)
def test_rewrite_function_accepts_exact_upgrade_and_downgrade_postimages(
    monkeypatch: pytest.MonkeyPatch,
    forward: bool,
    source: str,
    transform,
) -> None:
    migration = _migration()

    class Result:
        def __init__(self, row: tuple[str, str]):
            self.row = row

        def one(self) -> tuple[str, str]:
            return self.row

    class Connection:
        def __init__(self) -> None:
            self.definition = f"CREATE FUNCTION sample() AS $body${source}$body$"
            self.source = source

        def execute(self, statement, _params):
            return Result((self.definition, self.source))

        def exec_driver_sql(self, definition: str) -> None:
            self.definition = definition
            self.source = "current source" if forward else "legacy source"

    connection = Connection()
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

    migration._rewrite_function("sample()", transform, forward=forward)

    assert connection.source == ("current source" if forward else "legacy source")


def test_rewrite_function_escapes_literal_percent_for_psycopg_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    original = "SELECT 'https://%/license' AS evidence_ref /* old */"
    updated = "SELECT 'https://%/license' AS evidence_ref /* new */"

    class Result:
        def __init__(self, row: tuple[str, str]):
            self.row = row

        def one(self) -> tuple[str, str]:
            return self.row

    class Connection:
        def __init__(self) -> None:
            self.definition = f"CREATE FUNCTION sample() AS $body${original}$body$"
            self.source = original
            self.raw_definition = ""

        def execute(self, _statement, _params):
            return Result((self.definition, self.source))

        def exec_driver_sql(self, definition: str) -> None:
            self.raw_definition = definition
            self.definition = definition.replace("%%", "%")
            self.source = updated

    def transform(source: str, *, forward: bool) -> str:
        previous, next_source = (original, updated) if forward else (updated, original)
        if source.count(previous) != 1:
            raise RuntimeError("Không khớp source test.")
        return source.replace(previous, next_source, 1)

    connection = Connection()
    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

    migration._rewrite_function("sample()", transform, forward=True)

    assert "https://%%/license" in connection.raw_definition
    assert "https://%/license" in connection.definition
    assert connection.source == updated
