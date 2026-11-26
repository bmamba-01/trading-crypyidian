"""Hợp đồng domain cho lệnh bảo vệ Algo Service USDⓈ-M (WP-03C.2 / CRY-293).

Định nghĩa:
- Kiểm tra ràng buộc tham số theo tài liệu Binance USDⓈ-M Algo Service hiện hành
  (kiểm tra 2026-11-26): `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `STOP`, `TAKE_PROFIT`,
  `TRAILING_STOP_MARKET`, `workingType`, `closePosition`, `reduceOnly`, `positionSide`,
  và `callbackRate` trong đoạn [0.1, 20] (Change Log 23/10/2026).
- Quan hệ bền vững: entry intent ↔ venue algo order ↔ lệnh thường sinh sau trigger ↔ fills.
- Đánh giá độ phủ bảo vệ chỉ dựa trên trạng thái sàn xác nhận (không dựa vào intent nội bộ).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from trading_bot.adapters.binance_usdm.order_schemas import (
    AlgoOrderStatus,
    ConditionalAlgoOrderType,
)

ZERO = Decimal("0")
MIN_CALLBACK_RATE = Decimal("0.1")
MAX_CALLBACK_RATE = Decimal("20")
_CLIENT_ALGO_ID_RE = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")
_CLOSE_POSITION_ALLOWED_TYPES = frozenset(
    {
        ConditionalAlgoOrderType.STOP_MARKET,
        ConditionalAlgoOrderType.TAKE_PROFIT_MARKET,
    }
)
_LIMIT_PRICE_REQUIRED_TYPES = frozenset(
    {
        ConditionalAlgoOrderType.STOP,
        ConditionalAlgoOrderType.TAKE_PROFIT,
    }
)
_ACTIVE_ALGO_STATUSES = frozenset(
    {
        AlgoOrderStatus.NEW,
        AlgoOrderStatus.TRIGGERING,
        AlgoOrderStatus.TRIGGERED,
    }
)


class AlgoParameterError(ValueError):
    """Tham số lệnh Algo vi phạm ràng buộc hợp đồng Binance USDⓈ-M."""


class WorkingType(StrEnum):
    """Nguồn giá kích hoạt lệnh điều kiện trên Binance USDⓈ-M."""

    MARK_PRICE = "MARK_PRICE"
    CONTRACT_PRICE = "CONTRACT_PRICE"


class PositionMode(StrEnum):
    """Chế độ vị thế tài khoản Binance Futures."""

    ONE_WAY = "ONE_WAY"
    HEDGE = "HEDGE"


class PositionSide(StrEnum):
    """Phía vị thế tương ứng với chế độ One-Way hoặc Hedge."""

    BOTH = "BOTH"
    LONG = "LONG"
    SHORT = "SHORT"


class ProtectionGateState(StrEnum):
    """Trạng thái cổng bảo vệ của một vị thế đang theo dõi."""

    NO_EXPOSURE = "NO_EXPOSURE"
    COVERED = "COVERED"
    REPLACING = "REPLACING"
    UNCOVERED_BLOCKED = "UNCOVERED_BLOCKED"
    REJECTED_ESCALATED = "REJECTED_ESCALATED"


@dataclass(frozen=True, slots=True)
class AlgoOrderParams:
    """Tham số đặt lệnh điều kiện qua `POST /fapi/v1/algoOrder` (algoType=CONDITIONAL)."""

    symbol: str
    side: str
    order_type: ConditionalAlgoOrderType
    client_algo_id: str
    working_type: WorkingType = WorkingType.MARK_PRICE
    position_mode: PositionMode = PositionMode.ONE_WAY
    position_side: PositionSide = PositionSide.BOTH
    trigger_price: Decimal | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    reduce_only: bool = False
    close_position: bool = False
    callback_rate: Decimal | None = None
    activate_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise AlgoParameterError("symbol phải là chuỗi không rỗng.")
        if self.side not in {"BUY", "SELL"}:
            raise AlgoParameterError("side chỉ nhận BUY hoặc SELL.")
        if not isinstance(self.order_type, ConditionalAlgoOrderType):
            raise AlgoParameterError("order_type phải thuộc ConditionalAlgoOrderType.")
        if not _CLIENT_ALGO_ID_RE.match(self.client_algo_id):
            raise AlgoParameterError("client_algo_id không hợp lệ theo quy tắc Binance (1..36).")
        if not isinstance(self.working_type, WorkingType):
            raise AlgoParameterError("working_type phải là MARK_PRICE hoặc CONTRACT_PRICE.")
        if not isinstance(self.position_mode, PositionMode):
            raise AlgoParameterError("position_mode phải là ONE_WAY hoặc HEDGE.")
        if not isinstance(self.position_side, PositionSide):
            raise AlgoParameterError("position_side không hợp lệ.")

        # Ràng buộc chế độ One-Way vs Hedge (USDⓈ-M Futures)
        if self.position_mode is PositionMode.ONE_WAY:
            if self.position_side is not PositionSide.BOTH:
                raise AlgoParameterError("Chế độ ONE_WAY bắt buộc position_side=BOTH.")
        else:
            if self.position_side is PositionSide.BOTH:
                raise AlgoParameterError("Chế độ HEDGE bắt buộc position_side là LONG hoặc SHORT.")
            if self.reduce_only:
                raise AlgoParameterError("Không được gửi reduceOnly=true trong chế độ HEDGE.")

        # Ràng buộc closePosition vs quantity và reduceOnly
        if self.close_position:
            if self.order_type not in _CLOSE_POSITION_ALLOWED_TYPES:
                raise AlgoParameterError(
                    "closePosition=true chỉ hỗ trợ cho STOP_MARKET hoặc TAKE_PROFIT_MARKET."
                )
            if self.quantity is not None:
                raise AlgoParameterError("Không được gửi quantity cùng closePosition=true.")
            if self.reduce_only:
                raise AlgoParameterError("Không được gửi reduceOnly=true cùng closePosition=true.")
        else:
            if self.quantity is None or not self.quantity.is_finite() or self.quantity <= ZERO:
                raise AlgoParameterError("quantity phải là số dương khi closePosition=false.")

        # Ràng buộc theo từng loại lệnh điều kiện
        if self.order_type is ConditionalAlgoOrderType.TRAILING_STOP_MARKET:
            if (
                self.callback_rate is None
                or not self.callback_rate.is_finite()
                or not (MIN_CALLBACK_RATE <= self.callback_rate <= MAX_CALLBACK_RATE)
            ):
                raise AlgoParameterError(
                    "TRAILING_STOP_MARKET yêu cầu callbackRate trong đoạn [0.1, 20]."
                )
            if self.activate_price is not None and (
                not self.activate_price.is_finite() or self.activate_price <= ZERO
            ):
                raise AlgoParameterError("activatePrice phải lớn hơn 0 nếu được cung cấp.")
            if self.price is not None:
                raise AlgoParameterError("TRAILING_STOP_MARKET không nhận trường price.")
        elif self.order_type in _LIMIT_PRICE_REQUIRED_TYPES:
            if (
                self.trigger_price is None
                or not self.trigger_price.is_finite()
                or self.trigger_price <= ZERO
            ):
                raise AlgoParameterError(f"{self.order_type.value} yêu cầu triggerPrice > 0.")
            if self.price is None or not self.price.is_finite() or self.price <= ZERO:
                raise AlgoParameterError(f"{self.order_type.value} yêu cầu price > 0.")
            if self.callback_rate is not None:
                raise AlgoParameterError(f"{self.order_type.value} không nhận callbackRate.")
        else:
            # STOP_MARKET / TAKE_PROFIT_MARKET
            if (
                self.trigger_price is None
                or not self.trigger_price.is_finite()
                or self.trigger_price <= ZERO
            ):
                raise AlgoParameterError(f"{self.order_type.value} yêu cầu triggerPrice > 0.")
            if self.price is not None:
                raise AlgoParameterError(f"{self.order_type.value} không nhận trường price.")
            if self.callback_rate is not None:
                raise AlgoParameterError(f"{self.order_type.value} không nhận callbackRate.")

    def to_query_params(self) -> dict[str, str]:
        """Chuyển đổi sang bộ tham số REST chuẩn cho `POST /fapi/v1/algoOrder`."""
        params: dict[str, str] = {
            "algoType": "CONDITIONAL",
            "symbol": self.symbol,
            "side": self.side,
            "type": self.order_type.value,
            "workingType": self.working_type.value,
            "positionSide": self.position_side.value,
            "clientAlgoId": self.client_algo_id,
        }
        if self.trigger_price is not None:
            params["triggerPrice"] = _fmt_decimal(self.trigger_price)
        if self.price is not None:
            params["price"] = _fmt_decimal(self.price)
        if self.quantity is not None:
            params["quantity"] = _fmt_decimal(self.quantity)
        if self.reduce_only:
            params["reduceOnly"] = "true"
        if self.close_position:
            params["closePosition"] = "true"
        if self.callback_rate is not None:
            params["callbackRate"] = _fmt_decimal(self.callback_rate)
        if self.activate_price is not None:
            params["activatePrice"] = _fmt_decimal(self.activate_price)
        return params


@dataclass(frozen=True, slots=True)
class AlgoProtectionLink:
    """Quan hệ bền vững entry intent ↔ venue algo order ↔ lệnh thường sau trigger ↔ fills."""

    entry_intent_id: UUID
    symbol: str
    protective_side: str
    position_side: PositionSide
    order_type: ConditionalAlgoOrderType
    client_algo_id: str
    working_type: WorkingType
    requested_quantity: Decimal
    reduce_only: bool
    close_position: bool
    algo_id: int | None = None
    algo_status: AlgoOrderStatus = AlgoOrderStatus.UNKNOWN
    confirmed_quantity: Decimal = ZERO
    triggered_order_id: str | None = None
    triggered_quantity: Decimal = ZERO
    linked_fill_ids: tuple[str, ...] = ()
    rejection_reason: str | None = None
    updated_at: datetime | None = None

    @property
    def is_confirmed_active(self) -> bool:
        """Chỉ coi là bảo vệ đang có hiệu lực khi sàn đã ACK ở trạng thái NEW."""
        return self.algo_id is not None and self.algo_status is AlgoOrderStatus.NEW

    @property
    def is_open_on_exchange(self) -> bool:
        """Lệnh algo vẫn còn đang mở hoặc đang kích hoạt trên sàn."""
        return self.algo_id is not None and self.algo_status in _ACTIVE_ALGO_STATUSES


@dataclass(frozen=True, slots=True)
class ProtectionCoverageAssessment:
    """Kết quả đánh giá độ phủ bảo vệ dựa trên trạng thái sàn xác nhận."""

    symbol: str
    position_side: PositionSide
    confirmed_position_qty: Decimal
    confirmed_protective_qty: Decimal
    uncovered_quantity: Decimal
    gate_state: ProtectionGateState
    entry_gate_open: bool
    incident_reason_code: str | None = None
    orphan_algo_ids: tuple[int, ...] = field(default_factory=tuple)


def assess_protection_coverage(
    *,
    symbol: str,
    position_side: PositionSide,
    confirmed_position_qty: Decimal,
    algo_links: tuple[AlgoProtectionLink, ...],
) -> ProtectionCoverageAssessment:
    """Đánh giá độ phủ bảo vệ theo trạng thái sàn xác nhận (fail-closed).

    Quy tắc bất biến:
    1. Không tin intent nội bộ: chỉ tính `confirmed_quantity` (hoặc toàn bộ vị thế nếu
       `close_position=True`) của các algo đã được sàn ACK `NEW`.
    2. Bất kỳ algo nào bị `REJECTED` (ví dụ qua `ALGO_UPDATE.REJECTED`) đều đóng cổng
       và leo thang `PROTECTION_REJECTED`.
    3. Vị thế bằng 0 nhưng vẫn còn algo đang mở trên sàn → đánh dấu `orphan_algo_ids`.
    4. Vị thế dương nhưng `confirmed_protective_qty < confirmed_position_qty` → đóng cổng
       `UNCOVERED_BLOCKED` với `PROTECTION_MISSING`.
    """
    if confirmed_position_qty < ZERO:
        raise AlgoParameterError("confirmed_position_qty phải không âm (dùng giá trị tuyệt đối).")

    relevant = tuple(
        link for link in algo_links if link.symbol == symbol and link.position_side is position_side
    )
    open_ids = tuple(
        link.algo_id for link in relevant if link.algo_id is not None and link.is_open_on_exchange
    )

    if confirmed_position_qty == ZERO:
        return ProtectionCoverageAssessment(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=ZERO,
            confirmed_protective_qty=ZERO,
            uncovered_quantity=ZERO,
            gate_state=ProtectionGateState.NO_EXPOSURE,
            entry_gate_open=len(open_ids) == 0,
            incident_reason_code="ORPHAN_ALGO_PRESENT" if open_ids else None,
            orphan_algo_ids=open_ids,
        )

    rejected = [link for link in relevant if link.algo_status is AlgoOrderStatus.REJECTED]
    active = [link for link in relevant if link.is_confirmed_active]

    if any(link.close_position for link in active):
        confirmed_qty = confirmed_position_qty
    else:
        confirmed_qty = sum((link.confirmed_quantity for link in active), ZERO)

    uncovered = max(ZERO, confirmed_position_qty - confirmed_qty)

    if rejected and uncovered > ZERO:
        return ProtectionCoverageAssessment(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=confirmed_position_qty,
            confirmed_protective_qty=confirmed_qty,
            uncovered_quantity=uncovered,
            gate_state=ProtectionGateState.REJECTED_ESCALATED,
            entry_gate_open=False,
            incident_reason_code="PROTECTION_REJECTED",
        )

    if uncovered > ZERO:
        return ProtectionCoverageAssessment(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=confirmed_position_qty,
            confirmed_protective_qty=confirmed_qty,
            uncovered_quantity=uncovered,
            gate_state=ProtectionGateState.UNCOVERED_BLOCKED,
            entry_gate_open=False,
            incident_reason_code="PROTECTION_MISSING",
        )

    if rejected:
        return ProtectionCoverageAssessment(
            symbol=symbol,
            position_side=position_side,
            confirmed_position_qty=confirmed_position_qty,
            confirmed_protective_qty=confirmed_qty,
            uncovered_quantity=ZERO,
            gate_state=ProtectionGateState.REJECTED_ESCALATED,
            entry_gate_open=False,
            incident_reason_code="PROTECTION_REJECTED",
        )

    return ProtectionCoverageAssessment(
        symbol=symbol,
        position_side=position_side,
        confirmed_position_qty=confirmed_position_qty,
        confirmed_protective_qty=confirmed_qty,
        uncovered_quantity=ZERO,
        gate_state=ProtectionGateState.COVERED,
        entry_gate_open=True,
        incident_reason_code=None,
    )


def _fmt_decimal(value: Decimal) -> str:
    """Định dạng Decimal gọn không dùng ký hiệu khoa học."""
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
