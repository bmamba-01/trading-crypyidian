"""PostgreSQL E2E cho expiry feed chạy theo DB clock và idempotency."""

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
    "os.environ.update(WP05_EXPIRY_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp05_expiry_scanner.py','exec'),{'__name__':'__main__'})"
)
CURRENT_HEAD = "b8d1f3a5c7e9"


def test_expiry_scanner_uses_database_clock_and_keeps_expiry_event_after_renewal() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp05-expiry-e2e-{uuid.uuid4().hex[:10]}"
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

    probe = (root / "tests/e2e/probes/wp05_expiry_scanner.py").read_text(encoding="utf-8")
    payload = json.dumps({"state": {"workspace_id": str(uuid.uuid4())}, "code": probe})

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
        assert data == {
            "before_expiry_events": 0,
            "first_scan_events": 1,
            "repeat_scan_events": 0,
            "renewed_scan_events": 0,
            "expiry_event_count_after_renewal": 1,
            "expired_policy_history_unchanged": True,
            "expiry_payload_is_safe": True,
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
