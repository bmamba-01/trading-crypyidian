"""Hợp đồng chấm điểm AI có kiểm tra đầu vào và abstain fail-closed."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from trading_bot.domain.market.feature_snapshot import (
    DataFreshness,
    FeatureSnapshot,
    MarketRegime,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_FEATURE_NAME = re.compile(r"[a-z][a-z0-9_]*")


class ScoreDecision(StrEnum):
    """Quyết định của scorer; không phải lệnh hoặc quyền vượt risk gate."""

    APPROVE = "approve"
    VETO = "veto"
    ABSTAIN = "abstain"


class ScoreReason(StrEnum):
    """Mã lý do ổn định để audit và hiển thị."""

    FUTURE_SNAPSHOT = "FUTURE_SNAPSHOT"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    UNUSABLE_SNAPSHOT = "UNUSABLE_SNAPSHOT"
    FEATURE_SCHEMA_MISMATCH = "FEATURE_SCHEMA_MISMATCH"
    LOW_COVERAGE = "LOW_COVERAGE"
    UNKNOWN_REGIME = "UNKNOWN_REGIME"
    MISSING_FEATURE = "MISSING_FEATURE"
    FEATURE_WARMUP_INCOMPLETE = "FEATURE_WARMUP_INCOMPLETE"
    OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
    SOURCE_RIGHTS_INELIGIBLE = "SOURCE_RIGHTS_INELIGIBLE"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_FAILED = "MODEL_FAILED"
    INVALID_RESPONSE_SCHEMA = "INVALID_RESPONSE_SCHEMA"
    MODEL_VERSION_MISMATCH = "MODEL_VERSION_MISMATCH"
    UNCALIBRATED_SCORE = "UNCALIBRATED_SCORE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    MODEL_LATENCY_EXCEEDED = "MODEL_LATENCY_EXCEEDED"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"


class ModelInvocationStatus(StrEnum):
    """Trạng thái do application truyền lại sau lần gọi model bên ngoài."""

    SUCCEEDED = "succeeded"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """Ngưỡng và version được duyệt cho một hợp đồng chấm điểm."""

    policy_version: str
    model_id: str
    model_version: str
    response_schema_version: str
    feature_schema_hash: str
    required_features: tuple[str, ...]
    max_snapshot_age: timedelta
    max_model_latency: timedelta
    minimum_coverage: float
    minimum_confidence: float
    approve_threshold: float

    def __post_init__(self) -> None:
        for name in (
            "policy_version",
            "model_id",
            "model_version",
            "response_schema_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} không được để trống.")
        if not isinstance(self.feature_schema_hash, str) or not _SHA256.fullmatch(
            self.feature_schema_hash
        ):
            raise ValueError("feature_schema_hash phải là SHA-256 hex chữ thường.")
        if not isinstance(self.max_snapshot_age, timedelta) or self.max_snapshot_age <= timedelta(
            0
        ):
            raise ValueError("max_snapshot_age phải lớn hơn 0.")
        if not isinstance(self.max_model_latency, timedelta) or self.max_model_latency <= timedelta(
            0
        ):
            raise ValueError("max_model_latency phải lớn hơn 0.")
        for name in ("minimum_coverage", "minimum_confidence", "approve_threshold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} phải là số hữu hạn trong [0, 1].")
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} phải là số hữu hạn trong [0, 1].")
        if not isinstance(self.required_features, tuple) or any(
            not isinstance(feature, str) or not _FEATURE_NAME.fullmatch(feature)
            for feature in self.required_features
        ):
            raise ValueError("required_features phải chứa tên feature hợp lệ.")
        features = tuple(sorted(set(self.required_features)))
        if not features:
            raise ValueError("required_features phải chứa tên feature hợp lệ.")
        object.__setattr__(self, "required_features", features)


@dataclass(frozen=True, slots=True)
class ScoreRequest:
    """Yêu cầu gắn một candidate với snapshot đã có và trạng thái gate rõ ràng."""

    candidate_id: str
    strategy_id: str
    strategy_version: str
    strategy_hash: str
    snapshot: FeatureSnapshot
    context_refs: tuple[str, ...]
    context_hash: str
    requested_at: datetime
    source_dependent: bool
    source_rights_eligible: bool | None
    source_rights_hash: str | None
    is_out_of_distribution: bool

    def __post_init__(self) -> None:
        for name in ("candidate_id", "strategy_id", "strategy_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} không được để trống.")
        if not isinstance(self.strategy_hash, str) or not _SHA256.fullmatch(self.strategy_hash):
            raise ValueError("strategy_hash phải là SHA-256 hex chữ thường.")
        refs = _canonical_refs(self.context_refs, "context_refs")
        if refs != self.context_refs:
            raise ValueError("context_refs phải duy nhất và canonical.")
        if not isinstance(self.context_hash, str) or not _SHA256.fullmatch(self.context_hash):
            raise ValueError("context_hash phải là SHA-256 hex chữ thường.")
        if self.context_hash != _refs_hash(refs):
            raise ValueError("context_hash không khớp context_refs.")
        if not isinstance(self.snapshot, FeatureSnapshot):
            raise ValueError("snapshot phải là FeatureSnapshot đã đóng băng.")
        object.__setattr__(self, "requested_at", _utc(self.requested_at, "requested_at"))
        if type(self.source_dependent) is not bool:
            raise ValueError("source_dependent phải là boolean.")
        if type(self.is_out_of_distribution) is not bool:
            raise ValueError("is_out_of_distribution phải là boolean.")
        if self.source_dependent:
            if type(self.source_rights_eligible) is not bool:
                raise ValueError("source_rights_eligible phải là boolean khi có phụ thuộc nguồn.")
            if self.source_rights_hash is not None and (
                not isinstance(self.source_rights_hash, str)
                or not _SHA256.fullmatch(self.source_rights_hash)
            ):
                raise ValueError("source_rights_hash phải là SHA-256 hex chữ thường.")
            if self.source_rights_eligible and self.source_rights_hash is None:
                raise ValueError("source_rights_hash bắt buộc khi quyền nguồn đã xác minh.")
        elif self.source_rights_eligible is not None or self.source_rights_hash is not None:
            raise ValueError("Quyền nguồn không áp dụng khi candidate không phụ thuộc nguồn.")


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Kết quả scorer có lineage; không mang tham chiếu tới execution port."""

    candidate_id: str
    strategy_id: str
    strategy_version: str
    strategy_hash: str
    context_refs: tuple[str, ...]
    context_hash: str
    snapshot_id: str
    snapshot_hash: str
    source_rights_hash: str | None
    policy_version: str
    decision: ScoreDecision
    probability: float | None
    confidence: float | None
    uncertainty: float | None
    regime: MarketRegime
    evidence_refs: tuple[str, ...]
    model_id: str
    model_version: str
    model_latency: timedelta
    reason_codes: tuple[ScoreReason, ...]

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "strategy_id",
            "strategy_version",
            "snapshot_id",
            "policy_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} không được để trống.")
        if not isinstance(self.snapshot_hash, str) or not _SHA256.fullmatch(self.snapshot_hash):
            raise ValueError("snapshot_hash phải là SHA-256 hex chữ thường.")
        if not isinstance(self.strategy_hash, str) or not _SHA256.fullmatch(self.strategy_hash):
            raise ValueError("strategy_hash phải là SHA-256 hex chữ thường.")
        refs = _canonical_refs(self.context_refs, "context_refs")
        if refs != self.context_refs:
            raise ValueError("context_refs phải duy nhất và canonical.")
        if not isinstance(self.context_hash, str) or not _SHA256.fullmatch(self.context_hash):
            raise ValueError("context_hash phải là SHA-256 hex chữ thường.")
        if self.context_hash != _refs_hash(refs):
            raise ValueError("context_hash không khớp context_refs.")
        if self.source_rights_hash is not None and (
            not isinstance(self.source_rights_hash, str)
            or not _SHA256.fullmatch(self.source_rights_hash)
        ):
            raise ValueError("source_rights_hash phải là SHA-256 hex chữ thường.")
        if type(self.decision) is not ScoreDecision:
            raise ValueError("decision phải là ScoreDecision tường minh.")
        if type(self.reason_codes) is not tuple or any(
            type(reason) is not ScoreReason for reason in self.reason_codes
        ):
            raise ValueError("reason_codes phải là tuple[ScoreReason, ...].")
        if type(self.regime) is not MarketRegime:
            raise ValueError("regime phải là MarketRegime tường minh.")
        if type(self.evidence_refs) is not tuple or any(
            not isinstance(ref, str) or not ref.strip() for ref in self.evidence_refs
        ):
            raise ValueError("evidence_refs phải là tuple chuỗi không rỗng.")
        if self.evidence_refs != tuple(sorted(set(self.evidence_refs))):
            raise ValueError("evidence_refs phải duy nhất và canonical.")
        for name in ("model_id", "model_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Kết quả cần model identity hợp lệ.")
        if not isinstance(self.model_latency, timedelta) or self.model_latency < timedelta(0):
            raise ValueError("model_latency phải là thời lượng không âm.")
        if self.decision is ScoreDecision.ABSTAIN:
            if (
                not self.reason_codes
                or self.probability is not None
                or self.confidence is not None
                or self.uncertainty is not None
            ):
                raise ValueError("ABSTAIN cần reason và không được phát score.")
        elif (
            self.reason_codes
            or self.probability is None
            or self.confidence is None
            or self.uncertainty is None
        ):
            raise ValueError("APPROVE/VETO cần score/uncertainty hợp lệ và không có reason.")
        for name in ("probability", "confidence", "uncertainty"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} phải là số hữu hạn trong [0, 1].")


