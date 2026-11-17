"""AI-5 (CRY-236): migration trial cohort nhiều coin (a5c7e9b1d3f5).

Hash preimage/postimage được đo trên PostgreSQL thật (stack local, 17/11/2026); migration tự
kiểm hash + số lần thay mỗi patch nên lệch định nghĩa là fail closed. Unit này kiểm cấu trúc:
chuỗi revision, ghim đủ hash hai chiều, số patch khớp bộ đếm, công thức set-hash trùng Python.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_bot.adapters.database.replay_training_rights import replay_bundle_set_hash

MIGRATION = (
    Path(__file__).parents[2] / "migrations/versions/a5c7e9b1d3f5_ai5_learning_trial_cohort.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("ai5_trial_cohort", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(direction: str) -> list[str]:
    module = _module()
    statements: list[str] = []
    module.op = SimpleNamespace(
        execute=lambda sql: statements.append(str(sql)),
        create_table=lambda *a, **k: statements.append("CREATE TABLE"),
        create_index=lambda *a, **k: statements.append("CREATE INDEX"),
        drop_index=lambda *a, **k: statements.append("DROP INDEX"),
        drop_table=lambda *a, **k: statements.append("DROP TABLE"),
    )
    getattr(module, direction)()
    return statements


def test_revision_follows_wp06_head_and_pins_every_definition() -> None:
    module = _module()
    assert module.revision == "a5c7e9b1d3f5"
    assert module.down_revision == "e5b7d9f1a3c6"
    for signature, hashes in module.FUNCTION_DEFINITION_HASHES.items():
        assert set(hashes) == {"preimage", "postimage"}, signature
        for value in hashes.values():
            assert len(value) == 64, signature
    assert len(module.SCOPE_PATCHES) == len(module.PATCH_COUNTS[module.SCOPE_SIGNATURE])
    assert len(module.RECHECK_PATCHES) == len(module.PATCH_COUNTS[module.RECHECK_SIGNATURE])
    assert len(module.CLAIM_PATCHES) == len(module.PATCH_COUNTS[module.CLAIM_SIGNATURE])


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_rewrites_check_hashes_before_and_after(direction: str) -> None:
    module = _module()
    joined = "\n".join(_statements(direction))
    for hashes in module.FUNCTION_DEFINITION_HASHES.values():
        assert hashes["preimage"] in joined
        assert hashes["postimage"] in joined
    assert joined.count("AI5_COHORT_PREIMAGE_UNEXPECTED") == 4
    assert joined.count("AI5_COHORT_POSTIMAGE_UNEXPECTED") == 4


def test_scope_gate_no_longer_rejects_mixed_bundles_but_still_rejects_shared_runs() -> None:
    module = _module()
    replacements = "\n".join(after for _, after in module.SCOPE_PATCHES)
    removed = "\n".join(before for before, _ in module.SCOPE_PATCHES)
    assert "WP06_TRAINING_REPLAY_MIXED_BUNDLE" in removed
    assert "WP06_TRAINING_REPLAY_MIXED_BUNDLE" not in replacements
    assert "WP06_TRAINING_REPLAY_MIXED_RUN" in replacements
    assert "lineage.value->>'bundle_hash' = bundle_hash" in replacements


def test_downgrade_refuses_multi_trial_cohorts_before_restoring() -> None:
    statements = _statements("downgrade")
    assert "AI5_COHORT_DOWNGRADE_HAS_MULTI_TRIAL_CANDIDATES" in statements[0]


def test_evaluator_may_only_execute_bundle_list_helper() -> None:
    joined = "\n".join(_statements("upgrade"))
    assert "crypidian_learning_evaluator" in joined
    grant = next(
        line
        for line in joined.splitlines()
        if "wp06_scorer_manifest_replay_bundles" in line and "GRANT" in line
    )
    assert "crypidian_learning_evaluator" in grant


def test_python_set_hash_matches_sql_formula() -> None:
    first, second = "1" * 64, "2" * 64
    assert replay_bundle_set_hash([first]) == first
    expected = hashlib.sha256(
        ("[" + ",".join(f'"{value}"' for value in sorted([first, second])) + "]").encode()
    ).hexdigest()
    assert replay_bundle_set_hash([second, first]) == expected
    assert (
        expected
        == hashlib.sha256(json.dumps([first, second], separators=(",", ":")).encode()).hexdigest()
    )
    with pytest.raises(ValueError, match="Bundle set"):
        replay_bundle_set_hash([])
    with pytest.raises(ValueError, match="Bundle set"):
        replay_bundle_set_hash(["x" * 64])
