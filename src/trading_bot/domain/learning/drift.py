"""Hợp đồng đánh giá chất lượng dữ liệu và đề xuất huấn luyện lại."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID

from trading_bot.domain.learning.scoring import ScoreDecision, ScoreReason

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,119}\Z")
_METRIC_QUANTUM = Decimal("0.000000000001")


class DriftAssessmentStatus(StrEnum):
    """Kết luận có thể đưa ra cho một cohort quan sát."""

    DETECTED = "detected"
    NO_DRIFT = "no_drift"
    INCONCLUSIVE = "inconclusive"


class DriftReason(StrEnum):
    """Mã lý do ổn định, không chứa nội dung hay payload nguồn."""

    BASELINE_MISSING = "baseline_missing"
    COVERAGE_BELOW_THRESHOLD = "coverage_below_threshold"
    LOW_SAMPLE = "low_sample"
    PERFORMANCE_NOT_EVALUABLE = "performance_not_evaluable"
    QUALITY_BELOW_THRESHOLD = "quality_below_threshold"
    RIGHTS_UNVERIFIED = "rights_unverified"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"


@dataclass(frozen=True, slots=True)
class DriftPolicy:
    """Ngưỡng có phiên bản do máy chủ chọn cho coverage và chất lượng dữ liệu."""

    policy_version: str
    minimum_observations: int
    minimum_snapshot_coverage: float
    minimum_quality_rate: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy_version, str)
            or _VERSION.fullmatch(self.policy_version) is None
        ):
            raise ValueError("policy_version không hợp lệ.")
        if type(self.minimum_observations) is not int or self.minimum_observations < 1:
            raise ValueError("minimum_observations phải là số nguyên dương.")
        for name in ("minimum_snapshot_coverage", "minimum_quality_rate"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} phải là số hữu hạn trong [0, 1].")


@dataclass(frozen=True, slots=True)
class DriftObservation:
    """Projection metadata đã được repository xác minh từ observation ledger."""

    observation_id: UUID
    workspace_id: UUID
    challenger_registry_id: UUID
    challenger_revision: int
    observed_at: datetime
    snapshot_hash: str
    snapshot_coverage: float
    snapshot_quality_ok: bool
    baseline_available: bool
    schema_compatible: bool
    rights_eligible: bool
    challenger_decision: ScoreDecision
    challenger_reason: ScoreReason | None

    def __post_init__(self) -> None:
        for name in ("observation_id", "workspace_id", "challenger_registry_id"):
            value = getattr(self, name)
            if type(value) is not UUID or value.int == 0:
                raise ValueError(f"{name} phải là UUID khác zero.")
        if type(self.challenger_revision) is not int or self.challenger_revision < 1:
            raise ValueError("challenger_revision phải là số nguyên dương.")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if not isinstance(self.snapshot_hash, str) or _SHA256.fullmatch(self.snapshot_hash) is None:
            raise ValueError("snapshot_hash phải là SHA-256 hex chữ thường.")
        if (
            isinstance(self.snapshot_coverage, bool)
            or not isinstance(self.snapshot_coverage, (int, float))
            or not math.isfinite(self.snapshot_coverage)
            or not 0.0 <= self.snapshot_coverage <= 1.0
        ):
            raise ValueError("snapshot_coverage phải là số hữu hạn trong [0, 1].")
        for name in (
            "snapshot_quality_ok",
            "baseline_available",
            "schema_compatible",
            "rights_eligible",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} phải là boolean.")
        if type(self.challenger_decision) is not ScoreDecision:
            raise ValueError("challenger_decision phải là ScoreDecision.")
        if self.challenger_reason is not None and type(self.challenger_reason) is not ScoreReason:
            raise ValueError("challenger_reason phải là ScoreReason hoặc None.")
        if self.challenger_decision is ScoreDecision.ABSTAIN:
            if self.challenger_reason is None:
                raise ValueError("Challenger ABSTAIN cần reason code.")
        elif self.challenger_reason is not None:
            raise ValueError("Challenger không ABSTAIN không được gắn abstention reason.")


@dataclass(frozen=True, slots=True)
class LearningDriftAssessment:
    """Assessment bất biến; performance luôn inconclusive ở contract hiện tại."""

    assessment_id: UUID
    workspace_id: UUID
    policy_version: str
    cohort_registry_id: UUID | None
    cohort_revision: int | None
    window_start: datetime | None
    window_end: datetime | None
    member_ids: tuple[UUID, ...]
    membership_digest: str
    member_count: int
    eligible_count: int
    snapshot_coverage_rate: float | None
    quality_rate: float | None
    abstention_rate: float | None
    data_quality_status: DriftAssessmentStatus
    performance_status: DriftAssessmentStatus
    reason_codes: tuple[DriftReason, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("assessment_id", "workspace_id"):
            value = getattr(self, name)
            if type(value) is not UUID or value.int == 0:
                raise ValueError(f"{name} phải là UUID khác zero.")
        if (
            not isinstance(self.policy_version, str)
            or _VERSION.fullmatch(self.policy_version) is None
        ):
            raise ValueError("policy_version không hợp lệ.")
        if self.member_count == 0:
            if self.cohort_registry_id is not None or self.cohort_revision is not None:
                raise ValueError("Cohort rỗng không được ghim registry hoặc revision.")
            if any(
                value is not None
                for value in (
                    self.snapshot_coverage_rate,
                    self.quality_rate,
                    self.abstention_rate,
                )
            ):
                raise ValueError("Cohort rỗng phải giữ mọi metric là None.")
            if (
                self.data_quality_status is not DriftAssessmentStatus.INCONCLUSIVE
                or DriftReason.LOW_SAMPLE not in self.reason_codes
            ):
                raise ValueError("Cohort rỗng phải inconclusive và có reason LOW_SAMPLE.")
        else:
            if type(self.cohort_registry_id) is not UUID or self.cohort_registry_id.int == 0:
                raise ValueError("Cohort có member cần challenger registry ID hợp lệ.")
            if type(self.cohort_revision) is not int or self.cohort_revision < 1:
                raise ValueError("Cohort có member cần challenger revision dương.")
        if type(self.member_ids) is not tuple or any(
            type(item) is not UUID for item in self.member_ids
        ):
            raise ValueError("member_ids phải là tuple UUID.")
        if self.member_ids != tuple(sorted(set(self.member_ids), key=str)):
            raise ValueError("member_ids phải duy nhất và canonical.")
        if (
            not isinstance(self.membership_digest, str)
            or _SHA256.fullmatch(self.membership_digest) is None
        ):
            raise ValueError("membership_digest phải là SHA-256 hex chữ thường.")
        if self.membership_digest != _digest(_canonical([str(item) for item in self.member_ids])):
            raise ValueError("membership_digest không khớp member_ids.")
        if type(self.member_count) is not int or self.member_count != len(self.member_ids):
            raise ValueError("member_count không khớp membership.")
        if (
            type(self.eligible_count) is not int
            or not 0 <= self.eligible_count <= self.member_count
        ):
            raise ValueError("eligible_count không hợp lệ.")
        for name in ("snapshot_coverage_rate", "quality_rate", "abstention_rate"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} phải là số hữu hạn trong [0, 1] hoặc None.")
        if type(self.data_quality_status) is not DriftAssessmentStatus:
            raise ValueError("data_quality_status không hợp lệ.")
        if self.performance_status is not DriftAssessmentStatus.INCONCLUSIVE:
            raise ValueError("Performance drift chưa có calibration/outcome attribution.")
        if type(self.reason_codes) is not tuple or any(
            type(item) is not DriftReason for item in self.reason_codes
        ):
            raise ValueError("reason_codes phải là tuple DriftReason.")
        if self.reason_codes != tuple(sorted(set(self.reason_codes), key=lambda item: item.value)):
            raise ValueError("reason_codes phải duy nhất và canonical.")
        if self.window_start is None or self.window_end is None:
            if (
                self.window_start is not None
                or self.window_end is not None
                or self.member_count != 0
            ):
                raise ValueError("Window chỉ có thể None khi assessment không có member.")
        else:
            start = _utc(self.window_start, "window_start")
            end = _utc(self.window_end, "window_end")
            if start > end:
                raise ValueError("window_start không được sau window_end.")
            object.__setattr__(self, "window_start", start)
            object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))

    @property
    def canonical_json(self) -> str:
        """Bytes chuẩn metadata dùng để lưu hash bất biến."""
        return _canonical(
            {
                "assessment_id": str(self.assessment_id),
                "abstention_rate": self.abstention_rate,
                "created_at": self.created_at.isoformat(),
                "cohort_registry_id": (
                    str(self.cohort_registry_id) if self.cohort_registry_id is not None else None
                ),
                "cohort_revision": self.cohort_revision,
                "data_quality_status": self.data_quality_status.value,
                "eligible_count": self.eligible_count,
                "member_count": self.member_count,
                "member_ids": [str(item) for item in self.member_ids],
                "membership_digest": self.membership_digest,
                "performance_status": self.performance_status.value,
                "policy_version": self.policy_version,
                "quality_rate": self.quality_rate,
                "reason_codes": [item.value for item in self.reason_codes],
                "snapshot_coverage_rate": self.snapshot_coverage_rate,
                "window_end": self.window_end.isoformat() if self.window_end else None,
                "window_start": self.window_start.isoformat() if self.window_start else None,
                "workspace_id": str(self.workspace_id),
            }
        )

    @property
    def content_hash(self) -> str:
        """SHA-256 của canonical metadata."""
        return _digest(self.canonical_json)


@dataclass(frozen=True, slots=True)
class LearningRetrainProposal:
    """Đề xuất review metadata-only, không phải training job hay promotion."""

    proposal_id: UUID
    workspace_id: UUID
    assessment_id: UUID
    assessment_hash: str
    policy_version: str
    membership_digest: str
    idempotency_key: str
    reason_codes: tuple[DriftReason, ...]
    created_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        for name in ("proposal_id", "workspace_id", "assessment_id"):
            value = getattr(self, name)
            if type(value) is not UUID or value.int == 0:
                raise ValueError(f"{name} phải là UUID khác zero.")
        if (
            not isinstance(self.assessment_hash, str)
            or _SHA256.fullmatch(self.assessment_hash) is None
        ):
            raise ValueError("assessment_hash phải là SHA-256 hex chữ thường.")
        if (
            not isinstance(self.membership_digest, str)
            or _SHA256.fullmatch(self.membership_digest) is None
        ):
            raise ValueError("membership_digest phải là SHA-256 hex chữ thường.")
        if (
            not isinstance(self.idempotency_key, str)
            or _SHA256.fullmatch(self.idempotency_key) is None
        ):
            raise ValueError("idempotency_key phải là SHA-256 hex chữ thường.")
        if (
            not isinstance(self.policy_version, str)
            or _VERSION.fullmatch(self.policy_version) is None
        ):
            raise ValueError("policy_version không hợp lệ.")
        if type(self.reason_codes) is not tuple or not self.reason_codes:
            raise ValueError("Proposal cần ít nhất một reason dữ liệu.")
        allowed = {DriftReason.COVERAGE_BELOW_THRESHOLD, DriftReason.QUALITY_BELOW_THRESHOLD}
        if any(type(item) is not DriftReason or item not in allowed for item in self.reason_codes):
            raise ValueError("Proposal chỉ được nêu lý do chất lượng/coverage dữ liệu.")
        if self.reason_codes != tuple(sorted(set(self.reason_codes), key=lambda item: item.value)):
            raise ValueError("reason_codes phải duy nhất và canonical.")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if not isinstance(self.content_hash, str) or _SHA256.fullmatch(self.content_hash) is None:
            raise ValueError("content_hash phải là SHA-256 hex chữ thường.")
        if self.content_hash != _digest(self.canonical_json):
            raise ValueError("content_hash không khớp canonical proposal.")

    @property
    def canonical_json(self) -> str:
        """Bytes canonical chỉ gồm metadata assessment và lý do dữ liệu."""
        return _canonical(
            {
                "assessment_hash": self.assessment_hash,
                "assessment_id": str(self.assessment_id),
                "created_at": self.created_at.isoformat(),
                "idempotency_key": self.idempotency_key,
                "membership_digest": self.membership_digest,
                "policy_version": self.policy_version,
                "proposal_id": str(self.proposal_id),
                "reason_codes": [item.value for item in self.reason_codes],
                "workspace_id": str(self.workspace_id),
            }
        )


def assess_learning_drift(
    *,
    workspace_id: UUID,
    assessment_id: UUID,
    policy: DriftPolicy,
    observations: tuple[DriftObservation, ...],
    created_at: datetime,
) -> LearningDriftAssessment:
    """Đo coverage/chất lượng từ metadata đã xác minh; không suy luận PnL."""
    if type(workspace_id) is not UUID or workspace_id.int == 0:
        raise ValueError("workspace_id phải là UUID khác zero.")
    if type(assessment_id) is not UUID or assessment_id.int == 0:
        raise ValueError("assessment_id phải là UUID khác zero.")
    if type(policy) is not DriftPolicy:
        raise ValueError("policy phải là DriftPolicy do server chọn.")
    if type(observations) is not tuple or any(
        type(item) is not DriftObservation for item in observations
    ):
        raise ValueError("observations phải là tuple[DriftObservation, ...].")
    if any(item.workspace_id != workspace_id for item in observations):
        raise PermissionError("Observation nằm ngoài workspace assessment.")
    if len({item.challenger_registry_id for item in observations}) > 1:
        raise ValueError("Assessment chỉ được chứa cùng challenger registry.")
    if len({item.challenger_revision for item in observations}) > 1:
        raise ValueError("Assessment chỉ được chứa cùng challenger revision.")
    ids = tuple(item.observation_id for item in observations)
    if len(set(ids)) != len(ids):
        raise ValueError("Observation ID bị trùng.")

    ordered = tuple(sorted(observations, key=lambda item: str(item.observation_id)))
    member_ids = tuple(item.observation_id for item in ordered)
    membership_digest = _digest(_canonical([str(item) for item in member_ids]))
    reasons: set[DriftReason] = {DriftReason.PERFORMANCE_NOT_EVALUABLE}
    eligible = tuple(
        item
        for item in ordered
        if item.baseline_available and item.schema_compatible and item.rights_eligible
    )

    if len(ordered) < policy.minimum_observations:
        reasons.add(DriftReason.LOW_SAMPLE)
    if any(not item.baseline_available for item in ordered):
        reasons.add(DriftReason.BASELINE_MISSING)
    if any(not item.schema_compatible for item in ordered):
        reasons.add(DriftReason.SCHEMA_INCOMPATIBLE)
    if any(not item.rights_eligible for item in ordered):
        reasons.add(DriftReason.RIGHTS_UNVERIFIED)

    evidence_complete = not reasons.intersection(
        {
            DriftReason.LOW_SAMPLE,
            DriftReason.BASELINE_MISSING,
            DriftReason.SCHEMA_INCOMPATIBLE,
            DriftReason.RIGHTS_UNVERIFIED,
        }
    )
    if evidence_complete:
        coverage_rate = _rounded_rate(
            sum((Decimal(str(item.snapshot_coverage)) for item in eligible), Decimal(0)),
            len(eligible),
        )
        quality_rate = _rounded_rate(
            sum((Decimal(int(item.snapshot_quality_ok)) for item in eligible), Decimal(0)),
            len(eligible),
        )
        if coverage_rate < policy.minimum_snapshot_coverage:
            reasons.add(DriftReason.COVERAGE_BELOW_THRESHOLD)
        if quality_rate < policy.minimum_quality_rate:
            reasons.add(DriftReason.QUALITY_BELOW_THRESHOLD)
        data_status = (
            DriftAssessmentStatus.DETECTED
            if reasons.intersection(
                {DriftReason.COVERAGE_BELOW_THRESHOLD, DriftReason.QUALITY_BELOW_THRESHOLD}
            )
            else DriftAssessmentStatus.NO_DRIFT
        )
    else:
        coverage_rate = None
        quality_rate = None
        data_status = DriftAssessmentStatus.INCONCLUSIVE

    abstention_rate = (
        _rounded_rate(
            sum(
                (
                    Decimal(int(item.challenger_decision is ScoreDecision.ABSTAIN))
                    for item in ordered
                ),
                Decimal(0),
            ),
            len(ordered),
        )
        if ordered
        else None
    )
    return LearningDriftAssessment(
        assessment_id=assessment_id,
        workspace_id=workspace_id,
        policy_version=policy.policy_version,
        cohort_registry_id=(ordered[0].challenger_registry_id if ordered else None),
        cohort_revision=(ordered[0].challenger_revision if ordered else None),
        window_start=min((item.observed_at for item in ordered), default=None),
        window_end=max((item.observed_at for item in ordered), default=None),
        member_ids=member_ids,
        membership_digest=membership_digest,
        member_count=len(ordered),
        eligible_count=len(eligible),
        snapshot_coverage_rate=coverage_rate,
        quality_rate=quality_rate,
        abstention_rate=abstention_rate,
        data_quality_status=data_status,
        performance_status=DriftAssessmentStatus.INCONCLUSIVE,
        reason_codes=tuple(sorted(reasons, key=lambda item: item.value)),
        created_at=created_at,
    )


def create_learning_retrain_proposal(
    assessment: LearningDriftAssessment,
    *,
    proposal_id: UUID,
    created_at: datetime,
) -> LearningRetrainProposal:
    """Tạo proposal chỉ sau data-quality drift; không enqueue hoặc promote."""
    if type(assessment) is not LearningDriftAssessment:
        raise ValueError("assessment phải là LearningDriftAssessment.")
    if assessment.data_quality_status is not DriftAssessmentStatus.DETECTED:
        raise ValueError("Chỉ assessment DETECTED về dữ liệu mới tạo retrain proposal.")
    reasons = tuple(
        item
        for item in assessment.reason_codes
        if item in {DriftReason.COVERAGE_BELOW_THRESHOLD, DriftReason.QUALITY_BELOW_THRESHOLD}
    )
    if not reasons:
        raise ValueError("Assessment thiếu reason drift dữ liệu.")
    idempotency_key = _digest(
        _canonical(
            {
                "assessment_hash": assessment.content_hash,
                "membership_digest": assessment.membership_digest,
                "policy_version": assessment.policy_version,
                "workspace_id": str(assessment.workspace_id),
            }
        )
    )
    partial = {
        "assessment_hash": assessment.content_hash,
        "assessment_id": str(assessment.assessment_id),
        "created_at": _utc(created_at, "created_at").isoformat(),
        "idempotency_key": idempotency_key,
        "membership_digest": assessment.membership_digest,
        "policy_version": assessment.policy_version,
        "proposal_id": str(proposal_id),
        "reason_codes": [item.value for item in reasons],
        "workspace_id": str(assessment.workspace_id),
    }
    content_hash = _digest(_canonical(partial))
    return LearningRetrainProposal(
        proposal_id=proposal_id,
        workspace_id=assessment.workspace_id,
        assessment_id=assessment.assessment_id,
        assessment_hash=assessment.content_hash,
        policy_version=assessment.policy_version,
        membership_digest=assessment.membership_digest,
        idempotency_key=idempotency_key,
        reason_codes=reasons,
        created_at=created_at,
        content_hash=content_hash,
    )


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} phải có timezone.")
    return value.astimezone(UTC)


def _rounded_rate(total: Decimal, denominator: int) -> float:
    """Tính tỷ lệ theo 12 chữ số thập phân giống PostgreSQL NUMERIC."""
    if type(denominator) is not int or denominator < 1:
        raise ValueError("denominator phải là số nguyên dương.")
    return float((total / Decimal(denominator)).quantize(_METRIC_QUANTUM, rounding=ROUND_HALF_UP))


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "DriftAssessmentStatus",
    "DriftObservation",
    "DriftPolicy",
    "DriftReason",
    "LearningDriftAssessment",
    "LearningRetrainProposal",
    "assess_learning_drift",
    "create_learning_retrain_proposal",
]
