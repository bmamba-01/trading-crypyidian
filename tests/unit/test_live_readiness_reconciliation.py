"""Unit tests cho preflight chỉ đọc và đối soát trạng thái sàn (OPS07F / Task 5 / CRY-463)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.live_preflight_service import (
    LiveReadOnlyPreflightService,
    LocalReconciliationStateProvider,
    PreflightIncidentRecorder,
    ReadOnlyVenueGateway,
)
from trading_bot.domain.execution.live_preflight import (
    LivePreflightSnapshot,
    LocalReconciliationState,
    PreflightReasonCode,
    VenueOrderSummary,
    VenuePositionSnapshot,
    evaluate_live_preflight,
)

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
ACCOUNT_ID = "acc-live-01"
ENV = "LIVE"
WORKSPACE_ID = uuid4()
BOT_ID = uuid4()
ACCOUNT_UUID = uuid4()


def _valid_snapshot(
    *,
    account_id: str = ACCOUNT_ID,
    environment: str = ENV,
    observed_at: datetime = NOW,
    server_time: datetime = NOW,
    permissions: frozenset[str] = frozenset({"READ", "TRADE"}),
    positions: list[VenuePositionSnapshot] | None = None,
    open_orders: list[VenueOrderSummary] | None = None,
    algo_orders: list[VenueOrderSummary] | None = None,
    recent_activity: list[dict] | None = None,
) -> LivePreflightSnapshot:
    return LivePreflightSnapshot(
        account_id=account_id,
        environment=environment,
        observed_at=observed_at,
        server_time=server_time,
        api_permissions=permissions,
        balances={"USDT": Decimal("10000")},
        positions=positions or [],
        open_orders=open_orders or [],
        algo_orders=algo_orders or [],
        recent_activity=recent_activity or [],
    )


def _valid_local_state(
    *,
    account_id: str = ACCOUNT_ID,
    environment: str = ENV,
    known_orders: frozenset[str] = frozenset(),
    known_positions: dict[tuple[str, str], Decimal] | None = None,
    protection_symbols: frozenset[str] = frozenset(),
    unknown_intents: list | None = None,
    last_seq: int | None = None,
) -> LocalReconciliationState:
    return LocalReconciliationState(
        account_id=account_id,
        environment=environment,
        known_client_order_ids=known_orders,
        known_positions=known_positions or {},
        active_protection_symbols=protection_symbols,
        unknown_intent_ids=unknown_intents or [],
        last_processed_stream_seq=last_seq,
    )


def test_matched_clean_state_passes_preflight() -> None:
    """Tất cả trạng thái đều sạch và khớp -> preflight PASSED, không giữ reservation."""
    snap = _valid_snapshot()
    local = _valid_local_state()

    verdict = evaluate_live_preflight(snap, local, now=NOW)

    assert verdict.passed is True
    assert verdict.reason_code == PreflightReasonCode.PASSED
    assert verdict.hold_reservations is False
    assert len(verdict.evidence_hash) == 64


def test_account_and_environment_mismatch_fails() -> None:
    """Sai account_id hoặc environment lập tức fail-closed."""
    snap = _valid_snapshot(account_id="acc-other")
    local = _valid_local_state(account_id="acc-target")
    verdict = evaluate_live_preflight(snap, local, now=NOW)
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.ACCOUNT_MISMATCH
    assert verdict.hold_reservations is True

    snap_env = _valid_snapshot(environment="DEMO")
    local_env = _valid_local_state(environment="LIVE")
    verdict_env = evaluate_live_preflight(snap_env, local_env, now=NOW)
    assert verdict_env.passed is False
    assert verdict_env.reason_code == PreflightReasonCode.ENVIRONMENT_MISMATCH


def test_stale_snapshot_and_excessive_clock_drift_fails() -> None:
    """Snapshot cũ hơn max_age hoặc độ lệch đồng hồ quá lớn -> fail-closed."""
    old_time = NOW - timedelta(seconds=20)
    snap_stale = _valid_snapshot(observed_at=old_time, server_time=old_time)
    verdict = evaluate_live_preflight(snap_stale, _valid_local_state(), now=NOW)
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.SNAPSHOT_STALE

    drift_time = NOW + timedelta(milliseconds=1500)
    snap_drift = _valid_snapshot(observed_at=NOW, server_time=drift_time)
    verdict_drift = evaluate_live_preflight(snap_drift, _valid_local_state(), now=NOW)
    assert verdict_drift.passed is False
    assert verdict_drift.reason_code == PreflightReasonCode.CLOCK_DRIFT_EXCESSIVE


def test_dangerous_api_permissions_or_insufficient_read_fails() -> None:
    """Có quyền rút tiền (WITHDRAW) hoặc thiếu quyền READ -> fail-closed."""
    snap_withdraw = _valid_snapshot(permissions=frozenset({"READ", "TRADE", "WITHDRAW"}))
    verdict = evaluate_live_preflight(snap_withdraw, _valid_local_state(), now=NOW)
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.API_WITHDRAWAL_ENABLED

    snap_no_read = _valid_snapshot(permissions=frozenset({"TRADE"}))
    verdict_no_read = evaluate_live_preflight(snap_no_read, _valid_local_state(), now=NOW)
    assert verdict_no_read.passed is False
    assert verdict_no_read.reason_code == PreflightReasonCode.API_PERMISSIONS_INSUFFICIENT


def test_unknown_intent_holds_entry_gate() -> None:
    """Có intent UNKNOWN cục bộ -> đóng entry gate và giữ nguyên reservation."""
    snap = _valid_snapshot()
    local = _valid_local_state(unknown_intents=[uuid4()])
    verdict = evaluate_live_preflight(snap, local, now=NOW)

    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.UNKNOWN_STATE_HELD
    assert verdict.hold_reservations is True


def test_stream_gap_detected_fails() -> None:
    """Phát hiện sequence gap trong recent_activity -> fail-closed."""
    snap = _valid_snapshot(recent_activity=[{"stream_seq": 105}])
    local = _valid_local_state(last_seq=100)

    verdict = evaluate_live_preflight(snap, local, now=NOW)
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.STREAM_GAP_DETECTED


def test_foreign_position_and_exposure_mismatch() -> None:
    """Vị thế mở ngoại lai hoặc sai lệch số lượng so với sổ cái -> fail-closed."""
    venue_pos = VenuePositionSnapshot(
        symbol="BTCUSDT",
        position_side="BOTH",
        quantity=Decimal("0.5"),
        entry_price=Decimal("90000"),
        mark_price=Decimal("90500"),
        unrealized_pnl=Decimal("250"),
    )
    # 1. Foreign position (sàn có nhưng bot không quản lý)
    snap = _valid_snapshot(positions=[venue_pos])
    local_empty = _valid_local_state(known_positions={})
    verdict_foreign = evaluate_live_preflight(snap, local_empty, now=NOW)
    assert verdict_foreign.passed is False
    assert verdict_foreign.reason_code == PreflightReasonCode.FOREIGN_POSITION
    assert any(i["kind"] == "FOREIGN_POSITION" for i in verdict_foreign.incidents)

    # 2. Exposure mismatch (số lượng khác nhau)
    local_mismatch = _valid_local_state(
        known_positions={("BTCUSDT", "BOTH"): Decimal("0.3")},
        protection_symbols=frozenset({"BTCUSDT"}),
    )
    verdict_mismatch = evaluate_live_preflight(snap, local_mismatch, now=NOW)
    assert verdict_mismatch.passed is False
    assert verdict_mismatch.reason_code == PreflightReasonCode.EXPOSURE_MISMATCH


def test_missing_protection_fails() -> None:
    """Có vị thế mở nhưng thiếu lệnh bảo vệ cắt lỗ (protection) -> fail-closed."""
    venue_pos = VenuePositionSnapshot(
        symbol="ETHUSDT",
        position_side="BOTH",
        quantity=Decimal("5.0"),
        entry_price=Decimal("3000"),
        mark_price=Decimal("3050"),
        unrealized_pnl=Decimal("250"),
    )
    snap = _valid_snapshot(positions=[venue_pos])
    # Bot biết vị thế nhưng symbol ETHUSDT chưa có active protection
    local = _valid_local_state(
        known_positions={("ETHUSDT", "BOTH"): Decimal("5.0")},
        protection_symbols=frozenset(),  # rỗng -> thiếu protection
    )

    verdict = evaluate_live_preflight(snap, local, now=NOW)
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.PROTECTION_MISSING


def test_foreign_order_fails() -> None:
    """Có lệnh mở trên sàn mà client_order_id không khớp lệnh nào của bot."""
    venue_order = VenueOrderSummary(
        symbol="BTCUSDT",
        order_id="123456",
        client_order_id="manual-trader-order-99",
        side="SELL",
        type="LIMIT",
        orig_qty=Decimal("0.1"),
        executed_qty=Decimal("0"),
        price=Decimal("95000"),
        status="NEW",
    )
    snap = _valid_snapshot(open_orders=[venue_order])
    local = _valid_local_state(known_orders=frozenset({"bot-order-01"}))

    verdict = evaluate_live_preflight(snap, local, now=NOW)
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.FOREIGN_ORDER


@pytest.mark.asyncio
async def test_service_with_retry_and_endpoint_failure() -> None:
    """Application service retry chỉ đọc và fail-closed khi sàn lỗi 503/timeout."""

    class FailingGateway(ReadOnlyVenueGateway):
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_authoritative_snapshot(self) -> LivePreflightSnapshot:
            self.calls += 1
            raise RuntimeError("HTTP 503 Service Unavailable")

    class FakeStateProvider(LocalReconciliationStateProvider):
        async def load_local_state(self, **kwargs) -> LocalReconciliationState:
            return _valid_local_state()

    class FakeIncidentRecorder(PreflightIncidentRecorder):
        def __init__(self) -> None:
            self.recorded = []

        async def record_incident(self, **kwargs) -> None:
            self.recorded.append(kwargs)

    failing_gw = FailingGateway()
    recorder = FakeIncidentRecorder()
    service = LiveReadOnlyPreflightService(
        venue_gateway=failing_gw,
        state_provider=FakeStateProvider(),
        incident_recorder=recorder,
        max_retries=2,
        retry_backoff_base_s=0.01,
    )

    verdict = await service.run_preflight(
        workspace_id=WORKSPACE_ID, bot_id=BOT_ID, account_id=ACCOUNT_UUID
    )

    assert failing_gw.calls == 2
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.ENDPOINT_FAILURE
    assert verdict.hold_reservations is True
    assert len(recorder.recorded) == 1
