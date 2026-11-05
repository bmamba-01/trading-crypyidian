"""Lưu và nạp bundle replay bất biến theo workspace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import BacktestDatasetReplayBundleRecord
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    BacktestDatasetReplayBundleReference,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BacktestDatasetReplayBundleConflict(RuntimeError):
    """Cùng dataset hash nhưng payload, workspace hoặc dữ liệu lưu không khớp."""


@dataclass(frozen=True, slots=True)
class BacktestDatasetReplayBundleWriteResult:
    record: BacktestDatasetReplayBundleRecord
    is_duplicate: bool


class BacktestDatasetReplayBundleRepository:
    """Repository append-only; workspace/RLS và transaction do caller cung cấp."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        workspace_id: UUID,
        bundle: BacktestDatasetReplayBundle,
    ) -> BacktestDatasetReplayBundleWriteResult:
        """Lưu bundle một lần; retry cùng hash chỉ idempotent với bytes canonical giống nhau."""
        if type(workspace_id) is not UUID or type(bundle) is not BacktestDatasetReplayBundle:
            raise ValueError("workspace_id và bundle phải có kiểu tường minh.")
        if bundle.workspace_id != workspace_id:
            raise BacktestDatasetReplayBundleConflict(
                "workspace policy evidence không khớp workspace lưu bundle."
            )
        reference = BacktestDatasetReplayBundleReference.from_bundle(bundle)
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"backtest-dataset-bundle:{workspace_id}:{reference.bundle_hash}"},
        )
        existing = await self._session.scalar(
            select(BacktestDatasetReplayBundleRecord).where(
                BacktestDatasetReplayBundleRecord.workspace_id == workspace_id,
                BacktestDatasetReplayBundleRecord.bundle_hash == reference.bundle_hash,
            )
        )
        if existing is not None:
            self._assert_same_payload(existing, reference)
            return BacktestDatasetReplayBundleWriteResult(existing, True)

        record = BacktestDatasetReplayBundleRecord.from_reference(
            workspace_id=workspace_id,
            reference=reference,
        )
        self._session.add(record)
        await self._session.flush()
        return BacktestDatasetReplayBundleWriteResult(record, False)

    async def get_by_hash(
        self,
        *,
        workspace_id: UUID,
        bundle_hash: str,
    ) -> BacktestDatasetReplayBundleReference | None:
        """Nạp trong tenant cụ thể rồi kiểm canonical JSON, hash và cột chỉ mục."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(bundle_hash, str) or _SHA256.fullmatch(bundle_hash) is None:
            raise ValueError("bundle_hash phải là SHA-256 hex.")
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(BacktestDatasetReplayBundleRecord).where(
                BacktestDatasetReplayBundleRecord.workspace_id == workspace_id,
                BacktestDatasetReplayBundleRecord.bundle_hash == bundle_hash,
            )
        )
        if record is None:
            return None
        reference = record.to_reference()
        if reference.workspace_id != workspace_id or reference.bundle_hash != bundle_hash:
            raise BacktestDatasetReplayBundleConflict("dataset hash đọc lại không khớp request.")
        return reference

    async def list_recent(
        self, *, workspace_id: UUID, limit: int = 30
    ) -> tuple[BacktestDatasetReplayBundleReference, ...]:
        """Liệt kê bundle đã lưu để người vận hành chọn mà không cần nhập hash thủ công."""
        if type(workspace_id) is not UUID or not 1 <= limit <= 100:
            raise ValueError("Workspace hoặc giới hạn danh sách bundle không hợp lệ.")
        await self._bind_workspace(workspace_id)
        rows = await self._session.scalars(
            select(BacktestDatasetReplayBundleRecord)
            .where(BacktestDatasetReplayBundleRecord.workspace_id == workspace_id)
            .order_by(
                BacktestDatasetReplayBundleRecord.created_at.desc(),
                BacktestDatasetReplayBundleRecord.id.desc(),
            )
            .limit(limit)
        )
        references = tuple(row.to_reference() for row in rows.all())
        if any(reference.workspace_id != workspace_id for reference in references):
            raise BacktestDatasetReplayBundleConflict("Bundle đọc lại có scope hoặc hash sai.")
        return references

    @staticmethod
    def _assert_same_payload(
        record: BacktestDatasetReplayBundleRecord,
        reference: BacktestDatasetReplayBundleReference,
    ) -> None:
        try:
            stored = record.to_reference()
        except TypeError, ValueError:
            raise BacktestDatasetReplayBundleConflict(
                "dataset hash đã có payload canonical khác hoặc dữ liệu lưu sai."
            ) from None
        if (
            record.bundle_hash != reference.bundle_hash
            or record.canonical_bundle != reference.canonical_json
            or stored != reference
        ):
            raise BacktestDatasetReplayBundleConflict(
                "dataset hash đã có payload canonical khác hoặc dữ liệu lưu sai."
            )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
