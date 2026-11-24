"""Repository PostgreSQL cho nhật ký giao dịch, ảnh đính kèm và audit (tách khỏi ledger)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.trade_journal_entities import (
    TradeJournalAttachment,
    TradeJournalAudit,
    TradeJournalEntry,
)
from trading_bot.domain.portfolio.trade_journal import (
    JournalAuditAction,
    compute_expires_at,
)


def _entry_to_dict(row: TradeJournalEntry) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "bot_id": str(row.bot_id),
        "trade_id": str(row.trade_id),
        "author_user_id": str(row.author_user_id),
        "note_vi": row.note_vi,
        "checklist_items": list(row.checklist_items or []),
        "setup_tags": list(row.setup_tags or []),
        "mistake_tags": list(row.mistake_tags or []),
        "privacy_mode": row.privacy_mode,
        "retention_days": int(row.retention_days),
        "expires_at": row.expires_at.isoformat(),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _attachment_to_dict(row: TradeJournalAttachment) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "entry_id": str(row.entry_id),
        "file_name": row.file_name,
        "content_type": row.content_type,
        "byte_size": int(row.byte_size),
        "sha256": row.sha256,
        "storage_ref": row.storage_ref,
        "exif_stripped": bool(row.exif_stripped),
        "created_by_user_id": str(row.created_by_user_id),
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
    }


def _audit_to_dict(row: TradeJournalAudit) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "entry_id": str(row.entry_id),
        "trade_id": str(row.trade_id),
        "actor_user_id": str(row.actor_user_id),
        "action": row.action,
        "diff_summary": dict(row.diff_summary or {}),
        "created_at": row.created_at.isoformat(),
    }


class TradeJournalRepository:
    """Truy vấn và ghi nhật ký giao dịch trên session PostgreSQL với RLS theo workspace."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_entry(
        self, *, workspace_id: UUID, trade_id: UUID
    ) -> dict[str, Any] | None:
        stmt = select(TradeJournalEntry).where(
            TradeJournalEntry.workspace_id == workspace_id,
            TradeJournalEntry.trade_id == trade_id,
            TradeJournalEntry.deleted_at.is_(None),
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _entry_to_dict(row)

    async def list_entries(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None = None,
        trade_ids: Sequence[UUID] | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(TradeJournalEntry).where(
            TradeJournalEntry.workspace_id == workspace_id,
            TradeJournalEntry.deleted_at.is_(None),
        )
        if bot_id is not None:
            stmt = stmt.where(TradeJournalEntry.bot_id == bot_id)
        if trade_ids is not None:
            if not trade_ids:
                return []
            stmt = stmt.where(TradeJournalEntry.trade_id.in_(list(trade_ids)))
        stmt = stmt.order_by(TradeJournalEntry.updated_at.desc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_entry_to_dict(row) for row in rows]

    async def list_attachments(
        self, *, workspace_id: UUID, entry_id: UUID
    ) -> list[dict[str, Any]]:
        stmt = (
            select(TradeJournalAttachment)
            .where(
                TradeJournalAttachment.workspace_id == workspace_id,
                TradeJournalAttachment.entry_id == entry_id,
            )
            .order_by(TradeJournalAttachment.created_at.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_attachment_to_dict(row) for row in rows]

    async def list_audit_events(
        self, *, workspace_id: UUID, trade_id: UUID, limit: int = 20
    ) -> list[dict[str, Any]]:
        stmt = (
            select(TradeJournalAudit)
            .where(
                TradeJournalAudit.workspace_id == workspace_id,
                TradeJournalAudit.trade_id == trade_id,
            )
            .order_by(TradeJournalAudit.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_audit_to_dict(row) for row in rows]

    async def upsert_entry(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        trade_id: UUID,
        actor_user_id: UUID,
        note_vi: str,
        checklist_items: list[dict[str, Any]],
        setup_tags: list[str],
        mistake_tags: list[str],
        privacy_mode: str,
        retention_days: int,
        now: datetime,
    ) -> tuple[dict[str, Any], str]:
        stmt = select(TradeJournalEntry).where(
            TradeJournalEntry.workspace_id == workspace_id,
            TradeJournalEntry.trade_id == trade_id,
            TradeJournalEntry.deleted_at.is_(None),
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        expires_at = compute_expires_at(now, retention_days)
        if existing is None:
            entry = TradeJournalEntry(
                id=uuid4(),
                workspace_id=workspace_id,
                bot_id=bot_id,
                trade_id=trade_id,
                author_user_id=actor_user_id,
                note_vi=note_vi,
                checklist_items=checklist_items,
                setup_tags=setup_tags,
                mistake_tags=mistake_tags,
                privacy_mode=privacy_mode,
                retention_days=retention_days,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
            self._session.add(entry)
            action = JournalAuditAction.CREATED.value
            diff = {
                "setup_tags": setup_tags,
                "mistake_tags": mistake_tags,
                "privacy_mode": privacy_mode,
                "retention_days": retention_days,
            }
        else:
            entry = existing
            diff = {
                "prev_setup_tags": list(entry.setup_tags or []),
                "next_setup_tags": setup_tags,
                "prev_mistake_tags": list(entry.mistake_tags or []),
                "next_mistake_tags": mistake_tags,
                "privacy_mode": privacy_mode,
                "retention_days": retention_days,
            }
            entry.note_vi = note_vi
            entry.checklist_items = checklist_items
            entry.setup_tags = setup_tags
            entry.mistake_tags = mistake_tags
            entry.privacy_mode = privacy_mode
            entry.retention_days = retention_days
            entry.expires_at = expires_at
            entry.updated_at = now
            action = JournalAuditAction.UPDATED.value

        self._session.add(
            TradeJournalAudit(
                id=uuid4(),
                workspace_id=workspace_id,
                entry_id=entry.id,
                trade_id=trade_id,
                actor_user_id=actor_user_id,
                action=action,
                diff_summary=diff,
                created_at=now,
            )
        )
        await self._session.flush()
        return _entry_to_dict(entry), action

    async def add_attachment(
        self,
        *,
        workspace_id: UUID,
        entry_id: UUID,
        trade_id: UUID,
        actor_user_id: UUID,
        attachment_meta: dict[str, Any],
        expires_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        att = TradeJournalAttachment(
            id=uuid4(),
            workspace_id=workspace_id,
            entry_id=entry_id,
            file_name=attachment_meta["file_name"],
            content_type=attachment_meta["content_type"],
            byte_size=attachment_meta["byte_size"],
            sha256=attachment_meta["sha256"],
            storage_ref=attachment_meta["storage_ref"],
            exif_stripped=bool(attachment_meta["exif_stripped"]),
            created_by_user_id=actor_user_id,
            created_at=now,
            expires_at=expires_at,
        )
        self._session.add(att)
        self._session.add(
            TradeJournalAudit(
                id=uuid4(),
                workspace_id=workspace_id,
                entry_id=entry_id,
                trade_id=trade_id,
                actor_user_id=actor_user_id,
                action=JournalAuditAction.ATTACHMENT_ADDED.value,
                diff_summary={
                    "attachment_id": str(att.id),
                    "file_name": att.file_name,
                    "content_type": att.content_type,
                    "byte_size": att.byte_size,
                },
                created_at=now,
            )
        )
        await self._session.flush()
        return _attachment_to_dict(att)

    async def soft_delete_entry(
        self,
        *,
        workspace_id: UUID,
        trade_id: UUID,
        actor_user_id: UUID,
        now: datetime,
    ) -> bool:
        stmt = select(TradeJournalEntry).where(
            TradeJournalEntry.workspace_id == workspace_id,
            TradeJournalEntry.trade_id == trade_id,
            TradeJournalEntry.deleted_at.is_(None),
        )
        entry = (await self._session.execute(stmt)).scalar_one_or_none()
        if entry is None:
            return False
        entry.deleted_at = now
        entry.updated_at = now
        self._session.add(
            TradeJournalAudit(
                id=uuid4(),
                workspace_id=workspace_id,
                entry_id=entry.id,
                trade_id=trade_id,
                actor_user_id=actor_user_id,
                action=JournalAuditAction.SOFT_DELETED.value,
                diff_summary={"deleted_at": now.isoformat()},
                created_at=now,
            )
        )
        await self._session.flush()
        return True

    async def purge_expired(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: UUID,
        now: datetime,
    ) -> int:
        expired_stmt = select(TradeJournalEntry).where(
            TradeJournalEntry.workspace_id == workspace_id,
            TradeJournalEntry.expires_at <= now,
        )
        expired_rows = (await self._session.execute(expired_stmt)).scalars().all()
        if not expired_rows:
            return 0
        purged_count = len(expired_rows)
        for row in expired_rows:
            self._session.add(
                TradeJournalAudit(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    entry_id=row.id,
                    trade_id=row.trade_id,
                    actor_user_id=actor_user_id,
                    action=JournalAuditAction.RETENTION_PURGED.value,
                    diff_summary={"expires_at": row.expires_at.isoformat()},
                    created_at=now,
                )
            )
        entry_ids = [row.id for row in expired_rows]
        await self._session.execute(
            delete(TradeJournalEntry).where(
                TradeJournalEntry.workspace_id == workspace_id,
                TradeJournalEntry.id.in_(entry_ids),
            )
        )
        await self._session.flush()
        return purged_count
