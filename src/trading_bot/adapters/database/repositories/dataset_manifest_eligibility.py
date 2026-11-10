"""Repository invalidation và fail-closed rights-event lookup cho manifest WP-06."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import exists, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.schema import Table

from trading_bot.adapters.database.models import (
    ConsumerReceipt,
    DatasetManifestEligibilityEventRecord,
    DatasetManifestRecord,
    DatasetManifestSourceRefRecord,
    OutboxEvent,
)
from trading_bot.domain.learning.dataset import DatasetSourceReference

RIGHTS_EVENT_TYPE = "research.rights_changed.v1"
RIGHTS_CONSUMER_NAME = "wp06.dataset-rights-eligibility.v1"


class DatasetManifestEligibilityRepository:
    """Thêm invalidation cùng transaction và đọc gate trong workspace hiện hành."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_for_rights_change(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        occurred_at: datetime,
        source_id: str,
        item_id: str | None,
        change_type: str,
        policy_sequence: int | None,
        reason_code: str,
    ) -> int:
        """Append cho các manifest cũ bị ảnh hưởng; retry event không sinh dòng trùng."""
        if (
            type(workspace_id) is not UUID
            or type(event_id) is not UUID
            or not isinstance(source_id, str)
            or not source_id.strip()
            or not isinstance(occurred_at, datetime)
            or occurred_at.tzinfo is None
            or occurred_at.utcoffset() is None
        ):
            raise ValueError("Metadata invalidation cần giá trị đã xác minh")
        await self._bind_workspace(workspace_id)
        reference = DatasetManifestSourceRefRecord
        manifest = DatasetManifestRecord
        conditions = [
            reference.workspace_id == workspace_id,
            reference.source_id == source_id,
            manifest.created_at <= occurred_at,
        ]
        if change_type in {"policy_revision", "policy_expired"}:
            if policy_sequence is None:
                raise ValueError("policy event thiếu policy_sequence")
            if change_type == "policy_expired":
                conditions.append(reference.policy_sequence == policy_sequence)
            else:
                conditions.append(reference.policy_sequence < policy_sequence)
        else:
            if item_id is None:
                raise ValueError("item event thiếu item_id")
            conditions.append(reference.item_id == item_id)

        affected = (
            select(
                text("pg_catalog.gen_random_uuid()"),
                reference.workspace_id,
                reference.manifest_id,
                reference.sample_id,
                text(":outbox_event_id"),
                reference.source_id,
                reference.item_id,
                reference.revision_id,
                reference.policy_id,
                reference.policy_sequence,
                reference.purpose,
                reference.rights_hash,
                text(":change_type"),
                text(":reason_code"),
                text(":occurred_at"),
            )
            .select_from(reference)
            .join(
                manifest,
                (manifest.workspace_id == reference.workspace_id)
                & (manifest.id == reference.manifest_id),
            )
            .where(*conditions)
        )
        target: Table = cast(Table, DatasetManifestEligibilityEventRecord.__table__)
        statement = (
            insert(target)
            .from_select(
                [
                    "id",
                    "workspace_id",
                    "manifest_id",
                    "sample_id",
                    "outbox_event_id",
                    "source_id",
                    "item_id",
                    "revision_id",
                    "policy_id",
                    "policy_sequence",
                    "purpose",
                    "rights_hash",
                    "change_type",
                    "reason_code",
                    "occurred_at",
                ],
                affected,
            )
            .on_conflict_do_nothing(constraint="uq_dataset_manifest_eligibility_event_identity")
            .returning(DatasetManifestEligibilityEventRecord.id)
        )
        result = await self._session.execute(
            statement,
            {
                "outbox_event_id": event_id,
                "change_type": change_type,
                "reason_code": reason_code,
                "occurred_at": occurred_at,
            },
        )
        return len(result.scalars().all())

    async def find_invalidation(
        self,
        *,
        workspace_id: UUID,
        manifest_hash: str,
        sample_id: str,
        reference: DatasetSourceReference,
    ) -> str | None:
        """Tìm ledger event gắn chính xác với manifest/sample/source reference."""
        if type(workspace_id) is not UUID or type(reference) is not DatasetSourceReference:
            raise ValueError("workspace_id và reference cần kiểu rõ")
        await self._bind_workspace(workspace_id)
        statement = (
            select(DatasetManifestEligibilityEventRecord.reason_code)
            .join(
                DatasetManifestRecord,
                (
                    DatasetManifestRecord.workspace_id
                    == DatasetManifestEligibilityEventRecord.workspace_id
                )
                & (DatasetManifestRecord.id == DatasetManifestEligibilityEventRecord.manifest_id),
            )
            .where(
                DatasetManifestEligibilityEventRecord.workspace_id == workspace_id,
                DatasetManifestRecord.manifest_hash == manifest_hash,
                DatasetManifestEligibilityEventRecord.sample_id == sample_id,
                DatasetManifestEligibilityEventRecord.source_id == reference.source_id,
                DatasetManifestEligibilityEventRecord.item_id == reference.item_id,
                DatasetManifestEligibilityEventRecord.revision_id == reference.revision_id,
                DatasetManifestEligibilityEventRecord.policy_id == reference.policy_id,
                DatasetManifestEligibilityEventRecord.policy_sequence == reference.policy_sequence,
                DatasetManifestEligibilityEventRecord.purpose == reference.purpose.value,
            )
            .order_by(DatasetManifestEligibilityEventRecord.created_at)
            .limit(1)
        )
        return await self._session.scalar(statement)

    async def has_unprocessed_rights_event(
        self, *, workspace_id: UUID, source_id: str, item_id: str
    ) -> bool:
        """Pending, leased, delivered-without-receipt và dead event đều chặn dùng."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID")
        await self._bind_workspace(workspace_id)
        receipt_exists = exists(
            select(1).where(
                ConsumerReceipt.workspace_id == OutboxEvent.workspace_id,
                ConsumerReceipt.outbox_event_id == OutboxEvent.id,
                ConsumerReceipt.consumer_name == RIGHTS_CONSUMER_NAME,
            )
        )
        item_matches = or_(
            ~OutboxEvent.payload.has_key("item_id"),
            OutboxEvent.payload["item_id"].as_string() == item_id,
        )
        statement = select(
            exists(
                select(1).where(
                    OutboxEvent.workspace_id == workspace_id,
                    OutboxEvent.event_type == RIGHTS_EVENT_TYPE,
                    OutboxEvent.payload["source_id"].as_string() == source_id,
                    item_matches,
                    ~receipt_exists,
                )
            )
        )
        return bool(await self._session.scalar(statement))

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
