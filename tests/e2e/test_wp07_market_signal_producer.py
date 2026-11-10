"""WP-07B Compose E2E: nến đóng → BotDecision candidate → Telegram giả."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(WP07M_PHASE=p['phase'],WP07M_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'wp07_market_signal_producer.py','exec'),{'__name__':'__main__'})"
)


def test_closed_usdm_fixture_creates_one_candidate_delivery_without_execution() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp07-market-signal-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "signal",
        "--profile",
        "signal-producer",
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
        assert result.returncode == 0, (args, result.stdout[-5000:], result.stderr[-5000:])
        return result

    probe = (root / "tests/e2e/probes/wp07_market_signal_producer.py").read_text(encoding="utf-8")
    state = {
        name: str(uuid.uuid4())
        for name in (
            "workspace_id",
            "membership_id",
            "binding_id",
            "subscription_id",
            "account_id",
            "root_settings_id",
            "risk_profile_id",
            "bot_config_id",
            "instrument_id",
        )
    }
    state["now"] = datetime.now(UTC).isoformat()

    def run_probe(phase: str) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": state, "code": probe})
        service = {
            "seed": "migrator",
            "produce": "market-signal-worker",
            "prepare_filter_change": "migrator",
            "deliver": "telegram-signal-worker",
            "verify": "migrator",
        }[phase]
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            service,
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        lines = result.stdout.strip().splitlines()
        assert lines, (phase, result.stderr[-3000:])
        return cast(dict[str, Any], json.loads(lines[-1]))

    def migrate(*args: str) -> None:
        compose("run", "--rm", "--no-deps", "migrator", "alembic", *args)

    image_created = ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        migrate("upgrade", "head")
        migrate("check")
        seeded = run_probe("seed")
        state.update(seeded)

        producer = run_probe("produce")
        assert producer["dispatched_count"] == 1, producer
        assert producer["decision_count"] == 1, producer

        filter_change = run_probe("prepare_filter_change")
        assert filter_change == {"pending_receipt_seeded": True, "filter_changed": True}

        delivery = run_probe("deliver")
        assert delivery == {
            "sent": 1,
            "messages": 1,
            "recipient_chat_ids": [987654321],
            "message_contains_reference_only": True,
        }
        assert run_probe("deliver") == {
            "sent": 0,
            "messages": 0,
            "recipient_chat_ids": [],
            "message_contains_reference_only": True,
        }

        verified = run_probe("verify")
        assert verified["candidates"] == 1
        assert verified["sent_receipts"] == 1
        assert verified["filtered_receipts"] == 1
        assert verified["pending_mismatch_was_filtered_without_attempt"] is True
        assert verified["pending_evidence"] == 0
        assert set(verified["order_rows"].values()) == {0}
        assert verified["producer_has_no_execution_or_delivery_privilege"] is True
        assert verified["delivery_has_no_market_or_evidence_write_privilege"] is True
        assert verified["live_source_preferred_over_backfill"] is True
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", image],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=120,
                check=False,
            )
