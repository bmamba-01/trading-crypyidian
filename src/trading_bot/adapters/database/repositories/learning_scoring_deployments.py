"""Repository append-only cho scorer deployment; active modes đang fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningModelActivePointerRecord,
    LearningModelRegistryRecord,
    LearningScorerArtifactRecord,
    LearningScorerTestEvaluationReportRecord,
    LearningScorerTestOpportunityProofRecord,
    LearningScoringDeploymentEventRecord,
    LearningScoringDeploymentPointerRecord,
)
from trading_bot.adapters.database.repositories.learning_scorer_current_rights import (
    LearningScorerCurrentRightsRepository,
)
from trading_bot.application.learning_scorer_evaluation import ScorerTestEvaluationPolicy
from trading_bot.application.scorer_runtime_policy import ScorerRuntimePolicyProvider
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentAdminSnapshot,
    ScorerDeploymentEvent,
    ScorerDeploymentHealth,
    ScorerDeploymentMode,
    ScorerDeploymentSnapshot,
    ScorerDeploymentTarget,
)


class ScorerDeploymentConflict(RuntimeError):
    """Idempotency intent, deployment revision, hay registry pointer bị đổi."""


class ScorerDeploymentRightsUnavailable(RuntimeError):
    """Không có gateway kiểm tra quyền nguồn hiện hành an toàn cho scorer."""


class LearningScoringDeploymentRepository:
    """Đọc metadata và append event scorer trong workspace UoW hiện hành.

    Runtime target đi qua SECURITY DEFINER; ORM projection ở đây chỉ dùng cho
    giao diện admin và mutation governance. Mọi write/recheck cần READ COMMITTED.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_active_scoring_deployment(self, workspace_id: UUID) -> ScorerDeploymentSnapshot:
        """Nạp target runtime từ gateway; projection admin không đủ làm căn cứ."""
        from trading_bot.adapters.database.repositories.learning_scorer_runtime_targets import (
            PostgresScorerRuntimeTargetRepository,
        )

        resolved = await PostgresScorerRuntimeTargetRepository(self._session).resolve_active(
            workspace_id=workspace_id
        )
        if resolved is None:
            return ScorerDeploymentSnapshot(
                workspace_id=workspace_id,
                mode=ScorerDeploymentMode.DISABLED,
                deployment_event_id=None,
                deployment_revision=0,
                active_pointer_version=0,
                target=None,
            )
        return ScorerDeploymentSnapshot(
            workspace_id=workspace_id,
            mode=resolved.mode,
            deployment_event_id=resolved.deployment_event_id,
            deployment_revision=resolved.deployment_revision,
            active_pointer_version=resolved.target.active_pointer_version,
            target=resolved.target,
        )

    async def load_admin_scoring_deployment(
        self, workspace_id: UUID
    ) -> ScorerDeploymentAdminSnapshot:
        """Trả CAS hiện hành kể cả mode stale; không trả target runtime dùng được."""
        await self._bind_workspace(workspace_id)
        await self._lock(workspace_id, "current-pointer")
        await self._lock_registry_pointer(workspace_id)
        pointer_result = await self._session.execute(
            select(
                LearningScoringDeploymentPointerRecord.workspace_id,
                LearningScoringDeploymentPointerRecord.event_id,
                LearningScoringDeploymentPointerRecord.revision,
                LearningScoringDeploymentPointerRecord.active_pointer_version,
                LearningScoringDeploymentPointerRecord.mode,
            ).where(LearningScoringDeploymentPointerRecord.workspace_id == workspace_id)
        )
        pointer = pointer_result.one_or_none()
        if pointer is None:
            registry_result = await self._session.execute(
                select(
                    LearningModelActivePointerRecord.workspace_id,
                    LearningModelActivePointerRecord.version,
                ).where(LearningModelActivePointerRecord.workspace_id == workspace_id)
            )
            registry_pointer = registry_result.one_or_none()
            return ScorerDeploymentAdminSnapshot(
                workspace_id=workspace_id,
                mode=ScorerDeploymentMode.DISABLED,
                deployment_event_id=None,
                deployment_revision=0,
                active_pointer_version=(
                    registry_pointer.version if registry_pointer is not None else 0
                ),
                health=ScorerDeploymentHealth.DISABLED,
            )
        registry_result = await self._session.execute(
            select(
                LearningModelActivePointerRecord.workspace_id,
                LearningModelActivePointerRecord.version,
            ).where(LearningModelActivePointerRecord.workspace_id == workspace_id)
        )
        registry_pointer = registry_result.one_or_none()
        mode = ScorerDeploymentMode(pointer.mode)
        return ScorerDeploymentAdminSnapshot(
            workspace_id=workspace_id,
            mode=mode,
            deployment_event_id=pointer.event_id,
            deployment_revision=pointer.revision,
            active_pointer_version=(
                registry_pointer.version if registry_pointer is not None else 0
            ),
            health=(
                ScorerDeploymentHealth.DISABLED
                if mode is ScorerDeploymentMode.DISABLED
                else ScorerDeploymentHealth.RIGHTS_RECHECK_REQUIRED
            ),
        )

    async def verify_active_scoring_deployment(
        self,
        *,
        workspace_id: UUID,
        expected: ScorerDeploymentSnapshot,
    ) -> ScorerDeploymentSnapshot:
        """Recheck target runtime qua gateway trong decision UoW.

        Khi có mode scorer, caller bắt buộc dùng transaction PostgreSQL
        READ COMMITTED. Gateway giữ deployment, registry và source advisory lock
        đến commit cùng BotDecision/score.
        """
        if type(expected) is not ScorerDeploymentSnapshot:
            raise TypeError("expected phải là snapshot deployment scorer đã resolve.")
        if expected.workspace_id != workspace_id:
            raise ScorerDeploymentConflict("Workspace snapshot scorer không khớp UoW.")
        isolation = await self._session.scalar(text("SHOW transaction_isolation"))
        if str(isolation).lower() != "read committed":
            raise ScorerDeploymentConflict(
                "Runtime scorer yêu cầu transaction READ COMMITTED để recheck quyền hiện hành."
            )
        current = await self.load_active_scoring_deployment(workspace_id)
        if current != expected:
            raise ScorerDeploymentConflict("Deployment scorer hoặc Task6 pointer đã đổi.")
        return current

    async def append_disabled_event(
        self,
        *,
        workspace_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        expected_pointer_version: int,
        expected_deployment_revision: int,
        idempotency_key: UUID,
        reason: str,
    ) -> ScorerDeploymentEvent:
        """Ghi event DISABLED; API không được tự cung cấp actor hoặc target."""
        if type(workspace_id) is not UUID or type(actor_membership_id) is not UUID:
            raise ValueError("Workspace và actor phải là UUID từ Principal.")
        if type(idempotency_key) is not UUID:
            raise ValueError("idempotency_key phải là UUID.")
        if type(expected_pointer_version) is not int or expected_pointer_version < 0:
            raise ValueError("expected_pointer_version không hợp lệ.")
        if type(expected_deployment_revision) is not int or expected_deployment_revision < 0:
            raise ValueError("expected_deployment_revision không hợp lệ.")
        await self._bind_workspace(workspace_id)

        db_created_at = await self._session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        if db_created_at is None:
            raise ScorerDeploymentConflict("Không lấy được thời gian chuẩn PostgreSQL.")

        candidate = ScorerDeploymentEvent(
            event_id=uuid4(),
            workspace_id=workspace_id,
            revision=expected_deployment_revision + 1,
            mode=ScorerDeploymentMode.DISABLED,
            target=None,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=expected_pointer_version,
            expected_deployment_revision=expected_deployment_revision,
            reason=reason,
            idempotency_key=idempotency_key,
            previous_event_id=None,
            previous_event_hash=None,
            created_at=db_created_at.astimezone(UTC),
        )
        await self._lock(workspace_id, f"idempotency:{idempotency_key}")
        prior = await self._session.scalar(
            select(LearningScoringDeploymentEventRecord).where(
                LearningScoringDeploymentEventRecord.workspace_id == workspace_id,
                LearningScoringDeploymentEventRecord.idempotency_key == idempotency_key,
            )
        )
        if prior is not None:
            if prior.intent_hash != candidate.intent_hash:
                raise ScorerDeploymentConflict(
                    "Idempotency key đã được dùng cho ý định deployment khác."
                )
            return _event_snapshot(prior)

        await self._lock(workspace_id, "current-pointer")
        current_pointer = await self._session.scalar(
            select(LearningScoringDeploymentPointerRecord).where(
                LearningScoringDeploymentPointerRecord.workspace_id == workspace_id
            )
        )
        if current_pointer is None:
            raise ScorerDeploymentConflict("Chưa khởi tạo scorer deployment pointer.")
        if current_pointer.revision != expected_deployment_revision:
            raise ScorerDeploymentConflict("Deployment scorer hoặc Task6 pointer CAS đã cũ.")

        registry_pointer = await self._session.scalar(
            select(LearningModelActivePointerRecord).where(
                LearningModelActivePointerRecord.workspace_id == workspace_id
            )
        )
        actual_pointer_version = registry_pointer.version if registry_pointer is not None else 0
        if actual_pointer_version != expected_pointer_version:
            raise ScorerDeploymentConflict("Task6 active pointer CAS đã cũ.")

        prior_event: LearningScoringDeploymentEventRecord | None = None
        if current_pointer.event_id is not None:
            prior_event = await self._session.scalar(
                select(LearningScoringDeploymentEventRecord).where(
                    LearningScoringDeploymentEventRecord.workspace_id == workspace_id,
                    LearningScoringDeploymentEventRecord.id == current_pointer.event_id,
                )
            )
            if prior_event is None:
                raise ScorerDeploymentConflict("Deployment pointer predecessor không tồn tại.")
        event = ScorerDeploymentEvent(
            event_id=candidate.event_id,
            workspace_id=workspace_id,
            revision=candidate.revision,
            mode=candidate.mode,
            target=None,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=expected_pointer_version,
            expected_deployment_revision=expected_deployment_revision,
            reason=reason,
            idempotency_key=idempotency_key,
            previous_event_id=current_pointer.event_id,
            previous_event_hash=prior_event.event_hash if prior_event is not None else None,
            created_at=candidate.created_at,
        )
        self._session.add(LearningScoringDeploymentEventRecord.from_domain(event))
        await self._session.flush()
        return event

    async def append_active_event(
        self,
        *,
        workspace_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        mode: ScorerDeploymentMode,
        evaluation_report_id: UUID,
        expected_pointer_version: int,
        expected_deployment_revision: int,
        idempotency_key: UUID,
        reason: str,
        policy_provider: ScorerRuntimePolicyProvider,
    ) -> ScorerDeploymentEvent:
        """Phân giải đúng report đã chọn rồi append target sau khi recheck policy/quyền."""
        if mode not in {ScorerDeploymentMode.SHADOW, ScorerDeploymentMode.PAPER_GATE}:
            raise ValueError("append_active_event chỉ nhận SHADOW hoặc PAPER_GATE.")
        if any(type(value) is not UUID for value in (workspace_id, actor_membership_id)):
            raise ValueError("Workspace và actor phải là UUID authoritative.")
        if type(evaluation_report_id) is not UUID or type(idempotency_key) is not UUID:
            raise ValueError("Report và idempotency key phải là UUID.")
        if type(expected_pointer_version) is not int or expected_pointer_version < 0:
            raise ValueError("expected_pointer_version không hợp lệ.")
        if type(expected_deployment_revision) is not int or expected_deployment_revision < 0:
            raise ValueError("expected_deployment_revision không hợp lệ.")
        if not callable(getattr(policy_provider, "get_policy", None)):
            raise ScorerDeploymentRightsUnavailable("Scorer rights gateway thiếu policy provider.")
        isolation = await self._session.scalar(text("SHOW transaction_isolation"))
        if str(isolation).lower() != "read committed":
            raise ScorerDeploymentConflict("Kích hoạt scorer cần transaction READ COMMITTED.")
        await self._bind_workspace(workspace_id)
        await self._lock(workspace_id, f"idempotency:{idempotency_key}")
        prior = await self._session.scalar(
            select(LearningScoringDeploymentEventRecord).where(
                LearningScoringDeploymentEventRecord.workspace_id == workspace_id,
                LearningScoringDeploymentEventRecord.idempotency_key == idempotency_key,
            )
        )
        if prior is not None:
            stored_event = _event_snapshot(prior)
            if not _active_event_matches_request(
                stored_event,
                workspace_id=workspace_id,
                actor_membership_id=actor_membership_id,
                mode=mode,
                evaluation_report_id=evaluation_report_id,
                expected_pointer_version=expected_pointer_version,
                expected_deployment_revision=expected_deployment_revision,
                reason=reason,
            ):
                raise ScorerDeploymentConflict(
                    "Idempotency key đã được dùng cho ý định deployment khác."
                )
            return stored_event

        await self._lock(workspace_id, "current-pointer")
        await self._lock_registry_pointer(workspace_id)

        current_pointer = await self._session.scalar(
            select(LearningScoringDeploymentPointerRecord)
            .where(LearningScoringDeploymentPointerRecord.workspace_id == workspace_id)
            .with_for_update()
        )
        if current_pointer is None:
            raise ScorerDeploymentConflict("Chưa khởi tạo scorer deployment pointer.")
        if current_pointer.revision != expected_deployment_revision:
            raise ScorerDeploymentConflict("Deployment scorer CAS đã cũ.")

        registry_pointer = await self._session.scalar(
            select(LearningModelActivePointerRecord).where(
                LearningModelActivePointerRecord.workspace_id == workspace_id
            )
        )
        actual_pointer_version = registry_pointer.version if registry_pointer is not None else 0
        if actual_pointer_version != expected_pointer_version:
            raise ScorerDeploymentConflict("Task6 active pointer CAS đã cũ.")
        if registry_pointer is None or registry_pointer.registry_id is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway không tìm thấy model registry active."
            )
        registry_id = registry_pointer.registry_id
        await self._lock_registry_entry(workspace_id, registry_id)
        registry = await self._session.scalar(
            select(LearningModelRegistryRecord).where(
                LearningModelRegistryRecord.workspace_id == workspace_id,
                LearningModelRegistryRecord.id == registry_id,
            )
        )
        if registry is None or registry.state != "paper_active":
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway cần registry paper_active hiện hành."
            )

        report = await self._session.scalar(
            select(LearningScorerTestEvaluationReportRecord).where(
                LearningScorerTestEvaluationReportRecord.workspace_id == workspace_id,
                LearningScorerTestEvaluationReportRecord.id == evaluation_report_id,
            )
        )
        if report is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway không tìm thấy report đã chọn trong workspace."
            )
        artifact_rows = await self._session.scalars(
            select(LearningScorerArtifactRecord).where(
                LearningScorerArtifactRecord.workspace_id == workspace_id,
                LearningScorerArtifactRecord.artifact_hash == report.scorer_artifact_hash,
            )
        )
        artifact_matches = tuple(artifact_rows.all())
        if len(artifact_matches) != 1:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway không phân giải được đúng một artifact scorer."
            )
        artifact = artifact_matches[0]
        proof = await self._session.scalar(
            select(LearningScorerTestOpportunityProofRecord).where(
                LearningScorerTestOpportunityProofRecord.workspace_id == workspace_id,
                LearningScorerTestOpportunityProofRecord.id == report.proof_id,
            )
        )
        if proof is None:
            raise ScorerDeploymentRightsUnavailable(
                "Scorer rights gateway không tìm thấy proof TEST của report."
            )
        report_scope_hash = _report_scope_hash(report.report)
        if report.evaluation_status not in {
            "ACCEPTED_SHADOW_ONLY",
            "ACCEPTED_PAPER_GATE_ELIGIBLE",
        }:
            raise ScorerDeploymentRightsUnavailable(
                "Report TEST chưa ACCEPTED cho scorer deployment."
            )
        if mode is ScorerDeploymentMode.PAPER_GATE and (
            report.evaluation_status != "ACCEPTED_PAPER_GATE_ELIGIBLE"
            or report.candidate_completeness_verified is not True
            or report.scored_candidate_coverage_verified is not True
        ):
            raise ScorerDeploymentRightsUnavailable(
                "PAPER_GATE cần report, completeness và coverage đã được chấp nhận."
            )

        rights = await LearningScorerCurrentRightsRepository(
            self._session
        ).verify_deployment_rights(
            workspace_id=workspace_id,
            scorer_artifact_id=artifact.id,
            evaluation_report_id=report.id,
        )
        target = _active_target_from_resolved(
            workspace_id=workspace_id,
            mode=mode,
            active_pointer_version=actual_pointer_version,
            registry=registry,
            artifact=artifact,
            report=report,
            proof=proof,
            report_scope_hash=report_scope_hash,
            rights=rights,
            policy_provider=policy_provider,
        )

        db_created_at = await self._session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        if db_created_at is None:
            raise ScorerDeploymentConflict("Không lấy được thời gian chuẩn PostgreSQL.")
        candidate = ScorerDeploymentEvent(
            event_id=uuid4(),
            workspace_id=workspace_id,
            revision=expected_deployment_revision + 1,
            mode=mode,
            target=target,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            mfa_method="TOTP",
            expected_pointer_version=expected_pointer_version,
            expected_deployment_revision=expected_deployment_revision,
            reason=reason,
            idempotency_key=idempotency_key,
            previous_event_id=None,
            previous_event_hash=None,
            created_at=db_created_at.astimezone(UTC),
        )
        prior_event: LearningScoringDeploymentEventRecord | None = None
        if current_pointer.event_id is not None:
            prior_event = await self._session.scalar(
                select(LearningScoringDeploymentEventRecord).where(
                    LearningScoringDeploymentEventRecord.workspace_id == workspace_id,
                    LearningScoringDeploymentEventRecord.id == current_pointer.event_id,
                )
            )
            if prior_event is None:
                raise ScorerDeploymentConflict("Deployment pointer predecessor không tồn tại.")
        event = ScorerDeploymentEvent(
            event_id=candidate.event_id,
            workspace_id=workspace_id,
            revision=candidate.revision,
            mode=candidate.mode,
            target=candidate.target,
            actor_membership_id=candidate.actor_membership_id,
            mfa_verified_at=candidate.mfa_verified_at,
            mfa_method=candidate.mfa_method,
            expected_pointer_version=candidate.expected_pointer_version,
            expected_deployment_revision=candidate.expected_deployment_revision,
            reason=candidate.reason,
            idempotency_key=candidate.idempotency_key,
            previous_event_id=current_pointer.event_id,
            previous_event_hash=prior_event.event_hash if prior_event is not None else None,
            created_at=candidate.created_at,
        )
        self._session.add(LearningScoringDeploymentEventRecord.from_domain(event))
        await self._session.flush()
        return event

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def _lock(self, workspace_id: UUID, key: str) -> None:
        await self._session.execute(
            text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))"),
            {"key": f"learning-scoring-deployment:{workspace_id}:{key}"},
        )

    async def _lock_registry_pointer(self, workspace_id: UUID) -> None:
        """Giữ model pointer ổn định theo thứ tự scorer pointer rồi model pointer."""
        await self._session.execute(
            text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))"),
            {"key": f"learning-model-registry:{workspace_id}:active-pointer"},
        )

    async def _lock_registry_entry(self, workspace_id: UUID, registry_id: UUID) -> None:
        await self._session.execute(
            text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))"),
            {"key": f"learning-model-registry:{workspace_id}:registry-entry:{registry_id}"},
        )


