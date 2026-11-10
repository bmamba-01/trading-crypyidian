"""PostgreSQL E2E cho WP06G.2 consumer, ledger bất biến và event lag gate."""

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
    "os.environ['WP06_RIGHTS_STATE']=json.dumps(p['state']);"
    "exec(compile(p['code'],'wp06_rights_invalidation.py','exec'),{'__name__':'__main__'})"
)
PREVIOUS_HEAD = "c1e3f5a7b9d2"


def test_rights_consumer_appends_invalidation_and_unprocessed_events_fail_closed() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-rights-e2e-{uuid.uuid4().hex[:10]}"
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

    probe = (root / "tests/e2e/probes/wp06_rights_invalidation.py").read_text(encoding="utf-8")
    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
    }
    payload = json.dumps({"state": state, "code": probe})

    def migrate(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        return compose("run", "--rm", "--no-deps", "migrator", "alembic", *args, success=success)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", PREVIOUS_HEAD)
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
        downgrade = migrate("downgrade", PREVIOUS_HEAD, success=False)
        assert "WP06_RIGHTS_INVALIDATION_DOWNGRADE_REQUIRES_EMPTY_TABLE" in downgrade.stderr
        assert data == {
            "first_batch": {
                "expiry_events_enqueued": 0,
                "claimed": 5,
                "processed": 5,
                "invalidated_references": 3,
                "failed": 0,
                "dead_lettered": 0,
            },
            "duplicate_retry": {
                "expiry_events_enqueued": 0,
                "claimed": 1,
                "processed": 1,
                "invalidated_references": 0,
                "failed": 0,
                "dead_lettered": 0,
            },
            "ledger_rows_after_retry": 3,
            "ledger_reasons": {
                "source_item_superseded": 1,
                "source_item_takedown": 1,
                "source_policy_changed": 1,
            },
            "ledger_reason": "source_policy_changed",
            "gate_before_late_event": False,
            "gate_after_late_event": True,
            "trainer_can_read_source_policy": True,
            "trainer_cannot_write_ledger": True,
            "outbox_scope": {
                "current_workspace_rows": 5,
                "other_workspace_rows": 1,
                "unscoped_rows": 0,
            },
            "ingestion_cannot_write_outbox_identity_or_payload": True,
            "ingestion_cannot_read_manifest_payload": True,
        }
        compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "90",
            "ingestion-worker",
        )
        worker_logs = compose("logs", "--no-color", "ingestion-worker").stdout
        assert "Rights worker tick thất bại" not in worker_logs
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
