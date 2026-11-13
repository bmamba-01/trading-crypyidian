"""Compose E2E cho API policy admin với login PostgreSQL và session thật."""

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
    "os.environ.update(WP05_ADMIN_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp05_source_policy_admin.py','exec'),{'__name__':'__main__'})"
)


def test_policy_admin_authenticated_roles_recovery_quota_and_rls() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp05-admin-e2e-{uuid.uuid4().hex[:10]}"
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

    probe = (root / "tests/e2e/probes/wp05_source_policy_admin.py").read_text(encoding="utf-8")
    payload = json.dumps(
        {
            "state": {"workspace_id": str(uuid.uuid4()), "other_workspace_id": str(uuid.uuid4())},
            "code": probe,
        }
    )

    def migrate(*args: str) -> None:
        compose("run", "--rm", "--no-deps", "migrator", "alembic", *args)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", "head")
        migrate("check")
        migrate("heads")
        compose("config", "--quiet")
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "db-provisioner",
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
        assert data == {
            "stream_evidence_boundary_idempotency": True,
            "evidence_transaction_rollback": True,
            "real_admin_login": True,
            "authenticated_owner_reviewer": True,
            "policy_rows": 3,
            "receipt_rows": 3,
            "quota_rows": 1,
            "self_approval_denied": True,
            "foreign_workspace_denied": True,
            "provenance_preserved": True,
            "observer_retry_idempotent": True,
            "quota_read_only": True,
            "append_only_rls": True,
        }
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
