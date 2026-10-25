"""Adapter cấu hình hiệu lực MB-02 sang context quyết định MB-03 và điều kiện fan-out.

Ruling tích hợp (controller, 2026-10-25): fan-out evidence chỉ dành cho bot có cấu hình
bot PUBLISHED hiệu lực tại `as_of`. Bot chưa publish (hoặc config không resolve được) bị loại
khỏi fan-out kèm mã lý do được log và trả về cho caller; đường PAPER hiện có của bot đó vẫn
dùng root-fallback của MB-02. Kết quả tất định theo `as_of`: cùng dữ liệu đã publish luôn
cho cùng tập context (consumer ghim context hash vào job nên phụ thuộc điều này).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.bot_instances import BotInstanceRepository
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    EffectiveBotConfig,
    load_effective_bot_config,
)
from trading_bot.application.bot_dispatcher import BotDecisionContext
from trading_bot.application.outbox import UnitOfWork

logger = logging.getLogger(__name__)

BOT_CONFIG_NOT_PUBLISHED = "BOT_CONFIG_NOT_PUBLISHED"
BOT_CONFIG_SCOPE_MISMATCH = "BOT_CONFIG_SCOPE_MISMATCH"
BOT_CONFIG_HASH_MISMATCH = "BOT_CONFIG_HASH_MISMATCH"


def bot_decision_context_from_effective(config: EffectiveBotConfig) -> BotDecisionContext:
    """Chuyển `EffectiveBotConfig` đã kiểm hash thành context tối thiểu của dispatcher.

    `root_settings_version_id` là root `workspace_settings_version` (ruling fix round MB-03):
    `strategy_selection()` của context trùng với selection của cấu hình hiệu lực. Không đưa
    `resolved_at` vào context để kết quả chỉ phụ thuộc phiên bản đã publish.
    """
    if not config.verify_hash():
        raise BotConfigResolutionError(BOT_CONFIG_HASH_MISMATCH)
    return BotDecisionContext(
        workspace_id=config.workspace_id,
        bot_id=config.bot_id,
        account_id=config.account_id,
        bot_config_version_id=config.bot_config_version_id,
        bot_config_hash=config.bot_config_hash,
        root_settings_version_id=config.settings_version_id,
        root_settings_hash=config.settings_hash,
        strategy_release_id=config.strategy.release_id,
        symbols=config.symbols,
        effective_at=config.effective_at,
        strategy_parameters=config.strategy.parameters,
    )


@dataclass(frozen=True, slots=True)
class RuntimeBotRef:
    """Bot runtime ứng viên fan-out (bot ENABLED trên account PAPER ENABLED)."""

    bot_id: UUID
    account_id: UUID


@dataclass(frozen=True, slots=True)
class FanOutExclusion:
    """Bot bị loại khỏi fan-out và mã lý do (không chứa dữ liệu cấu hình)."""

    bot_id: UUID
    account_id: UUID
    reason_code: str


@dataclass(frozen=True, slots=True)
class FanOutEligibility:
    """Kết quả xét điều kiện fan-out của một workspace tại `as_of`."""

    workspace_id: UUID
    as_of: datetime
    contexts: tuple[BotDecisionContext, ...]
    exclusions: tuple[FanOutExclusion, ...]


class ScopedUnitOfWork(Protocol):
    """Phần UnitOfWork provider dùng: scope workspace, bind một account/bot mỗi transaction."""

    workspace_id: UUID

    @property
    def session(self) -> AsyncSession: ...

    async def bind_account(self, account_id: UUID, *, allow_switch: bool = False) -> UUID: ...


UnitOfWorkFactory = Callable[..., AbstractAsyncContextManager[ScopedUnitOfWork]]
BotLister = Callable[[ScopedUnitOfWork], Awaitable[Sequence[RuntimeBotRef]]]
ConfigLoader = Callable[
    [ScopedUnitOfWork, RuntimeBotRef, datetime], Awaitable[EffectiveBotConfig | None]
]


async def list_enabled_runtime_bots(uow: ScopedUnitOfWork) -> tuple[RuntimeBotRef, ...]:
    """Bot ENABLED trên account PAPER ENABLED của workspace (runtime thấy identity workspace)."""
    bots = await BotInstanceRepository(uow.session).list_enabled_for_runtime(
        workspace_id=uow.workspace_id
    )
    return tuple(RuntimeBotRef(bot_id=bot.id, account_id=bot.trading_account_id) for bot in bots)


async def load_published_bot_config(
    uow: ScopedUnitOfWork, bot: RuntimeBotRef, as_of: datetime
) -> EffectiveBotConfig | None:
    """Resolve config hiệu lực qua resolver MB-02; `None` khi bot chưa có version publish."""
    return await load_effective_bot_config(
        uow.session, workspace_id=uow.workspace_id, account_id=bot.account_id, as_of=as_of
    )


class EffectiveBotDecisionContextProvider:
    """`BotDecisionContextProvider` dựa trên resolver/repository MB-02.

    Liệt kê bot trong một transaction scope workspace, rồi resolve mỗi bot trong transaction
    riêng đã `bind_account` (một transaction một bot scope, không `allow_switch`) để RLS
    runtime chỉ cho thấy version PUBLISHED của đúng bot đó.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work_factory: UnitOfWorkFactory = UnitOfWork,
        list_bots: BotLister = list_enabled_runtime_bots,
        load_config: ConfigLoader = load_published_bot_config,
        on_exclusion: Callable[[FanOutExclusion], None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._unit_of_work_factory = unit_of_work_factory
        self._list_bots = list_bots
        self._load_config = load_config
        self._on_exclusion = on_exclusion

    async def contexts_for(
        self, *, workspace_id: UUID, as_of: datetime
    ) -> tuple[BotDecisionContext, ...]:
        return (await self.eligibility(workspace_id=workspace_id, as_of=as_of)).contexts

    async def eligibility(self, *, workspace_id: UUID, as_of: datetime) -> FanOutEligibility:
        """Context của bot đủ điều kiện và danh sách bot bị loại, sắp theo bot_id."""
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of phải có múi giờ.")
        point = as_of.astimezone(UTC)
        async with self._unit_of_work_factory(self._sessions, workspace_id=workspace_id) as uow:
            bots = sorted(await self._list_bots(uow), key=lambda item: item.bot_id)
        contexts: list[BotDecisionContext] = []
        exclusions: list[FanOutExclusion] = []
        for bot in bots:
            outcome = await self._resolve(workspace_id, bot, point)
            if isinstance(outcome, BotDecisionContext):
                contexts.append(outcome)
                continue
            exclusion = FanOutExclusion(
                bot_id=bot.bot_id, account_id=bot.account_id, reason_code=outcome
            )
            exclusions.append(exclusion)
            logger.warning(
                "Bot %s (account %s) bị loại khỏi fan-out evidence tại %s: %s",
                bot.bot_id,
                bot.account_id,
                point.isoformat(),
                outcome,
                extra={
                    "workspace_id": str(workspace_id),
                    "bot_id": str(bot.bot_id),
                    "reason_code": outcome,
                },
            )
            if self._on_exclusion is not None:
                self._on_exclusion(exclusion)
        return FanOutEligibility(
            workspace_id=workspace_id,
            as_of=point,
            contexts=tuple(contexts),
            exclusions=tuple(exclusions),
        )

    async def _resolve(
        self, workspace_id: UUID, bot: RuntimeBotRef, as_of: datetime
    ) -> BotDecisionContext | str:
        """Trả context hoặc mã lý do loại; lỗi hạ tầng (DB) vẫn ném để consumer retry."""
        try:
            async with self._unit_of_work_factory(self._sessions, workspace_id=workspace_id) as uow:
                bound = await uow.bind_account(bot.account_id)
                if bound != bot.bot_id:
                    return BOT_CONFIG_SCOPE_MISMATCH
                config = await self._load_config(uow, bot, as_of)
            if config is None:
                return BOT_CONFIG_NOT_PUBLISHED
            if (config.workspace_id, config.bot_id, config.account_id) != (
                workspace_id,
                bot.bot_id,
                bot.account_id,
            ):
                return BOT_CONFIG_SCOPE_MISMATCH
            return bot_decision_context_from_effective(config)
        except BotConfigResolutionError as error:
            return error.code
