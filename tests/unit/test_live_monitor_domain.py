"""Unit test cho mô hình hiển thị theo dõi bot trực tiếp (LIVE-MON, CRY-429)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.domain.monitoring.live_monitor import (
    LiveCursor,
    LiveEventKind,
    LiveSeverity,
    build_equity_view,
    build_feed_health,
    build_live_events,
    build_position_view,
    dedupe_events,
    measure_clock_skew_ms,
)

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
BOT = UUID("11111111-1111-4111-8111-111111111111")


def _row(source: str, **values: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": source,
        "id": f"{source}-1",
        "bot_id": BOT,
        "recorded_at": NOW - timedelta(seconds=5),
        "occurred_at": NOW - timedelta(seconds=6),
        "symbol": "BTCUSDT",
    }
    return base | values


def test_cursor_roundtrip_and_overlap() -> None:
    cursor = LiveCursor(NOW)
    assert LiveCursor.parse(cursor.encode()) == cursor
    assert cursor.query_floor() == NOW - timedelta(seconds=30)
    # "+" của múi giờ bị giải mã thành khoảng trắng khi query string không mã hóa.
    assert LiveCursor.parse(cursor.encode().replace("+", " ")) == cursor


@pytest.mark.parametrize("raw", ["", "abc", "2026-11-17T12:00:00", "x" * 200])
def test_cursor_rejects_invalid_or_naive(raw: str) -> None:
    assert LiveCursor.parse(raw) is None


def test_decision_abstain_is_muted_and_candidate_is_info() -> None:
    events = build_live_events(
        [
            _row(
                "decision",
                id="d1",
                status="ABSTAIN",
                reason_code="H03_NO_BREAKOUT",
                detail_vi="Chưa phá kênh",
                bar_close_at=datetime(2026, 11, 17, 11, 59, 59, 999000, tzinfo=UTC),
            ),
            _row(
                "decision", id="d2", status="CANDIDATE", reason_code="H03_LONG_ENTRY", side="LONG"
            ),
        ]
    )
    by_id = {event["id"]: event for event in events}
    assert by_id["decision:d1"]["kind"] == LiveEventKind.DECISION_ABSTAIN
    assert by_id["decision:d1"]["severity"] == LiveSeverity.MUTED
    assert "H03_NO_BREAKOUT" in by_id["decision:d1"]["detail_vi"]
    assert by_id["decision:d1"]["detail_vi"].startswith("Nến đóng 12:00 UTC. Chưa phá kênh")
    assert by_id["decision:d2"]["kind"] == LiveEventKind.DECISION_CANDIDATE
    assert by_id["decision:d2"]["severity"] == LiveSeverity.INFO


def test_close_by_stop_loss_is_alert_with_r_and_pnl() -> None:
    events = build_live_events(
        [
            _row(
                "position",
                id="p1",
                event_type="CLOSED",
                side="LONG",
                exit_reason="STOP_LOSS",
                net_pnl=Decimal("-10"),
                funding_total=Decimal("-0.5"),
                planned_risk=Decimal("10"),
                quote_asset="USDT",
            )
        ]
    )
    (event,) = events
    assert event["kind"] == LiveEventKind.POSITION_CLOSED
    assert event["severity"] == LiveSeverity.ALERT
    assert event["metrics"]["r_multiple"] == "-1.00"
    assert event["metrics"]["net_pnl"] == "-10"
    assert event["metrics"]["funding"] == "-0.5"
    assert "Dừng lỗ" in event["title_vi"]


def test_close_by_time_stop_is_notice_and_missing_risk_has_no_r() -> None:
    (event,) = build_live_events(
        [
            _row(
                "position",
                event_type="CLOSED",
                side="SHORT",
                exit_reason="TIME_STOP",
                net_pnl=Decimal("4"),
                planned_risk=None,
            )
        ]
    )
    assert event["severity"] == LiveSeverity.NOTICE
    assert event["metrics"]["r_multiple"] is None


def test_order_fill_stop_funding_reservation_and_expiry_kinds() -> None:
    events = build_live_events(
        [
            _row("reservation", id="r", reserved_risk=Decimal("12.5"), side="LONG"),
            _row("order", id="o", side="LONG", quantity=Decimal("0.1")),
            _row("fill", id="f1", fill_role="ENTRY", price=Decimal("100"), quantity=Decimal("1")),
            _row("fill", id="f2", fill_role="EXIT", price=Decimal("90"), quantity=Decimal("1")),
            _row("position", id="s", event_type="STOP_UPDATED", stop_price=Decimal("95")),
            _row("position", id="op", event_type="OPENED"),
            _row("funding", id="c", amount=Decimal("-0.12"), currency="USDT"),
            _row("expired", id="x"),
        ]
    )
    kinds = {event["id"]: event["kind"] for event in events}
    assert kinds == {
        "reservation:r": LiveEventKind.RESERVATION,
        "order:o": LiveEventKind.ORDER_ACCEPTED,
        "fill:f1": LiveEventKind.ENTRY_FILL,
        "fill:f2": LiveEventKind.EXIT_FILL,
        "position:s": LiveEventKind.STOP_MOVED,
        "position:op": LiveEventKind.POSITION_OPENED,
        "funding:c": LiveEventKind.FUNDING,
        "expired:x": LiveEventKind.ORDER_EXPIRED,
    }
    severity = {event["id"]: event["severity"] for event in events}
    assert severity["order:o"] == LiveSeverity.NOTICE
    assert severity["fill:f1"] == LiveSeverity.NOTICE
    stop = next(event for event in events if event["id"] == "position:s")
    assert stop["metrics"]["stop_price"] == "95"


def test_unknown_source_is_ignored_and_order_is_by_recorded_time() -> None:
    older = _row("order", id="old", recorded_at=NOW - timedelta(seconds=50))
    newer = _row("order", id="new", recorded_at=NOW - timedelta(seconds=1))
    events = build_live_events([newer, _row("mystery"), older])
    assert [event["id"] for event in events] == ["order:old", "order:new"]
    assert events[-1]["cursor"] == LiveCursor(newer["recorded_at"]).encode()  # type: ignore[arg-type]


def test_dedupe_events_is_bounded() -> None:
    seen: dict[str, None] = {}
    events = [{"id": f"e{i}"} for i in range(5)]
    assert len(dedupe_events(events, seen, limit=3)) == 5
    assert list(seen) == ["e2", "e3", "e4"]
    assert dedupe_events([{"id": "e4"}, {"id": "e9"}], seen, limit=3) == [{"id": "e9"}]


def _position(side: str = "LONG", **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "trade_id": "t1",
        "bot_id": BOT,
        "symbol": "BTCUSDT",
        "side": side,
        "quote_asset": "USDT",
        "opened_at": NOW - timedelta(minutes=90),
        "entry_fills": [
            {"price": Decimal("100"), "quantity": Decimal("1")},
            {"price": Decimal("110"), "quantity": Decimal("1")},
        ],
        "exit_quantity": Decimal("0"),
        "initial_stop": Decimal("95"),
        "current_stop": Decimal("100"),
        "target": Decimal("125"),
        "funding_total": Decimal("-0.2"),
        "fee_total": Decimal("0.1"),
        "mark": {"price": Decimal("115"), "at": NOW - timedelta(seconds=3), "source": "MARK"},
    }
    return values | overrides


def test_long_position_view_distances_r_and_hold_time() -> None:
    view = build_position_view(_position(), now=NOW)
    assert view["entry_price"] == "105"
    assert view["quantity"] == "2"
    assert view["unrealized_pnl"] == "20"
    assert view["r_multiple"] == "1.00"  # (115-105)/(105-95)
    assert view["stop_distance_pct"] == "13.04"  # (115-100)/115
    assert view["target_distance_pct"] == "8.70"  # (125-115)/115
    assert view["hold_seconds"] == 5400
    assert view["stop_moved"] is True
    assert view["mark_age_seconds"] == 3
    assert view["protected"] is True


def test_short_position_view_and_missing_mark() -> None:
    view = build_position_view(
        _position(
            "SHORT",
            entry_fills=[{"price": Decimal("100"), "quantity": Decimal("2")}],
            exit_quantity=Decimal("1"),
            initial_stop=Decimal("110"),
            current_stop=None,
            target=None,
            mark={"price": Decimal("90"), "at": NOW, "source": "BAR_CLOSE"},
        ),
        now=NOW,
    )
    assert view["quantity"] == "1"
    assert view["unrealized_pnl"] == "10"
    assert view["r_multiple"] == "1.00"
    assert view["stop_distance_pct"] == "22.22"  # (110-90)/90
    assert view["target_distance_pct"] is None
    assert view["stop_moved"] is False
    no_mark = build_position_view(
        _position(mark=None, initial_stop=None, current_stop=None), now=NOW
    )
    assert no_mark["unrealized_pnl"] is None
    assert no_mark["r_multiple"] is None
    assert no_mark["protected"] is False


def test_equity_view_peak_drawdown_and_curve() -> None:
    view = build_equity_view(
        seed_equity=Decimal("1000"),
        closed=[
            (NOW - timedelta(hours=3), Decimal("50")),
            (NOW - timedelta(hours=2), Decimal("-100")),
            (NOW - timedelta(hours=1), Decimal("20")),
        ],
        unrealized=Decimal("-10"),
        now=NOW,
    )
    assert view["equity"] == "960"
    assert view["peak"] == "1050"
    assert view["drawdown"] == "90"
    assert view["drawdown_pct"] == "8.57"
    assert view["realized_pnl"] == "-30"
    assert [point[1] for point in view["curve"]] == ["1000", "1050", "950", "970", "960"]


def test_equity_view_carries_history_before_window() -> None:
    view = build_equity_view(
        seed_equity=Decimal("1000"),
        closed=[(NOW - timedelta(hours=1), Decimal("10"))],
        unrealized=None,
        now=NOW,
        realized_before=Decimal("-50"),
        peak_realized_before=Decimal("200"),
    )
    assert view["equity"] == "960"
    assert view["realized_pnl"] == "-40"
    assert view["peak"] == "1200"
    assert view["drawdown_pct"] == "20.00"


def test_equity_view_without_seed_is_unknown() -> None:
    view = build_equity_view(seed_equity=None, closed=[], unrealized=None, now=NOW)
    assert view["equity"] is None
    assert view["drawdown_pct"] is None


def test_feed_health_statuses_and_breaker() -> None:
    health = build_feed_health(
        now=NOW,
        clock_skew_ms=-1500,
        latest_mark_at=NOW - timedelta(seconds=45),
        latest_bar_close_at=NOW - timedelta(seconds=150),
        outbox_oldest_pending_at=NOW - timedelta(seconds=5),
        outbox_pending=2,
        outbox_unconsumed_pending=9,
        daily_loss=Decimal("0"),
        drawdown_pct=Decimal("1"),
        api_error_count=0,
        unprotected_positions=1,
    )
    assert health["feed"]["status"] == "warn"
    assert health["feed"]["age_seconds"] == 150
    assert health["mark"]["age_seconds"] == 45
    assert health["clock"]["skew_ms"] == 1500
    assert health["clock"]["status"] == "stale"
    assert health["outbox"]["lag_seconds"] == 5
    assert health["outbox"]["status"] == "ok"
    assert health["outbox"]["unconsumed_pending"] == 9
    breaker = health["breaker"]
    assert breaker["state"] == "OPEN"
    # Nến 1m đóng mỗi 60 giây: breaker chỉ tính phần trễ vượt nhịp nến (150 - 60 = 90 > 30).
    assert set(breaker["reasons"]) == {"FEED_STALE", "CLOCK_SKEW", "MISSING_PROTECTION"}
    assert breaker["entry_allowed"] is False


def test_feed_health_fresh_bar_is_ok() -> None:
    health = build_feed_health(
        now=NOW,
        clock_skew_ms=0,
        latest_mark_at=None,
        latest_bar_close_at=NOW - timedelta(seconds=70),
        outbox_oldest_pending_at=NOW - timedelta(seconds=400),
        outbox_pending=1,
        outbox_unconsumed_pending=0,
        daily_loss=Decimal("0"),
        drawdown_pct=Decimal("0"),
        api_error_count=0,
        unprotected_positions=0,
    )
    assert health["feed"]["status"] == "ok"
    assert health["mark"]["status"] == "unknown"
    assert health["outbox"]["status"] == "stale"
    assert health["breaker"]["reasons"] == ["OUTBOX_QUEUE_LAG"]


def test_feed_health_without_data_is_unknown_and_breaker_closed() -> None:
    health = build_feed_health(
        now=NOW,
        clock_skew_ms=0,
        latest_mark_at=None,
        latest_bar_close_at=None,
        outbox_oldest_pending_at=None,
        outbox_pending=0,
        outbox_unconsumed_pending=0,
        daily_loss=Decimal("0"),
        drawdown_pct=None,
        api_error_count=0,
        unprotected_positions=0,
    )
    assert health["feed"]["status"] == "unknown"
    assert health["outbox"]["lag_seconds"] == 0
    assert health["breaker"]["state"] == "CLOSED"


def test_measure_clock_skew_uses_round_trip_midpoint() -> None:
    sent = NOW
    received = NOW + timedelta(seconds=4)  # truy vấn chậm 4 giây không được tính là lệch
    assert measure_clock_skew_ms(sent, NOW + timedelta(seconds=2), received) == 0
    assert (
        measure_clock_skew_ms(sent, NOW + timedelta(seconds=2, milliseconds=300), received) == 300
    )
