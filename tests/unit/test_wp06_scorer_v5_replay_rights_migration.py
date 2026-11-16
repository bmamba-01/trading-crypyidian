"""Lát B: recheck/deployment scorer đọc manifest v5 lineage replay và scope receipt mới.

Quyết định owner 16/11/2026 ("Làm tiếp lát tiếp theo"), theo spec
`2026-11-13-wp06-source-use-scope-consumers-design.md` mục Scorer TEST/deployment.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.adapters.database.replay_training_rights import (
    ReplayTrainingOperation,
    ReplayTrainingRightsReceipt,
)

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations/versions"
MIGRATION = VERSIONS / "d4f6a8b0c2e4_wp06_scorer_v5_replay_rights.py"


def _load(path: Path, name: str):
    assert path.exists(), f"Thiếu migration {path.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration():
    return _load(MIGRATION, "wp06_scorer_v5_replay_rights")


def _statements(direction: str) -> list[str]:
    module = _migration()
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    getattr(module, direction)()
    return statements


def _installed_gate_source() -> str:
    """Nguồn gate sau b7d9f1a3c5e8 + c2e4a6b8d0f1 (đúng thân hàm đang cài)."""
    gate = (VERSIONS / "b7d9f1a3c5e8_wp06_training_license_scope.py").read_text("utf-8")
    caller = _load(
        VERSIONS / "c2e4a6b8d0f1_wp06_scorer_test_proof_builder_evaluation.py",
        "wp06_builder_evaluation",
    )
    return gate.replace(caller.PREVIOUS_CALLER_CHECK, caller.CORRECTED_CALLER_CHECK, 1)


def _installed_recheck_source() -> str:
    """Nguồn recheck sau d7f9a1b3c5e8 + patch QA của 8ac4e2f6b0d1."""
    source = (VERSIONS / "d7f9a1b3c5e8_wp06_scorer_current_rights.py").read_text("utf-8")
    qa = _load(VERSIONS / "8ac4e2f6b0d1_wp06_first_party_qa_source.py", "wp06_qa_source")
    for previous, replacement, count in qa.CURRENT_RIGHTS_PATCHES:
        assert source.count(previous) >= count
        source = source.replace(previous, replacement, count)
    return source


def _receipt() -> ReplayTrainingRightsReceipt:
    now = datetime(2026, 11, 16, 1, 2, 3, tzinfo=UTC)
    return ReplayTrainingRightsReceipt(
        workspace_id=uuid4(),
        bundle_hash="a" * 64,
        child_dataset_hash="b" * 64,
        interval="1h",
        symbol="BTCUSDT",
        bars_content_hash="c" * 64,
        product="E2E_FIXTURE",
        venue="E2E_FIXTURE",
        contract="E2E_FIXTURE",
        data_start=now - timedelta(days=1),
        data_end=now,
        policy_id="policy",
        policy_fingerprint="d" * 64,
        bundle_policy_fingerprint="e" * 64,
        current_policy_fingerprint="d" * 64,
        terms_url="https://example.test/terms",
        verified_at=now,
        checked_at=now,
        valid_until=now + timedelta(minutes=5),
        license_evidence_id=uuid4(),
        evidence_digest="f" * 64,
        license_scope_hash="1" * 64,
        scope_hash="2" * 64,
        operation=ReplayTrainingOperation.EVALUATION,
        provider="CRYPIDIAN_FIRST_PARTY_QA",
    )


def test_migration_follows_builder_evaluation_head_and_pins_every_definition() -> None:
    module = _migration()
    assert module.revision == "d4f6a8b0c2e4"
    assert module.down_revision == "c2e4a6b8d0f1"
    caller = _load(
        VERSIONS / "c2e4a6b8d0f1_wp06_scorer_test_proof_builder_evaluation.py",
        "wp06_builder_evaluation_hashes",
    )
    hashes = module.FUNCTION_DEFINITION_HASHES
    assert (
        hashes[module.GATE_SIGNATURE]["preimage"]
        == caller.FUNCTION_DEFINITION_HASHES[caller.SIGNATURE]["postimage"]
    )
    for values in hashes.values():
        assert all(re.fullmatch("[0-9a-f]{64}", value) for value in values.values())
    for signature in (module.SOURCE_REFS_SIGNATURE, module.RECHECK_SIGNATURE):
        assert hashes[signature]["preimage"] != hashes[signature]["postimage"]


def test_every_patch_matches_installed_source_exactly_once() -> None:
    module = _migration()
    gate = _installed_gate_source()
    recheck = _installed_recheck_source()
    # Header do pg_get_functiondef sinh; phần thân phải khớp nguyên văn đúng một lần.
    assert gate.count(module.HELPER_PATCHES[1][0]) == 1
    for previous, replacement in module.SOURCE_REFS_PATCHES:
        assert recheck.count(previous) == 1
        assert recheck.count(replacement) == 0
    assert recheck.count(module.LEGACY_TRAINING_SCOPE_STATEMENT) == 1
    assert module.CURRENT_TRAINING_SCOPE_STATEMENT not in recheck


def test_helper_only_serves_evaluation_and_is_executable_only_by_scorer_rights_owner() -> None:
    module = _migration()
    sql = "\n".join(_statements("upgrade"))
    assert module.HELPER_PATCHES[1][1] == "IF target_operation IS DISTINCT FROM 'evaluation' THEN"
    assert "CREATE FUNCTION public.wp06_scorer_replay_evaluation_scope(" in sql
    assert (
        f"ALTER FUNCTION {module.HELPER_SIGNATURE} OWNER TO "
        "crypidian_learning_source_rights_reader" in sql
    )
    assert f"REVOKE ALL ON FUNCTION {module.HELPER_SIGNATURE} FROM PUBLIC" in sql
    grants = [line.strip() for line in sql.splitlines() if "GRANT " in line]
    assert grants == [
        f"GRANT EXECUTE ON FUNCTION {module.HELPER_SIGNATURE} TO "
        "crypidian_scorer_deployment_rights_reader"
    ]
    assert "ON TABLE" not in sql
    assert "CREATE POLICY" not in sql


def test_current_scope_statement_matches_python_receipt_scope_payload() -> None:
    module = _migration()
    statement = module.CURRENT_TRAINING_SCOPE_STATEMENT
    keys = re.findall(r"^\s+'([a-z_]+)', ", statement, flags=re.MULTILINE)
    payload = _receipt().scope_payload
    # compact canonical SQL không tự sắp key: thứ tự literal phải là thứ tự sort_keys Python.
    assert keys == sorted(payload)
    assert "'operation', 'evaluation'" in statement
    assert "wp06_scorer_replay_evaluation_scope(" in statement
    assert 'ORDER BY scope.child_dataset_hash COLLATE "C"' in statement
    for name in ("data_start", "data_end", "verified_at"):
        assert f"'{name}', public.wp06_canonical_utc_timestamp_json(scope.{name})" in statement
    assert payload["verified_at"] == "2026-11-16T01:02:03+00:00"


def test_v5_source_refs_branch_returns_no_source_item_refs_and_rejects_legacy_shapes() -> None:
    module = _migration()
    branch = module.SOURCE_REFS_PATCHES[1][1]
    v5 = branch[: branch.index("RETURN;")]
    for marker in (
        "sample.value ? 'source_references'",
        "sample.value ? 'source_rights_hashes'",
        "sample.value ? 'source_revision_refs'",
        "NOT (sample.value ? 'source_replay_bundle_hash')",
        "NOT (sample.value ? 'source_use_lineage_set_hash')",
        "canonical_manifest->'source_use_lineage'",
        "WP06_SCORER_RIGHTS_TYPED_SOURCE_LINEAGE_INVALID",
        "!~ '^[0-9a-f]{64}$'",
    ):
        assert marker in v5
    assert "RETURN QUERY" not in v5
    assert branch.endswith(
        "IF manifest_version IN ('dataset-manifest-v1','dataset-manifest-v2') THEN"
    )
    assert "'dataset-manifest-v4'" not in "".join(
        replacement for _, replacement in module.SOURCE_REFS_PATCHES
    )


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_rewrites_pin_hashes_before_and_after_in_both_directions(direction: str) -> None:
    module = _migration()
    sql = "\n".join(_statements(direction))
    hashes = module.FUNCTION_DEFINITION_HASHES
    for signature in (module.SOURCE_REFS_SIGNATURE, module.RECHECK_SIGNATURE):
        before, after = (
            ("preimage", "postimage") if direction == "upgrade" else ("postimage", "preimage")
        )
        start = sql.index(f"'{signature}'::regprocedure")
        assert sql.index(hashes[signature][before], start) < sql.index(
            hashes[signature][after], start
        )
    assert "_PREIMAGE_UNEXPECTED" in sql
    assert "_POSTIMAGE_UNEXPECTED" in sql
    assert "_DEFINITION_UNEXPECTED" in sql


def test_upgrade_creates_helper_first_and_downgrade_drops_it_last() -> None:
    module = _migration()
    upgrade = _statements("upgrade")
    downgrade = _statements("downgrade")
    assert "wp06_scorer_replay_evaluation_scope(" in upgrade[0]
    assert module.RECHECK_SIGNATURE in upgrade[-1]
    assert module.RECHECK_SIGNATURE in downgrade[0]
    assert downgrade[-1] == f"DROP FUNCTION {module.HELPER_SIGNATURE}"
    assert not any("DELETE" in item or "TRUNCATE" in item for item in downgrade)


def test_dollar_quote_tags_cannot_collide_with_patch_content() -> None:
    module = _migration()
    with pytest.raises(RuntimeError, match="DOLLAR_TAG_COLLISION"):
        module._dollar("old0", "x $old0$ y")
