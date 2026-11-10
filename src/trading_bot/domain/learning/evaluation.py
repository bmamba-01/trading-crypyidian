"""Chia tập thời gian và tính số liệu đánh giá thuần, có thể tái lập."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal("0")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


def _utc(value: datetime, name: str) -> datetime:
    _aware(value, name)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TemporalWindow:
    """Khoảng thời gian nửa mở [start, end), dùng để tránh chồng lấn mẫu."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = _utc(self.start, "start")
        end = _utc(self.end, "end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if start >= end:
            raise ValueError("Temporal window phải có start < end.")


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    """Một fold huấn luyện, validation và test theo đúng thứ tự thời gian."""

    train: TemporalWindow
    validation: TemporalWindow
    test: TemporalWindow


def walk_forward_splits(
    data_range: TemporalWindow,
    *,
    train: timedelta,
    validation: timedelta,
    test: timedelta,
    step: timedelta,
    purge: timedelta = timedelta(0),
    embargo: timedelta = timedelta(0),
) -> tuple[WalkForwardSplit, ...]:
    """Tạo fold cuốn chiếu; purge/embargo là khoảng bỏ trống giữa các cửa sổ."""
    durations = (train, validation, test, step)
    if any(duration <= timedelta(0) for duration in durations):
        raise ValueError("Train, validation, test và step phải dương.")
    if purge < timedelta(0) or embargo < timedelta(0):
        raise ValueError("Purge và embargo không được âm.")

    result: list[WalkForwardSplit] = []
    train_start = data_range.start
    while True:
        train_end = train_start + train
        validation_start = train_end + purge
        validation_end = validation_start + validation
        test_start = validation_end + embargo
        test_end = test_start + test
        if test_end > data_range.end:
            break
        result.append(
            WalkForwardSplit(
                TemporalWindow(train_start, train_end),
                TemporalWindow(validation_start, validation_end),
                TemporalWindow(test_start, test_end),
            )
        )
        train_start += step
    return tuple(result)


def purge_overlapping_samples(
    sample_windows: Sequence[TemporalWindow],
    protected_windows: Sequence[TemporalWindow],
) -> tuple[TemporalWindow, ...]:
    """Giữ lại các mẫu không chồng lấn với cửa sổ validation/test được bảo vệ."""
    return tuple(
        sample
        for sample in sample_windows
        if not any(
            sample.start < protected.end and protected.start < sample.end
            for protected in protected_windows
        )
    )


@dataclass(frozen=True, slots=True)
class SampleLineage:
    """Nguồn gốc tối thiểu của mẫu; availability là mốc muộn nhất của mọi đầu vào.

    Caller phải tổng hợp cả dữ liệu nguồn và các bản sửa đổi đã dùng vào
    feature_available_at. Khoảng nhãn biểu diễn thời gian quan sát kết quả,
    không biểu diễn cửa sổ đọc dữ liệu quá khứ của đặc trưng.
    """

    sample_id: str
    decision_at: datetime
    feature_available_at: datetime
    label_interval: TemporalWindow

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id.strip():
            raise ValueError("sample_id phải là chuỗi không rỗng.")
        for name in ("decision_at", "feature_available_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime):
                raise ValueError(f"{name} phải là thời điểm có múi giờ.")
            object.__setattr__(self, name, _utc(value, name))
        if not isinstance(self.label_interval, TemporalWindow):
            raise ValueError("label_interval phải có nguồn gốc thời gian đầy đủ.")
        if self.label_interval.start < self.decision_at:
            raise ValueError("Khoảng nhãn không được bắt đầu trước quyết định.")


class SampleExclusionReason(StrEnum):
    """Mã lý do loại mẫu ổn định; một mẫu có thể vi phạm nhiều điều kiện."""

    FEATURE_UNAVAILABLE_AT_DECISION = "feature_unavailable_at_decision"
    LABEL_INCOMPLETE_AT_CUTOFF = "label_incomplete_at_cutoff"
    PROTECTED_LABEL_OVERLAP = "protected_label_overlap"


@dataclass(frozen=True, slots=True)
class SampleExclusion:
    """Danh tính mẫu bị loại và đầy đủ lý do theo thứ tự kiểm tra cố định."""

    sample_id: str
    reasons: tuple[SampleExclusionReason, ...]


@dataclass(frozen=True, slots=True)
class TemporalSampleSelection:
    """Kết quả thuần, bất biến; hai tập đều được sắp theo sample_id."""

    eligible_sample_ids: tuple[str, ...]
    exclusions: tuple[SampleExclusion, ...]


def select_temporal_samples(
    samples: Sequence[SampleLineage],
    *,
    evaluation_cutoff: datetime,
    protected_samples: Sequence[SampleLineage] = (),
) -> TemporalSampleSelection:
    """Chọn mẫu đủ thời điểm và loại giao khoảng nhãn validation/test.

    Khoảng nhãn là nửa mở; hoàn tất đúng cutoff được chấp nhận. Dữ liệu
    lịch sử dùng chung không bị loại nếu đã khả dụng tại decision_at.
    Các mẫu bảo vệ luôn giữ nguyên vùng nhãn, kể cả khi bản thân chúng chưa
    đủ điều kiện tại cutoff; không âm thầm thu hẹp vùng bảo vệ. Thiếu nguồn
    gốc hay trùng danh tính ở bất kỳ vai trò nào làm toàn bộ phép chọn lỗi.
    """
    if not isinstance(evaluation_cutoff, datetime):
        raise ValueError("evaluation_cutoff phải là thời điểm có múi giờ.")
    evaluation_cutoff = _utc(evaluation_cutoff, "evaluation_cutoff")
    candidates = tuple(samples)
    protected = tuple(protected_samples)
    seen: set[str] = set()
    for sample in (*candidates, *protected):
        if not isinstance(sample, SampleLineage):
            raise ValueError("Mẫu phải có SampleLineage đầy đủ.")
        if sample.sample_id in seen:
            raise ValueError("sample_id bị trùng giữa các mẫu hoặc vai trò.")
        seen.add(sample.sample_id)

    eligible: list[str] = []
    exclusions: list[SampleExclusion] = []
    for sample in sorted(candidates, key=lambda item: item.sample_id):
        reasons: list[SampleExclusionReason] = []
        if sample.feature_available_at > sample.decision_at:
            reasons.append(SampleExclusionReason.FEATURE_UNAVAILABLE_AT_DECISION)
        if sample.label_interval.end > evaluation_cutoff:
            reasons.append(SampleExclusionReason.LABEL_INCOMPLETE_AT_CUTOFF)
        if any(
            sample.label_interval.start < held.label_interval.end
            and held.label_interval.start < sample.label_interval.end
            for held in protected
        ):
            reasons.append(SampleExclusionReason.PROTECTED_LABEL_OVERLAP)
        if reasons:
            exclusions.append(SampleExclusion(sample.sample_id, tuple(reasons)))
        else:
            eligible.append(sample.sample_id)
    return TemporalSampleSelection(tuple(eligible), tuple(exclusions))


def _normalized_entry_features(
    features: tuple[tuple[str, float], ...],
) -> tuple[tuple[str, float], ...]:
    """Chuẩn hóa vector bất biến, từ chối khóa trùng và số không hữu hạn."""
    if not isinstance(features, tuple) or not features:
        raise ValueError("entry_features phải là tuple không rỗng.")
    normalized: list[tuple[str, float]] = []
    for pair in features:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("entry_features phải chứa cặp khóa/giá trị.")
        key, value = pair
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Khóa entry feature không được rỗng.")
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Giá trị entry feature phải là số hữu hạn.")
        normalized.append((key.strip(), float(value) if value != 0 else 0.0))
    if len({key for key, _ in normalized}) != len(normalized):
        raise ValueError("Khóa entry feature bị trùng sau chuẩn hóa.")
    return tuple(sorted(normalized))


def calculate_entry_features_hash(
    snapshot_id: str,
    decision_at: datetime,
    schema_hash: str,
    features: tuple[tuple[str, float], ...],
) -> str:
    """SHA-256 của vector canonical, gắn snapshot/thời điểm UTC và schema."""
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("entry_decision_snapshot_id không được rỗng.")
    if not isinstance(decision_at, datetime):
        raise ValueError("entry_decision_at phải có múi giờ.")
    if not isinstance(schema_hash, str) or re.fullmatch(r"[0-9a-f]{64}", schema_hash) is None:
        raise ValueError("entry_feature_schema_hash phải là lowercase SHA-256.")
    payload = {
        "entry_decision_snapshot_id": snapshot_id,
        "entry_decision_at": _utc(decision_at, "entry_decision_at").isoformat(),
        "entry_feature_schema_hash": schema_hash,
        "entry_features": _normalized_entry_features(features),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationTrade:
    """Kết quả một giao dịch đã đóng; các chi phí được ghi tách riêng."""

    trade_id: str
    opened_at: datetime
    closed_at: datetime
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    exposure: Decimal
    entry_decision_snapshot_id: str | None = None
    entry_decision_at: datetime | None = None
    entry_feature_schema_hash: str | None = None
    entry_features: tuple[tuple[str, float], ...] = ()
    entry_features_hash: str | None = None
    entry_symbol: str | None = None
    entry_opportunity_event_id: str | None = None
    entry_candidate_id: str | None = None
    entry_vector_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.trade_id.strip():
            raise ValueError("trade_id không được để trống.")
        object.__setattr__(self, "opened_at", _utc(self.opened_at, "opened_at"))
        object.__setattr__(self, "closed_at", _utc(self.closed_at, "closed_at"))
        if self.closed_at < self.opened_at:
            raise ValueError("Giao dịch không thể đóng trước khi mở.")
        if not isinstance(self.gross_pnl, Decimal) or not self.gross_pnl.is_finite():
            raise ValueError("gross_pnl phải là Decimal hữu hạn.")
        for name in ("fees", "slippage", "exposure"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} phải là Decimal hữu hạn không âm.")
        if not isinstance(self.funding, Decimal) or not self.funding.is_finite():
            raise ValueError("funding phải là dòng tiền Decimal hữu hạn có dấu.")

        if (
            len(
                {
                    value is None
                    for value in (
                        self.entry_opportunity_event_id,
                        self.entry_candidate_id,
                        self.entry_vector_hash,
                    )
                }
            )
            != 1
        ):
            raise ValueError("Entry opportunity/candidate phải cùng có hoặc cùng vắng.")
        if self.entry_opportunity_event_id is not None:
            if (
                not isinstance(self.entry_opportunity_event_id, str)
                or not self.entry_opportunity_event_id.strip()
            ):
                raise ValueError("entry_opportunity_event_id không được trống.")
            if (
                not isinstance(self.entry_candidate_id, str)
                or re.fullmatch("[0-9a-f]{64}", self.entry_candidate_id) is None
            ):
                raise ValueError("entry_candidate_id phải là SHA-256 hex chữ thường.")
            if (
                not isinstance(self.entry_vector_hash, str)
                or re.fullmatch("[0-9a-f]{64}", self.entry_vector_hash) is None
            ):
                raise ValueError("entry_vector_hash phải là SHA-256 hex chữ thường.")
            if self.entry_decision_snapshot_id is None:
                raise ValueError("Entry opportunity đòi provenance đầy đủ.")

        metadata = (
            self.entry_decision_snapshot_id,
            self.entry_decision_at,
            self.entry_feature_schema_hash,
            self.entry_features_hash,
            self.entry_symbol,
        )
        if all(value is None for value in metadata) and self.entry_features == ():
            return
        if any(value is None for value in metadata):
            raise ValueError("Entry provenance phải đầy đủ hoặc hoàn toàn vắng mặt.")
        assert self.entry_decision_snapshot_id is not None
        assert self.entry_decision_at is not None
        assert self.entry_feature_schema_hash is not None
        if not isinstance(self.entry_symbol, str):
            raise ValueError("entry_symbol phải là mã symbol hợp lệ.")
        entry_symbol = self.entry_symbol.strip().upper()
        if re.fullmatch(r"[A-Z0-9_]{2,32}", entry_symbol) is None:
            raise ValueError("entry_symbol phải là mã symbol hợp lệ.")
        object.__setattr__(self, "entry_symbol", entry_symbol)
        expected_hash = calculate_entry_features_hash(
            self.entry_decision_snapshot_id,
            self.entry_decision_at,
            self.entry_feature_schema_hash,
            self.entry_features,
        )
        if self.entry_features_hash != expected_hash:
            raise ValueError("entry_features_hash không khớp SHA-256 canonical.")
        object.__setattr__(
            self, "entry_decision_at", _utc(self.entry_decision_at, "entry_decision_at")
        )
        object.__setattr__(self, "entry_features", _normalized_entry_features(self.entry_features))

    @property
    def has_entry_provenance(self) -> bool:
        """Outcome legacy thiếu bằng chứng entry nên không đủ điều kiện học."""
        return self.entry_decision_snapshot_id is not None

    @property
    def net_pnl(self) -> Decimal:
        """Lãi/lỗ sau phí, funding và trượt giá, mỗi khoản chỉ trừ một lần."""
        return self.gross_pnl - self.fees - self.funding - self.slippage


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """Số liệu net, chi phí, độ phủ và khoảng bất định của một run."""

    observation_count: int
    usable_observation_count: int
    trade_count: int
    coverage: float
    trade_frequency: float
    gross_pnl: Decimal
    net_pnl: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    expectancy: Decimal
    max_drawdown: Decimal
    average_exposure: Decimal
    confidence_interval: tuple[Decimal, Decimal] | None
    caveat_vi: str


def build_evaluation_metrics(
    trades: tuple[EvaluationTrade, ...],
    *,
    observation_count: int,
    seed: int,
    usable_observation_count: int | None = None,
) -> EvaluationMetrics:
    """Tính báo cáo và khoảng tin cậy bootstrap theo block liên tiếp."""
    usable_count = (
        observation_count if usable_observation_count is None else usable_observation_count
    )
    if (
        observation_count < 0
        or usable_count < 0
        or usable_count > observation_count
        or len(trades) > usable_count
    ):
        raise ValueError("Số quan sát usable phải nằm trong tổng và chứa đủ giao dịch.")
    if seed < 0:
        raise ValueError("Seed phải không âm.")
    ids = tuple(trade.trade_id for trade in trades)
    if len(ids) != len(set(ids)):
        raise ValueError("trade_id bị trùng.")
    if any(
        left.closed_at > right.closed_at for left, right in zip(trades, trades[1:], strict=False)
    ):
        raise ValueError("Giao dịch phải được sắp theo thời điểm đóng.")
    net_values = tuple(trade.net_pnl for trade in trades)
    gross_pnl = sum((trade.gross_pnl for trade in trades), ZERO)
    net_pnl = sum(net_values, ZERO)
    fees = sum((trade.fees for trade in trades), ZERO)
    funding = sum((trade.funding for trade in trades), ZERO)
    slippage = sum((trade.slippage for trade in trades), ZERO)
    expectancy = net_pnl / len(trades) if trades else ZERO
    equity = peak = drawdown = ZERO
    for value in net_values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    average_exposure = (
        sum((trade.exposure for trade in trades), ZERO) / len(trades) if trades else ZERO
    )
    interval = _block_bootstrap_interval(net_values, seed=seed) if len(trades) >= 2 else None
    return EvaluationMetrics(
        observation_count=observation_count,
        usable_observation_count=usable_count,
        trade_count=len(trades),
        coverage=(usable_count / observation_count if observation_count else 0.0),
        trade_frequency=(len(trades) / usable_count if usable_count else 0.0),
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        fees=fees,
        funding=funding,
        slippage=slippage,
        expectancy=expectancy,
        max_drawdown=drawdown,
        average_exposure=average_exposure,
        confidence_interval=interval,
        caveat_vi=(
            "Khoảng bất định bootstrap theo block chỉ mô tả mẫu này, không phải bằng chứng "
            "lợi nhuận tương lai hay cam kết có lợi thế. Exposure là trung bình mỗi giao dịch, "
            "không phải exposure theo thời gian."
        ),
    )


def _block_bootstrap_interval(values: tuple[Decimal, ...], *, seed: int) -> tuple[Decimal, Decimal]:
    """Ước lượng CI 95% bằng lấy mẫu block tròn để giữ phụ thuộc cục bộ."""
    sample_size = len(values)
    block_size = max(1, math.isqrt(sample_size))
    generator = random.Random(seed)
    estimates: list[Decimal] = []
    for _ in range(500):
        sample: list[Decimal] = []
        while len(sample) < sample_size:
            start = generator.randrange(sample_size)
            sample.extend(values[(start + offset) % sample_size] for offset in range(block_size))
        estimates.append(sum(sample[:sample_size], ZERO) / sample_size)
    estimates.sort()
    low = estimates[int(0.025 * (len(estimates) - 1))]
    high = estimates[int(0.975 * (len(estimates) - 1))]
    return low, high
