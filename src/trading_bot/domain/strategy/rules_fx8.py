"""Các release cải tiến FX-8 (CRY-30) cho H01, H03 và H06.

Mỗi thay đổi là một release mới (``EXPERIMENTAL_UNVALIDATED``) đặt trong module riêng biệt để
không làm thay đổi ``release_content_hash`` của các release cũ trong ``rules.py``, ``rules_v2.py``
và ``rules_v3.py``:

- ``h01-v2`` (``H01EmaTrendRuleV2``): cửa sổ vào lệnh sau giao cắt EMA20/EMA80 causal trong
  ``cross_window_bars`` nến 1h gần nhất (mặc định 3), lọc khoảng cách EMA tối thiểu theo ATR
  (``min_ema_gap_atr_multiple``, mặc định 0.15), và cooldown sau lệnh thoát ``STOP_LOSS``
  (``cooldown_bars_after_stop``, mặc định 6 nến 1h).
- ``h03-v4-15m``, ``h03-v4-1h``, ``h03-v4-4h``: phá vỡ kênh Donchian kèm biên đệm ATR
  (``breakout_atr_buffer``, mặc định 0.25), volume vượt bội số median (``volume_median_multiplier``,
  mặc định 1.5), lọc regime chống đi ngang (``min_adx=20.0``, ``min_efficiency_ratio=0.25``) và
  kênh thoát Donchian 20 nến cùng khung.
- ``h06-v3-1h`` (``H06MeanReversionRuleV31h``): hồi quy về trung bình trên khung ``1h``, xác nhận
  nến đảo chiều (đóng thuận chiều hoặc râu cản ≥ 40% biên độ nến), đặt mục tiêu chốt lời về trung
  bình 20 nến (kiểm biên tối thiểu ``min_tp_atr_multiple=1.2``) kèm thoát theo ``zscore_20`` về 0
  và giữ nguyên guard lịch vĩ mô chính thức AI-4 (fail-closed).
"""

from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal
from statistics import median
from types import MappingProxyType

from trading_bot.domain.market.features import ema
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    NoTrade,
    Strategy,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.rules import (
    _candidate,
    _inputs,
    _no,
    _value,
    release_content_hash,
)
from trading_bot.domain.strategy.rules_v2 import (
    EXIT_EVIDENCE_BY_RECIPE,
    ExitEvidenceSpec,
)
from trading_bot.domain.strategy.rules_v3 import EXIT_EVIDENCE_BY_RECIPE_V3

# SHA-256 của `rules.py` tại thời điểm phát hành FX-8 (dùng chung helper `_inputs`/`_candidate`).
BASE_RULES_SOURCE_SHA256 = "9c2f3d708f48afd657c05234142dbbb71ed64f436771e81b9d0fc5ca9c47369d"

H03_V4_TIMEFRAMES = MappingProxyType(
    {
        "h03-v4-15m": "15m",
        "h03-v4-1h": "1h",
        "h03-v4-4h": "4h",
    }
)

EXIT_EVIDENCE_BY_RECIPE_FX8 = MappingProxyType(
    {
        **{
            f"h03-exit-v4-{timeframe}": ExitEvidenceSpec(timeframe=timeframe, channel_period=20)
            for timeframe in H03_V4_TIMEFRAMES.values()
        },
        "h06-exit-v3-1h": ExitEvidenceSpec(timeframe="1h", feature_name="zscore_20"),
    }
)

_H01_V2_ALLOWED_PARAMS = frozenset(
    {"cross_window_bars", "min_ema_gap_atr_multiple", "cooldown_bars_after_stop"}
)
_H03_V4_ALLOWED_PARAMS = frozenset(
    {
        "donchian_lookback",
        "volume_median_lookback",
        "breakout_atr_buffer",
        "volume_median_multiplier",
        "min_adx",
        "min_efficiency_ratio",
    }
)
_H06_V3_ALLOWED_PARAMS = frozenset({"min_tp_atr_multiple"})


