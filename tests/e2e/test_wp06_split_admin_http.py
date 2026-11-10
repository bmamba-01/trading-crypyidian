"""Compose E2E cho API duyệt split qua HTTP và phiên đăng nhập database thật."""

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
    "os.environ.update(WP06_SPLIT_HTTP_STATE=json.dumps(p));"
    "exec(compile(open('/e2e-tests/e2e/probes/wp06_split_admin_http.py').read(),"
    "'wp06_split_admin_http.py','exec'),{'__name__':'__main__'})"
)


def test_split_admin_http_uses_real_session_csrf_mfa_and_persists_approval() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-split-http-{uuid.uuid4().hex[:10]}"
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

    def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
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
        if result.returncode != 0:
            raise AssertionError(
                f"Compose lệnh {args!r} thất bại:\n{result.stdout}\n{result.stderr}"
            )
        return result

    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
    }
    probe_mount = f"{root / 'tests'}:/e2e-tests:ro"
    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        check = compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
        assert "No new upgrade operations detected" in check.stdout + check.stderr
        assert heads.stdout.count("(head)") == 1

        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--volume",
            probe_mount,
            "db-provisioner",
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=json.dumps(state),
        )
        data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
        assert data == {
            "anonymous_denied": True,
            "csrf_denied": True,
            "foreign_workspace_hidden": True,
            "mfa_required": True,
            "metadata_only": True,
            "persisted_once": True,
            "retry_idempotent": True,
            "role_denied": True,
            "stale_key_conflict": True,
            "valid_reviewer_approved": True,
        }
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
