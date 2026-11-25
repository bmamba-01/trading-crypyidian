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
            "throttle": "telegram-signal-worker",
            "verify": "migrator",
            "outcome_seed": "migrator",
            "outcome_score": "market-signal-worker",
            "outcome_verify": "migrator",
            "outcome_admin": "admin-web",
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

        # TG-2: tín hiệu trùng vừa gửi bị chặn cooldown; trần 1 tin/giờ chặn; trần 2 cho qua.
        assert run_probe("throttle") == {
            "duplicate": "SIGNAL_COOLDOWN_DUPLICATE",
            "hourly_cap": "SIGNAL_HOURLY_CAP",
            "allowed": None,
            "suppressed_allowed": True,
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

        # CRY-430: chấm kết quả tín hiệu bằng nến đóng sau tín hiệu, rồi Admin + Run Report.
        state.update(run_probe("outcome_seed"))
        scored = run_probe("outcome_score")
        empty = {"examined": 1, "scored": 0, "pending": 3, "data_gap": 0, "invalid": 0}
        assert scored["early"] == empty, scored
        assert scored["after_six"] == {
            "examined": 1, "scored": 2, "pending": 1, "data_gap": 0, "invalid": 0,
        }  # fmt: skip
        assert scored["repeat"] == {
            "examined": 1, "scored": 0, "pending": 1, "data_gap": 0, "invalid": 0,
        }  # fmt: skip
        assert scored["after_gap"] == {
            "examined": 1, "scored": 0, "pending": 0, "data_gap": 1, "invalid": 0,
        }  # fmt: skip
        assert scored["done"]["examined"] == 0
        assert scored["unscoped_rows"] == 0
        assert scored["scoped_rows"] == 3
        assert scored["update_denied"] is True
        assert scored["foreign_bot_insert_denied"] is True

        outcomes = run_probe("outcome_verify")
        h1, h4, h24 = outcomes["rows"]
        assert (h1["horizon"], h1["status"], h1["first_touch"], h1["label"]) == (
            1, "SCORED", "NONE", 0,
        )  # fmt: skip
        assert h1["outcome_r"] == "0.0000"
        assert (h4["horizon"], h4["status"], h4["first_touch"]) == (4, "SCORED", "TP1")
        assert h4["tp1_hit"] is True and h4["tp2_hit"] is True  # noqa: PT018
        assert h4["invalidated"] is False and h4["same_bar_conflict"] is False  # noqa: PT018
        assert h4["bars_to_first_touch"] == 2
        assert h4["outcome_r"] == state["expected_tp1_r"]
        assert h4["label"] == 1
        assert (h24["horizon"], h24["status"], h24["label"]) == (24, "DATA_GAP", None)
        assert {row["method"] for row in outcomes["rows"]} == {"sig-outcome-v2"}
        assert outcomes["immutable"] == {
            "update": "SIGNAL_OUTCOME_IMMUTABLE",
            "delete": "SIGNAL_OUTCOME_IMMUTABLE",
        }
        assert outcomes["report_version"] == "ai1-run-report-v2"
        primary = outcomes["report_primary"]
        assert (primary["scored"], primary["correct"], primary["hit_rate_percent"]) == (
            1, 1, "100.0",
        )  # fmt: skip
        assert outcomes["report_delivery"][0]["key"] == "sent"

        admin = run_probe("outcome_admin")
        assert admin == {
            "status": [200, 200, 200],
            "primary_hit_rate": True,
            "primary_delivered": True,
            "primary_correct_badge": True,
            "gap_badge": True,
            "filtered_empty": True,
            "admin_direct_select_denied": True,
            "admin_telegram_delivery_select_denied": True,
            "admin_telegram_delivery_dml_denied": True,
            "admin_delete_denied": True,
            "owner_ai_analyses_status": 200,
            "operator_ai_analyses_status": 403,
            "operator_run_report_status": 403,
            "operator_run_report_json_status": 403,
            "reviewer_ai_analyses_status": 403,
            "reviewer_run_report_status": 403,
            "reviewer_run_report_json_status": 403,
            "reader_workspace_role_denials": {"operator": True, "analyst_reviewer": True},
            "reader_workspace_owner_allowed": True,
        }, admin
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