def exit_evidence_spec(recipe_id: str) -> ExitEvidenceSpec | None:
    """Bằng chứng thoát cho recipe v2/v3/FX-8; None nghĩa là recipe không dùng (toàn bộ v1)."""
    return (
        EXIT_EVIDENCE_BY_RECIPE_FX8.get(recipe_id)
        or EXIT_EVIDENCE_BY_RECIPE_V3.get(recipe_id)
        or EXIT_EVIDENCE_BY_RECIPE.get(recipe_id)
    )


def _check_selection(
    release_id: str, strategy_id: str, settings: StrategySelection
) -> NoTrade | None:
    """Kiểm tra cấu hình đã công bố và chọn đúng release FX-8."""
    if not settings.published:
        return _no(
            strategy_id,
            f"{strategy_id}_SETTINGS_UNPUBLISHED",
            "Cấu hình chiến lược chưa được công bố.",
        )
    if release_id not in settings.active_release_ids:
        return _no(
            strategy_id,
            f"{strategy_id}_NOT_SELECTED",
            "Bản phát hành chiến lược không nằm trong cấu hình đang chọn.",
        )
    return None


def _check_allowed_parameters(
    strategy_id: str,
    release_id: str,
    settings: StrategySelection,
    allowed: frozenset[str],
) -> NoTrade | None:
    """Từ chối tham số không thuộc phạm vi của release hiện hành."""
    unsupported = [key for key, _ in settings.parameters if key not in allowed]
    if unsupported:
        return _no(
            strategy_id,
            f"{strategy_id}_UNSUPPORTED_PARAMETERS",
            f"Release {release_id} không hỗ trợ tham số: {', '.join(unsupported)}.",
        )
    return None


def _has_recent_ema_cross(
    bars: tuple[OHLCVBar, ...],
    *,
    direction: str,
    cross_window_bars: int,
    fast_current: float,
    slow_current: float,
    fast_previous: float | None,
    slow_previous: float | None,
) -> bool:
    """Kiểm tra giao cắt EMA20/EMA80 causal trong ``cross_window_bars`` nến đóng gần nhất.

    Khi chuỗi nến đủ dài cho warmup EMA80 (≥ ``80 + cross_window_bars`` nến), tính chuỗi EMA
    causal trực tiếp từ giá đóng nến lịch sử để phát hiện điểm giao cắt cách hiện tại tối đa
    ``cross_window_bars`` nến mà không nhìn trước tương lai. Với chuỗi ngắn (snapshot tối giản),
    fallback về so sánh ``previous_value`` và giá trị hiện tại trên nến gần nhất.
    """
    n_bars = len(bars)
    if n_bars >= 80 + cross_window_bars:
        closes = [float(bar.close) for bar in bars]
        ema20_series = ema(closes, 20)
        ema80_series = ema(closes, 80)
        start_idx = max(80, n_bars - cross_window_bars)
        for idx in range(start_idx, n_bars):
            prev_fast = ema20_series[idx - 1]
            prev_slow = ema80_series[idx - 1]
            curr_fast = ema20_series[idx]
            curr_slow = ema80_series[idx]
            if prev_fast is None or prev_slow is None or curr_fast is None or curr_slow is None:
                continue
            if direction == "long" and prev_fast <= prev_slow and curr_fast > curr_slow:
                # Xác nhận sau điểm giao cắt không cắt ngược lại cho tới nến hiện tại.
                if all(
                    ema20_series[j] is not None
                    and ema80_series[j] is not None
                    and ema20_series[j] > ema80_series[j]  # type: ignore[operator]
                    for j in range(idx, n_bars)
                ):
                    return True
            if direction == "short" and prev_fast >= prev_slow and curr_fast < curr_slow:
                if all(
                    ema20_series[j] is not None
                    and ema80_series[j] is not None
                    and ema20_series[j] < ema80_series[j]  # type: ignore[operator]
                    for j in range(idx, n_bars)
                ):
                    return True
        return False

    if fast_previous is None or slow_previous is None:
        return False
    if direction == "long":
        return fast_previous <= slow_previous and fast_current > slow_current
    return fast_previous >= slow_previous and fast_current < slow_current


