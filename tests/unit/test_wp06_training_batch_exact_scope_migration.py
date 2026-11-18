"""Kiểm migration forward sửa thứ tự nhánh trainer source-scope."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/e2f4a6c8b0d4_wp06_training_batch_exact_scope.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration khôi phục trainer batch exact-scope."
    spec = importlib.util.spec_from_file_location("wp06_training_batch_exact_scope", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trainer_gateway_uses_persisted_exact_scope_before_receipt_and_payload() -> None:
    migration = _migration()
    gateway = migration.SOURCE_AWARE_GATEWAY

    assert "PERFORM public.wp06_check_replay_training_scope(" in gateway
    assert gateway.count("'training'") == 2
    assert "stored_manifest" in gateway
    assert "target_attempt_id, true" in gateway
    assert "target_attempt_id, false" in gateway
    assert "WP06_TRAINING_BATCH_SOURCE_RIGHTS_FAIL_CLOSED" not in gateway


def test_upgrade_preserves_previous_function_and_installs_exact_scope_gateway() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]

    assert "RENAME TO wp06_claim_learning_training_batch_pre_exact_scope" in upgrade
    assert "op.execute(SOURCE_AWARE_GATEWAY)" in upgrade
    assert "OWNER TO {TRAINER_READER}" in upgrade
    assert 'f"GRANT EXECUTE ON FUNCTION {GATEWAY}(uuid,uuid) TO {TRAINER}"' in upgrade
    assert "TO {TRAINER}" in upgrade


def test_downgrade_restores_the_original_claim_gateway_and_revision_is_forward_only() -> None:
    migration = _migration()
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade() -> None:", 1)[1]

    assert 'f"DROP FUNCTION {GATEWAY}(uuid,uuid)"' in downgrade
    assert "RENAME TO wp06_claim_learning_training_batch" in downgrade
    assert migration.revision == "e2f4a6c8b0d4"
    assert migration.down_revision == "e2f4a6c8b0d3"


def test_upgrade_gateway_is_the_source_aware_b7_postimage() -> None:
    migration = _migration()
    historical = (
        ROOT / "migrations/versions/b7d9f1a3c5e8_wp06_training_license_scope.py"
    ).read_text(encoding="utf-8")

    assert "PERFORM {SCOPE_CHECK}(" in historical
    assert migration.SOURCE_AWARE_GATEWAY.startswith(
        "CREATE FUNCTION public.wp06_claim_learning_training_batch("
    )
