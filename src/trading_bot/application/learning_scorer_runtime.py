"""Chấm candidate bằng scorer v2 đã hiệu chuẩn, không có cổng lưu trữ hay execution."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypedDict
from uuid import UUID

from trading_bot.application.learning_cpu_trainer import LearningCpuArtifact
from trading_bot.application.learning_scorer_artifact import (
    SCORER_ARTIFACT_SCHEMA_VERSION,
    SCORER_CALIBRATOR_VERSION,
    LearningScorerArtifact,
    _validation_logits,
)
from trading_bot.application.learning_scorer_evaluation import ScorerTestEvaluationPolicy
from trading_bot.domain.learning.feature_vector import LearningFeatureVector
from trading_bot.domain.market.feature_snapshot import DataFreshness
from trading_bot.domain.strategy.contracts import FeatureQuality, SeriesQuality

_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_CLASS_LABELS: Final = (-1, 0, 1)


class _RuntimeResultMetadata(TypedDict):
    candidate_id: str
    workspace_id: UUID
    decision_snapshot_id: str
    vector_hash: str
    feature_schema_version: str
    feature_schema_hash: str
    strategy_release_id: str
    strategy_release_content_hash: str
    timeframe_snapshot_ids: tuple[tuple[str, str], ...]
    source_event_digests: tuple[tuple[str, str], ...]
    source_rights_hash: str | None
    scorer_artifact_hash: str | None
    base_artifact_hash: str | None
    policy_hash: str


class RuntimeScorerDecision(StrEnum):
    """Kết quả scorer cho candidate strategy hiện có; không phải lệnh giao dịch."""

    APPROVE = "APPROVE"
    VETO = "VETO"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True, slots=True)
class LearningScorerRuntimeRequest:
    """Candidate và trạng thái quyền hiện hành được kiểm tra trước khi chấm."""

    candidate_id: str
    vector: LearningFeatureVector
    source_rights_eligible: bool | None
    source_rights_hash: str | None
    is_out_of_distribution: bool

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not _SHA256.fullmatch(self.candidate_id):
            raise ValueError("candidate_id phải là SHA-256 hex chữ thường.")
        if type(self.vector) is not LearningFeatureVector:
            raise ValueError("vector phải là LearningFeatureVector canonical.")
        if (
            self.source_rights_eligible is not None
            and type(self.source_rights_eligible) is not bool
        ):
            raise ValueError("source_rights_eligible phải là bool hoặc None.")
        if self.source_rights_hash is not None and (
            not isinstance(self.source_rights_hash, str)
            or not _SHA256.fullmatch(self.source_rights_hash)
        ):
            raise ValueError("source_rights_hash phải là SHA-256 hex chữ thường hoặc None.")
        if type(self.is_out_of_distribution) is not bool:
            raise ValueError("is_out_of_distribution phải là bool.")


@dataclass(frozen=True, slots=True)
class LearningScorerRuntimeResult:
    """Kết quả metadata-only; không giữ vector, feature value, nhãn hoặc payload TEST."""

    candidate_id: str
    workspace_id: UUID
    decision_snapshot_id: str
    vector_hash: str
    feature_schema_version: str
    feature_schema_hash: str
    strategy_release_id: str
    strategy_release_content_hash: str
    timeframe_snapshot_ids: tuple[tuple[str, str], ...]
    source_event_digests: tuple[tuple[str, str], ...]
    source_rights_hash: str | None
    scorer_artifact_hash: str | None
    base_artifact_hash: str | None
    policy_hash: str
    decision: RuntimeScorerDecision
    class_probabilities: tuple[tuple[int, float], ...]
    probability: float | None
    confidence: float | None
    uncertainty: float | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if type(self.decision) is not RuntimeScorerDecision:
            raise ValueError("decision phải là RuntimeScorerDecision.")
        if self.decision is RuntimeScorerDecision.ABSTAIN:
            if (
                self.reason_code is None
                or not self.reason_code.strip()
                or self.class_probabilities
                or self.probability is not None
                or self.confidence is not None
                or self.uncertainty is not None
            ):
                raise ValueError("ABSTAIN cần reason và không được phát phân phối xác suất.")
        elif (
            (self.decision is RuntimeScorerDecision.APPROVE and self.reason_code is not None)
            or (self.decision is RuntimeScorerDecision.VETO and not self.reason_code)
            or tuple(label for label, _ in self.class_probabilities) != _CLASS_LABELS
            or self.probability != self.class_probabilities[2][1]
            or self.confidence is None
            or self.uncertainty is None
            or not math.isclose(
                math.fsum(value for _, value in self.class_probabilities), 1.0, abs_tol=1e-12
            )
            or not math.isclose(
                self.confidence,
                max(value for _, value in self.class_probabilities),
                abs_tol=1e-12,
            )
            or not math.isclose(self.uncertainty, 1.0 - self.confidence, abs_tol=1e-12)
        ):
            raise ValueError("APPROVE/VETO cần phân phối ba lớp hợp lệ.")
        for _, value in self.class_probabilities:
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError("Xác suất lớp phải hữu hạn trong [0, 1].")


def score_runtime_candidate(
    *,
    request: LearningScorerRuntimeRequest,
    base_artifact: LearningCpuArtifact,
    scorer_artifact: LearningScorerArtifact,
    policy: ScorerTestEvaluationPolicy,
) -> LearningScorerRuntimeResult:
    """Tạo phân phối ba lớp từ vector hiện tại; mọi gate không đạt trả ABSTAIN."""
    if type(request) is not LearningScorerRuntimeRequest:
        raise ValueError("request phải là LearningScorerRuntimeRequest.")
    if type(base_artifact) is not LearningCpuArtifact:
        raise ValueError("base_artifact phải là LearningCpuArtifact đã xác minh.")
    if type(scorer_artifact) is not LearningScorerArtifact:
        raise ValueError("scorer_artifact phải là LearningScorerArtifact đã xác minh.")
    if type(policy) is not ScorerTestEvaluationPolicy:
        raise ValueError("policy phải là ScorerTestEvaluationPolicy đã ghim.")

    vector = request.vector
    if _artifact_hash(scorer_artifact) is None or _artifact_hash(base_artifact) is None:
        return _abstain(request, scorer_artifact, base_artifact, policy, "SCORER_ARTIFACT_INVALID")
    if request.is_out_of_distribution:
        return _abstain(request, scorer_artifact, base_artifact, policy, "OUT_OF_DISTRIBUTION")
    if request.source_rights_eligible is None or request.source_rights_hash is None:
        return _abstain(
            request, scorer_artifact, base_artifact, policy, "CURRENT_RIGHTS_UNVERIFIED"
        )
    if request.source_rights_eligible is False:
        return _abstain(request, scorer_artifact, base_artifact, policy, "SOURCE_RIGHTS_REVOKED")
    if any(
        item.freshness is not DataFreshness.FRESH
        or item.series_quality is not SeriesQuality.READY
        or not item.source_identity_pinned
        or item.available_at is None
        or item.available_at > vector.decision_time
        or not item.warmup_by_feature
        or any(not complete for _, complete in item.warmup_by_feature)
        or item.data_coverage <= 0.0
        or not item.source_event_ids
        or not item.source_bar_ids
        or any(quality is not FeatureQuality.READY for _, quality in item.quality_by_feature)
        or any(
            feature.available_at != item.available_at
            for feature in vector.features
            if feature.timeframe == item.timeframe
        )
        for item in vector.timeframe_lineage
    ):
        return _abstain(
            request, scorer_artifact, base_artifact, policy, "STALE_OR_UNKNOWN_FEATURES"
        )
    if scorer_artifact.state != "ready_for_evaluation":
        return _abstain(
            request, scorer_artifact, base_artifact, policy, "SCORER_ARTIFACT_NOT_READY"
        )
    if (
        scorer_artifact.schema_version != SCORER_ARTIFACT_SCHEMA_VERSION
        or scorer_artifact.calibrator_method != "temperature_scaling"
        or scorer_artifact.calibrator_version != SCORER_CALIBRATOR_VERSION
        or scorer_artifact.temperature is None
        or isinstance(scorer_artifact.temperature, bool)
        or not isinstance(scorer_artifact.temperature, (int, float))
        or not math.isfinite(scorer_artifact.temperature)
        or scorer_artifact.temperature <= 0.0
    ):
        return _abstain(request, scorer_artifact, base_artifact, policy, "SCORER_ARTIFACT_INVALID")
    if (
        scorer_artifact.base_artifact_hash != _artifact_hash(base_artifact)
        or base_artifact.schema_version != "crypidian.learning.cpu-logistic.v1"
        or base_artifact.calibrated is not False
        or base_artifact.scorer_state != "abstain"
        or scorer_artifact.workspace_id != base_artifact.workspace_id
        or scorer_artifact.attempt_id != base_artifact.attempt_id
        or scorer_artifact.normalized_dataset_id != base_artifact.normalized_dataset_id
        or scorer_artifact.manifest_hash != base_artifact.manifest_hash
        or scorer_artifact.fold_plan_hash != base_artifact.fold_plan_hash
        or scorer_artifact.fold_id != base_artifact.fold_id
        or scorer_artifact.approval_hash != base_artifact.approval_hash
    ):
        return _abstain(
            request,
            scorer_artifact,
            base_artifact,
            policy,
            "SCORER_BASE_ARTIFACT_HASH_MISMATCH",
        )
    if scorer_artifact.workspace_id != str(vector.workspace_id):
        return _abstain(
            request, scorer_artifact, base_artifact, policy, "SCORER_WORKSPACE_MISMATCH"
        )
    if (
        scorer_artifact.vector_schema_version != vector.schema_version
        or scorer_artifact.vector_schema_hash != vector.feature_schema_hash
        or base_artifact.feature_schema_hash != vector.feature_schema_hash
    ):
        return _abstain(
            request, scorer_artifact, base_artifact, policy, "SCORER_VECTOR_SCHEMA_MISMATCH"
        )
    if (
        scorer_artifact.strategy_release_id != vector.strategy_release_id
        or scorer_artifact.strategy_release_content_hash != vector.strategy_release_content_hash
    ):
        return _abstain(
            request,
            scorer_artifact,
            base_artifact,
            policy,
            "SCORER_STRATEGY_RELEASE_MISMATCH",
        )
    if (
        not base_artifact.feature_names
        or tuple(feature.key for feature in vector.features) != base_artifact.feature_names
        or len(base_artifact.feature_means) != len(base_artifact.feature_names)
        or len(base_artifact.feature_scales) != len(base_artifact.feature_names)
        or tuple(model.label for model in base_artifact.models) != _CLASS_LABELS
        or any(
            len(model.coefficients) != len(base_artifact.feature_names)
            for model in base_artifact.models
        )
    ):
        return _abstain(
            request,
            scorer_artifact,
            base_artifact,
            policy,
            "SCORER_FEATURE_SCHEMA_MISMATCH",
        )
    if not _all_finite_numbers(
        (*base_artifact.feature_means, *base_artifact.feature_scales)
        + tuple(model.intercept for model in base_artifact.models)
        + tuple(value for model in base_artifact.models for value in model.coefficients)
    ):
        return _abstain(
            request,
            scorer_artifact,
            base_artifact,
            policy,
            "SCORER_FEATURE_STATISTICS_INVALID",
        )
    if any(scale <= 0.0 for scale in base_artifact.feature_scales):
        return _abstain(
            request,
            scorer_artifact,
            base_artifact,
            policy,
            "SCORER_FEATURE_STATISTICS_INVALID",
        )

    try:
        logits = _validation_logits(
            base_artifact,
            tuple((feature.key, feature.value) for feature in vector.features),
        )
        probabilities = _softmax(
            tuple(logit / float(scorer_artifact.temperature) for logit in logits)
        )
    except ArithmeticError, TypeError, ValueError:
        return _abstain(request, scorer_artifact, base_artifact, policy, "SCORER_INVALID_FEATURES")

    confidence = max(probabilities)
    if confidence < policy.minimum_confidence:
        return _abstain(request, scorer_artifact, base_artifact, policy, "LOW_CONFIDENCE")
    probability = probabilities[2]
    decision = (
        RuntimeScorerDecision.APPROVE
        if probability >= policy.approve_probability_threshold
        else RuntimeScorerDecision.VETO
    )
    result = _result_metadata(request, scorer_artifact, base_artifact, policy)
    return LearningScorerRuntimeResult(
        **result,
        decision=decision,
        class_probabilities=tuple(zip(_CLASS_LABELS, probabilities, strict=True)),
        probability=probability,
        confidence=confidence,
        uncertainty=1.0 - confidence,
        reason_code=(
            None
            if decision is RuntimeScorerDecision.APPROVE
            else "POSITIVE_PROBABILITY_BELOW_THRESHOLD"
        ),
    )


def _abstain(
    request: LearningScorerRuntimeRequest,
    scorer_artifact: LearningScorerArtifact,
    base_artifact: LearningCpuArtifact,
    policy: ScorerTestEvaluationPolicy,
    reason_code: str,
) -> LearningScorerRuntimeResult:
    return LearningScorerRuntimeResult(
        **_result_metadata(request, scorer_artifact, base_artifact, policy),
        decision=RuntimeScorerDecision.ABSTAIN,
        class_probabilities=(),
        probability=None,
        confidence=None,
        uncertainty=None,
        reason_code=reason_code,
    )


def _result_metadata(
    request: LearningScorerRuntimeRequest,
    scorer_artifact: LearningScorerArtifact,
    base_artifact: LearningCpuArtifact,
    policy: ScorerTestEvaluationPolicy,
) -> _RuntimeResultMetadata:
    vector = request.vector
    return {
        "candidate_id": request.candidate_id,
        "workspace_id": vector.workspace_id,
        "decision_snapshot_id": vector.snapshot_id,
        "vector_hash": vector.vector_hash,
        "feature_schema_version": vector.schema_version,
        "feature_schema_hash": vector.feature_schema_hash,
        "strategy_release_id": vector.strategy_release_id,
        "strategy_release_content_hash": vector.strategy_release_content_hash,
        "timeframe_snapshot_ids": tuple(
            (lineage.timeframe, lineage.feature_snapshot_id) for lineage in vector.timeframe_lineage
        ),
        "source_event_digests": tuple(
            (lineage.timeframe, lineage.source_event_digest) for lineage in vector.timeframe_lineage
        ),
        "source_rights_hash": request.source_rights_hash,
        "scorer_artifact_hash": _artifact_hash(scorer_artifact),
        "base_artifact_hash": _artifact_hash(base_artifact),
        "policy_hash": policy.policy_hash,
    }


def _softmax(logits: tuple[float, ...]) -> tuple[float, float, float]:
    if len(logits) != len(_CLASS_LABELS) or any(not math.isfinite(value) for value in logits):
        raise ValueError("Scorer logits phải là ba số hữu hạn.")
    maximum = max(logits)
    exponentials = tuple(math.exp(value - maximum) for value in logits)
    total = math.fsum(exponentials)
    if not math.isfinite(total) or total <= 0.0:
        raise ValueError("Scorer logits không tạo được phân phối hữu hạn.")
    return tuple(value / total for value in exponentials)  # type: ignore[return-value]


def _all_finite_numbers(values: tuple[object, ...]) -> bool:
    return all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in values
    )


def _artifact_hash(artifact: LearningCpuArtifact | LearningScorerArtifact) -> str | None:
    try:
        value = artifact.artifact_hash
    except TypeError, ValueError, OverflowError:
        return None
    return value if _SHA256.fullmatch(value) else None


__all__ = [
    "LearningScorerRuntimeRequest",
    "LearningScorerRuntimeResult",
    "RuntimeScorerDecision",
    "score_runtime_candidate",
]
