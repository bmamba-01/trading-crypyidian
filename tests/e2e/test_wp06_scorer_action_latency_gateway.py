"""Compose E2E cho aggregate PAPER latency và ACL scorer-history."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_scorer_action_latency_gateway_acl_and_fail_closed() -> None:
    root = Path(__file__).parents[2]
    probe = root / "tests/e2e/probes/wp06_scorer_action_latency_gateway.py"
    assert probe.is_file(), "gateway latency cần probe PostgreSQL."
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
        admin_secret = resources.compose_file.parent / ".secrets" / "database_admin_web_password"
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
                    f"Compose {args!r} exit {result.returncode}:\n"
                    f"{result.stdout[-4000:]}\n{result.stderr[-7000:]}"
                )
            return result

        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout + check.stderr
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
                "-e",
                "WP06_ADMIN_PASSWORD_FILE=/run/secrets/database_admin_web_password",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_scorer_action_latency_gateway.py",
            )
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
            function_acl = outcome.pop("function_acl")
            assert outcome == {
                "admin_executes_aggregate_only_gateway": True,
                "aggregate_denies_missing_bot_without_rows": True,
                "evaluator_evidence_columns_allowlisted": True,
                "evaluator_role_isolated": True,
                "history_role_cannot_read_raw_latency_sources": True,
                "public_cannot_execute_gateway": True,
            }
            assert "{=X/" not in function_acl, function_acl
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
