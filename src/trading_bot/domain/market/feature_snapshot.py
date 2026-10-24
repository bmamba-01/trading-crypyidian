"""Hợp đồng thuần cho tính toán, chất lượng và replay của feature snapshot.

Module này không truy cập mạng, cơ sở dữ liệu hoặc đồng hồ hệ thống. Mọi mốc
thời gian, mã nguồn và ID dữ liệu được truyền vào để replay có thể tái lập.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from trading_bot.domain.market.features import (
    adx_dmi,
    atr,
    bollinger_bands,
    efficiency_ratio,
    ema,
    realized_volatility,
    rsi,
    sma,
    true_range,
    vwap,
    zscore,
)
from trading_bot.domain.market.models import MarketSnapshot, OHLCVBar
from trading_bot.domain.market.validation import inspect_bar_series

FEATURE_SCHEMA_VERSION = "crypidian-feature-snapshot-v1"
DEFAULT_EMA_PERIODS = (20, 80, 200)


class MarketRegime(StrEnum):
    """Nhãn trạng thái thị trường; không biểu thị hướng mua/bán."""

    TREND = "TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class DataFreshness(StrEnum):
    """Trạng thái tuổi dữ liệu so với mốc tính snapshot."""

    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class FeatureSettings:
    """Tham số và version được ghim cho một lần tính feature."""

    code_hash: str
    schema_version: str = FEATURE_SCHEMA_VERSION
    formula_version: str = "crypidian-formulas-v1"
    expected_interval: timedelta = timedelta(hours=1)
    max_event_age: timedelta = timedelta(hours=2)
    regime_history: timedelta = timedelta(days=90)
    sma_period: int = 20
    ema_periods: tuple[int, ...] = DEFAULT_EMA_PERIODS
    atr_period: int = 14
    rsi_period: int = 14
    adx_period: int = 14
    bollinger_period: int = 20
    realized_volatility_period: int = 20
    efficiency_ratio_period: int = 40
    zscore_period: int = 20
    trend_adx_threshold: float = 20.0
    range_adx_max: float = 18.0
    range_efficiency_max: float = 0.25
    high_volatility_quantile: float = 0.90

    def __post_init__(self) -> None:
        if (
            not self.code_hash.strip()
            or not self.schema_version.strip()
            or not self.formula_version.strip()
        ):
            raise ValueError("Hash mã nguồn và version schema/công thức không được để trống.")
        if self.expected_interval <= timedelta(0):
            raise ValueError("Khoảng nến kỳ vọng phải lớn hơn 0.")
        if self.max_event_age < timedelta(0):
            raise ValueError("Tuổi dữ liệu tối đa không được âm.")
        if self.regime_history <= timedelta(0):
            raise ValueError("Khoảng lịch sử regime phải lớn hơn 0.")
        periods = (
            self.sma_period,
            *self.ema_periods,
            self.atr_period,
            self.rsi_period,
            self.adx_period,
            self.bollinger_period,
            self.realized_volatility_period,
            self.efficiency_ratio_period,
            self.zscore_period,
        )
        if not periods or any(isinstance(value, bool) or value <= 0 for value in periods):
            raise ValueError("Mọi chu kỳ feature phải là số nguyên dương.")
        if len(set(self.ema_periods)) != len(self.ema_periods):
            raise ValueError("Chu kỳ EMA không được trùng nhau.")
        numeric_settings = (
            self.trend_adx_threshold,
            self.range_adx_max,
            self.range_efficiency_max,
            self.high_volatility_quantile,
        )
        if not all(math.isfinite(value) for value in numeric_settings):
            raise ValueError("Ngưỡng regime phải là số hữu hạn.")
        if not 0.0 <= self.range_adx_max <= self.trend_adx_threshold <= 100.0:
            raise ValueError("Ngưỡng ADX range/trend phải nằm trong [0, 100] và không chồng lấn.")
        if not 0.0 <= self.range_efficiency_max <= 1.0:
            raise ValueError("Ngưỡng ER range phải nằm trong [0, 1].")
        if not 0.0 < self.high_volatility_quantile < 1.0:
            raise ValueError("Phân vị biến động cao phải nằm giữa 0 và 1.")


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    """Kết quả phân loại regime có độ tin cậy, độ phủ và lý do."""

    label: MarketRegime
    confidence: float
    coverage: float
    reason_code: str
    reason_vi: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Độ tin cậy regime phải nằm trong [0, 1].")
        if not math.isfinite(self.coverage) or not 0.0 <= self.coverage <= 1.0:
            raise ValueError("Độ phủ regime phải nằm trong [0, 1].")
        if not self.reason_code.strip() or not self.reason_vi.strip():
            raise ValueError("Regime cần có mã lý do và giải thích tiếng Việt.")


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """Feature bất biến được gắn với đúng dữ liệu và version đã sinh ra nó."""

    snapshot_id: str
    content_hash: str
    workspace_id: UUID
    symbol: str
    timeframe: str
    as_of: datetime
    computed_at: datetime
    schema_hash: str
    formula_hash: str
    code_hash: str
    input_bar_ids: tuple[str, ...]
    input_event_ids: tuple[str, ...]
    features: Mapping[str, float | None]
    warmup: Mapping[str, bool]
    quality_flags: tuple[str, ...]
    data_freshness: DataFreshness
    data_coverage: float
    regime: RegimeAssessment
    is_usable: bool

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        _require_aware(self.computed_at, "computed_at")
        if self.computed_at < self.as_of:
            raise ValueError("Thời điểm tính snapshot không được trước as_of.")
        if not self.symbol.strip() or not self.timeframe.strip():
            raise ValueError("Snapshot cần có symbol và timeframe.")
        if len(self.input_bar_ids) != len(self.input_event_ids):
            raise ValueError("Danh sách bar ID và event ID phải có cùng số lượng.")
        if not math.isfinite(self.data_coverage) or not 0.0 <= self.data_coverage <= 1.0:
            raise ValueError("Độ phủ dữ liệu phải nằm trong [0, 1].")
        if any(value is not None and not math.isfinite(value) for value in self.features.values()):
            raise ValueError("Feature snapshot không được chứa giá trị NaN hoặc vô hạn.")
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        object.__setattr__(self, "warmup", MappingProxyType(dict(self.warmup)))
        object.__setattr__(self, "quality_flags", tuple(sorted(set(self.quality_flags))))

    @property
    def warmup_complete(self) -> bool:
        """Cho biết mọi feature được yêu cầu đã vượt qua giai đoạn khởi tạo."""
        return bool(self.warmup) and all(self.warmup.values())

    def canonical_payload(self) -> dict[str, Any]:
        """Tạo payload canonical; loại computed_at để replay không phụ thuộc đồng hồ."""
        return {
            "as_of": _utc_iso(self.as_of),
            "code_hash": self.code_hash,
            "data_coverage": self.data_coverage,
            "data_freshness": self.data_freshness.value,
            "features": dict(self.features),
            "formula_hash": self.formula_hash,
            "input_bar_ids": list(self.input_bar_ids),
            "input_event_ids": list(self.input_event_ids),
            "is_usable": self.is_usable,
            "quality_flags": list(self.quality_flags),
            "regime": {
                "confidence": self.regime.confidence,
                "coverage": self.regime.coverage,
                "label": self.regime.label.value,
                "reason_code": self.regime.reason_code,
                "reason_vi": self.regime.reason_vi,
            },
            "schema_hash": self.schema_hash,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "warmup": dict(self.warmup),
            "workspace_id": str(self.workspace_id),
        }

    def canonical_json(self) -> str:
        """Serialize nội dung theo khóa/thứ tự ổn định để hash và replay."""
        return _canonical_json(self.canonical_payload())


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bar_id(bar: OHLCVBar) -> str:
    return _hash(
        {
            "open_time": _utc_iso(bar.open_time),
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
        }
    )


def _feature_names(settings: FeatureSettings) -> tuple[str, ...]:
    names = [f"sma_{settings.sma_period}"]
    names.extend(f"ema_{period}" for period in settings.ema_periods)
    names.extend(
        (
            "tr",
            f"atr_{settings.atr_period}",
            f"rsi_{settings.rsi_period}",
            f"plus_di_{settings.adx_period}",
            f"minus_di_{settings.adx_period}",
            f"dx_{settings.adx_period}",
            f"adx_{settings.adx_period}",
            f"bollinger_middle_{settings.bollinger_period}",
            f"bollinger_upper_{settings.bollinger_period}",
            f"bollinger_lower_{settings.bollinger_period}",
            f"realized_volatility_{settings.realized_volatility_period}",
            f"efficiency_ratio_{settings.efficiency_ratio_period}",
            f"zscore_{settings.zscore_period}",
            "session_vwap",
        )
    )
    return tuple(names)


def _warmup_by_feature(settings: FeatureSettings, bar_count: int) -> dict[str, bool]:
    requirements = {
        f"sma_{settings.sma_period}": settings.sma_period,
        **{f"ema_{period}": period for period in settings.ema_periods},
        "tr": 1,
        f"atr_{settings.atr_period}": settings.atr_period,
        f"rsi_{settings.rsi_period}": settings.rsi_period + 1,
        f"plus_di_{settings.adx_period}": settings.adx_period + 1,
        f"minus_di_{settings.adx_period}": settings.adx_period + 1,
        f"dx_{settings.adx_period}": settings.adx_period + 1,
        f"adx_{settings.adx_period}": 2 * settings.adx_period,
        f"bollinger_middle_{settings.bollinger_period}": settings.bollinger_period,
        f"bollinger_upper_{settings.bollinger_period}": settings.bollinger_period,
        f"bollinger_lower_{settings.bollinger_period}": settings.bollinger_period,
        f"realized_volatility_{settings.realized_volatility_period}": (
            settings.realized_volatility_period + 1
        ),
        f"efficiency_ratio_{settings.efficiency_ratio_period}": (
            settings.efficiency_ratio_period + 1
        ),
        f"zscore_{settings.zscore_period}": settings.zscore_period + 1,
        "session_vwap": 1,
    }
    return {name: bar_count >= count for name, count in requirements.items()}


def feature_schema_hash(
    feature_names: Sequence[str] | None = None,
    *,
    schema_version: str = FEATURE_SCHEMA_VERSION,
) -> str:
    """Tạo hash schema từ version và danh sách feature đã đăng ký."""
    names = (
        tuple(feature_names)
        if feature_names is not None
        else (
            "sma_20",
            "ema_20",
            "ema_80",
            "ema_200",
            "tr",
            "atr_14",
            "rsi_14",
            "plus_di_14",
            "minus_di_14",
            "dx_14",
            "adx_14",
            "bollinger_middle_20",
            "bollinger_upper_20",
            "bollinger_lower_20",
            "realized_volatility_20",
            "efficiency_ratio_40",
            "zscore_20",
            "session_vwap",
        )
    )
    if len(names) != len(set(names)) or any(not name.strip() for name in names):
        raise ValueError("Tên feature schema phải không rỗng và không trùng.")
    return _hash(
        {
            "fields": dict.fromkeys(sorted(names), "number|null"),
            "schema_version": schema_version,
        }
    )


def _formula_hash(settings: FeatureSettings) -> str:
    return _hash(
        {
            "bollinger_ddof": 0,
            "ema_seed": "first_sma",
            "ema_periods": list(settings.ema_periods),
            "efficiency_ratio_period": settings.efficiency_ratio_period,
            "formula_version": settings.formula_version,
            "expected_interval_microseconds": int(
                settings.expected_interval.total_seconds() * 1_000_000
            ),
            "max_event_age_microseconds": int(settings.max_event_age.total_seconds() * 1_000_000),
            "periods": {
                "adx": settings.adx_period,
                "atr": settings.atr_period,
                "bollinger": settings.bollinger_period,
                "realized_volatility": settings.realized_volatility_period,
                "rsi": settings.rsi_period,
                "sma": settings.sma_period,
                "zscore": settings.zscore_period,
            },
            "regime_thresholds": {
                "high_volatility_quantile": settings.high_volatility_quantile,
                "range_adx_max": settings.range_adx_max,
                "range_efficiency_max": settings.range_efficiency_max,
                "trend_adx_threshold": settings.trend_adx_threshold,
            },
            "rma_seed": "first_period_arithmetic_mean",
            "regime_history_microseconds": int(settings.regime_history.total_seconds() * 1_000_000),
            "vwap_price": "ohlc_typical_price",
            "vwap_reset": "utc_calendar_day",
            "zscore_window": "prior_samples_excluding_current",
        }
    )


def _session_vwap(bars: tuple[OHLCVBar, ...]) -> tuple[float | None, ...]:
    output: list[float | None] = [None] * len(bars)
    start = 0
    while start < len(bars):
        session_day = bars[start].open_time.astimezone(UTC).date()
        end = start + 1
        while end < len(bars) and bars[end].open_time.astimezone(UTC).date() == session_day:
            end += 1
        prices = tuple(
            (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0 for bar in bars[start:end]
        )
        volumes = tuple(float(bar.volume) for bar in bars[start:end])
        output[start:end] = vwap(prices, volumes)
        start = end
    return tuple(output)


def _quantile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return ordered[lower_index] * (1.0 - weight) + ordered[upper_index] * weight


def _regime_assessment(
    features: Mapping[str, float | None],
    rv_history: Sequence[float | None],
    settings: FeatureSettings,
    *,
    data_usable: bool,
) -> RegimeAssessment:
    required = (
        features.get(f"adx_{settings.adx_period}"),
        features.get(f"efficiency_ratio_{settings.efficiency_ratio_period}"),
        features.get(f"realized_volatility_{settings.realized_volatility_period}"),
    )
    available = sum(value is not None for value in required)
    history_count = math.ceil(settings.regime_history / settings.expected_interval)
    past_values = [value for value in rv_history[-history_count - 1 : -1] if value is not None]
    coverage = min(1.0, available / len(required))
    if not data_usable:
        return RegimeAssessment(
            MarketRegime.UNKNOWN,
            0.0,
            coverage,
            "DATA_QUALITY_BLOCKED",
            "Chất lượng dữ liệu chưa đạt.",
        )
    if available != len(required) or len(past_values) < history_count:
        history_coverage = min(1.0, len(past_values) / history_count)
        return RegimeAssessment(
            MarketRegime.UNKNOWN,
            0.0,
            min(coverage, history_coverage),
            "REGIME_WARMUP_INCOMPLETE",
            "Chưa đủ feature hoặc lịch sử để phân loại trạng thái thị trường.",
        )

    adx_value, er_value, current_rv = required
    assert adx_value is not None
    assert er_value is not None
    assert current_rv is not None
    threshold = _quantile(past_values, settings.high_volatility_quantile)
    if current_rv > threshold:
        confidence = (
            1.0 if threshold == 0.0 else min(1.0, 0.5 + 0.5 * (current_rv - threshold) / current_rv)
        )
        return RegimeAssessment(
            MarketRegime.HIGH_VOLATILITY,
            confidence,
            1.0,
            "RV_ABOVE_PRIOR_QUANTILE",
            "Biến động hiện tại vượt phân vị lịch sử; không xác định hướng giao dịch.",
        )
    if adx_value >= settings.trend_adx_threshold:
        confidence = min(
            1.0,
            (adx_value - settings.trend_adx_threshold) / (100.0 - settings.trend_adx_threshold),
        )
        return RegimeAssessment(
            MarketRegime.TREND,
            confidence,
            1.0,
            "ADX_TREND_THRESHOLD",
            "ADX vượt ngưỡng xu hướng; nhãn này không cho biết hướng mua/bán.",
        )
    if adx_value < settings.range_adx_max and er_value < settings.range_efficiency_max:
        confidence = min(
            (settings.range_adx_max - adx_value) / settings.range_adx_max,
            (settings.range_efficiency_max - er_value) / settings.range_efficiency_max,
        )
        return RegimeAssessment(
            MarketRegime.RANGE,
            max(0.0, confidence),
            1.0,
            "ADX_ER_RANGE_THRESHOLDS",
            "ADX và ER thấp theo ngưỡng range; đây không phải tín hiệu đảo chiều.",
        )
    return RegimeAssessment(
        MarketRegime.UNKNOWN,
        0.0,
        1.0,
        "NO_REGIME_RULE_MATCHED",
        "Các feature hiện tại không thỏa quy tắc phân loại regime đã ghim.",
    )


class FeatureCalculator:
    """Tính feature snapshot từ đúng một chuỗi symbol/timeframe."""

    def __init__(self, settings: FeatureSettings) -> None:
        self.settings = settings

    def calculate(
        self,
        snapshot: MarketSnapshot,
        *,
        workspace_id: UUID,
        input_event_ids: Sequence[str],
        symbol: str | None = None,
        timeframe: str | None = None,
        computed_at: datetime | None = None,
    ) -> FeatureSnapshot:
        """Tạo snapshot bất biến; mọi ID/version và đồng hồ đều do caller cấp."""
        calculation_time = computed_at or snapshot.as_of
        _require_aware(calculation_time, "computed_at")
        if calculation_time < snapshot.as_of:
            raise ValueError("Thời điểm tính snapshot không được trước as_of.")
        if len(input_event_ids) != len(snapshot.bars):
            raise ValueError("Số lượng event ID phải khớp chính xác với số lượng nến.")
        if any(not value.strip() for value in input_event_ids):
            raise ValueError("Event ID không được để trống.")
        if len(set(input_event_ids)) != len(input_event_ids):
            raise ValueError("Mỗi event ID đầu vào phải là duy nhất.")
        paired_bars = sorted(
            zip(snapshot.bars, input_event_ids, strict=True),
            key=lambda pair: (pair[0].open_time, pair[0].event_at, pair[1]),
        )
        bars = tuple(pair[0] for pair in paired_bars)
        event_ids = tuple(pair[1] for pair in paired_bars)
        identities = {(bar.symbol, bar.timeframe) for bar in bars}
        if len(identities) > 1:
            raise ValueError("Một feature snapshot chỉ được chứa một symbol và timeframe.")
        if bars:
            bar_symbol, bar_timeframe = next(iter(identities))
            if symbol is not None and symbol != bar_symbol:
                raise ValueError("Symbol truyền vào không khớp dữ liệu nến.")
            if timeframe is not None and timeframe != bar_timeframe:
                raise ValueError("Timeframe truyền vào không khớp dữ liệu nến.")
            symbol, timeframe = bar_symbol, bar_timeframe
            quality = inspect_bar_series(
                bars,
                as_of=snapshot.as_of,
                expected_interval=self.settings.expected_interval,
                max_event_age=self.settings.max_event_age,
                expected_range_start=bars[0].open_time,
                expected_range_end=bars[-1].open_time + self.settings.expected_interval,
                backfill_complete=True,
            )
        else:
            if not symbol or not timeframe:
                raise ValueError("Snapshot rỗng cần có symbol và timeframe tường minh.")
            quality = inspect_bar_series(
                (),
                as_of=snapshot.as_of,
                expected_interval=self.settings.expected_interval,
                max_event_age=self.settings.max_event_age,
            )

        closes = tuple(float(bar.close) for bar in bars)
        highs = tuple(float(bar.high) for bar in bars)
        lows = tuple(float(bar.low) for bar in bars)
        feature_names = _feature_names(self.settings)
        feature_values: dict[str, float | None] = {}
        if bars:
            feature_values[f"sma_{self.settings.sma_period}"] = sma(
                closes, self.settings.sma_period
            )[-1]
            for period in self.settings.ema_periods:
                feature_values[f"ema_{period}"] = ema(closes, period)[-1]
            ranges = true_range(highs, lows, closes)
            feature_values["tr"] = ranges[-1]
            feature_values[f"atr_{self.settings.atr_period}"] = atr(
                highs, lows, closes, self.settings.atr_period
            )[-1]
            feature_values[f"rsi_{self.settings.rsi_period}"] = rsi(
                closes, self.settings.rsi_period
            )[-1]
            positive_di, negative_di, dx_values, adx_values = adx_dmi(
                highs, lows, closes, self.settings.adx_period
            )
            feature_values[f"plus_di_{self.settings.adx_period}"] = positive_di[-1]
            feature_values[f"minus_di_{self.settings.adx_period}"] = negative_di[-1]
            feature_values[f"dx_{self.settings.adx_period}"] = dx_values[-1]
            feature_values[f"adx_{self.settings.adx_period}"] = adx_values[-1]
            bb_mid, bb_upper, bb_lower = bollinger_bands(closes, self.settings.bollinger_period)
            feature_values[f"bollinger_middle_{self.settings.bollinger_period}"] = bb_mid[-1]
            feature_values[f"bollinger_upper_{self.settings.bollinger_period}"] = bb_upper[-1]
            feature_values[f"bollinger_lower_{self.settings.bollinger_period}"] = bb_lower[-1]
            feature_values[f"realized_volatility_{self.settings.realized_volatility_period}"] = (
                realized_volatility(closes, self.settings.realized_volatility_period)[-1]
            )
            feature_values[f"efficiency_ratio_{self.settings.efficiency_ratio_period}"] = (
                efficiency_ratio(closes, self.settings.efficiency_ratio_period)[-1]
            )
            feature_values[f"zscore_{self.settings.zscore_period}"] = zscore(
                closes, self.settings.zscore_period
            )[-1]
            feature_values["session_vwap"] = _session_vwap(bars)[-1]
        else:
            feature_values = dict.fromkeys(feature_names)

        # Giữ schema ổn định, kể cả khi một chuỗi còn đang ở warm-up.
        feature_values = {name: feature_values.get(name) for name in feature_names}
        warmup = _warmup_by_feature(self.settings, len(bars))
        quality_flags: list[str] = []
        if not bars:
            quality_flags.append("EMPTY_INPUT")
        if quality.duplicate_keys:
            quality_flags.append("DUPLICATE_BARS")
        if quality.gap_after:
            quality_flags.append("BAR_GAP")
        if quality.future_available_count:
            quality_flags.append("FUTURE_INPUT")
        if quality.unclosed_count:
            quality_flags.append("UNCLOSED_BAR")
        if quality.missing_expected_open_times:
            quality_flags.append("MISSING_BARS")
        if quality.backfill_complete is False:
            quality_flags.append("BACKFILL_INCOMPLETE")
        if quality.stale:
            quality_flags.append("STALE_DATA")
        if not all(warmup.values()):
            quality_flags.append("WARMUP_INCOMPLETE")
        if any(warmup[name] and value is None for name, value in feature_values.items()):
            quality_flags.append("FEATURE_UNDEFINED")
        data_usable = quality.is_usable and all(warmup.values())
        freshness = (
            DataFreshness.UNKNOWN
            if not bars
            else DataFreshness.STALE
            if quality.stale
            else DataFreshness.FRESH
        )
        unique_bar_count = len({_bar_id(bar) for bar in bars})
        expected_count = unique_bar_count + len(quality.missing_expected_open_times) if bars else 0
        data_coverage = unique_bar_count / expected_count if expected_count else 0.0
        rv_values = (
            realized_volatility(closes, self.settings.realized_volatility_period) if bars else ()
        )
        regime = _regime_assessment(
            feature_values,
            rv_values,
            self.settings,
            data_usable=data_usable,
        )
        schema_hash = feature_schema_hash(
            feature_names, schema_version=self.settings.schema_version
        )
        formula_hash = _formula_hash(self.settings)
        input_bar_ids = tuple(_bar_id(bar) for bar in bars)
        snapshot_seed = {
            "as_of": _utc_iso(snapshot.as_of),
            "code_hash": self.settings.code_hash,
            "data_coverage": data_coverage,
            "data_freshness": freshness.value,
            "features": feature_values,
            "formula_hash": formula_hash,
            "input_bar_ids": list(input_bar_ids),
            "input_event_ids": list(event_ids),
            "is_usable": data_usable,
            "quality_flags": sorted(set(quality_flags)),
            "regime": {
                "confidence": regime.confidence,
                "coverage": regime.coverage,
                "label": regime.label.value,
                "reason_code": regime.reason_code,
                "reason_vi": regime.reason_vi,
            },
            "schema_hash": schema_hash,
            "symbol": symbol,
            "timeframe": timeframe,
            "warmup": warmup,
            "workspace_id": str(workspace_id),
        }
        content_hash = _hash(snapshot_seed)
        return FeatureSnapshot(
            snapshot_id=content_hash,
            content_hash=content_hash,
            workspace_id=workspace_id,
            symbol=symbol,
            timeframe=timeframe,
            as_of=snapshot.as_of,
            computed_at=calculation_time,
            schema_hash=schema_hash,
            formula_hash=formula_hash,
            code_hash=self.settings.code_hash,
            input_bar_ids=input_bar_ids,
            input_event_ids=event_ids,
            features=feature_values,
            warmup=warmup,
            quality_flags=tuple(quality_flags),
            data_freshness=freshness,
            data_coverage=data_coverage,
            regime=regime,
            is_usable=data_usable,
        )
