"""TG-3: view model và bộ định dạng văn bản cho kết quả trade PAPER / tin tổng hợp."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.telegram_trade_results import (
    PAPER_RESULT_HEADER,
    TELEGRAM_TEXT_LIMIT,
    BotSummaryRow,
    ClosedTradeFact,
    TradeFillFact,
    TradeNotificationKind,
    TradeResultSource,
    build_bot_summary_row,
    build_paper_report_view,
    build_trade_result_view,
    format_duration,
    format_paper_report_text,
    format_signed,
    format_trade_result_text,
    r_multiple,
    realized_max_drawdown,
    report_window,
)

BOT = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
TRADE = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
OPENED = datetime(2026, 11, 15, 6, 0, tzinfo=UTC)
CLOSED = OPENED + timedelta(hours=2, minutes=5)


def _source(kind: TradeNotificationKind = TradeNotificationKind.CLOSED, **overrides: object):
    values: dict[str, object] = {
        "kind": kind,
        "bot_id": BOT,
        "bot_name": "H01 BTC",
        "trade_id": TRADE,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quote_asset": "USDT",
        "status": "CLOSED" if kind is TradeNotificationKind.CLOSED else "OPEN",
        "opened_at": OPENED,
        "closed_at": CLOSED if kind is TradeNotificationKind.CLOSED else None,
        "net_pnl": Decimal("-10.5"),
        "fills": (
            TradeFillFact("ENTRY", Decimal("0.01"), Decimal("68000"), OPENED),
            TradeFillFact("ENTRY", Decimal("0.01"), Decimal("68200"), OPENED),
            TradeFillFact("EXIT", Decimal("0.02"), Decimal("67500"), CLOSED),
        ),
        "reserved_risk": Decimal("10"),
        "exit_reason": "STOP_LOSS",
    }
    values.update(overrides)
    return TradeResultSource(**values)  # type: ignore[arg-type]


def test_closed_view_has_weighted_prices_reason_r_and_hold_time() -> None:
    view = build_trade_result_view(_source())

    assert view.entry_price == Decimal("68100")
    assert view.entry_quantity == Decimal("0.02")
    assert view.exit_price == Decimal("67500")
    assert view.exit_reason_code == "STOP_LOSS"
    assert view.exit_reason_vi == "Chạm dừng lỗ (SL)"
    assert view.r_multiple == Decimal("-1.05")
    assert view.hold_duration == timedelta(hours=2, minutes=5)
    payload = view.as_dict()
    assert payload["kind"] == "CLOSED"
    assert payload["hold_seconds"] == str(2 * 3600 + 300)
    assert payload["net_pnl"] == "-10.5"


def test_closed_text_is_plain_vietnamese_with_header_and_signed_pnl() -> None:
    text = format_trade_result_text(build_trade_result_view(_source()))

    assert text.splitlines()[0] == PAPER_RESULT_HEADER
    assert "Bot: H01 BTC" in text
    assert "Đóng lệnh PAPER: BTCUSDT · LONG" in text
    assert "Giá vào: 68100 · Khối lượng: 0.02" in text
    assert "Giá ra: 67500" in text
    assert "Lý do thoát: Chạm dừng lỗ (SL) (STOP_LOSS)" in text
    assert "PnL ròng (sau phí): -10.5 USDT" in text
    assert "Bội số R: -1.05R" in text
    assert "Thời gian giữ lệnh: 2 giờ 5 phút" in text
    # 06:00 UTC → 13:00 giờ VN.
    assert "15/11/2026 15:05 (giờ VN)" in text
    assert "<" not in text
    assert "*" not in text
    assert len(text) <= TELEGRAM_TEXT_LIMIT


def test_opened_view_and_text_show_entry_and_risk_only() -> None:
    view = build_trade_result_view(
        _source(
            TradeNotificationKind.OPENED,
            side="short",
            fills=(TradeFillFact("ENTRY", Decimal("1.5"), Decimal("3.25"), OPENED),),
        )
    )
    text = format_trade_result_text(view)

    assert view.direction == "SHORT"
    assert view.exit_price is None
    assert view.net_pnl is None
    assert view.r_multiple is None
    assert "Mở vị thế PAPER: BTCUSDT · SHORT" in text
    assert "Giá vào: 3.25 · Khối lượng: 1.5" in text
    assert "Thời điểm vào: 15/11/2026 13:00 (giờ VN)" in text
    assert "Rủi ro dự kiến (1R): 10 USDT" in text
    assert "PnL" not in text


def test_positive_pnl_has_plus_sign_and_missing_risk_says_not_computable() -> None:
    text = format_trade_result_text(
        build_trade_result_view(
            _source(net_pnl=Decimal("25.126"), reserved_risk=None, exit_reason="UNKNOWN_CODE")
        )
    )

    assert "PnL ròng (sau phí): +25.13 USDT" in text
    assert "Bội số R: không tính được" in text
    assert "Lý do thoát: UNKNOWN_CODE" in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"fills": (TradeFillFact("EXIT", Decimal(1), Decimal(1), CLOSED),)},
        {"status": "OPEN"},
        {"closed_at": None},
        {"side": "BUY"},
    ],
)
def test_view_rejects_incomplete_or_invalid_ledger_facts(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="PAPER"):
        build_trade_result_view(_source(**overrides))


def test_r_multiple_requires_positive_finite_risk() -> None:
    assert r_multiple(Decimal("5"), Decimal("2")) == Decimal("2.50")
    assert r_multiple(Decimal("5"), Decimal("0")) is None
    assert r_multiple(Decimal("5"), Decimal("NaN")) is None
    assert r_multiple(Decimal("5"), None) is None


def test_formatters_for_numbers_and_durations() -> None:
    assert format_signed(Decimal("0")) == "0"
    assert format_signed(Decimal("-0.001")) == "0"
    assert format_signed(Decimal("1.2")) == "+1.2"
    assert format_duration(timedelta(seconds=45)) == "45 giây"
    assert format_duration(timedelta(minutes=7)) == "7 phút"
    assert format_duration(timedelta(days=2, hours=3, minutes=9)) == "2 ngày 3 giờ"


def _fact(index: int, pnl: str) -> ClosedTradeFact:
    return ClosedTradeFact(
        UUID(int=index), Decimal(pnl), datetime(2026, 11, 1, tzinfo=UTC) + timedelta(hours=index)
    )


def test_realized_drawdown_follows_close_order_from_zero_peak() -> None:
    trades = [_fact(3, "-5"), _fact(1, "10"), _fact(2, "-8"), _fact(4, "4")]

    # Tích lũy 10 → 2 → -3 → 1; đỉnh 10, đáy -3 → sụt 13.
    assert realized_max_drawdown(trades) == Decimal("13")
    assert realized_max_drawdown([]) == Decimal("0")


def test_summary_row_counts_window_and_total_and_win_rate() -> None:
    window_start = datetime(2026, 11, 1, 2, tzinfo=UTC)
    window_end = datetime(2026, 11, 1, 6, tzinfo=UTC)
    row = build_bot_summary_row(
        bot_id=BOT,
        bot_name="A",
        closed_trades=[_fact(1, "10"), _fact(2, "-8"), _fact(3, "-5"), _fact(5, "4")],
        open_positions=2,
        window_start=window_start,
        window_end=window_end,
    )

    assert (row.window_closed_count, row.window_net_pnl) == (3, Decimal("-9"))
    assert (row.total_closed_count, row.total_winning_count) == (4, 2)
    assert row.total_net_pnl == Decimal("1")
    assert row.win_rate_percent == Decimal("50.0")
    assert row.open_positions == 2
    assert row.as_dict()["win_rate_percent"] == "50.0"


def _row(name: str, pnl: str, bot: int) -> BotSummaryRow:
    return BotSummaryRow(
        bot_id=UUID(int=bot),
        bot_name=name,
        quote_asset="USDT",
        window_closed_count=1,
        window_net_pnl=Decimal(pnl),
        total_closed_count=0 if pnl == "0" else 2,
        total_winning_count=1,
        total_net_pnl=Decimal(pnl),
        open_positions=0,
        max_drawdown=Decimal("1.5"),
    )


def test_report_view_sorts_by_total_net_pnl_and_text_lists_each_bot() -> None:
    start = datetime(2026, 11, 15, 0, tzinfo=UTC)
    view = build_paper_report_view(
        window_start=start,
        window_end=start + timedelta(hours=4),
        rows=[_row("Thấp", "-3", 1), _row("Cao", "12", 2), _row("Giữa", "0", 3)],
    )
    text = format_paper_report_text(view)

    assert [row.bot_name for row in view.rows] == ["Cao", "Giữa", "Thấp"]
    assert text.splitlines()[0] == "TỔNG HỢP KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT"
    assert text.index("1. Cao") < text.index("2. Giữa") < text.index("3. Thấp")
    assert "Tổng: 0 lệnh đóng · thắng - ·" in text
    assert "PnL ròng +12 USDT" in text
    assert "Sụt giảm tối đa: 1.5 USDT" in text
    assert view.as_dict()["rows"][0]["bot_name"] == "Cao"


def test_report_text_truncates_bots_to_fit_telegram_limit() -> None:
    start = datetime(2026, 11, 15, 0, tzinfo=UTC)
    rows = [_row("B" * 100 + str(index), str(index), index) for index in range(80)]
    text = format_paper_report_text(
        build_paper_report_view(
            window_start=start, window_end=start + timedelta(hours=4), rows=rows
        )
    )

    assert len(text) <= TELEGRAM_TEXT_LIMIT
    assert "(Lược bớt" in text


def test_empty_report_has_explicit_message() -> None:
    start = datetime(2026, 11, 15, 0, tzinfo=UTC)
    text = format_paper_report_text(
        build_paper_report_view(window_start=start, window_end=start + timedelta(hours=1), rows=())
    )

    assert "Chưa có bot nào để tổng hợp." in text


def test_report_window_is_last_closed_epoch_aligned_interval() -> None:
    interval = timedelta(hours=4)
    now = datetime(2026, 11, 15, 9, 59, 59, tzinfo=UTC)

    assert report_window(now, interval) == (
        datetime(2026, 11, 15, 4, tzinfo=UTC),
        datetime(2026, 11, 15, 8, tzinfo=UTC),
    )
    # Mọi thời điểm trong cùng cửa sổ (kể cả sau khởi động lại) cho cùng khóa.
    assert report_window(now - timedelta(hours=1, minutes=59), interval) == report_window(
        now, interval
    )
    assert report_window(datetime(2026, 11, 15, 12, tzinfo=UTC), interval)[0] == datetime(
        2026, 11, 15, 8, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="múi giờ"):
        report_window(datetime(2026, 11, 15), interval)  # noqa: DTZ001
    with pytest.raises(ValueError, match="tối thiểu"):
        report_window(now, timedelta(seconds=30))
