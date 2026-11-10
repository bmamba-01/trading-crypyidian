"""Kiểm tra hợp đồng order offline; fixture không xác nhận năng lực Demo."""

import json
from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.adapters.binance_usdm import order_schemas as schemas
from trading_bot.adapters.binance_usdm.submission_errors import (
    BinanceFailureKind,
    BinanceResponse,
    classify_submission_failure,
)
from trading_bot.domain.execution.venue_orders import SubmissionOutcome, VenueOrderState

FIXTURE = Path(__file__).parents[1] / "fixtures/binance_usdm/wp03_order_contracts.json"


def payload(name: str) -> dict[str, object]:
    return deepcopy(json.loads(FIXTURE.read_text())["cases"][name]["payload"])


@pytest.mark.parametrize("order_type", ["MARKET", "LIMIT"])
def test_regular_response_preserves_identity_type_and_optional_facts(order_type: str) -> None:
    value = payload("regular_ack")
    value["type"] = order_type
    result = schemas.parse_regular_order(value)
    assert result.order_id == 101
    assert result.client_order_id == "offline-entry"
    assert result.order_type.value == order_type
    assert result.executed_quantity is None
    assert result.update_time_ms is None
    assert result.canonical_state is VenueOrderState.ACKNOWLEDGED


@pytest.mark.parametrize(
    ("status", "quantity", "expected"),
    [
        ("NEW", "0", VenueOrderState.ACKNOWLEDGED),
        ("PARTIALLY_FILLED", "0.01", VenueOrderState.PARTIALLY_FILLED),
        ("FILLED", "0.02", VenueOrderState.FILLED),
        ("CANCELED", "0.01", VenueOrderState.CANCELED),
        ("EXPIRED", "0", VenueOrderState.EXPIRED),
        ("EXPIRED_IN_MATCH", "0", VenueOrderState.EXPIRED),
        ("SOMETHING_NEW", "0", VenueOrderState.UNKNOWN),
        ("REJECTED", "0", VenueOrderState.UNKNOWN),
        ("new", "0", VenueOrderState.UNKNOWN),
    ],
)
def test_regular_status_projection_keeps_unknown_raw_status(
    status: str, quantity: str, expected: VenueOrderState
) -> None:
    value = payload("regular_partial")
    value.update(status=status, executedQty=quantity)
    result = schemas.parse_regular_order(value)
    assert result.raw_status == status
    assert result.executed_quantity == Decimal(quantity)
    assert result.update_time_ms == 1750515742297
    assert result.canonical_state is expected


@pytest.mark.parametrize("order_type", list(schemas.ConditionalAlgoOrderType))
def test_conditional_types_cannot_enter_regular_parser_or_route(order_type: str) -> None:
    value = payload("regular_ack")
    value["type"] = order_type
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_regular_order(value)
    with pytest.raises(schemas.OrderSchemaError):
        schemas.regular_order_path(order_type)


@pytest.mark.parametrize("order_type", ["MARKET", "LIMIT"])
def test_regular_route_accepts_only_regular_types(order_type: str) -> None:
    assert schemas.regular_order_path(order_type) == "/fapi/v1/order"


@pytest.mark.parametrize("name", ["algo_new", "algo_triggered", "algo_finished", "algo_rejected"])
def test_algo_lifecycle_cannot_prove_regular_fill(name: str) -> None:
    result = schemas.parse_algo_order(payload(name))
    assert result.algo_id == 201
    assert result.client_algo_id == "offline-protection"
    assert result.order_type is schemas.ConditionalAlgoOrderType.STOP_MARKET
    assert result.trigger_price == Decimal("60000")
    assert result.canonical_state is VenueOrderState.UNKNOWN
    if name in {"algo_triggered", "algo_finished"}:
        assert result.actual_order_id == "301"
    else:
        assert result.actual_order_id is None


