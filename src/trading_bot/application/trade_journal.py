"""Dịch vụ Application cho nhật ký giao dịch, checklist, tag setup và mistake attribution."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.trade_journal import TradeJournalRepository
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.domain.portfolio.trade_journal import (
    JournalPrivacyMode,
    JournalValidationError,
    compute_mistake_attribution,
    compute_setup_tag_breakdown,
    is_entry_visible_to_actor,
    normalize_checklist_items,
    normalize_tags,
    sanitize_note,
    validate_attachment_metadata,
    validate_retention_days,
)


class TradeJournalError(Exception):
    """Lỗi nghiệp vụ/phân quyền của dịch vụ nhật ký giao dịch."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


class TradeJournalService:
    """Quản lý nhật ký, checklist, tag, ảnh đính kèm và phân bổ lỗi tách khỏi ledger."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = TradeJournalRepository,
        grants: Callable[[Any], Any] = BotGrantRepository,
    ) -> None:
        self._sessions = sessions
        self._unit_of_work = unit_of_work
        self._repository = repository
        self._grants = grants

    def _require_permission(self, principal: Principal, permission: Permission) -> None:
        try:
            authorize(principal, permission, raise_error=True)
        except PermissionError as error:
            raise TradeJournalError(
                403, "JOURNAL_FORBIDDEN", f"Thiếu quyền {permission.value} cho nhật ký."
            ) from error

    async def get_trade_journal(
        self,
        principal: Principal,
        *,
        trade_id: UUID,
        bot_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        self._require_permission(principal, Permission.TRADE_READ)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            if bot_id is not None:
                try:
                    await self._grants(unit.session).require(
                        principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                    )
                except PermissionError as error:
                    raise TradeJournalError(
                        404, "JOURNAL_NOT_FOUND", "Không tìm thấy nhật ký giao dịch."
                    ) from error
            repo = cast(TradeJournalRepository, self._repository(unit.session))
            entry = await repo.get_entry(workspace_id=principal.workspace_id, trade_id=trade_id)
            if entry is None:
                return None
            if not is_entry_visible_to_actor(
                entry,
                actor_user_id=principal.user_id,
                is_workspace_owner=Role.OWNER in principal.roles,
            ):
                return None
            entry["attachments"] = await repo.list_attachments(
                workspace_id=principal.workspace_id, entry_id=UUID(entry["id"])
            )
            entry["audit_events"] = await repo.list_audit_events(
                workspace_id=principal.workspace_id, trade_id=trade_id, limit=20
            )
            return entry

    async def list_trade_journals_map(
        self,
        principal: Principal,
        *,
        bot_id: UUID | None = None,
        trade_ids: Sequence[UUID] | None = None,
    ) -> dict[str, dict[str, Any]]:
        self._require_permission(principal, Permission.TRADE_READ)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            if bot_id is not None:
                try:
                    await self._grants(unit.session).require(
                        principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                    )
                except PermissionError as error:
                    raise TradeJournalError(
                        404, "JOURNAL_NOT_FOUND", "Không tìm thấy nhật ký giao dịch."
                    ) from error
            repo = cast(TradeJournalRepository, self._repository(unit.session))
            rows = await repo.list_entries(
                workspace_id=principal.workspace_id, bot_id=bot_id, trade_ids=trade_ids
            )
            is_owner = Role.OWNER in principal.roles
            return {
                row["trade_id"]: row
                for row in rows
                if is_entry_visible_to_actor(
                    row, actor_user_id=principal.user_id, is_workspace_owner=is_owner
                )
            }

    async def save_trade_journal(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        trade_id: UUID,
        note_vi: str | None = "",
        checklist_items: Sequence[Mapping[str, Any]] | None = None,
        setup_tags: Sequence[str] | None = None,
        mistake_tags: Sequence[str] | None = None,
        privacy_mode: str = JournalPrivacyMode.WORKSPACE_TEAM.value,
        retention_days: int | None = None,
    ) -> dict[str, Any]:
        self._require_permission(principal, Permission.TRADE_REVIEW)
        try:
            clean_note = sanitize_note(note_vi)
            clean_checklist = [
                item.as_dict() for item in normalize_checklist_items(checklist_items)
            ]
            clean_setup = list(normalize_tags(setup_tags, kind="setup"))
            clean_mistakes = list(normalize_tags(mistake_tags, kind="lỗi"))
            clean_retention = validate_retention_days(retention_days)
            clean_privacy = JournalPrivacyMode(privacy_mode).value
        except (JournalValidationError, ValueError) as error:
            code = getattr(error, "code", "JOURNAL_VALIDATION_ERROR")
            raise TradeJournalError(422, code, str(error)) from error

        now = datetime.now(UTC)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            try:
                await self._grants(unit.session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise TradeJournalError(
                    404, "JOURNAL_NOT_FOUND", "Không có quyền trên bot được chọn."
                ) from error
            repo = cast(TradeJournalRepository, self._repository(unit.session))
            entry, action = await repo.upsert_entry(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                trade_id=trade_id,
                actor_user_id=principal.user_id,
                note_vi=clean_note,
                checklist_items=clean_checklist,
                setup_tags=clean_setup,
                mistake_tags=clean_mistakes,
                privacy_mode=clean_privacy,
                retention_days=clean_retention,
                now=now,
            )
            entry["last_audit_action"] = action
            return entry

    async def add_attachment(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        trade_id: UUID,
        file_name: str,
        content_type: str,
        byte_size: int,
        sha256: str,
        storage_ref: str,
        exif_stripped: bool,
    ) -> dict[str, Any]:
        self._require_permission(principal, Permission.TRADE_REVIEW)
        try:
            meta = validate_attachment_metadata(
                file_name=file_name,
                content_type=content_type,
                byte_size=byte_size,
                sha256=sha256,
                storage_ref=storage_ref,
                exif_stripped=exif_stripped,
            )
        except JournalValidationError as error:
            raise TradeJournalError(422, error.code, error.message) from error

        now = datetime.now(UTC)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repo = cast(TradeJournalRepository, self._repository(unit.session))
            entry = await repo.get_entry(workspace_id=principal.workspace_id, trade_id=trade_id)
            if entry is None:
                raise TradeJournalError(
                    404,
                    "JOURNAL_ENTRY_REQUIRED",
                    "Hãy lưu nhật ký giao dịch trước khi đính kèm ảnh.",
                )
            return await repo.add_attachment(
                workspace_id=principal.workspace_id,
                entry_id=UUID(entry["id"]),
                trade_id=trade_id,
                actor_user_id=principal.user_id,
                attachment_meta=meta,
                expires_at=datetime.fromisoformat(entry["expires_at"]),
                now=now,
            )

    async def delete_trade_journal(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        trade_id: UUID,
    ) -> bool:
        self._require_permission(principal, Permission.TRADE_REVIEW)
        now = datetime.now(UTC)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repo = cast(TradeJournalRepository, self._repository(unit.session))
            return await repo.soft_delete_entry(
                workspace_id=principal.workspace_id,
                trade_id=trade_id,
                actor_user_id=principal.user_id,
                now=now,
            )

    async def purge_expired_journals(self, principal: Principal) -> int:
        self._require_permission(principal, Permission.WORKSPACE_KILL)
        now = datetime.now(UTC)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repo = cast(TradeJournalRepository, self._repository(unit.session))
            return await repo.purge_expired(
                workspace_id=principal.workspace_id,
                actor_user_id=principal.user_id,
                now=now,
            )

    async def build_attribution_summary(
        self,
        principal: Principal,
        *,
        trades: Sequence[Mapping[str, Any]],
        bot_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Tổng hợp chi phí lỗi theo mistake_tags và hiệu suất theo setup_tags cho báo cáo."""
        trade_ids: list[UUID] = []
        for trade in trades:
            raw_id = trade.get("trade_id")
            if raw_id:
                try:
                    trade_ids.append(UUID(str(raw_id)))
                except ValueError:
                    continue
        entries_map = await self.list_trade_journals_map(
            principal, bot_id=bot_id, trade_ids=trade_ids
        )
        return {
            "mistake_attribution": compute_mistake_attribution(trades, entries_map),
            "setup_tag_breakdown": compute_setup_tag_breakdown(trades, entries_map),
            "journaled_trade_count": len(entries_map),
        }
