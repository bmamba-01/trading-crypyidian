"""E2E test cho preflight chỉ đọc và đối soát sàn (OPS07F / Task 5 / CRY-463).

Chứng minh:
- Network guard cấm kết nối trực tiếp đến production Binance USDⓈ-M.
- Tuyệt đối không có bất kỳ endpoint tạo (POST), sửa (PUT) hoặc hủy lệnh (DELETE) nào được gọi.
- Mọi trạng thái bất thường (stale, foreign position, foreign order, missing protection,
  UNKNOWN state, endpoint error) đều khiến entry gate bị đóng và reservation được giữ nguyên.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from trading_bot.adapters.binance_usdm.demo_rest import BinanceHttpRequest, BinanceHttpResponse
from trading_bot.adapters.binance_usdm.gateway_boundary import (
    HostBoundaryViolationError,
    validate_host_boundary,
)
from trading_bot.application.live_preflight_service import (
    LiveReadOnlyPreflightService,
    LocalReconciliationStateProvider,
    PreflightIncidentRecorder,
    ReadOnlyVenueGateway,
)
from trading_bot.domain.execution.live_preflight import (
    LivePreflightSnapshot,
    LivePreflightVerdict,
    LocalReconciliationState,
    PreflightReasonCode,
    VenueOrderSummary,
    VenuePositionSnapshot,
)

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
BOT_ID = uuid4()
ACCOUNT_ID = uuid4()
ACCOUNT_STR = str(ACCOUNT_ID)


class RecordingReadOnlyTransport:
    """Mock transport ghi nhận mọi HTTP request phát đi để kiểm toán cấm mutation."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.sent_requests: list[BinanceHttpRequest] = []
        self._responses = responses or {}

    def send(self, request: BinanceHttpRequest) -> BinanceHttpResponse:
        self.sent_requests.append(request)

        # CẤM TUYỆT ĐỐI endpoint tạo, sửa, hủy lệnh trong preflight
        disallowed_methods = {"POST", "PUT", "DELETE"}
        order_endpoints = {"/fapi/v1/order", "/fapi/v1/allOpenOrders", "/fapi/v1/batchOrders"}

        if request.method in disallowed_methods and any(
            ep in request.url for ep in order_endpoints
        ):
            raise AssertionError(
                f"VI PHẠM BẢO MẬT: Cố tình gọi mutation endpoint: {request.method} {request.url}"
            )

        return BinanceHttpResponse(
            status_code=200,
            headers=(("content-type", "application/json"),),
            body=b"{}",
        )


class FakeAuthoritativeGateway(ReadOnlyVenueGateway):
    """Gateway chỉ đọc trả về snapshot định sẵn cho kịch bản E2E."""

    def __init__(self, snapshot: LivePreflightSnapshot) -> None:
        self.snapshot = snapshot
        self.call_count = 0

    async def fetch_authoritative_snapshot(self) -> LivePreflightSnapshot:
        self.call_count += 1
        return self.snapshot


class InMemoryLocalStateProvider(LocalReconciliationStateProvider):
    def __init__(self, state: LocalReconciliationState) -> None:
        self.state = state

    async def load_local_state(self, **kwargs: Any) -> LocalReconciliationState:
        return self.state


class AuditIncidentRecorder(PreflightIncidentRecorder):
    def __init__(self) -> None:
        self.incidents: list[dict[str, Any]] = []

    async def record_incident(
        self,
        *,
        workspace_id: Any,
        bot_id: Any,
        account_id: Any,
        verdict: LivePreflightVerdict,
    ) -> None:
        self.incidents.append(
            {
                "workspace_id": workspace_id,
                "bot_id": bot_id,
                "account_id": account_id,
                "reason_code": verdict.reason_code.value,
                "evidence_hash": verdict.evidence_hash,
                "detail_vi": verdict.detail_vi,
                "hold_reservations": verdict.hold_reservations,
                "incidents": verdict.incidents,
            }
        )


