"""Các rule chiến lược định lượng mở rộng (truyền thống & nâng cao Crypto Futures).

Module tách biệt khỏi `rules.py`, `rules_v2.py` và `scalping_rules.py` để giữ bất biến hash
các bản phát hành cũ. Chỉ đăng ký vào `StrategyRegistry` sản phẩm qua
`application/strategy_catalog.py`.

Danh mục phát hành trong module:
- `h02-v1`: Time-series momentum đa chân trời + hàng rào biến động thực tế (4h).
- `h04-v1`: Bollinger–Keltner volatility squeeze breakout có xác nhận khối lượng (15m).
- `h05-v1`: Tiếp diễn xu hướng tại nhịp hồi EMA20 + RSI [40, 60] (1h + 4h).
- `h07-v1`: Hồi quy Session VWAP trong ngày UTC có ngưỡng chi phí 3× (5m).
- `h21-v1`: Động lượng biểu đồ MACD lọc nhiễu theo ADX + ±DI + 4h EMA200 (1h + 4h).
- `h22-v1`: Phá vỡ thanh khoản xác nhận bằng Taker Order-Flow Imbalance (15m + 1h).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, timedelta
from statistics import median

from trading_bot.domain.market.features import atr, bollinger_bands, ema, macd
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    NoTrade,
    SeriesQuality,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.rules import release_content_hash


def _bar_key(symbol: str, bar: OHLCVBar) -> str:
    return f"{symbol}_{bar.timeframe}_{int(bar.open_time.timestamp())}"


def _ready_feature(
    snapshot: DecisionSnapshot,
    name: str,
    timeframe: str,
) -> FeatureObservation | None:
    obs = snapshot.get_feature(name, timeframe)
    if obs is None or obs.quality is not FeatureQuality.READY:
        return None
    if obs.available_at > snapshot.decision_time:
        return None
    if obs.value is None or not math.isfinite(obs.value):
        return None
    return obs


def _ready_series(
    snapshot: DecisionSnapshot,
    timeframe: str,
    *,
    min_bars: int = 1,
) -> tuple[MarketSeries | None, NoTrade | None]:
    series = snapshot.get_series(timeframe)
    if (
        series is None
        or series.quality is not SeriesQuality.READY
        or series.backfill_complete is not True
    ):
        return None, NoTrade(
            strategy_id="",
            code="SERIES_NOT_READY",
            detail_vi=f"Chuỗi nến khung {timeframe} chưa sẵn sàng để đánh giá.",
            missing_features=(timeframe,),
        )
    if not series.bars:
        return None, NoTrade(
            strategy_id="",
            code="WARMUP_INCOMPLETE",
            detail_vi=f"Chuỗi nến khung {timeframe} rỗng.",
            missing_features=(timeframe,),
        )
    latest = series.bars[-1]
    if not latest.is_closed or latest.available_at > snapshot.decision_time:
        return None, NoTrade(
            strategy_id="",
            code="BAR_UNAVAILABLE",
            detail_vi=f"Nến cuối khung {timeframe} chưa đóng hoặc chưa khả dụng tại decision_time.",
        )
    if len(series.bars) < min_bars:
        return None, NoTrade(
            strategy_id="",
            code="WARMUP_INCOMPLETE",
            detail_vi=(
                f"Khung {timeframe} mới có {len(series.bars)} nến, "
                f"cần tối thiểu {min_bars} nến warmup."
            ),
            missing_features=(f"{timeframe}:bars<{min_bars}",),
        )
    return series, None


def _precheck(
    strategy_id: str,
    release_id: str,
    snapshot: DecisionSnapshot,
    settings: StrategySelection,
    *,
    entry_timeframe: str,
    min_bars: int,
    require_flat: bool = True,
    respect_macro_blackout: bool = True,
) -> tuple[MarketSeries | None, NoTrade | None]:
    if not settings.published or release_id not in settings.active_release_ids:
        return None, NoTrade(
            strategy_id=strategy_id,
            code="STRATEGY_NOT_ACTIVE",
            detail_vi=f"Bản phát hành {release_id} chưa được chọn trong settings đã công bố.",
        )
    if respect_macro_blackout and snapshot.macro_blackout is True:
        return None, NoTrade(
            strategy_id=strategy_id,
            code="MACRO_BLACKOUT",
            detail_vi="Đang trong cửa sổ cấm giao dịch theo lịch sự kiện vĩ mô.",
        )
    if require_flat:
        if snapshot.position_is_flat is False:
            return None, NoTrade(
                strategy_id=strategy_id,
                code="NOT_FLAT",
                detail_vi="Đã có vị thế mở hoặc lệnh đang hoạt động; không mở thêm vị thế mới.",
            )
        if snapshot.position_is_flat is None:
            return None, NoTrade(
                strategy_id=strategy_id,
                code="POSITION_STATE_UNKNOWN",
                detail_vi="Trạng thái vị thế hiện tại chưa được xác định rõ ràng.",
            )
    series, err = _ready_series(snapshot, entry_timeframe, min_bars=min_bars)
    if err is not None:
        return None, NoTrade(
            strategy_id=strategy_id,
            code=err.code,
            detail_vi=err.detail_vi,
            missing_features=err.missing_features,
        )
    return series, None


def _build_candidate(
    *,
    rule: object,
    strategy_id: str,
    release_id: str,
    recipe: ExitRecipe,
    snapshot: DecisionSnapshot,
    settings: StrategySelection,
    entry_bar: OHLCVBar,
    direction: str,
    atr_value: float,
    valid_duration: timedelta,
    evidence_features: tuple[FeatureObservation, ...],
    evidence_bars: tuple[OHLCVBar, ...],
    reason_code: str,
    detail_vi: str,
) -> StrategyCandidate:
    signal_at = entry_bar.available_at
    # Đồng nhất với rules.py và BacktestDatasetRuntimeContext (evaluated_at = decision_time + 1µs).
    eligible_after = signal_at + timedelta(microseconds=1)
    expires_at = signal_at + valid_duration
    feature_ids = tuple(dict.fromkeys(obs.feature_id for obs in evidence_features))
    bar_keys = tuple(dict.fromkeys(_bar_key(snapshot.symbol, bar) for bar in evidence_bars))
    return StrategyCandidate(
        strategy_id=strategy_id,
        strategy_release_id=release_id,
        release_content_hash=release_content_hash(rule),
        settings_version_id=settings.settings_version_id,
        snapshot_id=snapshot.snapshot_id,
        symbol=snapshot.symbol,
        direction=direction,
        reference_price=entry_bar.close,
        atr_value=atr_value,
        signal_at=signal_at,
        eligible_after=eligible_after,
        expires_at=expires_at,
        recipe=recipe,
        evidence_feature_ids=feature_ids,
        evidence_bar_keys=bar_keys,
        macro_evidence_ids=snapshot.macro_evidence_ids,
        geometry_version="expanded-v1",
        reason_code=reason_code,
        detail_vi=detail_vi,
    )


def _percentile_threshold(values: list[float], percentile: float) -> float:
    """Tính phân vị điểm-thời-gian trên lịch sử quá khứ đã sắp xếp."""
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(math.floor((percentile / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


# ---------------------------------------------------------------------------
# 1. H02 — Time-Series Momentum đa chân trời (4h)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class H02TimeSeriesMomentumRule:
    """Động lượng chuỗi thời gian đa chân trời 4h (42/126/252 nến) kèm hàng rào biến động RV20."""

    strategy_id: str = "H02"
    release_id: str = "h02-v1"
    recipe: ExitRecipe = ExitRecipe(
        recipe_id="h02-exit-v1",
        stop_atr_multiple=3.0,
        trailing_atr_multiple=4.0,
        opposite_signal_exit=True,
        max_hold_bars=252,
    )
    horizons: tuple[int, int, int] = (42, 126, 252)
    min_rv: float = 0.002
    max_rv: float = 0.08

    @property
    def content_hash(self) -> str:
        return release_content_hash(self)

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        min_bars = max(self.horizons) + 1
        series, err = _precheck(
            self.strategy_id,
            self.release_id,
            snapshot,
            settings,
            entry_timeframe="4h",
            min_bars=min_bars,
        )
        if err is not None:
            return err
        assert series is not None

        atr_obs = _ready_feature(snapshot, "atr_14", "4h")
        rv_obs = _ready_feature(snapshot, "realized_volatility_20", "4h")
        missing = [
            name
            for name, obs in (("4h:atr_14", atr_obs), ("4h:realized_volatility_20", rv_obs))
            if obs is None or (obs.value or 0.0) <= 0.0
        ]
        if missing:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="MISSING_FEATURES",
                detail_vi="Thiếu feature ATR14 hoặc RV20 hợp lệ trên khung 4h.",
                missing_features=tuple(missing),
            )
        assert atr_obs is not None
        assert atr_obs.value is not None
        assert rv_obs is not None
        assert rv_obs.value is not None

        if not (self.min_rv <= rv_obs.value <= self.max_rv):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="VOLATILITY_OUT_OF_RANGE",
                detail_vi=(
                    f"RV20 hiện tại ({rv_obs.value:.4f}) nằm ngoài dải an toàn "
                    f"[{self.min_rv}, {self.max_rv}]."
                ),
            )

        bars = series.bars
        current_close = float(bars[-1].close)
        signs: list[int] = []
        for lookback in self.horizons:
            past_close = float(bars[-1 - lookback].close)
            if current_close > past_close:
                signs.append(1)
            elif current_close < past_close:
                signs.append(-1)
            else:
                signs.append(0)

        score = sum(signs) / len(signs)
        pos_count = sum(1 for s in signs if s > 0)
        neg_count = sum(1 for s in signs if s < 0)

        if pos_count >= 2 and score >= (1.0 / 3.0) - 1e-9:
            direction = "long"
        elif neg_count >= 2 and score <= -(1.0 / 3.0) + 1e-9:
            direction = "short"
        else:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi=f"Động lượng đa chân trời chưa đồng thuận (m={score:.2f}).",
            )

        return _build_candidate(
            rule=self,
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            recipe=self.recipe,
            snapshot=snapshot,
            settings=settings,
            entry_bar=bars[-1],
            direction=direction,
            atr_value=atr_obs.value,
            valid_duration=timedelta(hours=4),
            evidence_features=(atr_obs, rv_obs),
            evidence_bars=(bars[-1],),
            reason_code="H02_TSMOM_CONFIRMED",
            detail_vi=(
                f"H02 TSMOM {direction.upper()} đạt m={score:+.2f}, RV20={rv_obs.value:.4f}."
            ),
        )


# ---------------------------------------------------------------------------
# 2. H04 — Bollinger–Keltner Volatility Squeeze Breakout (15m)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class H04VolatilitySqueezeBreakoutRule:
    """Chiến lược nén biến động Bollinger–Keltner tại t-1 rồi bùng nổ phá vỡ có khối lượng tại t."""

    strategy_id: str = "H04"
    release_id: str = "h04-v1"
    recipe: ExitRecipe = ExitRecipe(
        recipe_id="h04-exit-v1",
        stop_atr_multiple=1.5,
        take_profit_atr_multiple=3.0,
        trailing_atr_multiple=2.0,
        max_hold_bars=32,
    )
    lookback: int = 20
    keltner_atr_multiple: float = 1.5
    volume_multiplier: float = 1.5
    bandwidth_percentile: float = 25.0

    @property
    def content_hash(self) -> str:
        return release_content_hash(self)

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        series, err = _precheck(
            self.strategy_id,
            self.release_id,
            snapshot,
            settings,
            entry_timeframe="15m",
            min_bars=60,
        )
        if err is not None:
            return err
        assert series is not None

        if series.quote_volumes is None or len(series.quote_volumes) != len(series.bars):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="QUOTE_VOLUME_UNAVAILABLE",
                detail_vi="Thiếu dữ liệu quote_volume để xác nhận khối lượng phá vỡ.",
            )

        atr_obs = _ready_feature(snapshot, "atr_14", "15m")
        if atr_obs is None or (atr_obs.value or 0.0) <= 0.0:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="MISSING_FEATURES",
                detail_vi="Thiếu feature ATR14 hợp lệ trên khung 15m.",
                missing_features=("15m:atr_14",),
            )
        assert atr_obs.value is not None

        bars = series.bars
        closes = [float(b.close) for b in bars]
        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]

        bb_mid, bb_upper, bb_lower = bollinger_bands(closes, period=self.lookback, deviations=2.0)
        ema20 = ema(closes, self.lookback)
        atr14 = atr(highs, lows, closes, 14)

        bw_series: list[float | None] = []
        for u, m, low_b in zip(bb_upper, bb_mid, bb_lower, strict=True):
            if u is None or m is None or low_b is None or m <= 0.0:
                bw_series.append(None)
            else:
                bw_series.append((u - low_b) / m)

        cur_idx = len(bars) - 1
        prev_idx = cur_idx - 1
        bw_cur = bw_series[cur_idx]
        bw_prev = bw_series[prev_idx]
        bb_u_cur = bb_upper[cur_idx]
        bb_l_cur = bb_lower[cur_idx]
        bb_u_prev = bb_upper[prev_idx]
        bb_l_prev = bb_lower[prev_idx]
        ema_prev = ema20[prev_idx]
        atr_prev = atr14[prev_idx]

        if (
            bw_cur is None
            or bw_prev is None
            or bb_u_cur is None
            or bb_l_cur is None
            or bb_u_prev is None
            or bb_l_prev is None
            or ema_prev is None
            or atr_prev is None
        ):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="WARMUP_INCOMPLETE",
                detail_vi="Chưa đủ chuỗi tính Bollinger BandWidth và Keltner Channel.",
            )

        # Kiểm tra nén tại t-1 (không dùng thông tin nến t để tính phân vị!)
        bw_history = [v for v in bw_series[max(0, prev_idx - 240) : prev_idx] if v is not None]
        if len(bw_history) < 20:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="WARMUP_INCOMPLETE",
                detail_vi="Chưa đủ lịch sử BandWidth quá khứ (tối thiểu 20 mẫu trước t-1).",
            )
        kc_upper_prev = ema_prev + self.keltner_atr_multiple * atr_prev
        kc_lower_prev = ema_prev - self.keltner_atr_multiple * atr_prev
        in_kc_squeeze = bb_u_prev < kc_upper_prev and bb_l_prev > kc_lower_prev
        in_pct_squeeze = bw_prev <= _percentile_threshold(bw_history, self.bandwidth_percentile)
        if not (in_kc_squeeze or in_pct_squeeze):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi="Không có trạng thái nén biến động BB–Keltner tại t-1.",
            )

        if bw_cur <= bw_prev:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi="BandWidth chưa mở rộng tại nến hiện tại.",
            )

        # Khối lượng và đỉnh/đáy lịch sử STRICTLY trước nến t: [t-20 : t]
        prior_qvols = list(series.quote_volumes[-1 - self.lookback : -1])
        prior_median_qvol = median(prior_qvols)
        cur_qvol = series.quote_volumes[-1]
        if prior_median_qvol <= 0.0 or cur_qvol <= self.volume_multiplier * prior_median_qvol:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi=(
                    f"Khối lượng quote ({cur_qvol:.2f}) chưa đạt "
                    f"{self.volume_multiplier}× trung vị 20 nến trước ({prior_median_qvol:.2f})."
                ),
            )

        hh20_prev = max(float(b.high) for b in bars[-1 - self.lookback : -1])
        ll20_prev = min(float(b.low) for b in bars[-1 - self.lookback : -1])
        close_cur = float(bars[-1].close)

        if close_cur > hh20_prev and close_cur > bb_u_cur:
            direction = "long"
        elif close_cur < ll20_prev and close_cur < bb_l_cur:
            direction = "short"
        else:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi="Giá đóng cửa chưa phá dải ngoài cùng biên Donchian 20 nến.",
            )

        return _build_candidate(
            rule=self,
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            recipe=self.recipe,
            snapshot=snapshot,
            settings=settings,
            entry_bar=bars[-1],
            direction=direction,
            atr_value=atr_obs.value,
            valid_duration=timedelta(minutes=15),
            evidence_features=(atr_obs,),
            evidence_bars=(bars[-1],),
            reason_code="H04_SQUEEZE_BREAKOUT",
            detail_vi=(
                f"H04 Squeeze Breakout {direction.upper()}: BW {bw_prev:.4f}→{bw_cur:.4f}, "
                f"Vol {cur_qvol / prior_median_qvol:.2f}× median."
            ),
        )


# ---------------------------------------------------------------------------
# 3. H05 — EMA Trend Pullback + RSI Regime Continuation (1h + 4h)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class H05TrendPullbackRule:
    """Tiếp diễn xu hướng đa khung khi giá điều chỉnh về EMA20 trong vùng RSI [40, 60]."""

    strategy_id: str = "H05"
    release_id: str = "h05-v1"
    recipe: ExitRecipe = ExitRecipe(
        recipe_id="h05-exit-v1",
        stop_atr_multiple=2.0,
        take_profit_atr_multiple=3.0,
        opposite_signal_exit=True,
        max_hold_bars=48,
    )
    min_adx: float = 18.0
    rsi_low: float = 40.0
    rsi_high: float = 60.0

    @property
    def content_hash(self) -> str:
        return release_content_hash(self)

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        series_1h, err = _precheck(
            self.strategy_id,
            self.release_id,
            snapshot,
            settings,
            entry_timeframe="1h",
            min_bars=20,
        )
        if err is not None:
            return err
        series_4h, err_4h = _ready_series(snapshot, "4h", min_bars=1)
        if err_4h is not None:
            return NoTrade(
                strategy_id=self.strategy_id,
                code=err_4h.code,
                detail_vi=err_4h.detail_vi,
                missing_features=err_4h.missing_features,
            )
        assert series_1h is not None
        assert series_4h is not None

        atr_obs = _ready_feature(snapshot, "atr_14", "1h")
        ema20_obs = _ready_feature(snapshot, "ema_20", "1h")
        ema80_obs = _ready_feature(snapshot, "ema_80", "1h")
        rsi_obs = _ready_feature(snapshot, "rsi_14", "1h")
        adx_obs = _ready_feature(snapshot, "adx_14", "1h")
        ema200_4h_obs = _ready_feature(snapshot, "ema_200", "4h")

        required = (
            ("1h:atr_14", atr_obs),
            ("1h:ema_20", ema20_obs),
            ("1h:ema_80", ema80_obs),
            ("1h:rsi_14", rsi_obs),
            ("1h:adx_14", adx_obs),
            ("4h:ema_200", ema200_4h_obs),
        )
        missing = [key for key, obs in required if obs is None]
        if missing or (atr_obs and (atr_obs.value or 0.0) <= 0.0):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="MISSING_FEATURES",
                detail_vi="Thiếu đặc trưng EMA/RSI/ADX (1h) hoặc EMA200 (4h) cho H05.",
                missing_features=tuple(missing or ("1h:atr_14",)),
            )
        assert atr_obs
        assert atr_obs.value is not None
        assert ema20_obs
        assert ema20_obs.value is not None
        assert ema80_obs
        assert ema80_obs.value is not None
        assert rsi_obs
        assert rsi_obs.value is not None
        assert adx_obs
        assert adx_obs.value is not None
        assert ema200_4h_obs
        assert ema200_4h_obs.value is not None

        if adx_obs.value < self.min_adx:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="TREND_TOO_WEAK",
                detail_vi=(
                    f"ADX14 ({adx_obs.value:.2f}) < {self.min_adx}; không đủ sức mạnh xu hướng."
                ),
            )
        if not (self.rsi_low <= rsi_obs.value <= self.rsi_high):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi=f"RSI14 ({rsi_obs.value:.2f}) nằm ngoài vùng nhịp hồi [40, 60].",
            )

        bars_1h = series_1h.bars
        bar_t = bars_1h[-1]
        bar_4h = series_4h.bars[-1]

        # Xác định độ dốc EMA80 (từ previous_value nếu có, hoặc tự tính khi >= 83 nến)
        if ema80_obs.previous_value is not None and ema80_obs.previous_value != ema80_obs.value:
            ema80_slope = ema80_obs.value - ema80_obs.previous_value
        elif len(bars_1h) >= 83:
            ema80_series = ema([float(b.close) for b in bars_1h], 80)
            cur_80 = ema80_series[-1]
            past_80 = ema80_series[-4]
            ema80_slope = (
                (cur_80 - past_80) if (cur_80 is not None and past_80 is not None) else 0.0
            )
        else:
            ema80_slope = 0.0

        close_t = float(bar_t.close)
        open_t = float(bar_t.open)
        high_t = float(bar_t.high)
        low_t = float(bar_t.low)
        close_4h = float(bar_4h.close)

        if (
            close_4h > ema200_4h_obs.value
            and ema20_obs.value > ema80_obs.value
            and ema80_slope > 0.0
            and low_t <= ema20_obs.value
            and close_t > ema20_obs.value
            and close_t > open_t
        ):
            direction = "long"
        elif (
            close_4h < ema200_4h_obs.value
            and ema20_obs.value < ema80_obs.value
            and ema80_slope < 0.0
            and high_t >= ema20_obs.value
            and close_t < ema20_obs.value
            and close_t < open_t
        ):
            direction = "short"
        else:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi="Chưa thỏa điều kiện nhịp hồi chạm và giữ EMA20 đồng thuận đa khung.",
            )

        return _build_candidate(
            rule=self,
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            recipe=self.recipe,
            snapshot=snapshot,
            settings=settings,
            entry_bar=bar_t,
            direction=direction,
            atr_value=atr_obs.value,
            valid_duration=timedelta(minutes=30),
            evidence_features=(
                atr_obs,
                ema20_obs,
                ema80_obs,
                rsi_obs,
                adx_obs,
                ema200_4h_obs,
            ),
            evidence_bars=(bar_t, bar_4h),
            reason_code="H05_PULLBACK_CONTINUATION",
            detail_vi=(
                f"H05 Trend Pullback {direction.upper()}: RSI={rsi_obs.value:.1f}, "
                f"EMA20={ema20_obs.value:.2f}, ADX={adx_obs.value:.1f}."
            ),
        )


# ---------------------------------------------------------------------------
# 4. H07 — Intraday Session VWAP & ATR Deviation Reversion (5m)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class H07SessionVwapReversionRule:
    """Hồi quy về Session VWAP UTC khi giá lệch >= 2 ATR trong biên ngang và qua ngưỡng phí 3×."""

    strategy_id: str = "H07"
    release_id: str = "h07-v1"
    recipe: ExitRecipe = ExitRecipe(
        recipe_id="h07-exit-v1",
        stop_atr_multiple=1.5,
        take_profit_atr_multiple=2.25,
        max_hold_bars=12,
    )
    min_session_bars: int = 12
    max_adx: float = 20.0
    max_er: float = 0.30
    min_atr_deviation: float = 2.0
    min_cost_hurdle_fraction: float = 0.003  # 3 × (0.05% + 0.05%) = 0.30%

    @property
    def content_hash(self) -> str:
        return release_content_hash(self)

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        series, err = _precheck(
            self.strategy_id,
            self.release_id,
            snapshot,
            settings,
            entry_timeframe="5m",
            min_bars=self.min_session_bars,
        )
        if err is not None:
            return err
        assert series is not None

        atr_obs = _ready_feature(snapshot, "atr_14", "5m")
        vwap_obs = _ready_feature(snapshot, "session_vwap", "5m")
        adx_obs = _ready_feature(snapshot, "adx_14", "5m")
        er_obs = _ready_feature(snapshot, "efficiency_ratio_40", "5m")
        missing = [
            key
            for key, obs in (
                ("5m:atr_14", atr_obs),
                ("5m:session_vwap", vwap_obs),
                ("5m:adx_14", adx_obs),
                ("5m:efficiency_ratio_40", er_obs),
            )
            if obs is None
        ]
        if missing or (atr_obs and (atr_obs.value or 0.0) <= 0.0):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="MISSING_FEATURES",
                detail_vi="Thiếu session_vwap/ATR14/ADX14/ER40 trên khung 5m.",
                missing_features=tuple(missing or ("5m:atr_14",)),
            )
        assert atr_obs
        assert atr_obs.value is not None
        assert vwap_obs
        assert vwap_obs.value is not None
        assert adx_obs
        assert adx_obs.value is not None
        assert er_obs
        assert er_obs.value is not None

        bars = series.bars
        bar_t = bars[-1]
        bar_prev = bars[-2]
        utc_day = bar_t.open_time.astimezone(UTC).date()
        same_day_count = sum(1 for b in bars if b.open_time.astimezone(UTC).date() == utc_day)
        if same_day_count < self.min_session_bars:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="SESSION_WARMUP_INCOMPLETE",
                detail_vi=(
                    f"Phiên UTC hiện mới có {same_day_count}/{self.min_session_bars} nến 5m."
                ),
            )

        if adx_obs.value >= self.max_adx or er_obs.value >= self.max_er:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="REGIME_NOT_RANGING",
                detail_vi=(
                    f"Thị trường đang có xu hướng "
                    f"(ADX={adx_obs.value:.1f}, ER={er_obs.value:.2f}); "
                    "không đánh hồi quy VWAP."
                ),
            )

        close_t = float(bar_t.close)
        close_prev = float(bar_prev.close)
        dev = (close_t - vwap_obs.value) / atr_obs.value

        if dev <= -self.min_atr_deviation and close_t > close_prev and close_t < vwap_obs.value:
            direction = "long"
        elif dev >= self.min_atr_deviation and close_t < close_prev and close_t > vwap_obs.value:
            direction = "short"
        else:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi=f"Chưa có đảo chiều cực trị VWAP (dev={dev:.2f} ATR).",
            )

        vwap_gap_fraction = abs(vwap_obs.value - close_t) / close_t
        if vwap_gap_fraction < self.min_cost_hurdle_fraction:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="COST_HURDLE_NOT_MET",
                detail_vi=(
                    f"Khoảng về VWAP ({vwap_gap_fraction:.4%}) nhỏ hơn 3× chi phí khứ hồi "
                    f"({self.min_cost_hurdle_fraction:.2%})."
                ),
            )

        return _build_candidate(
            rule=self,
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            recipe=self.recipe,
            snapshot=snapshot,
            settings=settings,
            entry_bar=bar_t,
            direction=direction,
            atr_value=atr_obs.value,
            valid_duration=timedelta(minutes=5),
            evidence_features=(atr_obs, vwap_obs, adx_obs, er_obs),
            evidence_bars=(bar_t,),
            reason_code="H07_VWAP_REVERSION",
            detail_vi=(
                f"H07 Session VWAP Reversion {direction.upper()}: dev={dev:+.2f} ATR, "
                f"VWAP={vwap_obs.value:.2f}."
            ),
        )


# ---------------------------------------------------------------------------
# 5. H21 — MACD Histogram + ADX Regime Momentum (1h + 4h)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class H21MacdAdxMomentumRule:
    """Chuyển pha MACD Histogram được xác nhận bằng ADX14 >= 22, ±DI và xu hướng 4h EMA200."""

    strategy_id: str = "H21"
    release_id: str = "h21-v1"
    recipe: ExitRecipe = ExitRecipe(
        recipe_id="h21-exit-v1",
        stop_atr_multiple=2.0,
        trailing_atr_multiple=2.5,
        opposite_signal_exit=True,
        max_hold_bars=96,
    )
    min_adx: float = 22.0

    @property
    def content_hash(self) -> str:
        return release_content_hash(self)

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        series_1h, err = _precheck(
            self.strategy_id,
            self.release_id,
            snapshot,
            settings,
            entry_timeframe="1h",
            min_bars=35,
        )
        if err is not None:
            return err
        series_4h, err_4h = _ready_series(snapshot, "4h", min_bars=1)
        if err_4h is not None:
            return NoTrade(
                strategy_id=self.strategy_id,
                code=err_4h.code,
                detail_vi=err_4h.detail_vi,
                missing_features=err_4h.missing_features,
            )
        assert series_1h is not None
        assert series_4h is not None

        atr_obs = _ready_feature(snapshot, "atr_14", "1h")
        adx_obs = _ready_feature(snapshot, "adx_14", "1h")
        plus_di_obs = _ready_feature(snapshot, "plus_di_14", "1h")
        minus_di_obs = _ready_feature(snapshot, "minus_di_14", "1h")
        ema200_4h_obs = _ready_feature(snapshot, "ema_200", "4h")

        missing = [
            key
            for key, obs in (
                ("1h:atr_14", atr_obs),
                ("1h:adx_14", adx_obs),
                ("1h:plus_di_14", plus_di_obs),
                ("1h:minus_di_14", minus_di_obs),
                ("4h:ema_200", ema200_4h_obs),
            )
            if obs is None
        ]
        if missing or (atr_obs and (atr_obs.value or 0.0) <= 0.0):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="MISSING_FEATURES",
                detail_vi="Thiếu feature ATR14/ADX14/±DI14 (1h) hoặc EMA200 (4h) cho H21.",
                missing_features=tuple(missing or ("1h:atr_14",)),
            )
        assert atr_obs
        assert atr_obs.value is not None
        assert adx_obs
        assert adx_obs.value is not None
        assert plus_di_obs
        assert plus_di_obs.value is not None
        assert minus_di_obs
        assert minus_di_obs.value is not None
        assert ema200_4h_obs
        assert ema200_4h_obs.value is not None

        if adx_obs.value < self.min_adx:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="TREND_TOO_WEAK",
                detail_vi=f"ADX14 ({adx_obs.value:.2f}) < {self.min_adx}; lọc bỏ whipsaw MACD.",
            )

        bars_1h = series_1h.bars
        bar_t = bars_1h[-1]
        bar_4h = series_4h.bars[-1]
        closes = [float(b.close) for b in bars_1h]
        macd_line, _, histogram = macd(closes, fast=12, slow=26, signal=9)
        hist_cur, hist_prev = histogram[-1], histogram[-2]
        macd_cur, macd_prev = macd_line[-1], macd_line[-2]
        if hist_cur is None or hist_prev is None or macd_cur is None or macd_prev is None:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="WARMUP_INCOMPLETE",
                detail_vi="Chưa đủ dữ liệu tính MACD Histogram.",
            )

        close_4h = float(bar_4h.close)
        if (
            close_4h > ema200_4h_obs.value
            and plus_di_obs.value > minus_di_obs.value
            and hist_prev <= 0.0 < hist_cur
            and macd_cur > macd_prev
        ):
            direction = "long"
        elif (
            close_4h < ema200_4h_obs.value
            and minus_di_obs.value > plus_di_obs.value
            and hist_prev >= 0.0 > hist_cur
            and macd_cur < macd_prev
        ):
            direction = "short"
        else:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi="MACD histogram chưa chuyển pha đồng thuận ±DI và 4h EMA200.",
            )

        return _build_candidate(
            rule=self,
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            recipe=self.recipe,
            snapshot=snapshot,
            settings=settings,
            entry_bar=bar_t,
            direction=direction,
            atr_value=atr_obs.value,
            valid_duration=timedelta(hours=1),
            evidence_features=(atr_obs, adx_obs, plus_di_obs, minus_di_obs, ema200_4h_obs),
            evidence_bars=(bar_t, bar_4h),
            reason_code="H21_MACD_ADX_MOMENTUM",
            detail_vi=(
                f"H21 MACD+ADX {direction.upper()}: Hist {hist_prev:+.4f}→{hist_cur:+.4f}, "
                f"ADX={adx_obs.value:.1f}."
            ),
        )


# ---------------------------------------------------------------------------
# 6. H22 — Taker Order-Flow Imbalance & Liquidity Breakout (15m + 1h)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class H22TakerFlowBreakoutRule:
    """Phá đỉnh/đáy 20 nến 15m kèm dòng lệnh chủ động taker và xu hướng 1h EMA80."""

    strategy_id: str = "H22"
    release_id: str = "h22-v1"
    recipe: ExitRecipe = ExitRecipe(
        recipe_id="h22-exit-v1",
        stop_atr_multiple=2.0,
        trailing_atr_multiple=2.5,
        opposite_signal_exit=True,
        max_hold_bars=32,
    )
    lookback: int = 20
    min_taker_imbalance: float = 0.25
    min_rolling3_imbalance: float = 0.12
    min_volume_multiple: float = 1.3
    min_adx: float = 18.0

    @property
    def content_hash(self) -> str:
        return release_content_hash(self)

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        series_15m, err = _precheck(
            self.strategy_id,
            self.release_id,
            snapshot,
            settings,
            entry_timeframe="15m",
            min_bars=self.lookback + 1,
        )
        if err is not None:
            return err
        series_1h, err_1h = _ready_series(snapshot, "1h", min_bars=1)
        if err_1h is not None:
            return NoTrade(
                strategy_id=self.strategy_id,
                code=err_1h.code,
                detail_vi=err_1h.detail_vi,
                missing_features=err_1h.missing_features,
            )
        assert series_15m is not None
        assert series_1h is not None

        atr_15_obs = _ready_feature(snapshot, "atr_14", "15m")
        adx_15_obs = _ready_feature(snapshot, "adx_14", "15m")
        ema80_1h_obs = _ready_feature(snapshot, "ema_80", "1h")
        atr_1h_obs = _ready_feature(snapshot, "atr_14", "1h")

        missing = [
            key
            for key, obs in (
                ("15m:atr_14", atr_15_obs),
                ("15m:adx_14", adx_15_obs),
                ("1h:ema_80", ema80_1h_obs),
                ("1h:atr_14", atr_1h_obs),
            )
            if obs is None
        ]
        if missing or (atr_15_obs and (atr_15_obs.value or 0.0) <= 0.0):
            return NoTrade(
                strategy_id=self.strategy_id,
                code="MISSING_FEATURES",
                detail_vi="Thiếu ATR14/ADX14 (15m) hoặc EMA80/ATR14 (1h) cho H22.",
                missing_features=tuple(missing or ("15m:atr_14",)),
            )
        assert atr_15_obs
        assert atr_15_obs.value is not None
        assert adx_15_obs
        assert adx_15_obs.value is not None
        assert ema80_1h_obs
        assert ema80_1h_obs.value is not None
        assert atr_1h_obs
        assert atr_1h_obs.value is not None

        bars_15m = series_15m.bars
        bar_t = bars_15m[-1]
        bar_1h = series_1h.bars[-1]

        recent_3 = bars_15m[-3:]
        ti_values: list[float] = []
        for item in recent_3:
            if item.taker_buy_base_volume is None or item.volume <= 0:
                return NoTrade(
                    strategy_id=self.strategy_id,
                    code="TAKER_FLOW_UNAVAILABLE",
                    detail_vi="Thiếu taker_buy_base_volume hợp lệ từ Binance klines (fail-closed).",
                )
            if item.taker_buy_base_volume < 0 or item.taker_buy_base_volume > item.volume:
                return NoTrade(
                    strategy_id=self.strategy_id,
                    code="TAKER_FLOW_UNAVAILABLE",
                    detail_vi="Dữ liệu taker_buy_base_volume vượt ngoài biên [0, volume].",
                )
            v = float(item.volume)
            tb = float(item.taker_buy_base_volume)
            ti_values.append((2.0 * tb - v) / v)

        if adx_15_obs.value < self.min_adx:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="TREND_TOO_WEAK",
                detail_vi=f"ADX14 15m ({adx_15_obs.value:.1f}) < {self.min_adx}.",
            )

        prior_vols = [float(b.volume) for b in bars_15m[-1 - self.lookback : -1]]
        median_vol = median(prior_vols)
        cur_vol = float(bar_t.volume)
        if median_vol <= 0.0 or cur_vol < self.min_volume_multiple * median_vol:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi="Khối lượng hiện tại chưa đủ đột biến xác nhận thanh khoản.",
            )

        hh20_prev = max(float(b.high) for b in bars_15m[-1 - self.lookback : -1])
        ll20_prev = min(float(b.low) for b in bars_15m[-1 - self.lookback : -1])
        close_t = float(bar_t.close)
        close_1h = float(bar_1h.close)
        ti_cur = ti_values[-1]
        ti_avg3 = sum(ti_values) / len(ti_values)

        if (
            close_t > hh20_prev
            and ti_cur >= self.min_taker_imbalance
            and ti_avg3 >= self.min_rolling3_imbalance
            and close_1h > ema80_1h_obs.value
        ):
            direction = "long"
        elif (
            close_t < ll20_prev
            and ti_cur <= -self.min_taker_imbalance
            and ti_avg3 <= -self.min_rolling3_imbalance
            and close_1h < ema80_1h_obs.value
        ):
            direction = "short"
        else:
            return NoTrade(
                strategy_id=self.strategy_id,
                code="NO_SIGNAL",
                detail_vi=(
                    "Chưa có đồng thuận phá vỡ thanh khoản và "
                    f"Taker Flow Imbalance (TI={ti_cur:+.2f})."
                ),
            )

        return _build_candidate(
            rule=self,
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            recipe=self.recipe,
            snapshot=snapshot,
            settings=settings,
            entry_bar=bar_t,
            direction=direction,
            atr_value=atr_15_obs.value,
            valid_duration=timedelta(minutes=15),
            evidence_features=(atr_15_obs, adx_15_obs, ema80_1h_obs, atr_1h_obs),
            evidence_bars=(bar_t, bar_1h),
            reason_code="H22_TAKER_FLOW_BREAKOUT",
            detail_vi=(
                f"H22 Taker Flow Breakout {direction.upper()}: TI={ti_cur:+.2f} "
                f"(TB3={ti_avg3:+.2f}), Vol={cur_vol / median_vol:.2f}× median."
            ),
        )


EXPANDED_RULES: tuple[tuple[object, str, str, str], ...] = (
    (
        H02TimeSeriesMomentumRule(),
        "Động lượng chuỗi thời gian đa chân trời (TSMOM 4h)",
        "Cổ điển Quant (Moskowitz/Liu-Tsyvinski): 3 lookback 42/126/252 nến 4h + hàng rào RV20.",
        "h02-exit-v1",
    ),
    (
        H04VolatilitySqueezeBreakoutRule(),
        "Nén biến động Bollinger–Keltner & Phá vỡ khối lượng (15m)",
        "Bollinger BandWidth trong Keltner tại t-1 + BW giãn nở + quote_volume > 1,5× median.",
        "h04-exit-v1",
    ),
    (
        H05TrendPullbackRule(),
        "Nhịp hồi xu hướng EMA20 + RSI Regime (1h/4h)",
        "Đồng thuận 4h EMA200 & 1h EMA20>EMA80, nến chạm giữ EMA20 kèm 40 ≤ RSI14 ≤ 60.",
        "h05-exit-v1",
    ),
    (
        H07SessionVwapReversionRule(),
        "Hồi quy Session VWAP trong ngày UTC (5m)",
        "ADX14 < 20, ER40 < 0,30, lệch VWAP ≥ 2 ATR đảo chiều & khoảng hồi ≥ 3× phí khứ hồi.",
        "h07-exit-v1",
    ),
    (
        H21MacdAdxMomentumRule(),
        "Động lượng biểu đồ MACD lọc nhiễu theo ADX (1h/4h)",
        "Histogram MACD(12,26,9) đổi dấu cùng chiều 4h EMA200, ADX14 ≥ 22 và ±DI xác nhận.",
        "h21-exit-v1",
    ),
    (
        H22TakerFlowBreakoutRule(),
        "Phá vỡ thanh khoản xác nhận Taker Order-Flow (15m/1h)",
        "Phá đỉnh/đáy 20 nến với TI=(2·taker_buy−V)/V ≥ ±0,25, khối lượng đột biến & 1h EMA80.",
        "h22-exit-v1",
    ),
)
