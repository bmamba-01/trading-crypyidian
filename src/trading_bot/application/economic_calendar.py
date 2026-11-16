"""Ca sử dụng lịch sự kiện kinh tế (CK-10): đọc theo workspace, thêm/sửa/xóa mềm có audit.

Quyền: đọc cần ``workspace:read``; ghi cần ``settings:write`` (owner, strategy_admin, …). Mỗi lần
ghi tạo một ``audit_event`` (before/after hash) trong **cùng** transaction với thay đổi dữ liệu.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.economic_calendar import (
    EconomicCalendarRepository,
    event_dict,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.domain.market.economic_calendar import EconomicEventInput


class EconomicCalendarError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class EconomicCalendarService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = EconomicCalendarRepository,
    ) -> None:
        self._sessions = sessions
        self._unit_of_work = unit_of_work
        self._repository = repository

    def _require(self, principal: Principal, permission: Permission) -> None:
        if not authorize(principal, permission):
            raise EconomicCalendarError(403, "Bạn không có quyền với lịch sự kiện.")

    async def list_between(
        self, principal: Principal, *, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        self._require(principal, Permission.WORKSPACE_READ)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            events: list[dict[str, Any]] = await self._repository(unit.session).list_between(
                workspace_id=principal.workspace_id, start=start, end=end
            )
        return events

    async def around(
        self, principal: Principal, *, now: datetime, days: int = 7
    ) -> list[dict[str, Any]]:
        """Sự kiện trong ±``days`` ngày quanh ``now`` (cho bảng Entry và tab Tin & Lịch)."""
        return await self.list_between(
            principal, start=now - timedelta(days=days), end=now + timedelta(days=days)
        )

    async def create(
        self, principal: Principal, data: EconomicEventInput, *, reason: str | None = None
    ) -> dict[str, Any]:
        self._require(principal, Permission.SETTINGS_WRITE)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            row = await self._repository(unit.session).create(
                workspace_id=principal.workspace_id,
                membership_id=principal.membership_id,
                data=data,
            )
            unit.record_audit(
                action="economic_event.create",
                target_type="economic_event",
                target_id=row.id,
                reason=reason,
                after_state=data.as_state(),
            )
            return event_dict(row)

    async def update(
        self,
        principal: Principal,
        event_id: UUID,
        data: EconomicEventInput,
        *,
        reason: str | None = None,
    ) -> None:
        self._require(principal, Permission.SETTINGS_WRITE)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repository = self._repository(unit.session)
            row = await repository.get_for_update(
                workspace_id=principal.workspace_id, event_id=event_id
            )
            if row is None:
                raise EconomicCalendarError(404, "Không tìm thấy sự kiện.")
            before = EconomicEventInput(
                row.title, row.region, row.impact, row.event_at, row.note
            ).as_state()
            await repository.update(row, data)
            unit.record_audit(
                action="economic_event.update",
                target_type="economic_event",
                target_id=event_id,
                reason=reason,
                before_state=before,
                after_state=data.as_state(),
            )

    async def delete(
        self, principal: Principal, event_id: UUID, *, reason: str | None = None
    ) -> None:
        self._require(principal, Permission.SETTINGS_WRITE)
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repository = self._repository(unit.session)
            row = await repository.get_for_update(
                workspace_id=principal.workspace_id, event_id=event_id
            )
            if row is None:
                raise EconomicCalendarError(404, "Không tìm thấy sự kiện.")
            before = EconomicEventInput(
                row.title, row.region, row.impact, row.event_at, row.note
            ).as_state()
            await repository.soft_delete(row)
            unit.record_audit(
                action="economic_event.delete",
                target_type="economic_event",
                target_id=event_id,
                reason=reason,
                before_state=before,
            )
