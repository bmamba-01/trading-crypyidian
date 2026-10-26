"""MB04.6: Compose E2E Concurrency, Tightening Breach, DB Roles/RLS và Migration Rehearsal."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

# Bootstrap ngắn đọc gói JSON từ stdin: Windows giới hạn dòng lệnh 32.767 ký tự.
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(MB04_PHASE=p['phase'],MB04_STATE=json.dumps(p.get('state',{})));"
    "exec(compile(p['code'],'workspace_risk_concurrency.py','exec'),{'__name__':'__main__'})"
)

PRE_MB04_HEAD = "f4d6b8c0e2a5"


def test_workspace_risk_concurrency_and_rls_isolation_e2e() -> None:
    """Toàn bộ kịch bản E2E MB04.6 chạy trên Compose PostgreSQL cô lập."""
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-mb04-e2e-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image, "DOCKER_CONTEXT": "desktop-linux"}
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
        *args: str, success: bool = True, stdin: str | None = None
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
        if (result.returncode == 0) != success:
            sys.stderr.write(
                f"\n--- STDOUT ---\n{result.stdout}\n--- STDERR ---\n{result.stderr}\n"
            )
            sys.stderr.flush()
        assert (result.returncode == 0) is success, (
            args,
            result.stdout[-5000:],
            result.stderr[-5000:],
        )
        return result

    probe_code = (root / "tests/e2e/probes/workspace_risk_concurrency.py").read_text(
        encoding="utf-8"
    )

    def run_probe(
        phase: str, state: dict[str, object] | None = None, service: str = "migrator"
    ) -> dict[str, object]:
        payload = json.dumps({"phase": phase, "state": state or {}, "code": probe_code})
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
        assert lines, f"Probe output empty for phase {phase}. stderr:\n{result.stderr}"
        return json.loads(lines[-1])

    ensure_image_available(root, image)

    try:
        # Khởi động PostgreSQL và provisioner
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres", "db-provisioner")

        # Nâng cấp lên head và xác nhận schema sạch
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")

        # Gieo dữ liệu: 1 workspace, 2 PAPER accounts (10k mỗi bên = 20k equity), 2 bots
        seeded = run_probe("seed", service="migrator")
        assert "workspace_id" in seeded
        assert "acc1_id" in seeded
        assert "bot1_id" in seeded

        # ---------------------------------------------------------------------
        # MB-04 fix I1: hàm SECURITY DEFINER do role NOLOGIN/NOBYPASSRLS riêng sở hữu.
        # ---------------------------------------------------------------------
        security = run_probe("security_checks", service="migrator")
        print("security", json.dumps(security))
        assert security["owner_role"] == [False] * 8, security
        assert security["aggregator_has_writes"] is False, security
        assert security["legacy_aggregate_present"] is False, security
        aggregate_fn = security["aggregate"]
        state_fn = security["account_state"]
        assert aggregate_fn["owner"] == "crypidian_risk_aggregator", security
        assert aggregate_fn["security_definer"] is True
        assert aggregate_fn["config"] == ["search_path=pg_catalog, pg_temp"]
        assert aggregate_fn["public_execute"] is False
        assert aggregate_fn["execute"] == {
            "crypidian_trading_runtime": True,
            "crypidian_admin_web": False,
            "crypidian_execution_gateway": False,
            "crypidian_ingestion_worker": False,
            "crypidian_trainer_worker": False,
            "crypidian_app": False,
        }, security
        assert state_fn["owner"] == "crypidian_risk_aggregator", security
        assert state_fn["security_definer"] is False
        assert state_fn["config"] == ["search_path=pg_catalog, pg_temp"]
        assert state_fn["public_execute"] is False
        assert {role for role, allowed in state_fn["execute"].items() if allowed} == {
            "crypidian_trading_runtime",
            "crypidian_admin_web",
        }, security

        # ---------------------------------------------------------------------
        # Kịch bản 1: Headroom Trap Concurrency Barrier
        # ---------------------------------------------------------------------
        # 2 Bot đồng thời xin 2,000 USDT (tổng 4,000 > 3,000 cap).
        # Đúng 1 Bot APPROVED (status='RESERVED'), 1 Bot DENY_NEW_ENTRY (WORKSPACE_CAP_EXCEEDED).
        barrier_res = run_probe("concurrency_barrier", state=seeded, service="trading-runtime")
        results = barrier_res["results"]
        assert len(results) == 2, results

        approved_count = sum(1 for r in results if r["approved"] is True)
        denied_count = sum(1 for r in results if r["approved"] is False)
        assert approved_count == 1, f"Kỳ vọng đúng 1 bot APPROVED, nhận được: {results}"
        assert denied_count == 1, f"Kỳ vọng đúng 1 bot bị DENY, nhận được: {results}"

        denied_entry = next(r for r in results if not r["approved"])
        assert denied_entry["reason_code"] == "WORKSPACE_CAP_EXCEEDED"

        # Query DB trực tiếp: sum(reserved_risk) của status='RESERVED'
        # bằng đúng 2,000 USDT <= 3,000 USDT
        db_sum = Decimal(str(barrier_res["db_reserved_sum"]))
        assert db_sum == Decimal("2000")
        assert db_sum <= Decimal("3000")
        # Minor review: kiểm thêm bằng truy vấn thẳng bảng, không chỉ qua hàm đang được kiểm.
        direct = run_probe("direct_reserved_sum", state=seeded, service="migrator")
        assert Decimal(direct["reserved_sum"]) == Decimal("2000"), direct
        assert direct["reserved_count"] == "1", direct

        # MB-04 fix I6: replay chỉ trả kết quả cho reservation còn RESERVED/UNKNOWN và còn hạn.
        replay = run_probe("replay_dead_reservation", state=seeded, service="trading-runtime")
        print("replay", json.dumps(replay))
        assert replay["released_first"] == [True, "APPROVED"], replay
        assert replay["released_replay"] == "ReservationConflict", replay
        assert replay["expired_first"] == [True, "APPROVED"], replay
        assert replay["expired_replay"] == "ReservationConflict", replay

        # ---------------------------------------------------------------------
        # Kịch bản 2: Admin Cap Tightening Race & Breach
        # ---------------------------------------------------------------------
        # Bot 1 có vị thế mở 1,200 USDT. Admin siết Root cap xuống 5% (1,000 USDT).
        tightened_state = run_probe("admin_tighten", state=seeded, service="migrator")
        # Bot 2 xin reserve 1,000 USDT -> CAPACITY_BREACH_AFTER_TIGHTENING.
        # Vị thế mở của Bot 1 KHÔNG bị flatten.
        tighten_res = run_probe("bot2_breach", state=tightened_state, service="trading-runtime")
        bot2_eval = tighten_res["bot2_result"]
        assert bot2_eval["approved"] is False
        assert bot2_eval["reason_code"] == "CAPACITY_BREACH_AFTER_TIGHTENING"
        assert tighten_res["bot1_trade_status"] == "OPEN"

        # ---------------------------------------------------------------------
        # Kịch bản 3: DB Roles & Cross-Bot RLS Isolation
        # ---------------------------------------------------------------------
        # Chạy dưới role crypidian_trading_runtime
        rls_res = run_probe("rls_checks", state=seeded, service="trading-runtime")
        assert rls_res["has_table_privilege"] is True
        assert rls_res["rolbypassrls"] is False
        # Đặt app.bot_id = bot1_id rồi SELECT reservation bot2_id -> 0 rows
        assert rls_res["bot2_reservations_count"] == 0
        # Gọi get_workspace_risk_aggregate scalar thành công, không rò rỉ bot_id/symbol
        agg = rls_res["aggregate"]
        assert agg["workspace_id"] == seeded["workspace_id"]
        assert Decimal(agg["total_equity_usdt"]) == Decimal("20000")
        assert Decimal(agg["total_open_risk_usdt"]) == Decimal("1200")
        assert agg["active_bot_count"] == 2
        assert agg["has_unknown_equity"] is False
        assert agg["unknown_after_mark_removal"] is True
        assert agg["function_execute"] is True
        assert agg["public_execute"] is False
        assert "bot_id" not in agg
        assert "symbol" not in agg

        # Kiểm tra các nhánh equity mà fixture concurrency không bao phủ.
        edge_cases = run_probe("equity_edge_cases", state=seeded, service="migrator")
        print("edge_cases", json.dumps(edge_cases))
        assert edge_cases == {
            "long_pnl_correct": True,
            "short_pnl_correct": True,
            "partial_exit_and_costs_correct": True,
            "future_mark_unknown": True,
            "realized_pnl_correct": True,
            "missing_seed_unknown": True,
            "future_seed_unknown": True,
            "missing_fill_unknown": True,
            "unsupported_quote_unknown": True,
            "unsupported_realized_quote_unknown": True,
            # MB-04 fix C3b/C3c/I2/I3/minor.
            "open_risk_counts_trade_opened_after_as_of": True,
            "backfilled_stale_mark_unknown": True,
            "negative_account_equity_unknown": True,
            "future_as_of_keeps_live_reservation": True,
            "account_state_matches_aggregate": True,
        }

        # ---------------------------------------------------------------------
        # Kịch bản 4: Migration Rehearsal (Upgrade -> Downgrade Guard -> Upgrade)
        # ---------------------------------------------------------------------
        # Chèn risk_profile_version có max_open_risk_fraction = 0.5
        guard_profile = run_probe("insert_custom_profile_guard", state=seeded, service="migrator")

        # Downgrade bị chặn bởi exception MB04_DOWNGRADE_GUARD_CUSTOM_ACCOUNT_CAPS_EXIST
        blocked = compose(
            "run",
            "--rm",
            "--no-deps",
            "migrator",
            "alembic",
            "downgrade",
            PRE_MB04_HEAD,
            success=False,
        )
        assert "MB04_DOWNGRADE_GUARD_CUSTOM_ACCOUNT_CAPS_EXIST" in blocked.stderr

        # Xóa row test -> downgrade thành công
        run_probe("cleanup_custom_profile_guard", state=guard_profile, service="migrator")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "downgrade", PRE_MB04_HEAD)

        # MB-04 fix I1: role aggregator tạo sẵn với LOGIN (cấp cluster) bị migration từ chối.
        run_probe("precreate_unsafe_aggregator", service="migrator")
        unsafe = compose(
            "run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head", success=False
        )
        assert "MB04_RISK_AGGREGATOR_ROLE_UNSAFE" in unsafe.stderr
        run_probe("harden_aggregator", service="migrator")

        # Upgrade lại lên head và alembic check sạch
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        assert run_probe("security_checks", service="migrator")["aggregate"]["owner"] == (
            "crypidian_risk_aggregator"
        )

    finally:
        # Dọn dẹp hoàn toàn containers và volumes
        compose("down", "--volumes", "--remove-orphans")
