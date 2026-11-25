"""Đặc trưng bối cảnh phái sinh point-in-time cho Funding cực trị và Open Interest (IND-3).

Module thuần (không I/O) tính các chỉ số nhân quả từ chuỗi ``DerivativesSnapshot`` đã thu thập
qua DATA-1 với ràng buộc bất biến tiền tố (``collected_at <= as_of``, không nhìn trước tương lai):

- ``funding_rate`` hiện tại và độ lệch cực trị so với cửa sổ quá khứ ngoại trừ mẫu hiện tại
  (``funding_zscore`` và ``funding_percentile`` bậc giữa).
- Trạng thái quá tải đám đông ``FundingCrowdingState`` (``CROWDED_LONG``, ``CROWDED_SHORT``,
  ``NEUTRAL``, hoặc fail-closed ``UNKNOWN`` khi thiếu mẫu hoặc dữ liệu quá cũ ``stale``).
- Biến động Open Interest ``oi_change_pct``, chế độ ``OpenInterestRegime`` (``EXPANDING``,
  ``CONTRACTING``, ``FLAT``, ``UNKNOWN``) và cờ biến động bất thường ``oi_anomaly``.

BẢO ĐẢM KIẾN TRÚC: Các đặc trưng này CHỈ phục vụ bộ lọc/cảnh báo bối cảnh tại tầng L4,
tuyệt đối không dùng làm ngòi nổ vào lệnh tại tầng L5.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from trading_bot.domain.research.derivatives import DIGEST_MAX_AGE, DerivativesSnapshot
from trading_bot.domain.strategy.contracts import FeatureObservation, FeatureQuality

if TYPE_CHECKING:
    from trading_bot.domain.strategy.contracts import DecisionSnapshot

DERIVATIVES_CONTEXT_FORMULA_VERSION = "ind3-funding-oi-context-v1"

# Tên feature chuẩn khi gắn vào DecisionSnapshot.features
FEATURE_FUNDING_RATE = "derivatives_funding_rate"
FEATURE_FUNDING_ZSCORE = "derivatives_funding_zscore"
FEATURE_FUNDING_PERCENTILE = "derivatives_funding_percentile"
FEATURE_OI_CHANGE_PCT = "derivatives_oi_change_pct"
FEATURE_CROWDING_CODE = "derivatives_crowding_code"
FEATURE_OI_REGIME_CODE = "derivatives_oi_regime_code"
FEATURE_OI_ANOMALY = "derivatives_oi_anomaly"
FEATURE_IS_STALE = "derivatives_is_stale"

_STD_EPSILON = 1e-12


class FundingCrowdingState(StrEnum):
    """Trạng thái nghiêng đám đông theo funding rate cực trị (fail-closed về UNKNOWN)."""

    CROWDED_LONG = "CROWDED_LONG"
    CROWDED_SHORT = "CROWDED_SHORT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class OpenInterestRegime(StrEnum):
    """Chế độ biến động Open Interest (fail-closed về UNKNOWN khi thiếu/cũ)."""

    EXPANDING = "EXPANDING"
    CONTRACTING = "CONTRACTING"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


_CROWDING_TO_CODE: dict[FundingCrowdingState, float | None] = {
    FundingCrowdingState.CROWDED_LONG: 1.0,
    FundingCrowdingState.CROWDED_SHORT: -1.0,
    FundingCrowdingState.NEUTRAL: 0.0,
    FundingCrowdingState.UNKNOWN: None,
}

_CODE_TO_CROWDING: dict[int, FundingCrowdingState] = {
    1: FundingCrowdingState.CROWDED_LONG,
    -1: FundingCrowdingState.CROWDED_SHORT,
    0: FundingCrowdingState.NEUTRAL,
}

_OI_REGIME_TO_CODE: dict[OpenInterestRegime, float | None] = {
    OpenInterestRegime.EXPANDING: 1.0,
    OpenInterestRegime.CONTRACTING: -1.0,
    OpenInterestRegime.FLAT: 0.0,
    OpenInterestRegime.UNKNOWN: None,
}

_CODE_TO_OI_REGIME: dict[int, OpenInterestRegime] = {
    1: OpenInterestRegime.EXPANDING,
    -1: OpenInterestRegime.CONTRACTING,
    0: OpenInterestRegime.FLAT,
}


@dataclass(frozen=True, slots=True)
class DerivativesContextConfig:
    """Cấu hình cửa sổ và ngưỡng xác định funding cực trị / biến động OI."""

    max_age: timedelta = DIGEST_MAX_AGE
    lookback_samples: int = 24
    min_samples: int = 8
    funding_zscore_extreme: float = 2.0
    funding_percentile_high: float = 0.90
    funding_percentile_low: float = 0.10
    funding_abs_extreme: Decimal = Decimal("0.0005")
    oi_change_threshold_pct: Decimal = Decimal("0.05")
    oi_spike_threshold_pct: Decimal = Decimal("0.15")

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("max_age phải lớn hơn 0.")
        if self.lookback_samples < 1:
            raise ValueError("lookback_samples phải >= 1.")
        if self.min_samples < 1:
            raise ValueError("min_samples phải >= 1.")
        if self.funding_zscore_extreme <= 0:
            raise ValueError("funding_zscore_extreme phải dương.")
        if not (0.5 < self.funding_percentile_high <= 1.0):
            raise ValueError("funding_percentile_high phải nằm trong (0.5, 1.0].")
        if not (0.0 <= self.funding_percentile_low < 0.5):
            raise ValueError("funding_percentile_low phải nằm trong [0.0, 0.5).")
        if self.funding_abs_extreme <= Decimal("0"):
            raise ValueError("funding_abs_extreme phải dương.")
        if self.oi_change_threshold_pct <= Decimal("0"):
            raise ValueError("oi_change_threshold_pct phải dương.")
        if self.oi_spike_threshold_pct < self.oi_change_threshold_pct:
            raise ValueError("oi_spike_threshold_pct phải >= oi_change_threshold_pct.")


@dataclass(frozen=True, slots=True)
class DerivativesContextObservation:
    """Quan sát bối cảnh phái sinh point-in-time tại ``as_of``."""

    symbol: str
    as_of: datetime
    latest_collected_at: datetime | None
    is_stale: bool
    sample_count: int
    funding_rate: Decimal | None
    funding_zscore: float | None
    funding_percentile: float | None
    crowding_state: FundingCrowdingState
    oi_value_usdt: Decimal | None
    oi_change_pct: Decimal | None
    oi_regime: OpenInterestRegime
    oi_anomaly: bool
    formula_version: str = DERIVATIVES_CONTEXT_FORMULA_VERSION

    def to_metrics(self) -> dict[str, Any]:
        """Xuất từ điển chỉ số gọn, JSON-safe phục vụ ContextGateResult.metrics."""
        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "latest_collected_at": (
                self.latest_collected_at.isoformat()
                if self.latest_collected_at is not None
                else None
            ),
            "is_stale": self.is_stale,
            "sample_count": self.sample_count,
            "funding_rate": float(self.funding_rate) if self.funding_rate is not None else None,
            "funding_zscore": self.funding_zscore,
            "funding_percentile": self.funding_percentile,
            "crowding_state": self.crowding_state.value,
            "oi_value_usdt": float(self.oi_value_usdt) if self.oi_value_usdt is not None else None,
            "oi_change_pct": (
                float(self.oi_change_pct) if self.oi_change_pct is not None else None
            ),
            "oi_regime": self.oi_regime.value,
            "oi_anomaly": self.oi_anomaly,
            "formula_version": self.formula_version,
        }

    def to_feature_observations(
        self,
        *,
        timeframe: str = "1h",
        snapshot_prefix: str = "deriv-ctx",
    ) -> tuple[FeatureObservation, ...]:
        """Đóng gói thành ``FeatureObservation`` chuẩn để gắn vào ``DecisionSnapshot.features``."""
        available_at = self.latest_collected_at or self.as_of
        if available_at > self.as_of:
            available_at = self.as_of
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
                available_at=available_at,
                quality=quality,
            )

        return (
            _make(
                FEATURE_FUNDING_RATE,
                float(self.funding_rate) if self.funding_rate is not None else None,
            ),
            _make(FEATURE_FUNDING_ZSCORE, self.funding_zscore),
            _make(FEATURE_FUNDING_PERCENTILE, self.funding_percentile),
            _make(
                FEATURE_OI_CHANGE_PCT,
                float(self.oi_change_pct) if self.oi_change_pct is not None else None,
            ),
            _make(FEATURE_CROWDING_CODE, _CROWDING_TO_CODE[self.crowding_state]),
            _make(FEATURE_OI_REGIME_CODE, _OI_REGIME_TO_CODE[self.oi_regime]),
            _make(FEATURE_OI_ANOMALY, 1.0 if self.oi_anomaly else 0.0),
            _make(FEATURE_IS_STALE, 1.0 if self.is_stale else 0.0),
        )


def _mid_rank_percentile(value: float, prior: Sequence[float]) -> float:
    """Phân vị bậc giữa ``(nhỏ_hơn + 0.5 * bằng) / n`` của ``value`` so với ``prior``."""
    less = sum(1 for item in prior if item < value)
    equal = sum(1 for item in prior if item == value)
    return (less + 0.5 * equal) / len(prior)


def _exclusive_zscore(value: float, prior: Sequence[float]) -> float | None:
    """Z-score của ``value`` so với trung bình và độ lệch chuẩn của cửa sổ ``prior``."""
    n = len(prior)
    if n == 0:
        return None
    mean_val = math.fsum(prior) / n
    variance = math.fsum((item - mean_val) ** 2 for item in prior) / n
    std_val = math.sqrt(variance)
    if not math.isfinite(std_val) or std_val <= _STD_EPSILON:
        return None
    z_val = (value - mean_val) / std_val
    return z_val if math.isfinite(z_val) else None


def compute_derivatives_context(
    snapshots: Sequence[DerivativesSnapshot],
    *,
    symbol: str,
    as_of: datetime,
    config: DerivativesContextConfig | None = None,
) -> DerivativesContextObservation:
    """Tính bối cảnh Funding & Open Interest point-in-time tại ``as_of`` (không nhìn trước).

    Bất biến tiền tố: chỉ đọc các ``DerivativesSnapshot`` khớp ``symbol`` có
    ``collected_at <= as_of`` và ``premium_time <= as_of``. Khi thiếu dữ liệu hoặc
    bản ghi mới nhất cũ hơn ``config.max_age``, hàm fail-closed trả ``is_stale=True`` và
    ``UNKNOWN``/``None`` cho mọi trạng thái.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of phải có múi giờ.")
    cfg = config or DerivativesContextConfig()
    instant = as_of.astimezone(UTC)

    # Lọc nghiêm ngặt dữ liệu đã khả dụng tại as_of và khử trùng theo collected_at
    dedup: dict[datetime, DerivativesSnapshot] = {}
    for snap in snapshots:
        if snap.symbol != symbol:
            continue
        snap_collected = snap.collected_at.astimezone(UTC)
        snap_premium = snap.premium_time.astimezone(UTC)
        if snap_collected > instant or snap_premium > instant:
            continue
        dedup[snap_collected] = snap

    eligible = [dedup[key] for key in sorted(dedup)]
    if not eligible:
        return DerivativesContextObservation(
            symbol=symbol,
            as_of=instant,
            latest_collected_at=None,
            is_stale=True,
            sample_count=0,
            funding_rate=None,
            funding_zscore=None,
            funding_percentile=None,
            crowding_state=FundingCrowdingState.UNKNOWN,
            oi_value_usdt=None,
            oi_change_pct=None,
            oi_regime=OpenInterestRegime.UNKNOWN,
            oi_anomaly=False,
        )

    latest = eligible[-1]
    latest_collected = latest.collected_at.astimezone(UTC)
    if instant - latest_collected > cfg.max_age:
        return DerivativesContextObservation(
            symbol=symbol,
            as_of=instant,
            latest_collected_at=latest_collected,
            is_stale=True,
            sample_count=len(eligible),
            funding_rate=None,
            funding_zscore=None,
            funding_percentile=None,
            crowding_state=FundingCrowdingState.UNKNOWN,
            oi_value_usdt=None,
            oi_change_pct=None,
            oi_regime=OpenInterestRegime.UNKNOWN,
            oi_anomaly=False,
        )

    prior_snaps = eligible[:-1][-cfg.lookback_samples :]
    funding_rate = latest.funding_rate
    funding_zscore: float | None = None
    funding_percentile: float | None = None
    crowding_state = FundingCrowdingState.UNKNOWN

    if len(prior_snaps) >= cfg.min_samples:
        prior_rates = [float(item.funding_rate) for item in prior_snaps]
        curr_rate_f = float(funding_rate)
        funding_percentile = _mid_rank_percentile(curr_rate_f, prior_rates)
        funding_zscore = _exclusive_zscore(curr_rate_f, prior_rates)

        is_extreme_pos = funding_rate > Decimal("0") and (
            (funding_zscore is not None and funding_zscore >= cfg.funding_zscore_extreme)
            or funding_percentile >= cfg.funding_percentile_high
            or funding_rate >= cfg.funding_abs_extreme
        )
        is_extreme_neg = funding_rate < Decimal("0") and (
            (funding_zscore is not None and funding_zscore <= -cfg.funding_zscore_extreme)
            or funding_percentile <= cfg.funding_percentile_low
            or funding_rate <= -cfg.funding_abs_extreme
        )
        if is_extreme_pos:
            crowding_state = FundingCrowdingState.CROWDED_LONG
        elif is_extreme_neg:
            crowding_state = FundingCrowdingState.CROWDED_SHORT
        else:
            crowding_state = FundingCrowdingState.NEUTRAL

    # Tính biến động Open Interest nhân quả
    oi_valid_latest = (
        latest.oi_value_usdt is not None
        and latest.oi_value_usdt >= Decimal("0")
        and (latest.oi_at is None or latest.oi_at.astimezone(UTC) <= instant)
    )
    oi_value_usdt = latest.oi_value_usdt if oi_valid_latest else None
    oi_change_pct: Decimal | None = None
    if oi_valid_latest and oi_value_usdt is not None:
        if latest.oi_change_24h is not None:
            oi_change_pct = latest.oi_change_24h
        else:
            ref_oi = next(
                (
                    item.oi_value_usdt
                    for item in prior_snaps
                    if item.oi_value_usdt is not None
                    and item.oi_value_usdt > Decimal("0")
                    and (item.oi_at is None or item.oi_at.astimezone(UTC) <= instant)
                ),
                None,
            )
            if ref_oi is not None:
                oi_change_pct = oi_value_usdt / ref_oi - Decimal("1")

    if oi_change_pct is None:
        oi_regime = OpenInterestRegime.UNKNOWN
        oi_anomaly = False
    else:
        if oi_change_pct >= cfg.oi_change_threshold_pct:
            oi_regime = OpenInterestRegime.EXPANDING
        elif oi_change_pct <= -cfg.oi_change_threshold_pct:
            oi_regime = OpenInterestRegime.CONTRACTING
        else:
            oi_regime = OpenInterestRegime.FLAT
        oi_anomaly = abs(oi_change_pct) >= cfg.oi_spike_threshold_pct

    return DerivativesContextObservation(
        symbol=symbol,
        as_of=instant,
        latest_collected_at=latest_collected,
        is_stale=False,
        sample_count=len(eligible),
        funding_rate=funding_rate,
        funding_zscore=funding_zscore,
        funding_percentile=funding_percentile,
        crowding_state=crowding_state,
        oi_value_usdt=oi_value_usdt,
        oi_change_pct=oi_change_pct,
        oi_regime=oi_regime,
        oi_anomaly=oi_anomaly,
    )


