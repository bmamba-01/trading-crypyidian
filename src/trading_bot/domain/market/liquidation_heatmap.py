"""Bản đồ nhiệt cụm thanh lý ước lượng point-in-time (IND-4).

Module thuần (không I/O) ước lượng các cụm thanh lý Long/Short từ chuỗi nến OHLCV,
Open Interest (nếu có) và sự kiện thanh lý thật ``forceOrder`` với ràng buộc nhân quả
tuyệt đối (``bar.close_time <= as_of``, ``liq.available_at <= as_of``,
``oi_snapshot.collected_at <= as_of``).

Quy trình 3 bước:
1. Tạo vùng entry tập trung thanh khoản từ swing high/low đã xác nhận nhân quả và
   các nến có ``quote_volume`` cao (nhân trọng số gia tăng OI nếu khả dụng), phân bổ
   theo các mức đòn bẩy phổ biến (10x, 20x, 25x, 50x, 100x) với tỷ lệ ký quỹ duy trì
   ``mmr = 0.004``:
   - Long quanh ``P``: ``P * (1 - 1/L + mmr)``
   - Short quanh ``P``: ``P * (1 + 1/L - mmr)``
2. Quét/suy giảm (Sweep & Decay): cụm bị vô hiệu nếu giá nến sau đó đã quét qua mức
   thanh lý (``low <= cluster_price`` với Long; ``high >= cluster_price`` với Short)
   hoặc suy giảm theo thanh lý thật ``forceOrder`` ghi nhận ở vùng giá đó và theo tuổi.
3. Tổng hợp thành ``long_liq_clusters`` (dưới giá hiện tại) và ``short_liq_clusters``
   (trên giá hiện tại), tính khoảng cách theo ATR, cường độ, độ lệch ``liq_heatmap_skew``
   và tổng giá trị thanh lý thật gần nhất.

BẢO ĐẢM KIẾN TRÚC: Chỉ phục vụ cổng bối cảnh tầng L4 và hiển thị chart đọc nhanh,
tuyệt đối không dùng làm ngòi nổ vào lệnh tại tầng L5.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.market.stream_events import LiquidationEvent
from trading_bot.domain.research.derivatives import DerivativesSnapshot
from trading_bot.domain.strategy.contracts import FeatureObservation, FeatureQuality

if TYPE_CHECKING:
    from trading_bot.domain.strategy.contracts import DecisionSnapshot

LIQUIDATION_HEATMAP_VERSION = "ind4-liq-heatmap-v1"

# Tên feature chuẩn khi gắn vào DecisionSnapshot.features
FEATURE_LIQ_NEAREST_SHORT_PRICE = "liq_heatmap_nearest_short_price"
FEATURE_LIQ_NEAREST_SHORT_ATR = "liq_heatmap_nearest_short_atr"
FEATURE_LIQ_SHORT_INTENSITY = "liq_heatmap_short_intensity"
FEATURE_LIQ_NEAREST_LONG_PRICE = "liq_heatmap_nearest_long_price"
FEATURE_LIQ_NEAREST_LONG_ATR = "liq_heatmap_nearest_long_atr"
FEATURE_LIQ_LONG_INTENSITY = "liq_heatmap_long_intensity"
FEATURE_LIQ_SKEW = "liq_heatmap_skew"
FEATURE_LIQ_REALIZED_LONG_NOTIONAL = "liq_heatmap_realized_long_notional"
FEATURE_LIQ_REALIZED_SHORT_NOTIONAL = "liq_heatmap_realized_short_notional"
FEATURE_LIQ_IS_STALE = "liq_heatmap_is_stale"

_DEFAULT_LEVERAGES: tuple[int, ...] = (10, 20, 25, 50, 100)
_DEFAULT_LEVERAGE_WEIGHTS: dict[int, float] = {
    10: 0.15,
    20: 0.25,
    25: 0.25,
    50: 0.20,
    100: 0.15,
}


@dataclass(frozen=True, slots=True)
class RealizedLiquidation:
    """Bản ghi thanh lý thật tối giản dùng cho tính toán domain thuần."""

    symbol: str
    side: str
    price: Decimal
    average_price: Decimal
    quantity: Decimal
    filled_quantity: Decimal
    event_at: datetime
    available_at: datetime

    @property
    def effective_price(self) -> Decimal:
        return self.average_price if self.average_price > Decimal("0") else self.price

    @property
    def notional(self) -> Decimal:
        qty = self.filled_quantity if self.filled_quantity > Decimal("0") else self.quantity
        return self.effective_price * qty

    @classmethod
    def from_event(cls, event: LiquidationEvent) -> RealizedLiquidation:
        return cls(
            symbol=event.symbol,
            side=event.side,
            price=event.price,
            average_price=event.average_price,
            quantity=event.quantity,
            filled_quantity=event.filled_quantity,
            event_at=event.trade_time,
            available_at=event.received_at,
        )


@dataclass(frozen=True, slots=True)
class LiquidationCluster:
    """Một cụm thanh lý ước lượng còn sống ở trên hoặc dưới giá hiện tại."""

    side: str  # "LONG" (bị thanh lý khi giá giảm) hoặc "SHORT" (bị thanh lý khi giá tăng)
    price: Decimal
    intensity: float
    leverage_hint: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "price": float(self.price),
            "intensity": self.intensity,
            "leverage_hint": self.leverage_hint,
        }


@dataclass(frozen=True, slots=True)
class LiquidationHeatmapConfig:
    """Cấu hình ước lượng cụm thanh lý và suy giảm theo thời gian/quét giá."""

    lookback_bars: int = 120
    min_bars: int = 20
    atr_period: int = 14
    swing_window: int = 3
    high_volume_multiplier: float = 1.35
    volume_baseline_window: int = 20
    leverages: tuple[int, ...] = _DEFAULT_LEVERAGES
    mmr: Decimal = Decimal("0.004")
    half_life_hours: float = 48.0
    realized_window: timedelta = timedelta(hours=4)
    bucket_atr_fraction: float = 0.25
    max_clusters_per_side: int = 12

    def __post_init__(self) -> None:
        if self.lookback_bars < 5:
            raise ValueError("lookback_bars phải >= 5.")
        if self.min_bars < 3:
            raise ValueError("min_bars phải >= 3.")
        if self.atr_period < 2:
            raise ValueError("atr_period phải >= 2.")
        if self.swing_window < 1:
            raise ValueError("swing_window phải >= 1.")
        if self.high_volume_multiplier <= 0:
            raise ValueError("high_volume_multiplier phải dương.")
        if self.volume_baseline_window < 2:
            raise ValueError("volume_baseline_window phải >= 2.")
        if not self.leverages or any(lev <= 1 for lev in self.leverages):
            raise ValueError("leverages phải khác rỗng và mọi mức > 1.")
        if not (Decimal("0") <= self.mmr < Decimal("0.2")):
            raise ValueError("mmr phải nằm trong [0, 0.2).")
        if self.half_life_hours <= 0:
            raise ValueError("half_life_hours phải dương.")
        if self.realized_window <= timedelta(0):
            raise ValueError("realized_window phải dương.")
        if self.bucket_atr_fraction <= 0:
            raise ValueError("bucket_atr_fraction phải dương.")
        if self.max_clusters_per_side < 1:
            raise ValueError("max_clusters_per_side phải >= 1.")


@dataclass(frozen=True, slots=True)
class LiquidationHeatmapObservation:
    """Quan sát bản đồ nhiệt thanh lý point-in-time tại ``as_of``."""

    symbol: str
    as_of: datetime
    is_stale: bool
    current_price: Decimal | None
    atr: Decimal | None
    long_liq_clusters: tuple[LiquidationCluster, ...]
    short_liq_clusters: tuple[LiquidationCluster, ...]
    nearest_short_liq_price: Decimal | None
    nearest_short_liq_atr: float | None
    short_liq_intensity: float
    nearest_long_liq_price: Decimal | None
    nearest_long_liq_atr: float | None
    long_liq_intensity: float
    liq_heatmap_skew: float | None
    recent_realized_liq_long_notional: Decimal
    recent_realized_liq_short_notional: Decimal
    formula_version: str = LIQUIDATION_HEATMAP_VERSION

    def to_metrics(self) -> dict[str, Any]:
        """Xuất từ điển chỉ số gọn, JSON-safe cho ContextGateResult và API chart."""
        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "is_stale": self.is_stale,
            "current_price": float(self.current_price) if self.current_price is not None else None,
            "atr": float(self.atr) if self.atr is not None else None,
            "long_liq_clusters": [c.to_dict() for c in self.long_liq_clusters],
            "short_liq_clusters": [c.to_dict() for c in self.short_liq_clusters],
            "nearest_short_liq_price": (
                float(self.nearest_short_liq_price)
                if self.nearest_short_liq_price is not None
                else None
            ),
            "nearest_short_liq_atr": self.nearest_short_liq_atr,
            "short_liq_intensity": self.short_liq_intensity,
            "nearest_long_liq_price": (
                float(self.nearest_long_liq_price)
                if self.nearest_long_liq_price is not None
                else None
            ),
            "nearest_long_liq_atr": self.nearest_long_liq_atr,
            "long_liq_intensity": self.long_liq_intensity,
            "liq_heatmap_skew": self.liq_heatmap_skew,
            "recent_realized_liq_long_notional": float(self.recent_realized_liq_long_notional),
            "recent_realized_liq_short_notional": float(self.recent_realized_liq_short_notional),
            "formula_version": self.formula_version,
        }

    def to_feature_observations(
        self,
        *,
        timeframe: str = "1h",
        snapshot_prefix: str = "liq-heatmap",
    ) -> tuple[FeatureObservation, ...]:
        """Đóng gói thành ``FeatureObservation`` chuẩn để gắn vào ``DecisionSnapshot.features``."""
        stamp = int(self.as_of.timestamp())

        def _make(name: str, value: float | None) -> FeatureObservation:
            if self.is_stale:
                quality = FeatureQuality.STALE
            elif value is None:
                quality = FeatureQuality.MISSING
            else:
                quality = FeatureQuality.READY
            return FeatureObservation(
                feature_id=f"{snapshot_prefix}:{self.symbol}:{stamp}:{name}",
                name=name,
                timeframe=timeframe,
                value=value,
                previous_value=None,
                available_at=self.as_of,
                quality=quality,
            )

        return (
            _make(
                FEATURE_LIQ_NEAREST_SHORT_PRICE,
                float(self.nearest_short_liq_price)
                if self.nearest_short_liq_price is not None
                else None,
            ),
            _make(FEATURE_LIQ_NEAREST_SHORT_ATR, self.nearest_short_liq_atr),
            _make(FEATURE_LIQ_SHORT_INTENSITY, self.short_liq_intensity),
            _make(
                FEATURE_LIQ_NEAREST_LONG_PRICE,
                float(self.nearest_long_liq_price)
                if self.nearest_long_liq_price is not None
                else None,
            ),
            _make(FEATURE_LIQ_NEAREST_LONG_ATR, self.nearest_long_liq_atr),
            _make(FEATURE_LIQ_LONG_INTENSITY, self.long_liq_intensity),
            _make(FEATURE_LIQ_SKEW, self.liq_heatmap_skew),
            _make(
                FEATURE_LIQ_REALIZED_LONG_NOTIONAL,
                float(self.recent_realized_liq_long_notional),
            ),
            _make(
                FEATURE_LIQ_REALIZED_SHORT_NOTIONAL,
                float(self.recent_realized_liq_short_notional),
            ),
            _make(FEATURE_LIQ_IS_STALE, 1.0 if self.is_stale else 0.0),
        )


def _compute_wilder_atr(bars: Sequence[OHLCVBar], period: int) -> Decimal | None:
    """Tính ATR theo chuẩn Wilder (1978) trên chuỗi nến đã sắp tăng dần."""
    if not bars:
        return None
    trs: list[float] = []
    for idx, bar in enumerate(bars):
        high = float(bar.high)
        low = float(bar.low)
        if idx == 0:
            trs.append(max(0.0, high - low))
        else:
            prev_close = float(bars[idx - 1].close)
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        atr_val = math.fsum(trs) / len(trs)
    else:
        atr_val = math.fsum(trs[:period]) / period
        for tr in trs[period:]:
            atr_val = ((period - 1) * atr_val + tr) / period
    if not math.isfinite(atr_val) or atr_val <= 0:
        return None
    return Decimal(str(round(atr_val, 8)))


def _bar_quote_volume(bar: OHLCVBar) -> float:
    if bar.quote_volume is not None and bar.quote_volume >= Decimal("0"):
        return float(bar.quote_volume)
    return float(bar.volume * bar.close)


def _oi_bonus_factor(
    bar: OHLCVBar,
    oi_series: Sequence[tuple[datetime, Decimal]],
) -> float:
    """Tính hệ số thưởng (> 1.0) nếu OI tăng trong khoảng nến ``bar``."""
    if len(oi_series) < 2:
        return 1.0
    prev_oi: Decimal | None = None
    curr_oi: Decimal | None = None
    for collected_at, oi_val in oi_series:
        if collected_at <= bar.open_time:
            prev_oi = oi_val
        elif collected_at <= bar.close_time:
            curr_oi = oi_val
    if prev_oi is None or curr_oi is None or prev_oi <= Decimal("0"):
        return 1.0
    delta_pct = float((curr_oi - prev_oi) / prev_oi)
    if delta_pct <= 0:
        return 1.0
    return 1.0 + min(delta_pct * 5.0, 2.0)


def _leverage_weight(leverage: int, leverages: Sequence[int]) -> float:
    if leverage in _DEFAULT_LEVERAGE_WEIGHTS and set(leverages) <= set(_DEFAULT_LEVERAGE_WEIGHTS):
        total = math.fsum(_DEFAULT_LEVERAGE_WEIGHTS[lev] for lev in leverages)
        return _DEFAULT_LEVERAGE_WEIGHTS[leverage] / total if total > 0 else 1.0 / len(leverages)
    return 1.0 / len(leverages)


def _normalize_liquidation(
    item: RealizedLiquidation | LiquidationEvent,
) -> RealizedLiquidation:
    if isinstance(item, RealizedLiquidation):
        return item
    return RealizedLiquidation.from_event(item)


def compute_liquidation_heatmap(
    bars: Sequence[OHLCVBar],
    *,
    symbol: str,
    as_of: datetime,
    liquidations: Sequence[RealizedLiquidation | LiquidationEvent] = (),
    oi_snapshots: Sequence[DerivativesSnapshot] = (),
    config: LiquidationHeatmapConfig | None = None,
) -> LiquidationHeatmapObservation:
    """Tính bản đồ nhiệt cụm thanh lý point-in-time tại ``as_of`` (không nhìn trước)."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of phải có múi giờ.")
    cfg = config or LiquidationHeatmapConfig()
    instant = as_of.astimezone(UTC)

    # Bất biến tiền tố: chỉ lấy nến đã đóng có close_time <= as_of và available_at <= as_of
    dedup_bars: dict[datetime, OHLCVBar] = {}
    for bar in bars:
        if bar.symbol != symbol or not bar.is_closed:
            continue
        if bar.close_time.astimezone(UTC) > instant or bar.available_at.astimezone(UTC) > instant:
            continue
        dedup_bars[bar.open_time.astimezone(UTC)] = bar

    sorted_bars = [dedup_bars[k] for k in sorted(dedup_bars)]

    # Lọc sự kiện thanh lý thật point-in-time
    eligible_liqs: list[RealizedLiquidation] = []
    for raw_liq in liquidations:
        liq = _normalize_liquidation(raw_liq)
        if liq.symbol != symbol:
            continue
        if liq.available_at.astimezone(UTC) > instant or liq.event_at.astimezone(UTC) > instant:
            continue
        eligible_liqs.append(liq)
    eligible_liqs.sort(key=lambda x: x.event_at)

    # Tính tổng notional thanh lý thật trong cửa sổ gần nhất
    window_start = instant - cfg.realized_window
    recent_long_notional = Decimal("0")
    recent_short_notional = Decimal("0")
    for liq in eligible_liqs:
        if liq.event_at.astimezone(UTC) >= window_start:
            if liq.side == "SELL":
                recent_long_notional += liq.notional
            elif liq.side == "BUY":
                recent_short_notional += liq.notional

    if len(sorted_bars) < cfg.min_bars:
        current_price = sorted_bars[-1].close if sorted_bars else None
        return LiquidationHeatmapObservation(
            symbol=symbol,
            as_of=instant,
            is_stale=True,
            current_price=current_price,
            atr=None,
            long_liq_clusters=(),
            short_liq_clusters=(),
            nearest_short_liq_price=None,
            nearest_short_liq_atr=None,
            short_liq_intensity=0.0,
            nearest_long_liq_price=None,
            nearest_long_liq_atr=None,
            long_liq_intensity=0.0,
            liq_heatmap_skew=None,
            recent_realized_liq_long_notional=recent_long_notional,
            recent_realized_liq_short_notional=recent_short_notional,
        )

    # Tính ATR nhân quả trên toàn chuỗi đến as_of rồi cắt cửa sổ lookback
    atr = _compute_wilder_atr(sorted_bars, cfg.atr_period)
    window_bars = sorted_bars[-cfg.lookback_bars :]
    current_price = window_bars[-1].close
    atr_float = (
        float(atr) if atr is not None and atr > Decimal("0") else float(current_price) * 0.01
    )

    # Chuẩn bị chuỗi OI point-in-time
    oi_dedup: dict[datetime, Decimal] = {}
    for snap in oi_snapshots:
        if snap.symbol != symbol or snap.oi_value_usdt is None:
            continue
        c_at = snap.collected_at.astimezone(UTC)
        p_at = snap.premium_time.astimezone(UTC)
        if c_at > instant or p_at > instant:
            continue
        if snap.oi_at is not None and snap.oi_at.astimezone(UTC) > instant:
            continue
        oi_dedup[c_at] = snap.oi_value_usdt
    oi_series = [(k, oi_dedup[k]) for k in sorted(oi_dedup)]

    # Bước 1: Tạo các cụm thanh lý ước lượng từ swing highs/lows đã xác nhận & nến volume cao
    raw_clusters: list[tuple[str, Decimal, float, int, int]] = []
    # (side, cluster_price, intensity, bar_index, leverage)
    n_bars = len(window_bars)
    qvols = [_bar_quote_volume(b) for b in window_bars]

    for idx, bar in enumerate(window_bars):
        # Baseline volume nhân quả từ các nến trước idx
        start_v = max(0, idx - cfg.volume_baseline_window)
        prior_vols = qvols[start_v:idx]
        avg_vol = math.fsum(prior_vols) / len(prior_vols) if prior_vols else qvols[idx]
        vol_ratio = qvols[idx] / avg_vol if avg_vol > 0 else 1.0
        is_high_vol = vol_ratio >= cfg.high_volume_multiplier

        # Swing chỉ xác nhận nhân quả khi có đủ swing_window nến bên phải (<= as_of)
        can_confirm_swing = idx >= cfg.swing_window and (idx + cfg.swing_window) <= n_bars - 1
        is_swing_low = False
        is_swing_high = False
        if can_confirm_swing:
            neighborhood = window_bars[idx - cfg.swing_window : idx + cfg.swing_window + 1]
            is_swing_low = all(bar.low <= nb.low for nb in neighborhood)
            is_swing_high = all(bar.high >= nb.high for nb in neighborhood)

        if not (is_swing_low or is_swing_high or is_high_vol):
            continue

        oi_factor = _oi_bonus_factor(bar, oi_series)
        # Chuẩn hóa cường độ cơ sở theo tỷ lệ volume tương đối * hệ số OI (đơn vị điểm cường độ)
        base_strength = max(vol_ratio, 1.0) * oi_factor * 100.0
        if is_swing_low or is_swing_high:
            base_strength *= 1.5

        if is_swing_low or is_high_vol:
            anchor_long = bar.low if is_swing_low else (bar.low + bar.close) / Decimal("2")
            for lev in cfg.leverages:
                liq_price = anchor_long * (Decimal("1") - Decimal("1") / Decimal(lev) + cfg.mmr)
                if liq_price > Decimal("0"):
                    weight = _leverage_weight(lev, cfg.leverages)
                    raw_clusters.append(("LONG", liq_price, base_strength * weight, idx, lev))

        if is_swing_high or is_high_vol:
            anchor_short = bar.high if is_swing_high else (bar.high + bar.close) / Decimal("2")
            for lev in cfg.leverages:
                liq_price = anchor_short * (Decimal("1") + Decimal("1") / Decimal(lev) - cfg.mmr)
                if liq_price > Decimal("0"):
                    weight = _leverage_weight(lev, cfg.leverages)
                    raw_clusters.append(("SHORT", liq_price, base_strength * weight, idx, lev))

    # Bước 2: Sweep & Decay nhân quả theo nến sau khi hình thành và theo forceOrder thật
    tolerance = Decimal(str(round(atr_float * 0.25, 8)))
    surviving_long: list[LiquidationCluster] = []
    surviving_short: list[LiquidationCluster] = []

    for side, cluster_price, base_intensity, bar_idx, lev in raw_clusters:
        # Chỉ xét cụm nằm đúng phía so với giá hiện tại
        if side == "LONG" and cluster_price >= current_price:
            continue
        if side == "SHORT" and cluster_price <= current_price:
            continue

        # Kiểm tra giá nến sau bar_idx đã quét qua mức thanh lý chưa
        swept = False
        for later_bar in window_bars[bar_idx + 1 :]:
            if side == "LONG" and later_bar.low <= cluster_price:
                swept = True
                break
            if side == "SHORT" and later_bar.high >= cluster_price:
                swept = True
                break
        if swept:
            continue

        # Suy giảm theo thời gian (half-life)
        age_hours = max(
            0.0,
            (instant - window_bars[bar_idx].close_time.astimezone(UTC)).total_seconds() / 3600.0,
        )
        time_decay = 0.5 ** (age_hours / cfg.half_life_hours)
        remaining = base_intensity * time_decay

        # Suy giảm thêm nếu có sự kiện thanh lý thật forceOrder ở vùng giá cụm sau khi hình thành
        bar_open = window_bars[bar_idx].open_time.astimezone(UTC)
        expected_liq_side = "SELL" if side == "LONG" else "BUY"
        matched_notional = Decimal("0")
        for liq in eligible_liqs:
            if liq.side == expected_liq_side and liq.event_at.astimezone(UTC) > bar_open:
                if abs(liq.effective_price - cluster_price) <= tolerance:
                    matched_notional += liq.notional
        if matched_notional > Decimal("0"):
            # Giảm cường độ tỷ lệ theo notional thanh lý thật (tối đa xóa sạch cụm)
            ref_notional = max(_bar_quote_volume(window_bars[bar_idx]) * 0.01, 100_000.0)
            liq_decay = max(0.0, 1.0 - float(matched_notional) / ref_notional)
            remaining *= liq_decay

        if remaining <= 1.0:
            continue

        cluster = LiquidationCluster(
            side=side,
            price=cluster_price.quantize(Decimal("0.01")),
            intensity=round(remaining, 4),
            leverage_hint=lev,
        )
        if side == "LONG":
            surviving_long.append(cluster)
        else:
            surviving_short.append(cluster)

    # Bước 3: Gom nhóm cụm gần nhau theo bucket_size và tổng hợp chỉ số
    bucket_size = max(atr_float * cfg.bucket_atr_fraction, float(current_price) * 0.001)
    long_clusters = _aggregate_clusters(
        surviving_long,
        side="LONG",
        bucket_size=bucket_size,
        max_count=cfg.max_clusters_per_side,
        descending=True,  # gần current_price nhất lên đầu
    )
    short_clusters = _aggregate_clusters(
        surviving_short,
        side="SHORT",
        bucket_size=bucket_size,
        max_count=cfg.max_clusters_per_side,
        descending=False,  # gần current_price nhất lên đầu
    )

    nearest_short_price = short_clusters[0].price if short_clusters else None
    nearest_long_price = long_clusters[0].price if long_clusters else None

    nearest_short_atr = (
        round(float(nearest_short_price - current_price) / atr_float, 4)
        if nearest_short_price is not None and atr_float > 0
        else None
    )
    nearest_long_atr = (
        round(float(current_price - nearest_long_price) / atr_float, 4)
        if nearest_long_price is not None and atr_float > 0
        else None
    )

    short_intensity = round(math.fsum(c.intensity for c in short_clusters), 4)
    long_intensity = round(math.fsum(c.intensity for c in long_clusters), 4)
    total_intensity = short_intensity + long_intensity
    skew = (
        round((short_intensity - long_intensity) / total_intensity, 6)
        if total_intensity > 0
        else None
    )

    return LiquidationHeatmapObservation(
        symbol=symbol,
        as_of=instant,
        is_stale=False,
        current_price=current_price,
        atr=atr,
        long_liq_clusters=long_clusters,
        short_liq_clusters=short_clusters,
        nearest_short_liq_price=nearest_short_price,
        nearest_short_liq_atr=nearest_short_atr,
        short_liq_intensity=short_intensity,
        nearest_long_liq_price=nearest_long_price,
        nearest_long_liq_atr=nearest_long_atr,
        long_liq_intensity=long_intensity,
        liq_heatmap_skew=skew,
        recent_realized_liq_long_notional=recent_long_notional,
        recent_realized_liq_short_notional=recent_short_notional,
    )


