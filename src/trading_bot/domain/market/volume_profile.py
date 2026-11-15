"""Áp lực mua/bán và hồ sơ khối lượng (volume profile) từ nến đã đóng.

- Delta nến dùng trường *taker buy base volume* của kline Binance USDⓈ-M:
  ``delta = 2 × taker_buy − volume``. Đây là dữ liệu thật của sàn; khi nến chưa có trường này
  (dữ liệu cũ, SIG-0 chưa thu) kết quả là ``None`` — không ước lượng.
- Phân bổ khối lượng của một nến vào các bin giá là **xấp xỉ**: chia đều theo dải [low, high].
  Muốn đúng theo giá cần aggTrades. Tỷ lệ mua/bán của nến được áp cho mọi bin nến đó phủ.
- Phân loại mất cân bằng cài độc lập theo khái niệm công khai: E = |mua − bán| / (mua + bán),
  Eref = Σ|mua − bán| / Σ(mua + bán); bin "hoạt động cao" khi khối lượng ≥ 1,25× trung bình
  các bin có khối lượng.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

VOLUME_PROFILE_FORMULA_VERSION = "sig1-volume-profile-v1"
HIGH_ACTIVITY_MULTIPLIER = 1.25


def bar_delta(volume: float, taker_buy_volume: float | None) -> float | None:
    """Delta một nến = mua chủ động − bán chủ động; ``None`` khi thiếu dữ liệu taker."""
    if taker_buy_volume is None:
        return None
    if volume < 0 or taker_buy_volume < 0 or taker_buy_volume > volume:
        raise ValueError("taker buy phải nằm trong [0, volume]")
    return 2.0 * taker_buy_volume - volume


def cumulative_delta(
    volumes: Sequence[float],
    taker_buys: Sequence[float | None],
    reset_keys: Sequence[object],
) -> tuple[float | None, ...]:
    """CVD reset khi ``reset_keys`` đổi (ví dụ ngày UTC). Thiếu taker ở một nến → ``None``
    cho nến đó và các nến sau trong cùng kỳ, vì tổng tích lũy không còn đúng."""
    if not len(volumes) == len(taker_buys) == len(reset_keys):
        raise ValueError("Các chuỗi phải cùng độ dài")
    output: list[float | None] = []
    total: float | None = 0.0
    previous_key: object = object()
    for volume, taker, key in zip(volumes, taker_buys, reset_keys, strict=True):
        if key != previous_key:
            total = 0.0
            previous_key = key
        delta = bar_delta(volume, taker)
        total = None if total is None or delta is None else total + delta
        output.append(total)
    return tuple(output)


BinCategory = Literal["high_imbalance", "high_activity_low_imbalance", "normal"]


@dataclass(frozen=True, slots=True)
class ProfileBin:
    """Một hàng giá của profile; ``buy``/``sell`` là ``None`` khi thiếu dữ liệu taker."""

    price_low: float
    price_high: float
    volume: float
    buy: float | None
    sell: float | None
    category: BinCategory

    @property
    def imbalance(self) -> float | None:
        """Mất cân bằng có dấu (mua − bán)/(mua + bán) trong [-1, 1]."""
        if self.buy is None or self.sell is None or self.buy + self.sell <= 0:
            return None
        return (self.buy - self.sell) / (self.buy + self.sell)


@dataclass(frozen=True, slots=True)
class VolumeProfile:
    bins: tuple[ProfileBin, ...]
    poc_price: float
    value_area_high: float
    value_area_low: float
    total_volume: float
    imbalance_reference: float | None
    formula_version: str = VOLUME_PROFILE_FORMULA_VERSION


def volume_profile(
    high: Sequence[float],
    low: Sequence[float],
    volume: Sequence[float],
    taker_buy: Sequence[float | None] | None = None,
    *,
    bins: int = 24,
    value_area: float = 0.70,
) -> VolumeProfile | None:
    """Volume profile trên các nến truyền vào; ``None`` khi không có khối lượng dương."""
    if bins < 2:
        raise ValueError("bins phải ≥ 2")
    if not 0 < value_area <= 1:
        raise ValueError("value_area phải trong (0, 1]")
    if not len(high) == len(low) == len(volume):
        raise ValueError("Các chuỗi phải cùng độ dài")
    takers: Sequence[float | None] = taker_buy if taker_buy is not None else [None] * len(volume)
    if len(takers) != len(volume):
        raise ValueError("Chuỗi taker buy phải cùng độ dài")
    if not high or math.fsum(volume) <= 0:
        return None
    top = max(high)
    bottom = min(low)
    if top <= bottom:
        top = bottom + max(abs(bottom) * 1e-9, 1e-12)
    step = (top - bottom) / bins
    totals = [0.0] * bins
    buys = [0.0] * bins
    sells = [0.0] * bins
    split_known = all(value is not None for value in takers)
    for bar_high, bar_low, bar_volume, taker in zip(high, low, volume, takers, strict=True):
        if bar_low > bar_high or bar_volume < 0:
            raise ValueError("Nến không hợp lệ")
        if bar_volume == 0:
            continue
        buy_share = None if taker is None else taker / bar_volume
        first = min(int((bar_low - bottom) / step), bins - 1)
        last = min(int((bar_high - bottom) / step), bins - 1)
        span = bar_high - bar_low
        for index in range(first, last + 1):
            if span <= 0:
                portion = 1.0
            else:
                lower = max(bar_low, bottom + index * step)
                upper = min(bar_high, bottom + (index + 1) * step)
                portion = max(upper - lower, 0.0) / span
            allocated = bar_volume * portion
            totals[index] += allocated
            if buy_share is not None:
                buys[index] += allocated * buy_share
                sells[index] += allocated * (1.0 - buy_share)

    total_volume = math.fsum(totals)
    poc_index = max(range(bins), key=lambda i: (totals[i], -i))
    included = {poc_index}
    below, above = poc_index - 1, poc_index + 1
    covered = totals[poc_index]
    while covered < value_area * total_volume and (below >= 0 or above < bins):
        below_volume = totals[below] if below >= 0 else -1.0
        above_volume = totals[above] if above < bins else -1.0
        if above_volume >= below_volume:
            included.add(above)
            covered += above_volume
            above += 1
        else:
            included.add(below)
            covered += below_volume
            below -= 1

    reference: float | None = None
    if split_known:
        gross = math.fsum(buys[i] + sells[i] for i in range(bins))
        net = math.fsum(abs(buys[i] - sells[i]) for i in range(bins))
        reference = net / gross if gross else None
    active = [value for value in totals if value > 0]
    mean_active = math.fsum(active) / len(active) if active else 0.0

    profile_bins: list[ProfileBin] = []
    for index in range(bins):
        buy = buys[index] if split_known else None
        sell = sells[index] if split_known else None
        category: BinCategory = "normal"
        if buy is not None and sell is not None and reference and buy + sell > 0:
            ratio = abs(buy - sell) / (buy + sell)
            if ratio >= 2 * reference:
                category = "high_imbalance"
            elif ratio <= reference / 2 and totals[index] >= HIGH_ACTIVITY_MULTIPLIER * mean_active:
                category = "high_activity_low_imbalance"
        profile_bins.append(
            ProfileBin(
                price_low=bottom + index * step,
                price_high=bottom + (index + 1) * step,
                volume=totals[index],
                buy=buy,
                sell=sell,
                category=category,
            )
        )
    return VolumeProfile(
        bins=tuple(profile_bins),
        poc_price=bottom + (poc_index + 0.5) * step,
        value_area_high=bottom + (max(included) + 1) * step,
        value_area_low=bottom + min(included) * step,
        total_volume=total_volume,
        imbalance_reference=reference,
    )
