"""Điều phối đánh giá TEST sau hai transaction một lần, không trả payload sớm."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from trading_bot.application.learning_cpu_trainer import LearningCpuArtifact
from trading_bot.application.learning_scorer_artifact import LearningScorerArtifact
from trading_bot.application.learning_scorer_evaluation import (
    ScorerTestEvaluationPolicy,
    ScorerTestEvaluationReport,
    ScorerTestEvaluationScope,
    ScorerTestRow,
    build_scorer_test_evaluation_report,
    scorer_test_payload_commitment_hash,
)


class ScorerTestEvaluationAccessConflict(RuntimeError):
    """Receipt TEST không được commit hoặc không khớp scope của request."""


class ScorerTestEvaluationReportConflict(RuntimeError):
    """Run đã có report khác request hoặc repository lưu sai canonical report."""


@dataclass(frozen=True, slots=True)
class ScorerTestEvaluationRequest:
    """Scope và artifact đã nạp; không nhận TEST payload từ caller."""

    scope: ScorerTestEvaluationScope
    scorer_artifact: LearningScorerArtifact
    base_artifact: LearningCpuArtifact
    policy: ScorerTestEvaluationPolicy
    evaluated_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.scope) is not ScorerTestEvaluationScope:
            raise ValueError("Request scorer TEST cần scope authoritative.")
        if type(self.scorer_artifact) is not LearningScorerArtifact:
            raise ValueError("Request scorer TEST cần artifact v2 typed.")
        if type(self.base_artifact) is not LearningCpuArtifact:
            raise ValueError("Request scorer TEST cần artifact base v1 typed.")
        if type(self.policy) is not ScorerTestEvaluationPolicy:
            raise ValueError("Request scorer TEST cần policy đã version hóa.")
        if self.evaluated_at is not None and (
            not isinstance(self.evaluated_at, datetime)
            or self.evaluated_at.tzinfo is None
            or self.evaluated_at.utcoffset() is None
        ):
            raise ValueError("evaluated_at phải có timezone.")


@dataclass(frozen=True, slots=True)
class ScorerTestAccessReceipt:
    """Receipt DB đã commit, chỉ ghim scope; không chứa payload TEST."""

    receipt_id: UUID
    workspace_id: UUID
    receipt_hash: str
    request_fingerprint: str
    scorer_artifact_hash: str
    policy_hash: str
    proof_id: UUID
    proof_hash: str
    current_rights_receipt_hash: str
    committed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ScorerTestPayloadConsumption:
    """Metadata đã commit cùng batch TEST; hàng chỉ tồn tại trong process evaluator."""

    consumption_id: UUID
    workspace_id: UUID
    receipt_id: UUID
    request_fingerprint: str
    payload_count: int
    membership_digest: str
    payload_commitment_hash: str
    consumption_hash: str
    current_rights_receipt_hash: str
    consumed_at: datetime
    rows: tuple[ScorerTestRow, ...]


@dataclass(frozen=True, slots=True)
class ScorerTestStoredReport:
    """Report đã lưu kèm fingerprint để retry không mở lại TEST payload."""

    record_id: UUID
    request_fingerprint: str
    report: ScorerTestEvaluationReport
    is_duplicate: bool
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class ScorerTestEvaluationResult:
    """Kết quả run mới hoặc canonical report đã được lấy lại idempotently."""

    report: ScorerTestEvaluationReport
    record_id: UUID
    is_duplicate: bool


class ScorerTestAccessGateway(Protocol):
    """DB adapter commits receipt, then report+one-shot payload atomically in phase two."""

    async def open_committed_receipt(
        self,
        scope: ScorerTestEvaluationScope,
        *,
        scorer_artifact_hash: str,
        policy_hash: str,
        request_fingerprint: str,
    ) -> ScorerTestAccessReceipt: ...

    async def evaluate_and_persist(
        self,
        receipt: ScorerTestAccessReceipt,
        *,
        request_fingerprint: str,
        evaluate_payload: Callable[[ScorerTestPayloadConsumption], ScorerTestEvaluationReport],
    ) -> ScorerTestStoredReport:
        """Recheck rights, read once, evaluate and persist before payload UoW commits."""
        ...


class ScorerTestReportRepository(Protocol):
    """Persist sanitized aggregate report under workspace/run uniqueness."""

    async def get_by_run(
        self, *, workspace_id: UUID, evaluation_run_id: UUID
    ) -> ScorerTestStoredReport | None: ...


class ScorerTestEvaluationService:
    """Receipt commit → one-time TEST read/commit → aggregate report persistence."""

    def __init__(
        self,
        access: ScorerTestAccessGateway,
        reports: ScorerTestReportRepository,
    ) -> None:
        self._access = access
        self._reports = reports

    async def evaluate(self, request: ScorerTestEvaluationRequest) -> ScorerTestEvaluationResult:
        """Chạy exact TEST một lần; report retry không tiêu thụ lại receipt."""
        if type(request) is not ScorerTestEvaluationRequest:
            raise ValueError("Cần ScorerTestEvaluationRequest tường minh.")
        fingerprint = scorer_test_evaluation_request_fingerprint(request)
        scope = request.scope
        existing = await self._reports.get_by_run(
            workspace_id=scope.workspace_id,
            evaluation_run_id=scope.evaluation_run_id,
        )
        if existing is not None:
            self._validate_stored_report(existing, request, fingerprint)
            return ScorerTestEvaluationResult(
                report=existing.report,
                record_id=existing.record_id,
                is_duplicate=True,
            )

        receipt = await self._access.open_committed_receipt(
            scope,
            scorer_artifact_hash=request.scorer_artifact.artifact_hash,
            policy_hash=request.policy.policy_hash,
            request_fingerprint=fingerprint,
        )
        self._validate_receipt(receipt, request, fingerprint)

        expected_evidence: list[tuple[UUID, str, str, UUID, str]] = []

        def evaluate_payload(
            consumption: ScorerTestPayloadConsumption,
        ) -> ScorerTestEvaluationReport:
            if expected_evidence:
                raise ScorerTestEvaluationAccessConflict(
                    "Gateway chỉ được mở đúng một batch TEST trong phase two."
                )
            self._validate_consumption(consumption, receipt, request)
            report = build_scorer_test_evaluation_report(
                scope=scope,
                scorer_artifact=request.scorer_artifact,
                base_artifact=request.base_artifact,
                policy=request.policy,
                rows=consumption.rows,
                evaluated_at=request.evaluated_at,
            )
            receipt_hash = scorer_test_access_receipt_hash(
                receipt=receipt,
                scope=scope,
            )
            consumption_hash = scorer_test_payload_consumption_hash(consumption)
            expected_evidence.append(
                (
                    receipt.receipt_id,
                    receipt_hash,
                    consumption.current_rights_receipt_hash,
                    consumption.consumption_id,
                    consumption_hash,
                )
            )
            return _bind_access_evidence(
                report,
                receipt=receipt,
                receipt_hash=receipt_hash,
                consumption=consumption,
                consumption_hash=consumption_hash,
            )

        # Phase two keeps the rights lock through report persistence and commit. Raw
        # rows are passed only to this in-process callback; the gateway returns metadata.
        stored = await self._access.evaluate_and_persist(
            receipt,
            request_fingerprint=fingerprint,
            evaluate_payload=evaluate_payload,
        )
        expected = expected_evidence[0] if expected_evidence else None
        self._validate_stored_report(
            stored,
            request,
            fingerprint,
            expected_receipt_id=None if expected is None else expected[0],
            expected_receipt_hash=None if expected is None else expected[1],
            expected_rights_hash=None if expected is None else expected[2],
            expected_consumption_id=None if expected is None else expected[3],
            expected_consumption_hash=None if expected is None else expected[4],
        )
        return ScorerTestEvaluationResult(
            report=stored.report,
            record_id=stored.record_id,
            is_duplicate=stored.is_duplicate,
        )

    @staticmethod
    def _validate_receipt(
        receipt: ScorerTestAccessReceipt,
        request: ScorerTestEvaluationRequest,
        fingerprint: str,
    ) -> None:
        scope = request.scope
        if (
            type(receipt) is not ScorerTestAccessReceipt
            or type(receipt.receipt_id) is not UUID
            or receipt.workspace_id != scope.workspace_id
            or not _is_sha256(receipt.receipt_hash)
            or receipt.request_fingerprint != fingerprint
            or receipt.scorer_artifact_hash != request.scorer_artifact.artifact_hash
            or receipt.policy_hash != request.policy.policy_hash
            or receipt.proof_id != scope.opportunity_proof_id
            or receipt.proof_hash != scope.opportunity_proof_hash
            or not _is_sha256(receipt.current_rights_receipt_hash)
            or not isinstance(receipt.committed_at, datetime)
            or receipt.committed_at.tzinfo is None
            or receipt.committed_at.utcoffset() is None
        ):
            raise ScorerTestEvaluationAccessConflict(
                "Receipt TEST chưa commit hoặc lệch artifact/proof/request scope."
            )

    @staticmethod
    def _validate_consumption(
        consumption: ScorerTestPayloadConsumption,
        receipt: ScorerTestAccessReceipt,
        request: ScorerTestEvaluationRequest,
    ) -> None:
        rows = consumption.rows if type(consumption) is ScorerTestPayloadConsumption else ()
        if (
            type(consumption) is not ScorerTestPayloadConsumption
            or type(consumption.consumption_id) is not UUID
            or consumption.workspace_id != request.scope.workspace_id
            or consumption.receipt_id != receipt.receipt_id
            or consumption.request_fingerprint != receipt.request_fingerprint
            or type(consumption.payload_count) is not int
            or consumption.payload_count <= 0
            or type(rows) is not tuple
            or not rows
            or consumption.payload_count != len(rows)
            or any(type(row) is not ScorerTestRow for row in rows)
            or consumption.membership_digest != request.scope.test_membership_digest
            or not _is_sha256(consumption.payload_commitment_hash)
            or consumption.payload_commitment_hash != scorer_test_payload_commitment_hash(rows)
            or not _is_sha256(consumption.consumption_hash)
            or not _is_sha256(consumption.current_rights_receipt_hash)
            or not isinstance(consumption.consumed_at, datetime)
            or consumption.consumed_at.tzinfo is None
            or consumption.consumed_at.utcoffset() is None
        ):
            raise ScorerTestEvaluationAccessConflict(
                "Payload consumption chưa commit hoặc lệch receipt, quyền, membership TEST."
            )

    @staticmethod
    def _validate_stored_report(
        stored: ScorerTestStoredReport,
        request: ScorerTestEvaluationRequest,
        fingerprint: str,
        *,
        expected_receipt_id: UUID | None = None,
        expected_receipt_hash: str | None = None,
        expected_rights_hash: str | None = None,
        expected_consumption_id: UUID | None = None,
        expected_consumption_hash: str | None = None,
    ) -> None:
        if type(stored) is not ScorerTestStoredReport or type(stored.record_id) is not UUID:
            raise ScorerTestEvaluationReportConflict("Report store trả record không hợp lệ.")
        report = stored.report
        scope = request.scope
        if (
            type(report) is not ScorerTestEvaluationReport
            or type(stored.is_duplicate) is not bool
            or not isinstance(stored.committed_at, datetime)
            or stored.committed_at.tzinfo is None
            or stored.committed_at.utcoffset() is None
            or stored.request_fingerprint != fingerprint
            or hashlib.sha256(report.canonical_json.encode("utf-8")).hexdigest()
            != report.report_hash
            or report.workspace_id != scope.workspace_id
            or report.evaluation_run_id != scope.evaluation_run_id
            or report.attempt_id != scope.attempt_id
            or report.normalized_dataset_id != scope.normalized_dataset_id
            or report.manifest_hash != scope.manifest_hash
            or report.fold_plan_hash != scope.fold_plan_hash
            or report.fold_id != scope.fold_id
            or report.approval_hash != scope.approval_hash
            or report.scorer_artifact_hash != request.scorer_artifact.artifact_hash
            or report.base_artifact_hash != request.base_artifact.artifact_hash
            or report.vector_schema_version != scope.vector_schema_version
            or report.vector_schema_hash != scope.vector_schema_hash
            or report.bundle_hash != scope.bundle_hash
            or report.test_membership_digest != scope.test_membership_digest
            or report.source_scope_hash != scope.source_scope_hash
            or report.opportunity_proof_id != scope.opportunity_proof_id
            or report.proof_hash != scope.opportunity_proof_hash
            or report.policy_version != request.policy.policy_version
            or report.policy_hash != request.policy.policy_hash
            or type(report.test_access_receipt_id) is not UUID
            or not _is_sha256(report.test_access_receipt_hash)
            or not _is_sha256(report.test_current_rights_receipt_hash)
            or type(report.test_payload_consumption_id) is not UUID
            or not _is_sha256(report.test_payload_commitment_hash)
            or not _is_sha256(report.test_payload_consumption_hash)
            or not isinstance(report.test_payload_consumed_at, datetime)
            or report.test_payload_consumed_at.tzinfo is None
            or report.test_payload_consumed_at.utcoffset() is None
            or (
                expected_receipt_id is not None
                and report.test_access_receipt_id != expected_receipt_id
            )
            or (
                expected_receipt_hash is not None
                and report.test_access_receipt_hash != expected_receipt_hash
            )
            or (
                expected_rights_hash is not None
                and report.test_current_rights_receipt_hash != expected_rights_hash
            )
            or (
                expected_consumption_id is not None
                and report.test_payload_consumption_id != expected_consumption_id
            )
            or (
                expected_consumption_hash is not None
                and report.test_payload_consumption_hash != expected_consumption_hash
            )
            or (
                request.evaluated_at is not None
                and report.evaluated_at != request.evaluated_at.astimezone(UTC)
            )
        ):
            raise ScorerTestEvaluationReportConflict(
                "Report lưu không khớp fingerprint, hash hoặc lineage TEST request."
            )


def scorer_test_evaluation_request_fingerprint(
    request: ScorerTestEvaluationRequest,
) -> str:
    """Ghim idempotency vào TEST proof, artifact v2 và policy hiện hành."""
    if type(request) is not ScorerTestEvaluationRequest:
        raise ValueError("Cần ScorerTestEvaluationRequest tường minh.")
    scope = request.scope
    payload = {
        "purpose": "scorer-test-evaluation",
        "workspace_id": str(scope.workspace_id),
        "evaluation_run_id": str(scope.evaluation_run_id),
        "attempt_id": str(scope.attempt_id),
        "normalized_dataset_id": str(scope.normalized_dataset_id),
        "manifest_hash": scope.manifest_hash,
        "fold_plan_hash": scope.fold_plan_hash,
        "fold_id": scope.fold_id,
        "approval_hash": scope.approval_hash,
        "bundle_hash": scope.bundle_hash,
        "vector_schema_version": scope.vector_schema_version,
        "vector_schema_hash": scope.vector_schema_hash,
        "test_membership_digest": scope.test_membership_digest,
        "source_scope_hash": scope.source_scope_hash,
        "opportunity_proof_id": str(scope.opportunity_proof_id),
        "opportunity_proof_hash": scope.opportunity_proof_hash,
        "scorer_artifact_hash": request.scorer_artifact.artifact_hash,
        "base_artifact_hash": request.base_artifact.artifact_hash,
        "policy_version": request.policy.policy_version,
        "policy_hash": request.policy.policy_hash,
        "evaluated_at": (
            None
            if request.evaluated_at is None
            else request.evaluated_at.astimezone(UTC).isoformat()
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scorer_test_access_receipt_hash(
    *, receipt: ScorerTestAccessReceipt, scope: ScorerTestEvaluationScope
) -> str:
    """Tạo hash kiểm chứng từ toàn bộ metadata receipt đã commit."""
    if (
        type(receipt) is not ScorerTestAccessReceipt
        or type(scope) is not ScorerTestEvaluationScope
        or not isinstance(receipt.committed_at, datetime)
        or receipt.committed_at.tzinfo is None
        or receipt.committed_at.utcoffset() is None
        or not _is_sha256(receipt.current_rights_receipt_hash)
    ):
        raise ValueError("Cần receipt TEST đã commit và scope authoritative.")
    if (
        receipt.proof_id != scope.opportunity_proof_id
        or receipt.proof_hash != scope.opportunity_proof_hash
        or not _is_sha256(receipt.receipt_hash)
    ):
        raise ValueError("Receipt hash hoặc proof scope không hợp lệ.")
    return receipt.receipt_hash


def scorer_test_payload_consumption_hash(consumption: ScorerTestPayloadConsumption) -> str:
    """Trả hash one-shot do database trigger sinh từ row đã commit."""
    if type(consumption) is not ScorerTestPayloadConsumption or not _is_sha256(
        consumption.consumption_hash
    ):
        raise ValueError("Cần metadata tiêu thụ TEST typed.")
    return consumption.consumption_hash


def _bind_access_evidence(
    report: ScorerTestEvaluationReport,
    *,
    receipt: ScorerTestAccessReceipt,
    receipt_hash: str,
    consumption: ScorerTestPayloadConsumption,
    consumption_hash: str,
) -> ScorerTestEvaluationReport:
    """Ghim receipt, lần tiêu thụ và quyền hiện hành vào canonical report hash."""
    if (
        type(report) is not ScorerTestEvaluationReport
        or not _is_sha256(receipt_hash)
        or not _is_sha256(consumption_hash)
    ):
        raise ValueError("Access evidence report không hợp lệ.")
    bound = replace(
        report,
        test_access_receipt_id=receipt.receipt_id,
        test_access_receipt_hash=receipt_hash,
        test_current_rights_receipt_hash=consumption.current_rights_receipt_hash,
        test_payload_consumption_id=consumption.consumption_id,
        test_payload_commitment_hash=consumption.payload_commitment_hash,
        test_payload_consumption_hash=consumption_hash,
        test_payload_consumed_at=consumption.consumed_at.astimezone(UTC),
        report_hash="0" * 64,
    )
    return replace(
        bound,
        report_hash=hashlib.sha256(bound.canonical_json.encode("utf-8")).hexdigest(),
    )


def _payload_hash(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "ScorerTestAccessGateway",
    "ScorerTestAccessReceipt",
    "ScorerTestEvaluationAccessConflict",
    "ScorerTestEvaluationReportConflict",
    "ScorerTestEvaluationRequest",
    "ScorerTestEvaluationResult",
    "ScorerTestEvaluationService",
    "ScorerTestReportRepository",
    "ScorerTestStoredReport",
    "scorer_test_evaluation_request_fingerprint",
]