def _report_scope_hash(report: object) -> str:
    if type(report) is not dict:
        raise ScorerDeploymentRightsUnavailable("Scorer report không có aggregate scope.")
    value = report.get("source_scope_hash")
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ScorerDeploymentRightsUnavailable("Scorer report source scope không hợp lệ.")
    return value


def _active_target_from_resolved(
    *,
    workspace_id: UUID,
    mode: ScorerDeploymentMode,
    active_pointer_version: int,
    registry: LearningModelRegistryRecord,
    artifact: LearningScorerArtifactRecord,
    report: LearningScorerTestEvaluationReportRecord,
    proof: LearningScorerTestOpportunityProofRecord,
    report_scope_hash: str,
    rights: object,
    policy_provider: ScorerRuntimePolicyProvider,
) -> ScorerDeploymentTarget:
    """Buộc target duy nhất vào registry, report/proof, quyền và policy hiện hành."""
    if any(
        value is None
        for value in (
            registry.id,
            registry.revision,
            artifact.id,
            artifact.artifact_hash,
            report.id,
            report.report_hash,
            report.evaluation_status,
            report.policy_version,
            report.policy_hash,
            report.current_rights_receipt_hash,
            proof.id,
            proof.proof_hash,
            proof.source_scope_hash,
        )
    ):
        raise ScorerDeploymentRightsUnavailable(
            "Scorer target thiếu định danh hoặc hash authoritative bắt buộc."
        )
    status_accepted = report.evaluation_status in {
        "ACCEPTED_SHADOW_ONLY",
        "ACCEPTED_PAPER_GATE_ELIGIBLE",
    }
    eligible = (
        getattr(rights, "shadow_eligible", False)
        if mode is ScorerDeploymentMode.SHADOW
        else getattr(rights, "paper_gate_eligible", False)
    )
    if (
        type(workspace_id) is not UUID
        or not status_accepted
        or registry.workspace_id != workspace_id
        or registry.state != "paper_active"
        or artifact.workspace_id != workspace_id
        or report.workspace_id != workspace_id
        or proof.workspace_id != workspace_id
        or artifact.state != "ready_for_evaluation"
        or artifact.base_artifact_id != registry.artifact_id
        or artifact.base_artifact_hash != registry.artifact_hash
        or artifact.artifact_hash != report.scorer_artifact_hash
        or report.base_artifact_hash != registry.artifact_hash
        or report.proof_id != proof.id
        or proof.proof_hash != report.proof_hash
        or proof.source_scope_hash != report_scope_hash
        or proof.completeness_verified != report.candidate_completeness_verified
        or not eligible
        or getattr(rights, "workspace_id", None) != workspace_id
        or getattr(rights, "scorer_artifact_id", None) != artifact.id
        or getattr(rights, "evaluation_report_id", None) != report.id
        or getattr(rights, "artifact_hash", None) != artifact.artifact_hash
        or getattr(rights, "report_hash", None) != report.report_hash
        or getattr(rights, "proof_hash", None) != report.proof_hash
        or getattr(rights, "policy_hash", None) != report.policy_hash
        or getattr(rights, "evaluation_status", None) != report.evaluation_status
        or getattr(rights, "candidate_completeness_verified", None)
        != report.candidate_completeness_verified
        or getattr(rights, "scored_candidate_coverage_verified", None)
        != report.scored_candidate_coverage_verified
        or getattr(rights, "source_scope_hash", None) != report_scope_hash
    ):
        raise ScorerDeploymentRightsUnavailable(
            "Scorer rights gateway từ chối report, proof, registry hoặc quyền hiện hành."
        )
    if mode is ScorerDeploymentMode.PAPER_GATE and (
        report.evaluation_status != "ACCEPTED_PAPER_GATE_ELIGIBLE"
        or report.candidate_completeness_verified is not True
        or report.scored_candidate_coverage_verified is not True
    ):
        raise ScorerDeploymentRightsUnavailable(
            "PAPER_GATE cần report, completeness và coverage đã được chấp nhận."
        )
    policy = policy_provider.get_policy(
        workspace_id=workspace_id,
        policy_id=report.policy_version,
        expected_policy_hash=report.policy_hash,
    )
    if (
        type(policy) is not ScorerTestEvaluationPolicy
        or policy.policy_version != report.policy_version
        or policy.policy_hash != report.policy_hash
    ):
        raise ScorerDeploymentRightsUnavailable(
            "Policy cấu hình không khớp version/hash của report TEST."
        )
    target = ScorerDeploymentTarget(
        registry_id=registry.id,
        registry_revision=registry.revision,
        active_pointer_version=active_pointer_version,
        scorer_artifact_id=artifact.id,
        scorer_artifact_hash=artifact.artifact_hash,
        evaluation_report_id=report.id,
        evaluation_report_hash=report.report_hash,
        evaluation_status=report.evaluation_status,
        proof_id=proof.id,
        proof_hash=proof.proof_hash,
        candidate_completeness_verified=report.candidate_completeness_verified,
        report_scope_hash=report_scope_hash,
        proof_scope_hash=proof.source_scope_hash,
        policy_id=report.policy_version,
        policy_hash=report.policy_hash,
        current_rights_receipt_hash=report.current_rights_receipt_hash,
    )
    try:
        target.require_mode(mode)
    except ValueError as error:
        raise ScorerDeploymentRightsUnavailable(
            "Scorer rights gateway từ chối target report theo mode đã chọn."
        ) from error
    return target


