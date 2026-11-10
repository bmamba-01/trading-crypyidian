"""Bộ parser và DTOs offline cho các API/event families của Binance Futures USDⓈ-M.

Cung cấp các hàm phân tích cú pháp nghiêm ngặt (fail-closed), bảo đảm dữ liệu
chuẩn hóa trước khi đưa vào domain/application logic:
1. Regular Order Response (POST /fapi/v1/order, GET /fapi/v1/order)
2. Algo Order Response (POST /fapi/v1/algoOrder, GET /fapi/v1/algoOrder)
3. User Data Stream Events (ORDER_TRADE_UPDATE, ALGO_UPDATE, ACCOUNT_UPDATE)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class VenueParserError(ValueError):
    """Lỗi phân tích cú pháp dữ liệu sàn; không mang thông tin nhạy cảm."""


class RegularOrderStatus(StrEnum):
    """Trạng thái của lệnh thường trên Binance Futures USDⓈ-M."""

    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"
    REJECTED = "REJECTED"


class RegularOrderType(StrEnum):
    """Loại lệnh thường trên Binance Futures USDⓈ-M."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP = "STOP"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"


class ConditionalAlgoOrderType(StrEnum):
    """Loại lệnh điều kiện Algo Service trên Binance Futures USDⓈ-M."""

    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    STOP = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"


class AlgoOrderStatus(StrEnum):
    """Trạng thái của lệnh algo trên Binance Futures USDⓈ-M."""

    NEW = "NEW"
    TRIGGERING = "TRIGGERING"
    TRIGGERED = "TRIGGERED"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OrderSide(StrEnum):
    """Chiều mua/bán của lệnh."""

    BUY = "BUY"
    SELL = "SELL"


class ExecutionType(StrEnum):
    """Loại thực thi trong báo cáo ORDER_TRADE_UPDATE."""

    NEW = "NEW"
    CANCELED = "CANCELED"
    CALCULATED = "CALCULATED"
    EXPIRED = "EXPIRED"
    TRADE = "TRADE"
    AMENDMENT = "AMENDMENT"


# ============================================================================
# DTOs
# ============================================================================


@dataclass(frozen=True, slots=True)
class RegularOrderResponse:
    """Kết quả chuẩn hóa từ REST API regular order (`/fapi/v1/order`)."""

    symbol: str
    order_id: int
    client_order_id: str
    status: RegularOrderStatus
    order_type: RegularOrderType
    side: OrderSide
    executed_qty: Decimal
    cum_quote: Decimal
    avg_price: Decimal
    orig_qty: Decimal
    price: Decimal
    update_time: datetime
    reduce_only: bool = False
    close_position: bool = False


@dataclass(frozen=True, slots=True)
class AlgoOrderResponse:
    """Kết quả chuẩn hóa từ REST API algo conditional order (`/fapi/v1/algoOrder`)."""

    algo_id: int
    client_algo_id: str
    algo_type: ConditionalAlgoOrderType
    algo_status: AlgoOrderStatus
    symbol: str
    side: OrderSide
    trigger_price: Decimal
    price: Decimal | None = None
    quantity: Decimal | None = None
    actual_order_id: int | None = None
    actual_price: Decimal | None = None
    update_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderTradeUpdateEvent:
    """Sự kiện WebSocket `ORDER_TRADE_UPDATE` đã chuẩn hóa."""

    event_time: datetime
    transaction_time: datetime
    symbol: str
    client_order_id: str
    order_id: int
    side: OrderSide
    order_type: RegularOrderType
    execution_type: ExecutionType
    order_status: RegularOrderStatus
    last_filled_qty: Decimal
    cum_filled_qty: Decimal
    last_filled_price: Decimal
    orig_qty: Decimal
    avg_price: Decimal
    trade_id: int
    order_trade_time: datetime


@dataclass(frozen=True, slots=True)
class AlgoUpdateEvent:
    """Sự kiện WebSocket `ALGO_UPDATE` đã chuẩn hóa."""

    event_time: datetime
    transaction_time: datetime
    algo_id: int
    client_algo_id: str
    algo_type: ConditionalAlgoOrderType
    algo_status: AlgoOrderStatus
    symbol: str
    side: OrderSide
    trigger_price: Decimal
    actual_order_id: int | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AccountBalanceUpdate:
    """Số dư tài sản trong sự kiện `ACCOUNT_UPDATE`."""

    asset: str
    wallet_balance: Decimal
    cross_wallet_balance: Decimal


