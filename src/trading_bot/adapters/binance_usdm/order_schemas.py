"""Parser thuần cho hợp đồng regular/Algo USDⓈ-M đã kiểm tra ngày 2026-11-02.

Không gọi API và không suy ra quyền giải phóng reservation từ DTO. Trường tùy chọn
thiếu vẫn là None; payload sai phải được caller xử lý như kết quả UNKNOWN.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from trading_bot.domain.execution.venue_orders import VenueOrderState


class OrderSchemaError(ValueError):
    """Payload không đủ tin cậy; exception không giữ dữ liệu response thô."""


class RegularOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ConditionalAlgoOrderType(StrEnum):
    STOP = "STOP"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"


class RegularOrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"
    UNKNOWN = "UNKNOWN"


class AlgoOrderStatus(StrEnum):
    NEW = "NEW"
    TRIGGERING = "TRIGGERING"
    TRIGGERED = "TRIGGERED"
    FINISHED = "FINISHED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RegularOrder:
    """Sự thật về order thường; status raw tách khỏi trạng thái domain."""

    symbol: str
    order_id: int
    client_order_id: str
    order_type: RegularOrderType
    status: RegularOrderStatus
    raw_status: str
    executed_quantity: Decimal | None
    original_quantity: Decimal | None
    update_time_ms: int | None

    @property
    def canonical_state(self) -> VenueOrderState:
        if self.status is RegularOrderStatus.NEW:
            return (
                VenueOrderState.ACKNOWLEDGED
                if self.executed_quantity in {None, Decimal(0)}
                else VenueOrderState.UNKNOWN
            )
        if self.status in {RegularOrderStatus.FILLED, RegularOrderStatus.PARTIALLY_FILLED}:
            if self.executed_quantity is None or self.executed_quantity <= 0:
                return VenueOrderState.UNKNOWN
            if self.original_quantity is not None:
                if self.status is RegularOrderStatus.FILLED:
                    if self.executed_quantity != self.original_quantity:
                        return VenueOrderState.UNKNOWN
                elif self.executed_quantity >= self.original_quantity:
                    return VenueOrderState.UNKNOWN
        if (
            self.status
            in {
                RegularOrderStatus.CANCELED,
                RegularOrderStatus.EXPIRED,
                RegularOrderStatus.EXPIRED_IN_MATCH,
            }
            and self.executed_quantity is None
        ):
            return VenueOrderState.UNKNOWN
        return {
            RegularOrderStatus.PARTIALLY_FILLED: VenueOrderState.PARTIALLY_FILLED,
            RegularOrderStatus.FILLED: VenueOrderState.FILLED,
            RegularOrderStatus.CANCELED: VenueOrderState.CANCELED,
            RegularOrderStatus.EXPIRED: VenueOrderState.EXPIRED,
            RegularOrderStatus.EXPIRED_IN_MATCH: VenueOrderState.EXPIRED,
        }.get(self.status, VenueOrderState.UNKNOWN)


@dataclass(frozen=True, slots=True)
class AlgoOrder:
    """Vòng đời Algo không phải bằng chứng vòng đời hoặc fill order thường."""

    symbol: str
    algo_id: int
    client_algo_id: str
    algo_type: str
    order_type: ConditionalAlgoOrderType
    status: AlgoOrderStatus
    raw_status: str
    trigger_price: Decimal
    actual_order_id: str | None
    actual_type: RegularOrderType | None
    actual_quantity: Decimal | None
    update_time_ms: int | None

    @property
    def canonical_state(self) -> VenueOrderState:
        # FINISHED bao gồm cả fill và cancel; phải query order thường để phân giải.
        return VenueOrderState.UNKNOWN


@dataclass(frozen=True, slots=True)
class OrderTradeUpdate:
    """ORDER_TRADE_UPDATE với thời gian envelope và trade riêng biệt."""

    event_time_ms: int
    transaction_time_ms: int
    order: RegularOrder
    execution_type: str
    trade_id: int
    last_filled_quantity: Decimal
    last_filled_price: Decimal

    @property
    def canonical_state(self) -> VenueOrderState:
        if self.execution_type not in {
            "NEW",
            "CANCELED",
            "CALCULATED",
            "EXPIRED",
            "TRADE",
            "AMENDMENT",
        }:
            return VenueOrderState.UNKNOWN
        return self.order.canonical_state


@dataclass(frozen=True, slots=True)
class AlgoUpdate:
    """ALGO_UPDATE, không nhận alias REST hoặc ORDER_TRADE_UPDATE."""

    event_time_ms: int
    transaction_time_ms: int
    order: AlgoOrder


def regular_order_path(order_type: object) -> str:
    """Chặn conditional và enum lạ trước khi serialize order thường."""
    _enum(RegularOrderType, order_type)
    return "/fapi/v1/order"


def validate_regular_order_family(data: dict[str, object]) -> None:
    """Chặn payload Algo/conditional; ACK tối thiểu có thể chưa mang type."""
    _exclude(data, {"algoId", "clientAlgoId", "algoStatus", "algoType", "orderType"})
    for key in ("type", "origType"):
        if key in data:
            regular_order_path(data[key])


def parse_regular_order(payload: object) -> RegularOrder:
    """Đọc projection REST; trường chưa có không được coi là số không."""
    data = decode_order_object(payload)
    validate_regular_order_family(data)
    raw_status = _text(data, "status")
    order_type = _enum(RegularOrderType, data.get("type"))
    executed = _optional_decimal(data, "executedQty")
    original = _optional_decimal(data, "origQty")
    if original is not None and executed is not None and executed > original:
        raise OrderSchemaError("Số đã khớp vượt số lượng gốc.")
    return RegularOrder(
        symbol=_text(data, "symbol"),
        order_id=_integer(data, "orderId", positive=True),
        client_order_id=_text(data, "clientOrderId"),
        order_type=order_type,
        status=_status(RegularOrderStatus, raw_status),
        raw_status=raw_status,
        executed_quantity=executed,
        original_quantity=original,
        update_time_ms=_optional_integer(data, "updateTime"),
    )


def parse_algo_order(payload: object) -> AlgoOrder:
    """Đọc REST Algo, không dùng orderId/status của order thường."""
    data = decode_order_object(payload)
    _exclude(data, {"orderId", "clientOrderId", "status", "executedQty", "type"})
    if _text(data, "algoType") != "CONDITIONAL":
        raise OrderSchemaError("Chỉ hỗ trợ Algo CONDITIONAL.")
    raw_status = _text(data, "algoStatus")
    return AlgoOrder(
        symbol=_text(data, "symbol"),
        algo_id=_integer(data, "algoId", positive=True),
        client_algo_id=_text(data, "clientAlgoId"),
        algo_type="CONDITIONAL",
        order_type=_enum(ConditionalAlgoOrderType, data.get("orderType")),
        status=_status(AlgoOrderStatus, raw_status),
        raw_status=raw_status,
        trigger_price=_decimal(data, "triggerPrice"),
        actual_order_id=_linked_order_id(data, "actualOrderId"),
        actual_type=(_enum(RegularOrderType, data["actualType"]) if "actualType" in data else None),
        actual_quantity=_optional_decimal(data, "actualQty"),
        update_time_ms=_optional_integer(data, "updateTime"),
    )


def parse_order_trade_update(payload: object) -> OrderTradeUpdate:
    """Lấy sự thật từ o.i/c/s/o/X/z; không fallback field top-level."""
    data, order = _event(payload, "ORDER_TRADE_UPDATE")
    translated = {
        target: order[source]
        for source, target in {
            "s": "symbol",
            "i": "orderId",
            "c": "clientOrderId",
            "o": "type",
            "X": "status",
            "z": "executedQty",
            "T": "updateTime",
            "q": "origQty",
            "ot": "origType",
        }.items()
        if source in order
    }
    for key in ("z", "T", "q"):
        if key not in order:
            raise OrderSchemaError("Thiếu sự thật bắt buộc trong event order.")
    regular_order = parse_regular_order(translated)
    last_quantity = _decimal(order, "l")
    if regular_order.executed_quantity is None or last_quantity > regular_order.executed_quantity:
        raise OrderSchemaError("Lượng khớp cuối không được vượt lượng khớp tích lũy.")
    return OrderTradeUpdate(
        event_time_ms=_integer(data, "E"),
        transaction_time_ms=_integer(data, "T"),
        order=regular_order,
        execution_type=_text(order, "x"),
        trade_id=_integer(order, "t"),
        last_filled_quantity=last_quantity,
        last_filled_price=_decimal(order, "L"),
    )


def parse_algo_update(payload: object) -> AlgoUpdate:
    """Đọc đúng o.aid/caid/at/X/ai/tp theo schema algoUpdate."""
    data, order = _event(payload, "ALGO_UPDATE")
    translated = {
        target: order[source]
        for source, target in {
            "s": "symbol",
            "aid": "algoId",
            "caid": "clientAlgoId",
            "at": "algoType",
            "o": "orderType",
            "X": "algoStatus",
            "tp": "triggerPrice",
            "ai": "actualOrderId",
            "act": "actualType",
            "aq": "actualQty",
        }.items()
        if source in order
    }
    return AlgoUpdate(
        event_time_ms=_integer(data, "E"),
        transaction_time_ms=_integer(data, "T"),
        order=parse_algo_order(translated),
    )


_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_ID = re.compile(r"[1-9][0-9]*\Z", re.ASCII)


def _enum[EnumT: StrEnum](enum_type: type[EnumT], value: object) -> EnumT:
    if not isinstance(value, str):
        raise OrderSchemaError("Enum phải là chuỗi đúng casing.")
    try:
        return enum_type(value)
    except ValueError:
        raise OrderSchemaError("Giá trị enum không thuộc hợp đồng đã xác minh.") from None


def _status[EnumT: StrEnum](enum_type: type[EnumT], value: str) -> EnumT:
    try:
        return enum_type(value)
    except ValueError:
        return enum_type("UNKNOWN")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OrderSchemaError("JSON có key trùng nên không xác định được sự thật.")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise OrderSchemaError("JSON có số không hữu hạn.")


def decode_order_object(payload: object) -> dict[str, object]:
    """Đọc JSON object duy nhất, từ chối key trùng và số không hữu hạn."""
    if isinstance(payload, str | bytes):
        try:
            payload = json.loads(payload, object_pairs_hook=_pairs, parse_constant=_constant)
        except UnicodeError, ValueError, RecursionError:
            raise OrderSchemaError("JSON không hợp lệ hoặc mơ hồ.") from None
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise OrderSchemaError("Payload phải là JSON object.")
    return payload


def _text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise OrderSchemaError("Thiếu trường chuỗi hợp lệ.")
    return value


def _integer(data: dict[str, object], key: str, *, positive: bool = False) -> int:
    value = data.get(key)
    if type(value) is not int or not (int(positive) <= value <= 2**63 - 1):
        raise OrderSchemaError("Trường số nguyên phải đúng kiểu và nằm trong miền int64.")
    return value


def _optional_integer(data: dict[str, object], key: str) -> int | None:
    return _integer(data, key) if key in data else None


def _decimal(data: dict[str, object], key: str) -> Decimal:
    value = _text(data, key)
    if not _DECIMAL.fullmatch(value):
        raise OrderSchemaError("Số thập phân phải là chuỗi không âm hợp lệ.")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise OrderSchemaError("Số thập phân không hợp lệ.") from None
    if not result.is_finite():
        raise OrderSchemaError("Số thập phân phải hữu hạn.")
    return result


def _optional_decimal(data: dict[str, object], key: str) -> Decimal | None:
    return _decimal(data, key) if key in data else None


def _linked_order_id(data: dict[str, object], key: str) -> str | None:
    if key not in data or data[key] == "":
        return None
    value = _text(data, key)
    if len(value) > 19 or not _ID.fullmatch(value) or int(value) > 2**63 - 1:
        raise OrderSchemaError("ID order liên kết phải là chuỗi số nguyên dương.")
    return value


def _exclude(data: dict[str, object], keys: set[str]) -> None:
    if keys.intersection(data):
        raise OrderSchemaError("Payload trộn hai họ order khác nhau.")


def _event(payload: object, event_type: str) -> tuple[dict[str, object], dict[str, object]]:
    data = decode_order_object(payload)
    if data.get("e") != event_type or not isinstance(data.get("o"), dict):
        raise OrderSchemaError("Sai event type hoặc thiếu object lồng nhau.")
    return data, decode_order_object(data["o"])
