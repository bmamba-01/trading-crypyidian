"""Tính báo cáo scorer ngoài mẫu từ batch TEST đã được gateway cấp quyền."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from trading_bot.application.learning_cpu_trainer import LearningCpuArtifact
from trading_bot.application.learning_scorer_artifact import (
    LearningScorerArtifact,
    _validation_logits,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CLASS_LABELS = (-1, 0, 1)
_STATUS = Literal["ACCEPTED_SHADOW_ONLY", "ACCEPTED_PAPER_GATE_ELIGIBLE", "INCONCLUSIVE"]
_OPPORTUNITY_STATUS = Literal["candidate", "no_candidate", "input_excluded"]


@dataclass(frozen=True, slots=True)
class ScorerTestEvaluationPolicy:
    """Ngưỡng đã ghim trước lần đọc TEST; không tự điều chỉnh theo holdout."""

    policy_version: str
    minimum_sample_count: int
    minimum_class_support: int
    maximum_calibration_error: float
    maximum_log_loss_regression: float
    maximum_brier_regression: float
    minimum_coverage: float
    minimum_candidate_retention: float
    maximum_false_veto_rate: float
    approve_probability_threshold: float
    minimum_confidence: float
    reliability_bin_count: int
    maximum_standardized_feature_deviation: float
    runtime_inference_timeout_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version không được để trống.")
        for name in (
            "minimum_sample_count",
            "minimum_class_support",
            "reliability_bin_count",
            "runtime_inference_timeout_ms",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} phải là số nguyên dương.")
        for name in (
            "maximum_calibration_error",
            "maximum_log_loss_regression",
            "maximum_brier_regression",
            "minimum_coverage",
            "minimum_candidate_retention",
            "maximum_false_veto_rate",
            "approve_probability_threshold",
            "minimum_confidence",
            "maximum_standardized_feature_deviation",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} phải là số hữu hạn.")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} phải là số hữu hạn không âm.")
            if (
                name
                in {
                    "maximum_calibration_error",
                    "minimum_coverage",
                    "minimum_candidate_retention",
                    "maximum_false_veto_rate",
                    "approve_probability_threshold",
                    "minimum_confidence",
                }
                and value > 1
            ):
                raise ValueError(f"{name} phải nằm trong [0, 1].")
        if self.maximum_standardized_feature_deviation <= 0:
            raise ValueError("maximum_standardized_feature_deviation phải lớn hơn 0.")

    @property
    def policy_hash(self) -> str:
        return _hash(asdict(self))


@dataclass(frozen=True, slots=True)
class ScorerTestOpportunityEntry:
    """Một TEST opportunity giữ ID băm và liên kết label đã đóng nếu có."""

    stable_id_hash: str
    status: _OPPORTUNITY_STATUS
    candidate_id_hash: str | None
    outcome_id_hash: str | None = None
    outcome_hash: str | None = None
    sample_id_hash: str | None = None
    sample_hash: str | None = None
    vector_hash: str | None = None

    def __post_init__(self) -> None:
        if not _is_sha256(self.stable_id_hash):
            raise ValueError("stable_id_hash phải là SHA-256.")
        if self.status not in {"candidate", "no_candidate", "input_excluded"}:
            raise ValueError("Trạng thái opportunity TEST không hợp lệ.")
        if self.status == "candidate":
            if not _is_sha256(self.candidate_id_hash):
                raise ValueError("Candidate opportunity cần candidate_id_hash.")
        elif self.candidate_id_hash is not None:
            raise ValueError("Opportunity không phải candidate không được có candidate ID.")
        linkage = (
            self.outcome_id_hash,
            self.outcome_hash,
            self.sample_id_hash,
            self.sample_hash,
            self.vector_hash,
        )
        linked_count = sum(value is not None for value in linkage)
        if self.status != "candidate" and linked_count:
            raise ValueError("Opportunity không phải candidate không được có label linkage.")
        if linked_count not in {0, len(linkage)} or any(
            value is not None and not _is_sha256(value) for value in linkage
        ):
            raise ValueError("Candidate linkage cần đủ năm SHA-256 hoặc không có nhãn.")

    @property
    def has_completed_label(self) -> bool:
        """Cho biết candidate có exact outcome/sample/vector lineage đã đóng."""
        return self.status == "candidate" and self.outcome_id_hash is not None


@dataclass(frozen=True, slots=True)
class ScorerTestOpportunityProof:
    """Inventory TEST đã pin scope; completeness suy ra từ entries và gap counts."""

    record_id: UUID
    workspace_id: UUID
    evaluation_run_id: UUID
    attempt_id: UUID
    normalized_dataset_id: UUID
    manifest_hash: str
    fold_plan_hash: str
    fold_id: str
    approval_hash: str
    bundle_hash: str
    vector_schema_version: str
    vector_schema_hash: str
    test_membership_digest: str
    source_scope_hash: str
    expected_opportunity_count: int
    source_gap_count: int
    membership_mismatch_count: int
    unlabeled_candidate_count: int
    entries: tuple[ScorerTestOpportunityEntry, ...]

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, name)) is not UUID
            for name in (
                "record_id",
                "workspace_id",
                "evaluation_run_id",
                "attempt_id",
                "normalized_dataset_id",
            )
        ):
            raise ValueError("Opportunity proof cần UUID authoritative.")
        for name in (
            "manifest_hash",
            "fold_plan_hash",
            "fold_id",
            "approval_hash",
            "bundle_hash",
            "vector_schema_hash",
            "test_membership_digest",
            "source_scope_hash",
        ):
            if not _is_sha256(getattr(self, name)):
                raise ValueError(f"{name} proof phải là SHA-256.")
        if not isinstance(self.vector_schema_version, str) or not self.vector_schema_version:
            raise ValueError("vector_schema_version proof không được để trống.")
        if any(
            type(getattr(self, name)) is not int or getattr(self, name) < 0
            for name in (
                "expected_opportunity_count",
                "source_gap_count",
                "membership_mismatch_count",
                "unlabeled_candidate_count",
            )
        ):
            raise ValueError("Opportunity proof counts phải không âm.")
        if type(self.entries) is not tuple or any(
            type(entry) is not ScorerTestOpportunityEntry for entry in self.entries
        ):
            raise ValueError("Opportunity proof cần tuple entry đã typed.")
        stable_ids = tuple(entry.stable_id_hash for entry in self.entries)
        if stable_ids != tuple(sorted(set(stable_ids))):
            raise ValueError("Opportunity proof entries phải duy nhất và canonical.")
        candidate_ids = self.candidate_id_hashes
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("Candidate ID hash bị lặp trong opportunity proof.")
        if self.unlabeled_candidate_count > self.candidate_count:
            raise ValueError("Số candidate thiếu nhãn vượt tổng candidate TEST.")
        actual_unlabeled_count = sum(
            entry.status == "candidate" and not entry.has_completed_label for entry in self.entries
        )
        if self.unlabeled_candidate_count != actual_unlabeled_count:
            raise ValueError("unlabeled_candidate_count lệch candidate thiếu linkage chính xác.")

    @property
    def candidate_id_hashes(self) -> tuple[str, ...]:
        return tuple(
            entry.candidate_id_hash
            for entry in self.entries
            if entry.status == "candidate" and entry.candidate_id_hash is not None
        )

    @property
    def candidate_count(self) -> int:
        return sum(entry.status == "candidate" for entry in self.entries)

    @property
    def no_candidate_count(self) -> int:
        return sum(entry.status == "no_candidate" for entry in self.entries)

    @property
    def input_excluded_count(self) -> int:
        return sum(entry.status == "input_excluded" for entry in self.entries)

    @property
    def completeness_verified(self) -> bool:
        return (
            len(self.entries) == self.expected_opportunity_count
            and self.source_gap_count == 0
            and self.membership_mismatch_count == 0
            and self.unlabeled_candidate_count == 0
            and self.input_excluded_count == 0
        )

    @property
    def canonical_json(self) -> str:
        """Bytes proof không chứa feature, nhãn hay outcome payload."""
        return _canonical(
            {
                "schema_version": "wp06-scorer-test-opportunity-proof-v2",
                "workspace_id": str(self.workspace_id),
                "evaluation_run_id": str(self.evaluation_run_id),
                "attempt_id": str(self.attempt_id),
                "normalized_dataset_id": str(self.normalized_dataset_id),
                "manifest_hash": self.manifest_hash,
                "fold_plan_hash": self.fold_plan_hash,
                "fold_id": self.fold_id,
                "approval_hash": self.approval_hash,
                "bundle_hash": self.bundle_hash,
                "vector_schema_version": self.vector_schema_version,
                "vector_schema_hash": self.vector_schema_hash,
                "test_membership_digest": self.test_membership_digest,
                "source_scope_hash": self.source_scope_hash,
                "expected_opportunity_count": self.expected_opportunity_count,
                "source_gap_count": self.source_gap_count,
                "membership_mismatch_count": self.membership_mismatch_count,
                "unlabeled_candidate_count": self.unlabeled_candidate_count,
                "entries": [
                    {
                        "stable_id_hash": entry.stable_id_hash,
                        "status": entry.status,
                        "candidate_id_hash": entry.candidate_id_hash,
                        "outcome_id_hash": entry.outcome_id_hash,
                        "outcome_hash": entry.outcome_hash,
                        "sample_id_hash": entry.sample_id_hash,
                        "sample_hash": entry.sample_hash,
                        "vector_hash": entry.vector_hash,
                    }
                    for entry in self.entries
                ],
            }
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScorerTestEvaluationScope:
    """Lineage TEST khớp attempt, dataset, fold, schema, membership và proof đã lưu."""

    workspace_id: UUID
    evaluation_run_id: UUID
    attempt_id: UUID
    normalized_dataset_id: UUID
    manifest_hash: str
    fold_plan_hash: str
    fold_id: str
    approval_hash: str
    vector_schema_version: str
    vector_schema_hash: str
    test_membership_digest: str
    bundle_hash: str
    source_scope_hash: str
    opportunity_proof: ScorerTestOpportunityProof

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, name)) is not UUID
            for name in (
                "workspace_id",
                "evaluation_run_id",
                "attempt_id",
                "normalized_dataset_id",
            )
        ):
            raise ValueError("Scope scorer TEST cần UUID authoritative.")
        for name in (
            "manifest_hash",
            "fold_plan_hash",
            "fold_id",
            "approval_hash",
            "vector_schema_hash",
            "test_membership_digest",
            "bundle_hash",
            "source_scope_hash",
        ):
            if (
                not isinstance(getattr(self, name), str)
                or _SHA256.fullmatch(getattr(self, name)) is None
            ):
                raise ValueError(f"{name} phải là SHA-256 chữ thường.")
        if not isinstance(self.vector_schema_version, str) or not self.vector_schema_version:
            raise ValueError("vector_schema_version không được để trống.")
        if type(self.opportunity_proof) is not ScorerTestOpportunityProof:
            raise ValueError("Scorer report cần proof TEST đã persist.")
        for name in (
            "workspace_id",
            "evaluation_run_id",
            "attempt_id",
            "normalized_dataset_id",
            "manifest_hash",
            "fold_plan_hash",
            "fold_id",
            "approval_hash",
            "bundle_hash",
            "vector_schema_version",
            "vector_schema_hash",
            "test_membership_digest",
            "source_scope_hash",
        ):
            expected = str(getattr(self, name)) if name.endswith("_id") else getattr(self, name)
            actual = (
                str(getattr(self.opportunity_proof, name))
                if name.endswith("_id")
                else getattr(self.opportunity_proof, name)
            )
            if expected != actual:
                raise ValueError("Opportunity proof TEST lệch scope report.")

    @property
    def opportunity_proof_id(self) -> UUID:
        return self.opportunity_proof.record_id

    @property
    def opportunity_proof_hash(self) -> str:
        return self.opportunity_proof.content_hash

    @property
    def candidate_completeness_verified(self) -> bool:
        return self.opportunity_proof.completeness_verified


@dataclass(frozen=True, slots=True)
class ScorerTestRow:
    """Một hàng TEST chỉ tồn tại trong evaluator process; không đi vào report."""

    sample_id: str
    outcome_id: UUID
    outcome_hash: str
    role: Literal["test"]
    sample_hash: str
    vector_hash: str
    candidate_id_hash: str
    features: tuple[tuple[str, float], ...]
    label_value: int
    trade: EvaluationTrade

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id.strip():
            raise ValueError("sample_id TEST không được để trống.")
        if type(self.outcome_id) is not UUID:
            raise ValueError("outcome_id TEST phải là UUID authoritative.")
        if self.role != "test":
            raise ValueError("Evaluator scorer chỉ chấp nhận role TEST.")
        for name in ("sample_hash", "vector_hash", "outcome_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} phải là SHA-256.")
        if not _is_sha256(self.candidate_id_hash):
            raise ValueError("candidate_id_hash phải là SHA-256.")
        if type(self.features) is not tuple or not self.features:
            raise ValueError("features TEST phải là tuple bất biến không rỗng.")
        names: list[str] = []
        for item in self.features:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("Feature TEST phải là cặp tên và giá trị.")
            name, value = item
            if (
                not isinstance(name, str)
                or not name.strip()
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Feature TEST có tên hoặc giá trị không hợp lệ.")
            names.append(name)
        if names != sorted(set(names)):
            raise ValueError("Tên feature TEST phải duy nhất và đã sắp xếp.")
        if type(self.label_value) is not int or self.label_value not in _CLASS_LABELS:
            raise ValueError("label TEST phải thuộc (-1, 0, 1).")
        if type(self.trade) is not EvaluationTrade:
            raise ValueError("TEST candidate cần outcome giao dịch đã đóng authoritative.")


@dataclass(frozen=True, slots=True)
class ScorerReliabilityBin:
    """Một bin độ tin cậy aggregate; không nêu sample hoặc trade ID."""

    lower_bound: float
    upper_bound: float
    sample_count: int
    mean_confidence: float
    accuracy: float


@dataclass(frozen=True, slots=True)
class ScorerPortfolioSummary:
    """Tổng hợp chi phí và drawdown trên cùng các trade đã đóng."""

    trade_count: int
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    net_pnl: Decimal
    maximum_drawdown: Decimal


@dataclass(frozen=True, slots=True)
class ScorerTestEvaluationReport:
    """Báo cáo ngoài mẫu chỉ chứa aggregate metrics và hash lineage."""

    status: _STATUS
    workspace_id: UUID
    evaluation_run_id: UUID
    attempt_id: UUID
    normalized_dataset_id: UUID
    manifest_hash: str
    fold_plan_hash: str
    fold_id: str
    approval_hash: str
    scorer_artifact_hash: str
    base_artifact_hash: str
    vector_schema_version: str
    vector_schema_hash: str
    bundle_hash: str
    test_membership_digest: str
    source_scope_hash: str
    opportunity_proof_id: UUID | None
    proof_hash: str | None
    candidate_completeness_verified: bool
    scored_candidate_coverage_verified: bool
    policy_version: str
    policy_hash: str
    evaluator_version: str
    evaluated_at: datetime
    test_sample_count: int
    class_support: tuple[tuple[int, int], ...]
    coverage: float
    abstain_count: int
    log_loss: float
    brier_score: float
    prior_baseline_log_loss: float
    identity_temperature_log_loss: float
    expected_calibration_error: float
    reliability_bins: tuple[ScorerReliabilityBin, ...]
    confusion_matrix: tuple[tuple[int, ...], ...]
    opportunity_count: int
    opportunity_candidate_count: int
    no_candidate_count: int
    input_excluded_count: int
    candidate_count: int
    scored_candidate_count: int
    approved_candidate_count: int
    vetoed_candidate_count: int
    false_veto_count: int
    false_veto_rate: float
    scored_candidate_retention: float
    candidate_retention: float | None
    unfiltered_portfolio: ScorerPortfolioSummary
    filtered_portfolio: ScorerPortfolioSummary
    test_access_receipt_id: UUID | None
    test_access_receipt_hash: str | None
    test_current_rights_receipt_hash: str | None
    test_payload_consumption_id: UUID | None
    test_payload_commitment_hash: str | None
    test_payload_consumption_hash: str | None
    test_payload_consumed_at: datetime | None
    report_hash: str

    @property
    def canonical_json(self) -> str:
        """JSON chuẩn không chứa sample/feature/label/trade payload."""
        payload = asdict(self)
        payload.pop("report_hash")
        payload["workspace_id"] = str(self.workspace_id)
        payload["evaluation_run_id"] = str(self.evaluation_run_id)
        payload["attempt_id"] = str(self.attempt_id)
        payload["normalized_dataset_id"] = str(self.normalized_dataset_id)
        payload["opportunity_proof_id"] = (
            None if self.opportunity_proof_id is None else str(self.opportunity_proof_id)
        )
        payload["test_access_receipt_id"] = (
            None if self.test_access_receipt_id is None else str(self.test_access_receipt_id)
        )
        payload["test_payload_consumption_id"] = (
            None
            if self.test_payload_consumption_id is None
            else str(self.test_payload_consumption_id)
        )
        payload["evaluated_at"] = self.evaluated_at.astimezone(UTC).isoformat()
        payload["test_payload_consumed_at"] = (
            None
            if self.test_payload_consumed_at is None
            else self.test_payload_consumed_at.astimezone(UTC).isoformat()
        )
        payload["class_support"] = [list(item) for item in self.class_support]
        payload["confusion_matrix"] = [list(item) for item in self.confusion_matrix]
        for key in ("unfiltered_portfolio", "filtered_portfolio"):
            portfolio = payload[key]
            for name in ("gross_pnl", "fees", "funding", "slippage", "net_pnl", "maximum_drawdown"):
                portfolio[name] = str(portfolio[name])
        payload["reliability_bins"] = [asdict(item) for item in self.reliability_bins]
        return _canonical(payload)


def build_scorer_test_evaluation_report(
    *,
    scope: ScorerTestEvaluationScope,
    scorer_artifact: LearningScorerArtifact,
    base_artifact: LearningCpuArtifact,
    policy: ScorerTestEvaluationPolicy,
    rows: tuple[ScorerTestRow, ...],
    evaluated_at: datetime | None = None,
) -> ScorerTestEvaluationReport:
    """Tính OOS metrics; chỉ hash và chỉ số aggregate được trả ra ngoài."""
    if type(scope) is not ScorerTestEvaluationScope:
        raise ValueError("Cần scope TEST đã được evaluator gateway xác minh.")
    if type(scorer_artifact) is not LearningScorerArtifact:
        raise ValueError("Cần scorer artifact v2 authoritative.")
    if type(base_artifact) is not LearningCpuArtifact:
        raise ValueError("Cần artifact v1 authoritative.")
    if type(policy) is not ScorerTestEvaluationPolicy:
        raise ValueError("Cần policy scorer TEST đã version hóa.")
    if scorer_artifact.state != "ready_for_evaluation" or scorer_artifact.temperature is None:
        raise ValueError("Scorer artifact chưa đủ điều kiện đọc TEST.")
    if base_artifact.artifact_hash != scorer_artifact.base_artifact_hash:
        raise ValueError("Scorer artifact không khớp base artifact v1.")
    if (
        isinstance(scorer_artifact.temperature, bool)
        or not isinstance(scorer_artifact.temperature, (int, float))
        or not math.isfinite(scorer_artifact.temperature)
        or scorer_artifact.temperature <= 0
    ):
        raise ValueError("Scorer artifact có temperature không hợp lệ.")
    expected_scope = (
        ("workspace_id", str(scope.workspace_id), scorer_artifact.workspace_id),
        ("attempt_id", str(scope.attempt_id), scorer_artifact.attempt_id),
        (
            "normalized_dataset_id",
            str(scope.normalized_dataset_id),
            scorer_artifact.normalized_dataset_id,
        ),
        ("manifest_hash", scope.manifest_hash, scorer_artifact.manifest_hash),
        ("fold_plan_hash", scope.fold_plan_hash, scorer_artifact.fold_plan_hash),
        ("fold_id", scope.fold_id, scorer_artifact.fold_id),
        ("approval_hash", scope.approval_hash, scorer_artifact.approval_hash),
        (
            "vector_schema_version",
            scope.vector_schema_version,
            scorer_artifact.vector_schema_version,
        ),
        ("vector_schema_hash", scope.vector_schema_hash, scorer_artifact.vector_schema_hash),
    )
    if any(expected != actual for _, expected, actual in expected_scope):
        raise ValueError("Scope TEST lệch scorer artifact v2.")
    if type(rows) is not tuple or not rows or any(type(row) is not ScorerTestRow for row in rows):
        raise ValueError("Batch TEST phải là tuple không rỗng chỉ gồm ScorerTestRow.")
    if len({row.sample_id for row in rows}) != len(rows):
        raise ValueError("Batch TEST chứa sample ID trùng.")
    if len({row.trade.trade_id for row in rows}) != len(rows):
        raise ValueError("Batch TEST chứa trade ID trùng.")
    row_candidate_hashes = tuple(row.candidate_id_hash for row in rows)
    if len(set(row_candidate_hashes)) != len(row_candidate_hashes):
        raise ValueError("Batch TEST chứa candidate lặp; không được tăng trọng số candidate.")
    proof_entries_by_candidate = {
        entry.candidate_id_hash: entry
        for entry in scope.opportunity_proof.entries
        if entry.status == "candidate" and entry.candidate_id_hash is not None
    }
    proof_candidate_hashes = set(proof_entries_by_candidate)
    if any(value not in proof_entries_by_candidate for value in row_candidate_hashes):
        raise ValueError("TEST row candidate không có trong opportunity proof authoritative.")
    for row in rows:
        entry = proof_entries_by_candidate[row.candidate_id_hash]
        expected_linkage = (
            _sha256_identifier(str(row.outcome_id)),
            row.outcome_hash,
            _sha256_identifier(row.sample_id),
            row.sample_hash,
            row.vector_hash,
        )
        observed_linkage = (
            entry.outcome_id_hash,
            entry.outcome_hash,
            entry.sample_id_hash,
            entry.sample_hash,
            entry.vector_hash,
        )
        if not entry.has_completed_label or observed_linkage != expected_linkage:
            raise ValueError("TEST row không khớp exact opportunity proof linkage.")
    sample_names = tuple(name for name, _ in rows[0].features)
    if sample_names != base_artifact.feature_names or any(
        tuple(name for name, _ in row.features) != sample_names for row in rows
    ):
        raise ValueError("TEST feature schema/order lệch artifact v1.")
    if scorer_test_payload_membership_digest(rows) != scope.test_membership_digest:
        raise ValueError("TEST membership digest không khớp các hàng được cấp.")
    timestamp = evaluated_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("evaluated_at phải có timezone.")

    distributions: list[tuple[float, float, float]] = []
    identity_distributions: list[tuple[float, float, float]] = []
    class_predictions: list[int | None] = []
    decisions: list[Literal["approve", "veto", "abstain"]] = []
    confidences: list[float] = []
    labels = tuple(row.label_value for row in rows)
    temperature = scorer_artifact.temperature
    for row in rows:
        logits = _validation_logits(base_artifact, row.features)
        distributions.append(
            _softmax(
                (
                    logits[0] / temperature,
                    logits[1] / temperature,
                    logits[2] / temperature,
                )
            )
        )
        identity_distributions.append(_softmax(logits))
        probabilities = distributions[-1]
        confidence = max(probabilities)
        confidences.append(confidence)
        if confidence < policy.minimum_confidence:
            decisions.append("abstain")
            class_predictions.append(None)
        elif probabilities[2] >= policy.approve_probability_threshold:
            decisions.append("approve")
            class_predictions.append(max(range(3), key=probabilities.__getitem__) - 1)
        else:
            decisions.append("veto")
            class_predictions.append(max(range(3), key=probabilities.__getitem__) - 1)

    class_support = tuple((label, labels.count(label)) for label in _CLASS_LABELS)
    log_loss = _mean_nll(distributions, labels)
    identity_log_loss = _mean_nll(identity_distributions, labels)
    prior = _training_prior(base_artifact)
    prior_log_loss = _mean_nll(tuple(prior for _ in rows), labels)
    brier_score = _mean_brier(distributions, labels)
    reliability_bins, calibration_error = _reliability_bins(
        distributions, labels, policy.reliability_bin_count
    )
    covered = sum(decision != "abstain" for decision in decisions)
    coverage = covered / len(rows)
    confusion = _confusion_matrix(labels, class_predictions)
    approved = sum(decision == "approve" for decision in decisions)
    vetoed = sum(decision == "veto" for decision in decisions)
    positive_count = labels.count(1)
    false_veto = sum(
        label == 1 and decision != "approve"
        for label, decision in zip(labels, decisions, strict=True)
    )
    false_veto_rate = false_veto / positive_count if positive_count else 1.0
    scored_retention = approved / len(rows)
    scored_candidate_coverage = set(row_candidate_hashes) == set(proof_candidate_hashes)
    candidate_retention = scored_retention if scored_candidate_coverage else None
    unfiltered_portfolio = _portfolio(tuple(row.trade for row in rows))
    filtered_portfolio = _portfolio(
        tuple(
            row.trade
            for row, decision in zip(rows, decisions, strict=True)
            if decision == "approve"
        )
    )
    metrics_pass = (
        len(rows) >= policy.minimum_sample_count
        and all(count >= policy.minimum_class_support for _, count in class_support)
        and calibration_error <= policy.maximum_calibration_error
        and log_loss <= min(prior_log_loss, identity_log_loss) + policy.maximum_log_loss_regression
        and brier_score
        <= _mean_brier(tuple(prior for _ in rows), labels) + policy.maximum_brier_regression
        and coverage >= policy.minimum_coverage
        and scored_retention >= policy.minimum_candidate_retention
        and false_veto_rate <= policy.maximum_false_veto_rate
    )
    if not metrics_pass:
        status: _STATUS = "INCONCLUSIVE"
    elif scope.candidate_completeness_verified and scored_candidate_coverage:
        status = "ACCEPTED_PAPER_GATE_ELIGIBLE"
    else:
        status = "ACCEPTED_SHADOW_ONLY"

    report = ScorerTestEvaluationReport(
        status=status,
        workspace_id=scope.workspace_id,
        evaluation_run_id=scope.evaluation_run_id,
        attempt_id=scope.attempt_id,
        normalized_dataset_id=scope.normalized_dataset_id,
        manifest_hash=scope.manifest_hash,
        fold_plan_hash=scope.fold_plan_hash,
        fold_id=scope.fold_id,
        approval_hash=scope.approval_hash,
        scorer_artifact_hash=scorer_artifact.artifact_hash,
        base_artifact_hash=base_artifact.artifact_hash,
        vector_schema_version=scope.vector_schema_version,
        vector_schema_hash=scope.vector_schema_hash,
        bundle_hash=scope.bundle_hash,
        test_membership_digest=scope.test_membership_digest,
        source_scope_hash=scope.source_scope_hash,
        opportunity_proof_id=scope.opportunity_proof_id,
        proof_hash=scope.opportunity_proof_hash,
        candidate_completeness_verified=scope.candidate_completeness_verified,
        scored_candidate_coverage_verified=scored_candidate_coverage,
        policy_version=policy.policy_version,
        policy_hash=policy.policy_hash,
        evaluator_version="wp06-scorer-test-evaluator-v1",
        evaluated_at=timestamp.astimezone(UTC),
        test_sample_count=len(rows),
        class_support=class_support,
        coverage=coverage,
        abstain_count=len(rows) - covered,
        log_loss=log_loss,
        brier_score=brier_score,
        prior_baseline_log_loss=prior_log_loss,
        identity_temperature_log_loss=identity_log_loss,
        expected_calibration_error=calibration_error,
        reliability_bins=reliability_bins,
        confusion_matrix=confusion,
        opportunity_count=scope.opportunity_proof.expected_opportunity_count,
        opportunity_candidate_count=scope.opportunity_proof.candidate_count,
        no_candidate_count=scope.opportunity_proof.no_candidate_count,
        input_excluded_count=scope.opportunity_proof.input_excluded_count,
        candidate_count=scope.opportunity_proof.candidate_count,
        scored_candidate_count=len(rows),
        approved_candidate_count=approved,
        vetoed_candidate_count=vetoed,
        false_veto_count=false_veto,
        false_veto_rate=false_veto_rate,
        scored_candidate_retention=scored_retention,
        candidate_retention=candidate_retention,
        unfiltered_portfolio=unfiltered_portfolio,
        filtered_portfolio=filtered_portfolio,
        test_access_receipt_id=None,
        test_access_receipt_hash=None,
        test_current_rights_receipt_hash=None,
        test_payload_consumption_id=None,
        test_payload_commitment_hash=None,
        test_payload_consumption_hash=None,
        test_payload_consumed_at=None,
        report_hash="0" * 64,
    )
    report_hash = hashlib.sha256(report.canonical_json.encode("utf-8")).hexdigest()
    from dataclasses import replace

    return replace(report, report_hash=report_hash)


def scorer_test_payload_membership_digest(rows: tuple[ScorerTestRow, ...]) -> str:
    """Băm đúng sample, sample-hash và vector-hash được phép trong TEST."""
    if type(rows) is not tuple or not rows or any(type(row) is not ScorerTestRow for row in rows):
        raise ValueError("Membership TEST cần tuple ScorerTestRow không rỗng.")
    return _hash(
        [
            {
                "sample_id": row.sample_id,
                "sample_hash": row.sample_hash,
                "vector_hash": row.vector_hash,
            }
            for row in sorted(rows, key=lambda item: item.sample_id)
        ]
    )


def scorer_test_payload_commitment_hash(rows: tuple[ScorerTestRow, ...]) -> str:
    """Băm candidate, normalized sample và outcome hash đã được xác minh."""
    if type(rows) is not tuple or not rows or any(type(row) is not ScorerTestRow for row in rows):
        raise ValueError("Payload commitment cần tuple TEST typed không rỗng.")
    members = [
        {
            "candidate_id_hash": row.candidate_id_hash,
            "outcome_hash": row.outcome_hash,
            "outcome_id": str(row.outcome_id),
            "sample_hash": row.sample_hash,
            "sample_id": row.sample_id,
            "vector_hash": row.vector_hash,
        }
        for row in sorted(rows, key=lambda item: item.sample_id)
    ]
    return _hash(members)


def _softmax(logits: tuple[float, float, float]) -> tuple[float, float, float]:
    peak = max(logits)
    values = tuple(math.exp(value - peak) for value in logits)
    denominator = math.fsum(values)
    probabilities = tuple(value / denominator for value in values)
    if any(not math.isfinite(value) for value in probabilities):
        raise ValueError("Scorer tạo xác suất TEST không hữu hạn.")
    return probabilities  # type: ignore[return-value]


def _mean_nll(
    distributions: tuple[tuple[float, float, float], ...] | list[tuple[float, float, float]],
    labels: tuple[int, ...],
) -> float:
    return -math.fsum(
        math.log(max(distribution[label + 1], 1e-15))
        for distribution, label in zip(distributions, labels, strict=True)
    ) / len(labels)


def _mean_brier(
    distributions: tuple[tuple[float, float, float], ...] | list[tuple[float, float, float]],
    labels: tuple[int, ...],
) -> float:
    return math.fsum(
        math.fsum(
            (probability - float(index == label + 1)) ** 2
            for index, probability in enumerate(distribution)
        )
        for distribution, label in zip(distributions, labels, strict=True)
    ) / len(labels)


def _training_prior(artifact: LearningCpuArtifact) -> tuple[float, float, float]:
    counts = tuple(count for _, count in artifact.training_class_counts)
    if (
        tuple(label for label, _ in artifact.training_class_counts) != _CLASS_LABELS
        or any(type(count) is not int or count < 0 for count in counts)
        or sum(counts) < 1
    ):
        raise ValueError("Artifact v1 thiếu training class prior authoritative.")
    total = sum(counts)
    return tuple(count / total for count in counts)  # type: ignore[return-value]


def _reliability_bins(
    distributions: list[tuple[float, float, float]], labels: tuple[int, ...], bin_count: int
) -> tuple[tuple[ScorerReliabilityBin, ...], float]:
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bin_count)]
    for distribution, label in zip(distributions, labels, strict=True):
        confidence = max(distribution)
        predicted = max(range(3), key=distribution.__getitem__) - 1
        bucket = min(int(confidence * bin_count), bin_count - 1)
        buckets[bucket].append((confidence, predicted == label))
    bins = []
    error = 0.0
    total = len(labels)
    for index, values in enumerate(buckets):
        if not values:
            continue
        mean_confidence = math.fsum(value for value, _ in values) / len(values)
        accuracy = sum(correct for _, correct in values) / len(values)
        error += len(values) / total * abs(mean_confidence - accuracy)
        bins.append(
            ScorerReliabilityBin(
                lower_bound=index / bin_count,
                upper_bound=(index + 1) / bin_count,
                sample_count=len(values),
                mean_confidence=mean_confidence,
                accuracy=accuracy,
            )
        )
    return tuple(bins), error


def _confusion_matrix(
    labels: tuple[int, ...], predictions: list[int | None]
) -> tuple[tuple[int, ...], ...]:
    matrix = [[0, 0, 0, 0] for _ in _CLASS_LABELS]
    for label, prediction in zip(labels, predictions, strict=True):
        matrix[label + 1][3 if prediction is None else prediction + 1] += 1
    return tuple(tuple(row) for row in matrix)


def _portfolio(trades: tuple[EvaluationTrade, ...]) -> ScorerPortfolioSummary:
    ordered = tuple(sorted(trades, key=lambda trade: (trade.closed_at, trade.opened_at)))
    equity = Decimal(0)
    peak = Decimal(0)
    drawdown = Decimal(0)
    for trade in ordered:
        equity += trade.net_pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    gross = sum((trade.gross_pnl for trade in ordered), Decimal(0))
    fees = sum((trade.fees for trade in ordered), Decimal(0))
    funding = sum((trade.funding for trade in ordered), Decimal(0))
    slippage = sum((trade.slippage for trade in ordered), Decimal(0))
    return ScorerPortfolioSummary(
        trade_count=len(ordered),
        gross_pnl=gross,
        fees=fees,
        funding=funding,
        slippage=slippage,
        net_pnl=gross - fees - funding - slippage,
        maximum_drawdown=drawdown,
    )


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _sha256_identifier(value: str) -> str:
    """Hash UTF-8 của định danh để proof không lưu raw identity."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "ScorerPortfolioSummary",
    "ScorerReliabilityBin",
    "ScorerTestEvaluationPolicy",
    "ScorerTestEvaluationReport",
    "ScorerTestEvaluationScope",
    "ScorerTestRow",
    "build_scorer_test_evaluation_report",
    "scorer_test_payload_membership_digest",
    "scorer_test_payload_commitment_hash",
]