@pytest.mark.parametrize(
    "status",
    [
        "NEW",
        "TRIGGERING",
        "TRIGGERED",
        "FINISHED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "FUTURE_STATUS",
    ],
)
def test_algo_status_is_separate_and_unknown_is_preserved(status: str) -> None:
    value = payload("algo_new")
    value["algoStatus"] = status
    result = schemas.parse_algo_order(value)
    assert result.raw_status == status
    assert result.canonical_state is VenueOrderState.UNKNOWN
    assert result.status.value == (status if status != "FUTURE_STATUS" else "UNKNOWN")


def test_algo_optional_fields_do_not_invent_nullability_or_fill() -> None:
    result = schemas.parse_algo_order(payload("algo_new"))
    assert result.actual_type is None
    assert result.actual_quantity is None
    assert result.update_time_ms is None
    value = payload("algo_new")
    value["icebergQuantity"] = "null"
    assert schemas.parse_algo_order(value).algo_id == 201


def test_order_event_uses_documented_nested_fields() -> None:
    value = payload("order_event")
    value.update(i=999, status="FILLED", executedQty="999")
    result = schemas.parse_order_trade_update(value)
    assert result.order.order_id == 101
    assert result.order.canonical_state is VenueOrderState.PARTIALLY_FILLED
    assert result.order.executed_quantity == Decimal("0.01")
    assert result.trade_id == 401
    assert result.last_filled_quantity == Decimal("0.01")
    assert result.event_time_ms == 1750515742303
    assert result.transaction_time_ms == 1750515742297
    assert result.order.update_time_ms == 1750515742296


def test_algo_event_uses_ai_and_caid_without_regular_aliases() -> None:
    value = payload("algo_event")
    value.update(algoId=999, actualOrderId="999", algoStatus="FILLED")
    result = schemas.parse_algo_update(value)
    assert result.order.algo_id == 201
    assert result.order.actual_order_id == "301"
    assert result.order.client_algo_id == "offline-protection"
    assert result.order.raw_status == "FINISHED"
    assert result.order.actual_quantity == Decimal("0.01")
    assert result.order.canonical_state is VenueOrderState.UNKNOWN
    assert result.order.update_time_ms is None


@pytest.mark.parametrize(
    ("parser", "name", "other"),
    [
        (schemas.parse_regular_order, "regular_ack", "algoId"),
        (schemas.parse_regular_order, "regular_ack", "algoStatus"),
        (schemas.parse_algo_order, "algo_new", "orderId"),
        (schemas.parse_algo_order, "algo_new", "status"),
    ],
)
def test_family_discriminator_collision_is_rejected(
    parser: Callable[[object], object], name: str, other: str
) -> None:
    value = payload(name)
    value[other] = "unexpected"
    with pytest.raises(schemas.OrderSchemaError):
        parser(value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("orderId", True),
        ("orderId", "101"),
        ("orderId", 0),
        ("orderId", -1),
        ("status", None),
        ("status", 1),
        ("clientOrderId", ""),
        ("symbol", " "),
        ("executedQty", 0.01),
        ("executedQty", "NaN"),
        ("executedQty", "Infinity"),
        ("executedQty", "-1"),
        ("executedQty", None),
        ("updateTime", True),
        ("updateTime", None),
        ("updateTime", -1),
        ("type", "UNKNOWN"),
    ],
)
def test_malformed_regular_values_fail_closed(key: str, value: object) -> None:
    data = payload("regular_partial")
    data[key] = value
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_regular_order(data)


@pytest.mark.parametrize("key", ["symbol", "orderId", "clientOrderId", "status", "type"])
def test_missing_required_regular_field_is_not_defaulted(key: str) -> None:
    data = payload("regular_ack")
    del data[key]
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_regular_order(data)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("algoType", "OTHER"),
        ("orderType", "MARKET"),
        ("algoId", "201"),
        ("algoId", False),
        ("actualOrderId", 301),
        ("actualOrderId", None),
        ("actualOrderId", "not-an-id"),
        ("actualType", "STOP"),
        ("actualQty", None),
        ("triggerPrice", "NaN"),
        ("algoStatus", None),
    ],
)
def test_malformed_algo_values_fail_closed(key: str, value: object) -> None:
    data = payload("algo_new")
    data[key] = value
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_algo_order(data)


