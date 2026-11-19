"""Compose PostgreSQL E2E cho race chặn entry PAPER và runtime thật."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_postgres_block_and_paper_runtime_are_serialized_in_both_orders() -> None:
    root = Path(__file__).parents[2]
    with isolated_compose_resources(root, postgres_tmpfs=True) as resources:
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

        def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [*base, *args],
                cwd=root,
                env=environment,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=360,
            )
            assert result.returncode == 0, (
                args[:8],
                result.stdout[-5000:],
                result.stderr[-8000:],
            )
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
            state = {
                "workspace_id": str(uuid4()),
                "other_workspace_id": str(uuid4()),
                "race_id": str(uuid4()),
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
                "/e2e-tests/e2e/probes/wp06_scorer_paper_entry_block.py",
                stdin=json.dumps(state),
            )
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
            assert outcome == {
                "block_first_prevented_entry_side_effects": True,
                "entry_first_preserved_committed_trade_and_conflicted": True,
                "workspace_reader_scope_and_immutable_audit": True,
                "shadow_entry_and_protective_exit_unaffected": True,
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
