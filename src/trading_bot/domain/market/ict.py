"""Lớp ICT/SMC (CK-3): killzone, FVG/IFVG/BPR, order block, đỉnh/đáy bằng nhau, ITH/ITL.

Định nghĩa ICT không thống nhất giữa các nguồn; module dùng **quy ước cố định** của WBS CK
(`ck3-ict-v1`), chỉ để mô tả và gắn nhãn trên chart cho tới khi qua kiểm định SIG-5. Mọi vùng có
``confirmed_index`` — nến đóng mà tại đó vùng mới được biết — để không vẽ/không dùng vùng sớm hơn
thực tế (không nhìn trước). Trạng thái tại một nến bất kỳ lấy bằng ``state_at``.

Quy ước:

- FVG tăng: ``low[t] > high[t-2]``; FVG giảm: ``high[t] < low[t-2]``; xác nhận khi nến t đóng;
  bỏ khe nhỏ hơn ``min_gap_atr × ATR``. Chạm một phần khi râu vào vùng, lấp đầy khi râu tới
  cạnh xa. **Đảo vai (IFVG)** khi **giá đóng cửa** xuyên cạnh xa (không dùng râu); IFVG mất
  hiệu lực khi giá đóng cửa quay lại xuyên cạnh còn lại.
- BPR: phần giao của hai FVG ngược chiều có nến xác nhận cách nhau ≤ N nến; hướng theo FVG xuất
  hiện sau; mất hiệu lực khi giá đóng cửa xuyên vùng ngược hướng.
- Order block: với mỗi lần phá cấu trúc (BOS/CHoCH) đã xác nhận, nến ngược chiều cuối cùng
  tại/trước điểm cực trị của nhịp hồi giữa swing bị phá và nến phá; vùng là cả thân + râu của
  nến đó; mất hiệu lực khi đóng cửa xuyên vùng.
- Đỉnh/đáy bằng nhau: hai swing cùng loại chênh ≤ ``tolerance_atr × ATR``, giữa chúng không có
  nến vượt mức; bị quét khi râu vượt mức sau khi xác nhận.
- ITH/ITL: swing có swing cùng loại liền trước và liền sau đều thấp hơn (đỉnh) / cao hơn (đáy);
  xác nhận khi swing liền sau được xác nhận.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from trading_bot.domain.market.structure import StructureBreak, SwingPoint

ICT_FORMULA_VERSION = "ck3-ict-v1"
KILLZONE_TABLE_VERSION = "ck3-killzone-v1"
KILLZONE_TIMEZONE = "America/New_York"

Direction = Literal["bullish", "bearish"]
KillzoneKind = Literal["killzone", "silver_bullet", "macro"]
ZoneState = Literal["active", "partial", "filled", "inverted", "invalidated"]


@dataclass(frozen=True, slots=True)
class KillzoneDefinition:
    """Một cửa sổ giờ New York; ``end <= start`` nghĩa là kết thúc sang ngày hôm sau."""

    key: str
    label_vi: str
    kind: KillzoneKind
    start: time
    end: time


# Bảng giờ có version (KILLZONE_TABLE_VERSION); đổi giờ phải đổi version.
DEFAULT_KILLZONES: tuple[KillzoneDefinition, ...] = (
    KillzoneDefinition("asia", "Killzone Á", "killzone", time(20, 0), time(0, 0)),
    KillzoneDefinition("london", "Killzone London", "killzone", time(2, 0), time(5, 0)),
    KillzoneDefinition("ny_am", "Killzone NY sáng", "killzone", time(7, 0), time(10, 0)),
    KillzoneDefinition("ny_pm", "Killzone NY chiều", "killzone", time(13, 30), time(16, 0)),
    KillzoneDefinition(
        "silver_bullet_london", "Silver Bullet London", "silver_bullet", time(3, 0), time(4, 0)
    ),
    KillzoneDefinition(
        "silver_bullet_am", "Silver Bullet NY sáng", "silver_bullet", time(10, 0), time(11, 0)
    ),
    KillzoneDefinition(
        "silver_bullet_pm", "Silver Bullet NY chiều", "silver_bullet", time(14, 0), time(15, 0)
    ),
    KillzoneDefinition("macro_0950", "Macro 09:50–10:10", "macro", time(9, 50), time(10, 10)),
)


@dataclass(frozen=True, slots=True)
class KillzoneWindow:
    key: str
    label_vi: str
    kind: KillzoneKind
    open_at: datetime
    close_at: datetime


def killzone_windows(
    start: datetime,
    end: datetime,
    definitions: Sequence[KillzoneDefinition] = DEFAULT_KILLZONES,
) -> tuple[KillzoneWindow, ...]:
    """Các cửa sổ ``[open, close)`` (UTC) giao với ``[start, end)``; tự xử lý DST New York."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Thời điểm phải có timezone")
    zone = ZoneInfo(KILLZONE_TIMEZONE)
    first: date = start.astimezone(zone).date() - timedelta(days=1)
    last: date = end.astimezone(zone).date() + timedelta(days=1)
    output: list[KillzoneWindow] = []
    for definition in definitions:
        day = first
        while day <= last:
            end_day = day + timedelta(days=1) if definition.end <= definition.start else day
            opened = datetime.combine(day, definition.start, tzinfo=zone).astimezone(UTC)
            closed = datetime.combine(end_day, definition.end, tzinfo=zone).astimezone(UTC)
            if closed > start and opened < end:
                output.append(
                    KillzoneWindow(
                        definition.key, definition.label_vi, definition.kind, opened, closed
                    )
                )
            day += timedelta(days=1)
    return tuple(sorted(output, key=lambda item: (item.open_at, item.key)))


