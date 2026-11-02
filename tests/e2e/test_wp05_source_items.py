"""PostgreSQL Compose cho WP05C metadata revision, restart, RLS và takedown."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(WP05C_PHASE=p['phase'],WP05C_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp05_source_items.py','exec'),{'__name__':'__main__'})"
)
PREVIOUS_HEAD = "b8d0f1a3c5e7"


def test_source_item_metadata_persists_with_restart_rls_and_durable_takedown() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp05-items-e2e-{uuid.uuid4().hex[:10]}"
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

    def compose(
        *args: str, success: bool = True, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            base + list(args),
            cwd=root,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if (result.returncode == 0) is not success:
            raise AssertionError(
                f"Compose lệnh {args!r} thất bại:\n{result.stdout}\n{result.stderr}"
            )
        return result

    probe = (root / "tests/e2e/probes/wp05_source_items.py").read_text(encoding="utf-8")
    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
    }

    def run_probe(phase: str) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": state, "code": probe})
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migrator",
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def migrate(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        return compose("run", "--rm", "--no-deps", "migrator", "alembic", *args, success=success)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", PREVIOUS_HEAD)
        migrate("upgrade", "head")
        migrate("check")
        migrate("downgrade", PREVIOUS_HEAD)
        migrate("upgrade", "head")
        migrate("check")
        state.update(run_probe("seed"))
        run_probe("persist")
        compose("restart", "postgres")
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        run_probe("readback")
        run_probe("roles")
        run_probe("guards")
        run_probe("withdraw")
        compose("restart", "postgres")
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        run_probe("withdrawn_readback")
        downgrade = migrate("downgrade", PREVIOUS_HEAD, success=False)
        assert "WP05_ITEM_HISTORY_DOWNGRADE_BLOCKED" in (downgrade.stdout + downgrade.stderr)
        migrate("check")
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
