"""Use case ghi nhận quyết định shadow fail-closed, không chấm điểm hay thực thi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.learning_shadow import (
    LearningShadowRepository,
    PaperSourceRightsReceipt,
    ShadowObservationSource,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.shadow import ShadowAbstainReason, ShadowObservation


@dataclass(frozen=True, slots=True)
class ShadowObservationBatch:
    """Kết quả một lượt đọc/append trong cùng workspace transaction."""

    workspace_id: UUID
    observations: tuple[ShadowObservation, ...]
    scanned_count: int
    created_count: int
    skipped_count: int


class ShadowObservationSetConflict(RuntimeError):
    """Tập decision thay đổi trước khi observer lấy được shared member fence."""


class LearningShadowApplication:
    """Chọn quyết định đã persist và ghi challenger ABSTAIN cùng lineage đã xác minh."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        batch_size: int = 100,
        unit_of_work_factory: Callable[..., UnitOfWork] = UnitOfWork,
        repository_factory: Callable[[AsyncSession], LearningShadowRepository] = (
            LearningShadowRepository
        ),
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("batch_size phải là số nguyên từ 1 đến 500.")
        if not callable(unit_of_work_factory) or not callable(repository_factory):
            raise ValueError("Factory UnitOfWork và repository phải gọi được.")
        self._sessions = sessions
        self._batch_size = batch_size
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_factory = repository_factory

    async def observe_pending(
        self,
        *,
        workspace_id: UUID,
        limit: int | None = None,
    ) -> ShadowObservationBatch:
        """Đọc nguồn từ DB; worker không truyền decision ID hay dữ liệu score."""
        if type(workspace_id) is not UUID or workspace_id.int == 0:
            raise ValueError("workspace_id phải là UUID khác zero.")
        selected_limit = self._batch_size if limit is None else limit
        if type(selected_limit) is not int or not 1 <= selected_limit <= self._batch_size:
            raise ValueError(f"limit phải là số nguyên từ 1 đến {self._batch_size}.")

        stored: list[ShadowObservation] = []
        created_count = 0
        async with self._unit_of_work_factory(
            self._sessions,
            workspace_id=workspace_id,
        ) as uow:
            repository = self._repository_factory(uow.session)
            # Ghim active pointer trước khi đọc ứng viên để tuần tự hóa với promote.
            await repository.lock_active_pointer(workspace_id=workspace_id)
            candidates = await repository.list_pending(
                workspace_id=workspace_id,
                limit=selected_limit,
            )
            await repository.lock_registry_entries(
                workspace_id=workspace_id,
                registry_ids=tuple(
                    sorted({item.challenger_model.registry_id for item in candidates}, key=str)
                ),
            )
            per_decision_source_ids: dict[UUID, tuple[str, ...]] = {}
            all_source_ids: set[str] = set()
            for source in candidates:
                decision_id = source.incumbent_decision.decision_id
                ids = await repository.decision_source_ids(
                    workspace_id=workspace_id,
                    decision_id=decision_id,
                )
                per_decision_source_ids[decision_id] = ids
                all_source_ids.update(ids)
            await repository.lock_source_ids(
                workspace_id=workspace_id,
                source_ids=tuple(sorted(all_source_ids)),
            )
            await repository.lock_member_set(workspace_id=workspace_id)
            confirmed = await repository.list_pending(
                workspace_id=workspace_id,
                limit=selected_limit,
            )
            if _candidate_identity(confirmed) != _candidate_identity(candidates):
                raise ShadowObservationSetConflict(
                    "Tập decision thay đổi trong lúc khóa member set; cần thử lại."
                )
            checked_at = await repository.authorization_clock(workspace_id=workspace_id)
            exact_fingerprints: dict[UUID, str | None] = {}
            for source in candidates:
                decision_id = source.incumbent_decision.decision_id
                if per_decision_source_ids[decision_id]:
                    exact_fingerprints[
                        decision_id
                    ] = await repository.authorize_inference_source_scope(
                        workspace_id=workspace_id,
                        decision_id=decision_id,
                        checked_at=checked_at,
                    )
                else:
                    exact_fingerprints[decision_id] = None
            if any(fingerprint is not None for fingerprint in exact_fingerprints.values()):
                # Receipt legacy chỉ là metadata audit; eligibility vẫn cần fingerprint exact.
                rights_receipt = await repository.current_paper_rights(
                    workspace_id=workspace_id,
                    source_id="BINANCE_USDM_REST",
                )
            else:
                rights_receipt = None
            for source in candidates:
                if source.incumbent_decision.workspace_id != workspace_id:
                    raise PermissionError("Repository trả decision ngoài workspace UnitOfWork.")
                if source.challenger_model.workspace_id != workspace_id:
                    raise PermissionError("Repository trả challenger ngoài workspace UnitOfWork.")
                decision_id = source.incumbent_decision.decision_id
                source_scope_fingerprint = exact_fingerprints[decision_id]
                rights_eligible = (
                    rights_receipt is not None
                    and source_scope_fingerprint is not None
                    and per_decision_source_ids[decision_id] == ("BINANCE_USDM_REST",)
                )
                observed_at = await repository.database_clock(workspace_id=workspace_id)
                reason = _abstain_reason(
                    rights_eligible=rights_eligible,
                    schema_compatible=source.schema_compatible,
                )
                observation = _build_observation(
                    workspace_id=workspace_id,
                    source=source,
                    rights_receipt=rights_receipt if rights_eligible else None,
                    rights_eligible=rights_eligible,
                    source_scope_fingerprint=(
                        source_scope_fingerprint if rights_eligible else None
                    ),
                    reason=reason,
                    observed_at=observed_at,
                )
                result = await repository.append(observation=observation)
                stored.append(result.observation)
                if not result.is_duplicate:
                    created_count += 1

        return ShadowObservationBatch(
            workspace_id=workspace_id,
            observations=tuple(stored),
            scanned_count=len(candidates),
            created_count=created_count,
            skipped_count=0,
        )


