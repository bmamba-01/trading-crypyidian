"""Compose E2E cho scorer deployment append-only, RLS, CAS và rights fail-closed."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_postgres_scorer_deployment_governance_and_rights_gate() -> None:
    root = Path(__file__).parents[2]
    probe = root / "tests/e2e/probes/wp06_scorer_deployment.py"
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
        secret = resources.compose_file.parent / ".secrets" / "database_admin_web_password"
        image_created = ensure_image_available(root, resources.image)

        def compose(*args: str, allowed_failure: bool = False) -> subprocess.CompletedProcess[str]:
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
            if not allowed_failure and result.returncode != 0:
                raise AssertionError(
                    f"Compose {args!r} failed ({result.returncode}):\n"
                    f"{result.stdout[-5000:]}\n{result.stderr[-7000:]}"
                )
            return result

        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1, heads.stdout
            assert "d7f9a1b3c5e8 (head)" in heads.stdout
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
                f"{secret}:/run/secrets/database_admin_web_password:ro",
                "-e",
                "WP06_ADMIN_PASSWORD_FILE=/run/secrets/database_admin_web_password",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_scorer_deployment.py",
            )
            observations = json.loads(result.stdout.strip().splitlines()[-1])
            assert observations == {
                "admin_insert_policy": True,
                "cas_race_one_conflict": True,
                "cas_race_one_winner": True,
                "changed_intent_conflict": True,
                "cross_workspace_rls": True,
                "current_state_readable": True,
                "event_count": 3,
                "forced_rls": True,
                "least_privilege": True,
                "same_intent_retry": True,
                "shadow_rights_gate_rejected": True,
                "stale_cas_rejected": True,
                "stale_cas_rollback": True,
                "stale_active_admin_readable": True,
                "stale_active_disabled": True,
                "runtime_rejects_unverified_active": True,
            }

            downgrade = compose(
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "downgrade",
                "b3d5f7a9c1e3",
                allowed_failure=True,
            )
            assert downgrade.returncode != 0
            assert "WP06_SCORING_DEPLOYMENT_POPULATED_DOWNGRADE_DENIED" in (
                downgrade.stdout + downgrade.stderr
            )
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout + check.stderr
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1, heads.stdout
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
