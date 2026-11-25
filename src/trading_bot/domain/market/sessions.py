"""Phiên giao dịch Á/Âu/Mỹ và mức thanh khoản theo phiên, không nhìn trước.

Giờ phiên khai báo theo timezone IANA của từng thị trường rồi đổi sang UTC cho *từng ngày*,
nên tự xử lý giờ mùa hè (DST): London và New York đổi giờ vào các tuần khác nhau. Phiên chỉ
ảnh hưởng biến động/khối lượng, không quyết định hướng giá; module này chỉ mô tả.

Máy trạng thái mức thanh khoản (cài độc lập từ khái niệm công khai):
``armed`` khi phiên/ngày đóng → ``rejected`` nếu nến đầu tiên vượt mức lại đóng cửa quay vào
trong (quét thanh khoản — liquidity sweep) hoặc ``accepted`` nếu đóng cửa bên ngoài →
``expired`` nếu tới hạn mà chưa bị chạm.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

SESSION_FORMULA_VERSION = "sig1-session-v1"
LIQUIDITY_FORMULA_VERSION = "sig1-liquidity-v1"
SESSION_STATS_FORMULA_VERSION = "sig1-session-stats-v1"


@dataclass(frozen=True, slots=True)
class SessionDefinition:
    """Một phiên theo giờ địa phương; ``close`` sau ``open`` trong cùng ngày địa phương."""

    key: str
    label_vi: str
    timezone: str
    open: time
    close: time


DEFAULT_SESSIONS: tuple[SessionDefinition, ...] = (
    SessionDefinition("asia", "Phiên Á (Tokyo)", "Asia/Tokyo", time(9, 0), time(18, 0)),
    SessionDefinition("london", "Phiên London", "Europe/London", time(8, 0), time(17, 0)),
    SessionDefinition("new_york", "Phiên New York", "America/New_York", time(9, 30), time(16, 0)),
)


def session_windows(
    definition: SessionDefinition, start: datetime, end: datetime
) -> tuple[tuple[datetime, datetime], ...]:
    """Các cửa sổ ``[open_utc, close_utc)`` của phiên giao với khoảng ``[start, end)``."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Thời điểm phải có timezone")
    zone = ZoneInfo(definition.timezone)
    local_day: date = start.astimezone(zone).date() - timedelta(days=1)
    last_day: date = end.astimezone(zone).date() + timedelta(days=1)
    windows: list[tuple[datetime, datetime]] = []
    while local_day <= last_day:
        opened = datetime.combine(local_day, definition.open, tzinfo=zone).astimezone(UTC)
        closed = datetime.combine(local_day, definition.close, tzinfo=zone).astimezone(UTC)
        if closed > start and opened < end:
            windows.append((opened, closed))
        local_day += timedelta(days=1)
    return tuple(windows)


@dataclass(frozen=True, slots=True)
class SessionRange:
    """Đỉnh/đáy của một phiên từ nến đã đóng; ``complete`` khi đã có nến phủ tới giờ đóng."""

    key: str
    label_vi: str
    open_at: datetime
    close_at: datetime
    high: float
    low: float
    high_index: int
    low_index: int
    bar_count: int
    complete: bool


def _validate(open_times: Sequence[datetime], *series: Sequence[float]) -> None:
    for values in series:
        if len(values) != len(open_times):
            raise ValueError("Các chuỗi nến phải có cùng độ dài")
    for earlier, later in zip(open_times, open_times[1:], strict=False):
        if later <= earlier:
            raise ValueError("Thời điểm mở nến phải tăng dần")
    if any(moment.tzinfo is None for moment in open_times):
        raise ValueError("Thời điểm mở nến phải có timezone")


def session_ranges(
    open_times: Sequence[datetime],
    high: Sequence[float],
    low: Sequence[float],
    interval: timedelta,
    sessions: Sequence[SessionDefinition] = DEFAULT_SESSIONS,
) -> tuple[SessionRange, ...]:
    """Đỉnh/đáy từng phiên; nến thuộc phiên khi ``open_at <= open_time < close_at``."""
    _validate(open_times, high, low)
    if not open_times:
        return ()
    last_close = open_times[-1] + interval
    output: list[SessionRange] = []
    for definition in sessions:
        for opened, closed in session_windows(definition, open_times[0], last_close):
            members = [i for i, moment in enumerate(open_times) if opened <= moment < closed]
            if not members:
                continue
            high_index = max(members, key=lambda i: (high[i], -i))
            low_index = min(members, key=lambda i: (low[i], i))
            output.append(
                SessionRange(
                    key=definition.key,
                    label_vi=definition.label_vi,
                    open_at=opened,
                    close_at=closed,
                    high=float(high[high_index]),
                    low=float(low[low_index]),
                    high_index=high_index,
                    low_index=low_index,
                    bar_count=len(members),
                    complete=last_close >= closed,
                )
            )
    return tuple(sorted(output, key=lambda item: (item.open_at, item.key)))


