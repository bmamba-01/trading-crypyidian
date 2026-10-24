"""Ba rule nghiên cứu H01, H03 và H06 với đầu vào point-in-time."""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, timedelta
from decimal import Decimal
from statistics import median

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

_TIMEFRAME_INTERVALS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


def _no(
    strategy_id: str,
    code: str,
    detail: str,
    *,
    missing: tuple[str, ...] = (),
    gate_details: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
) -> NoTrade:
    return NoTrade(
        strategy_id,
        code,
        detail,
        missing_features=missing,
        gate_details=gate_details or (f"{code}: {detail}",),
        evidence_refs=evidence_refs,
    )


def _value(features: dict[str, FeatureObservation], name: str) -> float:
    """Lấy giá trị số đã được _inputs xác nhận là sẵn sàng."""
    value = features[name].value
    if value is None:
        raise ValueError(f"Đặc trưng {name} không có giá trị.")
    return value


def release_content_hash(rule: object) -> str:
    """Hash mã rule, schema recipe và recipe version để nhận diện đúng release."""
    module = inspect.getmodule(type(rule))
    if module is None:
        raise ValueError("Không xác định được module nguồn của strategy rule.")
    recipe_schema = inspect.getsource(ExitRecipe)
    recipe = getattr(rule, "recipe", None)
    recipe_manifest = (
        {name: getattr(recipe, name) for name in recipe.__dataclass_fields__}
        if recipe is not None
        else getattr(rule, "recipe_manifest", None)
    )
    manifest = {
        "release_id": getattr(rule, "release_id", ""),
        "strategy_id": getattr(rule, "strategy_id", ""),
        "recipe_manifest": recipe_manifest,
        "rule_module_source": inspect.getsource(module),
        "recipe_schema_source": recipe_schema,
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _bar_key(bar: OHLCVBar) -> str:
    """Natural key UTC thay UUID sự kiện chưa có, gồm mã, khung và giờ mở nến."""
    open_time_utc = bar.open_time.astimezone(UTC).isoformat(timespec="microseconds")
    return f"{bar.symbol}|{bar.timeframe}|{open_time_utc}"


def _inputs(
    snapshot: DecisionSnapshot,
    timeframe: str,
    names: tuple[str, ...],
    strategy_id: str,
) -> tuple[MarketSeries, dict[str, FeatureObservation]] | NoTrade:
    series = snapshot.get_series(timeframe)
    if series is None or not series.bars:
        return _no(strategy_id, f"{strategy_id}_INSUFFICIENT_BARS", f"Thiếu chuỗi nến {timeframe}.")
    if any(not bar.is_closed for bar in series.bars):
        return _no(strategy_id, f"{strategy_id}_BAR_NOT_CLOSED", "Chuỗi có nến chưa đóng.")
    if any(
        max(bar.close_time, bar.event_at, bar.available_at) > snapshot.decision_time
        for bar in series.bars
    ):
        return _no(
            strategy_id,
            f"{strategy_id}_INPUT_UNAVAILABLE",
            "Mốc close/event/availability của nến vượt thời điểm quyết định.",
        )
    if series.quality is SeriesQuality.UNKNOWN:
        return _no(
            strategy_id,
            f"{strategy_id}_SERIES_QUALITY_UNKNOWN",
            "Chuỗi chưa được xác nhận chất lượng.",
        )
    if series.quality is SeriesQuality.STALE:
        return _no(strategy_id, f"{strategy_id}_SERIES_STALE", "Chuỗi được đánh dấu stale.")
    if series.quality is not SeriesQuality.READY:
        return _no(strategy_id, f"{strategy_id}_SERIES_INCOMPLETE", "Chuỗi không usable.")
    if series.expected_interval is None or series.max_event_age is None:
        return _no(
            strategy_id,
            f"{strategy_id}_SERIES_QUALITY_UNKNOWN",
            "Thiếu interval hoặc freshness limit đã cấu hình.",
        )
    if series.expected_interval != _TIMEFRAME_INTERVALS.get(timeframe):
        return _no(
            strategy_id,
            f"{strategy_id}_SERIES_INCOMPLETE",
            "Interval chuỗi không khớp timeframe của rule.",
        )
    if series.backfill_complete is not True:
        return _no(
            strategy_id,
            f"{strategy_id}_SERIES_INCOMPLETE",
            "Backfill của chuỗi chưa được xác nhận đầy đủ.",
        )
    if any(
        current.open_time - previous.open_time != series.expected_interval
        for previous, current in zip(series.bars, series.bars[1:], strict=False)
    ):
        return _no(
            strategy_id,
            f"{strategy_id}_SERIES_INCOMPLETE",
            "Chuỗi có khoảng trống hoặc nhịp nến không đúng interval.",
        )
    age = snapshot.decision_time - series.bars[-1].event_at
    if age < timedelta(0):
        return _no(
            strategy_id, f"{strategy_id}_INPUT_UNAVAILABLE", "Event mới nhất nằm sau decision_time."
        )
    if age > series.max_event_age:
        return _no(
            strategy_id,
            f"{strategy_id}_SERIES_STALE",
            "Event mới nhất quá tuổi tối đa cho timeframe.",
        )
    found: dict[str, FeatureObservation] = {}
    missing: list[str] = []
    for name in names:
        feature = snapshot.get_feature(name, timeframe)
        if feature is None or feature.quality is not FeatureQuality.READY or feature.value is None:
            missing.append(f"{timeframe}:{name}")
            continue
        if feature.available_at > snapshot.decision_time:
            return _no(
                strategy_id,
                f"{strategy_id}_INPUT_UNAVAILABLE",
                f"Đặc trưng {name} chỉ có sau thời điểm quyết định.",
            )
        if (
            series.max_event_age is not None
            and snapshot.decision_time - feature.available_at > series.max_event_age
        ):
            return _no(
                strategy_id,
                f"{strategy_id}_FEATURE_STALE",
                f"Đặc trưng {name} quá tuổi tối đa cho timeframe.",
                missing=(f"{timeframe}:{name}",),
            )
        found[name] = feature
    if missing:
        return _no(
            strategy_id,
            f"{strategy_id}_FEATURE_UNAVAILABLE",
            "Thiếu hoặc không dùng được đặc trưng bắt buộc.",
            missing=tuple(missing),
        )
    return series, found


def _selected(
    snapshot: DecisionSnapshot, settings: StrategySelection, strategy_id: str
) -> NoTrade | None:
    if not settings.published:
        return _no(
            strategy_id,
            f"{strategy_id}_SETTINGS_UNPUBLISHED",
            "Cấu hình chiến lược chưa được công bố.",
        )
    if strategy_id.lower() + "-v1" not in settings.active_release_ids:
        return _no(
            strategy_id,
            f"{strategy_id}_NOT_SELECTED",
            "Bản phát hành chiến lược không nằm trong cấu hình đang chọn.",
        )
    return None


def _candidate(
    *,
    strategy_id: str,
    release_id: str,
    snapshot: DecisionSnapshot,
    bar: OHLCVBar,
    direction: str,
    atr: float,
    recipe: ExitRecipe,
    evidence: tuple[FeatureObservation, ...],
    evidence_bars: tuple[OHLCVBar, ...],
    settings: StrategySelection,
    release_hash: str,
) -> StrategyCandidate:
    eligible_after = bar.close_time + timedelta(microseconds=1)
    duration = bar.close_time - bar.open_time + timedelta(microseconds=1)
    return StrategyCandidate(
        strategy_id=strategy_id,
        strategy_release_id=release_id,
        release_content_hash=release_hash,
        settings_version_id=settings.settings_version_id,
        snapshot_id=snapshot.snapshot_id,
        symbol=snapshot.symbol,
        direction=direction,
        reference_price=bar.close,
        atr_value=atr,
        signal_at=bar.close_time,
        eligible_after=eligible_after,
        expires_at=eligible_after + duration,
        recipe=recipe,
        evidence_feature_ids=tuple(item.feature_id for item in evidence),
        evidence_bar_keys=tuple(_bar_key(item) for item in evidence_bars),
        macro_evidence_ids=snapshot.macro_evidence_ids,
        geometry_version=None,
        reason_code=f"{strategy_id}_{direction.upper()}_ENTRY",
        detail_vi=(
            f"Rule {strategy_id} xác nhận tín hiệu {direction} từ nến đã đóng; "
            "đây chưa phải lệnh giao dịch."
        ),
    )


class H01EmaTrendRule:
    """H01: giao cắt EMA 1h, xu hướng 4h và ADX14 tối thiểu 20."""

    strategy_id = "H01"
    release_id = "h01-v1"
    __slots__ = ()
    recipe = ExitRecipe(
        "h01-exit-v1", 2.0, trailing_atr_multiple=3.0, opposite_signal_exit=True, max_hold_bars=120
    )

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        if settings.parameters:
            return _no(
                self.strategy_id,
                "H01_UNSUPPORTED_PARAMETERS",
                "H01-v1 chưa hỗ trợ override tham số runtime.",
            )
        denied = _selected(snapshot, settings, self.strategy_id)
        if denied:
            return denied
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
                "Cần hai nến 1h đã đóng để xác định giao cắt.",
            )
        fast, slow = features["ema_20"], features["ema_80"]
        if fast.previous_value is None or slow.previous_value is None:
            return _no(
                self.strategy_id,
                "H01_FEATURE_UNAVAILABLE",
                "Thiếu EMA của nến trước để xác nhận giao cắt.",
                missing=("1h:ema_20_previous", "1h:ema_80_previous"),
            )
        fast_value = _value(features, "ema_20")
        slow_value = _value(features, "ema_80")
        direction: str | None = None
        if fast.previous_value <= slow.previous_value and fast_value > slow_value:
            direction = "long"
        elif fast.previous_value >= slow.previous_value and fast_value < slow_value:
            direction = "short"
        if direction is None:
            return _no(
                self.strategy_id, "H01_NO_CROSS", "EMA20 không vừa cắt EMA80 trên nến 1h mới nhất."
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
        if _value(features, "adx_14") < 20:
            adx = _value(features, "adx_14")
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


class H03DonchianBreakoutRule:
    """H03: phá kênh Donchian 55 nến, volume vượt median 20 nến trước."""

    strategy_id = "H03"
    release_id = "h03-v1"
    __slots__ = ()
    recipe = ExitRecipe(
        "h03-exit-v1", 2.0, trailing_atr_multiple=3.0, max_hold_bars=120, exit_channel_period=20
    )

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        parameters = dict(settings.parameters)
        donchian_lookback = parameters.get("donchian_lookback", 55)
        volume_median_lookback = parameters.get("volume_median_lookback", 20)
        denied = _selected(snapshot, settings, self.strategy_id)
        if denied:
            return denied
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
        inputs = _inputs(snapshot, "1h", ("atr_14",), self.strategy_id)
        if isinstance(inputs, NoTrade):
            return inputs
        series, features = inputs
        if len(series.bars) < donchian_lookback + 1:
            return _no(
                self.strategy_id,
                "H03_INSUFFICIENT_BARS",
                f"Cần {donchian_lookback} nến quá khứ và nến hiện tại đã đóng.",
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
                f"Giá đóng không phá biên kênh {donchian_lookback} nến trước.",
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
                missing=("1h:atr_14",),
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


class H06RangeMeanReversionRule:
    """H06: hồi quy về trung bình trong vùng đi ngang 15 phút."""

    strategy_id = "H06"
    release_id = "h06-v1"
    recipe_id = "h06-exit-v1"
    __slots__ = ()
    recipe_manifest = (
        ("stop_atr_multiple", 1.5),
        ("take_profit_atr_multiple", 2.0),
        ("max_hold_bars", 16),
        ("exit_feature_name", "zscore_20"),
        ("long_exit", (">=", -0.2)),
        ("short_exit", ("<=", 0.2)),
        ("allow_scale_in", False),
    )

    def evaluate(
        self, snapshot: DecisionSnapshot, settings: StrategySelection
    ) -> StrategyCandidate | NoTrade:
        if settings.parameters:
            return _no(
                self.strategy_id,
                "H06_UNSUPPORTED_PARAMETERS",
                "H06-v1 chưa hỗ trợ override tham số runtime.",
            )
        denied = _selected(snapshot, settings, self.strategy_id)
        if denied:
            return denied
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
        inputs = _inputs(snapshot, "15m", names, self.strategy_id)
        if isinstance(inputs, NoTrade):
            return inputs
        series, features = inputs
        if len(series.bars) < 121:
            return _no(
                self.strategy_id,
                "H06_INSUFFICIENT_BARS",
                "Cần 120 nến quá khứ và nến hiện tại đã đóng.",
            )
        adx, er = _value(features, "adx_14"), _value(features, "efficiency_ratio_40")
        if adx >= 18 or er >= 0.25:
            return _no(
                self.strategy_id,
                "H06_NOT_RANGE",
                "Vùng đi ngang yêu cầu ADX14 < 18 và ER40 < 0.25.",
            )
        z_score, rsi_value = _value(features, "zscore_20"), _value(features, "rsi_14")
        if z_score < -2 and rsi_value < 30:
            direction = "long"
            threshold = -0.2
        elif z_score > 2 and rsi_value > 70:
            direction = "short"
            threshold = 0.2
        else:
            return _no(
                self.strategy_id,
                "H06_NO_EXTREME",
                "Z20 và RSI14 chưa đồng thời vượt ngưỡng vào lệnh.",
            )
        prior = series.bars[-121:-1]
        current = series.bars[-1]
        if (direction == "long" and current.close < min(bar.low for bar in prior)) or (
            direction == "short" and current.close > max(bar.high for bar in prior)
        ):
            return _no(
                self.strategy_id,
                "H06_ADVERSE_RANGE_BREAK",
                "Giá đã phá biên 120 nến theo hướng bất lợi.",
            )
        atr = _value(features, "atr_14")
        if atr <= 0:
            return _no(
                self.strategy_id,
                "H06_FEATURE_UNAVAILABLE",
                "ATR14 phải dương.",
                missing=("15m:atr_14",),
            )
        recipe = ExitRecipe(
            self.recipe_id,
            1.5,
            take_profit_atr_multiple=2.0,
            max_hold_bars=16,
            exit_feature_name="zscore_20",
            exit_feature_operator=">=" if direction == "long" else "<=",
            exit_feature_threshold=threshold,
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