def _active_event_matches_request(
    event: ScorerDeploymentEvent,
    *,
    workspace_id: UUID,
    actor_membership_id: UUID,
    mode: ScorerDeploymentMode,
    evaluation_report_id: UUID,
    expected_pointer_version: int,
    expected_deployment_revision: int,
    reason: str,
) -> bool:
    """So ý định caller với event đã persist, không re-authorize retry không ghi mới."""
    target = event.target
    return (
        target is not None
        and event.workspace_id == workspace_id
        and event.actor_membership_id == actor_membership_id
        and event.mode is mode
        and target.evaluation_report_id == evaluation_report_id
        and event.expected_pointer_version == expected_pointer_version
        and event.expected_deployment_revision == expected_deployment_revision
        and event.reason == reason
    )


def _event_snapshot(row: LearningScoringDeploymentEventRecord) -> ScorerDeploymentEvent:
    target = None
    if row.mode != ScorerDeploymentMode.DISABLED.value:
        if (
            row.registry_id is None
            or row.registry_revision is None
            or row.scorer_artifact_id is None
            or row.scorer_artifact_hash is None
            or row.evaluation_report_id is None
            or row.evaluation_report_hash is None
            or row.evaluation_status is None
            or row.proof_id is None
            or row.proof_hash is None
            or row.candidate_completeness_verified is None
            or row.report_scope_hash is None
            or row.proof_scope_hash is None
            or row.policy_id is None
            or row.policy_hash is None
            or row.current_rights_receipt_hash is None
        ):
            raise ScorerDeploymentRightsUnavailable(
                "Sự kiện active scorer thiếu metadata target authoritative."
            )
        target = ScorerDeploymentTarget(
            registry_id=row.registry_id,
            registry_revision=row.registry_revision,
            active_pointer_version=row.active_pointer_version,
            scorer_artifact_id=row.scorer_artifact_id,
            scorer_artifact_hash=row.scorer_artifact_hash,
            evaluation_report_id=row.evaluation_report_id,
            evaluation_report_hash=row.evaluation_report_hash,
            evaluation_status=row.evaluation_status,
            proof_id=row.proof_id,
            proof_hash=row.proof_hash,
            candidate_completeness_verified=row.candidate_completeness_verified,
            report_scope_hash=row.report_scope_hash,
            proof_scope_hash=row.proof_scope_hash,
            policy_id=row.policy_id,
            policy_hash=row.policy_hash,
            current_rights_receipt_hash=row.current_rights_receipt_hash,
        )
    event = ScorerDeploymentEvent(
        event_id=row.id,
        workspace_id=row.workspace_id,
        revision=row.revision,
        mode=ScorerDeploymentMode(row.mode),
        target=target,
        actor_membership_id=row.actor_membership_id,
        mfa_verified_at=row.mfa_verified_at,
        mfa_method=row.mfa_method,
        expected_pointer_version=row.expected_pointer_version,
        expected_deployment_revision=row.expected_deployment_revision,
        reason=row.reason,
        idempotency_key=row.idempotency_key,
        previous_event_id=row.previous_event_id,
        previous_event_hash=row.previous_event_hash,
        created_at=row.created_at,
    )
    if event.event_hash != row.event_hash or event.intent_hash != row.intent_hash:
        raise ScorerDeploymentConflict("Deployment event hash không khớp nội dung đã lưu.")
    return event


__all__ = [
    "LearningScoringDeploymentRepository",
    "ScorerDeploymentConflict",
    "ScorerDeploymentRightsUnavailable",
]