def score_candidate(
    request: ScoreRequest,
    policy: ScoringPolicy,
    *,
    invocation_status: ModelInvocationStatus,
    model_response: object,
    model_latency: timedelta,
) -> ScoreResult:
    """Kiểm tra policy/input trước; response lỗi hoặc mơ hồ luôn trở thành ABSTAIN."""
    if type(request) is not ScoreRequest or type(policy) is not ScoringPolicy:
        raise TypeError("request và policy phải đúng kiểu contract.")
    if type(invocation_status) is not ModelInvocationStatus:
        raise ValueError("invocation_status phải là enum tường minh.")
    if not isinstance(model_latency, timedelta) or model_latency < timedelta(0):
        raise ValueError("model_latency phải là thời lượng không âm.")

    snapshot = request.snapshot
    snapshot_as_of = _utc(snapshot.as_of, "snapshot.as_of")
    snapshot_computed_at = _utc(snapshot.computed_at, "snapshot.computed_at")
    if snapshot_as_of > request.requested_at or snapshot_computed_at > request.requested_at:
        return _abstain(request, policy, ScoreReason.FUTURE_SNAPSHOT)
    if snapshot.schema_hash != policy.feature_schema_hash:
        return _abstain(request, policy, ScoreReason.FEATURE_SCHEMA_MISMATCH)
    if not snapshot.is_usable or snapshot.quality_flags:
        return _abstain(request, policy, ScoreReason.UNUSABLE_SNAPSHOT)
    if snapshot.data_freshness is not DataFreshness.FRESH:
        return _abstain(request, policy, ScoreReason.STALE_SNAPSHOT)
    if request.requested_at - snapshot_as_of > policy.max_snapshot_age:
        return _abstain(request, policy, ScoreReason.STALE_SNAPSHOT)
    if min(snapshot.data_coverage, snapshot.regime.coverage) < policy.minimum_coverage:
        return _abstain(request, policy, ScoreReason.LOW_COVERAGE)
    if snapshot.regime.label is MarketRegime.UNKNOWN:
        return _abstain(request, policy, ScoreReason.UNKNOWN_REGIME)
    if any(snapshot.features.get(name) is None for name in policy.required_features):
        return _abstain(request, policy, ScoreReason.MISSING_FEATURE)
    if any(snapshot.warmup.get(name) is not True for name in policy.required_features):
        return _abstain(request, policy, ScoreReason.FEATURE_WARMUP_INCOMPLETE)
    evidence_refs = tuple(sorted({*snapshot.input_bar_ids, *snapshot.input_event_ids}))
    if not evidence_refs:
        return _abstain(request, policy, ScoreReason.MISSING_EVIDENCE)
    if request.is_out_of_distribution:
        return _abstain(request, policy, ScoreReason.OUT_OF_DISTRIBUTION)
    if request.source_dependent and not request.source_rights_eligible:
        return _abstain(request, policy, ScoreReason.SOURCE_RIGHTS_INELIGIBLE)

    status_reason = {
        ModelInvocationStatus.TIMEOUT: ScoreReason.MODEL_TIMEOUT,
        ModelInvocationStatus.UNAVAILABLE: ScoreReason.MODEL_UNAVAILABLE,
        ModelInvocationStatus.FAILED: ScoreReason.MODEL_FAILED,
    }.get(invocation_status)
    if status_reason is not None:
        return _abstain(request, policy, status_reason, model_latency=model_latency)
    if model_latency > policy.max_model_latency:
        return _abstain(
            request,
            policy,
            ScoreReason.MODEL_LATENCY_EXCEEDED,
            model_latency=model_latency,
        )
    parsed = _parse_response(model_response, policy)
    if isinstance(parsed, ScoreReason):
        return _abstain(request, policy, parsed, model_latency=model_latency)
    probability, confidence, uncertainty = parsed
    if confidence < policy.minimum_confidence:
        return _abstain(request, policy, ScoreReason.LOW_CONFIDENCE, model_latency=model_latency)
    decision = (
        ScoreDecision.APPROVE if probability >= policy.approve_threshold else ScoreDecision.VETO
    )
    return ScoreResult(
        candidate_id=request.candidate_id,
        strategy_id=request.strategy_id,
        strategy_version=request.strategy_version,
        strategy_hash=request.strategy_hash,
        context_refs=request.context_refs,
        context_hash=request.context_hash,
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.content_hash,
        source_rights_hash=request.source_rights_hash,
        policy_version=policy.policy_version,
        decision=decision,
        probability=probability,
        confidence=confidence,
        uncertainty=uncertainty,
        regime=snapshot.regime.label,
        evidence_refs=evidence_refs,
        model_id=policy.model_id,
        model_version=policy.model_version,
        model_latency=model_latency,
        reason_codes=(),
    )


