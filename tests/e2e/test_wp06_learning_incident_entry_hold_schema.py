"""Compose kiểm schema/ledger incident; không chứng minh deployment hoặc quyền nguồn."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_postgres_incident_hold_ledger_schema_and_runtime_projection() -> None:
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

        def compose(*args: str, failure_marker: str | None = None):
            result = subprocess.run(
                [*base, *args],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=360,
            )
            output = result.stdout + result.stderr
            if failure_marker:
                assert result.returncode != 0, output[-7000:]
                assert failure_marker in output, output[-7000:]
            else:
                assert result.returncode == 0, output[-9000:]
            return result

        try:
            compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
            compose("run", "--rm", "--no-deps", "db-provisioner")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "f4a6c8e0b2d5")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert "d2e4f6a8b0c1 (head)" in heads.stdout
            assert heads.stdout.count("(head)") == 1
            mounts = []
            for role in (
                "admin_web",
                "trading_runtime",
                "trainer_worker",
                "learning_evaluator",
                "market_signal_worker",
            ):
                secret = resources.compose_file.parent / ".secrets" / f"database_{role}_password"
                mounts.extend(("--volume", f"{secret}:/run/secrets/database_{role}_password:ro"))
            result = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                *mounts,
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_learning_incident_entry_hold_schema.py",
            )
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
            assert outcome == {
                "synthetic_metadata_only": True,
                "forced_rls_and_workspace_isolation": True,
                "runtime_execute_only": True,
                "immutable_canonical_chain": True,
                "terminal_release_and_cas": True,
                "idempotency_uniqueness": True,
                "mixed_incumbent_denied": True,
                "disabled_gateway_denied": True,
                "release_after_disabled_target": True,
                "multiple_holds_and_audit_rollback": True,
                "whitespace_only_reason_denied": True,
                "whitespace_only_request_id_denied": True,
            }
            compose(
                "run",
                "--rm",
                "--no-deps",
                "migrator",
                "alembic",
                "downgrade",
                "f4a6c8e0b2d5",
                failure_marker="WP06_INCIDENT_POPULATED_DOWNGRADE_DENIED",
            )
            current = compose("run", "--rm", "--no-deps", "migrator", "alembic", "current")
            assert "d2e4f6a8b0c1 (head)" in current.stdout
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
