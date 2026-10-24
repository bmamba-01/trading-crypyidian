"""Các phép kiểm tra chất lượng dữ liệu thị trường chạy offline."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from trading_bot.domain.market.models import OHLCVBar

MAX_EXPECTED_RANGE_BARS = 100_000


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


@dataclass(frozen=True, slots=True)
class BarSeriesQuality:
    """Kết quả kiểm tra nến, không tự sửa hoặc loại bỏ dữ liệu nguồn."""

    duplicate_keys: tuple[tuple[str, str, datetime], ...]
    gap_after: tuple[tuple[str, str, datetime], ...]
    future_available_count: int
    unclosed_count: int
    missing_expected_open_times: tuple[tuple[str, str, datetime], ...]
    missing_boundary_open_times: tuple[tuple[str, str, datetime], ...]
    backfill_complete: bool | None
    stale: bool
    visible_bar_count: int

    @property
    def is_usable(self) -> bool:
        """Chỉ cho phép dùng chuỗi đầy đủ, không trùng, không cũ tại as_of."""
        return (
            self.visible_bar_count > 0
            and not self.duplicate_keys
            and not self.gap_after
            and self.future_available_count == 0
            and self.unclosed_count == 0
            and not self.missing_expected_open_times
            and self.backfill_complete is not False
            and not self.stale
        )


def bars_available_as_of(bars: tuple[OHLCVBar, ...], as_of: datetime) -> tuple[OHLCVBar, ...]:
    """Trả về đúng các nến đã khả dụng tại mốc as_of theo thứ tự sự kiện."""
    _require_aware(as_of, "as_of")
    visible = (bar for bar in bars if bar.available_at <= as_of)
    return tuple(sorted(visible, key=lambda bar: (bar.event_at, bar.symbol, bar.timeframe)))


def inspect_bar_series(
    bars: tuple[OHLCVBar, ...],
    *,
    as_of: datetime,
    expected_interval: timedelta,
    max_event_age: timedelta,
    expected_range_start: datetime | None = None,
    expected_range_end: datetime | None = None,
    backfill_complete: bool = False,
    expected_streams: tuple[tuple[str, str], ...] = (),
) -> BarSeriesQuality:
    """Phát hiện trùng, thiếu nến, dữ liệu đến muộn và chuỗi đã cũ.

    Khi yêu cầu khoảng, hai biên xác định khoảng nửa mở `[start, end)`;
    chuỗi chưa được xác nhận backfill sẽ không được đánh dấu có thể sử dụng.
    """
    _require_aware(as_of, "as_of")
    if expected_interval <= timedelta(0):
        raise ValueError("expected_interval phải lớn hơn 0.")
    if max_event_age < timedelta(0):
        raise ValueError("max_event_age không được âm.")

    range_requested = expected_range_start is not None or expected_range_end is not None
    if expected_streams and not range_requested:
        raise ValueError("expected_streams chỉ dùng khi có khoảng expected range.")
    if any(not symbol.strip() or not timeframe.strip() for symbol, timeframe in expected_streams):
        raise ValueError("expected_streams không được chứa symbol/timeframe rỗng.")
    expected_times: tuple[datetime, ...] = ()
    if range_requested:
        if expected_range_start is None or expected_range_end is None:
            raise ValueError("Cần truyền đồng thời expected_range_start và expected_range_end.")
        _require_aware(expected_range_start, "expected_range_start")
        _require_aware(expected_range_end, "expected_range_end")
        if expected_range_start >= expected_range_end:
            raise ValueError("Khoảng expected range phải có start trước end.")
        duration = expected_range_end - expected_range_start
        if duration % expected_interval != timedelta(0):
            raise ValueError("Độ dài expected range phải chia hết cho expected_interval.")
        expected_count = duration // expected_interval
        if expected_count > MAX_EXPECTED_RANGE_BARS:
            raise ValueError(
                f"Expected range vượt giới hạn {MAX_EXPECTED_RANGE_BARS} nến để kiểm tra an toàn."
            )
        expected_times = tuple(
            expected_range_start + index * expected_interval for index in range(expected_count)
        )

    future_count = sum(bar.available_at > as_of for bar in bars)
    visible = bars_available_as_of(bars, as_of)

    seen: set[tuple[str, str, datetime]] = set()
    duplicate_keys: set[tuple[str, str, datetime]] = set()
    unique: dict[tuple[str, str], dict[datetime, OHLCVBar]] = {}
    for bar in visible:
        if bar.identity in seen:
            duplicate_keys.add(bar.identity)
        seen.add(bar.identity)
        unique.setdefault((bar.symbol, bar.timeframe), {}).setdefault(bar.open_time, bar)

    gaps: set[tuple[str, str, datetime]] = set()
    for (symbol, timeframe), grouped in unique.items():
        ordered = sorted(grouped.values(), key=lambda bar: bar.open_time)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.open_time - previous.open_time > expected_interval:
                gaps.add((symbol, timeframe, previous.open_time))

    stale = any(
        as_of - max(bar.event_at for bar in grouped.values()) > max_event_age
        for grouped in unique.values()
    )
    missing_expected: set[tuple[str, str, datetime]] = set()
    missing_boundaries: set[tuple[str, str, datetime]] = set()
    if range_requested:
        boundary_times = {expected_times[0], expected_times[-1]}
        streams_to_check = set(unique) | set(expected_streams)
        for symbol, timeframe in streams_to_check:
            grouped = unique.get((symbol, timeframe), {})
            available_open_times = set(grouped)
            for expected_time in expected_times:
                if expected_time not in available_open_times:
                    missing_expected.add((symbol, timeframe, expected_time))
                    if expected_time in boundary_times:
                        missing_boundaries.add((symbol, timeframe, expected_time))
    return BarSeriesQuality(
        duplicate_keys=tuple(sorted(duplicate_keys)),
        gap_after=tuple(sorted(gaps)),
        future_available_count=future_count,
        unclosed_count=sum(not bar.is_closed for bar in visible),
        missing_expected_open_times=tuple(sorted(missing_expected)),
        missing_boundary_open_times=tuple(sorted(missing_boundaries)),
        backfill_complete=backfill_complete if range_requested else None,
        stale=stale,
        visible_bar_count=len(visible),
    )