@dataclass(frozen=True, slots=True)
class PositionUpdate:
    """Thông tin vị thế trong sự kiện `ACCOUNT_UPDATE`."""

    symbol: str
    position_amount: Decimal
    entry_price: Decimal
    unrealized_profit: Decimal
    margin_type: str = "cross"
    isolated_wallet: Decimal = Decimal("0")
    position_side: str = "BOTH"


@dataclass(frozen=True, slots=True)
class AccountUpdateEvent:
    """Sự kiện WebSocket `ACCOUNT_UPDATE` đã chuẩn hóa."""

    event_time: datetime
    transaction_time: datetime
    event_reason: str
    balances: tuple[AccountBalanceUpdate, ...]
    positions: tuple[PositionUpdate, ...]


# ============================================================================
# Helpers
# ============================================================================


def _ensure_dict(payload: dict[str, Any] | str) -> dict[str, Any]:
    """Chuyển chuỗi JSON hoặc đối tượng thành dict và lọc bỏ metadata nội bộ."""
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except Exception as exc:
            raise VenueParserError("Payload phải là JSON object hợp lệ.") from exc
    elif isinstance(payload, dict):
        parsed = payload
    else:
        raise VenueParserError("Payload phải là JSON object (dict).")

    if not isinstance(parsed, dict):
        raise VenueParserError("Payload phải là JSON object (dict).")

    # Loại bỏ các trường metadata bắt đầu bằng dấu gạch dưới (ví dụ: _fixture_metadata)
    return {k: v for k, v in parsed.items() if not k.startswith("_")}


def _to_decimal(
    val: Any,
    field_name: str,
    *,
    allow_negative: bool = False,
    allow_none: bool = False,
) -> Decimal:
    """Chuyển đổi và kiểm tra giá trị Decimal hữu hạn, không âm (nếu yêu cầu)."""
    if val is None:
        if allow_none:
            return Decimal("0")
        raise VenueParserError(f"Trường {field_name} không được là None.")

    if isinstance(val, bool):
        raise VenueParserError(f"Trường {field_name} không được là kiểu bool.")

    try:
        dec = Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise VenueParserError(
            f"Trường {field_name} có giá trị Decimal không hợp lệ: {val!r}"
        ) from exc

    if not dec.is_finite():
        raise VenueParserError(f"Trường {field_name} không phải số hữu hạn: {val!r}")

    if not allow_negative and dec < Decimal("0"):
        raise VenueParserError(f"Trường {field_name} không được âm: {dec}")

    return dec


def _to_optional_decimal(
    val: Any,
    field_name: str,
    *,
    allow_negative: bool = False,
) -> Decimal | None:
    """Chuyển đổi Decimal cho trường tùy chọn."""
    if val is None or val == "":
        return None
    return _to_decimal(val, field_name, allow_negative=allow_negative)


def _to_int(
    val: Any,
    field_name: str,
    *,
    allow_none: bool = False,
    positive_only: bool = True,
) -> int:
    """Kiểm tra và chuyển đổi giá trị nguyên."""
    if val is None:
        if allow_none:
            return 0
        raise VenueParserError(f"Trường {field_name} không được là None.")

    if isinstance(val, bool):
        raise VenueParserError(f"Trường {field_name} không được là kiểu bool.")

    try:
        result = int(val)
    except (ValueError, TypeError) as exc:
        raise VenueParserError(f"Trường {field_name} không phải kiểu int hợp lệ: {val!r}") from exc

    if positive_only and result <= 0:
        raise VenueParserError(f"Trường {field_name} phải là số nguyên dương: {result}")

    return result


def _to_optional_int(val: Any, field_name: str) -> int | None:
    """Chuyển đổi số nguyên tùy chọn (trả về None nếu 0, None hoặc rỗng)."""
    if val is None or val == "" or val == 0 or val == "0":
        return None
    return _to_int(val, field_name, positive_only=True)


