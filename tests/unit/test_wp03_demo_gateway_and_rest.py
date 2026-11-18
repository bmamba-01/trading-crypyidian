"""Unit & offline E2E tests cho WP-03A.2 (CRY-289) và WP-03B.2 (CRY-291)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceHttpRequest,
    BinanceHttpResponse,
    CancelAccepted,
    DemoApiCredentials,
    DemoUsdMRestAdapter,
    OrderFound,
    SubmissionFailed,
)
from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.adapters.binance_usdm.gateway import BinanceUsdMExchangeGateway
from trading_bot.adapters.binance_usdm.gateway_boundary import (
    ApiKeyCredentials,
    BinanceExecutionMode,
    HostBoundaryViolationError,
    WsRouteKind,
    resolve_demo_credentials_from_mapping,
    validate_ws_route,
)
from trading_bot.adapters.binance_usdm.submission_errors import SubmissionOutcome
from trading_bot.domain.execution.venue_orders import (
    ExecutionMode,
    VenueOrderIntent,
    VenueOrderState,
)
from trading_bot.ports.exchange_gateway import ProbeCategory, ReadinessStatus


class _FakeClock:
    def __init__(self, now_ms: int = 1_700_000_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class _FakeDemoGatewayTransport:
    def __init__(self, server_time_ms: int) -> None:
        self.server_time_ms = server_time_ms
        self.requests: list[BinanceHttpRequest] = []

    def send(self, request: BinanceHttpRequest) -> BinanceHttpResponse:
        self.requests.append(request)
        if "/fapi/v1/time" in request.url:
            return BinanceHttpResponse(
                200, (), json.dumps({"serverTime": self.server_time_ms}).encode("utf-8")
            )
        if "/fapi/v3/account" in request.url:
            return BinanceHttpResponse(
                200, (), json.dumps({"canTrade": True, "assets": []}).encode("utf-8")
            )
        return BinanceHttpResponse(404, (), b"{}")


def test_wp03a2_ws_routes_and_demo_secret_resolution_and_server_time_offset() -> None:
    # 1. WS routes /public, /market, /private hợp lệ cho Demo, cấm production
    assert (
        validate_ws_route(
            mode=BinanceExecutionMode.DEMO,
            ws_base_url="wss://demo-fstream.binance.com",
            route_kind=WsRouteKind.PRIVATE,
        )
        == "wss://demo-fstream.binance.com/private"
    )
    with pytest.raises(HostBoundaryViolationError):
        validate_ws_route(
            mode=BinanceExecutionMode.DEMO,
            ws_base_url="wss://fstream.binance.com",
            route_kind=WsRouteKind.MARKET,
        )

    # 2. Tách secret Demo: không đọc placeholder binance_api_key
    assert (
        resolve_demo_credentials_from_mapping(
            {"binance_api_key": "live_key_1234", "binance_api_secret": "live_secret_1234"}
        )
        is None
    )
    creds = resolve_demo_credentials_from_mapping(
        {"binance_demo_api_key": "demo_key_1234", "binance_demo_api_secret": "demo_sec_1234"}
    )
    assert creds is not None
    assert creds.masked_api_key() == "***1234"
    assert "demo_sec_1234" not in repr(creds)

    # 3. Gateway sync server-time offset và áp dụng vào signed_timestamp_ms
    clock = _FakeClock(1_700_000_000_000)
    transport = _FakeDemoGatewayTransport(server_time_ms=1_700_000_000_450)
    gateway = BinanceUsdMExchangeGateway(
        account_id="demo-acc-1",
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://demo-fstream.binance.com",
        credentials=ApiKeyCredentials(api_key="demo_key_1234", secret_key="demo_sec_1234"),
        transport=transport,
        clock_ms=clock,
    )
    offset = gateway.sync_server_time()
    assert offset == 450
    assert gateway.signed_timestamp_ms() == 1_700_000_000_450
    assert gateway.ws_route_url(WsRouteKind.PUBLIC) == "wss://demo-fstream.binance.com/public"
    report = gateway.check_readiness()
    assert report.probes[ProbeCategory.PUBLIC_DATA].status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.USER_DATA].status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.TRADE_CAPABILITY].status is ReadinessStatus.READY


class _TimeoutAfterAcceptVenueTransport:
    """Fake venue nhận lệnh vào sổ nhưng rớt HTTP response ở lần submit đầu."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, object]] = {}
        self.trades: list[dict[str, object]] = []
        self._dropped_once = False

    def send(self, request: BinanceHttpRequest) -> BinanceHttpResponse:
        if request.method == "POST" and "/fapi/v1/order" in request.url:
            body_text = request.body.decode("ascii")
            params = dict(part.split("=", 1) for part in body_text.split("&") if "=" in part)
            cid = params["newClientOrderId"]
            if cid not in self.orders:
                self.orders[cid] = {
                    "orderId": 9001,
                    "clientOrderId": cid,
                    "symbol": params["symbol"],
                    "status": "NEW",
                    "type": params["type"],
                    "origQty": params["quantity"],
                    "executedQty": "0",
                    "updateTime": 1_700_000_000_100,
                }
            if not self._dropped_once:
                self._dropped_once = True
                raise TimeoutError("Kết nối bị rớt sau khi sàn đã nhận lệnh.")
            return BinanceHttpResponse(200, (), json.dumps(self.orders[cid]).encode("utf-8"))

        if request.method == "GET" and "/fapi/v1/order" in request.url:
            query = request.url.split("?", 1)[1]
            params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
            cid = params.get("origClientOrderId", "")
            if cid in self.orders:
                return BinanceHttpResponse(200, (), json.dumps(self.orders[cid]).encode("utf-8"))
            return BinanceHttpResponse(
                400, (), json.dumps({"code": -2013, "msg": "Order does not exist."}).encode("utf-8")
            )

        if request.method == "DELETE" and "/fapi/v1/order" in request.url:
            query = request.url.split("?", 1)[1]
            params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
            cid = params.get("origClientOrderId", "")
            if cid in self.orders:
                self.orders[cid]["status"] = "CANCELED"
                return BinanceHttpResponse(200, (), json.dumps(self.orders[cid]).encode("utf-8"))
            return BinanceHttpResponse(
                400, (), json.dumps({"code": -2011, "msg": "Unknown order sent."}).encode("utf-8")
            )

        if request.method == "GET" and "/fapi/v1/userTrades" in request.url:
            return BinanceHttpResponse(200, (), json.dumps(self.trades).encode("utf-8"))

        return BinanceHttpResponse(404, (), b"{}")


