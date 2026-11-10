"""Unit tests cho venue response/event parsers của Binance USDⓈ-M (TDD RED -> GREEN)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.domain.execution.venue_parsers import (
    AlgoOrderResponse,
    AlgoOrderStatus,
    AlgoUpdateEvent,
    ConditionalAlgoOrderType,
    ExecutionType,
    OrderSide,
    OrderTradeUpdateEvent,
    RegularOrderResponse,
    RegularOrderStatus,
    RegularOrderType,
    VenueParserError,
    parse_account_update,
    parse_algo_order_response,
    parse_algo_update,
    parse_order_trade_update,
    parse_regular_order_response,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "binance_usdm"


def _load_fixture(filename: str) -> dict[str, object]:
    path = FIXTURES_DIR / filename
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)
    return data


# ============================================================================
# 1. Regular Order Response Parser Tests
# ============================================================================


def test_parse_regular_order_accepted_fixture() -> None:
    payload = _load_fixture("regular_order_accepted.json")
    result = parse_regular_order_response(payload)

    assert isinstance(result, RegularOrderResponse)
    assert result.symbol == "BTCUSDT"
    assert result.order_id == 10000001
    assert result.client_order_id == "crypidian_test_001"
    assert result.status == RegularOrderStatus.NEW
    assert result.order_type == RegularOrderType.LIMIT
    assert result.side == OrderSide.BUY
    assert result.executed_qty == Decimal("0")
    assert result.cum_quote == Decimal("0")
    assert result.avg_price == Decimal("0.00000")
    assert result.orig_qty == Decimal("0.050")
    assert result.price == Decimal("50000.00")
    assert result.update_time == datetime.fromtimestamp(1798214400.0, tz=UTC)
    assert result.reduce_only is False
    assert result.close_position is False


def test_parse_regular_order_partial_fixture() -> None:
    payload = _load_fixture("regular_order_partial.json")
    result = parse_regular_order_response(payload)

    assert isinstance(result, RegularOrderResponse)
    assert result.status == RegularOrderStatus.PARTIALLY_FILLED
    assert result.executed_qty == Decimal("0.020")
    assert result.cum_quote == Decimal("1000.00")
    assert result.avg_price == Decimal("50000.00")


def test_parse_regular_order_with_json_string() -> None:
    payload = _load_fixture("regular_order_accepted.json")
    json_str = json.dumps(payload)
    result = parse_regular_order_response(json_str)
    assert result.order_id == 10000001


def test_parse_regular_order_rejects_unknown_status() -> None:
    payload = _load_fixture("regular_order_unknown_status.json")
    with pytest.raises(VenueParserError, match="status không hợp lệ"):
        parse_regular_order_response(payload)


def test_parse_regular_order_rejects_missing_order_id() -> None:
    payload = _load_fixture("regular_order_accepted.json")
    del payload["orderId"]
    with pytest.raises(VenueParserError, match="orderId bắt buộc"):
        parse_regular_order_response(payload)


def test_parse_regular_order_rejects_missing_client_order_id() -> None:
    payload = _load_fixture("regular_order_accepted.json")
    payload["clientOrderId"] = ""
    with pytest.raises(VenueParserError, match="clientOrderId không được để trống"):
        parse_regular_order_response(payload)


def test_parse_regular_order_rejects_negative_executed_qty() -> None:
    payload = _load_fixture("regular_order_accepted.json")
    payload["executedQty"] = "-0.01"
    with pytest.raises(VenueParserError, match="executedQty không được âm"):
        parse_regular_order_response(payload)


def test_parse_regular_order_rejects_non_finite_decimal() -> None:
    payload = _load_fixture("regular_order_accepted.json")
    payload["executedQty"] = "NaN"
    with pytest.raises(VenueParserError, match="không phải số hữu hạn"):
        parse_regular_order_response(payload)

    payload["executedQty"] = "Infinity"
    with pytest.raises(VenueParserError, match="không phải số hữu hạn"):
        parse_regular_order_response(payload)


def test_parse_regular_order_rejects_executed_qty_exceeding_expected_or_orig() -> None:
    payload = _load_fixture("regular_order_accepted.json")
    payload["origQty"] = "0.050"
    payload["executedQty"] = "0.060"
    with pytest.raises(VenueParserError, match="vượt quá origQty"):
        parse_regular_order_response(payload)

    # Test expected_qty
    payload["executedQty"] = "0.050"
    with pytest.raises(VenueParserError, match="vượt quá expected_qty"):
        parse_regular_order_response(payload, expected_qty=Decimal("0.040"))


def test_parse_regular_order_rejects_algo_payload() -> None:
    algo_payload = _load_fixture("algo_order_new.json")
    with pytest.raises(VenueParserError, match="Payload thuộc algo order"):
        parse_regular_order_response(algo_payload)


def test_parse_regular_order_rejects_non_object() -> None:
    with pytest.raises(VenueParserError, match="Payload phải là JSON object"):
        parse_regular_order_response("[]")

    with pytest.raises(VenueParserError, match="Payload phải là JSON object"):
        parse_regular_order_response("malformed-json-text")


# ============================================================================
# 2. Algo Order Response Parser Tests
# ============================================================================


def test_parse_algo_order_new_fixture() -> None:
    payload = _load_fixture("algo_order_new.json")
    result = parse_algo_order_response(payload)

    assert isinstance(result, AlgoOrderResponse)
    assert result.algo_id == 20000001
    assert result.client_algo_id == "crypidian_algo_001"
    assert result.algo_type == ConditionalAlgoOrderType.STOP_MARKET
    assert result.algo_status == AlgoOrderStatus.NEW
    assert result.symbol == "BTCUSDT"
    assert result.side == OrderSide.SELL
    assert result.trigger_price == Decimal("48000.00")
    assert result.quantity == Decimal("0.050")
    assert result.actual_order_id is None
    assert result.actual_price is None


def test_parse_algo_order_triggered_fixture() -> None:
    payload = _load_fixture("algo_order_triggered.json")
    result = parse_algo_order_response(payload)

    assert isinstance(result, AlgoOrderResponse)
    assert result.algo_id == 20000001
    assert result.algo_status == AlgoOrderStatus.TRIGGERED
    assert result.actual_order_id == 10000005
    assert result.actual_price == Decimal("47990.00")


def test_parse_algo_order_rejects_unknown_algo_type() -> None:
    payload = _load_fixture("algo_order_new.json")
    payload["algoType"] = "MAGIC_STOP"
    with pytest.raises(VenueParserError, match="algoType không hợp lệ"):
        parse_algo_order_response(payload)


def test_parse_algo_order_rejects_unknown_algo_status() -> None:
    payload = _load_fixture("algo_order_new.json")
    payload["algoStatus"] = "MAGIC_STATUS"
    with pytest.raises(VenueParserError, match="algoStatus không hợp lệ"):
        parse_algo_order_response(payload)


def test_parse_algo_order_rejects_missing_algo_id() -> None:
    payload = _load_fixture("algo_order_new.json")
    del payload["algoId"]
    with pytest.raises(VenueParserError, match="algoId bắt buộc"):
        parse_algo_order_response(payload)


def test_parse_algo_order_rejects_negative_trigger_price() -> None:
    payload = _load_fixture("algo_order_new.json")
    payload["triggerPrice"] = "-100"
    with pytest.raises(VenueParserError, match="triggerPrice không được âm"):
        parse_algo_order_response(payload)


def test_parse_algo_order_rejects_regular_order_payload() -> None:
    regular_payload = _load_fixture("regular_order_accepted.json")
    with pytest.raises(VenueParserError, match="Payload thuộc regular order"):
        parse_algo_order_response(regular_payload)


# ============================================================================
# 3. User Data Stream Event Parsers Tests
# ============================================================================


def test_parse_order_trade_update_fixture() -> None:
    payload = _load_fixture("order_trade_update_partial.json")
    event = parse_order_trade_update(payload)

    assert isinstance(event, OrderTradeUpdateEvent)
    assert event.symbol == "BTCUSDT"
    assert event.client_order_id == "crypidian_test_001"
    assert event.order_id == 10000001
    assert event.side == OrderSide.BUY
    assert event.order_type == RegularOrderType.LIMIT
    assert event.execution_type == ExecutionType.TRADE
    assert event.order_status == RegularOrderStatus.PARTIALLY_FILLED
    assert event.last_filled_qty == Decimal("0.020")
    assert event.cum_filled_qty == Decimal("0.020")
    assert event.last_filled_price == Decimal("50000.00")
    assert event.orig_qty == Decimal("0.050")
    assert event.avg_price == Decimal("50000.00")
    assert event.trade_id == 99901
    assert event.event_time == datetime.fromtimestamp(1798214405.1, tz=UTC)


def test_parse_order_trade_update_rejects_other_events() -> None:
    algo_payload = _load_fixture("algo_update_rejected.json")
    with pytest.raises(VenueParserError, match="Không phải ORDER_TRADE_UPDATE event"):
        parse_order_trade_update(algo_payload)

    account_payload = _load_fixture("account_update.json")
    with pytest.raises(VenueParserError, match="Không phải ORDER_TRADE_UPDATE event"):
        parse_order_trade_update(account_payload)


def test_parse_order_trade_update_rejects_negative_fill_quantity() -> None:
    payload = _load_fixture("order_trade_update_partial.json")
    assert isinstance(payload["o"], dict)
    payload["o"]["l"] = "-0.01"
    with pytest.raises(VenueParserError, match="last_filled_qty không được âm"):
        parse_order_trade_update(payload)


def test_parse_algo_update_fixture() -> None:
    payload = _load_fixture("algo_update_rejected.json")
    event = parse_algo_update(payload)

    assert isinstance(event, AlgoUpdateEvent)
    assert event.algo_id == 20000001
    assert event.client_algo_id == "crypidian_algo_001"
    assert event.algo_type == ConditionalAlgoOrderType.STOP_MARKET
    assert event.algo_status == AlgoOrderStatus.REJECTED
    assert event.symbol == "BTCUSDT"
    assert event.side == OrderSide.SELL
    assert event.trigger_price == Decimal("48000.00")
    assert event.actual_order_id is None
    assert event.rejection_reason == "INSUFFICIENT_MARGIN"


def test_parse_algo_update_rejects_other_events() -> None:
    order_trade_payload = _load_fixture("order_trade_update_partial.json")
    with pytest.raises(VenueParserError, match="Không phải ALGO_UPDATE event"):
        parse_algo_update(order_trade_payload)


def test_parse_account_update_fixture() -> None:
    payload = _load_fixture("account_update.json")
    event = parse_account_update(payload)

    assert event.event_reason == "ORDER"
    assert len(event.balances) == 1
    assert event.balances[0].asset == "USDT"
    assert event.balances[0].wallet_balance == Decimal("10000.50000000")
    assert event.balances[0].cross_wallet_balance == Decimal("9500.50000000")

    assert len(event.positions) == 1
    pos = event.positions[0]
    assert pos.symbol == "BTCUSDT"
    assert pos.position_amount == Decimal("0.020")
    assert pos.entry_price == Decimal("50000.00000")
    assert pos.unrealized_profit == Decimal("-15.20000000")
    assert pos.position_side == "BOTH"


def test_parse_account_update_allows_negative_position_amount_and_unrealized_profit() -> None:
    payload = _load_fixture("account_update.json")
    assert isinstance(payload["a"], dict)
    assert isinstance(payload["a"]["P"], list)
    payload["a"]["P"][0]["pa"] = "-0.500"
    payload["a"]["P"][0]["up"] = "-123.456"
    event = parse_account_update(payload)
    assert event.positions[0].position_amount == Decimal("-0.500")
    assert event.positions[0].unrealized_profit == Decimal("-123.456")


def test_parse_account_update_rejects_negative_wallet_balance() -> None:
    payload = _load_fixture("account_update.json")
    assert isinstance(payload["a"], dict)
    assert isinstance(payload["a"]["B"], list)
    payload["a"]["B"][0]["wb"] = "-10.0"
    with pytest.raises(VenueParserError, match="wallet_balance không được âm"):
        parse_account_update(payload)


def test_parse_account_update_rejects_negative_entry_price() -> None:
    payload = _load_fixture("account_update.json")
    assert isinstance(payload["a"], dict)
    assert isinstance(payload["a"]["P"], list)
    payload["a"]["P"][0]["ep"] = "-50000.0"
    with pytest.raises(VenueParserError, match="entry_price không được âm"):
        parse_account_update(payload)


def test_parse_account_update_rejects_other_events() -> None:
    order_trade_payload = _load_fixture("order_trade_update_partial.json")
    with pytest.raises(VenueParserError, match="Không phải ACCOUNT_UPDATE event"):
        parse_account_update(order_trade_payload)
