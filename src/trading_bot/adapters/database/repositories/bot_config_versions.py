"""Kho phiên bản cấu hình bot; mọi truy vấn nhận workspace_id và bot_id tường minh.

Repository không tự commit; caller giữ transaction (UnitOfWork) và advisory lock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import BotConfigRequest, BotConfigVersion
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash


class BotConfigVersionConflict(RuntimeError):
    """Draft đã đổi revision/trạng thái hoặc phiên bản cha không còn là head."""


def request_fingerprint(operation: str, payload: Mapping[str, Any]) -> str:
    """Dấu vân tay yêu cầu (IETF Idempotency-Key §2.4) trên JSON chuẩn hóa."""
    canonical = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BotConfigVersionRepository:
    """Thao tác version/request theo scope bot; RLS là lớp bảo vệ bổ sung."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_bot(self, *, workspace_id: UUID, bot_id: UUID) -> None:
        """Tuần tự hóa mutation cấu hình của cùng bot trong transaction hiện tại."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:bot_config_lock, 0))"),
            {"bot_config_lock": f"bot-config:{workspace_id}:{bot_id}"},
        )

    async def get(
        self, *, workspace_id: UUID, bot_id: UUID, version_id: UUID
    ) -> BotConfigVersion | None:
        return await self._session.scalar(
            select(BotConfigVersion).where(
                BotConfigVersion.workspace_id == workspace_id,
                BotConfigVersion.bot_id == bot_id,
                BotConfigVersion.id == version_id,
            )
        )

    async def get_by_number(
        self, *, workspace_id: UUID, bot_id: UUID, version: int
    ) -> BotConfigVersion | None:
        return await self._session.scalar(
            select(BotConfigVersion).where(
                BotConfigVersion.workspace_id == workspace_id,
                BotConfigVersion.bot_id == bot_id,
                BotConfigVersion.version == version,
            )
        )

    async def list_versions(self, *, workspace_id: UUID, bot_id: UUID) -> list[BotConfigVersion]:
        rows = await self._session.scalars(
            select(BotConfigVersion)
            .where(
                BotConfigVersion.workspace_id == workspace_id,
                BotConfigVersion.bot_id == bot_id,
            )
            .order_by(BotConfigVersion.version.desc())
        )
        return list(rows.all())

    async def get_head(self, *, workspace_id: UUID, bot_id: UUID) -> BotConfigVersion | None:
        """Version PUBLISHED mới nhất; làm mốc cho kiểm tra stale parent."""
        return await self._session.scalar(
            select(BotConfigVersion)
            .where(
                BotConfigVersion.workspace_id == workspace_id,
                BotConfigVersion.bot_id == bot_id,
                BotConfigVersion.status == "PUBLISHED",
            )
            .order_by(BotConfigVersion.version.desc())
            .limit(1)
        )

    async def get_effective(
        self, *, workspace_id: UUID, bot_id: UUID, as_of: datetime | None = None
    ) -> BotConfigVersion | None:
        """Version đã publish có hiệu lực tại ``as_of``; draft không bao giờ được trả."""
        point = as_of or datetime.now(UTC)
        return await self._session.scalar(
            select(BotConfigVersion)
            .where(
                BotConfigVersion.workspace_id == workspace_id,
                BotConfigVersion.bot_id == bot_id,
                BotConfigVersion.status == "PUBLISHED",
                BotConfigVersion.effective_at <= point,
            )
            .order_by(BotConfigVersion.version.desc())
            .limit(1)
        )

    async def create_draft(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        overlay: BotConfigOverlay,
        base_settings_version_id: UUID,
        base_settings_hash: str,
        created_by: UUID,
        parent_version_id: UUID | None,
        source_version_id: UUID | None = None,
    ) -> BotConfigVersion:
        next_version = await self._session.scalar(
            select(func.coalesce(func.max(BotConfigVersion.version), 0) + 1).where(
                BotConfigVersion.workspace_id == workspace_id,
                BotConfigVersion.bot_id == bot_id,
            )
        )
        payload = canonical_overlay(overlay)
        record = BotConfigVersion(
            id=uuid4(),
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            version=int(next_version or 1),
            parent_version_id=parent_version_id,
            source_version_id=source_version_id,
            schema_version=overlay.schema_version,
            overlay_json=payload,
            overlay_hash=overlay_hash(payload),
            base_settings_version_id=base_settings_version_id,
            base_settings_hash=base_settings_hash,
            status="DRAFT",
            revision=1,
            created_by=created_by,
            updated_by=created_by,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def update_draft(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        draft_id: UUID,
        expected_revision: int,
        overlay: BotConfigOverlay,
        base_settings_version_id: UUID,
        base_settings_hash: str,
        updated_by: UUID,
    ) -> BotConfigVersion:
        payload = canonical_overlay(overlay)
        updated_id = await self._session.scalar(
            update(BotConfigVersion)
            .where(
                BotConfigVersion.workspace_id == workspace_id,
                BotConfigVersion.bot_id == bot_id,
                BotConfigVersion.id == draft_id,
                BotConfigVersion.status == "DRAFT",
                BotConfigVersion.revision == expected_revision,
            )
            .values(
                overlay_json=payload,
                overlay_hash=overlay_hash(payload),
                schema_version=overlay.schema_version,
                base_settings_version_id=base_settings_version_id,
                base_settings_hash=base_settings_hash,
                revision=expected_revision + 1,
                updated_by=updated_by,
            )
            .returning(BotConfigVersion.id)
        )
        if updated_id is None:
            raise BotConfigVersionConflict("Bản nháp đã đổi hoặc không còn ở trạng thái DRAFT.")
        record = await self.get(workspace_id=workspace_id, bot_id=bot_id, version_id=draft_id)
        if record is None:
            raise BotConfigVersionConflict("Không đọc lại được bản nháp vừa sửa.")
        return record

    async def publish_draft(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        draft_id: UUID,
        expected_revision: int,
        expected_parent_version_id: UUID | None,
        approved_by: UUID,
        base_settings_version_id: UUID,
        base_settings_hash: str,
        effective_at: datetime | None = None,
    ) -> BotConfigVersion:
        """Chuyển DRAFT -> PUBLISHED khi revision và cha vẫn khớp head hiện tại."""
        draft = await self.get(workspace_id=workspace_id, bot_id=bot_id, version_id=draft_id)
        if draft is None or draft.status != "DRAFT" or draft.revision != expected_revision:
            raise BotConfigVersionConflict("Bản nháp đã đổi hoặc không còn ở trạng thái DRAFT.")
        if draft.parent_version_id != expected_parent_version_id:
            raise BotConfigVersionConflict("Phiên bản cha của bản nháp không khớp.")
        head = await self.get_head(workspace_id=workspace_id, bot_id=bot_id)
        if (head.id if head is not None else None) != expected_parent_version_id:
            raise BotConfigVersionConflict("Phiên bản cấu hình bot hiện hành đã thay đổi.")
        moment = effective_at or datetime.now(UTC)
        updated_id = await self._session.scalar(
            update(BotConfigVersion)
            .where(
                BotConfigVersion.workspace_id == workspace_id,
                BotConfigVersion.bot_id == bot_id,
                BotConfigVersion.id == draft_id,
                BotConfigVersion.status == "DRAFT",
                BotConfigVersion.revision == expected_revision,
            )
            .values(
                status="PUBLISHED",
                approved_by=approved_by,
                published_at=moment,
                effective_at=moment,
                base_settings_version_id=base_settings_version_id,
                base_settings_hash=base_settings_hash,
            )
            .returning(BotConfigVersion.id)
        )
        if updated_id is None:
            raise BotConfigVersionConflict("Bản nháp đã đổi hoặc không còn ở trạng thái DRAFT.")
        record = await self.get(workspace_id=workspace_id, bot_id=bot_id, version_id=draft_id)
        if record is None:
            raise BotConfigVersionConflict("Không đọc lại được phiên bản vừa publish.")
        return record

    async def find_request(
        self, *, workspace_id: UUID, bot_id: UUID, operation: str, idempotency_key: str
    ) -> BotConfigRequest | None:
        return await self._session.scalar(
            select(BotConfigRequest).where(
                BotConfigRequest.workspace_id == workspace_id,
                BotConfigRequest.bot_id == bot_id,
                BotConfigRequest.operation == operation,
                BotConfigRequest.idempotency_key == idempotency_key,
            )
        )

    async def record_request(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        version_id: UUID,
        created_by: UUID,
    ) -> BotConfigRequest:
        row = BotConfigRequest(
            id=uuid4(),
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            bot_config_version_id=version_id,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row
