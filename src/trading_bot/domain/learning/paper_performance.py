"""Chẩn đoán PAPER-only bằng điểm số xác suất và cohort đã trưởng thành."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum

CLASSES = (-1, 0, 1)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NORMALIZATION_TOLERANCE = Decimal("0.000000000001")


class PaperPerformanceStatus(StrEnum):
    """Trạng thái chẩn đoán; không điều khiển trạng thái hoặc quyền của bot."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    INCONCLUSIVE = "inconclusive"


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải là thời điểm có múi giờ.")
    return value.astimezone(UTC)


def _decimal(value: Decimal | int | float, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise ValueError(f"{name} phải là số hữu hạn.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as error:
        raise ValueError(f"{name} phải là số hữu hạn.") from error
    if not result.is_finite():
        raise ValueError(f"{name} phải là số hữu hạn.")
    return result


@dataclass(frozen=True, slots=True)
class PaperPerformancePolicy:
    """Ngưỡng chẩn đoán cố định, được ghim bằng phiên bản và SHA-256."""

    version: str = "wp06-paper-performance-v1"
    cohort_days: int = 30
    maturity_days: int = 30
    minimum_outcomes: int = 1_000
    minimum_class_support: int = 100
    minimum_coverage: Decimal = Decimal("0.80")
    maximum_brier_regression: Decimal = Decimal("0.01")
    maximum_log_loss_regression: Decimal = Decimal("0.01")
    log_loss_epsilon: Decimal = Decimal("0.000000000001")

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("Policy version không được để trống.")
        for name in ("cohort_days", "maturity_days", "minimum_outcomes", "minimum_class_support"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} phải là số nguyên dương.")
        for name in (
            "minimum_coverage",
            "maximum_brier_regression",
            "maximum_log_loss_regression",
            "log_loss_epsilon",
        ):
            value = _decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if not Decimal("0") < self.minimum_coverage <= Decimal("1"):
            raise ValueError("minimum_coverage phải nằm trong (0, 1].")
        if self.maximum_brier_regression < 0 or self.maximum_log_loss_regression < 0:
            raise ValueError("Ngưỡng regression không được âm.")
        if not Decimal("0") < self.log_loss_epsilon < Decimal("1"):
            raise ValueError("log_loss_epsilon phải nằm trong (0, 1).")

    @property
    def content_hash(self) -> str:
        """Hash canonical của toàn bộ tuple ngưỡng và quy ước lớp/clipping."""
        payload = {
            "class_order": list(CLASSES),
            "cohort_days": self.cohort_days,
            "log_loss_epsilon": str(self.log_loss_epsilon),
            "maturity_days": self.maturity_days,
            "maximum_brier_regression": str(self.maximum_brier_regression),
            "maximum_log_loss_regression": str(self.maximum_log_loss_regression),
            "minimum_class_support": self.minimum_class_support,
            "minimum_coverage": str(self.minimum_coverage),
            "minimum_outcomes": self.minimum_outcomes,
            "version": self.version,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PaperPerformanceCohort:
    """Số liệu aggregate của một cửa sổ quyết định và kết quả đã trưởng thành."""

    window_start: datetime
    window_end: datetime
    eligible_decision_count: int
    covered_outcome_count: int
    class_support: tuple[tuple[int, int], ...]
    observed_brier: Decimal | None
    observed_log_loss: Decimal | None
    baseline_brier: Decimal
    baseline_log_loss: Decimal
    membership_digest: str
    status: PaperPerformanceStatus = PaperPerformanceStatus.INCONCLUSIVE
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        start = _utc(self.window_start, "window_start")
        end = _utc(self.window_end, "window_end")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        if start >= end:
            raise ValueError("Cohort window phải có start < end.")
        for name in ("eligible_decision_count", "covered_outcome_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} phải là số nguyên không âm.")
        if self.covered_outcome_count > self.eligible_decision_count:
            raise ValueError("Covered outcomes không thể vượt eligible decisions.")
        support = tuple(self.class_support)
        if (
            len(support) != len(CLASSES)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not int
                or type(item[1]) is not int
                or item[1] < 0
                for item in support
            )
            or tuple(label for label, _ in support) != CLASSES
        ):
            raise ValueError("class_support phải có đúng thứ tự các lớp -1, 0, +1.")
        if sum(count for _, count in support) != self.covered_outcome_count:
            raise ValueError("Tổng class_support phải khớp số outcome được quy kết.")
        object.__setattr__(self, "class_support", support)
        for name in ("baseline_brier", "baseline_log_loss"):
            value = _decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        observed = (self.observed_brier, self.observed_log_loss)
        if (observed[0] is None) != (observed[1] is None):
            raise ValueError("Observed Brier và log-loss phải cùng có hoặc cùng thiếu.")
        if (self.covered_outcome_count == 0) != (observed[0] is None):
            raise ValueError("Observed score chỉ có khi có outcome được quy kết.")
        if observed[0] is not None and observed[1] is not None:
            object.__setattr__(self, "observed_brier", _decimal(observed[0], "observed_brier"))
            object.__setattr__(
                self, "observed_log_loss", _decimal(observed[1], "observed_log_loss")
            )
        if any(
            value < 0
            for value in (
                self.baseline_brier,
                self.baseline_log_loss,
            )
        ):
            raise ValueError("Brier và log-loss không được âm.")
        if any(value is not None and value < 0 for value in observed):
            raise ValueError("Brier và log-loss không được âm.")
        if (
            not isinstance(self.membership_digest, str)
            or _SHA256.fullmatch(self.membership_digest) is None
        ):
            raise ValueError("membership_digest phải là lowercase SHA-256.")
        if not isinstance(self.status, PaperPerformanceStatus):
            raise ValueError("status cohort không hợp lệ.")
        reasons = tuple(self.reasons)
        if any(not isinstance(reason, str) or not reason.strip() for reason in reasons):
            raise ValueError("reasons phải chứa mã lý do không rỗng.")
        if len(reasons) != len(set(reasons)):
            raise ValueError("reasons không được trùng.")
        object.__setattr__(self, "reasons", reasons)

    @property
    def coverage(self) -> Decimal:
        """Tỷ lệ intent đủ điều kiện có đúng một kết quả PAPER đã trưởng thành."""
        if self.eligible_decision_count == 0:
            return Decimal("0")
        return Decimal(self.covered_outcome_count) / Decimal(self.eligible_decision_count)

    @property
    def brier_regression(self) -> Decimal | None:
        if self.observed_brier is None:
            return None
        return self.observed_brier - self.baseline_brier

    @property
    def log_loss_regression(self) -> Decimal | None:
        if self.observed_log_loss is None:
            return None
        return self.observed_log_loss - self.baseline_log_loss

    @property
    def degraded(self) -> bool:
        """Cho biết cohort riêng này vượt đồng thời cả hai ngưỡng regression."""
        return self.status is PaperPerformanceStatus.DEGRADED


@dataclass(frozen=True, slots=True)
class PaperPerformanceAssessment:
    """Kết quả thuần của hai cohort; không có lệnh hay trạng thái bot."""

    as_of: datetime
    policy_version: str
    policy_hash: str
    status: PaperPerformanceStatus
    cohorts: tuple[PaperPerformanceCohort, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("policy_version không hợp lệ.")
        if _SHA256.fullmatch(self.policy_hash) is None:
            raise ValueError("policy_hash phải là lowercase SHA-256.")
        if not isinstance(self.status, PaperPerformanceStatus):
            raise ValueError("status assessment không hợp lệ.")
        cohorts = tuple(self.cohorts)
        if len(cohorts) != 2 or any(type(item) is not PaperPerformanceCohort for item in cohorts):
            raise ValueError("Assessment cần đúng hai PaperPerformanceCohort.")
        object.__setattr__(self, "cohorts", cohorts)
        reasons = tuple(self.reasons)
        if any(not isinstance(reason, str) or not reason.strip() for reason in reasons):
            raise ValueError("Assessment reasons không được rỗng.")
        object.__setattr__(self, "reasons", reasons)


def score_multiclass_outcomes(
    probabilities: Sequence[Sequence[Decimal | int | float]],
    labels: Sequence[int],
    *,
    epsilon: Decimal = Decimal("0.000000000001"),
) -> tuple[Decimal, Decimal]:
    """Tính multiclass Brier và log-loss theo thứ tự xác suất (-1, 0, +1)."""
    rows = tuple(tuple(row) for row in probabilities)
    targets = tuple(labels)
    if not rows or len(rows) != len(targets):
        raise ValueError("Cần cùng số vector xác suất và nhãn không rỗng.")
    if any(type(label) is not int or label not in CLASSES for label in targets):
        raise ValueError("Nhãn chỉ được là -1, 0 hoặc +1.")
    minimum_probability = _decimal(epsilon, "epsilon")
    if not Decimal("0") < minimum_probability < Decimal("1"):
        raise ValueError("epsilon phải nằm trong (0, 1).")

    brier_values: list[Decimal] = []
    loss_values: list[Decimal] = []
    with localcontext() as context:
        context.prec = 34
        for row, label in zip(rows, targets, strict=True):
            if len(row) != len(CLASSES):
                raise ValueError("Mỗi vector cần xác suất theo thứ tự -1, 0, +1.")
            values = tuple(_decimal(item, "probability") for item in row)
            if any(value < 0 or value > 1 for value in values):
                raise ValueError("Xác suất phải nằm trong [0, 1].")
            total = sum(values, Decimal("0"))
            if total <= 0 or abs(total - Decimal("1")) > _NORMALIZATION_TOLERANCE:
                raise ValueError("Tổng xác suất phải bằng 1 trong sai số 1e-12.")
            normalized = tuple(value / total for value in values)
            target_index = CLASSES.index(label)
            brier_values.append(
                sum(
                    (
                        (probability - (Decimal("1") if index == target_index else Decimal("0")))
                        ** 2
                        for index, probability in enumerate(normalized)
                    ),
                    Decimal("0"),
                )
            )
            loss_values.append(-max(minimum_probability, normalized[target_index]).ln())
        count = Decimal(len(rows))
        brier = sum(brier_values, Decimal("0")) / count
        log_loss = sum(loss_values, Decimal("0")) / count
    return brier, log_loss


def assess_paper_performance(
    cohorts: Sequence[PaperPerformanceCohort],
    *,
    as_of: datetime,
    policy: PaperPerformancePolicy,
) -> PaperPerformanceAssessment:
    """Áp dụng ngưỡng hai cohort đã trưởng thành; thiếu căn cứ là INCONCLUSIVE."""
    if type(policy) is not PaperPerformancePolicy:
        raise ValueError("policy phải là PaperPerformancePolicy.")
    checked_at = _utc(as_of, "as_of")
    ordered = tuple(cohorts)
    if len(ordered) != 2 or any(type(item) is not PaperPerformanceCohort for item in ordered):
        raise ValueError("Cần đúng hai cohort liên tiếp.")
    older, newer = ordered
    duration = timedelta(days=policy.cohort_days)
    maturity = timedelta(days=policy.maturity_days)
    if (
        older.window_end - older.window_start != duration
        or newer.window_end - newer.window_start != duration
        or older.window_end != newer.window_start
        or newer.window_end != checked_at - maturity
    ):
        raise ValueError("Cohort phải liền kề, dài đúng policy và đã trưởng thành theo as_of.")

    assessed: list[PaperPerformanceCohort] = []
    overall_reasons: list[str] = []
    for index, cohort in enumerate(ordered):
        reasons: list[str] = []
        if cohort.covered_outcome_count < policy.minimum_outcomes:
            reasons.append("minimum_outcomes_not_met")
        if any(count < policy.minimum_class_support for _, count in cohort.class_support):
            reasons.append("minimum_class_support_not_met")
        if cohort.coverage < policy.minimum_coverage:
            reasons.append("minimum_mature_coverage_not_met")
        enough = not reasons
        brier_regression = cohort.brier_regression
        log_loss_regression = cohort.log_loss_regression
        degraded = (
            enough
            and brier_regression is not None
            and log_loss_regression is not None
            and brier_regression > policy.maximum_brier_regression
            and log_loss_regression > policy.maximum_log_loss_regression
        )
        status = (
            PaperPerformanceStatus.INCONCLUSIVE
            if not enough
            else PaperPerformanceStatus.DEGRADED
            if degraded
            else PaperPerformanceStatus.HEALTHY
        )
        assessed.append(replace(cohort, status=status, reasons=tuple(reasons)))
        overall_reasons.extend(f"cohort_{index}_{reason}" for reason in reasons)

    if overall_reasons:
        status = PaperPerformanceStatus.INCONCLUSIVE
    elif all(item.status is PaperPerformanceStatus.DEGRADED for item in assessed):
        status = PaperPerformanceStatus.DEGRADED
        overall_reasons.append("joint_metric_regression_in_both_cohorts")
    else:
        status = PaperPerformanceStatus.HEALTHY
    return PaperPerformanceAssessment(
        as_of=checked_at,
        policy_version=policy.version,
        policy_hash=policy.content_hash,
        status=status,
        cohorts=tuple(assessed),
        reasons=tuple(overall_reasons),
    )
