"""Compose xác minh ACL gateway hiệu năng PAPER và denial khi thiếu target."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_paper_performance_aggregate_gateway_is_narrow_and_fails_closed() -> None:
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

        def compose(*args: str):
            result = subprocess.run(
                [*base, *args],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=360,
            )
            output = result.stdout + result.stderr
            assert result.returncode == 0, output[-9000:]
            return result

        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout + check.stderr
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1
            assert "d2e4f6a8b0c1 (head)" in heads.stdout
            admin_secret = (
                resources.compose_file.parent / ".secrets" / "database_admin_web_password"
            )
            probe = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                "--volume",
                f"{admin_secret}:/run/secrets/database_admin_web_password:ro",
                "-e",
                "CRYPIDIAN_E2E_WORKSPACE_ID=2c8b326f-9d1f-42d7-90ec-9265daaf703f",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_paper_performance_gateway.py",
            )
            outcome = json.loads(probe.stdout.strip().splitlines()[-1])
            assert outcome == {
                "narrow_evaluator_role": True,
                "aggregate_only_report_acl": True,
                "admin_can_execute_gateway": True,
                "public_and_app_cannot_execute": True,
                "missing_active_target_denied": True,
                "no_raw_member_fields_returned": True,
            }
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
                assert not subprocess.run(
                    command, capture_output=True, text=True, check=True
                ).stdout.strip()
            if image_created:
                subprocess.run(
                    ["docker", "image", "rm", resources.image],
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
