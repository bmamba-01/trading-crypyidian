"""Chuẩn hóa và giảm trạng thái sự kiện User Data Stream Binance USDⓈ-M (WP-03C.1 / CRY-292).

Đảm bảo:
- `CanonicalVenueEventEnvelope` lưu loại sự kiện, thời gian event/transaction/receive,
  phạm vi account, mã event/trade/order/client, phiên bản schema và facts đã khử nhạy cảm.
- Xử lý đầy đủ `ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE` (gồm `LIQUIDATION`/hoạt động ngoài luồng),
  `ALGO_UPDATE` (`TRIGGERING`/`REJECTED`/`EXPIRED`), `listenKeyExpired` và sự kiện lạ (fail-closed).
- `VenueEventReducer` chịu được sự kiện lặp và đến sai thứ tự (out-of-order):
  * Không bao giờ nhân đôi fill khi replay cùng `trade_id` hoặc `event_id`.
  * Không dùng `event_time_ms` đơn thuần để loại bỏ một `trade_id` riêng biệt chưa ghi nhận.
  * Không bao giờ làm lùi trạng thái kết thúc (`FILLED`, `CANCELED`, `EXPIRED`, `REJECTED`).
  * Hội tụ idempotent khi kết hợp replay stream với REST snapshot (`apply_rest_snapshot`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from trading_bot.adapters.binance_usdm.order_schemas import (
    AlgoOrder,
    AlgoOrderStatus,
    OrderSchemaError,
    RegularOrder,
    decode_order_object,
    parse_account_update_event,
    parse_algo_update,
    parse_order_trade_update,
)
from trading_bot.domain.execution.venue_orders import VenueOrderState

ZERO = Decimal("0")
VENUE_EVENT_SCHEMA_VERSION = "wp03c-venue-event-v1"
_EXTERNAL_ACCOUNT_REASONS = frozenset({"LIQUIDATION", "ADL", "MARGIN_TRANSFER", "ADMIN_DEPOSIT"})
_TERMINAL_ORDER_STATES = frozenset(
    {
        VenueOrderState.FILLED,
        VenueOrderState.CANCELED,
        VenueOrderState.EXPIRED,
        VenueOrderState.REJECTED,
    }
)
_TERMINAL_ALGO_STATES = frozenset(
    {
        AlgoOrderStatus.FINISHED,
        AlgoOrderStatus.CANCELED,
        AlgoOrderStatus.REJECTED,
        AlgoOrderStatus.EXPIRED,
    }
)
_STATE_RANK: dict[VenueOrderState, int] = {
    VenueOrderState.UNKNOWN: 0,
    VenueOrderState.CREATED: 1,
    VenueOrderState.SUBMITTING: 2,
    VenueOrderState.RETRYABLE: 2,
    VenueOrderState.ACKNOWLEDGED: 3,
    VenueOrderState.PARTIALLY_FILLED: 4,
    VenueOrderState.FILLED: 5,
    VenueOrderState.CANCELED: 5,
    VenueOrderState.EXPIRED: 5,
    VenueOrderState.REJECTED: 5,
}


class VenueEventType(StrEnum):
    """Loại sự kiện chuẩn hóa từ User Data Stream Binance USDⓈ-M."""

    ORDER_TRADE_UPDATE = "ORDER_TRADE_UPDATE"
    ACCOUNT_UPDATE = "ACCOUNT_UPDATE"
    ALGO_UPDATE = "ALGO_UPDATE"
    LISTEN_KEY_EXPIRED = "LISTEN_KEY_EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CanonicalVenueEventEnvelope:
    """Phong bì sự kiện venue chuẩn hóa, đã khử toàn bộ thông tin nhạy cảm."""

    event_type: VenueEventType
    workspace_id: str
    account_id: str
    event_time_ms: int
    transaction_time_ms: int | None
    received_at: datetime
    event_id: str
    schema_version: str = VENUE_EVENT_SCHEMA_VERSION
    symbol: str | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    trade_id: str | None = None
    algo_id: int | None = None
    is_liquidation_or_external: bool = False
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReducedOrderState:
    """Trạng thái lệnh thường hội tụ sau khi áp dụng stream và/hoặc REST snapshot."""

    symbol: str
    order_id: str
    client_order_id: str
    state: VenueOrderState
    cumulative_filled: Decimal
    seen_trade_ids: tuple[str, ...] = ()
    last_event_time_ms: int = 0


@dataclass(frozen=True, slots=True)
class ReducedAlgoState:
    """Trạng thái lệnh Algo hội tụ theo `algo_id`."""

    symbol: str
    algo_id: int
    client_algo_id: str
    status: AlgoOrderStatus
    actual_order_id: str | None = None
    actual_quantity: Decimal | None = None
    last_event_time_ms: int = 0


@dataclass(frozen=True, slots=True)
class ReducedPositionState:
    """Trạng thái vị thế hội tụ theo `(symbol, position_side)`."""

    symbol: str
    position_side: str
    position_amount: Decimal
    entry_price: Decimal
    unrealized_profit: Decimal
    last_event_time_ms: int = 0


def normalize_user_stream_payload(
    payload: object,
    *,
    workspace_id: str,
    account_id: str,
    received_at: datetime | None = None,
) -> CanonicalVenueEventEnvelope:
    """Phân tích và chuẩn hóa payload User Data Stream sang `CanonicalVenueEventEnvelope`."""
    ts = received_at or datetime.now(UTC)
    try:
        raw = decode_order_object(payload)
    except OrderSchemaError:
        return CanonicalVenueEventEnvelope(
            event_type=VenueEventType.UNKNOWN,
            workspace_id=workspace_id,
            account_id=account_id,
            event_time_ms=0,
            transaction_time_ms=None,
            received_at=ts,
            event_id=f"unknown:{int(ts.timestamp() * 1000)}",
            facts={"error": "malformed_json"},
        )

    event_name = raw.get("e")
    if event_name == "listenKeyExpired":
        event_time = _safe_int(raw.get("E"))
        return CanonicalVenueEventEnvelope(
            event_type=VenueEventType.LISTEN_KEY_EXPIRED,
            workspace_id=workspace_id,
            account_id=account_id,
            event_time_ms=event_time,
            transaction_time_ms=None,
            received_at=ts,
            event_id=f"listen_key_expired:{event_time}",
            facts={"reason": "listenKeyExpired"},
        )

    if event_name == "ORDER_TRADE_UPDATE":
        try:
            evt = parse_order_trade_update(raw)
        except OrderSchemaError:
            return _unknown_envelope(workspace_id, account_id, ts, raw, "invalid_order_trade")
        order_id_str = str(evt.order.order_id)
        trade_id_str = str(evt.trade_id) if evt.trade_id > 0 else None
        is_ext = evt.order.client_order_id.startswith(("autoclose-", "adl_autoclose"))
        ev_id = (
            f"trade:{order_id_str}:{trade_id_str}"
            if trade_id_str is not None
            else (
                f"order:{order_id_str}:{evt.execution_type}:{evt.order.raw_status}:"
                f"{evt.order.executed_quantity}:{evt.transaction_time_ms}"
            )
        )
        return CanonicalVenueEventEnvelope(
            event_type=VenueEventType.ORDER_TRADE_UPDATE,
            workspace_id=workspace_id,
            account_id=account_id,
            event_time_ms=evt.event_time_ms,
            transaction_time_ms=evt.transaction_time_ms,
            received_at=ts,
            event_id=ev_id,
            symbol=evt.order.symbol,
            order_id=order_id_str,
            client_order_id=evt.order.client_order_id,
            trade_id=trade_id_str,
            is_liquidation_or_external=is_ext,
            facts={
                "canonical_state": evt.canonical_state.value,
                "execution_type": evt.execution_type,
                "executed_qty": str(evt.order.executed_quantity or ZERO),
                "last_filled_qty": str(evt.last_filled_quantity),
                "last_filled_price": str(evt.last_filled_price),
            },
        )

    if event_name == "ALGO_UPDATE":
        try:
            algo_evt = parse_algo_update(raw)
        except OrderSchemaError:
            return _unknown_envelope(workspace_id, account_id, ts, raw, "invalid_algo_update")
        a = algo_evt.order
        ev_id = f"algo:{a.algo_id}:{a.status.value}:{algo_evt.transaction_time_ms}"
        return CanonicalVenueEventEnvelope(
            event_type=VenueEventType.ALGO_UPDATE,
            workspace_id=workspace_id,
            account_id=account_id,
            event_time_ms=algo_evt.event_time_ms,
            transaction_time_ms=algo_evt.transaction_time_ms,
            received_at=ts,
            event_id=ev_id,
            symbol=a.symbol,
            order_id=a.actual_order_id,
            client_order_id=a.client_algo_id,
            algo_id=a.algo_id,
            facts={
                "algo_status": a.status.value,
                "order_type": a.order_type.value,
                "trigger_price": str(a.trigger_price),
                "actual_order_id": a.actual_order_id,
                "actual_quantity": (
                    str(a.actual_quantity) if a.actual_quantity is not None else None
                ),
            },
        )

    if event_name == "ACCOUNT_UPDATE":
        try:
            acc_evt = parse_account_update_event(raw)
        except OrderSchemaError:
            return _unknown_envelope(workspace_id, account_id, ts, raw, "invalid_account_update")
        is_ext = acc_evt.event_reason in _EXTERNAL_ACCOUNT_REASONS
        ev_id = (
            f"account:{acc_evt.event_reason}:{acc_evt.transaction_time_ms}:{acc_evt.event_time_ms}"
        )
        return CanonicalVenueEventEnvelope(
            event_type=VenueEventType.ACCOUNT_UPDATE,
            workspace_id=workspace_id,
            account_id=account_id,
            event_time_ms=acc_evt.event_time_ms,
            transaction_time_ms=acc_evt.transaction_time_ms,
            received_at=ts,
            event_id=ev_id,
            is_liquidation_or_external=is_ext,
            facts={
                "event_reason": acc_evt.event_reason,
                "balances": [
                    {
                        "asset": b.asset,
                        "wallet_balance": str(b.wallet_balance),
                        "cross_wallet_balance": str(b.cross_wallet_balance),
                    }
                    for b in acc_evt.balances
                ],
                "positions": [
                    {
                        "symbol": p.symbol,
                        "position_side": p.position_side,
                        "position_amount": str(p.position_amount),
                        "entry_price": str(p.entry_price),
                        "unrealized_profit": str(p.unrealized_profit),
                    }
                    for p in acc_evt.positions
                ],
            },
        )

    return _unknown_envelope(workspace_id, account_id, ts, raw, "unsupported_event_type")


class VenueEventReducer:
    """Bộ giảm trạng thái sự kiện venue chịu lặp, out-of-order và hội tụ với REST snapshot."""

    def __init__(self) -> None:
        self._orders: dict[str, ReducedOrderState] = {}
        self._algos: dict[int, ReducedAlgoState] = {}
        self._positions: dict[tuple[str, str], ReducedPositionState] = {}
        self._seen_event_ids: set[str] = set()
        self._seen_trade_ids: set[str] = set()
        self._external_events: list[CanonicalVenueEventEnvelope] = []

    @property
    def orders(self) -> dict[str, ReducedOrderState]:
        return dict(self._orders)

    @property
    def algos(self) -> dict[int, ReducedAlgoState]:
        return dict(self._algos)

    @property
    def positions(self) -> dict[tuple[str, str], ReducedPositionState]:
        return dict(self._positions)

    @property
    def external_events(self) -> tuple[CanonicalVenueEventEnvelope, ...]:
        return tuple(self._external_events)

    def apply(self, envelope: CanonicalVenueEventEnvelope) -> bool:
        """Áp dụng một `CanonicalVenueEventEnvelope`. Trả về False nếu là sự kiện trùng lặp."""
        if envelope.event_id in self._seen_event_ids:
            return False
        self._seen_event_ids.add(envelope.event_id)

        if envelope.is_liquidation_or_external:
            self._external_events.append(envelope)

        if envelope.event_type is VenueEventType.ORDER_TRADE_UPDATE:
            self._apply_order_event(envelope)
        elif envelope.event_type is VenueEventType.ALGO_UPDATE:
            self._apply_algo_event(envelope)
        elif envelope.event_type is VenueEventType.ACCOUNT_UPDATE:
            self._apply_account_event(envelope)
        return True

    def apply_rest_snapshot(
        self,
        *,
        orders: tuple[RegularOrder, ...] = (),
        algos: tuple[AlgoOrder, ...] = (),
        positions: tuple[ReducedPositionState, ...] = (),
        snapshot_time_ms: int = 0,
    ) -> None:
        """Hội tụ idempotent trạng thái hiện tại với REST snapshot sau disconnect/gap."""
        for order in orders:
            oid = str(order.order_id)
            incoming_state = order.canonical_state
            incoming_cum = order.executed_quantity or ZERO
            existing = self._orders.get(oid)
            if existing is None:
                self._orders[oid] = ReducedOrderState(
                    symbol=order.symbol,
                    order_id=oid,
                    client_order_id=order.client_order_id,
                    state=incoming_state,
                    cumulative_filled=incoming_cum,
                    last_event_time_ms=order.update_time_ms or snapshot_time_ms,
                )
            else:
                merged_state = _merge_order_state(existing.state, incoming_state)
                self._orders[oid] = replace(
                    existing,
                    state=merged_state,
                    cumulative_filled=max(existing.cumulative_filled, incoming_cum),
                    last_event_time_ms=max(
                        existing.last_event_time_ms, order.update_time_ms or snapshot_time_ms
                    ),
                )

        for algo in algos:
            existing_algo = self._algos.get(algo.algo_id)
            if existing_algo is None:
                self._algos[algo.algo_id] = ReducedAlgoState(
                    symbol=algo.symbol,
                    algo_id=algo.algo_id,
                    client_algo_id=algo.client_algo_id,
                    status=algo.status,
                    actual_order_id=algo.actual_order_id,
                    actual_quantity=algo.actual_quantity,
                    last_event_time_ms=algo.update_time_ms or snapshot_time_ms,
                )
            else:
                next_status = (
                    existing_algo.status
                    if existing_algo.status in _TERMINAL_ALGO_STATES
                    else algo.status
                )
                self._algos[algo.algo_id] = replace(
                    existing_algo,
                    status=next_status,
                    actual_order_id=algo.actual_order_id or existing_algo.actual_order_id,
                    actual_quantity=(
                        algo.actual_quantity
                        if algo.actual_quantity is not None
                        else existing_algo.actual_quantity
                    ),
                    last_event_time_ms=max(
                        existing_algo.last_event_time_ms, algo.update_time_ms or snapshot_time_ms
                    ),
                )

        for pos in positions:
            key = (pos.symbol, pos.position_side)
            existing_pos = self._positions.get(key)
            if existing_pos is None or pos.last_event_time_ms >= existing_pos.last_event_time_ms:
                self._positions[key] = pos

    def _apply_order_event(self, envelope: CanonicalVenueEventEnvelope) -> None:
        assert envelope.order_id is not None
        assert envelope.symbol is not None
        incoming_state = VenueOrderState(str(envelope.facts["canonical_state"]))
        incoming_cum = Decimal(str(envelope.facts["executed_qty"]))
        last_fill = Decimal(str(envelope.facts["last_filled_qty"]))

        existing = self._orders.get(envelope.order_id)
        if existing is None:
            seen_trades: tuple[str, ...] = ()
            if envelope.trade_id is not None and envelope.trade_id not in self._seen_trade_ids:
                self._seen_trade_ids.add(envelope.trade_id)
                seen_trades = (envelope.trade_id,)
            self._orders[envelope.order_id] = ReducedOrderState(
                symbol=envelope.symbol,
                order_id=envelope.order_id,
                client_order_id=envelope.client_order_id or "",
                state=incoming_state,
                cumulative_filled=max(incoming_cum, last_fill),
                seen_trade_ids=seen_trades,
                last_event_time_ms=envelope.event_time_ms,
            )
            return

        # Không bao giờ dùng event_time đơn thuần để bỏ qua một trade_id riêng biệt chưa thấy
        new_trades = existing.seen_trade_ids
        updated_cum = max(existing.cumulative_filled, incoming_cum)
        if envelope.trade_id is not None and envelope.trade_id not in self._seen_trade_ids:
            self._seen_trade_ids.add(envelope.trade_id)
            new_trades = (*existing.seen_trade_ids, envelope.trade_id)
            # Nếu sự kiện trade đến trễ (incoming_cum nhỏ hơn nhưng trade_id mới),
            # vẫn bảo đảm cumulative không bao giờ giảm và phản ánh fill mới nếu vượt.
            updated_cum = max(updated_cum, existing.cumulative_filled + last_fill)

        merged_state = _merge_order_state(existing.state, incoming_state)
        self._orders[envelope.order_id] = replace(
            existing,
            state=merged_state,
            cumulative_filled=updated_cum,
            seen_trade_ids=new_trades,
            last_event_time_ms=max(existing.last_event_time_ms, envelope.event_time_ms),
        )

    def _apply_algo_event(self, envelope: CanonicalVenueEventEnvelope) -> None:
        assert envelope.algo_id is not None
        assert envelope.symbol is not None
        incoming_status = AlgoOrderStatus(str(envelope.facts["algo_status"]))
        actual_oid = envelope.facts.get("actual_order_id")
        actual_qty_raw = envelope.facts.get("actual_quantity")
        actual_qty = Decimal(str(actual_qty_raw)) if actual_qty_raw is not None else None

        existing = self._algos.get(envelope.algo_id)
        if existing is None:
            self._algos[envelope.algo_id] = ReducedAlgoState(
                symbol=envelope.symbol,
                algo_id=envelope.algo_id,
                client_algo_id=envelope.client_order_id or "",
                status=incoming_status,
                actual_order_id=str(actual_oid) if actual_oid is not None else None,
                actual_quantity=actual_qty,
                last_event_time_ms=envelope.event_time_ms,
            )
            return

        next_status = (
            existing.status if existing.status in _TERMINAL_ALGO_STATES else incoming_status
        )
        self._algos[envelope.algo_id] = replace(
            existing,
            status=next_status,
            actual_order_id=(
                str(actual_oid) if actual_oid is not None else existing.actual_order_id
            ),
            actual_quantity=actual_qty if actual_qty is not None else existing.actual_quantity,
            last_event_time_ms=max(existing.last_event_time_ms, envelope.event_time_ms),
        )

    def _apply_account_event(self, envelope: CanonicalVenueEventEnvelope) -> None:
        for p in envelope.facts.get("positions", []):
            key = (str(p["symbol"]), str(p["position_side"]))
            existing = self._positions.get(key)
            if existing is not None and envelope.event_time_ms < existing.last_event_time_ms:
                continue
            self._positions[key] = ReducedPositionState(
                symbol=str(p["symbol"]),
                position_side=str(p["position_side"]),
                position_amount=Decimal(str(p["position_amount"])),
                entry_price=Decimal(str(p["entry_price"])),
                unrealized_profit=Decimal(str(p["unrealized_profit"])),
                last_event_time_ms=envelope.event_time_ms,
            )


def _merge_order_state(current: VenueOrderState, incoming: VenueOrderState) -> VenueOrderState:
    """Không bao giờ lùi trạng thái kết thúc hoặc giảm bậc trạng thái."""
    if current in _TERMINAL_ORDER_STATES:
        return current
    if _STATE_RANK.get(incoming, 0) >= _STATE_RANK.get(current, 0):
        return incoming
    return current


def _unknown_envelope(
    workspace_id: str,
    account_id: str,
    received_at: datetime,
    raw: dict[str, object],
    reason: str,
) -> CanonicalVenueEventEnvelope:
    ev_time = _safe_int(raw.get("E"))
    ev_name = str(raw.get("e", "unknown"))
    return CanonicalVenueEventEnvelope(
        event_type=VenueEventType.UNKNOWN,
        workspace_id=workspace_id,
        account_id=account_id,
        event_time_ms=ev_time,
        transaction_time_ms=None,
        received_at=received_at,
        event_id=f"unknown:{ev_name}:{ev_time}:{reason}",
        facts={"raw_event_name": ev_name, "reason": reason},
    )


def _safe_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0
