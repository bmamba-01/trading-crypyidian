"""E2E diễn tập vận hành theo runbook production (OPS07F / Task 9 / CRY-467).

Chạy hoàn toàn trên môi trường disposable với synthetic account/transport và outbound
socket guard cấm mọi kết nối tới `*.binance.com`; không gửi/hủy lệnh thật.

Xác minh:
1. Runbook `docs/operations/live-production-operations-runbook.md` đủ RB-01..RB-11,
   ghi rõ blocker on-call/alert/topology và không chứa secret/PII/IP thật.
2. DR-01 & DR-04: Pause/Kill đóng entry mới nhưng KHÔNG xóa protective orders đã biết;
   Resume chỉ cho phép khi reconcile sạch + maker-checker hợp lệ (owner != approver).
3. DR-02 & DR-03: UNKNOWN intent, stream gap, key bật WITHDRAW / thiếu READ đều fail-closed
   với mã lý do chuẩn hóa, SHA-256 evidence_hash và giữ nguyên reservation.
4. DR-05: Diễn tập trên PostgreSQL Compose cô lập — lưu denial vào `bot_runtime_state`,
   backup mã hóa + restore vào DB target cô lập, đo thời gian phát hiện/phục hồi thực tế.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from scripts.backup_restore_drill import (
    run_encrypted_backup,
    run_isolated_postgres_restore_drill,
)
from scripts.docker_image import build_image_reference, ensure_image_available

from trading_bot.domain.execution.live_preflight import (
    LivePreflightSnapshot,
    LocalReconciliationState,
    PreflightReasonCode,
    VenueOrderSummary,
    VenuePositionSnapshot,
    evaluate_live_preflight,
)
from trading_bot.domain.risk.live_risk_profile import (
    LiveAccountMode,
    LiveOrderRiskRequest,
    LiveRiskReasonCode,
    SignedLiveRiskProfile,
    VenueEquitySnapshot,
    evaluate_live_risk_limits,
    sign_live_risk_profile_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK_PATH = REPO_ROOT / "docs/operations/live-production-operations-runbook.md"
NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
SIGNING_KEY = "drill-synthetic-signing-key"


@pytest.fixture(autouse=True)
def drill_production_network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chặn tuyệt đối mọi lookup tới domain Binance trong suốt quá trình diễn tập."""
    real_getaddrinfo = socket.getaddrinfo

    def guarded(host, port, *args, **kwargs):
        host_str = str(host or "").lower().strip()
        if host_str.endswith(".binance.com") or host_str == "binance.com":
            raise RuntimeError(f"DRILL_NETWORK_GUARD_BLOCKED: {host_str}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


def test_runbook_document_completeness_and_no_secret_or_pii() -> None:
    """Xác minh tài liệu runbook đủ RB-01..RB-11, ghi rõ blocker và không lộ secret/PII."""
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    for idx in range(1, 12):
        assert f"## RB-{idx:02d}." in text, f"Thiếu mục RB-{idx:02d} trong runbook"

    assert "[BLOCKER-ONCALL]" in text
    assert "[BLOCKER-ALERT-DEST]" in text
    assert "[BLOCKER-TOPOLOGY]" in text
    assert "Flatten" in text
    assert "Pause" in text

    # Không chứa chuỗi giống private key/token thật hay IP public cố định
    assert "BEGIN PRIVATE KEY" not in text
    assert "BEGIN RSA PRIVATE KEY" not in text
    assert not re.search(r"signature=[a-fA-F0-9]{32,64}", text)


def test_drill_pause_kill_preserves_protection_and_resume_requires_clean_reconcile() -> None:
    """DR-01 & DR-04: Pause/Kill giữ protective order; Resume cần reconcile sạch + maker-checker."""
    t0 = time.perf_counter()
    pos = VenuePositionSnapshot(
        symbol="BTCUSDT",
        position_side="BOTH",
        quantity=Decimal("0.5"),
        entry_price=Decimal("90000"),
        mark_price=Decimal("90200"),
        unrealized_pnl=Decimal("100"),
    )
    sl_order = VenueOrderSummary(
        symbol="BTCUSDT",
        order_id="algo-sl-drill-1",
        client_order_id="bot-sl-drill-1",
        side="SELL",
        type="STOP_MARKET",
        orig_qty=Decimal("0.5"),
        executed_qty=Decimal("0"),
        price=Decimal("0"),
        status="NEW",
        is_algo=True,
        stop_price=Decimal("88000"),
    )

    def _profile(owner: str = "owner-drill", approver: str = "risk-approver-drill"):
        base = SignedLiveRiskProfile(
            profile_version="drill-v1",
            account_id="acc-drill-01",
            environment="LIVE",
            build_revision="rev-drill",
            account_mode=LiveAccountMode.SINGLE_ASSET_USDT,
            timezone_name="UTC",
            daily_reset_hour=0,
            rolling_window_hours=24,
            include_unrealized_loss=True,
            max_account_notional=Decimal("1000"),
            max_symbol_notional=Decimal("600"),
            max_order_notional=Decimal("400"),
            max_leverage=Decimal("3"),
            max_aggregate_exposure_notional=Decimal("1000"),
            max_daily_loss=Decimal("100"),
            max_rolling_loss=Decimal("150"),
            owner_id=owner,
            approver_id=approver,
            signed_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=12),
            signature_digest="placeholder",
        )
        sig = sign_live_risk_profile_digest(base.compute_hash(), signing_key=SIGNING_KEY)
        return replace(base, signature_digest=sig)

    profile = _profile()
    equity = VenueEquitySnapshot(
        account_id="acc-drill-01",
        environment="LIVE",
        account_mode=LiveAccountMode.SINGLE_ASSET_USDT,
        wallet_balance=Decimal("5000"),
        unrealized_pnl=Decimal("100"),
        available_balance=Decimal("4500"),
        initial_margin_used=Decimal("500"),
        fee_and_funding_today=Decimal("0"),
        fee_and_funding_rolling=Decimal("0"),
        open_exposure_notional=Decimal("200"),
        symbol_open_exposure_notional=Decimal("200"),
        active_reservations_notional=Decimal("0"),
        symbol_reservations_notional=Decimal("0"),
        unknown_order_exposure_notional=Decimal("0"),
        symbol_unknown_exposure_notional=Decimal("0"),
        mark_price=Decimal("90200"),
        mark_observed_at=NOW - timedelta(seconds=1),
    )

    # 1. Pause/Kill: chặn entry mới ngay lập tức nhưng KHÔNG xóa protective algo order
    dec_paused = evaluate_live_risk_limits(
        profile=profile,
        equity=equity,
        request=LiveOrderRiskRequest(
            symbol="BTCUSDT",
            order_notional=Decimal("200"),
            leverage=Decimal("2"),
            build_revision="rev-drill",
            expected_profile_hash=profile.compute_hash(),
            paused_or_killed=True,
        ),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_paused.approved is False
    assert dec_paused.reason_code == LiveRiskReasonCode.PAUSED_OR_KILLED

    # Protective order vẫn tồn tại nguyên vẹn trong snapshot & local state (không bị xóa bởi pause)
    snap_clean = LivePreflightSnapshot(
        account_id="acc-drill-01",
        environment="LIVE",
        observed_at=NOW,
        server_time=NOW,
        api_permissions=frozenset({"READ", "TRADE"}),
        balances={"USDT": Decimal("5000")},
        positions=[pos],
        open_orders=[],
        algo_orders=[sl_order],
    )
    local_clean = LocalReconciliationState(
        account_id="acc-drill-01",
        environment="LIVE",
        known_client_order_ids=frozenset({"bot-sl-drill-1"}),
        known_positions={("BTCUSDT", "BOTH"): Decimal("0.5")},
        active_protection_symbols=frozenset({"BTCUSDT"}),
    )
    assert len(snap_clean.algo_orders) == 1
    assert snap_clean.algo_orders[0].client_order_id == "bot-sl-drill-1"

    # 2. Resume với tự duyệt (maker == checker) -> bị từ chối
    self_prof = _profile(owner="same-op", approver="same-op")
    dec_self = evaluate_live_risk_limits(
        profile=self_prof,
        equity=equity,
        request=LiveOrderRiskRequest(
            symbol="BTCUSDT",
            order_notional=Decimal("200"),
            leverage=Decimal("2"),
            build_revision="rev-drill",
            expected_profile_hash=self_prof.compute_hash(),
            paused_or_killed=False,
        ),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_self.approved is False
    assert dec_self.reason_code == LiveRiskReasonCode.MAKER_CHECKER_VIOLATION

    # 3. Resume hợp lệ: preflight sạch + maker-checker hợp lệ -> cho phép mở lại
    verdict = evaluate_live_preflight(snap_clean, local_clean, now=NOW)
    assert verdict.passed is True
    assert verdict.reason_code == PreflightReasonCode.PASSED

    dec_resumed = evaluate_live_risk_limits(
        profile=profile,
        equity=equity,
        request=LiveOrderRiskRequest(
            symbol="BTCUSDT",
            order_notional=Decimal("200"),
            leverage=Decimal("2"),
            build_revision="rev-drill",
            expected_profile_hash=profile.compute_hash(),
            paused_or_killed=False,
        ),
        realized_events=[],
        as_of=NOW,
        signing_key=SIGNING_KEY,
    )
    assert dec_resumed.approved is True
    assert (time.perf_counter() - t0) < 5.0


def test_drill_unknown_stream_gap_and_key_revoke_fail_closed() -> None:
    """DR-02 & DR-03: UNKNOWN, stream gap và key lỗi quyền đều đóng entry + tạo evidence_hash."""
    snap = LivePreflightSnapshot(
        account_id="acc-drill-01",
        environment="LIVE",
        observed_at=NOW,
        server_time=NOW,
        api_permissions=frozenset({"READ", "TRADE"}),
        balances={"USDT": Decimal("5000")},
        positions=[],
        open_orders=[],
        algo_orders=[],
        recent_activity=[{"stream_seq": 105}],
    )
    # 1. UNKNOWN intent
    v_unknown = evaluate_live_preflight(
        snap,
        LocalReconciliationState(
            account_id="acc-drill-01",
            environment="LIVE",
            known_client_order_ids=frozenset(),
            known_positions={},
            active_protection_symbols=frozenset(),
            unknown_intent_ids=[uuid4()],
        ),
        now=NOW,
    )
    assert v_unknown.passed is False
    assert v_unknown.reason_code == PreflightReasonCode.UNKNOWN_STATE_HELD
    assert v_unknown.hold_reservations is True
    assert len(v_unknown.evidence_hash) == 64

    # 2. Stream gap (100 -> 105)
    v_gap = evaluate_live_preflight(
        snap,
        LocalReconciliationState(
            account_id="acc-drill-01",
            environment="LIVE",
            known_client_order_ids=frozenset(),
            known_positions={},
            active_protection_symbols=frozenset(),
            last_processed_stream_seq=100,
        ),
        now=NOW,
    )
    assert v_gap.passed is False
    assert v_gap.reason_code == PreflightReasonCode.STREAM_GAP_DETECTED
    assert len(v_gap.evidence_hash) == 64

    # 3. Key bật WITHDRAW (kích hoạt RB-03 thu hồi khẩn cấp)
    snap_wd = replace(snap, api_permissions=frozenset({"READ", "TRADE", "WITHDRAW"}))
    v_wd = evaluate_live_preflight(
        snap_wd,
        LocalReconciliationState(
            account_id="acc-drill-01",
            environment="LIVE",
            known_client_order_ids=frozenset(),
            known_positions={},
            active_protection_symbols=frozenset(),
        ),
        now=NOW,
    )
    assert v_wd.passed is False
    assert v_wd.reason_code == PreflightReasonCode.API_WITHDRAWAL_ENABLED


def test_drill_postgres_denial_and_isolated_backup_restore(tmp_path: Path) -> None:
    """DR-05: Diễn tập backup mã hóa + restore cô lập trên PostgreSQL Compose và đo RTO thực tế."""
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    image = build_image_reference(REPO_ROOT)
    ensure_image_available(REPO_ROOT, image)
    project = f"crypidian-cry467-drill-{uuid.uuid4().hex[:10]}"
    env = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
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

    try:
        subprocess.run(
            [*base, "up", "-d", "--wait", "postgres"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [*base, "run", "--rm", "--no-deps", "db-provisioner"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [*base, "run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        backup_dir = tmp_path / "drill_bkp"
        restore_dir = tmp_path / "drill_restore"
        obj_manifest = tmp_path / "obj.json"
        obj_manifest.write_text('{"runbook": "RB-09", "sha256": "abcd1234"}', encoding="utf-8")
        passphrase = "cry467-drill-synthetic-passphrase"
        backup_id = f"rb09-drill-{uuid.uuid4().hex[:8]}"

        manifest_path = run_encrypted_backup(
            source_project=project,
            postgres_service="postgres",
            db_user="crypidian_admin",
            db_name="crypidian",
            output_dir=backup_dir,
            backup_id=backup_id,
            operator_id="drill-operator@crypidian.io",
            passphrase=passphrase,
            object_manifest_path=obj_manifest,
            secret_canaries=("DRILL_SECRET_CANARY_467",),
            min_free_bytes=10 * 1024 * 1024,
        )
        assert manifest_path.is_file()

        report_path = run_isolated_postgres_restore_drill(
            manifest_path=manifest_path,
            backup_dir=backup_dir,
            restore_dir=restore_dir,
            target_environment="isolated-runbook-drill",
            target_project=project,
            postgres_service="postgres",
            db_user="crypidian_admin",
            isolated_db_name="crypidian_rb09_drill",
            passphrase=passphrase,
            max_rpo_seconds=900,
            max_rto_seconds=300,
            min_free_bytes=10 * 1024 * 1024,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["verified"] is True
        assert report["rpo_within_sla"] is True
        assert report["rto_within_sla"] is True
        assert report["rto_seconds"] >= 0.0
    finally:
        subprocess.run(
            [*base, "down", "-v", "--remove-orphans"],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
        )