@pytest.mark.parametrize(
    ("parser", "name"),
    [(schemas.parse_order_trade_update, "order_event"), (schemas.parse_algo_update, "algo_event")],
)
@pytest.mark.parametrize(
    ("key", "value"), [("e", "ACCOUNT_UPDATE"), ("E", True), ("T", "1"), ("o", []), ("o", None)]
)
def test_event_invalid_envelope_fails_closed(
    parser: Callable[[object], object], name: str, key: str, value: object
) -> None:
    data = payload(name)
    data[key] = value
    with pytest.raises(schemas.OrderSchemaError):
        parser(data)


@pytest.mark.parametrize(
    ("parser", "name", "key"),
    [
        (schemas.parse_order_trade_update, "order_event", "i"),
        (schemas.parse_order_trade_update, "order_event", "X"),
        (schemas.parse_algo_update, "algo_event", "aid"),
        (schemas.parse_algo_update, "algo_event", "X"),
    ],
)
def test_missing_nested_field_never_falls_back_to_top_level(
    parser: Callable[[object], object], name: str, key: str
) -> None:
    data = payload(name)
    data[key] = data["o"].pop(key)
    with pytest.raises(schemas.OrderSchemaError):
        parser(data)


@pytest.mark.parametrize("value", [None, [], "[]", "{bad"])
def test_non_object_or_ambiguous_json_is_rejected(value: object) -> None:
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_regular_order(value)


@pytest.mark.parametrize(
    ("status", "qty"), [("FILLED", None), ("PARTIALLY_FILLED", "0"), ("NEW", "0.01")]
)
def test_incomplete_or_contradictory_fill_facts_cannot_project_success(
    status: str, qty: str | None
) -> None:
    data = payload("regular_ack")
    data["status"] = status
    if qty is not None:
        data["executedQty"] = qty
    assert schemas.parse_regular_order(data).canonical_state is VenueOrderState.UNKNOWN


def test_routing_mismatch_retains_unknown_outcome_without_retry() -> None:
    result = classify_submission_failure(BinanceResponse(400, -4120, "STOP_ORDER_SWITCH_ALGO"))
    assert result.kind is BinanceFailureKind.ROUTING_MISMATCH
    assert result.outcome is SubmissionOutcome.UNKNOWN
    assert result.retry_allowed is False


def test_fixture_metadata_identifies_source_date_surface_and_synthetic_data() -> None:
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["metadata"]["accessed_on"] == "2026-11-02"
    assert fixture["metadata"]["synthetic"] is True
    for case in fixture["cases"].values():
        assert case["surface"]
        assert case["sources"]


@pytest.mark.parametrize(("status", "quantity"), [("FILLED", "0.01"), ("PARTIALLY_FILLED", "0.02")])
def test_contradictory_original_quantity_keeps_unknown(status: str, quantity: str) -> None:
    value = payload("regular_partial")
    value.update(status=status, executedQty=quantity)
    assert schemas.parse_regular_order(value).canonical_state is VenueOrderState.UNKNOWN


@pytest.mark.parametrize("status", ["CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"])
def test_terminal_status_without_execution_facts_cannot_resolve_risk(status: str) -> None:
    value = payload("regular_ack")
    value["status"] = status
    assert schemas.parse_regular_order(value).canonical_state is VenueOrderState.UNKNOWN


def test_unknown_execution_type_has_no_successful_event_projection() -> None:
    value = payload("order_event")
    value["o"]["x"] = "FUTURE_EXECUTION"
    result = schemas.parse_order_trade_update(value)
    assert result.execution_type == "FUTURE_EXECUTION"
    assert result.canonical_state is VenueOrderState.UNKNOWN


