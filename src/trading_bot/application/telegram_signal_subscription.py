"""Điều phối opt-in tín hiệu theo bot và thành viên đã xác thực."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import BotInstance
from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.telegram_signals import TelegramSignalRepository
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.telegram_signals import TelegramSignalFilters
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal


@dataclass(frozen=True, slots=True)
class TelegramSignalSubscriptionState:
    bot_id: UUID
    enabled: bool
    binding_linked: bool = False
    filters: TelegramSignalFilters = field(default_factory=TelegramSignalFilters)


class TelegramSignalSubscriptionError(ValueError):
    """Lỗi opt-in có thông điệp tổng quát, không làm lộ binding khác."""


class TelegramSignalSubscriptionService:
    """Người dùng chỉ đổi subscription của chính mình trong phạm vi bot được xem."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = TelegramSignalRepository,
        grants: Callable[[Any], Any] = BotGrantRepository,
    ) -> None:
        self._sessions = sessions
        self._unit_of_work = unit_of_work
        self._repository = repository
        self._grants = grants

    async def get_state(
        self, principal: Principal, *, bot_id: UUID
    ) -> TelegramSignalSubscriptionState:
        """Đọc trạng thái opt-in và binding đang hoạt động của chính thành viên hiện tại."""
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
        ) as uow:
            await self._require_bot(uow.session, principal, bot_id)
            repository = self._repository(uow.session)
            binding = await repository.active_binding(
                workspace_id=principal.workspace_id,
                membership_id=principal.membership_id,
            )
            subscription = await repository.get_subscription(
                workspace_id=principal.workspace_id,
                membership_id=principal.membership_id,
                bot_id=bot_id,
            )
            linked = binding is not None
            enabled = bool(
                subscription is not None
                and subscription.enabled
                and binding is not None
                and subscription.telegram_binding_id == binding.id
            )
            return TelegramSignalSubscriptionState(
                bot_id=bot_id,
                enabled=enabled,
                binding_linked=linked,
                filters=_filters_from_row(subscription),
            )

    async def set_enabled(
        self, principal: Principal, *, bot_id: UUID, enabled: bool
    ) -> TelegramSignalSubscriptionState:
        if not isinstance(enabled, bool):
            raise TelegramSignalSubscriptionError("enabled chỉ nhận true hoặc false.")
        return await self.update_preference(
            principal,
            bot_id=bot_id,
            enabled=enabled,
            filters=None,
        )

    async def set_filters(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        filters: TelegramSignalFilters,
    ) -> TelegramSignalSubscriptionState:
        return await self.update_preference(
            principal,
            bot_id=bot_id,
            enabled=None,
            filters=filters,
        )

    async def update_preference(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        enabled: bool | None,
        filters: TelegramSignalFilters | None,
    ) -> TelegramSignalSubscriptionState:
        if enabled is not None and not isinstance(enabled, bool):
            raise TelegramSignalSubscriptionError("enabled chỉ nhận true hoặc false.")
        if enabled is None and filters is None:
            raise TelegramSignalSubscriptionError(
                "Cần cập nhật trạng thái hoặc ít nhất một bộ lọc."
            )
        if filters is not None and not isinstance(filters, TelegramSignalFilters):
            raise TelegramSignalSubscriptionError("Bộ lọc tín hiệu không hợp lệ.")
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
        ) as uow:
            await self._require_bot(uow.session, principal, bot_id)
            repository = self._repository(uow.session)
            current = await repository.get_subscription(
                workspace_id=principal.workspace_id,
                membership_id=principal.membership_id,
                bot_id=bot_id,
            )
            must_have_binding = enabled is True or filters is not None or current is None
            binding = (
                await repository.active_binding(
                    workspace_id=principal.workspace_id,
                    membership_id=principal.membership_id,
                )
                if must_have_binding
                else None
            )
            if must_have_binding and binding is None:
                raise TelegramSignalSubscriptionError(
                    "Hãy liên kết Telegram riêng trước khi chỉnh subscription."
                )

            if binding is not None:
                row = await repository.put_subscription(
                    workspace_id=principal.workspace_id,
                    membership_id=principal.membership_id,
                    bot_id=bot_id,
                    binding=binding,
                    enabled=enabled if enabled is not None else bool(current and current.enabled),
                    filters=filters,
                )
                target_id = row.id
                result = row.enabled
            else:
                await repository.set_bot_subscription_enabled(
                    workspace_id=principal.workspace_id,
                    membership_id=principal.membership_id,
                    bot_id=bot_id,
                    enabled=False,
                )
                # Repository trả số hàng cập nhật, không phải giá trị enabled.
                result = False
                row = current
                target_id = row.id if row is not None else bot_id
            uow.record_audit(
                action="telegram.signal_subscription.preference_updated",
                target_type="telegram_signal_subscription",
                target_id=target_id,
                reason="USER_SIGNAL_PREFERENCE",
                after_state={
                    "bot_id": str(bot_id),
                    "enabled": result,
                    "filters": _filters_from_row(row).as_dict(),
                },
            )
            return TelegramSignalSubscriptionState(
                bot_id=bot_id,
                enabled=result,
                binding_linked=binding is not None
                or (
                    row is not None
                    and await repository.active_binding(
                        workspace_id=principal.workspace_id,
                        membership_id=principal.membership_id,
                    )
                    is not None
                ),
                filters=_filters_from_row(row),
            )

    async def _require_bot(self, session: AsyncSession, principal: Principal, bot_id: UUID) -> None:
        try:
            await self._grants(session).require(
                principal, bot_id=bot_id, permission=BotPermission.READ
            )
        except PermissionError as error:
            raise TelegramSignalSubscriptionError(
                "Không tìm thấy bot trong phạm vi được cấp quyền."
            ) from error
        bot_exists = await session.scalar(
            select(BotInstance.id).where(
                BotInstance.workspace_id == principal.workspace_id,
                BotInstance.id == bot_id,
            )
        )
        if bot_exists is None:
            raise TelegramSignalSubscriptionError(
                "Không tìm thấy bot trong phạm vi được cấp quyền."
            )


def _filters_from_row(row: Any) -> TelegramSignalFilters:
    if row is None:
        return TelegramSignalFilters()
    return TelegramSignalFilters(
        symbols=tuple(row.symbols or ()),
        timeframes=tuple(row.timeframes or ()),
        directions=tuple(row.directions or ()),
        strategy_ids=tuple(row.strategy_ids or ()),
        setup_codes=tuple(row.setup_codes or ()),
    )