class H01EmaTrendRuleV2:
    """H01 v2 (FX-8): cửa sổ giao cắt EMA20/80 1h, khoảng cách EMA theo ATR và cooldown sau stop."""

    strategy_id = "H01"
    release_id = "h01-v2"
    __slots__ = ()
    recipe = ExitRecipe(
        "h01-exit-v2",
        2.0,
        trailing_atr_multiple=3.0,
        opposite_signal_exit=True,
        max_hold_bars=120,
    )

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        denied_params = _check_allowed_parameters(
            self.strategy_id, self.release_id, settings, _H01_V2_ALLOWED_PARAMS
        )
        if denied_params is not None:
            return denied_params
        denied = _check_selection(self.release_id, self.strategy_id, settings)
        if denied is not None:
            return denied

        params = dict(settings.parameters)
        cross_window_bars = int(params.get("cross_window_bars", 3))
        min_ema_gap_atr_multiple = float(params.get("min_ema_gap_atr_multiple", 0.15))
        cooldown_bars_after_stop = int(params.get("cooldown_bars_after_stop", 6))

        if snapshot.position_is_flat is None:
            return _no(
                self.strategy_id,
                "H01_POSITION_UNKNOWN",
                "Chưa xác định được vị thế đang flat hay không.",
            )
        if snapshot.position_is_flat is not True:
            return _no(
                self.strategy_id,
                "H01_POSITION_NOT_FLAT",
                "H01 chỉ tạo candidate khi không có vị thế đang mở.",
            )

        if cooldown_bars_after_stop > 0 and snapshot.last_stop_exit_at is not None:
            if snapshot.last_stop_exit_at > snapshot.decision_time:
                return _no(
                    self.strategy_id,
                    "H01_INPUT_UNAVAILABLE",
                    "Thời điểm stop gần nhất nằm sau decision_time.",
                )
            cooldown = timedelta(hours=cooldown_bars_after_stop)
            elapsed = snapshot.decision_time - snapshot.last_stop_exit_at
            if elapsed < cooldown:
                return _no(
                    self.strategy_id,
                    "H01_POST_STOP_COOLDOWN",
                    f"Đang trong cooldown {cooldown_bars_after_stop} nến 1h sau khi chạm stop.",
                    gate_details=(
                        f"elapsed_seconds={int(elapsed.total_seconds())}; "
                        f"required >= {int(cooldown.total_seconds())}",
                    ),
                )

        one_hour = _inputs(
            snapshot, "1h", ("ema_20", "ema_80", "adx_14", "atr_14"), self.strategy_id
        )
        if isinstance(one_hour, NoTrade):
            return one_hour
        four_hour = _inputs(snapshot, "4h", ("ema_200",), self.strategy_id)
        if isinstance(four_hour, NoTrade):
            return four_hour

        series, features = one_hour
        h4_series, h4_features = four_hour
        if len(series.bars) < 2:
            return _no(
                self.strategy_id,
                "H01_INSUFFICIENT_BARS",
                "Cần tối thiểu hai nến 1h đã đóng để xác định giao cắt.",
            )

        fast = features["ema_20"]
        slow = features["ema_80"]
        fast_value = _value(features, "ema_20")
        slow_value = _value(features, "ema_80")
        if fast_value > slow_value:
            direction = "long"
        elif fast_value < slow_value:
            direction = "short"
        else:
            return _no(
                self.strategy_id,
                "H01_NO_CROSS",
                f"EMA20 không ở rõ một phía so với EMA80 trong cửa sổ {cross_window_bars} nến 1h.",
            )

        if len(series.bars) < 80 + cross_window_bars and (
            fast.previous_value is None or slow.previous_value is None
        ):
            return _no(
                self.strategy_id,
                "H01_FEATURE_UNAVAILABLE",
                "Thiếu chuỗi nến 1h hoặc EMA của nến trước để xác nhận giao cắt.",
                missing=("1h:ema_20_previous", "1h:ema_80_previous"),
            )

        if not _has_recent_ema_cross(
            series.bars,
            direction=direction,
            cross_window_bars=cross_window_bars,
            fast_current=fast_value,
            slow_current=slow_value,
            fast_previous=fast.previous_value,
            slow_previous=slow.previous_value,
        ):
            return _no(
                self.strategy_id,
                "H01_NO_CROSS",
                f"Không có giao cắt EMA20/EMA80 hợp lệ trong {cross_window_bars} nến 1h gần nhất.",
            )

        h4_close = h4_series.bars[-1].close
        h4_ema = _value(h4_features, "ema_200")
        if (direction == "long" and Decimal(str(h4_ema)) >= h4_close) or (
            direction == "short" and Decimal(str(h4_ema)) <= h4_close
        ):
            return _no(
                self.strategy_id,
                "H01_HTF_TREND_MISMATCH",
                "Giá đóng 4h không cùng phía EMA200 với hướng giao cắt.",
            )

        adx = _value(features, "adx_14")
        if adx < 20.0:
            return _no(
                self.strategy_id,
                "H01_ADX_BELOW_20",
                "ADX14 1h phải đạt tối thiểu 20.",
                gate_details=(f"adx_14={adx}; required >= 20",),
            )

        atr = _value(features, "atr_14")
        if atr <= 0:
            return _no(
                self.strategy_id,
                "H01_FEATURE_UNAVAILABLE",
                "ATR14 phải dương.",
                missing=("1h:atr_14",),
            )

        ema_gap = abs(fast_value - slow_value)
        required_gap = min_ema_gap_atr_multiple * atr
        if ema_gap < required_gap:
            return _no(
                self.strategy_id,
                "H01_EMA_GAP_TOO_NARROW",
                f"Khoảng cách |EMA20 - EMA80| nhỏ hơn {min_ema_gap_atr_multiple} × ATR14.",
                gate_details=(f"ema_gap={ema_gap:.6f}; required >= {required_gap:.6f}",),
            )

        return _candidate(
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            snapshot=snapshot,
            bar=series.bars[-1],
            direction=direction,
            atr=atr,
            recipe=self.recipe,
            evidence=(fast, slow, h4_features["ema_200"], features["adx_14"], features["atr_14"]),
            evidence_bars=(series.bars[-2], series.bars[-1], h4_series.bars[-1]),
            settings=settings,
            release_hash=release_content_hash(self),
        )