LevelState = Literal["armed", "rejected", "accepted", "expired"]
Side = Literal["high", "low"]


@dataclass(frozen=True, slots=True)
class LiquidityLevel:
    """Một mức thanh khoản và trạng thái của nó tại nến cuối cùng được xét."""

    key: str
    label_vi: str
    side: Literal["high", "low"]
    price: float
    armed_at: datetime
    expires_at: datetime
    state: LevelState
    event_index: int | None

    @property
    def is_sweep(self) -> bool:
        """Quét thanh khoản: râu vượt mức rồi đóng cửa quay lại trong cùng nến."""
        return self.state == "rejected"


def _resolve_level(
    *,
    side: Literal["high", "low"],
    price: float,
    armed_at: datetime,
    expires_at: datetime,
    open_times: Sequence[datetime],
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    interval: timedelta,
) -> tuple[LevelState, int | None]:
    for index, moment in enumerate(open_times):
        if moment < armed_at:
            continue
        if moment >= expires_at:
            return "expired", None
        crossed = high[index] > price if side == "high" else low[index] < price
        if crossed:
            inside = close[index] <= price if side == "high" else close[index] >= price
            return ("rejected" if inside else "accepted"), index
    if open_times and open_times[-1] + interval >= expires_at:
        return "expired", None
    return "armed", None


