"""PostgreSQL Compose kiểm split approval trước khi seal và tách quyền trainer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available


def test_wp06_split_approval_is_preseal_immutable_and_workspace_scoped() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-split-approval-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
    ]

    def compose(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
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
        assert (result.returncode == 0) is success, (
            args,
            result.stdout[-4000:],
            result.stderr[-4000:],
        )
        return result

    ensure_image_available(root, image)
    probe = "/e2e-tests/e2e/probes/wp06_split_approval.py"
    probe_mount = f"{root / 'tests'}:/e2e-tests:ro"

    def run_probe(service: str, phase: str) -> dict[str, object]:
        output = compose(
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            probe_mount,
            "-e",
            f"WP06_APPROVAL_PHASE={phase}",
            service,
            "python",
            probe,
        )
        return json.loads(output.stdout.strip().splitlines()[-1])

    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
        assert "No new upgrade operations detected" in check.stdout + check.stderr
        assert heads.stdout.count("(head)") == 1
        assert "55c504fe5b86 (head)" in heads.stdout
        assert run_probe("admin-web", "admin") == {
            "approval_requires_staged_candidate": True,
            "training_seal_without_candidate_denied": True,
            "workspace_rls": True,
            "unscoped_read_denied": True,
        }
        assert run_probe("trainer-worker", "trainer") == {"trainer_select_denied": True}
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", "d1e3f5a7c9b2")
        current = compose("run", "--rm", "--no-deps", "migrator", "alembic", "current")
        assert "d1e3f5a7c9b2" in current.stdout
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