def _parse_response(
    response: object, policy: ScoringPolicy
) -> tuple[float, float, float] | ScoreReason:
    required_keys = {
        "model_id",
        "model_version",
        "schema_version",
        "probability",
        "confidence",
        "uncertainty",
        "calibrated",
    }
    if not isinstance(response, Mapping) or set(response) != required_keys:
        return ScoreReason.INVALID_RESPONSE_SCHEMA
    if response["model_id"] != policy.model_id or response["model_version"] != policy.model_version:
        return ScoreReason.MODEL_VERSION_MISMATCH
    if response["schema_version"] != policy.response_schema_version:
        return ScoreReason.INVALID_RESPONSE_SCHEMA
    calibrated = response["calibrated"]
    if type(calibrated) is not bool:
        return ScoreReason.INVALID_RESPONSE_SCHEMA
    if not calibrated:
        return ScoreReason.UNCALIBRATED_SCORE
    probability = _bounded_number(response["probability"])
    confidence = _bounded_number(response["confidence"])
    uncertainty = _bounded_number(response["uncertainty"])
    if probability is None or confidence is None or uncertainty is None:
        return ScoreReason.INVALID_RESPONSE_SCHEMA
    return probability, confidence, uncertainty


def _bounded_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def _abstain(
    request: ScoreRequest,
    policy: ScoringPolicy,
    reason: ScoreReason,
    *,
    model_latency: timedelta = timedelta(0),
) -> ScoreResult:
    snapshot = request.snapshot
    evidence_refs = tuple(sorted({*snapshot.input_bar_ids, *snapshot.input_event_ids}))
    return ScoreResult(
        candidate_id=request.candidate_id,
        strategy_id=request.strategy_id,
        strategy_version=request.strategy_version,
        strategy_hash=request.strategy_hash,
        context_refs=request.context_refs,
        context_hash=request.context_hash,
        snapshot_id=request.snapshot.snapshot_id,
        snapshot_hash=request.snapshot.content_hash,
        source_rights_hash=request.source_rights_hash,
        policy_version=policy.policy_version,
        decision=ScoreDecision.ABSTAIN,
        probability=None,
        confidence=None,
        uncertainty=None,
        regime=snapshot.regime.label,
        evidence_refs=evidence_refs,
        model_id=policy.model_id,
        model_version=policy.model_version,
        model_latency=model_latency,
        reason_codes=(reason,),
    )


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _canonical_refs(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if type(values) is not tuple or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"{name} phải là tuple chuỗi không rỗng.")
    return tuple(sorted(set(values)))


def _refs_hash(values: tuple[str, ...]) -> str:
    canonical = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
