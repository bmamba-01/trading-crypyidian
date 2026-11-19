"""Compose E2E cho QA denial của exact source scope Shadow/Drift."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_shadow_drift_source_scope_denies_qa_without_license_fixture() -> None:
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
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1
            assert "a6c8e0b2d4f7 (head)" in heads.stdout
            admin_secret = (
                resources.compose_file.parent / ".secrets" / ("database_admin_web_password")
            )
            observer_secret = (
                resources.compose_file.parent
                / ".secrets"
                / ("database_learning_shadow_observer_password")
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
                "--volume",
                f"{observer_secret}:/run/secrets/database_learning_shadow_observer_password:ro",
                "-e",
                "CRYPIDIAN_E2E_WORKSPACE_ID=2c8b326f-9d1f-42d7-90ec-9265daaf703f",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_shadow_drift_source_scope.py",
            )
            outcome = json.loads(probe.stdout.strip().splitlines()[-1])
            assert outcome == {
                "app_role_cannot_execute_scope_gates": True,
                "drift_qa_source_denied": True,
                "missing_assessment_recheck_false": True,
                "missing_decision_denied": True,
                "missing_observation_recheck_false": True,
                "observer_can_execute_shadow_source_gate": True,
                "observer_drift_qa_source_denied": True,
                "observer_qa_source_is_explicit": True,
                "observer_shadow_qa_source_denied": True,
                "qa_source_is_explicit": True,
                "shadow_qa_source_denied": True,
            }
        finally:
            compose("down", "--volumes", "--remove-orphans")
            if image_created:
                subprocess.run(
                    ["docker", "image", "rm", resources.image],
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
