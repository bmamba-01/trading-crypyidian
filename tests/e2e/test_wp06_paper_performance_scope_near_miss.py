"""PostgreSQL Compose kiểm matrix scope âm tính của PAPER performance."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_paper_performance_rejects_each_synthetic_scope_near_miss() -> None:
    root = Path(__file__).parents[2]
    probe = root / "tests/e2e/probes/wp06_paper_performance_scope_near_miss.py"
    assert probe.is_file()
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
            if result.returncode != 0:
                raise AssertionError(
                    f"Compose {args!r} failed ({result.returncode}):\n"
                    f"{result.stdout[-4000:]}\n{result.stderr[-6000:]}"
                )
            return result

        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1, heads.stdout
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout + check.stderr
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
                f"{admin_secret}:/run/secrets/database_admin_web_password:ro",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_paper_performance_scope_near_miss.py",
            )
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
            assert outcome == {
                "synthetic_fixture_only": True,
                "checked_dimensions": [
                    "contract",
                    "data_end",
                    "data_start",
                    "derived_use",
                    "intervals",
                    "outputs",
                    "product",
                    "provider",
                    "purpose",
                    "retention",
                    "source_id",
                    "symbols",
                    "venue",
                ],
                "every_scope_near_miss_denied_without_report": True,
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
                inventory = subprocess.run(command, capture_output=True, text=True, check=True)
                assert not inventory.stdout.strip(), (resource, inventory.stdout)
            if image_created:
                subprocess.run(
                    ["docker", "image", "rm", resources.image],
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
