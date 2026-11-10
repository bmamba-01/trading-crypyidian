"""Hợp đồng metadata-only cho kết quả scorer runtime bất biến."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid5

from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentMode

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCORE_EVENT_NAMESPACE = UUID("7a2d0a17-8c41-4c56-9c44-0ea37f2d9432")
_DECISION_STATUSES = frozenset({"CANDIDATE", "ABSTAIN", "VETO"})
_PROBABILITY_LABELS = (-1, 0, 1)


class LearningScoreDecision(StrEnum):
    """Kết quả scorer, chỉ áp dụng cho candidate strategy đã tồn tại."""

    APPROVE = "APPROVE"
    VETO = "VETO"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True, slots=True)
class LearningScoreEvent:
    """Kết quả scorer đã ghim vào quyết định, không chứa vector hoặc feature values."""

    workspace_id: UUID
    bot_id: UUID
    bot_decision_id: UUID
    bot_decision_hash: str
    incumbent_decision_hash: str
    incumbent_status: str
    final_status: str
    candidate_id: str
    candidate_hash: str
    decision_snapshot_id: str
    vector_hash: str
    feature_schema_version: str
    feature_schema_hash: str
    timeframe_snapshot_ids: tuple[tuple[str, str], ...]
    source_event_digests: tuple[tuple[str, str], ...]
    source_rights_fingerprint: str
    mode: ScorerDeploymentMode
    deployment_event_id: UUID
    deployment_revision: int
    registry_id: UUID
    registry_revision: int
    active_pointer_version: int
    scorer_artifact_id: UUID
    scorer_artifact_hash: str
    base_artifact_hash: str
    evaluation_report_id: UUID
    evaluation_report_hash: str
    evaluation_status: str
    report_scope_hash: str
    proof_id: UUID
    proof_hash: str
    proof_scope_hash: str
    candidate_completeness_verified: bool
    policy_id: str
    policy_hash: str
    decision: LearningScoreDecision
    reason_code: str | None
    class_probabilities: tuple[tuple[int, float], ...]
    probability: float | None
    confidence: float | None
    uncertainty: float | None
    model_latency_ms: int
    score_event_id: UUID = field(init=False)
    idempotency_key: str = field(init=False)
    event_hash: str = field(init=False)
    canonical_json: str = field(init=False)

    @classmethod
    def create(cls, **values: object) -> LearningScoreEvent:
        """Tạo event với id và idempotency key ổn định theo bot decision."""
        return cls(**values)  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "bot_id",
            "bot_decision_id",
            "deployment_event_id",
            "registry_id",
            "scorer_artifact_id",
            "evaluation_report_id",
            "proof_id",
        ):
            if type(getattr(self, name)) is not UUID:
                raise ValueError(f"{name} phải là UUID authoritative.")
        for name in (
            "bot_decision_hash",
            "incumbent_decision_hash",
            "candidate_id",
            "candidate_hash",
            "decision_snapshot_id",
            "vector_hash",
            "feature_schema_hash",
            "source_rights_fingerprint",
            "scorer_artifact_hash",
            "base_artifact_hash",
            "evaluation_report_hash",
            "report_scope_hash",
            "proof_hash",
            "proof_scope_hash",
            "policy_hash",
        ):
            _require_hash(getattr(self, name), name)
        if self.incumbent_status not in _DECISION_STATUSES or self.final_status not in (
            _DECISION_STATUSES
        ):
            raise ValueError("Trạng thái incumbent/final decision không hợp lệ.")
        if self.incumbent_status != "CANDIDATE":
            raise ValueError("Score event chỉ được gắn với candidate strategy.")
        if not isinstance(self.feature_schema_version, str) or not self.feature_schema_version:
            raise ValueError("feature_schema_version không được để trống.")
        if (
            type(self.mode) is not ScorerDeploymentMode
            or self.mode is ScorerDeploymentMode.DISABLED
        ):
            raise ValueError("Score event cần deployment SHADOW hoặc PAPER_GATE.")
        if (
            self.mode is ScorerDeploymentMode.SHADOW
            and self.bot_decision_hash != self.incumbent_decision_hash
        ):
            raise ValueError("SHADOW không được thay đổi hash của strategy decision.")
        try:
            score_decision = LearningScoreDecision(self.decision)
        except ValueError:
            raise ValueError("decision scorer không hợp lệ.") from None
        object.__setattr__(self, "decision", score_decision)
        expected_final_status = (
            self.incumbent_status
            if self.mode is ScorerDeploymentMode.SHADOW
            else {
                LearningScoreDecision.APPROVE: "CANDIDATE",
                LearningScoreDecision.VETO: "VETO",
                LearningScoreDecision.ABSTAIN: "ABSTAIN",
            }[score_decision]
        )
        if self.final_status != expected_final_status:
            raise ValueError("final_status không khớp mode và kết quả scorer.")
        if type(self.deployment_revision) is not int or self.deployment_revision < 1:
            raise ValueError("deployment_revision phải dương.")
        if type(self.registry_revision) is not int or self.registry_revision < 1:
            raise ValueError("registry_revision phải dương.")
        if type(self.active_pointer_version) is not int or self.active_pointer_version < 0:
            raise ValueError("active_pointer_version không hợp lệ.")
        if type(self.model_latency_ms) is not int or self.model_latency_ms < 0:
            raise ValueError("model_latency_ms không hợp lệ.")
        if type(self.candidate_completeness_verified) is not bool:
            raise ValueError("candidate_completeness_verified phải là bool.")
        if self.proof_scope_hash != self.report_scope_hash:
            raise ValueError("Proof scorer phải cùng scope với report.")
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id không được để trống.")
        if not isinstance(self.evaluation_status, str) or self.evaluation_status not in {
            "ACCEPTED_SHADOW_ONLY",
            "ACCEPTED_PAPER_GATE_ELIGIBLE",
        }:
            raise ValueError("Report scorer chưa được duyệt cho deployment.")
        if self.mode is ScorerDeploymentMode.PAPER_GATE and (
            self.evaluation_status != "ACCEPTED_PAPER_GATE_ELIGIBLE"
            or not self.candidate_completeness_verified
            or self.proof_scope_hash != self.report_scope_hash
        ):
            raise ValueError("PAPER_GATE cần report, proof và completeness đúng phạm vi.")
        if self.mode is ScorerDeploymentMode.SHADOW and (
            self.proof_scope_hash != self.report_scope_hash
            or self.evaluation_status
            not in {"ACCEPTED_SHADOW_ONLY", "ACCEPTED_PAPER_GATE_ELIGIBLE"}
        ):
            raise ValueError("SHADOW cần report scorer và proof cùng phạm vi.")

        timeframes = tuple(sorted(self.timeframe_snapshot_ids))
        digests = tuple(sorted(self.source_event_digests))
        if not timeframes or tuple(name for name, _ in timeframes) != tuple(
            name for name, _ in digests
        ):
            raise ValueError("Timeframe snapshot và source digest phải khớp nhau.")
        if len({name for name, _ in timeframes}) != len(timeframes):
            raise ValueError("Timeframe lineage không được lặp.")
        for timeframe, snapshot_hash in timeframes:
            if not timeframe.strip():
                raise ValueError("Timeframe không được để trống.")
            _require_hash(snapshot_hash, "timeframe_snapshot_id")
        for _, digest in digests:
            _require_hash(digest, "source_event_digest")
        object.__setattr__(self, "timeframe_snapshot_ids", timeframes)
        object.__setattr__(self, "source_event_digests", digests)

        probabilities = tuple(self.class_probabilities)
        if score_decision is LearningScoreDecision.ABSTAIN:
            if (
                not isinstance(self.reason_code, str)
                or not self.reason_code.strip()
                or probabilities
                or self.probability is not None
                or self.confidence is not None
                or self.uncertainty is not None
            ):
                raise ValueError("ABSTAIN cần reason và không được có phân phối xác suất.")
        elif (
            (score_decision is LearningScoreDecision.APPROVE and self.reason_code is not None)
            or (score_decision is LearningScoreDecision.VETO and not self.reason_code)
            or tuple(label for label, _ in probabilities) != _PROBABILITY_LABELS
            or self.probability != probabilities[2][1]
            or self.confidence is None
            or self.uncertainty is None
            or not math.isclose(math.fsum(value for _, value in probabilities), 1.0, abs_tol=1e-12)
            or not math.isclose(
                self.confidence, max(value for _, value in probabilities), abs_tol=1e-12
            )
            or not math.isclose(self.uncertainty, 1.0 - self.confidence, abs_tol=1e-12)
        ):
            raise ValueError("APPROVE/VETO cần distribution ba lớp hợp lệ.")
        for _, value in probabilities:
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError("Xác suất phải hữu hạn trong [0, 1].")
        object.__setattr__(self, "class_probabilities", probabilities)

        key = hashlib.sha256(
            f"learning-score-event-v1|{self.workspace_id}|{self.bot_id}|{self.bot_decision_id}".encode()
        ).hexdigest()
        payload = self.canonical_payload(idempotency_key=key)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "score_event_id", uuid5(_SCORE_EVENT_NAMESPACE, key))
        object.__setattr__(self, "canonical_json", canonical)
        object.__setattr__(
            self, "event_hash", hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )

    def canonical_payload(self, *, idempotency_key: str | None = None) -> dict[str, object]:
        """Trả metadata cố định; không xuất candidate payload hay feature values."""
        return {
            "active_pointer_version": self.active_pointer_version,
            "base_artifact_hash": self.base_artifact_hash,
            "bot_decision_hash": self.bot_decision_hash,
            "bot_decision_id": str(self.bot_decision_id),
            "bot_id": str(self.bot_id),
            "candidate_completeness_verified": self.candidate_completeness_verified,
            "candidate_hash": self.candidate_hash,
            "candidate_id": self.candidate_id,
            "class_probabilities": [[label, value] for label, value in self.class_probabilities],
            "confidence": self.confidence,
            "decision": self.decision.value,
            "decision_snapshot_id": self.decision_snapshot_id,
            "deployment_event_id": str(self.deployment_event_id),
            "deployment_revision": self.deployment_revision,
            "evaluation_report_hash": self.evaluation_report_hash,
            "evaluation_report_id": str(self.evaluation_report_id),
            "evaluation_status": self.evaluation_status,
            "feature_schema_hash": self.feature_schema_hash,
            "feature_schema_version": self.feature_schema_version,
            "final_status": self.final_status,
            "idempotency_key": idempotency_key or self.idempotency_key,
            "incumbent_decision_hash": self.incumbent_decision_hash,
            "incumbent_status": self.incumbent_status,
            "mode": self.mode.value,
            "model_latency_ms": self.model_latency_ms,
            "policy_hash": self.policy_hash,
            "policy_id": self.policy_id,
            "probability": self.probability,
            "proof_hash": self.proof_hash,
            "proof_id": str(self.proof_id),
            "proof_scope_hash": self.proof_scope_hash,
            "reason_code": self.reason_code,
            "registry_id": str(self.registry_id),
            "registry_revision": self.registry_revision,
            "report_scope_hash": self.report_scope_hash,
            "schema_version": "crypidian.learning.score-event.v1",
            "scorer_artifact_hash": self.scorer_artifact_hash,
            "scorer_artifact_id": str(self.scorer_artifact_id),
            "source_event_digests": [
                [timeframe, digest] for timeframe, digest in self.source_event_digests
            ],
            "source_rights_fingerprint": self.source_rights_fingerprint,
            "timeframe_snapshot_ids": [
                [timeframe, snapshot] for timeframe, snapshot in self.timeframe_snapshot_ids
            ],
            "uncertainty": self.uncertainty,
            "vector_hash": self.vector_hash,
            "workspace_id": str(self.workspace_id),
        }


def _require_hash(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")
