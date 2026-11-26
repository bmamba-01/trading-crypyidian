"""Unit tests cho WP-03C.2 (CRY-293): Partial fill và lệnh bảo vệ qua Algo Service."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from trading_bot.adapters.binance_usdm.algo_rest import (
    AlgoOrderAccepted,
    AlgoOrderCanceled,
    AlgoOrderFound,
    AlgoSubmissionFailed,
    BinanceUsdMAlgoRestAdapter,
)
from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceHttpRequest,
    BinanceHttpResponse,
    DemoApiCredentials,
)
from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.adapters.binance_usdm.order_schemas import (
    ConditionalAlgoOrderType,
    parse_algo_update,
)
from trading_bot.application.algo_protection_coordinator import AlgoProtectionCoordinator
from trading_bot.domain.execution.algo_protection import (
    AlgoOrderParams,
    AlgoParameterError,
    PositionMode,
    PositionSide,
    ProtectionGateState,
    WorkingType,
)


class _FakeAlgoTransport:
    """Fake HTTP transport cho Binance USDⓈ-M Algo Service, ghi lại mọi request."""

    def __init__(self) -> None:
        self.requests: list[BinanceHttpRequest] = []
        self._next_algo_id = 5000
        self.open_orders: dict[int, dict[str, object]] = {}
        self.fail_next_place: BinanceHttpResponse | None = None
        self.fail_cancel_algo_ids: set[int] = set()
        self.triggered_on_cancel_ids: set[int] = set()

    def send(self, request: BinanceHttpRequest) -> BinanceHttpResponse:
        self.requests.append(request)
        parsed = urlparse(request.url)
        assert parsed.path in {"/fapi/v1/algoOrder", "/fapi/v1/openAlgoOrders"}, parsed.path
        qs = parse_qs(parsed.query)

        if request.method == "POST" and parsed.path == "/fapi/v1/algoOrder":
            if self.fail_next_place is not None:
                resp = self.fail_next_place
                self.fail_next_place = None
                return resp
            self._next_algo_id += 1
            aid = self._next_algo_id
            payload: dict[str, object] = {
                "algoId": aid,
                "clientAlgoId": qs["clientAlgoId"][0],
                "algoType": "CONDITIONAL",
                "orderType": qs["type"][0],
                "algoStatus": "NEW",
                "symbol": qs["symbol"][0],
                "triggerPrice": qs.get("triggerPrice", ["50000"])[0],
                "updateTime": 1_730_000_000_000,
            }
            self.open_orders[aid] = payload
            return BinanceHttpResponse(200, (), json.dumps(payload).encode("utf-8"))

        if request.method == "DELETE" and parsed.path == "/fapi/v1/algoOrder":
            aid = int(qs["algoId"][0])
            if aid in self.fail_cancel_algo_ids:
                return BinanceHttpResponse(
                    400, (), b'{"code": -2011, "msg": "Unknown order sent."}'
                )
            existing = self.open_orders.pop(aid, None)
            if existing is None:
                return BinanceHttpResponse(
                    400, (), b'{"code": -2011, "msg": "Unknown order sent."}'
                )
            return BinanceHttpResponse(
                200,
                (),
                json.dumps(
                    {
                        "algoId": aid,
                        "clientAlgoId": existing["clientAlgoId"],
                        "code": "200",
                        "msg": "success",
                    }
                ).encode("utf-8"),
            )

        if request.method == "GET" and parsed.path == "/fapi/v1/algoOrder":
            aid = int(qs["algoId"][0])
            if aid in self.triggered_on_cancel_ids:
                return BinanceHttpResponse(
                    200,
                    (),
                    json.dumps(
                        {
                            "algoId": aid,
                            "clientAlgoId": f"triggered-{aid}",
                            "algoType": "CONDITIONAL",
                            "orderType": "STOP_MARKET",
                            "algoStatus": "TRIGGERED",
                            "symbol": "BTCUSDT",
                            "triggerPrice": "60000",
                            "actualOrderId": "99887766",
                            "actualType": "MARKET",
                            "actualQty": "0.5",
                            "updateTime": 1_730_000_001_000,
                        }
                    ).encode("utf-8"),
                )
            order = self.open_orders.get(aid)
            if order is None:
                return BinanceHttpResponse(400, (), b'{"code": -2013, "msg": "Order not found."}')
            return BinanceHttpResponse(200, (), json.dumps(order).encode("utf-8"))

        if request.method == "GET" and parsed.path == "/fapi/v1/openAlgoOrders":
            return BinanceHttpResponse(
                200, (), json.dumps(list(self.open_orders.values())).encode("utf-8")
            )

        return BinanceHttpResponse(404, (), b"{}")


def _make_adapter(transport: _FakeAlgoTransport) -> BinanceUsdMAlgoRestAdapter:
    return BinanceUsdMAlgoRestAdapter(
        target=BinanceDemoTarget(account_id="acc-demo", credential_ref="ref-demo"),
        credentials=DemoApiCredentials(
            workspace_id="ws-demo",
            account_id="acc-demo",
            credential_ref="ref-demo",
            api_key="demo-key-safe",
            api_secret="demo-secret-safe",
        ),
        transport=transport,
        clock_ms=lambda: 1_730_000_000_000,
    )


def test_algo_order_params_enforce_usdm_official_constraints() -> None:
    """Xác minh đầy đủ ràng buộc tham số Algo USDⓈ-M (callbackRate <= 20, closePosition, HEDGE)."""
    # 1. TRAILING_STOP_MARKET chấp nhận callbackRate=20 (Change Log 23/10/2026) và từ chối 20.1/0.05
    valid_trailing = AlgoOrderParams(
        symbol="BTCUSDT",
        side="SELL",
        order_type=ConditionalAlgoOrderType.TRAILING_STOP_MARKET,
        client_algo_id="trail-ok-1",
        quantity=Decimal("0.25"),
        callback_rate=Decimal("20"),
        activate_price=Decimal("70000"),
        reduce_only=True,
    )
    q = valid_trailing.to_query_params()
    assert q["algoType"] == "CONDITIONAL"
    assert q["callbackRate"] == "20"
    assert q["reduceOnly"] == "true"

    with pytest.raises(AlgoParameterError, match="callbackRate"):
        AlgoOrderParams(
            symbol="BTCUSDT",
            side="SELL",
            order_type=ConditionalAlgoOrderType.TRAILING_STOP_MARKET,
            client_algo_id="trail-bad-hi",
            quantity=Decimal("0.25"),
            callback_rate=Decimal("20.1"),
        )

    with pytest.raises(AlgoParameterError, match="callbackRate"):
        AlgoOrderParams(
            symbol="BTCUSDT",
            side="SELL",
            order_type=ConditionalAlgoOrderType.TRAILING_STOP_MARKET,
            client_algo_id="trail-bad-lo",
            quantity=Decimal("0.25"),
            callback_rate=Decimal("0.05"),
        )

    # 2. closePosition=true cấm quantity và reduceOnly=true; chỉ hỗ trợ STOP_MARKET/TP_MARKET
    with pytest.raises(AlgoParameterError, match="quantity"):
        AlgoOrderParams(
            symbol="BTCUSDT",
            side="SELL",
            order_type=ConditionalAlgoOrderType.STOP_MARKET,
            client_algo_id="cp-qty",
            trigger_price=Decimal("60000"),
            quantity=Decimal("0.5"),
            close_position=True,
        )
    with pytest.raises(AlgoParameterError, match="reduceOnly"):
        AlgoOrderParams(
            symbol="BTCUSDT",
            side="SELL",
            order_type=ConditionalAlgoOrderType.STOP_MARKET,
            client_algo_id="cp-ro",
            trigger_price=Decimal("60000"),
            close_position=True,
            reduce_only=True,
        )
    with pytest.raises(AlgoParameterError, match="closePosition"):
        AlgoOrderParams(
            symbol="BTCUSDT",
            side="SELL",
            order_type=ConditionalAlgoOrderType.STOP,
            client_algo_id="cp-limit",
            trigger_price=Decimal("60000"),
            price=Decimal("59900"),
            close_position=True,
        )

    # 3. Chế độ HEDGE cấm reduceOnly=true và cấm position_side=BOTH
    with pytest.raises(AlgoParameterError, match="HEDGE"):
        AlgoOrderParams(
            symbol="BTCUSDT",
            side="SELL",
            order_type=ConditionalAlgoOrderType.STOP_MARKET,
            client_algo_id="hedge-ro",
            trigger_price=Decimal("60000"),
            quantity=Decimal("0.5"),
            position_mode=PositionMode.HEDGE,
            position_side=PositionSide.LONG,
            reduce_only=True,
        )

    # 4. STOP/TAKE_PROFIT bắt buộc price + triggerPrice; STOP_MARKET cấm price
    valid_stop_limit = AlgoOrderParams(
        symbol="ETHUSDT",
        side="SELL",
        order_type=ConditionalAlgoOrderType.STOP,
        client_algo_id="stop-limit-1",
        trigger_price=Decimal("3000"),
        price=Decimal("2990"),
        quantity=Decimal("2"),
        working_type=WorkingType.CONTRACT_PRICE,
    )
    assert valid_stop_limit.to_query_params()["price"] == "2990"
    with pytest.raises(AlgoParameterError, match="price"):
        AlgoOrderParams(
            symbol="ETHUSDT",
            side="SELL",
            order_type=ConditionalAlgoOrderType.STOP_MARKET,
            client_algo_id="sm-price",
            trigger_price=Decimal("3000"),
            price=Decimal("2990"),
            quantity=Decimal("2"),
        )


def test_partial_fill_successive_fill_conservative_replace_and_orphan_cleanup() -> None:
    """Partial fill -> successive fill (place mới trước, cancel cũ sau) -> full exit -> dọn algo."""
    transport = _FakeAlgoTransport()
    adapter = _make_adapter(transport)
    coordinator = AlgoProtectionCoordinator(
        algo_port=adapter, clock=lambda: datetime(2026, 11, 26, 4, 0, tzinfo=UTC)
    )
    intent_id = uuid4()

    # 1. Entry chưa fill (0) -> không gửi lệnh Algo
    res0 = coordinator.on_confirmed_entry_fill(
        entry_intent_id=intent_id,
        symbol="BTCUSDT",
        protective_side="SELL",
        confirmed_position_qty=Decimal("0"),
        trigger_price=Decimal("60000"),
    )
    assert res0.assessment.gate_state is ProtectionGateState.NO_EXPOSURE
    assert res0.placed_algo_id is None
    assert len(transport.requests) == 0

    # 2. Partial fill 0.4 BTC -> đặt lệnh bảo vệ 0.4 BTC
    res1 = coordinator.on_confirmed_entry_fill(
        entry_intent_id=intent_id,
        symbol="BTCUSDT",
        protective_side="SELL",
        confirmed_position_qty=Decimal("0.4"),
        trigger_price=Decimal("60000"),
    )
    assert res1.placed_algo_id == 5001
    assert res1.entry_gate_open is True
    assert res1.assessment.gate_state is ProtectionGateState.COVERED
    assert res1.assessment.confirmed_protective_qty == Decimal("0.4")

    # 3. Successive fill lên 1.0 BTC -> đặt lệnh 5002 (1.0 BTC) TRƯỚC khi hủy 5001
    res2 = coordinator.on_confirmed_entry_fill(
        entry_intent_id=intent_id,
        symbol="BTCUSDT",
        protective_side="SELL",
        confirmed_position_qty=Decimal("1.0"),
        trigger_price=Decimal("60000"),
    )
    assert res2.placed_algo_id == 5002
    assert res2.canceled_algo_ids == (5001,)
    assert res2.entry_gate_open is True
    assert res2.assessment.confirmed_protective_qty == Decimal("1.0")
    # Kiểm chứng thứ tự HTTP: POST 5002 xảy ra trước DELETE 5001
    methods = [req.method for req in transport.requests]
    assert methods == ["POST", "POST", "DELETE"]

    # 4. Full exit về 0 nhưng chưa xác nhận no_open_trigger -> KHÔNG được hủy mồ côi
    res_wait = coordinator.cleanup_orphan_protections(
        symbol="BTCUSDT",
        confirmed_position_qty=Decimal("0"),
        no_open_trigger_confirmed=False,
    )
    assert res_wait.canceled_algo_ids == ()
    assert res_wait.assessment.orphan_algo_ids == (5002,)

    # 5. Đã xác nhận vị thế 0 và không còn open trigger -> dọn sạch algo mồ côi
    res_clean = coordinator.cleanup_orphan_protections(
        symbol="BTCUSDT",
        confirmed_position_qty=Decimal("0"),
        no_open_trigger_confirmed=True,
    )
    assert res_clean.canceled_algo_ids == (5002,)
    assert res_clean.assessment.orphan_algo_ids == ()
    assert res_clean.entry_gate_open is True
    assert transport.open_orders == {}


def test_algo_rejection_and_triggered_order_fill_linkage() -> None:
    """ALGO_UPDATE.REJECTED đóng gate & leo thang; trigger liên kết actualOrderId <-> fills."""
    transport = _FakeAlgoTransport()
    adapter = _make_adapter(transport)
    coordinator = AlgoProtectionCoordinator(
        algo_port=adapter, clock=lambda: datetime(2026, 11, 26, 4, 5, tzinfo=UTC)
    )
    intent_id = uuid4()

    # Đặt bảo vệ thành công cho 0.5 BTC
    res1 = coordinator.on_confirmed_entry_fill(
        entry_intent_id=intent_id,
        symbol="BTCUSDT",
        protective_side="SELL",
        confirmed_position_qty=Decimal("0.5"),
        trigger_price=Decimal("60000"),
    )
    assert res1.placed_algo_id == 5001

    # Nhận ALGO_UPDATE.TRIGGERED sinh actualOrderId=778899
    trig_update = parse_algo_update(
        {
            "e": "ALGO_UPDATE",
            "E": 1_730_000_005_000,
            "T": 1_730_000_005_000,
            "o": {
                "s": "BTCUSDT",
                "aid": 5001,
                "caid": coordinator.links_for(symbol="BTCUSDT")[0].client_algo_id,
                "at": "CONDITIONAL",
                "o": "STOP_MARKET",
                "X": "TRIGGERED",
                "tp": "60000",
                "ai": "778899",
                "act": "MARKET",
                "aq": "0.5",
            },
        }
    )
    coordinator.on_algo_update(trig_update, confirmed_position_qty=Decimal("0.5"))
    linked = coordinator.on_triggered_order_fill(
        actual_order_id="778899", trade_id="tr-9001", filled_qty=Decimal("0.5")
    )
    assert linked is not None
    assert linked.triggered_order_id == "778899"
    assert linked.linked_fill_ids == ("tr-9001",)

    # Đặt lệnh bảo vệ mới cho vị thế khác rồi bị ALGO_UPDATE.REJECTED -> đóng gate & leo thang
    res2 = coordinator.on_confirmed_entry_fill(
        entry_intent_id=uuid4(),
        symbol="ETHUSDT",
        protective_side="SELL",
        confirmed_position_qty=Decimal("3.0"),
        trigger_price=Decimal("3100"),
    )
    assert res2.placed_algo_id == 5002
    eth_client_id = coordinator.links_for(symbol="ETHUSDT")[0].client_algo_id
    rej_update = parse_algo_update(
        {
            "e": "ALGO_UPDATE",
            "E": 1_730_000_006_000,
            "T": 1_730_000_006_000,
            "o": {
                "s": "ETHUSDT",
                "aid": 5002,
                "caid": eth_client_id,
                "at": "CONDITIONAL",
                "o": "STOP_MARKET",
                "X": "REJECTED",
                "tp": "3100",
            },
        }
    )
    rej_res = coordinator.on_algo_update(
        rej_update,
        confirmed_position_qty=Decimal("3.0"),
        rejection_reason="Margin insufficient on trigger",
    )
    assert rej_res.entry_gate_open is False
    assert coordinator.entry_gate_open is False
    assert rej_res.assessment.gate_state is ProtectionGateState.REJECTED_ESCALATED
    assert rej_res.escalations[0].reason_code == "PROTECTION_REJECTED"


def test_algo_rest_adapter_reject_and_query_list() -> None:
    """Kiểm chứng REST adapter phân loại 4xx reject, query và list_open_algo_orders."""
    transport = _FakeAlgoTransport()
    adapter = _make_adapter(transport)

    # Lỗi 400 khi đặt lệnh
    transport.fail_next_place = BinanceHttpResponse(
        400, (), b'{"code": -2021, "msg": "Order would immediately trigger."}'
    )
    params = AlgoOrderParams(
        symbol="BTCUSDT",
        side="SELL",
        order_type=ConditionalAlgoOrderType.STOP_MARKET,
        client_algo_id="sm-err-1",
        trigger_price=Decimal("60000"),
        quantity=Decimal("0.2"),
        reduce_only=True,
    )
    failed = adapter.place_algo_order(params)
    assert isinstance(failed, AlgoSubmissionFailed)
    assert failed.error_code == -2021

    # Đặt thành công -> query -> list_open -> cancel
    ok = adapter.place_algo_order(params)
    assert isinstance(ok, AlgoOrderAccepted)
    found = adapter.query_algo_order(algo_id=ok.order.algo_id)
    assert isinstance(found, AlgoOrderFound)
    assert found.order.algo_id == ok.order.algo_id
    opened = adapter.list_open_algo_orders(symbol="BTCUSDT")
    assert isinstance(opened, tuple)
    assert len(opened) == 1
    canceled = adapter.cancel_algo_order(symbol="BTCUSDT", algo_id=ok.order.algo_id)
    assert isinstance(canceled, AlgoOrderCanceled)
