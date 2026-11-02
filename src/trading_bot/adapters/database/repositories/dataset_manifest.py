"""Persistence tenant-scoped cho manifest dataset đã được application xác minh."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import DatasetManifestRecord
from trading_bot.domain.learning.dataset import DatasetManifest


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
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"dataset-manifest:{workspace_id}:{manifest.content_hash}"},
        )
        existing = await self._session.scalar(
            select(DatasetManifestRecord).where(
                DatasetManifestRecord.workspace_id == workspace_id,
                DatasetManifestRecord.manifest_hash == manifest.content_hash,
            )
        )
        if existing is not None:
            if existing.canonical_manifest != manifest.canonical_json:
                raise DatasetManifestConflict("manifest hash đã có payload canonical khác.")
            verified = existing.to_domain()
            if verified.content_hash != manifest.content_hash:
                raise DatasetManifestConflict("manifest lưu trữ không khớp content hash.")
            return DatasetManifestWriteResult(existing, True)

        record = DatasetManifestRecord.from_domain(manifest)
        self._session.add(record)
        await self._session.flush()
        return DatasetManifestWriteResult(record, False)

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

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