class _H03DonchianBreakoutV4ByTimeframe:
    """H03 v4 (FX-8): phá kênh Donchian có biên đệm ATR, bội số median volume và lọc regime."""

    strategy_id = "H03"
    release_id = "h03-v4-1h"
    entry_timeframe = "1h"
    __slots__ = ()
    recipe: ExitRecipe

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        denied_params = _check_allowed_parameters(
            self.strategy_id, self.release_id, settings, _H03_V4_ALLOWED_PARAMS
        )
        if denied_params is not None:
            return denied_params
        denied = _check_selection(self.release_id, self.strategy_id, settings)
        if denied is not None:
            return denied

        timeframe = self.entry_timeframe
        params = dict(settings.parameters)
        donchian_lookback = int(params.get("donchian_lookback", 55))
        volume_median_lookback = int(params.get("volume_median_lookback", 20))
        breakout_atr_buffer = float(params.get("breakout_atr_buffer", 0.25))
        volume_median_multiplier = float(params.get("volume_median_multiplier", 1.5))
        min_adx = float(params.get("min_adx", 20.0))
        min_efficiency_ratio = float(params.get("min_efficiency_ratio", 0.25))

        if snapshot.position_is_flat is None:
            return _no(
                self.strategy_id,
                "H03_POSITION_UNKNOWN",
                "Chưa xác định được vị thế đang flat hay không.",
            )
        if snapshot.position_is_flat is not True:
            return _no(
                self.strategy_id,
                "H03_POSITION_NOT_FLAT",
                "H03 không mở thêm vị thế khi đã có vị thế.",
            )

        inputs = _inputs(
            snapshot,
            timeframe,
            ("atr_14", "adx_14", "efficiency_ratio_40"),
            self.strategy_id,
        )
        if isinstance(inputs, NoTrade):
            return inputs
        series, features = inputs
        if len(series.bars) < donchian_lookback + 1:
            return _no(
                self.strategy_id,
                "H03_INSUFFICIENT_BARS",
                f"Cần {donchian_lookback} nến {timeframe} quá khứ và nến hiện tại đã đóng.",
            )
        if series.quote_volumes is None:
            return _no(
                self.strategy_id,
                "H03_VOLUME_UNAVAILABLE",
                f"Thiếu quote volume để so median {volume_median_lookback} nến trước.",
            )

        atr = _value(features, "atr_14")
        if atr <= 0:
            return _no(
                self.strategy_id,
                "H03_FEATURE_UNAVAILABLE",
                "ATR14 phải dương.",
                missing=(f"{timeframe}:atr_14",),
            )

        prior = series.bars[-(donchian_lookback + 1) : -1]
        current = series.bars[-1]
        prior_high = max(bar.high for bar in prior)
        prior_low = min(bar.low for bar in prior)
        atr_buffer = Decimal(str(breakout_atr_buffer)) * Decimal(str(atr))
        if current.close > prior_high + atr_buffer:
            direction = "long"
        elif current.close < prior_low - atr_buffer:
            direction = "short"
        else:
            return _no(
                self.strategy_id,
                "H03_NO_BREAKOUT",
                f"Giá đóng {timeframe} chưa phá biên kênh {donchian_lookback} nến kèm đệm "
                f"{breakout_atr_buffer} ATR.",
            )

        prior_volume = series.quote_volumes[-(volume_median_lookback + 1) : -1]
        if len(prior_volume) != volume_median_lookback:
            return _no(
                self.strategy_id,
                "H03_VOLUME_NOT_ABOVE_MEDIAN",
                f"Thiếu đủ {volume_median_lookback} nến quote volume trước đó.",
            )
        median_volume = median(prior_volume)
        required_volume = volume_median_multiplier * median_volume
        if median_volume <= 0 or series.quote_volumes[-1] < required_volume:
            return _no(
                self.strategy_id,
                "H03_VOLUME_NOT_ABOVE_MEDIAN",
                f"Quote volume hiện tại phải đạt tối thiểu {volume_median_multiplier}× median "
                f"{volume_median_lookback} nến trước.",
                gate_details=(
                    f"quote_volume={series.quote_volumes[-1]:.4f}; "
                    f"required >= {volume_median_multiplier * median_volume:.4f}",
                ),
            )

        adx = _value(features, "adx_14")
        er = _value(features, "efficiency_ratio_40")
        if adx < min_adx or er < min_efficiency_ratio:
            return _no(
                self.strategy_id,
                "H03_REGIME_CHOP",
                f"Thị trường chưa đủ xu hướng cho breakout (yêu cầu ADX14 >= {min_adx} và "
                f"ER40 >= {min_efficiency_ratio}).",
                gate_details=(
                    f"adx_14={adx:.4f} (min {min_adx}); "
                    f"efficiency_ratio_40={er:.4f} (min {min_efficiency_ratio})",
                ),
            )

        return _candidate(
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            snapshot=snapshot,
            bar=current,
            direction=direction,
            atr=atr,
            recipe=self.recipe,
            evidence=(
                features["atr_14"],
                features["adx_14"],
                features["efficiency_ratio_40"],
            ),
            evidence_bars=tuple(prior) + (current,),
            settings=settings,
            release_hash=release_content_hash(self),
        )


