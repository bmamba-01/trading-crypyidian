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


def test_fold_evaluator_reads_selected_trade_in_trainer_read_only_snapshot() -> None:
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
            "trainer-worker",
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
        assert result["read_only_snapshot_required"] is True
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
