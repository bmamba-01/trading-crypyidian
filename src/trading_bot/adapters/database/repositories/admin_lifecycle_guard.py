"""Projection kiểm tra bot có phẳng cho luồng disable admin-web."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


class AdminLifecycleScopeDenied(PermissionError):
    """PostgreSQL từ chối kiểm tra trạng thái vòng đời theo membership hiện hành."""


@dataclass(frozen=True, slots=True)
class LifecycleGuardSnapshot:
    """Tồn tại và số đếm an toàn của một bot; không chứa dữ liệu ledger."""

    bot_exists: bool
    open_positions: int
    open_orders: int
    held_reservations: int


class AdminLifecycleGuardRepository:
    """Gọi projection DB hẹp, không đọc trực tiếp raw ledger bằng admin-safe."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_snapshot(self, *, workspace_id: UUID, bot_id: UUID) -> LifecycleGuardSnapshot:
        try:
            row: Any = (
                (
                    await self._session.execute(
                        text(
                            """
                            SELECT * FROM public.wp06_admin_bot_lifecycle_snapshot(
                                CAST(:workspace_id AS uuid), CAST(:bot_id AS uuid))
                            """
                        ),
                        {"workspace_id": workspace_id, "bot_id": bot_id},
                    )
                )
                .mappings()
                .one()
            )
        except DBAPIError as error:
            diagnostic = getattr(getattr(error.orig, "diag", None), "message_primary", None)
            if diagnostic in {
                "WP06_ADMIN_LIFECYCLE_SCOPE_DENIED",
                "WP06_ADMIN_LIFECYCLE_OWNER_REQUIRED",
            }:
                raise AdminLifecycleScopeDenied(
                    "Không đủ quyền kiểm tra vòng đời bot trong phạm vi hiện hành."
                ) from None
            raise

        return LifecycleGuardSnapshot(
            bot_exists=bool(row["bot_exists"]),
            open_positions=int(row["open_positions"]),
            open_orders=int(row["open_orders"]),
            held_reservations=int(row["held_reservations"]),
        )


__all__ = [
    "AdminLifecycleScopeDenied",
    "AdminLifecycleGuardRepository",
    "LifecycleGuardSnapshot",
]