def _to_str(val: Any, field_name: str, *, allow_empty: bool = False) -> str:
    """Kiểm tra chuỗi không rỗng."""
    if not isinstance(val, str):
        raise VenueParserError(f"Trường {field_name} phải là chuỗi.")
    stripped = val.strip()
    if not allow_empty and not stripped:
        raise VenueParserError(f"Trường {field_name} không được để trống.")
    return stripped


def _to_utc_datetime(val: Any, field_name: str, *, allow_none: bool = False) -> datetime:
    """Chuyển đổi timestamp ms hoặc datetime thành aware UTC datetime."""
    if val is None:
        if allow_none:
            return datetime.now(tz=UTC)
        raise VenueParserError(f"Trường {field_name} không được là None.")

    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=UTC)
        return val.astimezone(UTC)

    if isinstance(val, (int, float)):
        # Nếu giá trị ms (> 1e11), chia cho 1000
        sec = float(val) / 1000.0 if float(val) > 1e11 else float(val)
        try:
            return datetime.fromtimestamp(sec, tz=UTC)
        except Exception as exc:
            raise VenueParserError(f"Trường {field_name} có timestamp không hợp lệ: {val}") from exc

    if isinstance(val, str):
        try:
            num = float(val)
            sec = num / 1000.0 if num > 1e11 else num
            return datetime.fromtimestamp(sec, tz=UTC)
        except ValueError:
            pass

        try:
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except Exception as exc:
            raise VenueParserError(
                f"Trường {field_name} có định dạng datetime không hợp lệ: {val!r}"
            ) from exc

    raise VenueParserError(f"Trường {field_name} không chuyển đổi được thành datetime: {val!r}")


# ============================================================================
# Parsers
# ============================================================================


def parse_regular_order_response(
    payload: dict[str, Any] | str,
    *,
    expected_qty: Decimal | None = None,
) -> RegularOrderResponse:
    """Phân tích và xác thực phản hồi của lệnh thường Binance USDⓈ-M (fail-closed)."""
    data = _ensure_dict(payload)

    # Từ chối nếu payload thuộc algo order
    if "algoId" in data or "clientAlgoId" in data or "algoType" in data or "algoStatus" in data:
        raise VenueParserError("Payload thuộc algo order, không phải regular order.")

    # Kiểm tra ID bắt buộc
    if "orderId" not in data or data["orderId"] is None:
        raise VenueParserError("Trường orderId bắt buộc trong regular order response.")
    order_id = _to_int(data["orderId"], "orderId", positive_only=True)

    if "clientOrderId" not in data:
        raise VenueParserError("Trường clientOrderId bắt buộc trong regular order response.")
    client_order_id = _to_str(data["clientOrderId"], "clientOrderId")

    # Kiểm tra symbol
    symbol = _to_str(data.get("symbol"), "symbol")

    # Kiểm tra status
    raw_status = data.get("status")
    try:
        status = RegularOrderStatus(str(raw_status))
    except ValueError as exc:
        raise VenueParserError(f"Trường status không hợp lệ: {raw_status!r}") from exc

    # Kiểm tra order type
    raw_type = data.get("type")
    try:
        order_type = RegularOrderType(str(raw_type))
    except ValueError as exc:
        raise VenueParserError(f"Trường type không hợp lệ: {raw_type!r}") from exc

    # Kiểm tra side
    raw_side = data.get("side")
    try:
        side = OrderSide(str(raw_side))
    except ValueError as exc:
        raise VenueParserError(f"Trường side không hợp lệ: {raw_side!r}") from exc

    # Kiểm tra các trường số
    executed_qty = _to_decimal(data.get("executedQty", "0"), "executedQty")
    cum_quote = _to_decimal(data.get("cumQuote", "0"), "cumQuote")
    avg_price = _to_decimal(data.get("avgPrice", "0"), "avgPrice")
    orig_qty = _to_decimal(data.get("origQty", "0"), "origQty")
    price = _to_decimal(data.get("price", "0"), "price")

    # Ràng buộc số lượng fill
    if orig_qty > Decimal("0") and executed_qty > orig_qty:
        raise VenueParserError(
            f"executedQty ({executed_qty}) vượt quá origQty ({orig_qty}) "
            "trong regular order response."
        )

    if expected_qty is not None and executed_qty > expected_qty:
        raise VenueParserError(
            f"executedQty ({executed_qty}) vượt quá expected_qty ({expected_qty}) được yêu cầu."
        )

    # Thời gian cập nhật
    update_time = _to_utc_datetime(data.get("updateTime") or data.get("time"), "updateTime")

    reduce_only = bool(data.get("reduceOnly", False))
    close_position = bool(data.get("closePosition", False))

    return RegularOrderResponse(
        symbol=symbol,
        order_id=order_id,
        client_order_id=client_order_id,
        status=status,
        order_type=order_type,
        side=side,
        executed_qty=executed_qty,
        cum_quote=cum_quote,
        avg_price=avg_price,
        orig_qty=orig_qty,
        price=price,
        update_time=update_time,
        reduce_only=reduce_only,
        close_position=close_position,
    )