def test_valid_json_text_and_bytes_parse_identically() -> None:
    value = payload("regular_partial")
    assert schemas.parse_regular_order(json.dumps(value).encode()) == schemas.parse_regular_order(
        value
    )


@pytest.mark.parametrize("extra", ['"orderId":102', '"ignored":NaN', '"ignored":Infinity'])
def test_ambiguous_json_cannot_override_valid_order_facts(extra: str) -> None:
    valid = json.dumps(payload("regular_ack"))
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_regular_order(valid[:-1] + "," + extra + "}")


@pytest.mark.parametrize(
    "key",
    ["symbol", "algoId", "clientAlgoId", "algoStatus", "algoType", "orderType", "triggerPrice"],
)
def test_missing_required_algo_fact_is_not_defaulted(key: str) -> None:
    value = payload("algo_new")
    del value[key]
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_algo_order(value)


@pytest.mark.parametrize("value", ["9" * 5000, "0", "-1", "1.0", "9223372036854775808"])
def test_linked_order_id_must_fit_positive_int64_without_coercion(value: str) -> None:
    data = payload("algo_new")
    data["actualOrderId"] = value
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_algo_order(data)


@pytest.mark.parametrize(
    ("parser", "name", "key", "value"),
    [
        (schemas.parse_order_trade_update, "order_event", "z", 0.01),
        (schemas.parse_order_trade_update, "order_event", "t", True),
        (schemas.parse_order_trade_update, "order_event", "T", -1),
        (schemas.parse_order_trade_update, "order_event", "l", "NaN"),
        (schemas.parse_algo_update, "algo_event", "aid", True),
        (schemas.parse_algo_update, "algo_event", "aq", 0.01),
        (schemas.parse_algo_update, "algo_event", "ai", 301),
        (schemas.parse_algo_update, "algo_event", "tp", None),
        (schemas.parse_algo_update, "algo_event", "act", "0"),
    ],
)
def test_malformed_nested_facts_fail_closed(
    parser: Callable[[object], object], name: str, key: str, value: object
) -> None:
    data = payload(name)
    data["o"][key] = value
    with pytest.raises(schemas.OrderSchemaError):
        parser(data)


@pytest.mark.parametrize(
    ("status", "original", "cumulative", "last"),
    [
        ("FILLED", "0.02", "0.02", "3"),
        ("PARTIALLY_FILLED", "0.02", "0.01", "0.015"),
        ("NEW", "0.02", "0", "0.01"),
        ("FILLED", "0.02", "0.03", "0.01"),
        ("PARTIALLY_FILLED", "0.02", "0.01", "-0.001"),
    ],
)
def test_order_event_rejects_contradictory_fill_quantity_chain(
    status: str, original: str, cumulative: str, last: str
) -> None:
    data = payload("order_event")
    data["o"].update(
        x="NEW" if status == "NEW" else "TRADE", X=status, q=original, z=cumulative, l=last
    )
    with pytest.raises(schemas.OrderSchemaError):
        schemas.parse_order_trade_update(data)


@pytest.mark.parametrize(
    ("status", "cumulative", "last", "expected"),
    [
        ("FILLED", "0.02", "0.02", VenueOrderState.FILLED),
        ("FILLED", "0.02", "0.01", VenueOrderState.FILLED),
        ("PARTIALLY_FILLED", "0.01", "0.01", VenueOrderState.PARTIALLY_FILLED),
        ("NEW", "0", "0", VenueOrderState.ACKNOWLEDGED),
    ],
)
def test_order_event_accepts_consistent_fill_quantity_boundaries(
    status: str, cumulative: str, last: str, expected: VenueOrderState
) -> None:
    data = payload("order_event")
    data["o"].update(
        x="NEW" if status == "NEW" else "TRADE", X=status, q="0.02", z=cumulative, l=last
    )
    result = schemas.parse_order_trade_update(data)
    assert result.canonical_state is expected
    assert result.last_filled_quantity == Decimal(last)