class H03DonchianBreakoutRuleV415m(_H03DonchianBreakoutV4ByTimeframe):
    """H03 v4 (FX-8) trên nến 15 phút."""

    release_id = "h03-v4-15m"
    entry_timeframe = "15m"
    __slots__ = ()
    recipe = ExitRecipe(
        "h03-exit-v4-15m",
        2.0,
        trailing_atr_multiple=3.0,
        max_hold_bars=120,
        exit_channel_period=20,
    )


class H03DonchianBreakoutRuleV41h(_H03DonchianBreakoutV4ByTimeframe):
    """H03 v4 (FX-8) trên nến 1 giờ."""

    release_id = "h03-v4-1h"
    entry_timeframe = "1h"
    __slots__ = ()
    recipe = ExitRecipe(
        "h03-exit-v4-1h",
        2.0,
        trailing_atr_multiple=3.0,
        max_hold_bars=120,
        exit_channel_period=20,
    )


class H03DonchianBreakoutRuleV44h(_H03DonchianBreakoutV4ByTimeframe):
    """H03 v4 (FX-8) trên nến 4 giờ."""

    release_id = "h03-v4-4h"
    entry_timeframe = "4h"
    __slots__ = ()
    recipe = ExitRecipe(
        "h03-exit-v4-4h",
        2.0,
        trailing_atr_multiple=3.0,
        max_hold_bars=120,
        exit_channel_period=20,
    )


