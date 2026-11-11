"""Compose E2E cho gateway runtime scorer với quyền PostgreSQL thật."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_scorer_runtime_target_gateway_role_boundary_and_fail_closed() -> None:
    root = Path(__file__).parents[2]
    probe = root / "tests/e2e/probes/wp06_scorer_runtime_target_resolver.py"
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
        runtime_secret = (
            resources.compose_file.parent / ".secrets" / "database_trading_runtime_password"
        )
        admin_web_secret = (
            resources.compose_file.parent / ".secrets" / "database_admin_web_password"
        )
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
                    f"{result.stdout[-4000:]}\n{result.stderr[:3000]}\n"
                    f"--- stderr tail ---\n{result.stderr[-6000:]}"
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

            result = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                "--volume",
                f"{runtime_secret}:/run/secrets/database_trading_runtime_password:ro",
                "--volume",
                f"{admin_web_secret}:/run/secrets/database_admin_web_password:ro",
                "-e",
                "WP06_RUNTIME_PASSWORD_FILE=/run/secrets/database_trading_runtime_password",
                "-e",
                "WP06_ADMIN_WEB_PASSWORD_FILE=/run/secrets/database_admin_web_password",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_scorer_runtime_target_resolver.py",
            )
            observations = json.loads(result.stdout.strip().splitlines()[-1])
            assert observations == {
                "runtime_executes_resolver": True,
                "runtime_cannot_read_scorer_rows": True,
                "runtime_cannot_read_deployment_event": True,
                "runtime_cannot_read_deployment_pointer": True,
                "market_signal_worker_cannot_read_deployment_event": True,
                "market_signal_worker_cannot_read_deployment_pointer": True,
                "rights_role_has_only_target_metadata": True,
                "rights_role_has_deployment_pointer_metadata": True,
                "rights_role_has_event_payload_metadata": True,
                "rights_role_can_read_workspace_event": True,
                "trigger_uses_resolver": True,
                "trigger_binds_pending_predecessor": True,
                "trigger_is_before_insert": True,
                "after_insert_applies_new_pointer": True,
                "invalid_target_rejected": True,
                "disabled_workspace_is_inert": True,
            }
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
