"""Compose E2E cho projection receipt của deployment current-rights gateway."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_installed_scorer_receipt_projection_exposes_membership_digest() -> None:
    root = Path(__file__).parents[2]
    with isolated_compose_resources(root) as resources:
        base = [
            "docker",
            "compose",
            "--project-name",
            resources.project,
            "--project-directory",
            str(root),
            "-f",
            str(resources.compose_file),
        ]
        environment = os.environ | {"CRYPIDIAN_APP_IMAGE": resources.image}
        image_created = ensure_image_available(root, resources.image)

        def compose(*args: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [*base, *args],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=360,
            )
            assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-7000:]
            return result

        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "f4a6c8e0b2d5")
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1, heads.stdout
            assert "f5b7d9e1a3c6 (head)" in heads.stdout, heads.stdout
            result = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_scorer_receipt_projection.py",
            )
            observations = json.loads(result.stdout.strip().splitlines()[-1])
            assert observations == {
                "installed_projection_preserves_test_membership_digest": True,
                "historical_projection_reproduces_missing_record_field": True,
                "upgrade_downgrade_drift_guards": True,
                "roundtrip_preserves_function_metadata_and_body": True,
            }
            # Probe ghim migration f4 lịch sử; nâng head hiện hành trước kiểm metadata.
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout + check.stderr
        finally:
            compose("down", "--volumes", "--remove-orphans")
            for resource in ("container", "volume", "network"):
                command = [
                    "docker",
                    resource,
                    "ls",
                    "--quiet",
                    "--filter",
                    f"label=com.docker.compose.project={resources.project}",
                ]
                if resource == "container":
                    command.append("--all")
                inventory = subprocess.run(command, capture_output=True, text=True, check=True)
                assert not inventory.stdout.strip(), (resource, inventory.stdout)
            if image_created:
                subprocess.run(
                    ["docker", "image", "rm", resources.image],
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
