"""Phân loại nến VSA và ZigZag từ pivot đã xác nhận (CK-5), chỉ để mô tả trên chart.

VSA (Volume Spread Analysis — cài lại theo quy tắc phổ biến của Tom Williams, `ck5-vsa-v1`).
Mỗi nến t so với ``lookback`` nến **trước** nó (không nhìn trước):

- hạng phân vị = tỷ lệ nến trước có giá trị nhỏ hơn hẳn (0…1), cho độ rộng nến (high − low) và
  khối lượng; vị trí đóng = (close − low)/(high − low); nến tăng/giảm so với giá đóng nến trước.
- Thứ tự ưu tiên (nhãn đầu tiên khớp):
  1. ``buying_climax``: đỉnh mới của cửa sổ, volume ≥ 0,9, độ rộng ≥ 0,8, nến tăng, đóng ≤ 0,7.
  2. ``selling_climax``: đáy mới của cửa sổ, volume ≥ 0,9, độ rộng ≥ 0,8, đóng ≥ 0,3.
  3. ``upthrust``: đỉnh mới, đóng ≤ 0,3, volume ≥ 0,6.
  4. ``stopping_volume``: nến giảm, volume ≥ 0,8, đóng ≥ 0,5.
  5. ``test``: đáy thấp hơn đáy nến trước, độ rộng ≤ 0,4, đóng ≥ 0,5, volume < hai nến trước.
  6. ``no_demand``: nến tăng, độ rộng ≤ 0,3, volume < hai nến trước.
  7. ``no_supply``: nến giảm, độ rộng ≤ 0,3, volume < hai nến trước.
- Ngưỡng cố định trước backtest; đổi ngưỡng phải đổi ``VSA_FORMULA_VERSION``. Bộ màu riêng
  "HoanLe VSA" trên ảnh tham chiếu không có tài liệu nên **không** sao chép.

ZigZag (`ck5-zigzag-v1`): duyệt swing đã xác nhận theo thứ tự ``confirmed_index``; giữ một
pivot ứng viên, swing cùng loại cực trị hơn thay ứng viên, swing ngược loại cách ứng viên ≥
ngưỡng thì **chốt** ứng viên (``final_index`` = nến xác nhận swing ngược đó) và thành ứng viên
mới. Vì mỗi quyết định chỉ dùng swing đã biết tới lúc đó, điểm đã chốt không bao giờ đổi
(streaming = batch); đoạn từ điểm chốt cuối tới ứng viên là "chưa chốt".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from trading_bot.domain.market.structure import SwingPoint

VSA_FORMULA_VERSION = "ck5-vsa-v1"
ZIGZAG_FORMULA_VERSION = "ck5-zigzag-v1"
VSA_LOOKBACK = 20

VsaLabel = Literal[
    "buying_climax",
    "selling_climax",
    "upthrust",
    "stopping_volume",
    "test",
    "no_demand",
    "no_supply",
]
VSA_LABELS_VI: dict[str, str] = {
    "buying_climax": "Cao trào mua",
    "selling_climax": "Cao trào bán",
    "upthrust": "Upthrust (đẩy lên thất bại)",
    "stopping_volume": "Khối lượng chặn",
    "test": "Kiểm tra cung",
    "no_demand": "Không có cầu",
    "no_supply": "Không có cung",
}
# Nhóm ý nghĩa để tô màu: dấu hiệu sức mạnh (phía cầu) và dấu hiệu yếu (phía cung).
VSA_STRENGTH = frozenset({"selling_climax", "stopping_volume", "test", "no_supply"})
VSA_WEAKNESS = frozenset({"buying_climax", "upthrust", "no_demand"})


def _rank(value: float, window: Sequence[float]) -> float:
    return sum(1 for item in window if item < value) / len(window)


def vsa_labels(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    *,
    lookback: int = VSA_LOOKBACK,
) -> tuple[VsaLabel | None, ...]:
    """Nhãn VSA từng nến; ``None`` khi không khớp quy tắc nào hoặc chưa đủ ``lookback`` nến."""
    if not len(highs) == len(lows) == len(closes) == len(volumes):
        raise ValueError("Các chuỗi nến phải có cùng độ dài")
    if lookback < 3:
        raise ValueError("lookback phải ≥ 3")
    output: list[VsaLabel | None] = []
    for t in range(len(closes)):
        if t < lookback:
            output.append(None)
            continue
        window = range(t - lookback, t)
        spread = highs[t] - lows[t]
        if spread < 0:
            raise ValueError("Nến không hợp lệ")
        spread_rank = _rank(spread, [highs[i] - lows[i] for i in window])
        volume_rank = _rank(volumes[t], [volumes[i] for i in window])
        close_pos = (closes[t] - lows[t]) / spread if spread > 0 else 0.5
        up = closes[t] > closes[t - 1]
        down = closes[t] < closes[t - 1]
        new_high = highs[t] > max(highs[i] for i in window)
        new_low = lows[t] < min(lows[i] for i in window)
        quiet = volumes[t] < volumes[t - 1] and volumes[t] < volumes[t - 2]
        label: VsaLabel | None = None
        if new_high and volume_rank >= 0.9 and spread_rank >= 0.8 and up and close_pos <= 0.7:
            label = "buying_climax"
        elif new_low and volume_rank >= 0.9 and spread_rank >= 0.8 and close_pos >= 0.3:
            label = "selling_climax"
        elif new_high and close_pos <= 0.3 and volume_rank >= 0.6:
            label = "upthrust"
        elif down and volume_rank >= 0.8 and close_pos >= 0.5:
            label = "stopping_volume"
        elif lows[t] < lows[t - 1] and spread_rank <= 0.4 and close_pos >= 0.5 and quiet:
            label = "test"
        elif up and spread_rank <= 0.3 and quiet:
            label = "no_demand"
        elif down and spread_rank <= 0.3 and quiet:
            label = "no_supply"
        output.append(label)
    return tuple(output)


@dataclass(frozen=True, slots=True)
class ZigZagPoint:
    kind: Literal["high", "low"]
    index: int
    price: float
    confirmed_index: int
    final_index: int


@dataclass(frozen=True, slots=True)
class ZigZagResult:
    points: tuple[ZigZagPoint, ...]
    pending: SwingPoint | None
    formula_version: str = ZIGZAG_FORMULA_VERSION


def zigzag(
    swings: Sequence[SwingPoint],
    atr_values: Sequence[float | None],
    *,
    min_move_atr: float | None = 2.0,
    min_move_pct: float | None = None,
) -> ZigZagResult:
    """ZigZag không vẽ lại: điểm chốt chỉ phụ thuộc swing đã xác nhận trước ``final_index``."""
    if min_move_atr is None and min_move_pct is None:
        raise ValueError("Cần một ngưỡng: min_move_atr hoặc min_move_pct")

    def threshold(reference: float, at: int) -> float | None:
        limits: list[float] = []
        if min_move_pct is not None:
            limits.append(abs(reference) * min_move_pct / 100)
        if min_move_atr is not None:
            atr_value = atr_values[at] if 0 <= at < len(atr_values) else None
            if atr_value is None:
                return None  # chưa có ATR thì chưa đủ căn cứ chốt
            limits.append(min_move_atr * atr_value)
        return max(limits)

    points: list[ZigZagPoint] = []
    candidate: SwingPoint | None = None
    for swing in sorted(swings, key=lambda s: (s.confirmed_index, s.index)):
        if candidate is None:
            candidate = swing
            continue
        if swing.kind == candidate.kind:
            more_extreme = (
                swing.price > candidate.price
                if swing.kind == "high"
                else swing.price < candidate.price
            )
            if more_extreme:
                candidate = swing
            continue
        limit = threshold(candidate.price, swing.confirmed_index)
        if limit is None or abs(swing.price - candidate.price) < limit:
            continue
        points.append(
            ZigZagPoint(
                kind=candidate.kind,
                index=candidate.index,
                price=candidate.price,
                confirmed_index=candidate.confirmed_index,
                final_index=swing.confirmed_index,
            )
        )
        candidate = swing
    return ZigZagResult(points=tuple(points), pending=candidate)
