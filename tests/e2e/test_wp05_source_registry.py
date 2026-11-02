"""PostgreSQL Compose kiểm registry WP-05, RLS, quyền role và quota concurrent."""

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
    "os.environ.update(WP05_PHASE=p['phase'],WP05_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp05_source_registry.py','exec'),{'__name__':'__main__'})"
)
PREVIOUS_HEAD = "e2c4a6f8b1d3"


def test_source_registry_workspace_roles_append_only_and_concurrent_quota() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp05-registry-e2e-{uuid.uuid4().hex[:10]}"
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
        *args: str,
        stdin: str | None = None,
        success: bool = True,
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

    probe = (root / "tests/e2e/probes/wp05_source_registry.py").read_text(encoding="utf-8")
    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
        "source_id": f"source-{uuid.uuid4().hex[:12]}",
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
        return compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            *args,
            success=success,
        )

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
        run_probe("seed")
        run_probe("registry")
        transitions = run_probe("policy_transition_guard")
        assert len(transitions["transition_failures"]) == 3
        reservation = run_probe("concurrent_reservations")
        reserved = [key for key, status in reservation["reservations"] if status == "reserved"]
        assert len(reserved) == 1
        state["reserved_key"] = reserved[0]
        run_probe("append_only")
        run_probe("revoke")
        run_probe("revoked_retry")
        downgrade = migrate("downgrade", PREVIOUS_HEAD, success=False)
        assert "WP05_SOURCE_HISTORY_DOWNGRADE_BLOCKED" in (downgrade.stdout + downgrade.stderr)
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
