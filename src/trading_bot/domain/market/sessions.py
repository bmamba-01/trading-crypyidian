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
) -> tuple[LiquidityLevel, ...]:
    """Mức đỉnh/đáy của phiên đã đóng và của ngày UTC trước (PDH/PDL) kèm trạng thái.

    Mức phiên hết hạn khi phiên cùng loại kế tiếp mở; PDH/PDL hết hạn sau một ngày UTC.
    Chỉ phiên/ngày ``complete`` mới sinh mức (không dùng phiên đang chạy).
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
    return tuple(sorted(levels, key=lambda level: (level.armed_at, level.key)))
