"""Điều phối đánh giá fold từ manifest đã lưu và ghi report chẩn đoán bất biến."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.learning_fold_outcome_verifier import (
    DatabaseFoldOutcomeVerifier,
)
from trading_bot.adapters.database.repositories.learning_evaluation_access import (
    LearningEvaluationAccessConflict,
    LearningEvaluationAccessRepository,
)
from trading_bot.adapters.database.repositories.learning_evaluation_reports import (
    LearningEvaluationReportRepository,
)
from trading_bot.application.learning_fold_evaluation import (
    WalkForwardEvaluationReport,
    build_walk_forward_evaluation_report,
    walk_forward_evaluation_report_from_canonical_json,
)
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.dataset import DatasetManifest
from trading_bot.domain.learning.evaluation import WalkForwardSplit

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class LearningEvaluationRequest:
    """Tham số fold đánh giá; manifest và outcome luôn lấy từ kho authoritative."""

    workspace_id: UUID
    manifest_hash: str
    folds: tuple[WalkForwardSplit, ...]
    evaluation_cutoff: datetime
    seed: int
    idempotency_key: UUID

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID or type(self.idempotency_key) is not UUID:
            raise ValueError("workspace_id và idempotency_key phải là UUID.")
        if not isinstance(self.manifest_hash, str) or _SHA256.fullmatch(self.manifest_hash) is None:
            raise ValueError("manifest_hash phải là SHA-256 dạng hex thường.")
        if (
            type(self.folds) is not tuple
            or not self.folds
            or any(type(fold) is not WalkForwardSplit for fold in self.folds)
        ):
            raise ValueError("folds phải là tuple không rỗng gồm WalkForwardSplit.")
        if (
            not isinstance(self.evaluation_cutoff, datetime)
            or self.evaluation_cutoff.tzinfo is None
            or self.evaluation_cutoff.utcoffset() is None
        ):
            raise ValueError("evaluation_cutoff phải có timezone.")
        if type(self.seed) is not int or not 0 <= self.seed <= 2**31 - 1:
            raise ValueError("seed phải là số nguyên từ 0 đến 2,147,483,647.")


@dataclass(frozen=True, slots=True)
class LearningEvaluationResult:
    """Kết quả đánh giá và định danh report append-only trong workspace."""

    report: WalkForwardEvaluationReport
    record_id: UUID
    is_duplicate: bool


class LearningEvaluationReportService:
    """Mở receipt trong snapshot, commit rồi mới lưu và trả report diagnostic."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def evaluate(self, request: LearningEvaluationRequest) -> LearningEvaluationResult:
        """Commit receipt evaluator trước khi payload được trả ra ngoài transaction."""
        if type(request) is not LearningEvaluationRequest:
            raise ValueError("Yêu cầu đánh giá WP06.4 không hợp lệ.")
        from trading_bot.application.learning_evaluation_access import (
            evaluation_access_fingerprint,
        )

        fingerprint = evaluation_access_fingerprint(request)
        report: WalkForwardEvaluationReport
        existing_record_id: UUID | None = None
        async with UnitOfWork(
            self._sessions,
            workspace_id=request.workspace_id,
            snapshot_repeatable_read=False,
        ) as uow:
            report_repository = LearningEvaluationReportRepository(uow.session)
            existing = await report_repository.get_by_idempotency_key(
                workspace_id=request.workspace_id,
                idempotency_key=request.idempotency_key,
            )
            access_repository = LearningEvaluationAccessRepository(uow.session)
            if existing is not None:
                receipt = await access_repository.get_for_run(
                    workspace_id=request.workspace_id,
                    evaluation_run_id=request.idempotency_key,
                )
                if (
                    receipt is None
                    or receipt.request_fingerprint != fingerprint
                    or receipt.manifest_hash != request.manifest_hash
                    or receipt.evaluation_run_id != existing.idempotency_key
                    or receipt.purpose != "diagnostic-evaluation"
                    or receipt.actor_principal != "crypidian_learning_evaluator"
                    or receipt.fold_plan_hash != existing.fold_plan_hash
                    or receipt.split_policy_version != existing.split_policy_version
                ):
                    raise LearningEvaluationAccessConflict(
                        "Report đã có nhưng receipt không khớp request authoritative."
                    )
                report = walk_forward_evaluation_report_from_canonical_json(
                    existing.canonical_report,
                    expected_report_hash=existing.report_hash,
                )
                if (
                    report.manifest_hash != request.manifest_hash
                    or report.evaluation_cutoff != request.evaluation_cutoff
                    or report.seed != request.seed
                ):
                    raise LearningEvaluationAccessConflict(
                        "Report đã lưu không khớp fingerprint evaluation request."
                    )
                existing_record_id = existing.id
            else:
                access = await access_repository.open(
                    workspace_id=request.workspace_id,
                    manifest_hash=request.manifest_hash,
                    evaluation_run_id=request.idempotency_key,
                    request_fingerprint=fingerprint,
                )
                manifest = access.manifest
                if type(manifest) is not DatasetManifest:
                    raise LookupError("Gateway không trả manifest authoritative.")
                plan = build_manifest_fold_plan(
                    manifest,
                    folds=request.folds,
                    expected_split_policy_version=access.split_policy_version,
                    evaluation_cutoff=request.evaluation_cutoff,
                )
                if plan.content_hash != access.fold_plan_hash:
                    raise LearningEvaluationAccessConflict(
                        "Fold plan không khớp approval; transaction receipt sẽ rollback."
                    )
                report = await build_walk_forward_evaluation_report(
                    manifest,
                    plan,
                    verifier=DatabaseFoldOutcomeVerifier(
                        uow.session,
                        access_receipt_id=access.receipt_id,
                        manifest=manifest,
                    ),
                    seed=request.seed,
                    access_receipt_id=access.receipt_id,
                )

        # Transaction receipt đã commit; đến đây mới trả report hoặc bắt đầu persist.
        if existing_record_id is not None:
            return LearningEvaluationResult(report, existing_record_id, True)

        async with UnitOfWork(
            self._sessions,
            workspace_id=request.workspace_id,
            snapshot_read_only=False,
        ) as uow:
            result = await LearningEvaluationReportRepository(uow.session).append(
                workspace_id=request.workspace_id,
                idempotency_key=request.idempotency_key,
                report=report,
            )
            return LearningEvaluationResult(
                report=report,
                record_id=result.record.id,
                is_duplicate=result.is_duplicate,
            )