def liquidity_levels(
    open_times: Sequence[datetime],
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    interval: timedelta,
    sessions: Sequence[SessionDefinition] = DEFAULT_SESSIONS,
    *,
    include_weekly: bool = True,
) -> tuple[LiquidityLevel, ...]:
    """Mức đỉnh/đáy của phiên đã đóng, ngày UTC trước (PDH/PDL) và tuần UTC trước (PWH/PWL).

    Mức phiên hết hạn khi phiên cùng loại kế tiếp mở; PDH/PDL hết hạn sau một ngày UTC;
    PWH/PWL hết hạn sau một tuần UTC. Chỉ phiên/ngày/tuần ``complete`` mới sinh mức
    (tuyệt đối không dùng tuần đang mở).
    """
    _validate(open_times, high, low, close)
    if not open_times:
        return ()
    levels: list[LiquidityLevel] = []
    ranges = session_ranges(open_times, high, low, interval, sessions)
    for item in ranges:
        if not item.complete:
            continue
        definition = next(d for d in sessions if d.key == item.key)
        following = session_windows(definition, item.close_at, item.close_at + timedelta(days=4))
        expires_at = next(
            (opened for opened, _ in following if opened >= item.close_at),
            item.close_at + timedelta(days=1),
        )
        session_sides: tuple[tuple[Side, float], ...] = (("high", item.high), ("low", item.low))
        for side, price in session_sides:
            state, event = _resolve_level(
                side=side,
                price=price,
                armed_at=item.close_at,
                expires_at=expires_at,
                open_times=open_times,
                high=high,
                low=low,
                close=close,
                interval=interval,
            )
            levels.append(
                LiquidityLevel(
                    key=f"{item.key}_{side}",
                    label_vi=(
                        f"{'Đỉnh' if side == 'high' else 'Đáy'} "
                        f"{item.label_vi[:1].lower()}{item.label_vi[1:]}"
                    ),
                    side=side,
                    price=price,
                    armed_at=item.close_at,
                    expires_at=expires_at,
                    state=state,
                    event_index=event,
                )
            )

    last_close = open_times[-1] + interval
    day = open_times[0].astimezone(UTC).date()
    while True:
        day_start = datetime.combine(day, time(0), tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        if day_end > last_close:
            break
        members = [i for i, moment in enumerate(open_times) if day_start <= moment < day_end]
        if members:
            day_sides: tuple[tuple[Side, str, float], ...] = (
                ("high", "Đỉnh ngày trước (PDH)", float(max(high[i] for i in members))),
                ("low", "Đáy ngày trước (PDL)", float(min(low[i] for i in members))),
            )
            for side, label, price in day_sides:
                state, event = _resolve_level(
                    side=side,
                    price=price,
                    armed_at=day_end,
                    expires_at=day_end + timedelta(days=1),
                    open_times=open_times,
                    high=high,
                    low=low,
                    close=close,
                    interval=interval,
                )
                levels.append(
                    LiquidityLevel(
                        key=f"pd{side[0]}",
                        label_vi=label,
                        side=side,
                        price=price,
                        armed_at=day_end,
                        expires_at=day_end + timedelta(days=1),
                        state=state,
                        event_index=event,
                    )
                )
        day += timedelta(days=1)

    if include_weekly:
        first_open_utc = open_times[0].astimezone(UTC)
        first_monday = first_open_utc.date() - timedelta(days=first_open_utc.weekday())
        week_start = datetime.combine(first_monday, time(0), tzinfo=UTC)
        if first_open_utc > week_start:
            # Tuần đầu bị cắt giữa chừng -> bỏ qua, chỉ lấy từ tuần UTC trọn vẹn đầu tiên.
            week_start += timedelta(days=7)
        while True:
            week_end = week_start + timedelta(days=7)
            if week_end > last_close:
                break
            members = [i for i, moment in enumerate(open_times) if week_start <= moment < week_end]
            if members:
                week_sides: tuple[tuple[Side, str, float], ...] = (
                    ("high", "Đỉnh tuần trước (PWH)", float(max(high[i] for i in members))),
                    ("low", "Đáy tuần trước (PWL)", float(min(low[i] for i in members))),
                )
                for side, label, price in week_sides:
                    state, event = _resolve_level(
                        side=side,
                        price=price,
                        armed_at=week_end,
                        expires_at=week_end + timedelta(days=7),
                        open_times=open_times,
                        high=high,
                        low=low,
                        close=close,
                        interval=interval,
                    )
                    levels.append(
                        LiquidityLevel(
                            key=f"pw{side[0]}",
                            label_vi=label,
                            side=side,
                            price=price,
                            armed_at=week_end,
                            expires_at=week_end + timedelta(days=7),
                            state=state,
                            event_index=event,
                        )
                    )
            week_start = week_end

    return tuple(sorted(levels, key=lambda level: (level.armed_at, level.key)))


def _utc_week_start(moment: datetime) -> datetime:
    utc_moment = moment.astimezone(UTC)
    monday = utc_moment.date() - timedelta(days=utc_moment.weekday())
    return datetime.combine(monday, time(0), tzinfo=UTC)


def previous_week_high_low(
    open_times: Sequence[datetime],
    high: Sequence[float],
    low: Sequence[float],
    interval: timedelta,
) -> tuple[tuple[float | None, ...], tuple[float | None, ...]]:
    """Chuỗi PWH/PWL nhân quả theo tuần UTC hoàn tất gần nhất tại từng nến.

    Tuyệt đối không dùng tuần đang mở hoặc tuần đầu bị khuyết trước thứ Hai 00:00 UTC.
    Khi một tuần UTC trọn vẹn vừa đóng tại ``open_times[i] + interval``, PWH/PWL của tuần
    đó bắt đầu khả dụng từ nến ``i`` trở đi cho tới khi tuần tiếp theo hoàn tất.
    """
    _validate(open_times, high, low)
    if interval <= timedelta(0):
        raise ValueError("interval phải lớn hơn 0")
    if not open_times:
        return (), ()

    first_open_utc = open_times[0].astimezone(UTC)
    current_week_start: datetime | None = None
    current_week_end: datetime | None = None
    current_week_complete_from_start = False
    current_high = 0.0
    current_low = 0.0
    last_pwh: float | None = None
    last_pwl: float | None = None

    pwh_series: list[float | None] = []
    pwl_series: list[float | None] = []

    for moment, bar_high, bar_low in zip(open_times, high, low, strict=True):
        week_start = _utc_week_start(moment)
        week_end = week_start + timedelta(days=7)
        if current_week_start is None:
            current_week_start = week_start
            current_week_end = week_end
            current_week_complete_from_start = first_open_utc <= week_start
            current_high = float(bar_high)
            current_low = float(bar_low)
        elif week_start != current_week_start:
            if current_week_complete_from_start:
                last_pwh = current_high
                last_pwl = current_low
            current_week_start = week_start
            current_week_end = week_end
            current_week_complete_from_start = True
            current_high = float(bar_high)
            current_low = float(bar_low)
        else:
            current_high = max(current_high, float(bar_high))
            current_low = min(current_low, float(bar_low))

        bar_close = moment.astimezone(UTC) + interval
        assert current_week_end is not None
        if current_week_complete_from_start and bar_close >= current_week_end:
            last_pwh = current_high
            last_pwl = current_low

        pwh_series.append(last_pwh)
        pwl_series.append(last_pwl)

    return tuple(pwh_series), tuple(pwl_series)


@dataclass(frozen=True, slots=True)
class SessionRangePercentile:
    """Phân vị độ rộng phiên hoàn tất so với các phiên cùng loại trong quá khứ."""

    session_key: str
    open_at: datetime
    close_at: datetime
    range_size: float
    percentile: float | None
    sample_size: int
    formula_version: str = SESSION_STATS_FORMULA_VERSION


def session_range_percentiles(
    ranges: Sequence[SessionRange],
    *,
    min_samples: int = 5,
    lookback_sessions: int | None = None,
) -> tuple[SessionRangePercentile, ...]:
    """Tính phân vị độ rộng phiên (Asia/London/NY) thuần nhân quả, fail-closed khi thiếu mẫu.

    Chỉ xét phiên đã hoàn tất (``complete=True``). Lịch sử so sánh chỉ gồm các phiên cùng
    ``key`` đã đóng trước khi phiên hiện tại mở (``prior.close_at <= current.open_at``).
    Công thức phân vị bậc giữa (mid-rank): ``(số_nhỏ_hơn + 0.5 * số_bằng) / n``.
    Nếu cỡ mẫu ``n < min_samples``, trả ``percentile = None``.
    """
    if min_samples < 1:
        raise ValueError("min_samples phải ≥ 1")
    if lookback_sessions is not None and lookback_sessions < min_samples:
        raise ValueError("lookback_sessions phải ≥ min_samples")

    completed = sorted(
        (item for item in ranges if item.complete),
        key=lambda item: (item.close_at, item.open_at, item.key),
    )
    history_by_key: dict[str, list[SessionRange]] = {}
    results: list[SessionRangePercentile] = []

    for item in completed:
        prior_all = [
            prior for prior in history_by_key.get(item.key, ()) if prior.close_at <= item.open_at
        ]
        if lookback_sessions is not None:
            prior_all = prior_all[-lookback_sessions:]
        sample_size = len(prior_all)
        current_range = item.high - item.low
        if sample_size < min_samples:
            percentile: float | None = None
        else:
            prior_ranges = [prior.high - prior.low for prior in prior_all]
            below = sum(1 for value in prior_ranges if value < current_range)
            equal = sum(1 for value in prior_ranges if value == current_range)
            percentile = (below + 0.5 * equal) / sample_size
        results.append(
            SessionRangePercentile(
                session_key=item.key,
                open_at=item.open_at,
                close_at=item.close_at,
                range_size=current_range,
                percentile=percentile,
                sample_size=sample_size,
            )
        )
        history_by_key.setdefault(item.key, []).append(item)

    return tuple(results)


@dataclass(frozen=True, slots=True)
class LiquidityBaseRate:
    """Base rate lịch sử quét/phá biên thanh khoản kèm cỡ mẫu ``n`` (fail-closed khi thiếu mẫu)."""

    level_key: str
    sample_size: int
    swept_count: int
    accepted_count: int
    expired_count: int
    sweep_rate: float | None
    acceptance_rate: float | None
    touch_rate: float | None
    formula_version: str = SESSION_STATS_FORMULA_VERSION


def liquidity_base_rates(
    levels: Sequence[LiquidityLevel],
    *,
    min_samples: int = 10,
) -> tuple[LiquidityBaseRate, ...]:
    """Tính base rate quét (``rejected``) / phá biên (``accepted``) kèm ``sample_size = n``.

    Chỉ đếm các mức đã kết thúc vòng đời (``state != 'armed'``) để không bị lệch bởi mức
    chưa ngã ngũ. Khi ``sample_size < min_samples``, các tỷ lệ trả ``None`` (fail-closed).
    """
    if min_samples < 1:
        raise ValueError("min_samples phải ≥ 1")

    counts: dict[str, list[int]] = {}
    for level in levels:
        if level.state == "armed":
            continue
        bucket = counts.setdefault(level.key, [0, 0, 0])
        if level.state == "rejected":
            bucket[0] += 1
        elif level.state == "accepted":
            bucket[1] += 1
        elif level.state == "expired":
            bucket[2] += 1

    output: list[LiquidityBaseRate] = []
    for key in sorted(counts):
        swept, accepted, expired = counts[key]
        total = swept + accepted + expired
        if total < min_samples:
            sweep_rate: float | None = None
            acceptance_rate: float | None = None
            touch_rate: float | None = None
        else:
            sweep_rate = swept / total
            acceptance_rate = accepted / total
            touch_rate = (swept + accepted) / total
        output.append(
            LiquidityBaseRate(
                level_key=key,
                sample_size=total,
                swept_count=swept,
                accepted_count=accepted,
                expired_count=expired,
                sweep_rate=sweep_rate,
                acceptance_rate=acceptance_rate,
                touch_rate=touch_rate,
            )
        )
    return tuple(output)
