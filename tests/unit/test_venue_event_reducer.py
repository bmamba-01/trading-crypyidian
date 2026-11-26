"""Unit tests cho bộ chuẩn hóa & giảm trạng thái sự kiện venue (WP-03C.1 / CRY-292)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.adapters.binance_usdm.order_schemas import (
    AlgoOrderStatus,
    RegularOrder,
    RegularOrderStatus,
    RegularOrderType,
)
from trading_bot.domain.execution.venue_event_reducer import (
    ReducedPositionState,
    VenueEventReducer,
    VenueEventType,
    normalize_user_stream_payload,
)
from trading_bot.domain.execution.venue_orders import VenueOrderState


def _order_trade_payload(
    *,
    event_time_ms: int,
    tx_time_ms: int,
    order_id: int,
    status: str,
    exec_type: str,
    trade_id: int,
    last_qty: str,
    cum_qty: str,
    orig_qty: str = "1.0",
) -> dict[str, object]:
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": event_time_ms,
        "T": tx_time_ms,
        "o": {
            "s": "BTCUSDT",
            "c": "cid-100",
            "o": "MARKET",
            "ot": "MARKET",
            "x": exec_type,
            "X": status,
            "i": order_id,
            "l": last_qty,
            "z": cum_qty,
            "L": "65000",
            "q": orig_qty,
            "t": trade_id,
            "T": tx_time_ms,
        },
    }


def test_reducer_deduplicates_handles_out_of_order_and_preserves_terminal_state() -> None:
    """Sự kiện lặp không nhân đôi fill; sự kiện trễ không làm lùi terminal state."""
    reducer = VenueEventReducer()
    ts = datetime(2026, 11, 26, 6, 10, tzinfo=UTC)

    # 1. Nhận sự kiện FILLED (cum=1.0, trade_id=202) trước (out-of-order)
    ev_filled = normalize_user_stream_payload(
        _order_trade_payload(
            event_time_ms=2000,
            tx_time_ms=2000,
            order_id=1001,
            status="FILLED",
            exec_type="TRADE",
            trade_id=202,
            last_qty="0.6",
            cum_qty="1.0",
        ),
        workspace_id="ws-1",
        account_id="acc-1",
        received_at=ts,
    )
    assert reducer.apply(ev_filled) is True
    # Replay trùng lặp -> bỏ qua, không nhân đôi fill
    assert reducer.apply(ev_filled) is False
    assert reducer.orders["1001"].state is VenueOrderState.FILLED
    assert reducer.orders["1001"].cumulative_filled == Decimal("1.0")

    # 2. Sự kiện PARTIALLY_FILLED đến trễ (event_time_ms=1000 < 2000, trade_id=201 riêng biệt)
    ev_late_partial = normalize_user_stream_payload(
        _order_trade_payload(
            event_time_ms=1000,
            tx_time_ms=1000,
            order_id=1001,
            status="PARTIALLY_FILLED",
            exec_type="TRADE",
            trade_id=201,
            last_qty="0.4",
            cum_qty="0.4",
        ),
        workspace_id="ws-1",
        account_id="acc-1",
        received_at=ts,
    )
    assert reducer.apply(ev_late_partial) is True
    # State không bị lùi về PARTIALLY_FILLED; trade_id=201 vẫn được ghi nhận (không bỏ qua theo E)
    assert reducer.orders["1001"].state is VenueOrderState.FILLED
    assert "201" in reducer.orders["1001"].seen_trade_ids
    assert "202" in reducer.orders["1001"].seen_trade_ids


def test_reducer_algo_unknown_and_disconnect_gap_rest_snapshot_convergence() -> None:
    """ALGO_UPDATE, sự kiện lạ (UNKNOWN), và replay + REST snapshot hội tụ idempotent."""
    reducer = VenueEventReducer()
    ts = datetime(2026, 11, 26, 6, 15, tzinfo=UTC)

    # 1. ALGO_UPDATE TRIGGERING -> REJECTED không lùi lại
    ev_trig = normalize_user_stream_payload(
        {
            "e": "ALGO_UPDATE",
            "E": 3000,
            "T": 3000,
            "o": {
                "s": "BTCUSDT",
                "aid": 8001,
                "caid": "caid-8001",
                "at": "CONDITIONAL",
                "o": "STOP_MARKET",
                "X": "TRIGGERING",
                "tp": "61000",
            },
        },
        workspace_id="ws-1",
        account_id="acc-1",
        received_at=ts,
    )
    ev_rej = normalize_user_stream_payload(
        {
            "e": "ALGO_UPDATE",
            "E": 3100,
            "T": 3100,
            "o": {
                "s": "BTCUSDT",
                "aid": 8001,
                "caid": "caid-8001",
                "at": "CONDITIONAL",
                "o": "STOP_MARKET",
                "X": "REJECTED",
                "tp": "61000",
            },
        },
        workspace_id="ws-1",
        account_id="acc-1",
        received_at=ts,
    )
    reducer.apply(ev_trig)
    reducer.apply(ev_rej)
    assert reducer.algos[8001].status is AlgoOrderStatus.REJECTED

    # 2. Sự kiện lạ fail-closed thành UNKNOWN
    ev_unknown = normalize_user_stream_payload(
        {"e": "FUTURE_NEW_EVENT", "E": 3200},
        workspace_id="ws-1",
        account_id="acc-1",
        received_at=ts,
    )
    assert ev_unknown.event_type is VenueEventType.UNKNOWN

    # 3. Mô phỏng disconnect/gap: stream chỉ thấy 0.4, REST snapshot thấy 1.0 FILLED
    ev_before_gap = normalize_user_stream_payload(
        _order_trade_payload(
            event_time_ms=4000,
            tx_time_ms=4000,
            order_id=2002,
            status="PARTIALLY_FILLED",
            exec_type="TRADE",
            trade_id=501,
            last_qty="0.4",
            cum_qty="0.4",
        ),
        workspace_id="ws-1",
        account_id="acc-1",
        received_at=ts,
    )
    reducer.apply(ev_before_gap)
    rest_order = RegularOrder(
        symbol="BTCUSDT",
        order_id=2002,
        client_order_id="cid-100",
        order_type=RegularOrderType.MARKET,
        status=RegularOrderStatus.FILLED,
        raw_status="FILLED",
        executed_quantity=Decimal("1.0"),
        original_quantity=Decimal("1.0"),
        update_time_ms=5000,
    )
    rest_pos = ReducedPositionState(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amount=Decimal("1.0"),
        entry_price=Decimal("65000"),
        unrealized_profit=Decimal("12.5"),
        last_event_time_ms=5000,
    )
    reducer.apply_rest_snapshot(orders=(rest_order,), positions=(rest_pos,), snapshot_time_ms=5000)
    # Áp dụng lại snapshot lần 2 và replay sự kiện cũ -> hoàn toàn idempotent
    reducer.apply_rest_snapshot(orders=(rest_order,), positions=(rest_pos,), snapshot_time_ms=5000)
    reducer.apply(ev_before_gap)
    assert reducer.orders["2002"].state is VenueOrderState.FILLED
    assert reducer.orders["2002"].cumulative_filled == Decimal("1.0")
    assert reducer.positions[("BTCUSDT", "BOTH")].position_amount == Decimal("1.0")