def active_killzones(
    now: datetime, definitions: Sequence[KillzoneDefinition] = DEFAULT_KILLZONES
) -> tuple[KillzoneWindow, ...]:
    """Cửa sổ đang mở tại ``now``."""
    return tuple(
        item
        for item in killzone_windows(now, now + timedelta(seconds=1), definitions)
        if item.open_at <= now < item.close_at
    )


def _check_lengths(*series: Sequence[float | None]) -> int:
    lengths = {len(values) for values in series}
    if len(lengths) != 1:
        raise ValueError("Các chuỗi nến phải có cùng độ dài")
    return lengths.pop()


@dataclass(frozen=True, slots=True)
class PriceZone:
    """Vùng giá ICT (FVG, BPR, order block) và các mốc thay đổi trạng thái theo chỉ số nến."""

    kind: Literal["fvg", "bpr", "order_block"]
    direction: Direction
    bottom: float
    top: float
    created_index: int
    confirmed_index: int
    first_touch_index: int | None = None
    filled_index: int | None = None
    inverted_index: int | None = None
    invalidated_index: int | None = None

    @property
    def mitigated_index(self) -> int | None:
        """Lần đầu giá quay lại chạm vùng (thuật ngữ thường dùng với order block)."""
        return self.first_touch_index

    @property
    def state(self) -> ZoneState:
        return self._state(None)

    def state_at(self, index: int) -> ZoneState | None:
        """Trạng thái tại nến ``index`` (đã đóng); ``None`` nếu vùng chưa được xác nhận."""
        if index < self.confirmed_index:
            return None
        return self._state(index)

    def _state(self, index: int | None) -> ZoneState:
        def seen(event: int | None) -> bool:
            return event is not None and (index is None or event <= index)

        if seen(self.invalidated_index):
            return "invalidated"
        if seen(self.inverted_index):
            return "inverted"
        if seen(self.filled_index):
            return "filled"
        if seen(self.first_touch_index):
            return "partial"
        return "active"


