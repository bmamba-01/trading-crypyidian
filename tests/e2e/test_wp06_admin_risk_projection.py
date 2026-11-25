"""Compose E2E cho projection risk admin: current membership và số liệu aggregate."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_admin_risk_projection_is_scoped_and_exposes_only_aggregates() -> None:
    root = Path(__file__).parents[2]
    probe = root / "tests/e2e/probes/wp06_admin_risk_projection.py"
    assert probe.is_file(), "Thiếu probe quyền DB cho risk projection admin."

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
        secrets = resources.compose_file.parent / ".secrets"
        admin_secret = secrets / "database_admin_web_password"
        postgres_secret = secrets / "postgres_password"
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
            if result.returncode != 0:
                raise AssertionError(
                    f"Compose {args!r} exit {result.returncode}:\n"
                    f"{result.stdout[-2000:]}\n{result.stderr[-4000:]}"
                )
            return result

        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1
            assert "f0a2c4e6b8e0 (head)" in heads.stdout

            result = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                "--volume",
                f"{admin_secret}:/run/secrets/database_admin_web_password:ro",
                "--volume",
                f"{postgres_secret}:/run/secrets/postgres_password:ro",
                "-e",
                "WP06_ADMIN_PASSWORD_FILE=/run/secrets/database_admin_web_password",
                "-e",
                "WP06_DATABASE_ADMIN_PASSWORD_FILE=/run/secrets/postgres_password",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_admin_risk_projection.py",
            )
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
            assert all(outcome.values()), outcome
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
