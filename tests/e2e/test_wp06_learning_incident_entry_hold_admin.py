"""Compose E2E cho API entry hold với phiên admin PostgreSQL thật."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_incident_entry_hold_admin_http_requires_role_mfa_csrf_and_is_idempotent() -> None:
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
            secret_mounts = []
            for role in ("admin_web", "trading_runtime"):
                secret = resources.compose_file.parent / ".secrets" / f"database_{role}_password"
                secret_mounts.extend(
                    ("--volume", f"{secret}:/run/secrets/database_{role}_password:ro")
                )
            seed_state = {
                "workspace_id": str(uuid4()),
                "other_workspace_id": str(uuid4()),
            }
            result = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                *secret_mounts,
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_learning_incident_entry_hold_admin.py",
                stdin=json.dumps(seed_state),
            )
            result_data = json.loads(result.stdout.strip().splitlines()[-1])
            assert result_data == {
                "anonymous_denied": True,
                "csrf_denied": True,
                "fresh_mfa_required": True,
                "role_denied": True,
                "workspace_scoped_metadata": True,
                "strict_body": True,
                "stale_target_denied": True,
                "release_persisted_once": True,
                "release_retry_idempotent": True,
                "release_retry_conflict": True,
                "audit_atomic_and_unique": True,
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