def _track_zone(
    zone: PriceZone,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    invertible: bool,
) -> PriceZone:
    """Duyệt nến sau ``confirmed_index`` để ghi mốc chạm, lấp, đảo vai và mất hiệu lực."""
    touch = filled = inverted = invalidated = None
    bullish = zone.direction == "bullish"
    for index in range(zone.confirmed_index + 1, len(closes)):
        # Vùng tăng là hỗ trợ: giá đi xuống chạm cạnh trên, lấp tới cạnh dưới.
        near, far = (zone.top, zone.bottom) if bullish else (zone.bottom, zone.top)
        wick_near = lows[index] <= near if bullish else highs[index] >= near
        wick_far = lows[index] <= far if bullish else highs[index] >= far
        close_through_far = closes[index] < far if bullish else closes[index] > far
        if inverted is None:
            if touch is None and wick_near:
                touch = index
            if filled is None and wick_far:
                filled = index
            if close_through_far:
                if invertible:
                    inverted = index
                else:
                    invalidated = index
                    break
        else:
            # Sau khi đảo vai, cạnh "gần" cũ trở thành cạnh kháng cự/hỗ trợ ngược lại.
            back_through = closes[index] > zone.top if bullish else closes[index] < zone.bottom
            if back_through:
                invalidated = index
                break
    return PriceZone(
        kind=zone.kind,
        direction=zone.direction,
        bottom=zone.bottom,
        top=zone.top,
        created_index=zone.created_index,
        confirmed_index=zone.confirmed_index,
        first_touch_index=touch,
        filled_index=filled,
        inverted_index=inverted,
        invalidated_index=invalidated,
    )


def fair_value_gaps(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    atr_values: Sequence[float | None] | None,
    min_gap_atr: float = 0.1,
) -> tuple[PriceZone, ...]:
    """FVG ba nến kèm trạng thái lấp/đảo vai; ``atr_values`` None thì không lọc khe nhỏ."""
    count = _check_lengths(opens, highs, lows, closes)
    if atr_values is not None and len(atr_values) != count:
        raise ValueError("Các chuỗi nến phải có cùng độ dài")
    gaps: list[PriceZone] = []
    for t in range(2, count):
        candidates: list[tuple[Direction, float, float]] = []
        if lows[t] > highs[t - 2]:
            candidates.append(("bullish", highs[t - 2], lows[t]))
        if highs[t] < lows[t - 2]:
            candidates.append(("bearish", highs[t], lows[t - 2]))
        for direction, bottom, top in candidates:
            threshold = atr_values[t] if atr_values is not None else None
            if threshold is not None and top - bottom < min_gap_atr * threshold:
                continue
            gaps.append(
                _track_zone(
                    PriceZone("fvg", direction, bottom, top, t - 2, t),
                    highs,
                    lows,
                    closes,
                    invertible=True,
                )
            )
    return tuple(gaps)


def balanced_price_ranges(
    gaps: Sequence[PriceZone],
    closes: Sequence[float],
    *,
    max_distance_bars: int = 10,
) -> tuple[PriceZone, ...]:
    """Giao của hai FVG ngược chiều gần nhau; mỗi FVG sau chỉ ghép với FVG trước gần nhất."""
    ordered = sorted(gaps, key=lambda gap: (gap.confirmed_index, gap.created_index))
    output: list[PriceZone] = []
    for position, later in enumerate(ordered):
        for earlier in reversed(ordered[:position]):
            if later.confirmed_index - earlier.confirmed_index > max_distance_bars:
                break
            if earlier.direction == later.direction:
                continue
            bottom = max(earlier.bottom, later.bottom)
            top = min(earlier.top, later.top)
            if top <= bottom:
                continue
            zone = PriceZone(
                "bpr", later.direction, bottom, top, earlier.created_index, later.confirmed_index
            )
            # BPR không đảo vai; chỉ theo dõi chạm và mất hiệu lực khi đóng cửa xuyên vùng.
            invalidated = next(
                (
                    index
                    for index in range(zone.confirmed_index + 1, len(closes))
                    if (
                        closes[index] < bottom
                        if zone.direction == "bullish"
                        else closes[index] > top
                    )
                ),
                None,
            )
            output.append(
                PriceZone(
                    "bpr",
                    zone.direction,
                    bottom,
                    top,
                    zone.created_index,
                    zone.confirmed_index,
                    invalidated_index=invalidated,
                )
            )
            break
    return tuple(output)


