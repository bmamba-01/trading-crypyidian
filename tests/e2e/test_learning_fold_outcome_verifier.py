"""Kiểm tra bộ xác minh outcome fold WP06 qua PostgreSQL Compose."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available


def _compose(
    repository_root: Path,
    project_name: str,
    environment: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--profile",
            "learning",
            "--project-name",
            project_name,
            "-f",
            "compose.yaml",
            *args,
        ],
        check=False,
        capture_output=True,
        cwd=repository_root,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert result.returncode == 0, (args, result.stdout[-5000:], result.stderr[-5000:])
    return result


def test_evaluator_uses_one_shot_receipt_before_disclosing_fold_payload() -> None:
    repository_root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    project_name = f"crypidian-fold-verifier-e2e-{uuid.uuid4().hex[:10]}"
    image_reference = build_image_reference(repository_root)
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image_reference,
    }
    workspace_id = str(uuid.uuid4())
    test_volume = f"{repository_root / 'tests'}:/e2e-tests:ro"
    script = "/e2e-tests/e2e/probes/wp06_fold_outcome_verifier.py"
    ensure_image_available(repository_root, image_reference)
    try:
        _compose(
            repository_root,
            project_name,
            environment,
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
            "postgres",
        )
        _compose(
            repository_root, project_name, environment, "run", "--rm", "--no-deps", "db-provisioner"
        )
        _compose(
            repository_root,
            project_name,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "upgrade",
            "head",
        )
        _compose(
            repository_root,
            project_name,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "check",
        )
        heads = _compose(
            repository_root,
            project_name,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "heads",
        )
        assert heads.stdout.strip().count("(head)") == 1, heads.stdout
        assert "f5b7d9e1a3c6 (head)" in heads.stdout
        seeded = _compose(
            repository_root,
            project_name,
            environment | {"CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id},
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "--volume",
            test_volume,
            "migrator",
            "python",
            script,
            "seed",
        )
        state = json.loads(seeded.stdout.strip().splitlines()[-1])
        verified = _compose(
            repository_root,
            project_name,
            environment
            | {
                "CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id,
                "WP06_STATE": json.dumps(state),
            },
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "-e",
            f"WP06_STATE={json.dumps(state)}",
            "--volume",
            test_volume,
            "learning-evaluator",
            "python",
            script,
            "verify",
        )
        result = json.loads(verified.stdout.strip().splitlines()[-1])
        assert result["manifest_hash"] == state["manifest_hash"]
        assert result["train_sample_ids"] == ["train-sample"]
        assert result["validation_sample_ids"] == ["validation-sample"]
        assert result["test_sample_ids"] == ["test-sample"]
        assert result["train_trade_count"] == 1
        assert result["train_net_pnl"] != "0"
        assert result["validation_trade_count"] == 1
        assert result["test_trade_count"] == 1
        assert result["candidate_completeness_verified"] is False
        assert result["holdout_payload_in_report"] is False
        assert result["mismatched_workspace_rejected"] is True
        assert result["repeatable_read_write_required"] is True
        assert result["rollback_before_commit_allows_retry"] is True
        assert result["crash_after_commit_retry_denied"] is True
        assert result["concurrent_single_receipt_winner"] is True
        assert result["receipt_persisted_before_report"] is True
        assert result["receipt_fold_consumed_once"] is True
        assert result["evaluator_direct_payload_select_denied"] is True
        assert result["persisted_report_id"]
        assert result["persisted_report_hash"] == result["report_hash"]
        assert result["persisted_report_canonical_hash_matches"] is True
        assert result["persisted_report_duplicate_retry"] is True
        assert result["retry_kept_same_record"] is True
        assert result["changed_payload_rejected"] is True
        assert result["holdout_payload_in_canonical_report"] is False
        assert result["cross_workspace_hidden"] is True
        assert result["evaluator_select_insert_only"] is True
        assert result["evaluator_update_denied"] is True
        assert result["evaluator_delete_denied"] is True

        admin_payload_check = _compose(
            repository_root,
            project_name,
            environment
            | {
                "CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id,
                "WP06_STATE": json.dumps(state),
            },
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "-e",
            f"WP06_STATE={json.dumps(state)}",
            "--volume",
            test_volume,
            "admin-web",
            "python",
            script,
            "verify-payload-denied",
        )
        admin_payload_result = json.loads(admin_payload_check.stdout.strip().splitlines()[-1])
        assert admin_payload_result == {
            "app_payload_select_denied": True,
            "direct_payload_select_denied": True,
            "legacy_verifier_denied": True,
            "metadata_column_allowed": True,
        }
        trainer_payload_check = _compose(
            repository_root,
            project_name,
            environment
            | {
                "CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id,
                "WP06_STATE": json.dumps(state),
            },
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "-e",
            f"WP06_STATE={json.dumps(state)}",
            "--volume",
            test_volume,
            "trainer-worker",
            "python",
            script,
            "verify-payload-denied",
        )
        trainer_payload_result = json.loads(trainer_payload_check.stdout.strip().splitlines()[-1])
        assert trainer_payload_result == {
            "app_payload_select_denied": True,
            "direct_payload_select_denied": True,
            "legacy_verifier_denied": True,
            "metadata_column_allowed": True,
        }

        admin_verified = _compose(
            repository_root,
            project_name,
            environment
            | {
                "CRYPIDIAN_E2E_WORKSPACE_ID": workspace_id,
                "WP06_STATE": json.dumps(state),
            },
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"CRYPIDIAN_E2E_WORKSPACE_ID={workspace_id}",
            "-e",
            f"WP06_STATE={json.dumps(state)}",
            "--volume",
            test_volume,
            "admin-web",
            "python",
            script,
            "verify-admin",
        )
        admin_result = json.loads(admin_verified.stdout.strip().splitlines()[-1])
        assert admin_result["admin_can_read"] is True
        assert admin_result["admin_select_only"] is True
        assert admin_result["admin_update_denied"] is True
        assert admin_result["admin_delete_denied"] is True
        assert admin_result["admin_cross_workspace_hidden"] is True

        downgrade = subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "paper",
                "--project-name",
                project_name,
                "-f",
                "compose.yaml",
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "downgrade",
                "a8c0e2f4b6d9",
            ],
            check=False,
            capture_output=True,
            cwd=repository_root,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        downgrade_output = downgrade.stdout + downgrade.stderr
        assert downgrade.returncode != 0
        assert "WP06_SPLIT_CANDIDATE_DOWNGRADE_REQUIRES_EMPTY_TABLE" in downgrade_output
        current = _compose(
            repository_root,
            project_name,
            environment,
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "current",
        )
        assert "f5b7d9e1a3c6 (head)" in current.stdout
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "paper",
                "--project-name",
                project_name,
                "-f",
                "compose.yaml",
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
