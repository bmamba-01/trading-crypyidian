"""Checklist điều kiện có cấu trúc cho rule và Strategy Card (SIG-3 / MI-1), chỉ để diễn giải.

Checklist đọc **cùng** ``DecisionSnapshot``, ngưỡng của rule và ``StrategyPipelineEngine`` nhưng
nằm ở module riêng: hash release (``release_content_hash``) băm mã nguồn module rules, nên sửa
rules để sinh checklist sẽ đổi danh tính mọi release đang ghim. Test parity bảo đảm: rule/card
ra candidate ⇔ mọi điều kiện trong checklist đạt. Checklist không quyết định vào lệnh; rule và
``StrategyCardStrategy`` vẫn là nguồn sự thật.

``passed`` là ``None`` khi thiếu dữ liệu hoặc điều kiện chưa áp dụng (ví dụ chưa biết hướng hay
ngòi nổ L5 bị ngắt mạch tại L4).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from functools import lru_cache
from statistics import median
from typing import TYPE_CHECKING, Any

from trading_bot.domain.market.features import atr, bollinger_bands, ema, macd
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    SeriesQuality,
    StrategySelection,
)
from trading_bot.domain.strategy.expanded_rules import (
    H02TimeSeriesMomentumRule,
    H04VolatilitySqueezeBreakoutRule,
    H05TrendPullbackRule,
    H07SessionVwapReversionRule,
    H21MacdAdxMomentumRule,
    H22TakerFlowBreakoutRule,
    _percentile_threshold,
)
from trading_bot.domain.strategy.rules_v3 import H03_V3_TIMEFRAMES
from trading_bot.domain.strategy_card.anatomy import (
    lower_wick_ratio,
    upper_wick_ratio,
)
from trading_bot.domain.strategy_card.models import CompositionLayer

if TYPE_CHECKING:
    from trading_bot.application.strategy_pipeline.pipeline_engine import PipelineExecutionResult
    from trading_bot.domain.strategy_card.schema import StrategyCard

CHECKLIST_VERSION = "sig3-checklist-v1"


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """Một điều kiện: giá trị thực, phép so, ngưỡng và kết quả."""

    key: str
    label_vi: str
    actual: str
    operator: str
    threshold: str
    passed: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label_vi": self.label_vi,
            "actual": self.actual,
            "operator": self.operator,
            "threshold": self.threshold,
            "passed": self.passed,
        }


def _fmt(value: float | Decimal | None, digits: int = 4) -> str:
    if value is None:
        return "thiếu dữ liệu"
    number = float(value)
    return f"{number:.{digits}f}".rstrip("0").rstrip(".") if abs(number) < 1e6 else f"{number:.0f}"


def _feature(snapshot: DecisionSnapshot, name: str, timeframe: str) -> Any:
    feature = snapshot.get_feature(name, timeframe)
    if feature is None or feature.value is None:
        return None
    return feature


def _h01(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    fast = _feature(snapshot, "ema_20", "1h")
    slow = _feature(snapshot, "ema_80", "1h")
    direction: str | None = None
    if (
        fast is not None
        and slow is not None
        and fast.previous_value is not None
        and slow.previous_value is not None
    ):
        if fast.previous_value <= slow.previous_value and fast.value > slow.value:
            direction = "long"
        elif fast.previous_value >= slow.previous_value and fast.value < slow.value:
            direction = "short"
        cross = ChecklistItem(
            "ema_cross",
            "EMA20 vừa cắt EMA80 (1h)",
            f"EMA20 {_fmt(fast.value)} / EMA80 {_fmt(slow.value)}; nến trước "
            f"{_fmt(fast.previous_value)} / {_fmt(slow.previous_value)}",
            "cắt",
            "EMA20 đổi phía so với EMA80",
            direction is not None,
        )
    else:
        cross = ChecklistItem(
            "ema_cross", "EMA20 vừa cắt EMA80 (1h)", "thiếu dữ liệu", "cắt", "—", None
        )
    items = [cross]
    h4 = snapshot.get_series("4h")
    ema200 = _feature(snapshot, "ema_200", "4h")
    if h4 is not None and h4.bars and ema200 is not None:
        close = h4.bars[-1].close
        side_ok = (
            None
            if direction is None
            else (
                Decimal(str(ema200.value)) < close
                if direction == "long"
                else Decimal(str(ema200.value)) > close
            )
        )
        items.append(
            ChecklistItem(
                "htf_trend",
                "Giá đóng 4h cùng phía EMA200 với hướng giao cắt",
                f"đóng 4h {_fmt(close, 2)} / EMA200 {_fmt(ema200.value, 2)}",
                ">" if direction == "long" else "<" if direction == "short" else "cùng phía",
                "EMA200 4h",
                side_ok,
            )
        )
    else:
        items.append(
            ChecklistItem("htf_trend", "Giá 4h cùng phía EMA200", "thiếu dữ liệu", "—", "—", None)
        )
    adx = _feature(snapshot, "adx_14", "1h")
    items.append(
        ChecklistItem(
            "adx",
            "ADX14 1h đủ mạnh",
            _fmt(adx.value if adx else None, 2),
            ">=",
            "20",
            None if adx is None else adx.value >= 20,
        )
    )
    return tuple(items)


def _h03(
    snapshot: DecisionSnapshot, settings: StrategySelection, timeframe: str = "1h"
) -> tuple[ChecklistItem, ...]:
    parameters = dict(settings.parameters)
    lookback = parameters.get("donchian_lookback", 55)
    volume_lookback = parameters.get("volume_median_lookback", 20)
    series = snapshot.get_series(timeframe)
    if series is None or len(series.bars) < lookback + 1:
        return (
            ChecklistItem(
                "donchian_breakout",
                f"Giá đóng phá kênh Donchian {lookback} nến ({timeframe})",
                "thiếu dữ liệu",
                "phá",
                "—",
                None,
            ),
        )
    prior = series.bars[-(lookback + 1) : -1]
    current = series.bars[-1]
    high = max(bar.high for bar in prior)
    low = min(bar.low for bar in prior)
    broke_up, broke_down = current.close > high, current.close < low
    items = [
        ChecklistItem(
            "donchian_breakout",
            f"Giá đóng phá kênh Donchian {lookback} nến ({timeframe})",
            f"đóng {_fmt(current.close, 2)}; kênh {_fmt(low, 2)} – {_fmt(high, 2)}",
            ">" if broke_up else "<" if broke_down else "ngoài kênh",
            _fmt(high, 2) if not broke_down else _fmt(low, 2),
            broke_up or broke_down,
        )
    ]
    volumes = series.quote_volumes
    if volumes is None or len(volumes) < volume_lookback + 1:
        items.append(
            ChecklistItem(
                "volume_above_median",
                f"Quote volume > median {volume_lookback} nến trước",
                "thiếu dữ liệu",
                ">",
                "—",
                None,
            )
        )
    else:
        reference = median(volumes[-(volume_lookback + 1) : -1])
        items.append(
            ChecklistItem(
                "volume_above_median",
                f"Quote volume > median {volume_lookback} nến trước",
                _fmt(volumes[-1], 0),
                ">",
                _fmt(reference, 0),
                volumes[-1] > reference,
            )
        )
    return tuple(items)


def _h06(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    def value(name: str) -> float | None:
        feature = _feature(snapshot, name, "15m")
        return None if feature is None else float(feature.value)

    adx, er, z, rsi = (value(n) for n in ("adx_14", "efficiency_ratio_40", "zscore_20", "rsi_14"))
    macro_ok = (
        None
        if snapshot.macro_blackout is None or snapshot.macro_is_fresh is None
        else (not snapshot.macro_blackout and snapshot.macro_is_fresh)
    )
    long_extreme = z is not None and rsi is not None and z < -2 and rsi < 30
    short_extreme = z is not None and rsi is not None and z > 2 and rsi > 70
    items = [
        ChecklistItem(
            "macro_clear",
            "Không trong khoảng cấm quanh sự kiện vĩ mô, lịch còn mới",
            "thiếu dữ liệu"
            if macro_ok is None
            else ("đang cấm" if snapshot.macro_blackout else "rõ"),
            "=",
            "không cấm",
            macro_ok,
        ),
        ChecklistItem(
            "range_adx",
            "ADX14 15m thấp (đi ngang)",
            _fmt(adx, 2),
            "<",
            "18",
            None if adx is None else adx < 18,
        ),
        ChecklistItem(
            "range_er",
            "Efficiency ratio 40 thấp",
            _fmt(er, 3),
            "<",
            "0.25",
            None if er is None else er < 0.25,
        ),
        ChecklistItem(
            "extreme",
            "Z20 và RSI14 cùng vượt ngưỡng",
            f"Z20 {_fmt(z, 2)} / RSI14 {_fmt(rsi, 1)}",
            "vượt",
            "Z < −2 & RSI < 30, hoặc Z > 2 & RSI > 70",
            None if z is None or rsi is None else (long_extreme or short_extreme),
        ),
    ]
    series = snapshot.get_series("15m")
    if series is not None and len(series.bars) >= 121 and (long_extreme or short_extreme):
        prior = series.bars[-121:-1]
        close = series.bars[-1].close
        floor, ceiling = min(bar.low for bar in prior), max(bar.high for bar in prior)
        safe = close >= floor if long_extreme else close <= ceiling
        items.append(
            ChecklistItem(
                "no_adverse_break",
                "Chưa phá biên 120 nến theo hướng bất lợi",
                f"đóng {_fmt(close, 2)}; biên {_fmt(floor, 2)} – {_fmt(ceiling, 2)}",
                ">=" if long_extreme else "<=",
                _fmt(floor if long_extreme else ceiling, 2),
                safe,
            )
        )
    return tuple(items)


def _ready_feature(
    snapshot: DecisionSnapshot, name: str, timeframe: str
) -> FeatureObservation | None:
    """Lấy feature ở trạng thái READY, hữu hạn và khả dụng tại ``decision_time``."""
    obs = snapshot.get_feature(name, timeframe)
    if obs is None or obs.quality is not FeatureQuality.READY:
        return None
    if obs.available_at > snapshot.decision_time:
        return None
    if obs.value is None or not math.isfinite(obs.value):
        return None
    return obs


def _ready_series(
    snapshot: DecisionSnapshot, timeframe: str, *, min_bars: int = 1
) -> MarketSeries | None:
    """Lấy chuỗi nến READY, đã backfill đủ và nến cuối đã đóng tới ``decision_time``."""
    series = snapshot.get_series(timeframe)
    if (
        series is None
        or series.quality is not SeriesQuality.READY
        or series.backfill_complete is not True
        or not series.bars
    ):
        return None
    latest = series.bars[-1]
    if not latest.is_closed or latest.available_at > snapshot.decision_time:
        return None
    if len(series.bars) < min_bars:
        return None
    return series


def _append_expanded_preconditions(
    items: list[ChecklistItem],
    release_id: str,
    snapshot: DecisionSnapshot,
    settings: StrategySelection,
    atr_checks: tuple[tuple[str, FeatureObservation | None, bool], ...],
) -> None:
    """Bổ sung các điều kiện tiền kiểm khi bị vi phạm/thiếu để giữ parity 100% với rule."""
    if not settings.published or release_id not in settings.active_release_ids:
        items.append(
            ChecklistItem(
                "strategy_active",
                f"Bản phát hành {release_id} đã công bố và được chọn",
                "chưa kích hoạt" if settings.published else "chưa công bố",
                "=",
                "đang kích hoạt",
                False,
            )
        )
    if snapshot.macro_blackout is True:
        items.append(
            ChecklistItem(
                "macro_clear",
                "Không trong khoảng cấm quanh sự kiện vĩ mô",
                "đang cấm",
                "=",
                "không cấm",
                False,
            )
        )
    if snapshot.position_is_flat is not True:
        items.append(
            ChecklistItem(
                "position_flat",
                "Không có vị thế mở hoặc lệnh đang hoạt động",
                "thiếu dữ liệu" if snapshot.position_is_flat is None else "đang có vị thế",
                "=",
                "phẳng",
                None if snapshot.position_is_flat is None else False,
            )
        )
    for tf, obs, must_be_positive in atr_checks:
        if obs is None:
            items.append(
                ChecklistItem(
                    "atr_valid" if len(atr_checks) == 1 else f"atr_valid_{tf}",
                    f"Feature ATR14 khung {tf} hợp lệ",
                    "thiếu dữ liệu",
                    ">" if must_be_positive else "sẵn sàng",
                    "0" if must_be_positive else "có giá trị",
                    None,
                )
            )
        elif must_be_positive and (obs.value or 0.0) <= 0.0:
            items.append(
                ChecklistItem(
                    "atr_valid" if len(atr_checks) == 1 else f"atr_valid_{tf}",
                    f"Feature ATR14 khung {tf} dương",
                    _fmt(obs.value, 4),
                    ">",
                    "0",
                    False,
                )
            )


def _h02(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    rule = H02TimeSeriesMomentumRule()
    min_bars = max(rule.horizons) + 1
    series = _ready_series(snapshot, "4h", min_bars=min_bars)
    atr_obs = _ready_feature(snapshot, "atr_14", "4h")
    rv_obs = _ready_feature(snapshot, "realized_volatility_20", "4h")

    if rv_obs is None:
        rv_item = ChecklistItem(
            "rv_regime",
            "Biến động thực tế RV20 (4h) trong dải an toàn",
            "thiếu dữ liệu",
            "trong",
            f"[{rule.min_rv}, {rule.max_rv}]",
            None,
        )
    else:
        assert rv_obs.value is not None
        rv_ok = rv_obs.value > 0.0 and rule.min_rv <= rv_obs.value <= rule.max_rv
        rv_item = ChecklistItem(
            "rv_regime",
            "Biến động thực tế RV20 (4h) trong dải an toàn",
            _fmt(rv_obs.value, 4),
            "trong",
            f"[{rule.min_rv}, {rule.max_rv}]",
            rv_ok,
        )

    if series is None:
        tsmom_item = ChecklistItem(
            "tsmom_horizon",
            "Động lượng đa chân trời 4h (42/126/252 nến) đồng thuận",
            "thiếu dữ liệu",
            "đồng thuận ≥ 2/3",
            "|m| >= 0.333",
            None,
        )
    else:
        bars = series.bars
        current_close = float(bars[-1].close)
        signs: list[int] = []
        for lookback in rule.horizons:
            past_close = float(bars[-1 - lookback].close)
            signs.append(
                1 if current_close > past_close else -1 if current_close < past_close else 0
            )
        score = sum(signs) / len(signs)
        pos_count = sum(1 for s in signs if s > 0)
        neg_count = sum(1 for s in signs if s < 0)
        tsmom_ok = (pos_count >= 2 and score >= (1.0 / 3.0) - 1e-9) or (
            neg_count >= 2 and score <= -(1.0 / 3.0) + 1e-9
        )
        tsmom_item = ChecklistItem(
            "tsmom_horizon",
            "Động lượng đa chân trời 4h (42/126/252 nến) đồng thuận",
            f"m={score:+.2f} (+{pos_count}/-{neg_count})",
            "đồng thuận ≥ 2/3",
            "|m| >= 0.333",
            tsmom_ok,
        )

    items = [rv_item, tsmom_item]
    _append_expanded_preconditions(
        items, rule.release_id, snapshot, settings, (("4h", atr_obs, True),)
    )
    return tuple(items)


def _h04(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    rule = H04VolatilitySqueezeBreakoutRule()
    series = _ready_series(snapshot, "15m", min_bars=60)
    atr_obs = _ready_feature(snapshot, "atr_14", "15m")

    squeeze_passed: bool | None = None
    squeeze_actual = "thiếu dữ liệu"
    bw_passed: bool | None = None
    bw_actual = "thiếu dữ liệu"
    vol_passed: bool | None = None
    vol_actual = "thiếu dữ liệu"
    vol_threshold = f"> {rule.volume_multiplier}× median"
    break_passed: bool | None = None
    break_actual = "thiếu dữ liệu"
    break_op = "phá"
    break_threshold = "đỉnh/đáy 20 nến & dải BB"

    if series is not None:
        bars = series.bars
        if series.quote_volumes is not None and len(series.quote_volumes) == len(bars):
            prior_qvols = list(series.quote_volumes[-1 - rule.lookback : -1])
            prior_median_qvol = median(prior_qvols)
            cur_qvol = series.quote_volumes[-1]
            vol_passed = (
                prior_median_qvol > 0.0 and cur_qvol > rule.volume_multiplier * prior_median_qvol
            )
            vol_actual = _fmt(cur_qvol, 0)
            vol_threshold = _fmt(rule.volume_multiplier * prior_median_qvol, 0)

        closes = [float(b.close) for b in bars]
        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]
        bb_mid, bb_upper, bb_lower = bollinger_bands(closes, period=rule.lookback, deviations=2.0)
        ema20 = ema(closes, rule.lookback)
        atr14 = atr(highs, lows, closes, 14)
        bw_series = [
            (u - low_b) / m
            if (u is not None and m is not None and low_b is not None and m > 0.0)
            else None
            for u, m, low_b in zip(bb_upper, bb_mid, bb_lower, strict=True)
        ]
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
        bw_history = [v for v in bw_series[max(0, prev_idx - 240) : prev_idx] if v is not None]

        if (
            bw_cur is not None
            and bw_prev is not None
            and bb_u_cur is not None
            and bb_l_cur is not None
            and bb_u_prev is not None
            and bb_l_prev is not None
            and ema_prev is not None
            and atr_prev is not None
            and len(bw_history) >= 20
        ):
            kc_upper_prev = ema_prev + rule.keltner_atr_multiple * atr_prev
            kc_lower_prev = ema_prev - rule.keltner_atr_multiple * atr_prev
            in_kc_squeeze = bb_u_prev < kc_upper_prev and bb_l_prev > kc_lower_prev
            pct_threshold = _percentile_threshold(bw_history, rule.bandwidth_percentile)
            in_pct_squeeze = bw_prev <= pct_threshold
            squeeze_passed = in_kc_squeeze or in_pct_squeeze
            squeeze_actual = f"BW(t-1) {_fmt(bw_prev, 4)} / P25 {_fmt(pct_threshold, 4)}" + (
                " (trong Keltner)" if in_kc_squeeze else ""
            )
            bw_passed = bw_cur > bw_prev
            bw_actual = f"{_fmt(bw_prev, 4)} → {_fmt(bw_cur, 4)}"

            hh20_prev = max(float(b.high) for b in bars[-1 - rule.lookback : -1])
            ll20_prev = min(float(b.low) for b in bars[-1 - rule.lookback : -1])
            close_cur = float(bars[-1].close)
            broke_up = close_cur > hh20_prev and close_cur > bb_u_cur
            broke_down = close_cur < ll20_prev and close_cur < bb_l_cur
            break_passed = broke_up or broke_down
            break_op = ">" if broke_up else "<" if broke_down else "ngoài biên"
            break_actual = (
                f"đóng {_fmt(close_cur, 2)}; HH/LL {_fmt(ll20_prev, 2)} – {_fmt(hh20_prev, 2)}"
            )
            break_threshold = (
                f"{_fmt(max(hh20_prev, bb_u_cur), 2)}"
                if not broke_down
                else f"{_fmt(min(ll20_prev, bb_l_cur), 2)}"
            )

    items = [
        ChecklistItem(
            "prior_squeeze",
            "Trạng thái nén biến động Bollinger–Keltner tại t-1",
            squeeze_actual,
            "trong KC hoặc ≤ P25",
            "nén BB–Keltner / P25",
            squeeze_passed,
        ),
        ChecklistItem(
            "bw_expansion",
            "BandWidth mở rộng tại nến hiện tại",
            bw_actual,
            ">",
            "BW(t-1)",
            bw_passed,
        ),
        ChecklistItem(
            "volume_surge",
            f"Quote volume > {rule.volume_multiplier}× trung vị {rule.lookback} nến trước",
            vol_actual,
            ">",
            vol_threshold,
            vol_passed,
        ),
        ChecklistItem(
            "band_breakout",
            f"Giá đóng phá đỉnh/đáy {rule.lookback} nến và dải Bollinger",
            break_actual,
            break_op,
            break_threshold,
            break_passed,
        ),
    ]
    _append_expanded_preconditions(
        items, rule.release_id, snapshot, settings, (("15m", atr_obs, True),)
    )
    return tuple(items)


def _h05(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    rule = H05TrendPullbackRule()
    series_1h = _ready_series(snapshot, "1h", min_bars=20)
    series_4h = _ready_series(snapshot, "4h", min_bars=1)

    atr_obs = _ready_feature(snapshot, "atr_14", "1h")
    ema20_obs = _ready_feature(snapshot, "ema_20", "1h")
    ema80_obs = _ready_feature(snapshot, "ema_80", "1h")
    rsi_obs = _ready_feature(snapshot, "rsi_14", "1h")
    adx_obs = _ready_feature(snapshot, "adx_14", "1h")
    ema200_4h_obs = _ready_feature(snapshot, "ema_200", "4h")

    htf_dir: str | None = None
    if series_4h is not None and ema200_4h_obs is not None:
        assert ema200_4h_obs.value is not None
        close_4h = float(series_4h.bars[-1].close)
        if close_4h > ema200_4h_obs.value:
            htf_dir = "long"
        elif close_4h < ema200_4h_obs.value:
            htf_dir = "short"
        htf_item = ChecklistItem(
            "htf_trend",
            "Giá đóng 4h xác nhận xu hướng so với EMA200",
            f"đóng 4h {_fmt(close_4h, 2)} / EMA200 {_fmt(ema200_4h_obs.value, 2)}",
            ">" if htf_dir == "long" else "<" if htf_dir == "short" else "khác",
            "EMA200 4h",
            htf_dir is not None,
        )
    else:
        htf_item = ChecklistItem(
            "htf_trend",
            "Giá đóng 4h xác nhận xu hướng so với EMA200",
            "thiếu dữ liệu",
            "—",
            "—",
            None,
        )

    ema_dir: str | None = None
    if series_1h is not None and ema20_obs is not None and ema80_obs is not None:
        assert ema20_obs.value is not None
        assert ema80_obs.value is not None
        bars_1h = series_1h.bars
        if ema80_obs.previous_value is not None and ema80_obs.previous_value != ema80_obs.value:
            ema80_slope = ema80_obs.value - ema80_obs.previous_value
        elif len(bars_1h) >= 83:
            ema80_series = ema([float(b.close) for b in bars_1h], 80)
            cur_80, past_80 = ema80_series[-1], ema80_series[-4]
            ema80_slope = (
                (cur_80 - past_80) if (cur_80 is not None and past_80 is not None) else 0.0
            )
        else:
            ema80_slope = 0.0

        if ema20_obs.value > ema80_obs.value and ema80_slope > 0.0:
            ema_dir = "long"
        elif ema20_obs.value < ema80_obs.value and ema80_slope < 0.0:
            ema_dir = "short"

        ema_ok = ema_dir == htf_dir if htf_dir is not None else (ema_dir is not None)
        ema_item = ChecklistItem(
            "ema_alignment",
            "EMA20/EMA80 (1h) và độ dốc EMA80 đồng thuận xu hướng",
            f"EMA20 {_fmt(ema20_obs.value, 2)} / EMA80 {_fmt(ema80_obs.value, 2)} "
            f"(dốc {_fmt(ema80_slope, 4)})",
            "đồng thuận",
            "cùng chiều 4h EMA200" if htf_dir is not None else "EMA20 ≠ EMA80 & có độ dốc",
            ema_ok,
        )
    else:
        ema_item = ChecklistItem(
            "ema_alignment",
            "EMA20/EMA80 (1h) và độ dốc EMA80 đồng thuận xu hướng",
            "thiếu dữ liệu",
            "đồng thuận",
            "—",
            None,
        )

    adx_item = ChecklistItem(
        "adx",
        "ADX14 1h đủ mạnh",
        _fmt(adx_obs.value if adx_obs else None, 2),
        ">=",
        _fmt(rule.min_adx, 1),
        None if adx_obs is None or adx_obs.value is None else adx_obs.value >= rule.min_adx,
    )
    rsi_item = ChecklistItem(
        "rsi_band",
        "RSI14 1h nằm trong vùng nhịp hồi [40, 60]",
        _fmt(rsi_obs.value if rsi_obs else None, 2),
        "trong",
        f"[{_fmt(rule.rsi_low, 0)}, {_fmt(rule.rsi_high, 0)}]",
        None
        if rsi_obs is None or rsi_obs.value is None
        else (rule.rsi_low <= rsi_obs.value <= rule.rsi_high),
    )

    target_dir = (
        ema_dir if (ema_dir is not None and (htf_dir is None or ema_dir == htf_dir)) else htf_dir
    )
    if series_1h is not None and ema20_obs is not None and target_dir is not None:
        assert ema20_obs.value is not None
        bar_t = series_1h.bars[-1]
        close_t, open_t, high_t, low_t = (
            float(bar_t.close),
            float(bar_t.open),
            float(bar_t.high),
            float(bar_t.low),
        )
        if target_dir == "long":
            reclaim_ok = low_t <= ema20_obs.value < close_t and close_t > open_t
            reclaim_thr = f"low ≤ EMA20 {_fmt(ema20_obs.value, 2)} < close & nến xanh"
        else:
            reclaim_ok = high_t >= ema20_obs.value > close_t and close_t < open_t
            reclaim_thr = f"high ≥ EMA20 {_fmt(ema20_obs.value, 2)} > close & nến đỏ"
        pullback_item = ChecklistItem(
            "pullback_reclaim",
            "Nến 1h chạm và đóng giữ qua EMA20 theo xu hướng",
            f"O {_fmt(open_t, 2)} H {_fmt(high_t, 2)} L {_fmt(low_t, 2)} C {_fmt(close_t, 2)}",
            "chạm & giữ",
            reclaim_thr,
            reclaim_ok,
        )
    else:
        pullback_item = ChecklistItem(
            "pullback_reclaim",
            "Nến 1h chạm và đóng giữ qua EMA20 theo xu hướng",
            "thiếu dữ liệu" if (series_1h is None or ema20_obs is None) else "chưa xác định hướng",
            "chạm & giữ",
            "—",
            None,
        )

    items = [htf_item, ema_item, adx_item, rsi_item, pullback_item]
    _append_expanded_preconditions(
        items, rule.release_id, snapshot, settings, (("1h", atr_obs, True),)
    )
    return tuple(items)


def _h07(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    rule = H07SessionVwapReversionRule()
    raw_ready = _ready_series(snapshot, "5m", min_bars=1)
    series = _ready_series(snapshot, "5m", min_bars=rule.min_session_bars)

    atr_obs = _ready_feature(snapshot, "atr_14", "5m")
    vwap_obs = _ready_feature(snapshot, "session_vwap", "5m")
    adx_obs = _ready_feature(snapshot, "adx_14", "5m")
    er_obs = _ready_feature(snapshot, "efficiency_ratio_40", "5m")

    if series is not None:
        utc_day = series.bars[-1].open_time.astimezone(UTC).date()
        same_day_count = sum(
            1 for b in series.bars if b.open_time.astimezone(UTC).date() == utc_day
        )
        session_item = ChecklistItem(
            "session_warmup",
            f"Phiên UTC hiện tại có tối thiểu {rule.min_session_bars} nến 5m",
            f"{same_day_count} nến",
            ">=",
            f"{rule.min_session_bars} nến",
            same_day_count >= rule.min_session_bars,
        )
    else:
        actual_bars = (
            "thiếu dữ liệu"
            if raw_ready is None
            else f"{len(raw_ready.bars)}/{rule.min_session_bars} nến"
        )
        session_item = ChecklistItem(
            "session_warmup",
            f"Phiên UTC hiện tại có tối thiểu {rule.min_session_bars} nến 5m",
            actual_bars,
            ">=",
            f"{rule.min_session_bars} nến",
            None,
        )

    if adx_obs is not None and er_obs is not None:
        assert adx_obs.value is not None
        assert er_obs.value is not None
        range_item = ChecklistItem(
            "range_regime",
            "Thị trường đi ngang (ADX14 < 20 và ER40 < 0.30)",
            f"ADX {_fmt(adx_obs.value, 2)} / ER {_fmt(er_obs.value, 3)}",
            "<",
            f"ADX < {_fmt(rule.max_adx, 0)} & ER < {_fmt(rule.max_er, 2)}",
            adx_obs.value < rule.max_adx and er_obs.value < rule.max_er,
        )
    else:
        range_item = ChecklistItem(
            "range_regime",
            "Thị trường đi ngang (ADX14 < 20 và ER40 < 0.30)",
            "thiếu dữ liệu",
            "<",
            f"ADX < {_fmt(rule.max_adx, 0)} & ER < {_fmt(rule.max_er, 2)}",
            None,
        )

    if (
        series is not None
        and len(series.bars) >= 2
        and vwap_obs is not None
        and atr_obs is not None
        and (atr_obs.value or 0.0) > 0.0
    ):
        assert vwap_obs.value is not None
        assert atr_obs.value is not None
        close_t = float(series.bars[-1].close)
        close_prev = float(series.bars[-2].close)
        dev = (close_t - vwap_obs.value) / atr_obs.value
        long_ok = (
            dev <= -rule.min_atr_deviation and close_t > close_prev and close_t < vwap_obs.value
        )
        short_ok = (
            dev >= rule.min_atr_deviation and close_t < close_prev and close_t > vwap_obs.value
        )
        dev_item = ChecklistItem(
            "vwap_deviation",
            "Giá đóng lệch ≥ 2 ATR khỏi Session VWAP và đảo chiều",
            f"dev={dev:+.2f} ATR (C {_fmt(close_t, 2)} / trước {_fmt(close_prev, 2)})",
            "lệch & đảo chiều",
            f"|dev| ≥ {_fmt(rule.min_atr_deviation, 1)} ATR về phía VWAP",
            long_ok or short_ok,
        )
    else:
        dev_item = ChecklistItem(
            "vwap_deviation",
            "Giá đóng lệch ≥ 2 ATR khỏi Session VWAP và đảo chiều",
            "thiếu dữ liệu",
            "lệch & đảo chiều",
            f"|dev| ≥ {_fmt(rule.min_atr_deviation, 1)} ATR về phía VWAP",
            None,
        )

    if series is not None and vwap_obs is not None:
        assert vwap_obs.value is not None
        close_t = float(series.bars[-1].close)
        gap_fraction = abs(vwap_obs.value - close_t) / close_t
        cost_item = ChecklistItem(
            "cost_hurdle",
            "Khoảng hồi về VWAP đạt tối thiểu 3× chi phí khứ hồi",
            f"{gap_fraction * 100:.3f}%",
            ">=",
            f"{rule.min_cost_hurdle_fraction * 100:.2f}%",
            gap_fraction >= rule.min_cost_hurdle_fraction,
        )
    else:
        cost_item = ChecklistItem(
            "cost_hurdle",
            "Khoảng hồi về VWAP đạt tối thiểu 3× chi phí khứ hồi",
            "thiếu dữ liệu",
            ">=",
            f"{rule.min_cost_hurdle_fraction * 100:.2f}%",
            None,
        )

    items = [session_item, range_item, dev_item, cost_item]
    _append_expanded_preconditions(
        items, rule.release_id, snapshot, settings, (("5m", atr_obs, True),)
    )
    return tuple(items)


def _h21(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    rule = H21MacdAdxMomentumRule()
    series_1h = _ready_series(snapshot, "1h", min_bars=35)
    series_4h = _ready_series(snapshot, "4h", min_bars=1)

    atr_obs = _ready_feature(snapshot, "atr_14", "1h")
    adx_obs = _ready_feature(snapshot, "adx_14", "1h")
    plus_di_obs = _ready_feature(snapshot, "plus_di_14", "1h")
    minus_di_obs = _ready_feature(snapshot, "minus_di_14", "1h")
    ema200_4h_obs = _ready_feature(snapshot, "ema_200", "4h")

    htf_dir: str | None = None
    if series_4h is not None and ema200_4h_obs is not None:
        assert ema200_4h_obs.value is not None
        close_4h = float(series_4h.bars[-1].close)
        if close_4h > ema200_4h_obs.value:
            htf_dir = "long"
        elif close_4h < ema200_4h_obs.value:
            htf_dir = "short"
        htf_item = ChecklistItem(
            "htf_trend",
            "Giá đóng 4h xác nhận xu hướng so với EMA200",
            f"đóng 4h {_fmt(close_4h, 2)} / EMA200 {_fmt(ema200_4h_obs.value, 2)}",
            ">" if htf_dir == "long" else "<" if htf_dir == "short" else "khác",
            "EMA200 4h",
            htf_dir is not None,
        )
    else:
        htf_item = ChecklistItem(
            "htf_trend",
            "Giá đóng 4h xác nhận xu hướng so với EMA200",
            "thiếu dữ liệu",
            "—",
            "—",
            None,
        )

    adx_item = ChecklistItem(
        "adx",
        "ADX14 1h đủ mạnh chống nhiễu",
        _fmt(adx_obs.value if adx_obs else None, 2),
        ">=",
        _fmt(rule.min_adx, 1),
        None if adx_obs is None or adx_obs.value is None else adx_obs.value >= rule.min_adx,
    )

    di_dir: str | None = None
    if plus_di_obs is not None and minus_di_obs is not None:
        assert plus_di_obs.value is not None
        assert minus_di_obs.value is not None
        if plus_di_obs.value > minus_di_obs.value:
            di_dir = "long"
        elif minus_di_obs.value > plus_di_obs.value:
            di_dir = "short"
        di_ok = di_dir == htf_dir if htf_dir is not None else (di_dir is not None)
        di_item = ChecklistItem(
            "di_alignment",
            "Cặp ±DI14 (1h) đồng thuận hướng xu hướng",
            f"+DI {_fmt(plus_di_obs.value, 2)} / -DI {_fmt(minus_di_obs.value, 2)}",
            "cùng chiều",
            "cùng hướng 4h EMA200" if htf_dir is not None else "+DI ≠ -DI",
            di_ok,
        )
    else:
        di_item = ChecklistItem(
            "di_alignment",
            "Cặp ±DI14 (1h) đồng thuận hướng xu hướng",
            "thiếu dữ liệu",
            "cùng chiều",
            "—",
            None,
        )

    target_dir = (
        di_dir if (di_dir is not None and (htf_dir is None or di_dir == htf_dir)) else htf_dir
    )
    if series_1h is not None:
        closes = [float(b.close) for b in series_1h.bars]
        macd_line, _, histogram = macd(closes, fast=12, slow=26, signal=9)
        hist_cur, hist_prev = histogram[-1], histogram[-2]
        macd_cur, macd_prev = macd_line[-1], macd_line[-2]
        if (
            hist_cur is not None
            and hist_prev is not None
            and macd_cur is not None
            and macd_prev is not None
        ):
            if target_dir == "long":
                cross_ok: bool | None = hist_prev <= 0.0 < hist_cur and macd_cur > macd_prev
            elif target_dir == "short":
                cross_ok = hist_prev >= 0.0 > hist_cur and macd_cur < macd_prev
            else:
                cross_ok = None
            macd_item = ChecklistItem(
                "macd_hist_cross",
                "MACD Histogram(12,26,9) chuyển pha cùng MACD line",
                f"Hist {_fmt(hist_prev, 4)} → {_fmt(hist_cur, 4)}",
                "đổi dấu 0",
                "cắt lên & MACD tăng"
                if target_dir == "long"
                else "cắt xuống & MACD giảm"
                if target_dir == "short"
                else "chưa rõ hướng",
                cross_ok,
            )
        else:
            macd_item = ChecklistItem(
                "macd_hist_cross",
                "MACD Histogram(12,26,9) chuyển pha cùng MACD line",
                "thiếu dữ liệu",
                "đổi dấu 0",
                "—",
                None,
            )
    else:
        macd_item = ChecklistItem(
            "macd_hist_cross",
            "MACD Histogram(12,26,9) chuyển pha cùng MACD line",
            "thiếu dữ liệu",
            "đổi dấu 0",
            "—",
            None,
        )

    items = [htf_item, adx_item, di_item, macd_item]
    _append_expanded_preconditions(
        items, rule.release_id, snapshot, settings, (("1h", atr_obs, True),)
    )
    return tuple(items)


def _h22(snapshot: DecisionSnapshot, settings: StrategySelection) -> tuple[ChecklistItem, ...]:
    rule = H22TakerFlowBreakoutRule()
    series_15m = _ready_series(snapshot, "15m", min_bars=rule.lookback + 1)
    series_1h = _ready_series(snapshot, "1h", min_bars=1)

    atr_15_obs = _ready_feature(snapshot, "atr_14", "15m")
    adx_15_obs = _ready_feature(snapshot, "adx_14", "15m")
    ema80_1h_obs = _ready_feature(snapshot, "ema_80", "1h")
    atr_1h_obs = _ready_feature(snapshot, "atr_14", "1h")

    ti_cur: float | None = None
    ti_avg3: float | None = None
    if series_15m is None:
        taker_item = ChecklistItem(
            "taker_flow_valid",
            "Dữ liệu taker_buy_base_volume 3 nến 15m hợp lệ",
            "thiếu dữ liệu",
            "hợp lệ",
            "0 ≤ taker_buy ≤ volume",
            None,
        )
    else:
        recent_3 = series_15m.bars[-3:]
        if any(item.taker_buy_base_volume is None for item in recent_3):
            taker_item = ChecklistItem(
                "taker_flow_valid",
                "Dữ liệu taker_buy_base_volume 3 nến 15m hợp lệ",
                "thiếu taker_buy_base_volume",
                "hợp lệ",
                "0 ≤ taker_buy ≤ volume",
                None,
            )
        elif any(
            item.volume <= 0
            or (item.taker_buy_base_volume is not None and item.taker_buy_base_volume < 0)
            or (item.taker_buy_base_volume is not None and item.taker_buy_base_volume > item.volume)
            for item in recent_3
        ):
            taker_item = ChecklistItem(
                "taker_flow_valid",
                "Dữ liệu taker_buy_base_volume 3 nến 15m hợp lệ",
                "dữ liệu ngoài biên [0, volume]",
                "hợp lệ",
                "0 ≤ taker_buy ≤ volume",
                False,
            )
        else:
            ti_values = [
                (2.0 * float(item.taker_buy_base_volume) - float(item.volume)) / float(item.volume)
                for item in recent_3
                if item.taker_buy_base_volume is not None
            ]
            ti_cur = ti_values[-1]
            ti_avg3 = sum(ti_values) / len(ti_values)
            taker_item = ChecklistItem(
                "taker_flow_valid",
                "Dữ liệu taker_buy_base_volume 3 nến 15m hợp lệ",
                f"TI={ti_cur:+.2f} (TB3={ti_avg3:+.2f})",
                "hợp lệ",
                "0 ≤ taker_buy ≤ volume",
                True,
            )

    adx_item = ChecklistItem(
        "adx",
        "ADX14 15m đủ mạnh",
        _fmt(adx_15_obs.value if adx_15_obs else None, 2),
        ">=",
        _fmt(rule.min_adx, 1),
        None
        if adx_15_obs is None or adx_15_obs.value is None
        else adx_15_obs.value >= rule.min_adx,
    )

    if series_15m is not None:
        bars_15m = series_15m.bars
        prior_vols = [float(b.volume) for b in bars_15m[-1 - rule.lookback : -1]]
        median_vol = median(prior_vols)
        cur_vol = float(bars_15m[-1].volume)
        vol_item = ChecklistItem(
            "volume_surge",
            f"Khối lượng 15m ≥ {rule.min_volume_multiple}× trung vị {rule.lookback} nến trước",
            _fmt(cur_vol, 2),
            ">=",
            _fmt(rule.min_volume_multiple * median_vol, 2),
            median_vol > 0.0 and cur_vol >= rule.min_volume_multiple * median_vol,
        )
    else:
        vol_item = ChecklistItem(
            "volume_surge",
            f"Khối lượng 15m ≥ {rule.min_volume_multiple}× trung vị {rule.lookback} nến trước",
            "thiếu dữ liệu",
            ">=",
            f"{rule.min_volume_multiple}× median",
            None,
        )

    if (
        series_15m is not None
        and series_1h is not None
        and ema80_1h_obs is not None
        and ti_cur is not None
        and ti_avg3 is not None
    ):
        assert ema80_1h_obs.value is not None
        bars_15m = series_15m.bars
        hh20_prev = max(float(b.high) for b in bars_15m[-1 - rule.lookback : -1])
        ll20_prev = min(float(b.low) for b in bars_15m[-1 - rule.lookback : -1])
        close_t = float(bars_15m[-1].close)
        close_1h = float(series_1h.bars[-1].close)
        long_ok = (
            close_t > hh20_prev
            and ti_cur >= rule.min_taker_imbalance
            and ti_avg3 >= rule.min_rolling3_imbalance
            and close_1h > ema80_1h_obs.value
        )
        short_ok = (
            close_t < ll20_prev
            and ti_cur <= -rule.min_taker_imbalance
            and ti_avg3 <= -rule.min_rolling3_imbalance
            and close_1h < ema80_1h_obs.value
        )
        breakout_item = ChecklistItem(
            "taker_imbalance_breakout",
            "Phá đỉnh/đáy 20 nến kèm Taker Imbalance & 1h EMA80",
            f"15m {_fmt(close_t, 2)} (biên {_fmt(ll20_prev, 2)}–{_fmt(hh20_prev, 2)}), "
            f"TI={ti_cur:+.2f}, 1h {_fmt(close_1h, 2)}/EMA80 {_fmt(ema80_1h_obs.value, 2)}",
            "đồng thuận",
            f"|TI| ≥ {rule.min_taker_imbalance} & |TB3| ≥ {rule.min_rolling3_imbalance}",
            long_ok or short_ok,
        )
    else:
        breakout_item = ChecklistItem(
            "taker_imbalance_breakout",
            "Phá đỉnh/đáy 20 nến kèm Taker Imbalance & 1h EMA80",
            "thiếu dữ liệu",
            "đồng thuận",
            f"|TI| ≥ {rule.min_taker_imbalance} & |TB3| ≥ {rule.min_rolling3_imbalance}",
            None,
        )

    items = [taker_item, adx_item, vol_item, breakout_item]
    _append_expanded_preconditions(
        items,
        rule.release_id,
        snapshot,
        settings,
        (("15m", atr_15_obs, True), ("1h", atr_1h_obs, False)),
    )
    return tuple(items)


@lru_cache(maxsize=1)
def _strategy_cards_by_release() -> dict[str, StrategyCard]:
    from trading_bot.application.strategy_pipeline.card_strategy import strategy_card_release_id
    from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards

    return {strategy_card_release_id(card): card for card in build_default_demo_cards()}


def _find_node_decimal(node_values: dict[str, Any], substrings: tuple[str, ...]) -> Decimal | None:
    for key, val in node_values.items():
        lowered = key.lower()
        if any(sub in lowered for sub in substrings) and isinstance(val, (int, float, Decimal)):
            return Decimal(str(val))
    return None


def _find_source_for_node(
    node_id: str,
    card: StrategyCard,
    node_values: dict[str, Any],
    fallback_substrings: tuple[str, ...],
) -> str | None:
    connected = [
        edge.from_node
        for edge in card.edges
        if edge.to_node == node_id
        and edge.from_node in node_values
        and edge.to_port not in {"flag", "gate", "price"}
    ]
    for prefix in ("l3", "l2", "l1"):
        for src_id in connected:
            if src_id.lower().startswith(prefix) and isinstance(
                node_values.get(src_id), (int, float, Decimal)
            ):
                return src_id
    for key in node_values:
        if any(sub in key.lower() for sub in fallback_substrings) and isinstance(
            node_values.get(key), (int, float, Decimal)
        ):
            return key
    return None


def _card_l4_items(
    card: StrategyCard,
    snapshot: DecisionSnapshot,
    result: PipelineExecutionResult,
    *,
    warmup_sources: set[str],
    atr_single_bar_source: str,
) -> list[ChecklistItem]:
    macro_blocked = bool(getattr(snapshot, "macro_blackout", False))
    items: list[ChecklistItem] = [
        ChecklistItem(
            "macro_clear",
            "Không trong khoảng cấm quanh sự kiện vĩ mô",
            "đang cấm" if macro_blocked else "rõ",
            "=",
            "không cấm",
            not macro_blocked,
        )
    ]
    bar = snapshot.series[0].bars[-1] if (snapshot.series and snapshot.series[0].bars) else None
    l4_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L4_CONTEXT_GATE]
    atr_added = False

    for node in l4_nodes:
        op = node.operation.lower()
        params = node.parameters
        if "rv_regime" in op or "realized_volatility" in op:
            min_rv = Decimal(str(params["min_rv"])) if "min_rv" in params else None
            max_rv = Decimal(str(params["max_rv"])) if "max_rv" in params else None
            rv_src = next(
                (src for nid, src in result.indicator_sources.items() if "rv" in nid.lower()), ""
            )
            rv_val = _find_node_decimal(result.node_values, ("rv",))
            if macro_blocked:
                rv_passed: bool | None = None
                rv_actual = "ngắt mạch macro blackout"
            elif rv_src in warmup_sources or rv_val is None:
                rv_passed = None
                rv_actual = "thiếu dữ liệu"
            else:
                rv_passed = (min_rv is None or rv_val >= min_rv) and (
                    max_rv is None or rv_val <= max_rv
                )
                rv_actual = _fmt(rv_val, 4)
            items.append(
                ChecklistItem(
                    "l4_rv_regime",
                    "Cổng L4: Biến động thực tế RV trong dải an toàn",
                    rv_actual,
                    "trong",
                    f"[{_fmt(min_rv, 4)}, {_fmt(max_rv, 4)}]",
                    rv_passed,
                )
            )
        elif "adx" in op:
            min_adx = Decimal(str(params["min_adx"])) if "min_adx" in params else None
            max_adx = Decimal(str(params["max_adx"])) if "max_adx" in params else None
            adx_src = next(
                (src for nid, src in result.indicator_sources.items() if "adx" in nid.lower()), ""
            )
            adx_val = _find_node_decimal(result.node_values, ("adx",))
            if min_adx is not None and max_adx is None:
                adx_op, adx_thr = ">=", _fmt(min_adx, 2)
            elif max_adx is not None and min_adx is None:
                adx_op, adx_thr = "<", _fmt(max_adx, 2)
            else:
                adx_op, adx_thr = "trong", f"[{_fmt(min_adx, 2)}, {_fmt(max_adx, 2)})"
            if macro_blocked:
                adx_passed: bool | None = None
                adx_actual = "ngắt mạch macro blackout"
            elif adx_src in warmup_sources or adx_val is None:
                adx_passed = None
                adx_actual = "thiếu dữ liệu"
            else:
                adx_passed = (min_adx is None or adx_val >= min_adx) and (
                    max_adx is None or adx_val < max_adx
                )
                adx_actual = _fmt(adx_val, 2)
            items.append(
                ChecklistItem(
                    "l4_adx_regime",
                    "Cổng L4: Chế độ xu hướng ADX14",
                    adx_actual,
                    adx_op,
                    adx_thr,
                    adx_passed,
                )
            )

        if not atr_added and (
            "volatility" in op or "atr" in op or "min_atr" in params or "max_atr" in params
        ):
            atr_added = True
            min_atr = Decimal(str(params["min_atr"])) if "min_atr" in params else None
            max_atr = Decimal(str(params["max_atr"])) if "max_atr" in params else None
            atr_src = next(
                (src for nid, src in result.indicator_sources.items() if "atr" in nid.lower()), ""
            )
            atr_val = _find_node_decimal(result.node_values, ("atr",))
            if atr_val is None and bar is not None:
                atr_val = bar.high - bar.low
            atr_op = ">=" if max_atr is None else "trong"
            atr_thr = (
                _fmt(min_atr, 4) if max_atr is None else f"[{_fmt(min_atr, 4)}, {_fmt(max_atr, 4)}]"
            )
            if macro_blocked:
                atr_passed: bool | None = None
                atr_actual = "ngắt mạch macro blackout"
            elif atr_val is None:
                atr_passed = None
                atr_actual = "thiếu dữ liệu"
            else:
                atr_ok = (min_atr is None or atr_val >= min_atr) and (
                    max_atr is None or atr_val <= max_atr
                )
                # Nếu chỉ là biên độ 1 nến mà vượt min_atr, coi là thiếu warmup ATR; nếu dưới
                # min_atr thì cổng L4 thực sự chặn nên ghi False.
                atr_passed = (
                    False if not atr_ok else (None if atr_src == atr_single_bar_source else True)
                )
                atr_actual = _fmt(atr_val, 4)
            items.append(
                ChecklistItem(
                    "l4_volatility_atr",
                    "Cổng L4: Biên độ biến động ATR",
                    atr_actual,
                    atr_op,
                    atr_thr,
                    atr_passed,
                )
            )

    return items


def _card_l5_items(
    card: StrategyCard,
    snapshot: DecisionSnapshot,
    result: PipelineExecutionResult,
    *,
    warmup_sources: set[str],
) -> list[ChecklistItem]:
    l5_nodes = [n for n in card.nodes if n.layer == CompositionLayer.L5_STRATEGY_TRIGGER]
    items: list[ChecklistItem] = []
    bar = snapshot.series[0].bars[-1] if (snapshot.series and snapshot.series[0].bars) else None

    for node in l5_nodes:
        op = node.operation.lower()
        params = node.parameters
        if result.trigger_result is None or bar is None:
            if "wick_reversal" in op:
                items.extend(
                    [
                        ChecklistItem(
                            "l5_zscore_extreme",
                            "Ngòi nổ L5: Z-Score 20 đạt cực trị đảo chiều",
                            "ngắt mạch tại L4",
                            "|Z20| >=",
                            _fmt(params.get("z_threshold", 1.5), 2),
                            None,
                        ),
                        ChecklistItem(
                            "l5_wick_reversal",
                            "Ngòi nổ L5: Nến rút chân & thân hẹp",
                            "ngắt mạch tại L4",
                            "thân & râu",
                            "đạt ngưỡng",
                            None,
                        ),
                    ]
                )
            elif "vwap_reversion" in op:
                items.extend(
                    [
                        ChecklistItem(
                            "l5_vwap_deviation",
                            "Ngòi nổ L5: Độ lệch chuẩn ATR khỏi Session VWAP",
                            "ngắt mạch tại L4",
                            "|dev| >=",
                            _fmt(params.get("dev_threshold", 1.75), 2),
                            None,
                        ),
                        ChecklistItem(
                            "l5_close_location",
                            "Ngòi nổ L5: Vị trí đóng cửa xác nhận đảo chiều",
                            "ngắt mạch tại L4",
                            "xác nhận",
                            "≥ 0.5 (LONG) / ≤ 0.5 (SHORT)",
                            None,
                        ),
                    ]
                )
            else:
                items.append(
                    ChecklistItem(
                        node.node_id,
                        f"Ngòi nổ L5 ({node.operation})",
                        "ngắt mạch tại L4",
                        ">=",
                        _fmt(params.get("threshold", 0.5), 2),
                        None,
                    )
                )
            continue

        if "wick_reversal" in op:
            z_thresh = Decimal(str(params.get("z_threshold", "1.5")))
            min_wick = Decimal(str(params.get("min_wick", "0.45")))
            max_body = Decimal(str(params.get("max_body", "0.45")))
            z_src = result.indicator_sources.get("l3_zscore", "")
            z_val = _find_node_decimal(result.node_values, ("zscore", "l3_zscore"))
            body_val = _find_node_decimal(result.node_values, ("l1_body", "body"))
            lower_val = lower_wick_ratio(bar.open, bar.high, bar.low, bar.close)
            upper_val = upper_wick_ratio(bar.open, bar.high, bar.low, bar.close)
            if z_src in warmup_sources or z_val is None or body_val is None:
                items.extend(
                    [
                        ChecklistItem(
                            "l5_zscore_extreme",
                            "Ngòi nổ L5: Z-Score 20 đạt cực trị đảo chiều",
                            "thiếu dữ liệu",
                            "|Z20| >=",
                            _fmt(z_thresh, 2),
                            None,
                        ),
                        ChecklistItem(
                            "l5_wick_reversal",
                            "Ngòi nổ L5: Nến rút chân & thân hẹp",
                            "thiếu dữ liệu",
                            "thân & râu",
                            f"thân ≤ {_fmt(max_body, 2)} & râu ≥ {_fmt(min_wick, 2)}",
                            None,
                        ),
                    ]
                )
            else:
                z_passed = abs(z_val) >= z_thresh
                items.append(
                    ChecklistItem(
                        "l5_zscore_extreme",
                        "Ngòi nổ L5: Z-Score 20 đạt cực trị đảo chiều",
                        _fmt(z_val, 4),
                        "|Z20| >=",
                        _fmt(z_thresh, 2),
                        z_passed,
                    )
                )
                if z_val <= -z_thresh:
                    wick_passed: bool | None = body_val <= max_body and lower_val >= min_wick
                    wick_actual = f"thân {_fmt(body_val, 2)} / râu dưới {_fmt(lower_val, 2)}"
                    wick_thr = f"thân ≤ {_fmt(max_body, 2)} & râu dưới ≥ {_fmt(min_wick, 2)}"
                elif z_val >= z_thresh:
                    wick_passed = body_val <= max_body and upper_val >= min_wick
                    wick_actual = f"thân {_fmt(body_val, 2)} / râu trên {_fmt(upper_val, 2)}"
                    wick_thr = f"thân ≤ {_fmt(max_body, 2)} & râu trên ≥ {_fmt(min_wick, 2)}"
                else:
                    wick_passed = None
                    wick_actual = f"thân {_fmt(body_val, 2)} (chưa rõ hướng Z)"
                    wick_thr = f"thân ≤ {_fmt(max_body, 2)} & râu ≥ {_fmt(min_wick, 2)}"
                items.append(
                    ChecklistItem(
                        "l5_wick_reversal",
                        "Ngòi nổ L5: Nến rút chân & thân hẹp",
                        wick_actual,
                        "đạt",
                        wick_thr,
                        wick_passed,
                    )
                )
        elif "vwap_reversion" in op:
            dev_thresh = Decimal(str(params.get("dev_threshold", "1.75")))
            dev_src = result.indicator_sources.get("l3_vwap_dev", "")
            dev_val = _find_node_decimal(result.node_values, ("vwap_dev", "l3_vwap_dev"))
            loc_val = _find_node_decimal(result.node_values, ("l1_location", "location"))
            if dev_src in warmup_sources or dev_val is None:
                items.extend(
                    [
                        ChecklistItem(
                            "l5_vwap_deviation",
                            "Ngòi nổ L5: Độ lệch chuẩn ATR khỏi Session VWAP",
                            "thiếu dữ liệu",
                            "|dev| >=",
                            _fmt(dev_thresh, 2),
                            None,
                        ),
                        ChecklistItem(
                            "l5_close_location",
                            "Ngòi nổ L5: Vị trí đóng cửa xác nhận đảo chiều",
                            "thiếu dữ liệu",
                            "xác nhận",
                            "≥ 0.5 (LONG) / ≤ 0.5 (SHORT)",
                            None,
                        ),
                    ]
                )
            else:
                items.append(
                    ChecklistItem(
                        "l5_vwap_deviation",
                        "Ngòi nổ L5: Độ lệch chuẩn ATR khỏi Session VWAP",
                        _fmt(dev_val, 4),
                        "|dev| >=",
                        _fmt(dev_thresh, 2),
                        abs(dev_val) >= dev_thresh,
                    )
                )
                if dev_val <= -dev_thresh:
                    loc_passed: bool | None = loc_val is None or loc_val >= Decimal("0.5")
                    loc_op, loc_thr = ">=", "0.5"
                elif dev_val >= dev_thresh:
                    loc_passed = loc_val is None or loc_val <= Decimal("0.5")
                    loc_op, loc_thr = "<=", "0.5"
                else:
                    loc_passed = None
                    loc_op, loc_thr = "xác nhận", "≥ 0.5 (LONG) / ≤ 0.5 (SHORT)"
                items.append(
                    ChecklistItem(
                        "l5_close_location",
                        "Ngòi nổ L5: Vị trí đóng cửa xác nhận đảo chiều",
                        _fmt(loc_val, 4),
                        loc_op,
                        loc_thr,
                        loc_passed,
                    )
                )
        elif any(
            name in op
            for name in ("tsmom", "squeeze_breakout", "pullback", "macd_adx", "taker_flow")
        ):
            threshold = Decimal(str(params.get("threshold", "0.25")))
            src_id = _find_source_for_node(
                node.node_id,
                card,
                result.node_values,
                ("l3_", "tsmom", "squeeze", "pullback", "macd", "flow"),
            )
            src_origin = result.indicator_sources.get(src_id or "", "")
            score_val = (
                Decimal(str(result.node_values[src_id]))
                if src_id is not None
                and isinstance(result.node_values.get(src_id), (int, float, Decimal))
                else None
            )
            if src_id is None or src_origin in warmup_sources or score_val is None:
                passed: bool | None = None
                actual = "thiếu dữ liệu"
            else:
                passed = abs(score_val) >= threshold
                actual = _fmt(score_val, 4)
            items.append(
                ChecklistItem(
                    node.node_id,
                    f"Ngòi nổ L5 ({node.operation})",
                    actual,
                    "|điểm| >=",
                    _fmt(threshold, 2),
                    passed,
                )
            )
        else:
            threshold = Decimal(str(params.get("threshold", "0.5")))
            src_id = _find_source_for_node(
                node.node_id, card, result.node_values, ("wick", "body", "location")
            )
            score_val = (
                Decimal(str(result.node_values[src_id]))
                if src_id is not None
                and isinstance(result.node_values.get(src_id), (int, float, Decimal))
                else None
            )
            items.append(
                ChecklistItem(
                    node.node_id,
                    f"Ngòi nổ L5 ({node.operation})",
                    _fmt(score_val, 4),
                    ">=",
                    _fmt(threshold, 2),
                    None if score_val is None else score_val >= threshold,
                )
            )
    return items


def _strategy_card_checklist(
    card: StrategyCard, snapshot: DecisionSnapshot
) -> tuple[ChecklistItem, ...]:
    """Checklist L0–L5 trực tiếp từ ``StrategyPipelineEngine.execute()`` cho Strategy Card."""
    from trading_bot.application.strategy_pipeline.pipeline_engine import (
        ATR_SOURCE_SINGLE_BAR,
        WARMUP_SOURCE_INSUFFICIENT,
        StrategyPipelineEngine,
    )

    result = StrategyPipelineEngine().execute(card, snapshot)
    warmup_sources = {WARMUP_SOURCE_INSUFFICIENT, "taker_flow_unavailable"}

    if result.status == "DATA_HYGIENE_FAILED":
        has_bars = bool(snapshot.series and snapshot.series[0].bars)
        l0_item = ChecklistItem(
            "l0_data_hygiene",
            "Dữ liệu nến L0 hợp lệ",
            result.reasons[0] if (has_bars and result.reasons) else "thiếu dữ liệu",
            "hợp lệ",
            "chuỗi nến đóng OHLC > 0",
            False if has_bars else None,
        )
        warmup_item = ChecklistItem(
            "indicator_warmup",
            "Đủ nến warmup / feature chỉ báo L2–L3",
            "chưa đánh giá (lỗi L0)" if has_bars else "thiếu dữ liệu",
            "đầy đủ",
            "tất cả node L2–L3 sẵn sàng",
            None,
        )
        l4_items = _card_l4_items(
            card,
            snapshot,
            result,
            warmup_sources=warmup_sources,
            atr_single_bar_source=ATR_SOURCE_SINGLE_BAR,
        )
        l4_items = [
            ChecklistItem(
                item.key,
                item.label_vi,
                "chưa đánh giá (lỗi L0)",
                item.operator,
                item.threshold,
                None,
            )
            for item in l4_items
        ]
        l5_items = _card_l5_items(card, snapshot, result, warmup_sources=warmup_sources)
        return (l0_item, warmup_item, *l4_items, *l5_items)

    l0_item = ChecklistItem(
        "l0_data_hygiene",
        "Dữ liệu nến L0 hợp lệ",
        "hợp lệ",
        "hợp lệ",
        "chuỗi nến đóng OHLC > 0",
        True,
    )
    all_unwarmed = sorted(
        node_id
        for node_id, src in result.indicator_sources.items()
        if src in warmup_sources or src == ATR_SOURCE_SINGLE_BAR
    )
    warmup_total = len(result.indicator_sources)
    if all_unwarmed:
        warmup_item = ChecklistItem(
            "indicator_warmup",
            "Đủ nến warmup / feature chỉ báo L2–L3",
            f"thiếu tại {', '.join(all_unwarmed)}",
            "đầy đủ",
            "tất cả node L2–L3 có feature hoặc đủ nến warmup",
            None,
        )
    else:
        warmup_item = ChecklistItem(
            "indicator_warmup",
            "Đủ nến warmup / feature chỉ báo L2–L3",
            f"{warmup_total}/{warmup_total} node sẵn sàng",
            "đầy đủ",
            "tất cả node L2–L3 có feature hoặc đủ nến warmup",
            True,
        )

    l4_items = _card_l4_items(
        card,
        snapshot,
        result,
        warmup_sources=warmup_sources,
        atr_single_bar_source=ATR_SOURCE_SINGLE_BAR,
    )
    l5_items = _card_l5_items(card, snapshot, result, warmup_sources=warmup_sources)
    return (l0_item, warmup_item, *l4_items, *l5_items)


_BUILDERS: dict[str, Callable[[DecisionSnapshot, StrategySelection], tuple[ChecklistItem, ...]]] = {
    "H01": _h01,
    "H03": _h03,
    "H06": _h06,
}

_EXPANDED_RULE_BUILDERS: dict[
    str, Callable[[DecisionSnapshot, StrategySelection], tuple[ChecklistItem, ...]]
] = {
    "h02-v1": _h02,
    "h04-v1": _h04,
    "h05-v1": _h05,
    "h07-v1": _h07,
    "h21-v1": _h21,
    "h22-v1": _h22,
}


def strategy_checklist(
    release_id: str, snapshot: DecisionSnapshot, settings: StrategySelection
) -> tuple[ChecklistItem, ...]:
    """Checklist cho các rule H01..H22 và Strategy Cards ``sc-binance-usdm-*``; khác trả rỗng."""
    # H03 v3: mỗi release một khung vào lệnh (15m/1h/4h) — checklist đọc đúng khung đó.
    v3_timeframe = H03_V3_TIMEFRAMES.get(release_id)
    if v3_timeframe is not None:
        return _h03(snapshot, settings, v3_timeframe)
    normalized = release_id.lower()
    expanded_builder = _EXPANDED_RULE_BUILDERS.get(normalized)
    if expanded_builder is not None:
        return expanded_builder(snapshot, settings)
    card = _strategy_cards_by_release().get(normalized)
    if card is not None:
        return _strategy_card_checklist(card, snapshot)
    builder = _BUILDERS.get(release_id.split("-", 1)[0].upper())
    if builder is None:
        return ()
    return builder(snapshot, settings)
