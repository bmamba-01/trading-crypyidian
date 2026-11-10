"""Lưu và xác minh report WP06.4 append-only theo workspace."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import LearningEvaluationReportRecord
from trading_bot.application.learning_fold_evaluation import (
    EVALUATOR_VERSION,
    WalkForwardEvaluationReport,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class LearningEvaluationReportConflict(RuntimeError):
    """Idempotency key hoặc report đã lưu không khớp payload chẩn đoán."""


@dataclass(frozen=True, slots=True)
class LearningEvaluationReportWriteResult:
    record: LearningEvaluationReportRecord
    is_duplicate: bool


class LearningEvaluationReportRepository:
    """Repository append-only; workspace/RLS và transaction do caller cung cấp."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        workspace_id: UUID,
        idempotency_key: UUID,
        report: WalkForwardEvaluationReport,
    ) -> LearningEvaluationReportWriteResult:
        """Thêm report một lần; retry cùng khóa chỉ nhận payload canonical giống hệt."""
        self._validate_identifiers(workspace_id, idempotency_key)
        payload = self.validate_report(report)
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"wp06-learning-evaluation:{workspace_id}:{idempotency_key}"},
        )
        existing = await self._session.scalar(
            select(LearningEvaluationReportRecord).where(
                LearningEvaluationReportRecord.workspace_id == workspace_id,
                LearningEvaluationReportRecord.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            self.assert_same_request(
                existing,
                workspace_id=workspace_id,
                idempotency_key=idempotency_key,
                report=report,
            )
            return LearningEvaluationReportWriteResult(existing, True)

        record = LearningEvaluationReportRecord(
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            report_hash=report.report_hash,
            manifest_hash=report.manifest_hash,
            fold_plan_hash=report.fold_plan_hash,
            split_policy_version=report.split_policy_version,
            evaluator_version=EVALUATOR_VERSION,
            evaluation_mode="diagnostic",
            candidate_completeness_verified=False,
            evaluation_cutoff=report.evaluation_cutoff,
            seed=report.seed,
            report=payload,
            canonical_report=report.canonical_json,
        )
        self._session.add(record)
        await self._session.flush()
        self.validate_record(record)
        return LearningEvaluationReportWriteResult(record, False)

    async def get_by_idempotency_key(
        self,
        *,
        workspace_id: UUID,
        idempotency_key: UUID,
    ) -> LearningEvaluationReportRecord | None:
        """Nạp report tenant-scoped và xác minh canonical/hash/metadata khi đọc lại."""
        self._validate_identifiers(workspace_id, idempotency_key)
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(LearningEvaluationReportRecord).where(
                LearningEvaluationReportRecord.workspace_id == workspace_id,
                LearningEvaluationReportRecord.idempotency_key == idempotency_key,
            )
        )
        if record is not None:
            self.validate_record(record)
        return record

    @classmethod
    def assert_same_request(
        cls,
        record: LearningEvaluationReportRecord,
        *,
        workspace_id: UUID,
        idempotency_key: UUID,
        report: WalkForwardEvaluationReport,
    ) -> None:
        """Từ chối tái dùng key cho tenant, nội dung hoặc hash khác."""
        cls._validate_identifiers(workspace_id, idempotency_key)
        expected_payload = cls.validate_report(report)
        cls.validate_record(record)
        if record.workspace_id != workspace_id or record.idempotency_key != idempotency_key:
            raise LearningEvaluationReportConflict(
                "Report idempotency workspace/key không khớp request."
            )
        if (
            record.report_hash != report.report_hash
            or record.canonical_report != report.canonical_json
            or record.report != expected_payload
            or record.manifest_hash != report.manifest_hash
            or record.fold_plan_hash != report.fold_plan_hash
            or record.split_policy_version != report.split_policy_version
            or record.evaluation_cutoff != report.evaluation_cutoff
            or record.seed != report.seed
        ):
            raise LearningEvaluationReportConflict(
                "Idempotency key đã được dùng cho report canonical khác."
            )

    @staticmethod
    def validate_report(report: WalkForwardEvaluationReport) -> dict[str, object]:
        """Kiểm cấu trúc report trước khi cấp đường persist diagnostic."""
        if type(report) is not WalkForwardEvaluationReport:
            raise LearningEvaluationReportConflict("Report WP06.4 phải có kiểu authoritative.")
        if report.candidate_completeness_verified is not False:
            raise LearningEvaluationReportConflict(
                "Report chẩn đoán không được xác nhận candidate completeness."
            )
        canonical = report.canonical_json
        try:
            payload = json.loads(canonical)
        except TypeError, json.JSONDecodeError:
            raise LearningEvaluationReportConflict(
                "Canonical report không phải JSON hợp lệ."
            ) from None
        if (
            not isinstance(payload, dict)
            or json.dumps(payload, sort_keys=True, separators=(",", ":")) != canonical
            or hashlib.sha256(canonical.encode()).hexdigest() != report.report_hash
            or payload.get("evaluation_mode") != "diagnostic"
            or payload.get("candidate_completeness_verified") is not False
            or payload.get("evaluator_version") != EVALUATOR_VERSION
            or payload.get("manifest_hash") != report.manifest_hash
            or payload.get("fold_plan_hash") != report.fold_plan_hash
            or payload.get("split_policy_version") != report.split_policy_version
            or payload.get("evaluation_cutoff") != report.evaluation_cutoff.isoformat()
            or payload.get("seed") != report.seed
        ):
            raise LearningEvaluationReportConflict(
                "Report không khớp payload/hash chẩn đoán canonical."
            )
        return payload

    @staticmethod
    def validate_record(record: LearningEvaluationReportRecord) -> None:
        """Từ chối record có JSONB, canonical bytes, hash hoặc lineage lệch nhau."""
        if type(record) is not LearningEvaluationReportRecord:
            raise LearningEvaluationReportConflict("Record WP06.4 không hợp lệ.")
        try:
            payload = json.loads(record.canonical_report)
            canonical_again = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except TypeError, json.JSONDecodeError:
            raise LearningEvaluationReportConflict("Canonical report lưu không đọc được.") from None
        if not isinstance(payload, dict):
            raise LearningEvaluationReportConflict("Report JSON/hash/lineage lưu không nhất quán.")
        if (
            record.evaluation_mode != "diagnostic"
            or record.candidate_completeness_verified is not False
            or payload.get("evaluation_mode") != "diagnostic"
            or payload.get("candidate_completeness_verified") is not False
        ):
            raise LearningEvaluationReportConflict(
                "Report lưu không thuộc chế độ chẩn đoán fail-closed."
            )
        if (
            canonical_again != record.canonical_report
            or hashlib.sha256(record.canonical_report.encode()).hexdigest() != record.report_hash
            or record.report != payload
            or payload.get("evaluator_version") != record.evaluator_version
            or record.evaluator_version != EVALUATOR_VERSION
            or payload.get("manifest_hash") != record.manifest_hash
            or payload.get("fold_plan_hash") != record.fold_plan_hash
            or payload.get("split_policy_version") != record.split_policy_version
            or payload.get("evaluation_cutoff") != record.evaluation_cutoff.isoformat()
            or payload.get("seed") != record.seed
        ):
            raise LearningEvaluationReportConflict(
                "Report JSON/hash/lineage đã lưu không nhất quán."
            )

    @staticmethod
    def _validate_identifiers(workspace_id: UUID, idempotency_key: UUID) -> None:
        if type(workspace_id) is not UUID or type(idempotency_key) is not UUID:
            raise ValueError("workspace_id và idempotency_key phải là UUID.")

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
