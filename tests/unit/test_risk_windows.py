"""Unit test cho ngữ nghĩa cửa sổ lỗ theo ngày và cửa sổ trượt (WP-02 Risk Windows)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trading_bot.domain.risk.risk_windows import (
    LossWindowPolicy,
    RealizedTradePnlEvent,
    calculate_loss_windows,
)


def test_daily_loss_window_resets_at_pinned_timezone_hour() -> None:
    """Lỗ ngày phải reset đúng mốc giờ theo múi giờ đã ghim (Asia/Ho_Chi_Minh)."""
    vn_tz = ZoneInfo("Asia/Ho_Chi_Minh")
    policy = LossWindowPolicy(
        timezone_name="Asia/Ho_Chi_Minh",
        daily_reset_hour=7,
        rolling_window=timedelta(hours=24),
    )
    # 06:30 VN ngày 14/11 vẫn thuộc phiên ngày 13/11 (sau 07:00 13/11)
    # 07:30 VN ngày 14/11 là phiên mới ngày 14/11
    events = [
        RealizedTradePnlEvent(
            trade_id="t-old-day",
            closed_at=datetime(2026, 11, 13, 6, 59, tzinfo=vn_tz),
            net_pnl=Decimal("-200"),
        ),
        RealizedTradePnlEvent(
            trade_id="t-prev-session",
            closed_at=datetime(2026, 11, 13, 20, 0, tzinfo=vn_tz),
            net_pnl=Decimal("-150"),
        ),
        RealizedTradePnlEvent(
            trade_id="t-new-session",
            closed_at=datetime(2026, 11, 14, 8, 0, tzinfo=vn_tz),
            net_pnl=Decimal("-50"),
        ),
    ]

    # Kiểm tra lúc 06:30 VN ngày 14/11: lỗ ngày chỉ tính t-prev-session (-150),
    # còn rolling 24h (từ 06:30 ngày 13/11) vẫn gồm cả t-old-day (06:59, -200) -> 350
    snap_before_reset = calculate_loss_windows(
        events,
        as_of=datetime(2026, 11, 14, 6, 30, tzinfo=vn_tz),
        policy=policy,
    )
    assert snap_before_reset.realized_loss_today == Decimal("150")
    assert snap_before_reset.realized_loss_rolling == Decimal("350")

    # Kiểm tra lúc 09:00 VN ngày 14/11: phiên ngày chỉ tính t-new-session (-50),
    # còn rolling 24h vẫn gồm cả t-prev-session (-150) và t-new-session (-50) = 200
    snap_after_reset = calculate_loss_windows(
        events,
        as_of=datetime(2026, 11, 14, 9, 0, tzinfo=vn_tz),
        policy=policy,
    )
    assert snap_after_reset.realized_loss_today == Decimal("50")
    assert snap_after_reset.realized_loss_rolling == Decimal("200")


def test_loss_window_ignores_future_events_and_clamps_profit_to_zero_loss() -> None:
    """Không tính lệnh đóng ở tương lai (anti-lookahead) và lãi ròng cho loss = 0."""
    as_of = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    events = [
        RealizedTradePnlEvent("t-win", as_of - timedelta(hours=2), Decimal("300")),
        RealizedTradePnlEvent("t-loss", as_of - timedelta(hours=1), Decimal("-100")),
        RealizedTradePnlEvent("t-future", as_of + timedelta(minutes=1), Decimal("-999")),
    ]
    snap = calculate_loss_windows(events, as_of=as_of, policy=LossWindowPolicy())
    # Net PnL = +200 -> không có lỗ ròng trong ngày
    assert snap.realized_loss_today == Decimal("0")
    assert snap.realized_loss_rolling == Decimal("0")
    assert snap.gross_loss_today == Decimal("100")


def test_loss_window_can_include_unrealized_loss_and_rejects_naive_datetime() -> None:
    """Hỗ trợ cộng lỗ chưa thực hiện (MTM) và từ chối datetime thiếu timezone."""
    as_of = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    policy = LossWindowPolicy(include_unrealized_loss=True)
    events = [RealizedTradePnlEvent("t1", as_of - timedelta(hours=1), Decimal("-80"))]

    snap = calculate_loss_windows(events, as_of=as_of, policy=policy, unrealized_pnl=Decimal("-45"))
    assert snap.realized_loss_today == Decimal("125")
    assert snap.realized_loss_rolling == Decimal("125")

    with pytest.raises(ValueError, match="timezone"):
        calculate_loss_windows(
            events,
            as_of=datetime(2026, 11, 14, 10, 0),  # noqa: DTZ001
            policy=policy,
        )
