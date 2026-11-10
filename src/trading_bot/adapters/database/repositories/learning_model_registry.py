"""Repository tenant-scoped cho registry, audit events và active pointer CAS."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    LearningEvaluationReportRecord,
    LearningModelActivePointerRecord,
    LearningModelRegistryEventRecord,
    LearningModelRegistryRecord,
    LearningTrainingArtifactRecord,
)
from trading_bot.adapters.database.repositories.learning_training_artifacts import (
    LearningTrainingArtifactRepository,
)
from trading_bot.domain.learning.model_registry import (
    LearningModelActivePointer,
    LearningModelRegistryAction,
    LearningModelRegistryEntry,
    LearningModelRegistryEvent,
    LearningModelRegistryState,
    LearningModelTransitionRejected,
    transition_learning_model_state,
)


class LearningModelRegistryConflict(RuntimeError):
    """Artifact, state, pointer hoặc idempotency key xung đột."""


class LearningModelRegistryRepository:
    """Mỗi phương thức dùng transaction/workspace đã bind bởi UnitOfWork."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_entries(self, *, workspace_id: UUID) -> tuple[LearningModelRegistryEntry, ...]:
        await self._bind_workspace(workspace_id)
        rows = (
            await self._session.scalars(
                select(LearningModelRegistryRecord)
                .where(LearningModelRegistryRecord.workspace_id == workspace_id)
                .order_by(
                    LearningModelRegistryRecord.created_at.desc(),
                    LearningModelRegistryRecord.id,
                )
            )
        ).all()
        return tuple(_entry_snapshot(row) for row in rows)

    async def get_entry(
        self, *, workspace_id: UUID, registry_id: UUID
    ) -> LearningModelRegistryEntry:
        await self._bind_workspace(workspace_id)
        row = await self._session.scalar(
            select(LearningModelRegistryRecord).where(
                LearningModelRegistryRecord.workspace_id == workspace_id,
                LearningModelRegistryRecord.id == registry_id,
            )
        )
        if row is None:
            raise LookupError("Không tìm thấy model registry entry.")
        return _entry_snapshot(row)

    async def list_events(
        self, *, workspace_id: UUID, registry_id: UUID
    ) -> tuple[LearningModelRegistryEvent, ...]:
        await self._bind_workspace(workspace_id)
        await self._require_entry(workspace_id, registry_id, lock=False)
        rows = (
            await self._session.scalars(
                select(LearningModelRegistryEventRecord)
                .where(
                    LearningModelRegistryEventRecord.workspace_id == workspace_id,
                    LearningModelRegistryEventRecord.registry_id == registry_id,
                )
                .order_by(LearningModelRegistryEventRecord.sequence)
            )
        ).all()
        return tuple(_event_snapshot(row) for row in rows)

    async def get_active_pointer(self, *, workspace_id: UUID) -> LearningModelActivePointer:
        await self._bind_workspace(workspace_id)
        row = await self._session.scalar(
            select(LearningModelActivePointerRecord).where(
                LearningModelActivePointerRecord.workspace_id == workspace_id
            )
        )
        if row is None:
            return LearningModelActivePointer(
                workspace_id=workspace_id,
                registry_id=None,
                version=0,
                updated_by_membership_id=None,
                changed_at=datetime(1970, 1, 1, tzinfo=UTC),
            )
        return _pointer_snapshot(row)

    async def register_artifact(
        self,
        *,
        workspace_id: UUID,
        artifact_id: UUID,
        actor_membership_id: UUID,
        idempotency_key: UUID,
        policy_version: str,
        reason: str,
    ) -> LearningModelRegistryEntry:
        _validate_ids(workspace_id, artifact_id, actor_membership_id, idempotency_key)
        reason = _validate_reason(reason)
        await self._bind_workspace(workspace_id)
        await self._lock(workspace_id, f"model-registry-key:{idempotency_key}")
        existing_key = await self._find_event_by_key(workspace_id, idempotency_key)
        if existing_key is not None:
            existing_entry = await self._require_entry(
                workspace_id, existing_key.registry_id, lock=False
            )
            if (
                existing_key.action == LearningModelRegistryAction.REGISTER.value
                and existing_entry.artifact_id == artifact_id
                and existing_key.actor_membership_id == actor_membership_id
                and existing_key.reason == reason
                and existing_key.policy_version == policy_version
            ):
                return _entry_snapshot(existing_entry)
            raise LearningModelRegistryConflict("Idempotency key đã gắn thao tác registry khác.")

        artifact = await self._session.scalar(
            select(LearningTrainingArtifactRecord).where(
                LearningTrainingArtifactRecord.workspace_id == workspace_id,
                LearningTrainingArtifactRecord.id == artifact_id,
            )
        )
        if artifact is None:
            raise LookupError("Không tìm thấy artifact trong workspace hiện hành.")
        LearningTrainingArtifactRepository.validate_record(artifact)
        await self._lock(workspace_id, f"artifact:{artifact_id}")
        existing = await self._session.scalar(
            select(LearningModelRegistryRecord).where(
                LearningModelRegistryRecord.workspace_id == workspace_id,
                LearningModelRegistryRecord.artifact_id == artifact_id,
            )
        )
        if existing is not None:
            raise LearningModelRegistryConflict("Artifact đã được đăng ký.")

        registry_id = uuid4()
        row = LearningModelRegistryRecord(
            id=registry_id,
            workspace_id=workspace_id,
            artifact_id=artifact.id,
            artifact_hash=artifact.artifact_hash,
            manifest_hash=artifact.manifest_hash,
            fold_plan_hash=artifact.fold_plan_hash,
            approval_hash=artifact.approval_hash,
            evaluation_report_id=None,
            evaluation_report_hash=None,
            state=LearningModelRegistryState.DRAFT.value,
            revision=1,
            created_by_membership_id=actor_membership_id,
        )
        self._session.add(row)
        await self._session.flush()
        self._session.add(
            _event_row(
                workspace_id=workspace_id,
                registry_id=registry_id,
                sequence=1,
                action=LearningModelRegistryAction.REGISTER,
                from_state=None,
                to_state=LearningModelRegistryState.DRAFT,
                actor_membership_id=actor_membership_id,
                policy_version=policy_version,
                reason_code="artifact_registered",
                reason=reason,
                idempotency_key=idempotency_key,
                mfa_verified_at=None,
                expected_revision=None,
                expected_pointer_version=None,
                evaluation_report_id=None,
                evaluation_report_hash=None,
            )
        )
        await self._session.flush()
        await self._session.refresh(row)
        return _entry_snapshot(row)

    async def attach_evaluation(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        evaluation_report_id: UUID,
        evaluation_report_hash: str,
        actor_membership_id: UUID,
        expected_revision: int,
        idempotency_key: UUID,
        policy_version: str,
        reason: str,
    ) -> LearningModelRegistryEntry:
        return await self._transition(
            workspace_id=workspace_id,
            registry_id=registry_id,
            action=LearningModelRegistryAction.EVALUATE,
            actor_membership_id=actor_membership_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            policy_version=policy_version,
            reason_code="evaluation_attached",
            reason=reason,
            evaluation_report_id=evaluation_report_id,
            evaluation_report_hash=evaluation_report_hash,
        )

    async def approve(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        expected_revision: int,
        idempotency_key: UUID,
        policy_version: str,
        reason: str,
    ) -> LearningModelRegistryEntry:
        return await self._transition(
            workspace_id=workspace_id,
            registry_id=registry_id,
            action=LearningModelRegistryAction.APPROVE,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            policy_version=policy_version,
            reason_code="approved_for_paper",
            reason=reason,
        )

    async def reject(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        expected_revision: int,
        idempotency_key: UUID,
        policy_version: str,
        reason_code: str,
        reason: str,
    ) -> LearningModelRegistryEntry:
        return await self._transition(
            workspace_id=workspace_id,
            registry_id=registry_id,
            action=LearningModelRegistryAction.REJECT,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            policy_version=policy_version,
            reason_code=reason_code,
            reason=reason,
        )

    async def retire(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        expected_revision: int,
        idempotency_key: UUID,
        policy_version: str,
        reason: str,
    ) -> LearningModelRegistryEntry:
        _validate_ids(workspace_id, registry_id, actor_membership_id, idempotency_key)
        await self._bind_workspace(workspace_id)
        await self._lock(workspace_id, "active-pointer")
        existing = await self._find_event_by_key(workspace_id, idempotency_key)
        if existing is None:
            active = await self._session.scalar(
                select(LearningModelActivePointerRecord).where(
                    LearningModelActivePointerRecord.workspace_id == workspace_id
                )
            )
            if active is not None and active.registry_id == registry_id:
                raise LearningModelRegistryConflict(
                    "Không thể retire model đang active nếu chưa thay active pointer."
                )
        return await self._transition(
            workspace_id=workspace_id,
            registry_id=registry_id,
            action=LearningModelRegistryAction.RETIRE,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            policy_version=policy_version,
            reason_code="model_retired",
            reason=reason,
        )

    async def activate(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        expected_revision: int,
        expected_pointer_version: int,
        expected_current_registry_id: UUID | None,
        idempotency_key: UUID,
        policy_version: str,
        reason: str,
    ) -> tuple[LearningModelRegistryEntry, LearningModelActivePointer]:
        return await self._change_active_pointer(
            workspace_id=workspace_id,
            registry_id=registry_id,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            expected_revision=expected_revision,
            expected_pointer_version=expected_pointer_version,
            expected_current_registry_id=expected_current_registry_id,
            idempotency_key=idempotency_key,
            policy_version=policy_version,
            reason=reason,
            action=LearningModelRegistryAction.ACTIVATE,
        )

    async def rollback(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        expected_revision: int,
        expected_pointer_version: int,
        expected_current_registry_id: UUID | None,
        idempotency_key: UUID,
        policy_version: str,
        reason: str,
    ) -> tuple[LearningModelRegistryEntry, LearningModelActivePointer]:
        return await self._change_active_pointer(
            workspace_id=workspace_id,
            registry_id=registry_id,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            expected_revision=expected_revision,
            expected_pointer_version=expected_pointer_version,
            expected_current_registry_id=expected_current_registry_id,
            idempotency_key=idempotency_key,
            policy_version=policy_version,
            reason=reason,
            action=LearningModelRegistryAction.ROLLBACK,
        )

    async def _change_active_pointer(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        actor_membership_id: UUID,
        mfa_verified_at: datetime,
        expected_revision: int,
        expected_pointer_version: int,
        expected_current_registry_id: UUID | None,
        idempotency_key: UUID,
        policy_version: str,
        reason: str,
        action: LearningModelRegistryAction,
    ) -> tuple[LearningModelRegistryEntry, LearningModelActivePointer]:
        _validate_ids(workspace_id, registry_id, actor_membership_id, idempotency_key)
        if (
            expected_current_registry_id is not None
            and type(expected_current_registry_id) is not UUID
        ):
            raise ValueError("expected_current_registry_id phải là UUID hoặc None.")
        if type(expected_pointer_version) is not int or expected_pointer_version < 0:
            raise ValueError("expected_pointer_version phải là số nguyên không âm.")
        reason = _validate_reason(reason)
        await self._bind_workspace(workspace_id)
        await self._lock(workspace_id, f"model-registry-key:{idempotency_key}")
        await self._lock(workspace_id, "active-pointer")
        existing = await self._find_event_by_key(workspace_id, idempotency_key)
        if existing is not None:
            target = await self._require_entry(workspace_id, existing.registry_id, lock=False)
            if (
                existing.action == action.value
                and existing.registry_id == registry_id
                and existing.actor_membership_id == actor_membership_id
                and existing.reason == reason
                and existing.policy_version == policy_version
                and existing.expected_revision == expected_revision
                and existing.mfa_verified_at == mfa_verified_at
                and existing.expected_pointer_version == expected_pointer_version
            ):
                return _entry_snapshot(target), await self.get_active_pointer(
                    workspace_id=workspace_id
                )
            raise LearningModelRegistryConflict("Idempotency key đã gắn thao tác registry khác.")

        pointer = await self._get_or_create_pointer(workspace_id, lock=True)
        if (
            pointer.version != expected_pointer_version
            or pointer.registry_id != expected_current_registry_id
        ):
            raise LearningModelRegistryConflict("Active pointer đã đổi; cần tải lại revision mới.")

        target = await self._require_entry(workspace_id, registry_id, lock=True)
        previous = (
            await self._require_entry(workspace_id, pointer.registry_id, lock=True)
            if pointer.registry_id is not None
            else None
        )
        if previous is not None:
            await self._transition(
                workspace_id=workspace_id,
                registry_id=previous.id,
                action=LearningModelRegistryAction.RETIRE,
                actor_membership_id=actor_membership_id,
                mfa_verified_at=mfa_verified_at,
                expected_revision=previous.revision,
                idempotency_key=uuid4(),
                policy_version=policy_version,
                reason_code="replaced_by_approved_model",
                reason=reason,
                expected_pointer_version=expected_pointer_version,
            )

        await self._transition(
            workspace_id=workspace_id,
            registry_id=registry_id,
            action=action,
            actor_membership_id=actor_membership_id,
            mfa_verified_at=mfa_verified_at,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            policy_version=policy_version,
            reason_code=(
                "paper_model_activated"
                if action is LearningModelRegistryAction.ACTIVATE
                else "paper_model_rollback"
            ),
            reason=reason,
            expected_pointer_version=expected_pointer_version,
        )
        pointer_row = await self._session.scalar(
            select(LearningModelActivePointerRecord)
            .where(LearningModelActivePointerRecord.workspace_id == workspace_id)
            .with_for_update()
        )
        if pointer_row is None:
            raise LearningModelRegistryConflict("Active pointer chưa được khởi tạo.")
        if (
            pointer_row.version != expected_pointer_version
            or pointer_row.registry_id != expected_current_registry_id
        ):
            raise LearningModelRegistryConflict("Active pointer CAS không khớp.")
        pointer_row.registry_id = registry_id
        pointer_row.version = expected_pointer_version + 1
        pointer_row.updated_by_membership_id = actor_membership_id
        pointer_row.changed_at = datetime.now(UTC)
        await self._session.flush()
        await self._session.refresh(target)
        await self._session.refresh(pointer_row)
        return _entry_snapshot(target), _pointer_snapshot(pointer_row)

    async def _transition(
        self,
        *,
        workspace_id: UUID,
        registry_id: UUID,
        action: LearningModelRegistryAction,
        actor_membership_id: UUID,
        expected_revision: int,
        idempotency_key: UUID,
        policy_version: str,
        reason_code: str,
        reason: str,
        mfa_verified_at: datetime | None = None,
        evaluation_report_id: UUID | None = None,
        evaluation_report_hash: str | None = None,
        expected_pointer_version: int | None = None,
    ) -> LearningModelRegistryEntry:
        _validate_ids(workspace_id, registry_id, actor_membership_id, idempotency_key)
        reason = _validate_reason(reason)
        await self._bind_workspace(workspace_id)
        await self._lock(workspace_id, f"model-registry-key:{idempotency_key}")
        existing = await self._find_event_by_key(workspace_id, idempotency_key)
        if existing is not None:
            current = await self._require_entry(workspace_id, existing.registry_id, lock=False)
            if _same_event_intent(
                existing,
                registry_id=registry_id,
                action=action,
                actor_membership_id=actor_membership_id,
                expected_revision=expected_revision,
                policy_version=policy_version,
                reason_code=reason_code,
                reason=reason,
                mfa_verified_at=mfa_verified_at,
                expected_pointer_version=expected_pointer_version,
                evaluation_report_id=evaluation_report_id,
                evaluation_report_hash=evaluation_report_hash,
            ):
                return _entry_snapshot(current)
            raise LearningModelRegistryConflict("Idempotency key đã gắn thao tác registry khác.")

        row = await self._require_entry(workspace_id, registry_id, lock=True)
        if row.revision != expected_revision:
            raise LearningModelRegistryConflict("Registry revision đã đổi; cần tải lại entry.")
        report_id = evaluation_report_id
        report_hash = evaluation_report_hash
        if action is LearningModelRegistryAction.EVALUATE:
            if report_id is None or report_hash is None:
                raise ValueError("Evaluation report id/hash là bắt buộc.")
            report = await self._session.scalar(
                select(LearningEvaluationReportRecord).where(
                    LearningEvaluationReportRecord.workspace_id == workspace_id,
                    LearningEvaluationReportRecord.id == report_id,
                )
            )
            if report is None:
                raise LookupError("Không tìm thấy evaluation report trong workspace.")
            if (
                report.report_hash != report_hash
                or report.manifest_hash != row.manifest_hash
                or report.fold_plan_hash != row.fold_plan_hash
                or report.evaluation_mode != "diagnostic"
                or report.candidate_completeness_verified is not False
            ):
                raise LearningModelRegistryConflict(
                    "Evaluation report không khớp artifact hoặc không thuộc chế độ chẩn đoán."
                )
        has_evaluation = row.evaluation_report_id is not None
        approved = await self._has_approval(workspace_id, registry_id)
        previously_active = await self._was_active(workspace_id, registry_id)
        try:
            next_state = transition_learning_model_state(
                LearningModelRegistryState(row.state),
                action,
                has_evaluation=has_evaluation or action is LearningModelRegistryAction.EVALUATE,
                approved=approved or action is LearningModelRegistryAction.APPROVE,
                previously_active=previously_active,
            )
        except LearningModelTransitionRejected as error:
            raise LearningModelRegistryConflict(str(error)) from None

        event = _event_row(
            workspace_id=workspace_id,
            registry_id=registry_id,
            sequence=expected_revision + 1,
            action=action,
            from_state=LearningModelRegistryState(row.state),
            to_state=next_state,
            actor_membership_id=actor_membership_id,
            policy_version=policy_version,
            reason_code=reason_code,
            reason=reason,
            idempotency_key=idempotency_key,
            mfa_verified_at=mfa_verified_at,
            expected_revision=expected_revision,
            expected_pointer_version=expected_pointer_version,
            evaluation_report_id=report_id,
            evaluation_report_hash=report_hash,
        )
        self._session.add(event)
        await self._session.flush()
        await self._session.refresh(row)
        return _entry_snapshot(row)

    async def _get_or_create_pointer(
        self, workspace_id: UUID, *, lock: bool
    ) -> LearningModelActivePointerRecord:
        statement = select(LearningModelActivePointerRecord).where(
            LearningModelActivePointerRecord.workspace_id == workspace_id
        )
        if lock:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        if row is None:
            row = LearningModelActivePointerRecord(
                workspace_id=workspace_id,
                registry_id=None,
                version=0,
                updated_by_membership_id=None,
            )
            self._session.add(row)
            await self._session.flush()
            if lock:
                await self._session.refresh(row)
        return row

    async def _has_approval(self, workspace_id: UUID, registry_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(LearningModelRegistryEventRecord.id).where(
                    LearningModelRegistryEventRecord.workspace_id == workspace_id,
                    LearningModelRegistryEventRecord.registry_id == registry_id,
                    LearningModelRegistryEventRecord.action
                    == LearningModelRegistryAction.APPROVE.value,
                )
            )
        )

    async def _was_active(self, workspace_id: UUID, registry_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(LearningModelRegistryEventRecord.id).where(
                    LearningModelRegistryEventRecord.workspace_id == workspace_id,
                    LearningModelRegistryEventRecord.registry_id == registry_id,
                    LearningModelRegistryEventRecord.action.in_(
                        [
                            LearningModelRegistryAction.ACTIVATE.value,
                            LearningModelRegistryAction.ROLLBACK.value,
                        ]
                    ),
                    LearningModelRegistryEventRecord.to_state
                    == LearningModelRegistryState.PAPER_ACTIVE.value,
                )
            )
        )

    async def _find_event_by_key(
        self, workspace_id: UUID, idempotency_key: UUID
    ) -> LearningModelRegistryEventRecord | None:
        return await self._session.scalar(
            select(LearningModelRegistryEventRecord).where(
                LearningModelRegistryEventRecord.workspace_id == workspace_id,
                LearningModelRegistryEventRecord.idempotency_key == idempotency_key,
            )
        )

    async def _require_entry(
        self, workspace_id: UUID, registry_id: UUID, *, lock: bool
    ) -> LearningModelRegistryRecord:
        if lock:
            # Entry không cấp UPDATE; advisory lock tuần tự hóa application writes.
            # Trigger SECURITY DEFINER vẫn khóa hàng và kiểm revision lúc ghi event.
            await self._lock(workspace_id, f"registry-entry:{registry_id}")
        statement = select(LearningModelRegistryRecord).where(
            LearningModelRegistryRecord.workspace_id == workspace_id,
            LearningModelRegistryRecord.id == registry_id,
        )
        row = await self._session.scalar(statement)
        if row is None:
            raise LookupError("Không tìm thấy model registry entry.")
        return row

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def _lock(self, workspace_id: UUID, lock_key: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"learning-model-registry:{workspace_id}:{lock_key}"},
        )


