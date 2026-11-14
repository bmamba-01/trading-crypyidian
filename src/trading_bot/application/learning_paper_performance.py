"""Ca sử dụng tạo báo cáo chẩn đoán PAPER không điều khiển bot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.learning_paper_performance_entities import (
    LearningPaperPerformanceReportRecord,
)
from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.learning_paper_performance import (
    LearningPaperPerformanceRepository,
    PaperPerformanceIdempotencyConflict,
)
from trading_bot.adapters.database.repositories.learning_scorer_current_rights import (
    LearningScorerCurrentRightsRepository,
)
from trading_bot.adapters.database.repositories.learning_scorer_runtime_targets import (
    PostgresScorerRuntimeTargetRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.domain.learning.paper_performance import (
    PaperPerformanceAssessment,
    PaperPerformanceCohort,
    PaperPerformancePolicy,
    PaperPerformanceStatus,
    assess_paper_performance,
)


@dataclass(frozen=True, slots=True)
class PaperPerformanceReportSummary:
    """Projection an toàn chỉ chứa metadata và số liệu đã tổng hợp."""

    report_id: UUID
    bot_id: UUID
    as_of: datetime
    status: PaperPerformanceStatus
    policy_version: str
    policy_hash: str
    target_fingerprint: str
    baseline_report_hash: str
    report_hash: str
    report: dict[str, Any]


class LearningPaperPerformanceService:
    """Tạo facade dùng Principal đã xác thực và workspace hiện hành."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def for_principal(self, principal: Principal) -> PrincipalLearningPaperPerformanceService:
        if type(principal) is not Principal:
            raise TypeError("principal phải đến từ ranh giới xác thực.")
        return PrincipalLearningPaperPerformanceService(self._sessions, principal)


