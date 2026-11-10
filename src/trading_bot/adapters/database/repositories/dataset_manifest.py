"""Persistence tenant-scoped cho manifest dataset đã được application xác minh."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    DatasetManifestRecord,
    DatasetManifestSourceRefRecord,
)
from trading_bot.adapters.database.repositories.learning_split_approvals import (
    LearningSplitApprovalRepository,
)
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSourceReference
from trading_bot.domain.learning.evaluation import WalkForwardSplit


class DatasetManifestConflict(RuntimeError):
    """Hash manifest đã tồn tại nhưng payload canonical khác."""


@dataclass(frozen=True, slots=True)
class DatasetManifestWriteResult:
    record: DatasetManifestRecord
    is_duplicate: bool


class DatasetManifestRepository:
    """Append-only store; RLS và transaction của caller giới hạn quyền ghi/đọc."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self, *, workspace_id: UUID, manifest: DatasetManifest
    ) -> DatasetManifestWriteResult:
        """Lưu payload canonical một lần; cùng hash chỉ idempotent nếu bytes trùng."""
        if type(workspace_id) is not UUID or type(manifest) is not DatasetManifest:
            raise ValueError("workspace_id và manifest phải có kiểu tường minh.")
        if workspace_id != manifest.workspace_id:
            raise DatasetManifestConflict("workspace manifest không khớp workspace transaction.")
        await self._bind_workspace(workspace_id)
        if any(sample.source_dependent for sample in manifest.samples) and (
            manifest.manifest_version != "dataset-manifest-v3"
        ):
            raise DatasetManifestConflict(
                "Manifest có source lineage legacy không được lưu làm dataset mới."
            )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"dataset-manifest:{workspace_id}:{manifest.content_hash}"},
        )
        existing_id = await self._session.scalar(
            select(DatasetManifestRecord.id).where(
                DatasetManifestRecord.workspace_id == workspace_id,
                DatasetManifestRecord.manifest_hash == manifest.content_hash,
            )
        )
        if existing_id is not None:
            canonical_matches = await self._session.scalar(
                text(
                    "SELECT public.wp06_manifest_payload_matches("
                    "CAST(:workspace_id AS uuid), CAST(:manifest_hash AS text), "
                    "CAST(:canonical_manifest AS text))"
                ),
                {
                    "workspace_id": workspace_id,
                    "manifest_hash": manifest.content_hash,
                    "canonical_manifest": manifest.canonical_json,
                },
            )
            if not canonical_matches:
                raise DatasetManifestConflict("manifest hash đã có payload canonical khác.")
            record = DatasetManifestRecord.from_domain(manifest)
            record.id = existing_id
            return DatasetManifestWriteResult(record, True)

        record = DatasetManifestRecord.from_domain(manifest)
        self._session.add(record)
        await self._session.flush()
        for sample in manifest.samples:
            for reference in sample.source_references:
                self._session.add(
                    DatasetManifestSourceRefRecord.from_reference(
                        workspace_id=workspace_id,
                        manifest_id=record.id,
                        sample_id=sample.sample_id,
                        reference=reference,
                    )
                )
        await self._session.flush()
        return DatasetManifestWriteResult(record, False)

    async def append_for_training(
        self,
        *,
        workspace_id: UUID,
        manifest: DatasetManifest,
        folds: tuple[WalkForwardSplit, ...],
        evaluation_cutoff: datetime,
    ) -> DatasetManifestWriteResult:
        """Chỉ seal manifest training khi approval exact-match đã được ghi trước đó."""
        if type(workspace_id) is not UUID or type(manifest) is not DatasetManifest:
            raise ValueError("workspace_id và manifest phải có kiểu tường minh.")
        if workspace_id != manifest.workspace_id:
            raise DatasetManifestConflict("workspace manifest không khớp workspace transaction.")
        plan = build_manifest_fold_plan(
            manifest,
            folds=folds,
            expected_split_policy_version=manifest.split_policy_version,
            evaluation_cutoff=evaluation_cutoff,
        )
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"dataset-manifest:{workspace_id}:{manifest.content_hash}"},
        )
        approval = await LearningSplitApprovalRepository(self._session).get_for_training_seal(
            workspace_id=workspace_id,
            manifest_hash=manifest.content_hash,
            split_policy_version=manifest.split_policy_version,
            fold_plan_hash=plan.content_hash,
        )
        if approval is None:
            raise DatasetManifestConflict(
                "Không thể seal training: thiếu approval exact-match của manifest và fold plan."
            )
        return await self.append(workspace_id=workspace_id, manifest=manifest)

    async def get_by_hash(
        self, *, workspace_id: UUID, manifest_hash: str
    ) -> DatasetManifest | None:
        """Nạp manifest trong workspace rồi dựng lại từ canonical bytes đã băm."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(DatasetManifestRecord).where(
                DatasetManifestRecord.workspace_id == workspace_id,
                DatasetManifestRecord.manifest_hash == manifest_hash,
            )
        )
        return record.to_domain() if record is not None else None

    async def find_source_reference_uses(
        self,
        *,
        workspace_id: UUID,
        reference: DatasetSourceReference,
    ) -> tuple[DatasetManifestSourceRefRecord, ...]:
        """Tra ngược lineage nguồn; rights hash đọc lại để phát hiện policy drift."""
        if type(workspace_id) is not UUID or type(reference) is not DatasetSourceReference:
            raise ValueError("workspace_id và reference phải có kiểu tường minh.")
        await self._bind_workspace(workspace_id)
        result = await self._session.scalars(
            select(DatasetManifestSourceRefRecord)
            .where(
                DatasetManifestSourceRefRecord.workspace_id == workspace_id,
                DatasetManifestSourceRefRecord.source_id == reference.source_id,
                DatasetManifestSourceRefRecord.item_id == reference.item_id,
                DatasetManifestSourceRefRecord.revision_id == reference.revision_id,
                DatasetManifestSourceRefRecord.policy_id == reference.policy_id,
                DatasetManifestSourceRefRecord.policy_sequence == reference.policy_sequence,
                DatasetManifestSourceRefRecord.purpose == reference.purpose.value,
            )
            .order_by(
                DatasetManifestSourceRefRecord.manifest_id,
                DatasetManifestSourceRefRecord.sample_id,
            )
        )
        return tuple(result.all())

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
