"""Compose E2E cho projection hold bằng đúng vai trò runtime PAPER."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_runtime_hold_projection_is_scoped_persistent_and_requires_all_releases() -> None:
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

        def compose(*args: str, stdin: str | None = None):
            result = subprocess.run(
                [*base, *args],
                cwd=root,
                env=environment,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=360,
            )
            output = result.stdout + result.stderr
            assert result.returncode == 0, output[-10000:]
            return result

        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout + check.stderr
            secret = (
                resources.compose_file.parent / ".secrets" / "database_trading_runtime_password"
            )
            admin_secret = (
                resources.compose_file.parent / ".secrets" / "database_admin_web_password"
            )
            result = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                "--volume",
                f"{secret}:/run/secrets/database_trading_runtime_password:ro",
                "--volume",
                f"{admin_secret}:/run/secrets/database_admin_web_password:ro",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_learning_incident_entry_hold_runtime.py",
                stdin=json.dumps(
                    {"workspace_id": str(uuid4()), "other_workspace_id": str(uuid4())}
                ),
            )
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
            assert outcome == {
                "runtime_only_projection": True,
                "workspace_scoped": True,
                "restart_persists_hold": True,
                "multiple_holds_require_all_releases": True,
                "read_committed_fail_closed": True,
                "governance_entry_l1_serialized": True,
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
