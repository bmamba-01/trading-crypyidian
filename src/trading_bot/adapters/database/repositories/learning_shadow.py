"""Lưu và xác minh lineage shadow bằng truy vấn metadata theo workspace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, bindparam, exists, func, literal, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from trading_bot.adapters.database.models import (
    BotDecisionRecord,
    FeatureSnapshotRecord,
    LearningModelActivePointerRecord,
    LearningModelRegistryEventRecord,
    LearningModelRegistryRecord,
    LearningShadowObservationRecord,
    LearningTrainingArtifactRecord,
    SharedSignalEvidenceRecord,
)
from trading_bot.domain.learning.model_registry import LearningModelRegistryState
from trading_bot.domain.learning.shadow import (
    IncumbentDecisionReference,
    IncumbentDecisionStatus,
    ShadowAbstainReason,
    ShadowModelReference,
    ShadowObservation,
    ShadowSnapshotReference,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ID = "BINANCE_USDM_REST"


class LearningShadowConflict(RuntimeError):
    """Idempotency key đã gắn với lineage observation không khớp."""


class LearningShadowCorruptRecord(RuntimeError):
    """Observation đã lưu không vượt qua kiểm tra hash/domain khi đọc lại."""


@dataclass(frozen=True, slots=True)
class ShadowObservationSource:
    """Projection lineage đã đọc từ cột metadata; không giữ vector hoặc payload."""

    incumbent_decision: IncumbentDecisionReference
    challenger_model: ShadowModelReference
    snapshot: ShadowSnapshotReference
    active_registry_id: UUID
    active_registry_revision: int
    active_artifact_hash: str
    active_pointer_version: int
    snapshot_coverage: float
    snapshot_quality_ok: bool
    schema_compatible: bool


@dataclass(frozen=True, slots=True)
class PaperSourceRightsReceipt:
    """Receipt policy hiện hành đã được DB xác minh trong source lock."""

    source_id: str
    policy_id: str
    policy_sequence: int
    known_at: datetime
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or self.source_id != _SOURCE_ID:
            raise ValueError("Receipt quyền nguồn không khớp Binance USD-M.")
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id không được để trống.")
        if type(self.policy_sequence) is not int or self.policy_sequence < 1:
            raise ValueError("policy_sequence phải là số nguyên dương.")
        if not isinstance(self.known_at, datetime) or not _is_aware(self.known_at):
            raise ValueError("known_at phải có timezone.")
        if not isinstance(self.fingerprint, str) or _SHA256.fullmatch(self.fingerprint) is None:
            raise ValueError("fingerprint phải là SHA-256 dạng hex.")
        object.__setattr__(self, "known_at", self.known_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class ShadowObservationAppendResult:
    """Kết quả append; retry trùng trả lại chính observation đã lưu."""

    observation: ShadowObservation
    is_duplicate: bool


class LearningShadowRepository:
    """Repository tenant-scoped chỉ đọc metadata và append observation bất biến."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def pending_statement(*, workspace_id: UUID, limit: int) -> Select[Any]:
        """Tạo projection hạn chế cột, chỉ cho quyết định có challenger được duyệt."""
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit phải là số nguyên từ 1 đến 500.")

        decision = BotDecisionRecord
        evidence = SharedSignalEvidenceRecord
        snapshot = FeatureSnapshotRecord
        challenger = LearningModelRegistryRecord
        challenger_artifact = LearningTrainingArtifactRecord
        pointer = LearningModelActivePointerRecord
        active_registry = aliased(LearningModelRegistryRecord, name="active_registry")
        approval = LearningModelRegistryEventRecord

        latest_approval = (
            select(
                LearningModelRegistryEventRecord.workspace_id.label("workspace_id"),
                LearningModelRegistryEventRecord.registry_id.label("registry_id"),
                func.max(LearningModelRegistryEventRecord.sequence).label("sequence"),
            )
            .where(
                LearningModelRegistryEventRecord.workspace_id == workspace_id,
                LearningModelRegistryEventRecord.action == "approve",
                LearningModelRegistryEventRecord.to_state == "evaluated",
                LearningModelRegistryEventRecord.mfa_verified_at.is_not(None),
            )
            .group_by(
                LearningModelRegistryEventRecord.workspace_id,
                LearningModelRegistryEventRecord.registry_id,
            )
            .subquery("latest_registry_approval")
        )
        event_hash = func.public.wp06_learning_registry_approval_event_hash(
            challenger.workspace_id,
            challenger.id,
            approval.id,
        ).label("registry_approval_event_hash")

        prior_observation = exists(
            select(literal(1)).where(
                LearningShadowObservationRecord.workspace_id == workspace_id,
                LearningShadowObservationRecord.incumbent_decision_id == decision.id,
                LearningShadowObservationRecord.challenger_registry_id == challenger.id,
                LearningShadowObservationRecord.challenger_revision == challenger.revision,
                LearningShadowObservationRecord.snapshot_hash == snapshot.content_hash,
            )
        )
        statement = (
            select(
                decision.id.label("decision_id"),
                decision.workspace_id.label("workspace_id"),
                decision.decision_hash.label("decision_hash"),
                decision.strategy_release_id.label("strategy_release_id"),
                decision.strategy_release_hash.label("strategy_release_hash"),
                decision.status.label("decision_status"),
                decision.evaluated_at.label("decision_evaluated_at"),
                evidence.feature_snapshot_id.label("snapshot_id"),
                evidence.evidence_hash.label("evidence_hash"),
                snapshot.content_hash.label("snapshot_hash"),
                snapshot.schema_hash.label("snapshot_schema_hash"),
                snapshot.as_of.label("snapshot_as_of"),
                snapshot.data_coverage.label("snapshot_coverage"),
                snapshot.quality_flags.label("snapshot_quality_flags"),
                snapshot.data_freshness.label("snapshot_data_freshness"),
                snapshot.is_usable.label("snapshot_is_usable"),
                challenger.id.label("challenger_registry_id"),
                challenger.artifact_id.label("challenger_artifact_id"),
                challenger.artifact_hash.label("challenger_artifact_hash"),
                challenger.manifest_hash.label("challenger_manifest_hash"),
                challenger.fold_plan_hash.label("challenger_fold_plan_hash"),
                challenger.approval_hash.label("training_approval_hash"),
                challenger.revision.label("challenger_revision"),
                challenger.state.label("challenger_state"),
                challenger_artifact.feature_schema_hash.label("artifact_schema_hash"),
                approval.id.label("registry_approval_event_id"),
                approval.sequence.label("registry_approval_event_sequence"),
                event_hash,
                pointer.registry_id.label("active_registry_id"),
                pointer.version.label("active_pointer_version"),
                active_registry.revision.label("active_registry_revision"),
                active_registry.artifact_hash.label("active_artifact_hash"),
            )
            .select_from(decision)
            .join(
                evidence,
                and_(
                    evidence.workspace_id == decision.workspace_id,
                    evidence.id == decision.evidence_id,
                    evidence.evidence_hash == decision.evidence_hash,
                ),
            )
            .join(
                snapshot,
                and_(
                    snapshot.workspace_id == evidence.workspace_id,
                    snapshot.snapshot_id == evidence.feature_snapshot_id,
                ),
            )
            .join(
                pointer,
                pointer.workspace_id == decision.workspace_id,
            )
            .join(
                active_registry,
                and_(
                    active_registry.workspace_id == pointer.workspace_id,
                    active_registry.id == pointer.registry_id,
                ),
            )
            .join(
                challenger,
                and_(
                    challenger.workspace_id == decision.workspace_id,
                    challenger.state == LearningModelRegistryState.EVALUATED.value,
                    challenger.id != pointer.registry_id,
                ),
            )
            .join(
                challenger_artifact,
                and_(
                    challenger_artifact.workspace_id == challenger.workspace_id,
                    challenger_artifact.id == challenger.artifact_id,
                ),
            )
            .join(
                latest_approval,
                and_(
                    latest_approval.c.workspace_id == challenger.workspace_id,
                    latest_approval.c.registry_id == challenger.id,
                    latest_approval.c.sequence <= challenger.revision,
                ),
            )
            .join(
                approval,
                and_(
                    approval.workspace_id == latest_approval.c.workspace_id,
                    approval.registry_id == latest_approval.c.registry_id,
                    approval.sequence == latest_approval.c.sequence,
                ),
            )
            .where(
                decision.workspace_id == workspace_id,
                decision.strategy_release_hash.is_not(None),
                decision.evaluated_at >= snapshot.as_of,
                func.public.wp06_learning_registry_approval_event_hash(
                    challenger.workspace_id,
                    challenger.id,
                    approval.id,
                ).is_not(None),
                ~prior_observation,
            )
            .order_by(
                decision.evaluated_at.asc(),
                decision.id.asc(),
                challenger.id.asc(),
            )
            .limit(limit)
        )
        return cast(Select[Any], statement)

    @staticmethod
    def rights_supported_statement(*, workspace_id: UUID, decision_id: UUID) -> Select[Any]:
        """Gọi bộ kiểm tra DB, không lấy source_refs hoặc input-event payload về app."""
        return select(
            func.public.wp06_shadow_decision_source_rights_supported(
                bindparam("workspace_id", type_=BotDecisionRecord.workspace_id.type),
                bindparam("decision_id", type_=BotDecisionRecord.id.type),
            )
        ).params(workspace_id=workspace_id, decision_id=decision_id)

    @staticmethod
    def database_clock_statement() -> Select[Any]:
        """Lấy thời điểm bắt đầu transaction để trigger DB xác nhận timestamp."""
        return select(func.transaction_timestamp())

    async def list_pending(
        self, *, workspace_id: UUID, limit: int = 100
    ) -> tuple[ShadowObservationSource, ...]:
        await self._bind_workspace(workspace_id)
        rows = (
            (
                await self._session.execute(
                    self.pending_statement(workspace_id=workspace_id, limit=limit)
                )
            )
            .mappings()
            .all()
        )
        return tuple(_source_from_mapping(row) for row in rows)

    async def lock_active_pointer(self, *, workspace_id: UUID) -> None:
        """Tuần tự hóa observer với thay đổi active pointer trong transaction."""
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"learning-model-registry:{workspace_id}:active-pointer"},
        )

    async def decision_source_rights_supported(
        self, *, workspace_id: UUID, decision_id: UUID
    ) -> bool:
        await self._bind_workspace(workspace_id)
        if type(decision_id) is not UUID or decision_id.int == 0:
            raise ValueError("decision_id phải là UUID khác zero.")
        value = await self._session.scalar(
            self.rights_supported_statement(
                workspace_id=workspace_id,
                decision_id=decision_id,
            )
        )
        return value is True

    async def current_paper_rights(
        self,
        *,
        workspace_id: UUID,
        source_id: str = _SOURCE_ID,
    ) -> PaperSourceRightsReceipt | None:
        """Lấy receipt hiện hành khi policy cho phép PAPER_DEMO trong source lock."""
        await self._bind_workspace(workspace_id)
        if source_id != _SOURCE_ID:
            return None
        receipt = await self._session.scalar(
            text("SELECT public.wp06_shadow_current_paper_rights_receipt(:workspace_id)"),
            {"workspace_id": workspace_id},
        )
        if not isinstance(receipt, dict) or set(receipt) != {
            "source_id",
            "policy_id",
            "policy_sequence",
            "known_at",
            "fingerprint",
        }:
            return None
        try:
            known_at = _parse_receipt_time(receipt["known_at"])
            return PaperSourceRightsReceipt(
                source_id=receipt["source_id"],
                policy_id=receipt["policy_id"],
                policy_sequence=receipt["policy_sequence"],
                known_at=known_at,
                fingerprint=receipt["fingerprint"],
            )
        except TypeError, ValueError:
            return None

    async def database_clock(self, *, workspace_id: UUID) -> datetime:
        await self._bind_workspace(workspace_id)
        value = await self._session.scalar(self.database_clock_statement())
        if not isinstance(value, datetime) or not _is_aware(value):
            raise RuntimeError("Database transaction_timestamp() thiếu timezone.")
        return value.astimezone(UTC)

    async def append(self, *, observation: ShadowObservation) -> ShadowObservationAppendResult:
        if type(observation) is not ShadowObservation:
            raise ValueError("observation phải là ShadowObservation đã xác thực.")
        await self._bind_workspace(observation.workspace_id)
        existing = await self._existing_by_logical_key(observation=observation)
        if existing is not None:
            return self._resolve_duplicate(existing, observation)

        existing = await self._existing_by_key(
            workspace_id=observation.workspace_id,
            idempotency_key=observation.idempotency_key,
        )
        if existing is not None:
            return self._resolve_duplicate(existing, observation)

        record = _record_from_observation(observation)
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError as error:
            if not _is_observation_unique_violation(error):
                raise
            existing = await self._existing_by_logical_key(observation=observation)
            if existing is None:
                existing = await self._existing_by_key(
                    workspace_id=observation.workspace_id,
                    idempotency_key=observation.idempotency_key,
                )
            if existing is None:
                raise LearningShadowConflict(
                    "Unique key bị tranh chấp nhưng không có bản ghi để đối chiếu."
                ) from error
            try:
                return self._resolve_duplicate(existing, observation)
            except LearningShadowConflict as conflict:
                raise conflict from error
        return ShadowObservationAppendResult(observation=observation, is_duplicate=False)

    def _resolve_duplicate(
        self,
        existing: LearningShadowObservationRecord,
        requested: ShadowObservation,
    ) -> ShadowObservationAppendResult:
        try:
            stored = _observation_from_record(existing)
        except (LearningShadowCorruptRecord, ValueError) as error:
            raise LearningShadowConflict(
                "Observation đã lưu không khớp hash hoặc lineage yêu cầu."
            ) from error
        if (
            not _same_logical_identity(stored, requested)
            or stored.idempotency_key != requested.idempotency_key
        ):
            raise LearningShadowConflict(
                "Logical identity đã được dùng cho lineage observation khác."
            )
        return ShadowObservationAppendResult(observation=stored, is_duplicate=True)

    async def _existing_by_logical_key(
        self, *, observation: ShadowObservation
    ) -> LearningShadowObservationRecord | None:
        decision = observation.incumbent_decision
        challenger = observation.challenger_model
        return await self._session.scalar(
            select(LearningShadowObservationRecord).where(
                LearningShadowObservationRecord.workspace_id == observation.workspace_id,
                LearningShadowObservationRecord.incumbent_decision_id == decision.decision_id,
                LearningShadowObservationRecord.challenger_registry_id == challenger.registry_id,
                LearningShadowObservationRecord.challenger_revision == challenger.revision,
                LearningShadowObservationRecord.snapshot_hash == observation.snapshot.snapshot_hash,
            )
        )

    async def _existing_by_key(
        self, *, workspace_id: UUID, idempotency_key: str
    ) -> LearningShadowObservationRecord | None:
        return await self._session.scalar(
            select(LearningShadowObservationRecord).where(
                LearningShadowObservationRecord.workspace_id == workspace_id,
                LearningShadowObservationRecord.idempotency_key == idempotency_key,
            )
        )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )


def _source_from_mapping(row: Any) -> ShadowObservationSource:
    workspace_id = row["workspace_id"]
    snapshot = ShadowSnapshotReference(
        workspace_id=workspace_id,
        snapshot_id=row["snapshot_id"],
        snapshot_hash=row["snapshot_hash"],
        schema_hash=row["snapshot_schema_hash"],
        as_of=row["snapshot_as_of"],
    )
    challenger = ShadowModelReference(
        workspace_id=workspace_id,
        registry_id=row["challenger_registry_id"],
        artifact_id=row["challenger_artifact_id"],
        artifact_hash=row["challenger_artifact_hash"],
        manifest_hash=row["challenger_manifest_hash"],
        fold_plan_hash=row["challenger_fold_plan_hash"],
        training_approval_hash=row["training_approval_hash"],
        registry_approval_event_id=row["registry_approval_event_id"],
        registry_approval_event_hash=row["registry_approval_event_hash"],
        registry_approval_event_sequence=row["registry_approval_event_sequence"],
        revision=row["challenger_revision"],
        state=LearningModelRegistryState(row["challenger_state"]),
    )
    incumbent = IncumbentDecisionReference(
        decision_id=row["decision_id"],
        workspace_id=workspace_id,
        decision_hash=row["decision_hash"],
        strategy_release_id=row["strategy_release_id"],
        strategy_release_hash=row["strategy_release_hash"],
        status=IncumbentDecisionStatus(row["decision_status"]),
        snapshot=snapshot,
        evaluated_at=row["decision_evaluated_at"],
    )
    return ShadowObservationSource(
        incumbent_decision=incumbent,
        challenger_model=challenger,
        snapshot=snapshot,
        active_registry_id=row["active_registry_id"],
        active_registry_revision=row["active_registry_revision"],
        active_artifact_hash=row["active_artifact_hash"],
        active_pointer_version=row["active_pointer_version"],
        snapshot_coverage=float(row["snapshot_coverage"]),
        snapshot_quality_ok=(
            row["snapshot_is_usable"]
            and row["snapshot_data_freshness"] == "FRESH"
            and row["snapshot_quality_flags"] == []
        ),
        schema_compatible=row["snapshot_schema_hash"] == row["artifact_schema_hash"],
    )