def _sample_intent() -> VenueOrderIntent:
    return VenueOrderIntent(
        intent_id=uuid4(),
        workspace_id="ws-1",
        account_id="acc-demo-1",
        idempotency_key="idem-001",
        reservation_id=uuid4(),
        plan_hash="a" * 64,
        settings_version="1",
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.01"),
        client_order_id="cryp-demo-001",
        created_at=datetime(2026, 11, 18, 0, 0, tzinfo=UTC),
        order_type="LIMIT",
        limit_price=Decimal("50000"),
        mode=ExecutionMode.DEMO,
        attempt_count=0,
    ).begin_submission(at=datetime(2026, 11, 18, 0, 0, tzinfo=UTC))


def test_wp03b2_timeout_after_accept_reconciles_single_order_and_cancel() -> None:
    clock = _FakeClock(1_700_000_000_000)
    venue = _TimeoutAfterAcceptVenueTransport()
    adapter = DemoUsdMRestAdapter(
        target=BinanceDemoTarget(account_id="acc-demo-1", credential_ref="ref-1"),
        workspace_id="ws-1",
        credentials=DemoApiCredentials(
            workspace_id="ws-1",
            account_id="acc-demo-1",
            credential_ref="ref-1",
            api_key="demo_key_1234",
            api_secret="demo_sec_1234",
        ),
        transport=venue,
        clock_ms=clock,
    )
    intent = _sample_intent()

    # 1. Submit đầu bị timeout sau khi sàn đã nhận -> phân loại UNKNOWN (không retry mù)
    res = adapter.submit_order(intent)
    assert isinstance(res, SubmissionFailed)
    assert res.failure.outcome is SubmissionOutcome.UNKNOWN
    assert len(venue.orders) == 1

    # 2. Reconcile có giới hạn tìm thấy đúng 1 lệnh đã tồn tại trên sàn
    evidence = adapter.reconcile_order_with_history(intent, max_queries=2)
    assert isinstance(evidence.order, OrderFound)
    assert evidence.order.venue_order_id == "9001"
    assert evidence.authoritative_absence is False

    # 3. Cancel độc lập trả CancelAccepted và cảnh báo racing_fill_possible=True
    canceled = adapter.cancel_order(intent, venue_order_id="9001")
    assert isinstance(canceled, CancelAccepted)
    assert canceled.state is VenueOrderState.CANCELED
    assert canceled.racing_fill_possible is True
    assert len(venue.orders) == 1


def test_wp03b2_rate_limit_429_respects_retry_after_with_fake_clock() -> None:
    clock = _FakeClock(1_700_000_000_000)

    class _RateLimitTransport:
        def send(self, _request: BinanceHttpRequest) -> BinanceHttpResponse:
            return BinanceHttpResponse(
                429,
                (("Retry-After", "12"),),
                json.dumps({"code": -1003, "msg": "Too many requests."}).encode("utf-8"),
            )

    adapter = DemoUsdMRestAdapter(
        target=BinanceDemoTarget(account_id="acc-demo-1", credential_ref="ref-1"),
        workspace_id="ws-1",
        credentials=DemoApiCredentials(
            workspace_id="ws-1",
            account_id="acc-demo-1",
            credential_ref="ref-1",
            api_key="demo_key_1234",
            api_secret="demo_sec_1234",
        ),
        transport=_RateLimitTransport(),
        clock_ms=clock,
    )
    res = adapter.submit_order(_sample_intent())
    assert isinstance(res, SubmissionFailed)
    assert res.retry_not_before == datetime.fromtimestamp((clock.now_ms + 12_000) / 1000, tz=UTC)
