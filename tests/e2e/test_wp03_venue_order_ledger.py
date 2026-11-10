"""PostgreSQL Compose kiểm venue ledger, idempotency, transition guard và RLS."""

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
    "os.environ.update(WP03_PHASE=p['phase'],WP03_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp03_venue_order_ledger.py','exec'),{'__name__':'__main__'})"
)
PREVIOUS_HEAD = "c6e8a0b2d4f6"


def test_venue_order_ledger_idempotency_transitions_rls_and_downgrade_guard() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp03-ledger-e2e-{uuid.uuid4().hex[:10]}"
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

    def compose(*args: str, stdin: str | None = None, success: bool = True):
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
        assert (result.returncode == 0) is success, (
            args,
            result.stdout[-12000:],
            result.stderr[-12000:],
        )
        return result

    probe = (root / "tests/e2e/probes/wp03_venue_order_ledger.py").read_text(encoding="utf-8")
    state = {
        "workspace_id": str(uuid.uuid4()),
        "other_workspace_id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "other_account_id": str(uuid.uuid4()),
        "settings_version_id": str(uuid.uuid4()),
        "membership_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "plan_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "intent_id": str(uuid.uuid4()),
        "reservation_id": str(uuid.uuid4()),
        "other_reservation_id": str(uuid.uuid4()),
        "cross_bot_reservation_id": str(uuid.uuid4()),
        "paper_transfer_reservation_id": str(uuid.uuid4()),
        "guard_reservation_id": str(uuid.uuid4()),
        "guard_intent_id": str(uuid.uuid4()),
        "risk_decision_id": str(uuid.uuid4()),
        "other_risk_decision_id": str(uuid.uuid4()),
        "other_intent_id": str(uuid.uuid4()),
        "other_client_order_id": f"wp03{uuid.uuid4().hex[:24]}",
        "guard_client_order_id": f"wp03{uuid.uuid4().hex[:24]}",
        "client_order_id": f"wp03{uuid.uuid4().hex[:24]}",
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

    def migrate(*args: str, success: bool = True):
        return compose("run", "--rm", "--no-deps", "migrator", "alembic", *args, success=success)

    ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", PREVIOUS_HEAD)
        migrate("upgrade", "head")
        migrate("check")
        seed_result = run_probe("seed")
        state["bot_id"] = str(seed_result["bot_id"])
        state["other_bot_id"] = str(seed_result["other_bot_id"])
        spoof_result = run_probe("trigger_security")
        assert spoof_result["spoof_transfer_blocked"] is True
        assert spoof_result["spoof_reservation_status"] == "RESERVED"
        result = run_probe("ledger")
        assert result["duplicate_create"] is True
        assert result["first_create"] is False
        assert result["transition_state"] == "SUBMITTING"
        assert result["transition_sequence"] == 2
        assert result["transition_count"] == 2
        assert result["invalid_transition_rejected"] is True
        assert result["same_state_rejected"] is True
        assert result["reservation_mismatch_rejected"] is True
        assert result["retry_without_deadline_rejected"] is True
        assert result["reservation_release_blocked"] is True
        assert result["reservation_transfer_blocked"] is True
        assert result["missing_workspace_update_rows"] == 0
        assert result["wrong_workspace_update_rows"] == 0
        assert result["missing_bot_update_rows"] == 0
        assert result["wrong_bot_update_rows"] == 0
        assert result["wrong_account_update_rows"] == 0
        assert result["reservation_after_wrong_scope"] == "RESERVED"
        assert result["reservation_after_submit"] == "UNKNOWN"
        assert result["paper_unknown_release_succeeded"] is True
        assert result["paper_unknown_transfer_succeeded"] is True
        assert result["unknown_retry_blocked"] is True
        assert result["last_event_clear_rejected"] is True
        assert result["event_mutation_rejected"] is True
        assert result["cross_workspace_visible"] is False
        assert result["cross_bot_visible"] is False
        reconciled = run_probe("reconcile")
        assert reconciled["absent_status"] == "HELD"
        assert reconciled["absent_reason"] == "RESERVATION_BRIDGE_REQUIRED"
        assert reconciled["absent_transition_count"] == 3
        assert reconciled["partial_status"] == "APPLIED"
        assert reconciled["partial_state"] == "PARTIALLY_FILLED"
        assert reconciled["partial_transition_count"] == 4
        assert reconciled["reservation_status"] == "UNKNOWN"
        downgrade = migrate("downgrade", PREVIOUS_HEAD, success=False)
        assert "WP03_RISK_GUARD_DOWNGRADE_BLOCKED" in (downgrade.stdout + downgrade.stderr)
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