def _record_from_observation(observation: ShadowObservation) -> LearningShadowObservationRecord:
    decision = observation.incumbent_decision
    model = observation.challenger_model
    return LearningShadowObservationRecord(
        id=observation.observation_id,
        workspace_id=observation.workspace_id,
        incumbent_decision_id=decision.decision_id,
        decision_hash=decision.decision_hash,
        strategy_release_id=decision.strategy_release_id,
        strategy_release_hash=decision.strategy_release_hash,
        incumbent_status=decision.status.value,
        decision_evaluated_at=decision.evaluated_at,
        snapshot_id=observation.snapshot.snapshot_id,
        snapshot_hash=observation.snapshot.snapshot_hash,
        snapshot_schema_hash=observation.snapshot.schema_hash,
        snapshot_as_of=observation.snapshot.as_of,
        challenger_registry_id=model.registry_id,
        active_registry_id=observation.active_registry_id,
        active_registry_revision=observation.active_registry_revision,
        active_artifact_hash=observation.active_artifact_hash,
        challenger_artifact_id=model.artifact_id,
        challenger_artifact_hash=model.artifact_hash,
        challenger_manifest_hash=model.manifest_hash,
        challenger_fold_plan_hash=model.fold_plan_hash,
        training_approval_hash=model.training_approval_hash,
        challenger_revision=model.revision,
        challenger_state=model.state.value,
        registry_approval_event_id=model.registry_approval_event_id,
        registry_approval_event_hash=model.registry_approval_event_hash,
        registry_approval_event_sequence=model.registry_approval_event_sequence,
        active_pointer_version=observation.active_pointer_version,
        snapshot_coverage=observation.snapshot_coverage,
        snapshot_quality_ok=observation.snapshot_quality_ok,
        schema_compatible=observation.schema_compatible,
        rights_eligible=observation.rights_eligible,
        rights_fingerprint=observation.rights_fingerprint,
        rights_source_id=observation.rights_source_id,
        rights_policy_id=observation.rights_policy_id,
        rights_policy_sequence=observation.rights_policy_sequence,
        rights_known_at=observation.rights_known_at,
        challenger_decision="abstain",
        challenger_reason=observation.challenger_reason.value,
        observed_at=observation.observed_at,
        idempotency_key=observation.idempotency_key,
        content_hash=observation.content_hash,
    )