class PrincipalLearningPaperPerformanceService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], principal: Principal) -> None:
        self._sessions = sessions
        self._principal = principal
        self._policy = PaperPerformancePolicy()

    async def list_recent(self, *, limit: int = 50) -> tuple[PaperPerformanceReportSummary, ...]:
        self._require_read()
        async with self._uow() as unit:
            rows = await LearningPaperPerformanceRepository(unit.session).list_recent(
                workspace_id=self._principal.workspace_id,
                limit=limit,
            )
            visible: list[PaperPerformanceReportSummary] = []
            grants = BotGrantRepository(unit.session)
            for row in rows:
                try:
                    await grants.require(
                        self._principal,
                        bot_id=row.bot_id,
                        permission=BotPermission.READ,
                    )
                except PermissionError:
                    continue
                visible.append(_summary_from_record(row))
            return tuple(visible)

    async def assess(
        self,
        *,
        bot_id: UUID,
        idempotency_key: UUID,
    ) -> PaperPerformanceReportSummary:
        """Đánh giá target hiện hành, lưu report bất biến và không đổi trạng thái vận hành."""
        self._require_read()
        if type(bot_id) is not UUID or type(idempotency_key) is not UUID:
            raise ValueError("bot_id và idempotency_key phải là UUID authoritative.")
        workspace_id = self._principal.workspace_id
        async with self._uow() as unit:
            session = unit.session
            await BotGrantRepository(session).require(
                self._principal,
                bot_id=bot_id,
                permission=BotPermission.READ,
            )
            repository = LearningPaperPerformanceRepository(session)
            lock_name = f"wp06-paper-performance:{workspace_id}:{idempotency_key}"
            await session.execute(
                text("SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))"),
                {"key": lock_name},
            )
            prior = await repository.get_by_idempotency_key(
                workspace_id=workspace_id,
                idempotency_key=idempotency_key,
            )
            if prior is not None:
                if prior.bot_id != bot_id:
                    raise PaperPerformanceIdempotencyConflict(
                        "Idempotency-Key đã được dùng cho bot khác."
                    )
                return _summary_from_record(prior)

            target = await PostgresScorerRuntimeTargetRepository(session).resolve_active(
                workspace_id=workspace_id
            )
            if target is None:
                raise LookupError("Workspace chưa có scorer target đang hoạt động.")
            rights = await LearningScorerCurrentRightsRepository(session).verify_deployment_rights(
                workspace_id=workspace_id,
                scorer_artifact_id=target.target.scorer_artifact_id,
                evaluation_report_id=target.target.evaluation_report_id,
            )
            if rights.fingerprint != target.source_rights_fingerprint:
                raise PermissionError("Quyền scorer hiện hành không còn khớp target.")

            fingerprint = _target_fingerprint(workspace_id, bot_id, target)
            as_of = await session.scalar(text("SELECT transaction_timestamp()"))
            if not isinstance(as_of, datetime) or as_of.tzinfo is None:
                raise RuntimeError("PostgreSQL không trả transaction timestamp có múi giờ.")
            rows = await repository.aggregate_active_target(
                workspace_id=workspace_id,
                bot_id=bot_id,
                target_fingerprint=fingerprint,
                baseline_report_id=target.target.evaluation_report_id,
                baseline_report_hash=target.target.evaluation_report_hash,
                as_of=as_of,
            )
            cohorts = tuple(_cohort_from_row(row) for row in sorted(rows, key=_cohort_order))
            assessment = assess_paper_performance(
                cohorts,
                as_of=as_of,
                policy=self._policy,
            )
            membership_digest = hashlib.sha256(
                "\n".join(item.membership_digest for item in assessment.cohorts).encode("ascii")
            ).hexdigest()
            report = _assessment_report(
                assessment,
                target_fingerprint=fingerprint,
                baseline_report_hash=target.target.evaluation_report_hash,
            )
            prior_snapshot = await repository.get_snapshot(
                workspace_id=workspace_id,
                target_fingerprint=fingerprint,
                policy_hash=self._policy.content_hash,
                as_of=as_of,
                membership_digest=membership_digest,
            )
            if prior_snapshot is not None:
                return _summary_from_record(prior_snapshot)
            canonical = json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            report_record = LearningPaperPerformanceReportRecord.from_report(
                report_id=uuid4(),
                workspace_id=workspace_id,
                bot_id=bot_id,
                baseline_report_id=target.target.evaluation_report_id,
                baseline_report_hash=target.target.evaluation_report_hash,
                policy_version=self._policy.version,
                policy_hash=self._policy.content_hash,
                target_fingerprint=fingerprint,
                membership_digest=membership_digest,
                as_of=as_of,
                status=assessment.status.value,
                idempotency_key=idempotency_key,
                report=report,
                canonical_report=canonical,
                report_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            )
            await repository.insert(report_record)
            return _summary_from_record(report_record)

    def _require_read(self) -> None:
        if not authorize(self._principal, Permission.LEARNING_MODEL_READ):
            raise PermissionError("Không đủ quyền đọc chẩn đoán scorer.")

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(
            self._sessions,
            workspace_id=self._principal.workspace_id,
            actor_membership_id=self._principal.membership_id,
        )


