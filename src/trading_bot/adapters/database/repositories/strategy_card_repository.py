"""Tầng lưu trữ (Repository) cho Strategy Card trong PostgreSQL.

Đảm bảo cách ly tenant (Row-Level Security), khóa phân tán (Advisory Lock),
và tính toàn vẹn bất biến (Immutable Versioning).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.strategy_card_entities import StrategyCardRecord
from trading_bot.domain.strategy_card.schema import StrategyCard, compute_canonical_hash

if TYPE_CHECKING:
    pass


class StrategyCardConflict(RuntimeError):
    """Xảy ra khi có xung đột phiên bản hoặc băm nội dung của thẻ chiến lược."""


class StrategyCardNotFoundError(RuntimeError):
    """Xảy ra khi không tìm thấy thẻ chiến lược được yêu cầu."""


class StrategyCardRepository:
    """Repository quản lý lưu trữ và truy vấn Strategy Card trong PostgreSQL có RLS."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        """Thiết lập workspace_id vào session cho Row-Level Security."""
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def save_card(
        self,
        *,
        workspace_id: UUID,
        card: StrategyCard,
        is_active: bool = False,
    ) -> StrategyCardRecord:
        """Lưu thẻ chiến lược mới với advisory transaction lock và bảo toàn tính bất biến."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(card, StrategyCard):
            raise ValueError("card phải là StrategyCard.")

        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"strategy-card:{workspace_id}:{card.card_id}"},
        )

        content_hash = compute_canonical_hash(card)

        # 1. Kiểm tra idempotent theo content_hash
        existing_by_hash = await self._session.scalar(
            select(StrategyCardRecord).where(
                StrategyCardRecord.workspace_id == workspace_id,
                StrategyCardRecord.content_hash == content_hash,
            )
        )
        if existing_by_hash is not None:
            if existing_by_hash.card_id != card.card_id or existing_by_hash.version != card.version:
                raise StrategyCardConflict(
                    f"Content hash '{content_hash}' đã tồn tại dưới card_id "
                    f"'{existing_by_hash.card_id}' và phiên bản '{existing_by_hash.version}'."
                )
            return existing_by_hash

        # 2. Kiểm tra conflict (card_id, version) nhưng hash khác (chống sửa thẻ đã xuất bản)
        existing_by_version = await self._session.scalar(
            select(StrategyCardRecord).where(
                StrategyCardRecord.workspace_id == workspace_id,
                StrategyCardRecord.card_id == card.card_id,
                StrategyCardRecord.version == card.version,
            )
        )
        if existing_by_version is not None:
            raise StrategyCardConflict(
                f"Phiên bản '{card.version}' của chiến lược '{card.card_id}' đã tồn tại "
                "với nội dung khác (không thể sửa đổi hoặc ghi đè thẻ chiến lược đã công bố)."
            )

        if is_active:
            await self._session.execute(
                text(
                    "UPDATE strategy_cards SET is_active = FALSE "
                    "WHERE workspace_id = :workspace_id AND card_id = :card_id"
                ),
                {"workspace_id": str(workspace_id), "card_id": card.card_id},
            )

        record = StrategyCardRecord.from_domain(
            workspace_id=workspace_id,
            card=card,
            is_active=is_active,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_card(
        self,
        *,
        workspace_id: UUID,
        card_id: str,
        version: str,
    ) -> StrategyCard | None:
        """Truy vấn StrategyCard từ cơ sở dữ liệu và phục hồi đối tượng domain."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(StrategyCardRecord).where(
                StrategyCardRecord.workspace_id == workspace_id,
                StrategyCardRecord.card_id == card_id,
                StrategyCardRecord.version == version,
            )
        )
        return record.to_domain() if record is not None else None

    async def get_card_record(
        self,
        *,
        workspace_id: UUID,
        card_id: str,
        version: str,
    ) -> StrategyCardRecord | None:
        """Truy vấn StrategyCardRecord ORM phục vụ metadata và trạng thái kích hoạt."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        return await self._session.scalar(
            select(StrategyCardRecord).where(
                StrategyCardRecord.workspace_id == workspace_id,
                StrategyCardRecord.card_id == card_id,
                StrategyCardRecord.version == version,
            )
        )

    async def list_cards(
        self,
        *,
        workspace_id: UUID,
        is_active: bool | None = None,
    ) -> list[StrategyCardRecord]:
        """Liệt kê danh sách bản ghi StrategyCardRecord theo workspace."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        stmt = select(StrategyCardRecord).where(StrategyCardRecord.workspace_id == workspace_id)
        if is_active is not None:
            stmt = stmt.where(StrategyCardRecord.is_active == is_active)
        stmt = stmt.order_by(StrategyCardRecord.created_at.desc())
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def get_active_card(
        self,
        *,
        workspace_id: UUID,
        card_id: str,
    ) -> StrategyCard | None:
        """Lấy StrategyCard đang có hiệu lực (is_active=True)."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(StrategyCardRecord).where(
                StrategyCardRecord.workspace_id == workspace_id,
                StrategyCardRecord.card_id == card_id,
                StrategyCardRecord.is_active.is_(True),
            )
        )
        return record.to_domain() if record is not None else None

    async def set_active_card(
        self,
        *,
        workspace_id: UUID,
        card_id: str,
        version: str,
    ) -> StrategyCardRecord:
        """Kích hoạt phiên bản cụ thể của chiến lược và hạ cờ các phiên bản cũ."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        await self._bind_workspace(workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"strategy-card:{workspace_id}:{card_id}"},
        )

        target = await self._session.scalar(
            select(StrategyCardRecord).where(
                StrategyCardRecord.workspace_id == workspace_id,
                StrategyCardRecord.card_id == card_id,
                StrategyCardRecord.version == version,
            )
        )
        if target is None:
            raise StrategyCardNotFoundError(
                f"Không tìm thấy thẻ chiến lược '{card_id}' phiên bản '{version}'."
            )

        await self._session.execute(
            text(
                "UPDATE strategy_cards SET is_active = FALSE "
                "WHERE workspace_id = :workspace_id AND card_id = :card_id"
            ),
            {"workspace_id": str(workspace_id), "card_id": card_id},
        )
        target.is_active = True
        await self._session.flush()
        return target