def _entry_snapshot(row: LearningModelRegistryRecord) -> LearningModelRegistryEntry:
    return LearningModelRegistryEntry(
        id=row.id,
        workspace_id=row.workspace_id,
        artifact_id=row.artifact_id,
        artifact_hash=row.artifact_hash,
        manifest_hash=row.manifest_hash,
        fold_plan_hash=row.fold_plan_hash,
        approval_hash=row.approval_hash,
        state=LearningModelRegistryState(row.state),
        revision=row.revision,
        evaluation_report_id=row.evaluation_report_id,
        evaluation_report_hash=row.evaluation_report_hash,
        created_by_membership_id=row.created_by_membership_id,
        created_at=row.created_at,
    )


def _event_snapshot(row: LearningModelRegistryEventRecord) -> LearningModelRegistryEvent:
    return LearningModelRegistryEvent(
        id=row.id,
        workspace_id=row.workspace_id,
        registry_id=row.registry_id,
        sequence=row.sequence,
        action=LearningModelRegistryAction(row.action),
        from_state=LearningModelRegistryState(row.from_state) if row.from_state else None,
        to_state=LearningModelRegistryState(row.to_state),
        actor_membership_id=row.actor_membership_id,
        policy_version=row.policy_version,
        reason_code=row.reason_code,
        reason=row.reason,
        idempotency_key=row.idempotency_key,
        mfa_verified_at=row.mfa_verified_at,
        expected_revision=row.expected_revision,
        expected_pointer_version=row.expected_pointer_version,
        evaluation_report_id=row.evaluation_report_id,
        evaluation_report_hash=row.evaluation_report_hash,
        created_at=row.created_at,
    )