def parse_algo_order_response(payload: dict[str, Any] | str) -> AlgoOrderResponse:
    """Phân tích và xác thực phản hồi của lệnh algo conditional Binance USDⓈ-M (fail-closed)."""
    data = _ensure_dict(payload)

    # Từ chối nếu payload thuộc regular order
    if "orderId" in data and "algoId" not in data:
        raise VenueParserError("Payload thuộc regular order, không phải algo order.")

    # Kiểm tra ID bắt buộc
    if "algoId" not in data or data["algoId"] is None:
        raise VenueParserError("Trường algoId bắt buộc trong algo order response.")
    algo_id = _to_int(data["algoId"], "algoId", positive_only=True)

    if "clientAlgoId" not in data:
        raise VenueParserError("Trường clientAlgoId bắt buộc trong algo order response.")
    client_algo_id = _to_str(data["clientAlgoId"], "clientAlgoId")

    symbol = _to_str(data.get("symbol"), "symbol")

    # Kiểm tra algoType
    raw_type = data.get("algoType")
    try:
        algo_type = ConditionalAlgoOrderType(str(raw_type))
    except ValueError as exc:
        raise VenueParserError(f"Trường algoType không hợp lệ: {raw_type!r}") from exc

    # Kiểm tra algoStatus
    raw_status = data.get("algoStatus")
    try:
        algo_status = AlgoOrderStatus(str(raw_status))
    except ValueError as exc:
        raise VenueParserError(f"Trường algoStatus không hợp lệ: {raw_status!r}") from exc

    # Kiểm tra side
    raw_side = data.get("side")
    try:
        side = OrderSide(str(raw_side))
    except ValueError as exc:
        raise VenueParserError(f"Trường side không hợp lệ: {raw_side!r}") from exc

    # Giá trigger
    trigger_price = _to_decimal(data.get("triggerPrice", "0"), "triggerPrice")

    price = _to_optional_decimal(data.get("price"), "price")
    quantity = _to_optional_decimal(data.get("quantity"), "quantity")

    actual_order_id = _to_optional_int(data.get("actualOrderId"), "actualOrderId")
    actual_price = _to_optional_decimal(data.get("actualPrice"), "actualPrice")

    update_time_raw = data.get("updateTime") or data.get("createTime")
    update_time = (
        _to_utc_datetime(update_time_raw, "updateTime") if update_time_raw is not None else None
    )

    return AlgoOrderResponse(
        algo_id=algo_id,
        client_algo_id=client_algo_id,
        algo_type=algo_type,
        algo_status=algo_status,
        symbol=symbol,
        side=side,
        trigger_price=trigger_price,
        price=price,
        quantity=quantity,
        actual_order_id=actual_order_id,
        actual_price=actual_price,
        update_time=update_time,
    )