def _is_reversal_candle(bar: OHLCVBar, direction: str) -> bool:
    """Xác nhận nến đảo chiều bằng thân nến thuận chiều hoặc râu cản ≥ 40% biên độ nến."""
    bar_range = bar.high - bar.low
    if direction == "long":
        if bar.close > bar.open:
            return True
        if bar_range > Decimal("0"):
            lower_wick = min(bar.open, bar.close) - bar.low
            return lower_wick >= Decimal("0.4") * bar_range
        return False
    if bar.close < bar.open:
        return True
    if bar_range > Decimal("0"):
        upper_wick = bar.high - max(bar.open, bar.close)
        return upper_wick >= Decimal("0.4") * bar_range
    return False


class H06MeanReversionRuleV31h:
    """H06 v3 (FX-8): hồi quy về trung bình khung 1h kèm nến đảo chiều và TP = mean_20."""

    strategy_id = "H06"
    release_id = "h06-v3-1h"
    recipe_id = "h06-exit-v3-1h"
    __slots__ = ()
    recipe_manifest = (
        ("stop_atr_multiple", 1.5),
        ("take_profit_mode", "mean_20"),
        ("min_tp_atr_multiple", 1.2),
        ("max_hold_bars", 16),
        ("exit_feature_name", "zscore_20"),
        ("long_exit", (">=", 0.0)),
        ("short_exit", ("<=", 0.0)),
        ("allow_scale_in", False),
    )

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        denied_params = _check_allowed_parameters(
            self.strategy_id, self.release_id, settings, _H06_V3_ALLOWED_PARAMS
        )
        if denied_params is not None:
            return denied_params
        denied = _check_selection(self.release_id, self.strategy_id, settings)
        if denied is not None:
            return denied

        params = dict(settings.parameters)
        min_tp_atr_multiple = float(params.get("min_tp_atr_multiple", 1.2))

        if (
            snapshot.macro_blackout is None
            or not snapshot.macro_evidence_ids
            or snapshot.macro_evidence_available_at is None
            or snapshot.macro_evidence_available_at > snapshot.decision_time
            or snapshot.macro_is_fresh is None
        ):
            return _no(
                self.strategy_id,
                "H06_MACRO_UNKNOWN",
                "Chưa có trạng thái và bằng chứng lịch macro tại decision_time.",
                evidence_refs=snapshot.macro_evidence_ids,
            )
        if snapshot.macro_is_fresh is not True:
            return _no(
                self.strategy_id,
                "H06_MACRO_STALE",
                "Lịch macro đã stale tại decision_time.",
                evidence_refs=snapshot.macro_evidence_ids,
            )
        if snapshot.macro_blackout:
            return _no(
                self.strategy_id,
                "H06_MACRO_BLACKOUT",
                "Đang trong khoảng cấm giao dịch quanh sự kiện vĩ mô.",
                evidence_refs=snapshot.macro_evidence_ids,
            )
        if snapshot.position_is_flat is None:
            return _no(
                self.strategy_id,
                "H06_POSITION_UNKNOWN",
                "Chưa xác định được vị thế đang flat hay không.",
            )
        if snapshot.position_is_flat is not True:
            return _no(
                self.strategy_id, "H06_POSITION_NOT_FLAT", "H06 cấm scale-in khi vị thế đã mở."
            )

        names = ("adx_14", "efficiency_ratio_40", "zscore_20", "rsi_14", "atr_14")
        inputs = _inputs(snapshot, "1h", names, self.strategy_id)
        if isinstance(inputs, NoTrade):
            return inputs
        series, features = inputs
        if len(series.bars) < 121:
            return _no(
                self.strategy_id,
                "H06_INSUFFICIENT_BARS",
                "Cần 120 nến 1h quá khứ và nến hiện tại đã đóng.",
            )

        adx = _value(features, "adx_14")
        er = _value(features, "efficiency_ratio_40")
        if adx >= 18.0 or er >= 0.25:
            return _no(
                self.strategy_id,
                "H06_NOT_RANGE",
                "Vùng đi ngang 1h yêu cầu ADX14 < 18 và ER40 < 0.25.",
            )

        z_score = _value(features, "zscore_20")
        rsi_value = _value(features, "rsi_14")
        if z_score < -2.0 and rsi_value < 30.0:
            direction = "long"
        elif z_score > 2.0 and rsi_value > 70.0:
            direction = "short"
        else:
            return _no(
                self.strategy_id,
                "H06_NO_EXTREME",
                "Z20 và RSI14 1h chưa đồng thời vượt ngưỡng vào lệnh.",
            )

        prior = series.bars[-121:-1]
        current = series.bars[-1]
        if (direction == "long" and current.close < min(bar.low for bar in prior)) or (
            direction == "short" and current.close > max(bar.high for bar in prior)
        ):
            return _no(
                self.strategy_id,
                "H06_ADVERSE_RANGE_BREAK",
                "Giá đã phá biên 120 nến 1h theo hướng bất lợi.",
            )

        if not _is_reversal_candle(current, direction):
            return _no(
                self.strategy_id,
                "H06_NO_REVERSAL_CANDLE",
                "Nến 1h hiện tại chưa có xác nhận đảo chiều (thân thuận chiều hoặc râu cản ≥ 40%).",
            )

        atr = _value(features, "atr_14")
        if atr <= 0:
            return _no(
                self.strategy_id,
                "H06_FEATURE_UNAVAILABLE",
                "ATR14 phải dương.",
                missing=("1h:atr_14",),
            )

        mean_20 = sum(bar.close for bar in series.bars[-20:]) / Decimal("20")
        distance_to_mean = (
            float(mean_20 - current.close)
            if direction == "long"
            else float(current.close - mean_20)
        )
        tp_atr_multiple = distance_to_mean / atr
        if not math.isfinite(tp_atr_multiple) or tp_atr_multiple < min_tp_atr_multiple:
            return _no(
                self.strategy_id,
                "H06_TP_BELOW_MIN_ATR",
                f"Khoảng cách tới trung bình 20 nến nhỏ hơn biên tối thiểu "
                f"{min_tp_atr_multiple} × ATR14.",
                gate_details=(
                    f"tp_atr_multiple={tp_atr_multiple:.4f}; required >= {min_tp_atr_multiple}",
                ),
            )

        recipe = ExitRecipe(
            self.recipe_id,
            1.5,
            take_profit_atr_multiple=round(tp_atr_multiple, 6),
            max_hold_bars=16,
            exit_feature_name="zscore_20",
            exit_feature_operator=">=" if direction == "long" else "<=",
            exit_feature_threshold=0.0,
            allow_scale_in=False,
        )
        return _candidate(
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            snapshot=snapshot,
            bar=current,
            direction=direction,
            atr=atr,
            recipe=recipe,
            evidence=tuple(features[name] for name in names),
            evidence_bars=tuple(prior) + (current,),
            settings=settings,
            release_hash=release_content_hash(self),
        )


