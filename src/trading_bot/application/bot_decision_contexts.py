"""Adapter cấu hình hiệu lực MB-02 sang context quyết định MB-03 và điều kiện fan-out.

Ruling tích hợp (controller, 2026-10-25): fan-out evidence chỉ dành cho bot có cấu hình
bot PUBLISHED hiệu lực tại `as_of`. Bot chưa publish (hoặc config không resolve được) bị loại
khỏi fan-out kèm mã lý do được log và trả về cho caller; đường PAPER hiện có của bot đó vẫn
dùng root-fallback của MB-02.

Tính tất định: nội dung của một context đã được chọn chỉ phụ thuộc các version đã publish
hiệu lực tại `as_of` (root settings, config bot, release), nên resolve lại ở cùng `as_of`
luôn cho cùng context/context hash (consumer ghim context hash vào job). Riêng việc một bot
có được *chọn* hay không vẫn có thể đổi theo trạng thái hiện tại của bot/account (ENABLED,
PAPER) và của strategy release trong workspace (vd. release bị RETIRED làm resolver lỗi);
khi đó bot bị loại kèm lý do chứ không bao giờ nhận context có nội dung khác.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.bot_runtime_entities import (
    AccountActivityIncidentRecord,
)
from trading_bot.adapters.database.models.entities import TradingAccount
from trading_bot.adapters.database.repositories.bot_runtime_controls import read_runtime_control
from trading_bot.adapters.database.repositories.bot_runtime_states import (
    BotRuntimeStateRepository,
)
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
# Mã loại theo trạng thái runtime (MB05.8, ruling 18).
BOT_DISABLED = "BOT_DISABLED"
BOT_PAUSED = "BOT_PAUSED"
WORKSPACE_KILLED = "WORKSPACE_KILLED"
EXTERNAL_ACTIVITY_UNRECONCILED = "EXTERNAL_ACTIVITY_UNRECONCILED"
BOT_RECOVERY_REQUIRED = "BOT_RECOVERY_REQUIRED"
BOT_STANDBY = "BOT_STANDBY"


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
    """Bot PAPER của workspace; `enabled=False` khi bot hoặc account DISABLED (bị loại)."""

    bot_id: UUID
    account_id: UUID
    enabled: bool = True


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


RuntimeGate = Callable[[ScopedUnitOfWork, RuntimeBotRef], Awaitable[str | None]]


async def list_enabled_runtime_bots(uow: ScopedUnitOfWork) -> tuple[RuntimeBotRef, ...]:
    """Mọi bot PAPER của workspace kèm cờ ENABLED (bot và account) để loại có mã lý do."""
    rows = (
        await uow.session.execute(
            select(
                BotInstance.id,
                BotInstance.trading_account_id,
                BotInstance.status,
                TradingAccount.status,
            )
            .join(
                TradingAccount,
                (TradingAccount.workspace_id == BotInstance.workspace_id)
                & (TradingAccount.id == BotInstance.trading_account_id),
            )
            .where(BotInstance.workspace_id == uow.workspace_id, TradingAccount.mode == "PAPER")
            .order_by(BotInstance.id)
        )
    ).all()
    return tuple(
        RuntimeBotRef(
            bot_id=bot_id,
            account_id=account_id,
            enabled=bot_status == "ENABLED" and account_status == "ENABLED",
        )
        for bot_id, account_id, bot_status, account_status in rows
    )


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
        runtime_gate: RuntimeGate | None = None,
    ) -> None:
        self._sessions = sessions
        self._unit_of_work_factory = unit_of_work_factory
        self._list_bots = list_bots
        self._load_config = load_config
        self._on_exclusion = on_exclusion
        self._runtime_gate = runtime_gate

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
        if not bot.enabled:
            return BOT_DISABLED
        try:
            async with self._unit_of_work_factory(self._sessions, workspace_id=workspace_id) as uow:
                bound = await uow.bind_account(bot.account_id)
                if bound != bot.bot_id:
                    return BOT_CONFIG_SCOPE_MISMATCH
                if self._runtime_gate is not None:
                    # Đọc control/incident/state trong scope đúng bot; resume không backfill.
                    blocked = await self._runtime_gate(uow, bot)
                    if blocked is not None:
                        return blocked
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


async def _count_open_incidents(session: AsyncSession, *, workspace_id: UUID, bot_id: UUID) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(AccountActivityIncidentRecord)
        .where(
            AccountActivityIncidentRecord.workspace_id == workspace_id,
            AccountActivityIncidentRecord.bot_id == bot_id,
            AccountActivityIncidentRecord.status == "OPEN",
        )
    )
    return int(value or 0)


async def _load_state(session: AsyncSession, *, workspace_id: UUID, bot_id: UUID) -> Any:
    return await BotRuntimeStateRepository(session).get(workspace_id=workspace_id, bot_id=bot_id)


class RuntimeFanOutGate:
    """Loại bot khỏi fan-out theo control/incident/readiness (ruling 18), đọc trong scope bot.

    Chưa có hàng `bot_runtime_state` nghĩa là bot chưa từng chạy runtime: không loại theo
    readiness (giữ hành vi MB-03 cho môi trường chưa chạy runtime).
    """

    def __init__(
        self,
        *,
        control_reader: Callable[..., Awaitable[Any]] = read_runtime_control,
        incident_counter: Callable[..., Awaitable[int]] = _count_open_incidents,
        state_loader: Callable[..., Awaitable[Any]] = _load_state,
    ) -> None:
        self._control_reader = control_reader
        self._incident_counter = incident_counter
        self._state_loader = state_loader

    async def __call__(self, uow: ScopedUnitOfWork, bot: RuntimeBotRef) -> str | None:
        scope = {"workspace_id": uow.workspace_id, "bot_id": bot.bot_id}
        control = await self._control_reader(uow.session, **scope)
        if control.workspace_kill_state.value == "KILLED":
            return WORKSPACE_KILLED
        if control.bot_entry_state.value == "PAUSED":
            return BOT_PAUSED
        if await self._incident_counter(uow.session, **scope):
            return EXTERNAL_ACTIVITY_UNRECONCILED
        state = await self._state_loader(uow.session, **scope)
        readiness = getattr(state, "readiness", None) if state is not None else None
        if readiness in ("RECOVERY_REQUIRED", "STARTING"):
            return BOT_RECOVERY_REQUIRED
        if readiness == "STANDBY":
            return BOT_STANDBY
        return None


class FanOutExclusionRecorder:
    """Lưu bền lý do loại: `bot_runtime_state.fanout_exclusion_code` (bot đang giữ lease, có
    fence) và audit `bot.fanout.excluded` chỉ khi mã đổi (khử nhiễu, ruling 18).

    Bot không do process này giữ lease (DISABLED, STANDBY ở process khác) không ghi state được
    (không có fence): mã chỉ được khử nhiễu trong bộ nhớ process và vẫn có audit khi đổi.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession] | None,
        workspace_id: UUID,
        accounts: Callable[[UUID], Any],
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        state_repository_factory: Callable[[Any], Any] = BotRuntimeStateRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._accounts = accounts
        self._unit_of_work_factory = unit_of_work_factory
        self._state_repository_factory = state_repository_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._memory: dict[UUID, str | None] = {}

    async def record(self, eligibility: FanOutEligibility) -> None:
        codes: dict[tuple[UUID, UUID], str | None] = {
            (item.bot_id, item.account_id): item.reason_code for item in eligibility.exclusions
        }
        for context in eligibility.contexts:
            codes[(context.bot_id, context.account_id)] = None
        for (bot_id, account_id), code in sorted(codes.items(), key=lambda item: str(item[0][0])):
            await self._record_one(bot_id, account_id, code)
        for bot_id in [bot for bot, value in self._memory.items() if value is not None]:
            if all(key[0] != bot_id for key in codes):
                self._memory[bot_id] = None

    async def _record_one(self, bot_id: UUID, account_id: UUID, code: str | None) -> None:
        account = self._accounts(account_id)
        held = account is not None and getattr(account, "bot_id", None) == bot_id
        async with self._unit_of_work_factory(
            self._sessions, workspace_id=self._workspace_id, bot_id=bot_id if held else None
        ) as uow:
            previous = self._memory.get(bot_id)
            repository = self._state_repository_factory(uow.session) if held else None
            state = None
            if repository is not None:
                state = await repository.get(workspace_id=self._workspace_id, bot_id=bot_id)
                previous = getattr(state, "fanout_exclusion_code", None) if state else None
            if code is not None and code != previous:
                uow.record_audit(
                    action="bot.fanout.excluded",
                    target_type="bot_instance",
                    target_id=bot_id,
                    reason=code,
                )
            if repository is not None and code != previous:
                await repository.upsert(
                    fence=account.fence,
                    workspace_id=self._workspace_id,
                    bot_id=bot_id,
                    account_id=account_id,
                    readiness=getattr(state, "readiness", None) or "READY",
                    reason_code=getattr(state, "reason_code", None) if state else None,
                    heartbeat_at=self._clock(),
                    fanout_exclusion_code=code,
                )
        self._memory[bot_id] = code