def _candidate_identity(
    candidates: tuple[ShadowObservationSource, ...],
) -> tuple[tuple[UUID, UUID, int, str], ...]:
    return tuple(
        sorted(
            (
                source.incumbent_decision.decision_id,
                source.challenger_model.registry_id,
                source.challenger_model.revision,
                source.snapshot.snapshot_hash,
            )
            for source in candidates
        )
    )


def _abstain_reason(
    *,
    rights_eligible: bool,
    schema_compatible: bool,
) -> ShadowAbstainReason:
    if not rights_eligible:
        return ShadowAbstainReason.SOURCE_RIGHTS_INELIGIBLE
    if not schema_compatible:
        return ShadowAbstainReason.FEATURE_SCHEMA_MISMATCH
    return ShadowAbstainReason.UNCALIBRATED_SCORE


def _build_observation(
    *,
    workspace_id: UUID,
    source: ShadowObservationSource,
    rights_receipt: PaperSourceRightsReceipt | None,
    rights_eligible: bool,
    source_scope_fingerprint: str | None,
    reason: ShadowAbstainReason,
    observed_at: datetime,
) -> ShadowObservation:
    receipt_fields = (
        {
            "rights_fingerprint": rights_receipt.fingerprint,
            "rights_source_id": rights_receipt.source_id,
            "rights_policy_id": rights_receipt.policy_id,
            "rights_policy_sequence": rights_receipt.policy_sequence,
            "rights_known_at": rights_receipt.known_at,
        }
        if rights_receipt is not None
        else {
            "rights_fingerprint": None,
            "rights_source_id": None,
            "rights_policy_id": None,
            "rights_policy_sequence": None,
            "rights_known_at": None,
        }
    )
    return ShadowObservation(
        observation_id=uuid4(),
        workspace_id=workspace_id,
        snapshot=source.snapshot,
        challenger_model=source.challenger_model,
        incumbent_decision=source.incumbent_decision,
        active_registry_id=source.active_registry_id,
        active_registry_revision=source.active_registry_revision,
        active_artifact_hash=source.active_artifact_hash,
        active_pointer_version=source.active_pointer_version,
        snapshot_coverage=source.snapshot_coverage,
        snapshot_quality_ok=source.snapshot_quality_ok,
        schema_compatible=source.schema_compatible,
        rights_eligible=rights_eligible,
        source_scope_fingerprint=source_scope_fingerprint,
        challenger_reason=reason,
        observed_at=observed_at,
        **receipt_fields,
    )


__all__ = [
    "LearningShadowApplication",
    "ShadowObservationBatch",
    "ShadowObservationSetConflict",
]