FX8_RULES: tuple[tuple[Strategy, str, str, str], ...] = (
    (
        H01EmaTrendRuleV2(),
        "EMA theo xu hướng (v2, FX-8)",
        (
            "Giao cắt EMA20/80 trong cửa sổ 3 nến 1h gần nhất; |EMA20 - EMA80| >= 0.15 ATR14; "
            "giá đóng 4h cùng phía EMA200; ADX14 >= 20; cooldown 6 nến 1h sau stop; stop 2 ATR; "
            "trailing 3 ATR; thoát giao cắt ngược; tối đa 120 nến 1h."
        ),
        "h01-exit-v2",
    ),
    (
        H03DonchianBreakoutRuleV415m(),
        "Phá kênh Donchian (v4, 15m, FX-8)",
        (
            "Giá đóng 15m phá kênh Donchian 55 nến kèm đệm 0.25 ATR14; quote volume >= 1.5× "
            "median 20 nến; lọc regime ADX14 >= 20 và ER40 >= 0.25; stop 2 ATR; trailing 3 ATR; "
            "kênh thoát 20 nến 15m; tối đa 120 nến 15m."
        ),
        "h03-exit-v4-15m",
    ),
    (
        H03DonchianBreakoutRuleV41h(),
        "Phá kênh Donchian (v4, 1h, FX-8)",
        (
            "Giá đóng 1h phá kênh Donchian 55 nến kèm đệm 0.25 ATR14; quote volume >= 1.5× "
            "median 20 nến; lọc regime ADX14 >= 20 và ER40 >= 0.25; stop 2 ATR; trailing 3 ATR; "
            "kênh thoát 20 nến 1h; tối đa 120 nến 1h."
        ),
        "h03-exit-v4-1h",
    ),
    (
        H03DonchianBreakoutRuleV44h(),
        "Phá kênh Donchian (v4, 4h, FX-8)",
        (
            "Giá đóng 4h phá kênh Donchian 55 nến kèm đệm 0.25 ATR14; quote volume >= 1.5× "
            "median 20 nến; lọc regime ADX14 >= 20 và ER40 >= 0.25; stop 2 ATR; trailing 3 ATR; "
            "kênh thoát 20 nến 4h; tối đa 120 nến 4h."
        ),
        "h03-exit-v4-4h",
    ),
    (
        H06MeanReversionRuleV31h(),
        "Hồi quy về trung bình (v3, 1h, FX-8)",
        (
            "Khung 1h: ADX14 < 18 và ER40 < 0.25; Z20/RSI14 đồng thuận cực trị; lọc lịch vĩ mô "
            "AI-4 và phá biên 120 nến; xác nhận nến đảo chiều (thân thuận chiều hoặc râu ≥ 40%); "
            "stop 1.5 ATR; TP về trung bình 20 nến (>= 1.2 ATR); thoát tại Z20 = 0 hoặc 16 nến 1h."
        ),
        "h06-exit-v3-1h",
    ),
)
