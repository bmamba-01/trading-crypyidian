"""Anchored VWAP (IND-2 / CRY-70) neo theo quy tắc, tính bằng ``Decimal`` và không nhìn trước.

Hỗ trợ ba quy tắc neo tường minh:
1. ``week_start_utc``: reset tại đầu tuần UTC (thứ Hai 00:00:00 UTC).
2. ``month_start_utc``: reset tại đầu tháng UTC (ngày 01, 00:00:00 UTC).
3. ``confirmed_event``: neo tại sự kiện quá khứ đã xác nhận (ví dụ swing pivot đã đủ
   ``right_bars``). Giá trị chỉ bắt đầu có hiệu lực từ ``confirmed_index`` — tuyệt đối
   không phát sinh trước thời điểm sự kiện được xác nhận để bảo toàn bất biến tiền tố
   (prefix invariance).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from trading_bot.domain.market.structure import confirmed_swings

ANCHORED_VWAP_FORMULA_VERSION = "ind2-anchored-vwap-v1"
_THREE = Decimal("3")
_ZERO = Decimal("0")


class AnchorRule(StrEnum):
    """Quy tắc xác định mốc neo cho Anchored VWAP."""

    WEEK_START_UTC = "week_start_utc"
    MONTH_START_UTC = "month_start_utc"
    CONFIRMED_EVENT = "confirmed_event"


@dataclass(frozen=True, slots=True)
class ConfirmedAnchorEvent:
    """Một sự kiện neo đã xác nhận trong quá khứ.

    ``anchor_index`` là nến bắt đầu tích lũy VWAP; ``confirmed_index`` là nến sớm nhất
    hệ thống được phép biết sự kiện này tồn tại (``confirmed_index >= anchor_index``).
    """

    anchor_index: int
    confirmed_index: int
    label: str = ""

    def __post_init__(self) -> None:
        if self.anchor_index < 0:
            raise ValueError("anchor_index không được âm")
        if self.confirmed_index < self.anchor_index:
            raise ValueError("confirmed_index phải lớn hơn hoặc bằng anchor_index")


@dataclass(frozen=True, slots=True)
class AnchoredVwapResult:
    """Kết quả Anchored VWAP theo từng nến đóng; dùng ``Decimal`` chính xác."""

    anchor_rule: AnchorRule
    values: tuple[Decimal | None, ...]
    active_anchor_indices: tuple[int | None, ...]
    formula_version: str = ANCHORED_VWAP_FORMULA_VERSION


def _to_decimal(value: Decimal | int | float | str, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} phải là số hữu hạn")
    result = (
        value
        if isinstance(value, Decimal)
        else Decimal(repr(value) if isinstance(value, float) else str(value))
    )
    if not result.is_finite():
        raise ValueError(f"{name} phải là số hữu hạn")
    return result


def _validate_inputs(
    open_times: Sequence[datetime],
    high: Sequence[Decimal | int | float | str],
    low: Sequence[Decimal | int | float | str],
    close: Sequence[Decimal | int | float | str],
    volume: Sequence[Decimal | int | float | str],
) -> tuple[tuple[datetime, ...], tuple[Decimal, ...], tuple[Decimal, ...]]:
    """Kiểm tra đầu vào OHLCV và trả về (open_times_utc, typical_prices, volumes)."""
    length = len(open_times)
    if not (len(high) == len(low) == len(close) == len(volume) == length):
        raise ValueError("Các chuỗi open_times/high/low/close/volume phải có cùng độ dài")
    normalized_times: list[datetime] = []
    for index, moment in enumerate(open_times):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Thời điểm mở nến phải có timezone")
        utc_moment = moment.astimezone(UTC)
        if index > 0 and utc_moment <= normalized_times[-1]:
            raise ValueError("Thời điểm mở nến phải tăng dần")
        normalized_times.append(utc_moment)

    typical_prices: list[Decimal] = []
    volumes: list[Decimal] = []
    for raw_h, raw_l, raw_c, raw_v in zip(high, low, close, volume, strict=True):
        h = _to_decimal(raw_h, "high")
        lo = _to_decimal(raw_l, "low")
        c = _to_decimal(raw_c, "close")
        v = _to_decimal(raw_v, "volume")
        if not (lo <= c <= h):
            raise ValueError("Nến không hợp lệ: close phải nằm trong [low, high]")
        if v < _ZERO:
            raise ValueError("Khối lượng không được âm")
        typical_prices.append((h + lo + c) / _THREE)
        volumes.append(v)
    return tuple(normalized_times), tuple(typical_prices), tuple(volumes)


def _anchored_by_calendar_key(
    open_times: Sequence[datetime],
    high: Sequence[Decimal | int | float | str],
    low: Sequence[Decimal | int | float | str],
    close: Sequence[Decimal | int | float | str],
    volume: Sequence[Decimal | int | float | str],
    *,
    rule: AnchorRule,
) -> AnchoredVwapResult:
    times_utc, typical_prices, volumes = _validate_inputs(open_times, high, low, close, volume)
    values: list[Decimal | None] = []
    anchors: list[int | None] = []
    previous_key: tuple[int, int] | None = None
    weighted_sum = _ZERO
    volume_sum = _ZERO
    anchor_index: int | None = None

    for index, (moment, price, vol) in enumerate(
        zip(times_utc, typical_prices, volumes, strict=True)
    ):
        if rule is AnchorRule.WEEK_START_UTC:
            iso = moment.isocalendar()
            key = (iso.year, iso.week)
        else:
            key = (moment.year, moment.month)
        if key != previous_key:
            previous_key = key
            weighted_sum = _ZERO
            volume_sum = _ZERO
            anchor_index = index
        weighted_sum += price * vol
        volume_sum += vol
        values.append(weighted_sum / volume_sum if volume_sum > _ZERO else None)
        anchors.append(anchor_index)

    return AnchoredVwapResult(
        anchor_rule=rule,
        values=tuple(values),
        active_anchor_indices=tuple(anchors),
    )


def anchored_vwap_week_utc(
    open_times: Sequence[datetime],
    high: Sequence[Decimal | int | float | str],
    low: Sequence[Decimal | int | float | str],
    close: Sequence[Decimal | int | float | str],
    volume: Sequence[Decimal | int | float | str],
) -> AnchoredVwapResult:
    """Anchored VWAP reset vào đầu tuần UTC (thứ Hai 00:00:00 UTC)."""
    return _anchored_by_calendar_key(
        open_times, high, low, close, volume, rule=AnchorRule.WEEK_START_UTC
    )


def anchored_vwap_month_utc(
    open_times: Sequence[datetime],
    high: Sequence[Decimal | int | float | str],
    low: Sequence[Decimal | int | float | str],
    close: Sequence[Decimal | int | float | str],
    volume: Sequence[Decimal | int | float | str],
) -> AnchoredVwapResult:
    """Anchored VWAP reset vào đầu tháng UTC (ngày 01, 00:00:00 UTC)."""
    return _anchored_by_calendar_key(
        open_times, high, low, close, volume, rule=AnchorRule.MONTH_START_UTC
    )


def anchored_vwap_confirmed_events(
    open_times: Sequence[datetime],
    high: Sequence[Decimal | int | float | str],
    low: Sequence[Decimal | int | float | str],
    close: Sequence[Decimal | int | float | str],
    volume: Sequence[Decimal | int | float | str],
    events: Sequence[ConfirmedAnchorEvent],
) -> AnchoredVwapResult:
    """Anchored VWAP neo vào sự kiện đã xác nhận gần nhất (tuyệt đối không nhìn trước).

    Tại mỗi nến ``i``, chỉ xét các sự kiện có ``confirmed_index <= i``. Khi một sự kiện
    mới được xác nhận tại ``i``, VWAP được tính tích lũy từ ``anchor_index`` đến ``i``.
    Trước khi có sự kiện nào được xác nhận, giá trị là ``None``.
    """
    _, typical_prices, volumes = _validate_inputs(open_times, high, low, close, volume)
    length = len(typical_prices)
    if length == 0:
        return AnchoredVwapResult(
            anchor_rule=AnchorRule.CONFIRMED_EVENT,
            values=(),
            active_anchor_indices=(),
        )

    ordered_events = sorted(
        (event for event in events if event.confirmed_index < length),
        key=lambda item: (item.confirmed_index, item.anchor_index, item.label),
    )
    values: list[Decimal | None] = []
    anchors: list[int | None] = []
    event_cursor = 0
    active_event: ConfirmedAnchorEvent | None = None
    weighted_sum = _ZERO
    volume_sum = _ZERO

    for index in range(length):
        switched = False
        while (
            event_cursor < len(ordered_events)
            and ordered_events[event_cursor].confirmed_index <= index
        ):
            active_event = ordered_events[event_cursor]
            event_cursor += 1
            switched = True

        if active_event is None:
            values.append(None)
            anchors.append(None)
            continue

        if switched:
            weighted_sum = _ZERO
            volume_sum = _ZERO
            for pos in range(active_event.anchor_index, index + 1):
                weighted_sum += typical_prices[pos] * volumes[pos]
                volume_sum += volumes[pos]
        else:
            weighted_sum += typical_prices[index] * volumes[index]
            volume_sum += volumes[index]

        values.append(weighted_sum / volume_sum if volume_sum > _ZERO else None)
        anchors.append(active_event.anchor_index)

    return AnchoredVwapResult(
        anchor_rule=AnchorRule.CONFIRMED_EVENT,
        values=tuple(values),
        active_anchor_indices=tuple(anchors),
    )


def anchored_vwap_from_swings(
    open_times: Sequence[datetime],
    high: Sequence[Decimal | int | float | str],
    low: Sequence[Decimal | int | float | str],
    close: Sequence[Decimal | int | float | str],
    volume: Sequence[Decimal | int | float | str],
    *,
    left_bars: int = 3,
    right_bars: int = 3,
    swing_kind: Literal["all", "high", "low"] = "all",
) -> AnchoredVwapResult:
    """Anchored VWAP neo theo swing pivot đã xác nhận (không nhìn trước)."""
    # Kiểm tra đầu vào trước khi tìm pivot để báo lỗi nhất quán.
    _validate_inputs(open_times, high, low, close, volume)
    swings = confirmed_swings(
        [float(_to_decimal(v, "high")) for v in high],
        [float(_to_decimal(v, "low")) for v in low],
        left_bars=left_bars,
        right_bars=right_bars,
    )
    events = tuple(
        ConfirmedAnchorEvent(
            anchor_index=swing.index,
            confirmed_index=swing.confirmed_index,
            label=f"swing_{swing.kind}",
        )
        for swing in swings
        if swing_kind == "all" or swing.kind == swing_kind
    )
    return anchored_vwap_confirmed_events(open_times, high, low, close, volume, events)
