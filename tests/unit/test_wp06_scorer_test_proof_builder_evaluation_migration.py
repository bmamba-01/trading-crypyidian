"""Builder được gọi operation EVALUATION của gate replay để dựng scorer TEST proof.

Quyết định owner 16/11/2026 (hướng B): `LearningScorerTestOpportunityProofRepository` giữ
operation EVALUATION đúng spec 2026-11-13 dòng 34, còn builder vẫn là role duy nhất đọc
lineage và ghi `learning_scorer_test_opportunity_proof`. Migration chỉ mở caller check của
operation `evaluation` cho thêm builder; không cấp thêm quyền bảng/hàm nào.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/c2e4a6b8d0f1_wp06_scorer_test_proof_builder_evaluation.py"
SOURCE_MIGRATION = ROOT / "migrations/versions/b7d9f1a3c5e8_wp06_training_license_scope.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration cho builder gọi EVALUATION khi dựng TEST proof"
    spec = importlib.util.spec_from_file_location("scorer_test_proof_builder_eval", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(direction: str) -> str:
    module = _migration()
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    getattr(module, direction)()
    return "\n".join(statements)


def test_migration_follows_previous_head_and_targets_only_the_bundle_gate() -> None:
    module = _migration()
    assert module.revision == "c2e4a6b8d0f1"
    assert module.down_revision == "f1b8e93fa47f"
    assert module.SIGNATURE == "public.wp06_authorize_replay_training_bundle(uuid,text,text)"
    hashes = module.FUNCTION_DEFINITION_HASHES[module.SIGNATURE]
    assert hashes["preimage"] != hashes["postimage"]
    assert all(len(value) == 64 for value in hashes.values())


def test_previous_caller_check_exists_exactly_once_in_source_migration() -> None:
    module = _migration()
    source = SOURCE_MIGRATION.read_text(encoding="utf-8")
    assert source.count(module.PREVIOUS_CALLER_CHECK) == 1
    assert module.CORRECTED_CALLER_CHECK not in source


def test_corrected_check_allows_builder_and_evaluator_only_for_evaluation() -> None:
    module = _migration()
    corrected = module.CORRECTED_CALLER_CHECK
    assert "target_operation = 'evaluation'" in corrected
    assert "'crypidian_learning_evaluator'" in corrected
    assert "'crypidian_candidate_builder'" in corrected
    assert "training" not in corrected
    for role in ("crypidian_admin_web", "crypidian_trainer_worker", "crypidian_app"):
        assert role not in corrected


@pytest.mark.parametrize(
    ("direction", "before", "after"),
    [("upgrade", "preimage", "postimage"), ("downgrade", "postimage", "preimage")],
)
def test_rewrite_pins_full_definition_hash_before_and_after(direction, before, after) -> None:
    module = _migration()
    sql = _statements(direction)
    hashes = module.FUNCTION_DEFINITION_HASHES[module.SIGNATURE]
    assert sql.index(hashes[before]) < sql.index("EXECUTE replace(") < sql.index(hashes[after])
    assert "WP06_SCORER_TEST_PROOF_EVAL_CALLER_PREIMAGE_UNEXPECTED" in sql
    assert "WP06_SCORER_TEST_PROOF_EVAL_CALLER_POSTIMAGE_UNEXPECTED" in sql
    assert "WP06_SCORER_TEST_PROOF_EVAL_CALLER_DEFINITION_UNEXPECTED" in sql
    # CREATE OR REPLACE từ pg_get_functiondef giữ owner/ACL/SECURITY DEFINER; không GRANT mới.
    assert "GRANT" not in sql
    assert "REVOKE" not in sql
    assert "CREATE POLICY" not in sql


def test_upgrade_replaces_previous_with_corrected_and_downgrade_reverses() -> None:
    module = _migration()
    upgrade = _statements("upgrade")
    downgrade = _statements("downgrade")
    assert upgrade.index(module.PREVIOUS_CALLER_CHECK) < upgrade.index(
        module.CORRECTED_CALLER_CHECK
    )
    assert downgrade.index(module.CORRECTED_CALLER_CHECK) < downgrade.index(
        module.PREVIOUS_CALLER_CHECK
    )


def test_scorer_test_proof_keeps_evaluation_operation_and_builder_snapshot() -> None:
    from trading_bot.adapters.database.repositories import (
        learning_scorer_test_opportunity_proofs as repository,
    )

    source = inspect.getsource(repository.LearningScorerTestOpportunityProofRepository)
    assert "operation=ReplayTrainingOperation.EVALUATION" in source
    assert repository._CANDIDATE_BUILDER == "crypidian_candidate_builder"
    assert "role != _CANDIDATE_BUILDER" in source