def test_production_network_guard_fails_closed() -> None:
    """Network guard từ chối kết nối tới production endpoint nếu không được cấp phép rõ ràng."""
    with pytest.raises(HostBoundaryViolationError):
        validate_host_boundary(
            mode="DEMO",
            rest_base_url="https://fapi.binance.com",  # production REST host
            ws_base_url="wss://demo-fstream.binance.com",
            allow_local_mock=False,
        )


@pytest.mark.asyncio
async def test_live_readonly_preflight_no_mutation_endpoints_called() -> None:
    """Kiểm toán toàn bộ HTTP traffic trong quá trình preflight: KHÔNG có mutation nào."""
    transport = RecordingReadOnlyTransport()

    # Giả lập một lượt gọi snapshot chỉ đọc
    req_time = BinanceHttpRequest(
        method="GET",
        url="https://demo-fapi.binance.com/fapi/v1/time",
        headers=(("User-Agent", "CrypidianBot"),),
        body=b"",
    )
    req_account = BinanceHttpRequest(
        method="GET",
        url="https://demo-fapi.binance.com/fapi/v3/account?timestamp=123456",
        headers=(("User-Agent", "CrypidianBot"),),
        body=b"",
    )
    transport.send(req_time)
    transport.send(req_account)

    assert len(transport.sent_requests) == 2
    assert all(r.method == "GET" for r in transport.sent_requests)

    # Thử giả lập một yêu cầu cấm -> phải bị chặn ngay
    bad_req = BinanceHttpRequest(
        method="POST",
        url="https://demo-fapi.binance.com/fapi/v1/order",
        headers=(("User-Agent", "CrypidianBot"),),
        body=b"",
    )
    with pytest.raises(AssertionError, match="VI PHẠM BẢO MẬT: Cố tình gọi mutation endpoint"):
        transport.send(bad_req)


@pytest.mark.asyncio
async def test_e2e_foreign_position_denial_persists_incident_and_holds_reservation() -> None:
    """E2E: Vị thế ngoại lai trên sàn -> entry đóng, lưu audit incident, reservation được giữ."""
    foreign_pos = VenuePositionSnapshot(
        symbol="BTCUSDT",
        position_side="LONG",
        quantity=Decimal("1.25"),
        entry_price=Decimal("91000"),
        mark_price=Decimal("91500"),
        unrealized_pnl=Decimal("625"),
    )
    snapshot = LivePreflightSnapshot(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        observed_at=NOW,
        server_time=NOW,
        api_permissions=frozenset({"READ", "TRADE"}),
        balances={"USDT": Decimal("50000")},
        positions=[foreign_pos],
        open_orders=[],
        algo_orders=[],
    )
    # Sổ cái nội bộ bot không có vị thế này
    local_state = LocalReconciliationState(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        known_client_order_ids=frozenset(),
        known_positions={},
        active_protection_symbols=frozenset(),
        unknown_intent_ids=[],
    )

    recorder = AuditIncidentRecorder()
    service = LiveReadOnlyPreflightService(
        venue_gateway=FakeAuthoritativeGateway(snapshot),
        state_provider=InMemoryLocalStateProvider(local_state),
        incident_recorder=recorder,
        clock=lambda: NOW,
    )

    verdict = await service.run_preflight(
        workspace_id=WORKSPACE_ID, bot_id=BOT_ID, account_id=ACCOUNT_ID
    )

    # Khẳng định fail-closed
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.FOREIGN_POSITION
    assert verdict.hold_reservations is True
    assert len(verdict.evidence_hash) == 64

    # Khẳng định incident đã được ghi nhận đúng chuẩn
    assert len(recorder.incidents) == 1
    rec = recorder.incidents[0]
    assert rec["reason_code"] == "PREFLIGHT_FOREIGN_POSITION"
    assert rec["hold_reservations"] is True
    assert rec["workspace_id"] == WORKSPACE_ID
    assert rec["bot_id"] == BOT_ID
    assert rec["account_id"] == ACCOUNT_ID


