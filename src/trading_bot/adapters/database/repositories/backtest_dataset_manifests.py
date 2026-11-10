"""Lưu và nạp backtest dataset manifest bất biến theo workspace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import BacktestDatasetManifestRecord
from trading_bot.application.backtest_dataset_manifest import BacktestDatasetManifest

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BacktestDatasetManifestConflict(RuntimeError):
    """Cùng dataset hash nhưng payload, workspace hoặc dữ liệu lưu không khớp."""


@dataclass(frozen=True, slots=True)
class BacktestDatasetManifestWriteResult:
    record: BacktestDatasetManifestRecord
    is_duplicate: bool


class BacktestDatasetManifestRepository:
    """Repository append-only; workspace/RLS và transaction do caller cung cấp."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        workspace_id: UUID,
        manifest: BacktestDatasetManifest,
    ) -> BacktestDatasetManifestWriteResult:
        """Lưu manifest một lần; retry cùng hash chỉ idempotent với bytes canonical giống nhau."""
        if type(workspace_id) is not UUID or type(manifest) is not BacktestDatasetManifest:
            raise ValueError("workspace_id và manifest phải có kiểu tường minh.")
        if manifest.rights_workspace_id != workspace_id:
            raise BacktestDatasetManifestConflict(
                "workspace policy evidence không khớp workspace lưu manifest."
            )
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"backtest-dataset-manifest:{workspace_id}:{manifest.dataset_hash}"},
        )
        existing = await self._session.scalar(
            select(BacktestDatasetManifestRecord).where(
                BacktestDatasetManifestRecord.workspace_id == workspace_id,
                BacktestDatasetManifestRecord.dataset_hash == manifest.dataset_hash,
            )
        )
        if existing is not None:
            self._assert_same_payload(existing, manifest)
            return BacktestDatasetManifestWriteResult(existing, True)

        record = BacktestDatasetManifestRecord.from_manifest(
            workspace_id=workspace_id,
            manifest=manifest,
        )
        self._session.add(record)
        await self._session.flush()
        return BacktestDatasetManifestWriteResult(record, False)

    async def get_by_hash(
        self,
        *,
        workspace_id: UUID,
        dataset_hash: str,
    ) -> BacktestDatasetManifest | None:
        """Nạp trong tenant cụ thể rồi kiểm canonical JSON, hash và cột chỉ mục."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(dataset_hash, str) or _SHA256.fullmatch(dataset_hash) is None:
            raise ValueError("dataset_hash phải là SHA-256 hex.")
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(BacktestDatasetManifestRecord).where(
                BacktestDatasetManifestRecord.workspace_id == workspace_id,
                BacktestDatasetManifestRecord.dataset_hash == dataset_hash,
            )
        )
        if record is None:
            return None
        manifest = record.to_domain()
        if manifest.dataset_hash != dataset_hash:
            raise BacktestDatasetManifestConflict("dataset hash đọc lại không khớp request.")
        return manifest

    @staticmethod
    def _assert_same_payload(
        record: BacktestDatasetManifestRecord,
        manifest: BacktestDatasetManifest,
    ) -> None:
        try:
            stored = record.to_domain()
        except (TypeError, ValueError):
            raise BacktestDatasetManifestConflict(
                "dataset hash đã có payload canonical khác hoặc dữ liệu lưu sai."
            ) from None
        if (
            record.dataset_hash != manifest.dataset_hash
            or record.canonical_manifest != manifest.canonical_json
            or stored != manifest
        ):
            raise BacktestDatasetManifestConflict(
                "dataset hash đã có payload canonical khác hoặc dữ liệu lưu sai."
            )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
