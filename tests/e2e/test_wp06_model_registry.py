"""PostgreSQL Compose E2E cho role matrix/RLS của model registry WP06."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available


def test_model_registry_schema_is_workspace_scoped_and_least_privilege() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-registry-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--profile",
        "learning-proof",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
    ]

    def compose(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Compose command {args!r} failed.\nstdout:\n{result.stdout[-8000:]}"
                f"\nstderr:\n{result.stderr[-8000:]}"
            )
        return result

    ensure_image_available(root, image)
    probe = "/e2e-tests/e2e/probes/wp06_model_registry.py"
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
        check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        assert "b2e4a6c8d0f2 (head)" in heads.stdout
        assert "No new upgrade operations detected" in check.stdout + check.stderr

        probe_result = compose(
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            f"{root / 'tests'}:/e2e-tests:ro",
            "admin-web",
            "python",
            probe,
        )
        assert json.loads(probe_result.stdout.strip().splitlines()[-1]) == {
            "event_triggers": True,
            "least_privilege": True,
            "rls_force": True,
        }
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