def _pointer_snapshot(row: LearningModelActivePointerRecord) -> LearningModelActivePointer:
    return LearningModelActivePointer(
        workspace_id=row.workspace_id,
        registry_id=row.registry_id,
        version=row.version,
        updated_by_membership_id=row.updated_by_membership_id,
        changed_at=row.changed_at,
    )


def _event_row(
    *,
    workspace_id: UUID,
    registry_id: UUID,
    sequence: int,
    action: LearningModelRegistryAction,
    from_state: LearningModelRegistryState | None,
    to_state: LearningModelRegistryState,
    actor_membership_id: UUID,
    policy_version: str,
    reason_code: str,
    reason: str,
    idempotency_key: UUID,
    mfa_verified_at: datetime | None,
    expected_revision: int | None,
    expected_pointer_version: int | None,
    evaluation_report_id: UUID | None,
    evaluation_report_hash: str | None,
) -> LearningModelRegistryEventRecord:
    return LearningModelRegistryEventRecord(
        id=uuid4(),
        workspace_id=workspace_id,
        registry_id=registry_id,
        sequence=sequence,
        action=action.value,
        from_state=from_state.value if from_state else None,
        to_state=to_state.value,
        actor_membership_id=actor_membership_id,
        policy_version=policy_version,
        reason_code=reason_code,
        reason=reason,
        idempotency_key=idempotency_key,
        mfa_verified_at=mfa_verified_at,
        expected_revision=expected_revision,
        expected_pointer_version=expected_pointer_version,
        evaluation_report_id=evaluation_report_id,
        evaluation_report_hash=evaluation_report_hash,
    )