def parse_order_trade_update(payload: dict[str, Any] | str) -> OrderTradeUpdateEvent:
    """Phân tích và xác thực sự kiện `ORDER_TRADE_UPDATE` từ User Data Stream."""
    data = _ensure_dict(payload)

    event_type = data.get("e")
    if event_type != "ORDER_TRADE_UPDATE":
        raise VenueParserError(f"Không phải ORDER_TRADE_UPDATE event (nhận: {event_type!r}).")

    event_time = _to_utc_datetime(data.get("E"), "E")
    transaction_time = _to_utc_datetime(data.get("T"), "T")

    nested = data.get("o")
    if not isinstance(nested, dict):
        raise VenueParserError("Thiếu trường o (order details dict) trong ORDER_TRADE_UPDATE.")

    symbol = _to_str(nested.get("s"), "s (symbol)")
    client_order_id = _to_str(nested.get("c"), "c (clientOrderId)")
    order_id = _to_int(nested.get("i"), "i (orderId)", positive_only=True)

    raw_side = nested.get("S")
    try:
        side = OrderSide(str(raw_side))
    except ValueError as exc:
        raise VenueParserError(f"Trường S (side) không hợp lệ: {raw_side!r}") from exc

    raw_order_type = nested.get("o")
    try:
        order_type = RegularOrderType(str(raw_order_type))
    except ValueError as exc:
        raise VenueParserError(f"Trường o (orderType) không hợp lệ: {raw_order_type!r}") from exc

    raw_exec_type = nested.get("x")
    try:
        execution_type = ExecutionType(str(raw_exec_type))
    except ValueError as exc:
        raise VenueParserError(f"Trường x (executionType) không hợp lệ: {raw_exec_type!r}") from exc

    raw_status = nested.get("X")
    try:
        order_status = RegularOrderStatus(str(raw_status))
    except ValueError as exc:
        raise VenueParserError(f"Trường X (orderStatus) không hợp lệ: {raw_status!r}") from exc

    last_filled_qty = _to_decimal(nested.get("l", "0"), "last_filled_qty")
    cum_filled_qty = _to_decimal(nested.get("z", "0"), "cum_filled_qty")
    last_filled_price = _to_decimal(nested.get("L", "0"), "last_filled_price")
    orig_qty = _to_decimal(nested.get("q", "0"), "orig_qty")
    avg_price = _to_decimal(nested.get("ap", "0"), "avg_price")

    trade_id = _to_int(nested.get("t", 0), "t (tradeId)", positive_only=False)
    order_trade_time = _to_utc_datetime(nested.get("T"), "T (orderTradeTime)")

    return OrderTradeUpdateEvent(
        event_time=event_time,
        transaction_time=transaction_time,
        symbol=symbol,
        client_order_id=client_order_id,
        order_id=order_id,
        side=side,
        order_type=order_type,
        execution_type=execution_type,
        order_status=order_status,
        last_filled_qty=last_filled_qty,
        cum_filled_qty=cum_filled_qty,
        last_filled_price=last_filled_price,
        orig_qty=orig_qty,
        avg_price=avg_price,
        trade_id=trade_id,
        order_trade_time=order_trade_time,
    )


def parse_algo_update(payload: dict[str, Any] | str) -> AlgoUpdateEvent:
    """Phân tích và xác thực sự kiện `ALGO_UPDATE` từ User Data Stream."""
    data = _ensure_dict(payload)

    event_type = data.get("e")
    if event_type != "ALGO_UPDATE":
        raise VenueParserError(f"Không phải ALGO_UPDATE event (nhận: {event_type!r}).")

    event_time = _to_utc_datetime(data.get("E"), "E")
    transaction_time = _to_utc_datetime(data.get("T"), "T")

    # Có thể ở trong nested 'o' hoặc ở cấp top-level
    raw_nested = data.get("o")
    src: dict[str, Any] = raw_nested if isinstance(raw_nested, dict) else data

    # Lấy các trường hỗ trợ cả ký hiệu ngắn (ai, ca, at, as, s, S, tp, ao, cr) lẫn dài
    algo_id_raw = src.get("ai") or src.get("algoId")
    if algo_id_raw is None:
        raise VenueParserError("Thiếu algoId trong ALGO_UPDATE.")
    algo_id = _to_int(algo_id_raw, "algoId", positive_only=True)

    client_algo_id_raw = src.get("ca") or src.get("clientAlgoId")
    client_algo_id = _to_str(client_algo_id_raw, "clientAlgoId")

    raw_type = src.get("at") or src.get("algoType")
    try:
        algo_type = ConditionalAlgoOrderType(str(raw_type))
    except ValueError as exc:
        raise VenueParserError(
            f"Trường algoType không hợp lệ trong ALGO_UPDATE: {raw_type!r}"
        ) from exc

    raw_status = src.get("as") or src.get("algoStatus")
    try:
        algo_status = AlgoOrderStatus(str(raw_status))
    except ValueError as exc:
        raise VenueParserError(
            f"Trường algoStatus không hợp lệ trong ALGO_UPDATE: {raw_status!r}"
        ) from exc

    symbol = _to_str(src.get("s") or src.get("symbol"), "symbol")

    raw_side = src.get("S") or src.get("side")
    try:
        side = OrderSide(str(raw_side))
    except ValueError as exc:
        raise VenueParserError(f"Trường side không hợp lệ trong ALGO_UPDATE: {raw_side!r}") from exc

    trigger_price = _to_decimal(src.get("tp") or src.get("triggerPrice", "0"), "triggerPrice")

    actual_order_id = _to_optional_int(src.get("ao") or src.get("actualOrderId"), "actualOrderId")
    rejection_reason_raw = src.get("cr") or src.get("rejectionReason")
    rejection_reason = str(rejection_reason_raw) if rejection_reason_raw else None

    return AlgoUpdateEvent(
        event_time=event_time,
        transaction_time=transaction_time,
        algo_id=algo_id,
        client_algo_id=client_algo_id,
        algo_type=algo_type,
        algo_status=algo_status,
        symbol=symbol,
        side=side,
        trigger_price=trigger_price,
        actual_order_id=actual_order_id,
        rejection_reason=rejection_reason,
    )