def _observation_from_record(record: LearningShadowObservationRecord) -> ShadowObservation:
    snapshot = ShadowSnapshotReference(
        workspace_id=record.workspace_id,
        snapshot_id=record.snapshot_id,
        snapshot_hash=record.snapshot_hash,
        schema_hash=record.snapshot_schema_hash,
        as_of=record.snapshot_as_of,
    )
    model = ShadowModelReference(
        workspace_id=record.workspace_id,
        registry_id=record.challenger_registry_id,
        artifact_id=record.challenger_artifact_id,
        artifact_hash=record.challenger_artifact_hash,
        manifest_hash=record.challenger_manifest_hash,
        fold_plan_hash=record.challenger_fold_plan_hash,
        training_approval_hash=record.training_approval_hash,
        registry_approval_event_id=record.registry_approval_event_id,
        registry_approval_event_hash=record.registry_approval_event_hash,
        registry_approval_event_sequence=record.registry_approval_event_sequence,
        revision=record.challenger_revision,
        state=LearningModelRegistryState(record.challenger_state),
    )
    incumbent = IncumbentDecisionReference(
        decision_id=record.incumbent_decision_id,
        workspace_id=record.workspace_id,
        decision_hash=record.decision_hash,
        strategy_release_id=record.strategy_release_id,
        strategy_release_hash=record.strategy_release_hash,
        status=IncumbentDecisionStatus(record.incumbent_status),
        snapshot=snapshot,
        evaluated_at=record.decision_evaluated_at,
    )
    observation = ShadowObservation(
        observation_id=record.id,
        workspace_id=record.workspace_id,
        snapshot=snapshot,
        challenger_model=model,
        incumbent_decision=incumbent,
        active_registry_id=record.active_registry_id,
        active_registry_revision=record.active_registry_revision,
        active_artifact_hash=record.active_artifact_hash,
        active_pointer_version=record.active_pointer_version,
        snapshot_coverage=record.snapshot_coverage,
        snapshot_quality_ok=record.snapshot_quality_ok,
        schema_compatible=record.schema_compatible,
        rights_eligible=record.rights_eligible,
        rights_fingerprint=record.rights_fingerprint,
        rights_source_id=record.rights_source_id,
        rights_policy_id=record.rights_policy_id,
        rights_policy_sequence=record.rights_policy_sequence,
        rights_known_at=record.rights_known_at,
        challenger_reason=ShadowAbstainReason(record.challenger_reason),
        observed_at=record.observed_at,
    )
    if (
        record.challenger_decision != "abstain"
        or record.idempotency_key != observation.idempotency_key
        or record.content_hash != observation.content_hash
    ):
        raise LearningShadowCorruptRecord("Observation hash/idempotency không khớp domain.")
    return observation