def order_blocks(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    breaks: Sequence[StructureBreak],
    *,
    lookback_bars: int = 10,
) -> tuple[PriceZone, ...]:
    """Order block cho từng lần phá cấu trúc đã xác nhận (BOS/CHoCH của SIG-1)."""
    count = _check_lengths(opens, highs, lows, closes)
    output: list[PriceZone] = []
    for item in breaks:
        if not 0 <= item.swing_index < item.break_index < count:
            continue
        span = range(item.swing_index, item.break_index + 1)
        bullish = item.direction == "bullish"
        # Điểm cực trị của nhịp hồi trước khi phá: đáy thấp nhất (phá lên) / đỉnh cao nhất.
        extreme = (
            min(span, key=lambda i: (lows[i], -i))
            if bullish
            else max(span, key=lambda i: (highs[i], i))
        )
        candle = next(
            (
                index
                for index in range(extreme, max(extreme - lookback_bars, -1), -1)
                if (closes[index] < opens[index] if bullish else closes[index] > opens[index])
            ),
            None,
        )
        if candle is None:
            continue
        output.append(
            _track_zone(
                PriceZone(
                    "order_block",
                    item.direction,
                    lows[candle],
                    highs[candle],
                    candle,
                    item.break_index,
                ),
                highs,
                lows,
                closes,
                invertible=False,
            )
        )
    return tuple(output)


@dataclass(frozen=True, slots=True)
class EqualLevel:
    """Đỉnh/đáy bằng nhau — vùng thanh khoản; ``swept_index`` khi râu vượt mức lần đầu."""

    side: Literal["high", "low"]
    price: float
    swing_indices: tuple[int, int]
    confirmed_index: int
    swept_index: int | None


def equal_levels(
    swings: Sequence[SwingPoint],
    highs: Sequence[float],
    lows: Sequence[float],
    atr_values: Sequence[float | None],
    *,
    tolerance_atr: float = 0.1,
    compare_last: int = 3,
) -> tuple[EqualLevel, ...]:
    """Cặp swing cùng loại gần bằng nhau (≤ ``tolerance_atr`` × ATR tại nến xác nhận)."""
    _check_lengths(highs, lows, atr_values)
    output: list[EqualLevel] = []
    for side in ("high", "low"):
        same = sorted((s for s in swings if s.kind == side), key=lambda s: s.confirmed_index)
        for position, later in enumerate(same):
            atr_value = (
                atr_values[later.confirmed_index]
                if later.confirmed_index < len(atr_values)
                else None
            )
            if atr_value is None or atr_value <= 0:
                continue
            for earlier in reversed(same[max(0, position - compare_last) : position]):
                if abs(later.price - earlier.price) > tolerance_atr * atr_value:
                    continue
                level = (
                    max(later.price, earlier.price)
                    if side == "high"
                    else min(later.price, earlier.price)
                )
                between = range(earlier.index + 1, later.index)
                if side == "high" and any(highs[i] > level for i in between):
                    continue
                if side == "low" and any(lows[i] < level for i in between):
                    continue
                swept = next(
                    (
                        i
                        for i in range(later.confirmed_index + 1, len(highs))
                        if (highs[i] > level if side == "high" else lows[i] < level)
                    ),
                    None,
                )
                output.append(
                    EqualLevel(
                        side=side,
                        price=level,
                        swing_indices=(earlier.index, later.index),
                        confirmed_index=later.confirmed_index,
                        swept_index=swept,
                    )
                )
                break
    return tuple(sorted(output, key=lambda item: (item.confirmed_index, item.side)))


@dataclass(frozen=True, slots=True)
class IntermediateSwing:
    kind: Literal["high", "low"]
    index: int
    price: float
    confirmed_index: int


def intermediate_swings(swings: Sequence[SwingPoint]) -> tuple[IntermediateSwing, ...]:
    """ITH/ITL: swing nổi bật hơn hai swing cùng loại liền kề; biết khi swing sau xác nhận."""
    output: list[IntermediateSwing] = []
    for kind in ("high", "low"):
        same = sorted((s for s in swings if s.kind == kind), key=lambda s: s.index)
        for previous, current, following in zip(same, same[1:], same[2:], strict=False):
            higher = current.price > previous.price and current.price > following.price
            lower = current.price < previous.price and current.price < following.price
            if (kind == "high" and higher) or (kind == "low" and lower):
                output.append(
                    IntermediateSwing(
                        kind=kind,
                        index=current.index,
                        price=current.price,
                        confirmed_index=max(following.confirmed_index, current.confirmed_index),
                    )
                )
    return tuple(sorted(output, key=lambda item: (item.confirmed_index, item.kind)))