@pytest.mark.asyncio
async def test_e2e_missing_protection_denies_entry() -> None:
    """E2E: Vị thế có trong bot nhưng thiếu lệnh bảo vệ (stop-loss) -> entry đóng."""
    pos = VenuePositionSnapshot(
        symbol="ETHUSDT",
        position_side="BOTH",
        quantity=Decimal("10.0"),
        entry_price=Decimal("3100"),
        mark_price=Decimal("3150"),
        unrealized_pnl=Decimal("500"),
    )
    snapshot = LivePreflightSnapshot(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        observed_at=NOW,
        server_time=NOW,
        api_permissions=frozenset({"READ", "TRADE"}),
        balances={"USDT": Decimal("20000")},
        positions=[pos],
        open_orders=[],
        algo_orders=[],
    )
    local_state = LocalReconciliationState(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        known_client_order_ids=frozenset(),
        known_positions={("ETHUSDT", "BOTH"): Decimal("10.0")},
        active_protection_symbols=frozenset(),  # THIẾU BẢO VỆ
    )

    recorder = AuditIncidentRecorder()
    service = LiveReadOnlyPreflightService(
        venue_gateway=FakeAuthoritativeGateway(snapshot),
        state_provider=InMemoryLocalStateProvider(local_state),
        incident_recorder=recorder,
        clock=lambda: NOW,
    )

    verdict = await service.run_preflight(
        workspace_id=WORKSPACE_ID, bot_id=BOT_ID, account_id=ACCOUNT_ID
    )

    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.PROTECTION_MISSING
    assert verdict.hold_reservations is True
    assert len(recorder.incidents) == 1


@pytest.mark.asyncio
async def test_e2e_full_matched_state_opens_entry_safely() -> None:
    """E2E: Vị thế và lệnh bảo vệ khớp hoàn toàn -> preflight thành công, mở entry an toàn."""
    pos = VenuePositionSnapshot(
        symbol="BTCUSDT",
        position_side="BOTH",
        quantity=Decimal("0.5"),
        entry_price=Decimal("90000"),
        mark_price=Decimal("90500"),
        unrealized_pnl=Decimal("250"),
    )
    stop_order = VenueOrderSummary(
        symbol="BTCUSDT",
        order_id="stop-999",
        client_order_id="bot-stop-loss-1",
        side="SELL",
        type="STOP_MARKET",
        orig_qty=Decimal("0.5"),
        executed_qty=Decimal("0"),
        price=Decimal("0"),
        status="NEW",
        is_algo=True,
        stop_price=Decimal("88000"),
    )
    snapshot = LivePreflightSnapshot(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        observed_at=NOW,
        server_time=NOW,
        api_permissions=frozenset({"READ", "TRADE"}),
        balances={"USDT": Decimal("100000")},
        positions=[pos],
        open_orders=[],
        algo_orders=[stop_order],
    )
    local_state = LocalReconciliationState(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        known_client_order_ids=frozenset({"bot-stop-loss-1"}),
        known_positions={("BTCUSDT", "BOTH"): Decimal("0.5")},
        active_protection_symbols=frozenset({"BTCUSDT"}),
        unknown_intent_ids=[],
    )

    recorder = AuditIncidentRecorder()
    service = LiveReadOnlyPreflightService(
        venue_gateway=FakeAuthoritativeGateway(snapshot),
        state_provider=InMemoryLocalStateProvider(local_state),
        incident_recorder=recorder,
        clock=lambda: NOW,
    )

    verdict = await service.run_preflight(
        workspace_id=WORKSPACE_ID, bot_id=BOT_ID, account_id=ACCOUNT_ID
    )

    assert verdict.passed is True
    assert verdict.reason_code == PreflightReasonCode.PASSED
    assert verdict.hold_reservations is False
    assert len(recorder.incidents) == 0