def _same_logical_identity(left: ShadowObservation, right: ShadowObservation) -> bool:
    return (
        left.workspace_id == right.workspace_id
        and left.incumbent_decision.decision_id == right.incumbent_decision.decision_id
        and left.challenger_model.registry_id == right.challenger_model.registry_id
        and left.challenger_model.revision == right.challenger_model.revision
        and left.snapshot.snapshot_hash == right.snapshot.snapshot_hash
    )


def _is_observation_unique_violation(error: IntegrityError) -> bool:
    original = error.orig
    diagnostic = getattr(original, "diag", None)
    return getattr(diagnostic, "constraint_name", None) in {
        "uq_learning_shadow_observation_idempotency",
        "uq_learning_shadow_observation_logical_identity",
    }


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _parse_receipt_time(value: object) -> datetime:
    if isinstance(value, datetime):
        if not _is_aware(value):
            raise ValueError("Receipt time lacks timezone.")
        return value.astimezone(UTC)
    if not isinstance(value, str):
        raise ValueError("Receipt time must be ISO text.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not _is_aware(parsed):
        raise ValueError("Receipt time lacks timezone.")
    return parsed.astimezone(UTC)


__all__ = [
    "LearningShadowConflict",
    "LearningShadowCorruptRecord",
    "LearningShadowRepository",
    "PaperSourceRightsReceipt",
    "ShadowObservationAppendResult",
    "ShadowObservationSource",
]
