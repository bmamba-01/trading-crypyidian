"""Ngữ nghĩa cửa sổ lỗ theo ngày giao dịch (pinned timezone) và cửa sổ trượt (rolling window)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class LossWindowPolicy:
    """Chính sách xác định ranh giới thời gian reset lỗ ngày và cửa sổ trượt."""

    timezone_name: str = "UTC"
    daily_reset_hour: int = 0
    rolling_window: timedelta = timedelta(hours=24)
    include_unrealized_loss: bool = False

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Múi giờ không hợp lệ: {self.timezone_name}") from exc
        if not 0 <= self.daily_reset_hour <= 23:
            raise ValueError("daily_reset_hour phải nằm trong khoảng 0..23.")
        if self.rolling_window <= timedelta(0):
            raise ValueError("rolling_window phải lớn hơn 0.")


# Quyết định của chủ sở hữu (2026-11-14): giới hạn lỗ ngày reset lúc 00:00 UTC, trùng ngày
# giao dịch của sàn; lỗ lũy kế trượt 24 giờ. Mọi cổng rủi ro (runtime, replay, health) dùng
# đúng chính sách này. Múi giờ Asia/Ho_Chi_Minh chỉ dùng để hiển thị.
RISK_LOSS_WINDOW_POLICY = LossWindowPolicy()


@dataclass(frozen=True, slots=True)
class RealizedTradePnlEvent:
    """Sự kiện lời/lỗ đã chốt của một giao dịch tại thời điểm đóng."""

    trade_id: str
    closed_at: datetime
    net_pnl: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.closed_at, "closed_at")
        if not isinstance(self.net_pnl, Decimal) or not self.net_pnl.is_finite():
            raise ValueError("net_pnl phải là Decimal hữu hạn.")


@dataclass(frozen=True, slots=True)
class LossWindowSnapshot:
    """Ảnh chụp mức lỗ đã ghi nhận trong cửa sổ ngày và cửa sổ trượt tại mốc as_of."""

    as_of: datetime
    daily_window_start: datetime
    daily_window_end: datetime
    rolling_window_start: datetime
    realized_loss_today: Decimal
    realized_loss_rolling: Decimal
    gross_loss_today: Decimal
    gross_loss_rolling: Decimal
    trade_count_today: int
    trade_count_rolling: int
    timezone_name: str


def calculate_loss_windows(
    events: Iterable[RealizedTradePnlEvent],
    *,
    as_of: datetime,
    policy: LossWindowPolicy | None = None,
    unrealized_pnl: Decimal = ZERO,
) -> LossWindowSnapshot:
    """Tính toán lỗ ngày và lỗ cửa sổ trượt tại thời điểm as_of, chống lookahead."""
    _require_aware(as_of, "as_of")
    if not isinstance(unrealized_pnl, Decimal) or not unrealized_pnl.is_finite():
        raise ValueError("unrealized_pnl phải là Decimal hữu hạn.")

    active_policy = policy or LossWindowPolicy()
    tz = ZoneInfo(active_policy.timezone_name)
    as_of_utc = as_of.astimezone(UTC)
    as_of_local = as_of_utc.astimezone(tz)

    daily_start_local = as_of_local.replace(
        hour=active_policy.daily_reset_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if as_of_local < daily_start_local:
        daily_start_local -= timedelta(days=1)

    daily_start_utc = daily_start_local.astimezone(UTC)
    daily_end_utc = (daily_start_local + timedelta(days=1)).astimezone(UTC)
    rolling_start_utc = as_of_utc - active_policy.rolling_window

    net_today = ZERO
    gross_loss_today = ZERO
    count_today = 0

    net_rolling = ZERO
    gross_loss_rolling = ZERO
    count_rolling = 0

    for event in events:
        event_utc = event.closed_at.astimezone(UTC)
        # Bỏ qua mọi sự kiện xảy ra sau as_of để ngăn chặn rò rỉ dữ liệu tương lai.
        if event_utc > as_of_utc:
            continue
        if daily_start_utc <= event_utc <= as_of_utc:
            net_today += event.net_pnl
            count_today += 1
            if event.net_pnl < ZERO:
                gross_loss_today += abs(event.net_pnl)
        if rolling_start_utc <= event_utc <= as_of_utc:
            net_rolling += event.net_pnl
            count_rolling += 1
            if event.net_pnl < ZERO:
                gross_loss_rolling += abs(event.net_pnl)

    loss_today = max(ZERO, -net_today)
    loss_rolling = max(ZERO, -net_rolling)

    if active_policy.include_unrealized_loss and unrealized_pnl < ZERO:
        unrealized_loss = abs(unrealized_pnl)
        loss_today += unrealized_loss
        loss_rolling += unrealized_loss

    return LossWindowSnapshot(
        as_of=as_of_utc,
        daily_window_start=daily_start_utc,
        daily_window_end=daily_end_utc,
        rolling_window_start=rolling_start_utc,
        realized_loss_today=loss_today,
        realized_loss_rolling=loss_rolling,
        gross_loss_today=gross_loss_today,
        gross_loss_rolling=gross_loss_rolling,
        trade_count_today=count_today,
        trade_count_rolling=count_rolling,
        timezone_name=active_policy.timezone_name,
    )


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} bắt buộc phải có timezone-aware.")