def _validate_ids(workspace_id: UUID, *identifiers: UUID) -> None:
    if any(type(identifier) is not UUID for identifier in (workspace_id, *identifiers)):
        raise ValueError("Registry identifiers phải là UUID.")


def _validate_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise ValueError("reason phải là chuỗi.")
    normalized = reason.strip()
    if not 12 <= len(normalized) <= 500:
        raise ValueError("reason phải có từ 12 đến 500 ký tự.")
    return normalized


def _same_event_intent(
    event: LearningModelRegistryEventRecord,
    *,
    registry_id: UUID,
    action: LearningModelRegistryAction,
    actor_membership_id: UUID,
    expected_revision: int,
    policy_version: str,
    reason_code: str,
    reason: str,
    mfa_verified_at: datetime | None,
    expected_pointer_version: int | None,
    evaluation_report_id: UUID | None,
    evaluation_report_hash: str | None,
) -> bool:
    return (
        event.registry_id == registry_id
        and event.action == action.value
        and event.actor_membership_id == actor_membership_id
        and event.expected_revision == expected_revision
        and event.policy_version == policy_version
        and event.reason_code == reason_code
        and event.reason == reason
        and event.mfa_verified_at == mfa_verified_at
        and event.expected_pointer_version == expected_pointer_version
        and event.evaluation_report_id == evaluation_report_id
        and event.evaluation_report_hash == evaluation_report_hash
    )


__all__ = ["LearningModelRegistryConflict", "LearningModelRegistryRepository"]
