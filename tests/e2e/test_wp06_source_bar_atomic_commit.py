"""Compose E2E cho atomicity của lineage BotDecision→MarketBar."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from scripts.docker_image import ensure_image_available

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources


def test_committed_decision_rejects_late_source_bar_append() -> None:
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
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "f8e0a2c4d6bd")
            compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
            check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
            assert "No new upgrade operations detected" in check.stdout + check.stderr
            heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
            assert heads.stdout.count("(head)") == 1
            assert "f4b6d8e0a2c7 (head)" in heads.stdout

            worker_secret = (
                resources.compose_file.parent
                / ".secrets"
                / "database_market_signal_worker_password"
            )
            probe = compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--volume",
                f"{root / 'tests'}:/e2e-tests:ro",
                "--volume",
                f"{worker_secret}:/run/secrets/database_market_signal_worker_password:ro",
                "-e",
                f"CRYPIDIAN_E2E_WORKSPACE_ID={uuid4()}",
                "migrator",
                "python",
                "/e2e-tests/e2e/probes/wp06_source_bar_atomic_commit.py",
            )
            outcome = json.loads(probe.stdout.strip().splitlines()[-1])
            assert outcome == {
                "decision_stamp_trigger_registered": True,
                "late_append_denied_by_transaction_marker": True,
                "legacy_parent_has_no_stamp": True,
                "marker_xid8_column_registered": True,
                "source_bar_gate_registered": True,
                "source_lineage_unchanged": True,
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
