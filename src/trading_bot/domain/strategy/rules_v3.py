"""Release v3 của H03: phá kênh Donchian trên khung vào lệnh chọn được (15m, 1h, 4h).

Owner quyết 15/11/2026: cho bot H03 chạy nến 15m/1h/4h để so kết quả theo khung. Vì mọi đường
runtime/replay/admin lấy khung theo ``release_id`` (``entry_timeframe_for_release``), mỗi khung
là **một release riêng** — ``h03-v3-15m``, ``h03-v3-1h``, ``h03-v3-4h`` — thay vì tham số khung
trong bot config. Logic vào lệnh giống H03 v1 (kênh Donchian ``donchian_lookback`` nến trước,
quote volume > median ``volume_median_lookback`` nến trước, đều chỉnh được theo bot); phần thoát
giống H03 v2 (kênh thoát 20 nến ngược chiều trên **cùng khung**, ``max_hold_bars`` đếm theo nến
khung chiến lược).

Module tách riêng vì hash release băm toàn bộ nguồn module chứa rule: sửa ``rules.py`` hoặc
``rules_v2.py`` sẽ đổi hash các release đang được ghim trong DB.
"""

from __future__ import annotations

from statistics import median
from types import MappingProxyType

from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    NoTrade,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.rules import (
    H03DonchianBreakoutRule,
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

# SHA-256 của `rules.py` tại thời điểm phát hành v3 (v3 dùng helper từ file đó); unit test đối
# chiếu để hash release v3 buộc đổi khi `rules.py` đổi.
BASE_RULES_SOURCE_SHA256 = "9c2f3d708f48afd657c05234142dbbb71ed64f436771e81b9d0fc5ca9c47369d"

H03_V3_TIMEFRAMES = MappingProxyType({"h03-v3-15m": "15m", "h03-v3-1h": "1h", "h03-v3-4h": "4h"})

EXIT_EVIDENCE_BY_RECIPE_V3 = MappingProxyType(
    {
        f"h03-exit-v3-{timeframe}": ExitEvidenceSpec(timeframe=timeframe, channel_period=20)
        for timeframe in H03_V3_TIMEFRAMES.values()
    }
)


def exit_evidence_spec(recipe_id: str) -> ExitEvidenceSpec | None:
    """Bằng chứng thoát cho recipe v2/v3; None nghĩa là recipe không dùng (toàn bộ v1)."""
    return EXIT_EVIDENCE_BY_RECIPE_V3.get(recipe_id) or EXIT_EVIDENCE_BY_RECIPE.get(recipe_id)


class _H03DonchianBreakoutByTimeframe(H03DonchianBreakoutRule):
    """H03 v3 dùng chung: như v1 nhưng đọc chuỗi nến của ``entry_timeframe``."""

    entry_timeframe = "1h"
    __slots__ = ()

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        timeframe = self.entry_timeframe
        parameters = dict(settings.parameters)
        donchian_lookback = parameters.get("donchian_lookback", 55)
        volume_median_lookback = parameters.get("volume_median_lookback", 20)
        # Không dùng `rules._selected`: hàm đó đòi "h03-v1" trong selection nên release khác
        # (v2/v3) bị coi là chưa chọn. v3 kiểm đúng release_id của chính nó.
        if not settings.published:
            return _no(
                self.strategy_id,
                f"{self.strategy_id}_SETTINGS_UNPUBLISHED",
                "Cấu hình chiến lược chưa được công bố.",
            )
        if self.release_id not in settings.active_release_ids:
            return _no(
                self.strategy_id,
                f"{self.strategy_id}_NOT_SELECTED",
                "Bản phát hành chiến lược không nằm trong cấu hình đang chọn.",
            )
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
        inputs = _inputs(snapshot, timeframe, ("atr_14",), self.strategy_id)
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
        prior = series.bars[-(donchian_lookback + 1) : -1]
        current = series.bars[-1]
        prior_high = max(bar.high for bar in prior)
        prior_low = min(bar.low for bar in prior)
        if current.close > prior_high:
            direction = "long"
        elif current.close < prior_low:
            direction = "short"
        else:
            return _no(
                self.strategy_id,
                "H03_NO_BREAKOUT",
                f"Giá đóng {timeframe} không phá biên kênh {donchian_lookback} nến trước.",
            )
        prior_volume = series.quote_volumes[-(volume_median_lookback + 1) : -1]
        if len(prior_volume) != volume_median_lookback or series.quote_volumes[-1] <= median(
            prior_volume
        ):
            return _no(
                self.strategy_id,
                "H03_VOLUME_NOT_ABOVE_MEDIAN",
                f"Quote volume hiện tại phải lớn hơn median {volume_median_lookback} nến trước.",
            )
        atr = _value(features, "atr_14")
        if atr <= 0:
            return _no(
                self.strategy_id,
                "H03_FEATURE_UNAVAILABLE",
                "ATR14 phải dương.",
                missing=(f"{timeframe}:atr_14",),
            )
        return _candidate(
            strategy_id=self.strategy_id,
            release_id=self.release_id,
            snapshot=snapshot,
            bar=current,
            direction=direction,
            atr=atr,
            recipe=self.recipe,
            evidence=(features["atr_14"],),
            evidence_bars=tuple(prior) + (current,),
            settings=settings,
            release_hash=release_content_hash(self),
        )


class H03DonchianBreakoutRuleV3M15(_H03DonchianBreakoutByTimeframe):
    """H03 v3 trên nến 15 phút."""

    release_id = "h03-v3-15m"
    entry_timeframe = "15m"
    __slots__ = ()
    recipe = ExitRecipe(
        "h03-exit-v3-15m", 2.0, trailing_atr_multiple=3.0, max_hold_bars=120, exit_channel_period=20
    )


class H03DonchianBreakoutRuleV3H1(_H03DonchianBreakoutByTimeframe):
    """H03 v3 trên nến 1 giờ."""

    release_id = "h03-v3-1h"
    entry_timeframe = "1h"
    __slots__ = ()
    recipe = ExitRecipe(
        "h03-exit-v3-1h", 2.0, trailing_atr_multiple=3.0, max_hold_bars=120, exit_channel_period=20
    )


class H03DonchianBreakoutRuleV3H4(_H03DonchianBreakoutByTimeframe):
    """H03 v3 trên nến 4 giờ."""

    release_id = "h03-v3-4h"
    entry_timeframe = "4h"
    __slots__ = ()
    recipe = ExitRecipe(
        "h03-exit-v3-4h", 2.0, trailing_atr_multiple=3.0, max_hold_bars=120, exit_channel_period=20
    )
