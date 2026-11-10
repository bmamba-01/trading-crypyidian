"""PostgreSQL E2E cho feed quyền nguồn ghi cùng transaction."""

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
    "os.environ.update(WP05_OUTBOX_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp05_rights_change_outbox.py','exec'),{'__name__':'__main__'})"
)
CURRENT_HEAD = "f6b8d0e2a4c6"


def test_rights_change_outbox_is_atomic_immutable_and_triggered_by_database() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp05-outbox-e2e-{uuid.uuid4().hex[:10]}"
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

    probe = (root / "tests/e2e/probes/wp05_rights_change_outbox.py").read_text(encoding="utf-8")
    state = {"workspace_id": str(uuid.uuid4())}
    payload = json.dumps({"state": state, "code": probe})

    def migrate(*args: str) -> None:
        compose("run", "--rm", "--no-deps", "migrator", "alembic", *args)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", CURRENT_HEAD)
        migrate("upgrade", "head")
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
        data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
        assert data["rights_event_count"] == 4
        assert data["duplicate_retry_count"] == 4
        assert data["rollback_preserved_count"] == 4
        assert data["event_payloads_are_source_only"] is True
        assert data["forged_event_rejected"] is True
        assert data["payload_rewrite_rejected"] is True
        assert data["delivery_update_allowed"] is True
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