def _aggregate_clusters(
    clusters: Sequence[LiquidationCluster],
    *,
    side: str,
    bucket_size: float,
    max_count: int,
    descending: bool,
) -> tuple[LiquidationCluster, ...]:
    if not clusters:
        return ()
    buckets: dict[int, list[LiquidationCluster]] = {}
    for cluster in clusters:
        idx = int(float(cluster.price) / bucket_size) if bucket_size > 0 else 0
        buckets.setdefault(idx, []).append(cluster)

    merged: list[LiquidationCluster] = []
    for items in buckets.values():
        total_int = math.fsum(c.intensity for c in items)
        if total_int <= 0:
            continue
        weighted_price = math.fsum(float(c.price) * c.intensity for c in items) / total_int
        dominant = max(items, key=lambda c: c.intensity)
        merged.append(
            LiquidationCluster(
                side=side,
                price=Decimal(str(round(weighted_price, 2))),
                intensity=round(total_int, 4),
                leverage_hint=dominant.leverage_hint,
            )
        )
    merged.sort(key=lambda c: c.price, reverse=descending)
    return tuple(merged[:max_count])


def observation_from_snapshot_features(
    snapshot: DecisionSnapshot,
) -> LiquidationHeatmapObservation | None:
    """Khôi phục ``LiquidationHeatmapObservation`` từ các feature ``liq_heatmap_*`` trên snapshot.

    Bảo vệ point-in-time: bỏ qua mọi feature có ``available_at > snapshot.decision_time``.
    """
    by_name: dict[str, FeatureObservation] = {}
    for feat in snapshot.features:
        if feat.name.startswith("liq_heatmap_") and feat.available_at <= snapshot.decision_time:
            existing = by_name.get(feat.name)
            if existing is None or feat.available_at >= existing.available_at:
                by_name[feat.name] = feat

    if not by_name:
        return None

    stale_feat = by_name.get(FEATURE_LIQ_IS_STALE)
    is_stale = any(f.quality is FeatureQuality.STALE for f in by_name.values()) or (
        stale_feat is not None and stale_feat.value is not None and stale_feat.value >= 0.5
    )

    def _ready_float(name: str) -> float | None:
        feat = by_name.get(name)
        if feat is None or feat.quality is not FeatureQuality.READY or feat.value is None:
            return None
        return float(feat.value)

    if is_stale:
        return LiquidationHeatmapObservation(
            symbol=snapshot.symbol,
            as_of=snapshot.decision_time.astimezone(UTC),
            is_stale=True,
            current_price=None,
            atr=None,
            long_liq_clusters=(),
            short_liq_clusters=(),
            nearest_short_liq_price=None,
            nearest_short_liq_atr=None,
            short_liq_intensity=0.0,
            nearest_long_liq_price=None,
            nearest_long_liq_atr=None,
            long_liq_intensity=0.0,
            liq_heatmap_skew=None,
            recent_realized_liq_long_notional=Decimal("0"),
            recent_realized_liq_short_notional=Decimal("0"),
        )

    ns_price = _ready_float(FEATURE_LIQ_NEAREST_SHORT_PRICE)
    ns_atr = _ready_float(FEATURE_LIQ_NEAREST_SHORT_ATR)
    s_int = _ready_float(FEATURE_LIQ_SHORT_INTENSITY) or 0.0
    nl_price = _ready_float(FEATURE_LIQ_NEAREST_LONG_PRICE)
    nl_atr = _ready_float(FEATURE_LIQ_NEAREST_LONG_ATR)
    l_int = _ready_float(FEATURE_LIQ_LONG_INTENSITY) or 0.0
    skew = _ready_float(FEATURE_LIQ_SKEW)
    r_long = _ready_float(FEATURE_LIQ_REALIZED_LONG_NOTIONAL) or 0.0
    r_short = _ready_float(FEATURE_LIQ_REALIZED_SHORT_NOTIONAL) or 0.0

    short_clusters = (
        (
            LiquidationCluster(
                side="SHORT",
                price=Decimal(str(ns_price)),
                intensity=s_int,
            ),
        )
        if ns_price is not None and s_int > 0
        else ()
    )
    long_clusters = (
        (
            LiquidationCluster(
                side="LONG",
                price=Decimal(str(nl_price)),
                intensity=l_int,
            ),
        )
        if nl_price is not None and l_int > 0
        else ()
    )

    return LiquidationHeatmapObservation(
        symbol=snapshot.symbol,
        as_of=snapshot.decision_time.astimezone(UTC),
        is_stale=False,
        current_price=None,
        atr=None,
        long_liq_clusters=long_clusters,
        short_liq_clusters=short_clusters,
        nearest_short_liq_price=Decimal(str(ns_price)) if ns_price is not None else None,
        nearest_short_liq_atr=ns_atr,
        short_liq_intensity=s_int,
        nearest_long_liq_price=Decimal(str(nl_price)) if nl_price is not None else None,
        nearest_long_liq_atr=nl_atr,
        long_liq_intensity=l_int,
        liq_heatmap_skew=skew,
        recent_realized_liq_long_notional=Decimal(str(r_long)),
        recent_realized_liq_short_notional=Decimal(str(r_short)),
    )


__all__ = [
    "FEATURE_LIQ_IS_STALE",
    "FEATURE_LIQ_LONG_INTENSITY",
    "FEATURE_LIQ_NEAREST_LONG_ATR",
    "FEATURE_LIQ_NEAREST_LONG_PRICE",
    "FEATURE_LIQ_NEAREST_SHORT_ATR",
    "FEATURE_LIQ_NEAREST_SHORT_PRICE",
    "FEATURE_LIQ_REALIZED_LONG_NOTIONAL",
    "FEATURE_LIQ_REALIZED_SHORT_NOTIONAL",
    "FEATURE_LIQ_SHORT_INTENSITY",
    "FEATURE_LIQ_SKEW",
    "LIQUIDATION_HEATMAP_VERSION",
    "LiquidationCluster",
    "LiquidationHeatmapConfig",
    "LiquidationHeatmapObservation",
    "RealizedLiquidation",
    "compute_liquidation_heatmap",
    "observation_from_snapshot_features",
]
