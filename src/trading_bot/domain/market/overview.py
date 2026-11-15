"""Tổng quan thị trường cho buồng lái biểu đồ (CK-1, CK-2).

Mọi hàm ở đây là hàm thuần trên nến **đã đóng** (và giá trực tiếp nếu có), không đọc đồng hồ
hệ thống: thời điểm hiện tại luôn được truyền vào. Xu hướng từng khung tái dùng máy trạng thái
BOS/CHoCH của SIG-1 (`structure.market_structure`) nên chart và chiến lược đọc cùng một định
nghĩa; ở đây chỉ tổng hợp thêm mức vô hiệu, biên dao động và mức đồng thuận giữa các khung.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from trading_bot.domain.market.sessions import (
    DEFAULT_SESSIONS,
    SessionDefinition,
    session_windows,
)
from trading_bot.domain.market.structure import StructureBreak, market_structure

OVERVIEW_FORMULA_VERSION = "ck1-overview-v1"
MTF_FORMULA_VERSION = "ck2-mtf-v1"
# Cần đủ nến để có ít nhất vài swing xác nhận (3 nến mỗi bên) trước khi kết luận xu hướng.
MIN_TREND_BARS = 20
_DAY = timedelta(days=1)

TrendState = Literal["up", "down", "range", "unknown"]
RangeZone = Literal["premium", "discount", "equilibrium"]
TREND_LABELS_VI: dict[str, str] = {
    "up": "TĂNG",
    "down": "GIẢM",
    "range": "Đi ngang (chưa phá cấu trúc)",
    "unknown": "Chưa đủ dữ liệu",
}


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None:
        raise ValueError("Thời điểm phải có timezone")


@dataclass(frozen=True, slots=True)
class CandleClock:
    """Nến đang hình thành theo đồng hồ UTC và số giây còn lại tới khi đóng."""

    open_at: datetime
    close_at: datetime
    seconds_left: int


def candle_clock(now: datetime, interval_seconds: int) -> CandleClock:
    """Biên nến Binance cho khung ≤ 1 ngày: neo theo mốc epoch UTC (ví dụ 4h: 00/04/08…)."""
    _require_aware(now)
    if interval_seconds <= 0 or 86_400 % interval_seconds != 0:
        raise ValueError("Chỉ hỗ trợ khung chia hết một ngày (≤ 1d)")
    epoch = int(now.astimezone(UTC).timestamp())
    opened = epoch - epoch % interval_seconds
    open_at = datetime.fromtimestamp(opened, UTC)
    close_at = open_at + timedelta(seconds=interval_seconds)
    return CandleClock(open_at, close_at, int((close_at - now).total_seconds()))


@dataclass(frozen=True, slots=True)
class ActiveSession:
    key: str
    label_vi: str
    open_at: datetime
    close_at: datetime


def active_sessions(
    now: datetime, sessions: Sequence[SessionDefinition] = DEFAULT_SESSIONS
) -> tuple[ActiveSession, ...]:
    """Các phiên đang mở tại ``now`` (giờ địa phương từng thị trường, tự theo DST)."""
    _require_aware(now)
    active: list[ActiveSession] = []
    for definition in sessions:
        for opened, closed in session_windows(definition, now, now + timedelta(seconds=1)):
            if opened <= now < closed:
                active.append(ActiveSession(definition.key, definition.label_vi, opened, closed))
    return tuple(active)


@dataclass(frozen=True, slots=True)
class DayRange:
    """Biên ngày UTC hiện tại và đỉnh/đáy ngày trước (PDH/PDL)."""

    day_start: datetime
    high: float | None
    low: float | None
    previous_high: float | None
    previous_low: float | None
    previous_complete: bool


def day_range(
    open_times: Sequence[datetime],
    highs: Sequence[float],
    lows: Sequence[float],
    interval: timedelta,
    now: datetime,
    *,
    live_price: float | None = None,
) -> DayRange:
    """Biên ngày UTC từ nến đã đóng (cộng giá trực tiếp nếu có).

    ``previous_complete`` chỉ đúng khi đủ số nến của cả ngày trước; thiếu nến thì PDH/PDL vẫn
    trả nhưng được đánh dấu để giao diện ghi chú, không coi là mức đầy đủ.
    """
    _require_aware(now)
    if not len(open_times) == len(highs) == len(lows):
        raise ValueError("Các chuỗi nến phải cùng độ dài")
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    previous_start = day_start - _DAY
    today_high: list[float] = []
    today_low: list[float] = []
    previous_high: list[float] = []
    previous_low: list[float] = []
    for opened, high, low in zip(open_times, highs, lows, strict=True):
        if day_start <= opened < day_start + _DAY:
            today_high.append(high)
            today_low.append(low)
        elif previous_start <= opened < day_start:
            previous_high.append(high)
            previous_low.append(low)
    if live_price is not None:
        today_high.append(live_price)
        today_low.append(live_price)
    expected = int(_DAY / interval) if interval <= _DAY else 1
    return DayRange(
        day_start=day_start,
        high=max(today_high) if today_high else None,
        low=min(today_low) if today_low else None,
        previous_high=max(previous_high) if previous_high else None,
        previous_low=min(previous_low) if previous_low else None,
        previous_complete=len(previous_high) >= expected,
    )


@dataclass(frozen=True, slots=True)
class LastBreak:
    kind: Literal["BOS", "CHOCH"]
    direction: Literal["bullish", "bearish"]
    level: float
    at: datetime


@dataclass(frozen=True, slots=True)
class TimeframeTrend:
    """Một ô của ma trận đa khung.

    ``invalidation_price``: tăng thì là đáy swing xác nhận gần nhất, giảm thì là đỉnh swing gần
    nhất — đóng cửa vượt mức này nghĩa là cấu trúc của khung đã đổi. ``position_pct`` là vị trí
    giá đóng gần nhất trong biên giữa đỉnh và đáy swing xác nhận gần nhất (có thể < 0 hoặc
    > 100 khi giá đã ra khỏi biên).
    """

    timeframe: str
    trend: TrendState
    bar_count: int
    last_close: float | None
    last_change_pct: float | None
    invalidation_price: float | None
    range_high: float | None
    range_low: float | None
    position_pct: float | None
    zone: RangeZone | None
    last_break: LastBreak | None
    as_of: datetime | None

    @property
    def label_vi(self) -> str:
        return TREND_LABELS_VI[self.trend]


def timeframe_trend(
    timeframe: str,
    open_times: Sequence[datetime],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    left_bars: int = 3,
    right_bars: int = 3,
    opens: Sequence[float] | None = None,
) -> TimeframeTrend:
    """Xu hướng của một khung từ nến đã đóng; thiếu nến thì trả ``unknown``, không đoán."""
    count = len(closes)
    if not len(open_times) == len(highs) == len(lows) == count:
        raise ValueError("Các chuỗi nến phải cùng độ dài")
    last_close = closes[-1] if closes else None
    change: float | None = None
    if opens is not None and count and opens[-1]:
        change = (closes[-1] - opens[-1]) / opens[-1] * 100
    as_of = open_times[-1] if open_times else None
    if count < MIN_TREND_BARS:
        return TimeframeTrend(
            timeframe,
            "unknown",
            count,
            last_close,
            change,
            None,
            None,
            None,
            None,
            None,
            None,
            as_of,
        )
    structure = market_structure(highs, lows, closes, left_bars=left_bars, right_bars=right_bars)
    last_index = count - 1
    known = [swing for swing in structure.swings if swing.confirmed_index <= last_index]
    latest_high = next((swing for swing in reversed(known) if swing.kind == "high"), None)
    latest_low = next((swing for swing in reversed(known) if swing.kind == "low"), None)
    current = structure.trend[-1]
    trend: TrendState = current if current is not None else "range"
    invalidation: float | None = None
    if trend == "up" and latest_low is not None:
        invalidation = latest_low.price
    elif trend == "down" and latest_high is not None:
        invalidation = latest_high.price
    range_high = latest_high.price if latest_high is not None else None
    range_low = latest_low.price if latest_low is not None else None
    position: float | None = None
    zone: RangeZone | None = None
    if range_high is not None and range_low is not None and range_high > range_low:
        assert last_close is not None
        position = (last_close - range_low) / (range_high - range_low) * 100
        zone = "premium" if position > 50 else "discount" if position < 50 else "equilibrium"
    last_break = _last_break(structure.breaks, open_times)
    return TimeframeTrend(
        timeframe,
        trend,
        count,
        last_close,
        change,
        invalidation,
        range_high,
        range_low,
        position,
        zone,
        last_break,
        as_of,
    )


def _last_break(
    breaks: Sequence[StructureBreak], open_times: Sequence[datetime]
) -> LastBreak | None:
    if not breaks:
        return None
    item = breaks[-1]
    return LastBreak(item.kind, item.direction, item.level, open_times[item.break_index])


@dataclass(frozen=True, slots=True)
class MtfConsensus:
    """Đồng thuận = số khung có xu hướng cùng chiều khung chính / số khung đã xác định hướng.

    Không cộng chỉ báo đo cùng một thứ; mỗi khung chỉ góp một phiếu từ cấu trúc giá của nó.
    """

    primary: str
    direction: TrendState
    aligned: int
    known: int
    label_vi: str


def mtf_consensus(cells: Sequence[TimeframeTrend], *, primary: str) -> MtfConsensus:
    directional = [cell for cell in cells if cell.trend in ("up", "down")]
    primary_cell = next((cell for cell in cells if cell.timeframe == primary), None)
    if primary_cell is None or primary_cell.trend not in ("up", "down"):
        return MtfConsensus(
            primary,
            "unknown",
            0,
            len(directional),
            "Chưa đủ dữ liệu để xác định xu hướng đa khung",
        )
    aligned = sum(1 for cell in directional if cell.trend == primary_cell.trend)
    label = (
        f"Khung chính {primary.upper()} {TREND_LABELS_VI[primary_cell.trend]} · "
        f"{aligned}/{len(directional)} khung có xu hướng cùng chiều"
    )
    return MtfConsensus(primary, primary_cell.trend, aligned, len(directional), label)
