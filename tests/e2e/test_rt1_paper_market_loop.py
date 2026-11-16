"""RT-1 lát 2 Compose E2E: nến REST → CANDIDATE thật → PAPER tự vào và đóng lệnh bằng stop.

Đi qua mọi ranh giới thật: producer `market-signal-worker` dưới role của nó, `trading-runtime`
với provider/event loop production (`runtime_market_dispatch_enabled=True`), migration mark
fallback, RLS và trigger ledger. Không có Binance/sàn: chỉ nến đã seed nguồn
`BINANCE_USDM_REST_BACKFILL`, socket Python bị chặn trong pha runtime.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from scripts.docker_image import build_image_reference, ensure_image_available

PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(RT1_PHASE=p['phase'],RT1_STATE=json.dumps(p['state']));"
    "exec(compile(p['code'],'rt1_paper_market_loop.py','exec'),{'__name__':'__main__'})"
)
SERVICES = {
    "seed": "migrator",
    "genesis": "trading-runtime",
    "produce": "market-signal-worker",
    "runtime": "trading-runtime",
    "mark_check": "trading-runtime",
    "web_read": "admin-web",
    "calendar": "admin-web",
    "scanner": "admin-web",
    "add_minutes": "migrator",
    "add_stop_bar": "migrator",
    "verify": "migrator",
}


def _sleep_until(moment: datetime) -> None:
    remaining = (moment - datetime.now(UTC)).total_seconds()
    if remaining > 0:
        time.sleep(remaining)


def test_rest_candles_drive_paper_entry_fill_and_stop_exit_idempotently() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-rt1-paper-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
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
        assert result.returncode == 0, (args[:6], result.stdout[-5000:], result.stderr[-5000:])
        return result

    probe = (root / "tests/e2e/probes/rt1_paper_market_loop.py").read_text(encoding="utf-8")
    state: dict[str, Any] = {
        "workspace_id": str(uuid.uuid4()),
        "now": datetime.now(UTC).isoformat(),
    }

    def run(phase: str) -> dict[str, Any]:
        payload = json.dumps({"phase": phase, "state": state, "code": probe})
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            SERVICES[phase],
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=payload,
        )
        lines = result.stdout.strip().splitlines()
        assert lines, (phase, result.stderr[-3000:])
        return cast(dict[str, Any], json.loads(lines[-1]))

    image_created = ensure_image_available(root, image)
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        heads = compose("run", "--rm", "--no-deps", "migrator", "alembic", "heads")
        assert "b8d0f2a4c6e8 (head)" in heads.stdout
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        state.update(run("seed"))
        # CK-11: bộ quét đọc nến 4h thật (đủ 21 ngày), 1h thiếu 21 ngày bị loại kèm lý do;
        # lượt quét ghi audit_event và hiện trong nhật ký.
        assert run("scanner") == {
            "universe": 1,
            "passed_4h": ["BTCUSDT"],
            "deterministic": True,
            "excluded_1h": ["Thiếu dữ liệu 21 ngày để tính biên và thay đổi tuần."],
            "runs": 1,
            "run_summary_matches": True,
        }
        # CK-10: lịch sự kiện bằng role admin-web — audit đủ 3 thao tác, xóa mềm, RLS.
        assert run("calendar") == {
            "role": "crypidian_admin_web",
            "listed_titles": ["CPI Mỹ"],
            "updated_titles": ["CPI Mỹ (lõi)"],
            "after_delete": 0,
            "audit_actions": [
                "economic_event.create",
                "economic_event.update",
                "economic_event.delete",
            ],
            "soft_deleted_rows": 1,
            "delete_denied": True,
            "other_workspace_rows": 0,
        }
        assert run("genesis") == {"genesis": True}

        produced = run("produce")
        assert produced["dispatched_count"] == 1, produced
        assert produced["decision_count"] == 1, produced
        state.update(run("add_minutes"))
        minute_base = datetime.fromisoformat(state["minute_base"])
        # Nến 0..2 phải đóng và khả dụng (close + 1s) theo đồng hồ thật trước khi runtime chạy.
        _sleep_until(minute_base + timedelta(minutes=3, seconds=3))

        # Tick 1: candidate → intent → khớp ở nến 1m đầu sau eligible_at → theo dõi 2 nến.
        first = run("runtime")
        assert first["provider"] == "PostgresPaperDecisionContextProvider"
        assert first["network_attempts"] == []
        assert first["readiness"]["execution_capabilities"] == {
            "paper_recovery": "ready",
            "market_dispatch": "running",
            "exchange_execution": "disabled",
        }, json.dumps(first["readiness"], default=str)  # chuỗi JSON: pytest không cắt chi tiết
        dispatch = first["readiness"]["market_dispatch"]
        assert dispatch["errors"] == 0, dispatch
        assert first["ledger"] == {"intents": ["FILLED"], "trades": ["OPEN"], "fills": ["ENTRY"]}

        # Mark chỉ có nguồn REST backfill: vốn account có vị thế mở vẫn xác định được.
        marked = run("mark_check")
        assert marked == {"known": True, "reason": None, "open_positions": 1, "mark_refs": 1}

        # CK-7: admin-web đọc đúng vị thế mở (fill, SL kế hoạch, UPnL) và quyết định tại nến.
        opened = run("web_read")
        assert opened["role"] == "crypidian_admin_web"
        assert opened["positions"] == [
            {
                "side": opened["positions"][0]["side"],
                "fills": 1,
                "has_initial_stop": True,
                "stop_below_entry": True,
                "upnl_matches_quantity": True,
            }
        ], opened
        assert opened["decision_status"] == "CANDIDATE"
        assert opened["decision_rows"] >= 1
        assert opened["hidden_without_membership"] == 0  # RLS admin-web cần membership
        # CK-12: chỗ chạy khớp ledger (1 lệnh mở / max_positions 2), giới hạn từ cấu hình hiệu lực.
        assert opened["monitor_slots"] == "1/2", opened
        assert (opened["monitor_daily_limit"], opened["monitor_rolling_limit"]) == (200.0, 500.0)
        assert opened["monitor_note"] is None
        # CK-9: admin-web đếm đúng nến có bản sửa và khoảng trống ingest chưa lấp.
        assert opened["revised_1h_bars"] == 1
        assert opened["open_gaps_1h"] == 1

        # Chạy lại không có nến mới: không ghi thêm gì.
        before = run("verify")
        again = run("runtime")
        assert again["ledger"] == first["ledger"]
        assert again["readiness"]["market_dispatch"]["errors"] == 0
        after = run("verify")
        assert after["fills"] == before["fills"]
        assert after["intents"] == before["intents"]

        # Nến 1m chạm stop → đóng lệnh; nến sau đó không được dùng.
        assert run("add_stop_bar") == {"added": 2}
        _sleep_until(minute_base + timedelta(minutes=4, seconds=3))
        closed = run("runtime")
        assert closed["network_attempts"] == []
        assert closed["ledger"] == {
            "intents": ["FILLED"],
            "trades": ["CLOSED"],
            "fills": ["ENTRY", "EXIT"],
        }
        final = run("verify")
        closed_read = run("web_read")
        assert closed_read["positions"] == []  # đã đóng bằng stop → không còn vị thế mở
        assert closed_read["monitor_slots"] == "0/2"
        assert closed_read["monitor_daily_loss_positive"] is True  # lệnh lỗ vừa đóng được tính
        rerun = run("runtime")
        assert rerun["ledger"] == closed["ledger"]
        assert run("verify") == final

        assert final["decisions"] == ["CANDIDATE"]
        assert final["intents"] == [
            {
                "status": "FILLED",
                "full": True,
                "feature_snapshot_id_len": 64,
                "context_provider": "rt1-postgres-paper-context-v1",
                "has_bot_decision": True,
            }
        ]
        [trade] = final["trades"]
        assert trade["status"] == "CLOSED"
        assert trade["pnl_consistent"] is True
        assert trade["loss"] is True
        entry, exit_fill = final["fills"]
        assert entry["role"] == "ENTRY"
        assert entry["occurred_at"] == final["minute_open"]["0"]
        assert entry["bar_source"] == "BINANCE_USDM_REST_BACKFILL"
        assert exit_fill["role"] == "EXIT"
        assert trade["closed_at"] < final["minute_open"]["4"]
        assert final["exit_reasons"] == ["STOP_LOSS"]
        assert final["reservations"] == ["TRANSFERRED"]
        assert final["venue_rows"] == {
            "exchange_order": 1,
            "exchange_order_with_venue_id": 0,
            "venue_order_intent": 0,
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
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", image],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=120,
                check=False,
            )