def parse_account_update(payload: dict[str, Any] | str) -> AccountUpdateEvent:
    """Phân tích và xác thực sự kiện `ACCOUNT_UPDATE` từ User Data Stream."""
    data = _ensure_dict(payload)

    event_type = data.get("e")
    if event_type != "ACCOUNT_UPDATE":
        raise VenueParserError(f"Không phải ACCOUNT_UPDATE event (nhận: {event_type!r}).")

    event_time = _to_utc_datetime(data.get("E"), "E")
    transaction_time = _to_utc_datetime(data.get("T"), "T")

    nested = data.get("a")
    if not isinstance(nested, dict):
        raise VenueParserError("Thiếu trường a (account update dict) trong ACCOUNT_UPDATE.")

    event_reason = _to_str(nested.get("m"), "m (event_reason)")

    balances_raw = nested.get("B", [])
    if not isinstance(balances_raw, list):
        raise VenueParserError("Trường B (balances) phải là list.")

    balances: list[AccountBalanceUpdate] = []
    for item in balances_raw:
        if not isinstance(item, dict):
            raise VenueParserError("Mỗi phần tử balance phải là dict.")
        asset = _to_str(item.get("a"), "a (asset)")
        wallet_balance = _to_decimal(item.get("wb", "0"), "wallet_balance")
        cross_wallet_balance = _to_decimal(item.get("cw", "0"), "cross_wallet_balance")
        balances.append(
            AccountBalanceUpdate(
                asset=asset,
                wallet_balance=wallet_balance,
                cross_wallet_balance=cross_wallet_balance,
            )
        )

    positions_raw = nested.get("P", [])
    if not isinstance(positions_raw, list):
        raise VenueParserError("Trường P (positions) phải là list.")

    positions: list[PositionUpdate] = []
    for pos_item in positions_raw:
        if not isinstance(pos_item, dict):
            raise VenueParserError("Mỗi phần tử position phải là dict.")
        symbol = _to_str(pos_item.get("s"), "s (symbol)")
        # position_amount và unrealized_profit được phép âm!
        position_amount = _to_decimal(
            pos_item.get("pa", "0"), "position_amount", allow_negative=True
        )
        entry_price = _to_decimal(pos_item.get("ep", "0"), "entry_price", allow_negative=False)
        unrealized_profit = _to_decimal(
            pos_item.get("up", "0"), "unrealized_profit", allow_negative=True
        )
        margin_type = str(pos_item.get("mt", "cross"))
        isolated_wallet = _to_decimal(
            pos_item.get("iw", "0"), "isolated_wallet", allow_negative=False
        )
        position_side = str(pos_item.get("ps", "BOTH"))

        positions.append(
            PositionUpdate(
                symbol=symbol,
                position_amount=position_amount,
                entry_price=entry_price,
                unrealized_profit=unrealized_profit,
                margin_type=margin_type,
                isolated_wallet=isolated_wallet,
                position_side=position_side,
            )
        )

    return AccountUpdateEvent(
        event_time=event_time,
        transaction_time=transaction_time,
        event_reason=event_reason,
        balances=tuple(balances),
        positions=tuple(positions),
    )