def _target_fingerprint(workspace_id: UUID, bot_id: UUID, target: Any) -> str:
    runtime = target.target
    payload = {
        "workspace_id": str(workspace_id),
        "bot_id": str(bot_id),
        "registry_id": str(runtime.registry_id),
        "registry_revision": runtime.registry_revision,
        "scorer_artifact_id": str(runtime.scorer_artifact_id),
        "scorer_artifact_hash": runtime.scorer_artifact_hash,
        "base_artifact_hash": target.base_artifact_hash,
        "policy_id": runtime.policy_id,
        "policy_hash": runtime.policy_hash,
        "deployment_event_id": str(target.deployment_event_id),
        "deployment_revision": target.deployment_revision,
        "evaluation_report_id": str(runtime.evaluation_report_id),
        "evaluation_report_hash": runtime.evaluation_report_hash,
        "proof_hash": runtime.proof_hash,
        "report_scope_hash": runtime.report_scope_hash,
        "feature_schema_hash": target.scorer_artifact.vector_schema_hash,
        "source_rights_fingerprint": target.source_rights_fingerprint,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cohort_order(row: Any) -> int:
    return 0 if row["cohort_name"] == "older" else 1


def _cohort_from_row(row: Any) -> PaperPerformanceCohort:
    support = (
        (-1, int(row["class_minus_one_count"])),
        (0, int(row["class_zero_count"])),
        (1, int(row["class_plus_one_count"])),
    )
    return PaperPerformanceCohort(
        window_start=row["window_start"],
        window_end=row["window_end"],
        eligible_decision_count=int(row["eligible_decision_count"]),
        covered_outcome_count=int(row["covered_outcome_count"]),
        class_support=support,
        observed_brier=(
            Decimal(str(row["observed_brier"])) if row["observed_brier"] is not None else None
        ),
        observed_log_loss=(
            Decimal(str(row["observed_log_loss"])) if row["observed_log_loss"] is not None else None
        ),
        baseline_brier=Decimal(str(row["baseline_brier"])),
        baseline_log_loss=Decimal(str(row["baseline_log_loss"])),
        membership_digest=row["membership_digest"],
    )


def _assessment_report(
    assessment: PaperPerformanceAssessment,
    *,
    target_fingerprint: str,
    baseline_report_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": "crypidian.learning.paper-performance.v1",
        "scope": "PAPER_ONLY_DIAGNOSTIC",
        "selection_bias_note": (
            "Chỉ gồm intent PAPER được scorer duyệt, risk chấp thuận và có "
            "giao dịch đóng đúng hạn; "
            "không suy diễn kết quả cho quyết định không vào lệnh hoặc lợi nhuận tương lai."
        ),
        "status": assessment.status.value,
        "reasons": list(assessment.reasons),
        "as_of": assessment.as_of.isoformat(),
        "policy_version": assessment.policy_version,
        "policy_hash": assessment.policy_hash,
        "target_fingerprint": target_fingerprint,
        "baseline": {
            "report_hash": baseline_report_hash,
            "description": "TEST baseline đã đóng băng",
        },
        "thresholds": {
            "cohort_days": 30,
            "maturity_days": 30,
            "minimum_outcomes": 1000,
            "minimum_class_support": 100,
            "minimum_coverage": "0.80",
            "maximum_brier_regression": "0.01",
            "maximum_log_loss_regression": "0.01",
        },
        "cohorts": [_cohort_report(item) for item in assessment.cohorts],
        "automation": "NO_STATE_CHANGE",
    }


def _cohort_report(cohort: PaperPerformanceCohort) -> dict[str, Any]:
    return {
        "window_start": cohort.window_start.isoformat(),
        "window_end": cohort.window_end.isoformat(),
        "eligible_decision_count": cohort.eligible_decision_count,
        "covered_outcome_count": cohort.covered_outcome_count,
        "coverage": str(cohort.coverage),
        "class_support": [
            {"class": label, "count": count} for label, count in cohort.class_support
        ],
        "observed_brier": (
            str(cohort.observed_brier) if cohort.observed_brier is not None else None
        ),
        "baseline_brier": str(cohort.baseline_brier),
        "brier_regression": (
            str(cohort.brier_regression) if cohort.brier_regression is not None else None
        ),
        "observed_log_loss": (
            str(cohort.observed_log_loss) if cohort.observed_log_loss is not None else None
        ),
        "baseline_log_loss": str(cohort.baseline_log_loss),
        "log_loss_regression": (
            str(cohort.log_loss_regression) if cohort.log_loss_regression is not None else None
        ),
        "membership_digest": cohort.membership_digest,
        "status": cohort.status.value,
        "reasons": list(cohort.reasons),
    }


def _summary_from_record(
    record: LearningPaperPerformanceReportRecord,
) -> PaperPerformanceReportSummary:
    return PaperPerformanceReportSummary(
        report_id=record.id,
        bot_id=record.bot_id,
        as_of=record.as_of,
        status=PaperPerformanceStatus(record.status),
        policy_version=record.policy_version,
        policy_hash=record.policy_hash,
        target_fingerprint=record.target_fingerprint,
        baseline_report_hash=record.baseline_report_hash,
        report_hash=record.report_hash,
        report=record.report,
    )


__all__ = [
    "LearningPaperPerformanceService",
    "PaperPerformanceReportSummary",
    "PrincipalLearningPaperPerformanceService",
]