def observation_from_snapshot_features(
    snapshot: DecisionSnapshot,
) -> DerivativesContextObservation | None:
    """Khôi phục ``DerivativesContextObservation`` từ các feature ``derivatives_*`` trên snapshot.

    Trả ``None`` nếu snapshot không chứa bất kỳ feature phái sinh nào. Bảo vệ point-in-time:
    bỏ qua mọi feature có ``available_at > snapshot.decision_time``.
    """
    by_name: dict[str, FeatureObservation] = {}
    for feat in snapshot.features:
        if feat.name.startswith("derivatives_") and feat.available_at <= snapshot.decision_time:
            existing = by_name.get(feat.name)
            if existing is None or feat.available_at >= existing.available_at:
                by_name[feat.name] = feat

    if not by_name:
        return None

    stale_feat = by_name.get(FEATURE_IS_STALE)
    is_stale = any(f.quality is FeatureQuality.STALE for f in by_name.values()) or (
        stale_feat is not None and stale_feat.value is not None and stale_feat.value >= 0.5
    )
    latest_at = max(f.available_at for f in by_name.values())

    def _ready_float(name: str) -> float | None:
        feat = by_name.get(name)
        if feat is None or feat.quality is not FeatureQuality.READY or feat.value is None:
            return None
        return float(feat.value)

    if is_stale:
        return DerivativesContextObservation(
            symbol=snapshot.symbol,
            as_of=snapshot.decision_time.astimezone(UTC),
            latest_collected_at=latest_at.astimezone(UTC),
            is_stale=True,
            sample_count=0,
            funding_rate=None,
            funding_zscore=None,
            funding_percentile=None,
            crowding_state=FundingCrowdingState.UNKNOWN,
            oi_value_usdt=None,
            oi_change_pct=None,
            oi_regime=OpenInterestRegime.UNKNOWN,
            oi_anomaly=False,
        )

    fr_val = _ready_float(FEATURE_FUNDING_RATE)
    fz_val = _ready_float(FEATURE_FUNDING_ZSCORE)
    fp_val = _ready_float(FEATURE_FUNDING_PERCENTILE)
    oi_chg_val = _ready_float(FEATURE_OI_CHANGE_PCT)
    crowd_code = _ready_float(FEATURE_CROWDING_CODE)
    oi_reg_code = _ready_float(FEATURE_OI_REGIME_CODE)
    oi_anom_val = _ready_float(FEATURE_OI_ANOMALY)

    crowding_state = (
        _CODE_TO_CROWDING.get(int(round(crowd_code)), FundingCrowdingState.UNKNOWN)
        if crowd_code is not None
        else FundingCrowdingState.UNKNOWN
    )
    oi_regime = (
        _CODE_TO_OI_REGIME.get(int(round(oi_reg_code)), OpenInterestRegime.UNKNOWN)
        if oi_reg_code is not None
        else OpenInterestRegime.UNKNOWN
    )

    return DerivativesContextObservation(
        symbol=snapshot.symbol,
        as_of=snapshot.decision_time.astimezone(UTC),
        latest_collected_at=latest_at.astimezone(UTC),
        is_stale=False,
        sample_count=1,
        funding_rate=Decimal(str(fr_val)) if fr_val is not None else None,
        funding_zscore=fz_val,
        funding_percentile=fp_val,
        crowding_state=crowding_state,
        oi_value_usdt=None,
        oi_change_pct=Decimal(str(oi_chg_val)) if oi_chg_val is not None else None,
        oi_regime=oi_regime,
        oi_anomaly=bool(oi_anom_val is not None and oi_anom_val >= 0.5),
    )


__all__ = [
    "DERIVATIVES_CONTEXT_FORMULA_VERSION",
    "FEATURE_CROWDING_CODE",
    "FEATURE_FUNDING_PERCENTILE",
    "FEATURE_FUNDING_RATE",
    "FEATURE_FUNDING_ZSCORE",
    "FEATURE_IS_STALE",
    "FEATURE_OI_ANOMALY",
    "FEATURE_OI_CHANGE_PCT",
    "FEATURE_OI_REGIME_CODE",
    "DerivativesContextConfig",
    "DerivativesContextObservation",
    "FundingCrowdingState",
    "OpenInterestRegime",
    "compute_derivatives_context",
    "observation_from_snapshot_features",
]
