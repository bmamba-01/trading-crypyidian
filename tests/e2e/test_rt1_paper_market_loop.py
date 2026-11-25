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
from decimal import Decimal
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
    "card_consensus": "admin-web",
    "add_minutes": "migrator",
    "add_stop_bar": "migrator",
    "verify": "migrator",
    "breaker": "trading-runtime",
    "risk_appetite": "admin-web",
    "exit_draft": "admin-web",
    "exit_publish": "admin-web",
    "exit_resolve": "trading-runtime",
    "pause_resume": "migrator",
    "outcome": "trading-runtime",
    "macro_write": "ingestion-worker",
    "macro_read": "trading-runtime",
    "derivatives_write": "ingestion-worker",
    "derivatives_read": "trading-runtime",
    "listings_write": "ingestion-worker",
    "listings_read": "trading-runtime",
    "run_report": "report-reader",
    "analyst": "migrator",
    "tg_seed": "migrator",
    "tg_results": "telegram-signal-worker",
    "tg_verify": "migrator",
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
        "--profile",
        "signal",
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
        assert "c9d0e1f2a3b4 (head)" in heads.stdout
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        state.update(run("seed"))
        # CK-11: bộ quét đọc nến 4h thật (đủ 21 ngày), 1h thiếu 21 ngày bị loại kèm lý do;
        # lượt quét ghi audit_event và hiện trong nhật ký.
        assert run("scanner") == {
            "reader_role": "crypidian_admin_report_reader",
            "universe": 1,
            "passed_4h": ["BTCUSDT"],
            "deterministic": True,
            "excluded_1h": ["Thiếu dữ liệu 21 ngày để tính biên và thay đổi tuần."],
            "runs": 1,
            "run_summary_matches": True,
        }
        # SIG-6: cảnh báo đồng thuận đo trên nến đã lưu, đọc bằng report-reader qua RLS; mã
        # không có nến và workspace khác trả rỗng (không bịa dữ liệu, không rò workspace).
        assert run("card_consensus") == {
            "role": "crypidian_admin_report_reader",
            "enough_bars": True,
            "within_limit": True,
            "positive_prices": True,
            "status": "ok",
            "missing": 0,
            "other_workspace": 0,
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

        # CK-7: report-reader đọc vị thế/sự kiện theo quyền; admin-web gọi service ghép bảng.
        opened = run("web_read")
        assert opened["role"] == "crypidian_admin_report_reader"
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
        assert opened["hidden_without_membership"] == 0  # RLS reader cần membership
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
        assert run("add_stop_bar") == {"added": 2, "funding_seeded": 1}
        _sleep_until(minute_base + timedelta(minutes=4, seconds=3))
        closed = run("runtime")
        assert closed["network_attempts"] == []
        assert closed["ledger"] == {
            "intents": ["FILLED"],
            "trades": ["CLOSED"],
            "fills": ["ENTRY", "EXIT"],
        }
        final = run("verify")
        # FX-3 lát 2: kỳ funding seed tại mốc nến 3 được ghi đúng số trước nến chạm stop (nến 3
        # vẫn được xử lý: lệnh đóng bằng STOP_LOSS ở nến 3, không bị bỏ qua).
        closed_dispatch = closed["readiness"]["market_dispatch"]
        assert closed_dispatch["funding_events"] == 1, closed_dispatch
        assert closed_dispatch["funding_missing"] == 0, closed_dispatch
        assert closed_dispatch["errors"] == 0, closed_dispatch
        assert final["funding"] == {
            "rows": 1,
            "amount_matches": [True],
            "event_ids": [True],
            "at_funding_time": [True],
            "trade_total_matches": True,
        }, final["funding"]
        closed_read = run("web_read")
        assert closed_read["positions"] == []  # đã đóng bằng stop → không còn vị thế mở
        assert closed_read["monitor_slots"] == "0/2"
        assert closed_read["monitor_daily_loss_positive"] is True  # lệnh lỗ vừa đóng được tính
        rerun = run("runtime")
        assert rerun["ledger"] == closed["ledger"]
        # Restart: recovery đối soát funding_event_ids ↔ trade_cost FUNDING vẫn khớp.
        assert rerun["readiness"]["execution_capabilities"]["paper_recovery"] == "ready", (
            json.dumps(rerun["readiness"], default=str)
        )
        assert rerun["readiness"]["market_dispatch"]["funding_events"] == 0
        assert run("verify") == final

        # TG-3: trade PAPER đã đóng → đúng một tin kết quả (và một tin mở, một tin tổng hợp) tới
        # sender ghi nhớ, dưới role signal-worker thật; chạy lại/khởi động lại không gửi trùng.
        assert run("tg_seed") == {"tg_seeded": True}
        results = run("tg_results")
        assert results["sent"] == [3, 0, 0], results
        assert results["restart_messages"] == 0, results
        assert results["recipients"] == [777000111], results
        assert (results["opens"], results["closes"], results["summaries"]) == (1, 1, 1), results
        assert results["close_header"] == "KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT", results
        assert results["close_reason"] is True, results
        assert results["close_loss_pnl"] is True, results
        assert results["close_r_negative"] is True, results
        assert results["summary_lists_bot"] is True, results
        assert 0 < results["max_length"] <= 4096, results
        assert results["privileges"] == {
            "trade_table_select": False,
            "trade_net_pnl_select": True,
            "trade_lineage_select": False,
            "trade_any_update": False,
            "fill_any_insert": False,
            "order_intent_any_select": False,
        }, results
        assert results["rls"] == {"own_bot": 1, "other_bot": 0}, results
        assert results["lineage_denied"] is True, results
        assert run("tg_verify") == {
            "notifications": [["CLOSED", "SENT", 1, True], ["OPENED", "SENT", 1, True]],
            "reports": [["SENT", 1, True, "4:00:00"]],
        }

        # Lý do candidate lưu bền bằng role runtime thật: idempotent và chặn bot khác (RLS).
        assert run("outcome") == {
            "rows": 1,
            "user": "crypidian_trading_runtime",
            "other_bot_denied": True,
        }

        # AI-1: Run Report tất định từ ledger thật (lệnh lỗ chạm stop → R âm, có MAE).
        report = run("run_report")
        assert report["deterministic"] is True, report
        assert report["cli_payload_matches"] is True, report
        # CRY-380: lưu lần đầu, lần hai trùng hash bỏ qua; mọi UPDATE/DELETE bị trigger chặn.
        assert report["stored"] == [True, False], report
        assert report["stored_rows"] == 1, report
        assert report["immutable"] == [True, True], report
        assert report["report_rights"] == [False, False, True], report
        assert report["stored_rows"] == 1, report
        assert report["manual_reader_account_rows"] == 1, report
        assert report["manual_reader_cross_workspace_rows"] == 0, report
        assert report["manual_reader_sensitive_account_column_denied"] is True, report
        assert report["closed_trades"] == 1, report
        assert report["r_mean"] is not None, report
        assert Decimal(report["r_mean"]) < 0, report
        assert report["mae_mean"] is not None, report
        assert report["candidates"] == report["candidates_with_intent"] == 1, report

        # AI-2: kết quả hợp lệ và kết quả bịa số đều được lưu để kiểm toán; chỉ một hợp lệ.
        assert run("analyst") == {
            "runs": 2,
            "valid_runs": 1,
            "admin_web_select": False,
            "admin_web_insert": False,
            "forced_rls": True,
            "report_reader_structured_output_select": True,
            "report_reader_raw_stdout_select": False,
            "report_reader_analyst_insert": False,
            # AI-3: admin-web không đọc trực tiếp; reader chỉ nhận cột kết quả cần thiết.
            "backtest_admin_web_select": False,
            "backtest_admin_web_insert": False,
            "backtest_runtime_select": False,
            "backtest_forced_rls": True,
            "report_reader_backtest_result_select": True,
            "report_reader_backtest_insert": False,
            "backtests": 1,
            "backtest_duplicate_rejected": True,
            "context_sql_null": 1,
            "context_stored": 1,
        }

        # AI-4: ingestion ghi lịch vĩ mô (append-only), runtime đọc trạng thái point-in-time.
        assert run("macro_write") == {"written": 3, "update_denied": True}
        assert run("macro_read") == {
            "during_blackout": True,
            "during_fresh": True,
            "evidence": 3,
            "after_blackout": False,
            "before_fetch_unknown": True,
            "admin_web_insert": False,
        }
        # AI-4 lát 2: ảnh chụp phái sinh append-only, đọc point-in-time bằng role runtime.
        written = run("derivatives_write")
        assert written["written"] == 2, written
        assert written["update_delete_denied"] == [True, True], written
        assert run("derivatives_read") == {
            "symbols": ["ETHUSDT", "BTCUSDT"],
            "oi_change": "10.00",
            "before_collect": 0,
            "other_workspace": 0,
            "admin_web_insert": False,
        }
        # AI-4 lát 3: lần kiểm exchangeInfo append-only; lịch gỡ chỉ thấy sau lần kiểm phát hiện.
        listed = run("listings_write")
        assert listed["update_delete_denied"] == [True, True], listed
        # Lần kiểm không đổi nội dung được ghi (entries SQL NULL) và là mốc kiểm mới nhất.
        assert listed["last"] == "2026-11-16T10:30:00+00:00", listed
        assert run("listings_read") == {
            "at_risk": [["ZECUSDT", "20/11 15:00"]],
            "changes": [["DELIST_SCHEDULED", "ZECUSDT"]],
            "before_at_risk": [],
            "admin_web_insert": False,
            "blocks": {
                "zec_after": "SYMBOL_DELIST_SCHEDULED",
                "zec_before": None,
                "c0_after": None,
                "unlisted_after": "SYMBOL_NOT_LISTED",
                "no_data": None,
            },
            "exits": {
                "zec_far": None,
                "zec_within_24h": "DELIST_EXIT",
                "c0_within": None,
                "unlisted": "DELIST_EXIT",
            },
        }

        # FX-2: ngắt mạch drawdown đo bằng role runtime trên ledger thật.
        before = run("breaker")
        assert before["baseline_at"] is None, before
        assert Decimal(before["actual_peak"]) == Decimal(before["seed"]), before  # lệnh lỗ
        assert before["actual_tripped"] is False, before
        assert before["stressed_tripped"] is True, before  # vốn = 89% đỉnh ⇒ sụt 11%
        resumed = run("pause_resume")
        after = run("breaker")
        assert after["baseline_at"] == resumed["resumed_at"], (after, resumed)
        # Sau "Tiếp tục", đỉnh tính lại từ vốn đã chốt tại mốc (seed + lỗ đã chốt).
        assert Decimal(after["actual_peak"]) == Decimal(before["seed"]) + Decimal(
            before["realized"]
        ), after
        assert after["actual_tripped"] is False, after
        assert after["max_drawdown_fraction"] == "0.10", after  # mặc định FX-2
        assert after["mild_tripped"] is False, after  # sụt 6% < 10%

        # CRY-428: chỉnh khẩu vị rủi ro qua service thật (admin-web), runtime dùng ngưỡng mới.
        appetite = run("risk_appetite")
        assert appetite["before_default"] is True, appetite
        assert appetite["stale_mfa"] == "403:Cần xác thực MFA gần đây.", appetite
        assert appetite["saved_status"] == "published", appetite
        assert appetite["risk_profile_changed"] is True, appetite
        assert appetite["unchanged_status"] == "unchanged", appetite
        assert appetite["after_drawdown"] == "0.05", appetite
        assert appetite["after_version"] == appetite["saved_version"], appetite
        assert appetite["after_profile_version"] == appetite["before_profile_version"] + 1
        assert appetite["after_daily_loss"] == appetite["expected_daily_loss"], appetite
        assert appetite["bot_daily_loss"] == appetite["expected_daily_loss"], appetite
        assert appetite["bot_loss_today"] is not None, appetite  # đọc ledger bằng role admin-web
        assert appetite["exit_release_count"] >= 12, appetite
        assert appetite["audit_actions"][-3:] == [
            "risk_profile.version_created",
            "settings.published",
            "risk_appetite.published",
        ], appetite
        assert appetite["audit_reasons"] == ["CRY-428 E2E: siết drawdown 5%"], appetite
        tightened = run("breaker")
        assert tightened["max_drawdown_fraction"] == "0.05", tightened
        assert tightened["mild_tripped"] is True, tightened  # sụt 6% ≥ 5%

        # CRY-428: ghi đè thoát lệnh theo bot; publish bắt buộc có backtest bản nháp.
        drafted = run("exit_draft")
        assert drafted["invalid_errors"] == ["exit.take_profit_atr_multiple"], drafted
        assert drafted["valid"] is True, drafted
        assert drafted["warnings"] == ["EXIT_BACKTEST_REQUIRED"], drafted
        assert drafted["blocked"] == [409, ["exit"]], drafted
        assert drafted["forged"] == "denied", drafted
        state["exit_draft_id"] = drafted["draft_id"]
        state["exit_draft_revision"] = drafted["draft_revision"]
        # Script backtest thật của owner (container migrator, cùng image). RT-1 không có quyền
        # BACKTEST_RESEARCH nên coin bị loại → INCONCLUSIVE, nhưng bằng chứng vẫn được lưu.
        script = subprocess.run(
            [
                sys.executable,
                "scripts/run_proposal_backtest.py",
                "--bot-draft",
                state["bot_id"],
                "--workspace-id",
                state["workspace_id"],
                "--project",
                project,
                "--max-symbols",
                "1",
                "--workers",
                "1",
                "--timeout",
                "600",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert script.returncode == 0, (script.stdout[-3000:], script.stderr[-3000:])
        backtested = json.loads(script.stdout)
        assert backtested["draft_id"] == drafted["draft_id"], backtested
        assert backtested["record_id"], backtested
        assert backtested["verdict"] == "INCONCLUSIVE", backtested
        published_exit = run("exit_publish")
        assert published_exit["warnings"] == [], published_exit
        assert published_exit["status"] == "PUBLISHED", published_exit
        assert published_exit["exit"] == {"stop_atr_multiple": "1.5", "max_hold_bars": 60}
        assert published_exit["owner_rows"] == [[drafted["draft_id"], "INCONCLUSIVE", False]], (
            published_exit
        )
        assert published_exit["accepted"] == [
            f"proposal_backtest={backtested['record_id']} verdict=INCONCLUSIVE"
        ], published_exit
        resolved = run("exit_resolve")
        assert resolved == {
            "release_id": "h01-v1",
            "exit_override": {"stop_atr_multiple": "1.5", "max_hold_bars": 60},
            "hash_ok": True,
        }, resolved

        assert final["decisions"] == ["CANDIDATE"]
        assert final["intents"] == [
            {
                "status": "FILLED",
                "full": True,
                "feature_snapshot_id_len": 64,
                "context_provider": "rt1-postgres-paper-context-v3",
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
