"""Worker gửi signal-only Telegram từ BotDecision đã lưu; không có cổng thực thi lệnh."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.telegram_signals import (
    SignalDeliveryClaim,
    SignalSubscriptionTarget,
    TelegramSignalRepository,
)
from trading_bot.adapters.telegram.bot_api_sender import TelegramApiError
from trading_bot.application.outbox import UnitOfWork, retry_delay_seconds
from trading_bot.application.telegram_signals import (
    build_signal_message,
    is_candidate_deliverable,
)
from trading_bot.ports.telegram import TelegramMessageSender

logger = logging.getLogger(__name__)


class TelegramSignalDeliveryWorker:
    """Poll candidate decisions, lưu delivery bền vững rồi gửi qua Bot API.

    Worker không nhận trading gateway, exchange credential, planner, reservation hay ledger port;
    DB role của nó cũng không có quyền ghi vào các bảng đó.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: UUID,
        sender: TelegramMessageSender,
        worker_id: str,
        tick_seconds: float = 5.0,
        max_candidate_age: timedelta = timedelta(minutes=2),
        lease_duration: timedelta = timedelta(seconds=45),
        max_attempts: int = 5,
        batch_size: int = 100,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        unit_of_work: Callable[..., UnitOfWork] = UnitOfWork,
        repository: Callable[[AsyncSession], TelegramSignalRepository] = TelegramSignalRepository,
    ) -> None:
        if not worker_id.strip() or not 0.1 <= tick_seconds <= 300:
            raise ValueError("Thông số Telegram signal worker không hợp lệ.")
        if max_candidate_age <= timedelta(0) or lease_duration <= timedelta(0):
            raise ValueError("Cửa sổ freshness và lease phải dương.")
        if max_attempts < 1 or batch_size < 1:
            raise ValueError("Số lần thử và batch_size phải dương.")
        self._sessions = session_factory
        self._workspace_id = workspace_id
        self._sender = sender
        self._worker_id = worker_id
        self._tick_seconds = tick_seconds
        self._max_candidate_age = max_candidate_age
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._batch_size = batch_size
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._unit_of_work = unit_of_work
        self._repository = repository
        self.last_tick_succeeded = False
        self.last_tick_at: datetime | None = None
        self.last_error_code: str | None = None
        self._last_sent_by_chat: dict[int, datetime] = defaultdict(
            lambda: datetime.min.replace(tzinfo=UTC)
        )

    async def run(self, stopping: asyncio.Event) -> None:
        """Chạy tick đến khi lifespan yêu cầu dừng; lỗi tick sẽ thử lại ở chu kỳ sau."""
        while not stopping.is_set():
            try:
                await self.tick()
                self.last_tick_succeeded = True
                self.last_tick_at = self._now()
                self.last_error_code = None
            except Exception as error:
                self.last_tick_succeeded = False
                self.last_error_code = _error_code(error)
                logger.warning("Telegram signal worker tick lỗi; code=%s", self.last_error_code)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                continue

    async def tick(self) -> int:
        """Phát hiện candidate mới, tạo delivery idempotent và gửi các delivery tới hạn."""
        now = self._now()
        _require_aware(now)
        async with self._unit_of_work(self._sessions, workspace_id=self._workspace_id) as uow:
            targets = await self._repository(uow.session).list_enabled_subscriptions(
                workspace_id=self._workspace_id
            )
        by_bot: dict[UUID, list[SignalSubscriptionTarget]] = defaultdict(list)
        for target in targets:
            by_bot[target.bot_id].append(target)

        for bot_id, bot_targets in by_bot.items():
            async with self._unit_of_work(
                self._sessions, workspace_id=self._workspace_id, bot_id=bot_id
            ) as uow:
                repository = self._repository(uow.session)
                candidates = await repository.recent_candidates(
                    workspace_id=self._workspace_id,
                    bot_id=bot_id,
                    created_after=now - self._max_candidate_age,
                    limit=self._batch_size,
                )
                for candidate in candidates:
                    if not is_candidate_deliverable(
                        candidate, now=now, max_age=self._max_candidate_age
                    ):
                        continue
                    for target in bot_targets:
                        if not target.filters.matches(candidate):
                            continue
                        await repository.enqueue_delivery(
                            workspace_id=self._workspace_id,
                            bot_id=bot_id,
                            subscription_id=target.subscription_id,
                            decision_id=candidate.decision_id,
                            available_at=now,
                        )

        sent = 0
        for bot_id in by_bot:
            for _ in range(self._batch_size):
                claim = await self._claim_one(bot_id, now)
                if claim is None:
                    break
                # claim_one đã đối chiếu lại bộ lọc hiện hành trong cùng transaction và
                # ghi receipt FILTERED; không cấp lease hoặc gửi candidate cũ tới chat.
                if claim.filtered:
                    continue
                if not is_candidate_deliverable(
                    claim.candidate, now=self._now(), max_age=self._max_candidate_age
                ):
                    await self._mark_expired(claim)
                    continue
                await self._pace_chat(claim.chat_id)
                if not is_candidate_deliverable(
                    claim.candidate, now=self._now(), max_age=self._max_candidate_age
                ):
                    await self._mark_expired(claim)
                    continue
                if not await self._renew_claim(claim):
                    continue
                if not is_candidate_deliverable(
                    claim.candidate, now=self._now(), max_age=self._max_candidate_age
                ):
                    await self._mark_expired(claim)
                    continue
                try:
                    await self._sender.send_message(
                        claim.chat_id, build_signal_message(claim.candidate)
                    )
                except TelegramApiError as error:
                    await self._mark_failed(claim, error.code, error.retry_after)
                except Exception as error:
                    await self._mark_failed(claim, _error_code(error), None)
                else:
                    await self._mark_sent(claim)
                    self._last_sent_by_chat[claim.chat_id] = self._now()
                    sent += 1
        return sent

    async def _claim_one(self, bot_id: UUID, now: datetime) -> SignalDeliveryClaim | None:
        async with self._unit_of_work(
            self._sessions, workspace_id=self._workspace_id, bot_id=bot_id
        ) as uow:
            return await self._repository(uow.session).claim_one(
                workspace_id=self._workspace_id,
                bot_id=bot_id,
                worker_id=self._worker_id,
                now=now,
                lease_duration=self._lease_duration,
                max_attempts=self._max_attempts,
            )

    async def _mark_sent(self, claim: SignalDeliveryClaim) -> None:
        async with self._unit_of_work(
            self._sessions, workspace_id=self._workspace_id, bot_id=claim.bot_id
        ) as uow:
            await self._repository(uow.session).mark_sent(
                workspace_id=self._workspace_id,
                bot_id=claim.bot_id,
                delivery_id=claim.delivery_id,
                worker_id=self._worker_id,
                sent_at=self._now(),
            )

    async def _renew_claim(self, claim: SignalDeliveryClaim) -> bool:
        async with self._unit_of_work(
            self._sessions, workspace_id=self._workspace_id, bot_id=claim.bot_id
        ) as uow:
            return await self._repository(uow.session).renew_claim(
                workspace_id=self._workspace_id,
                bot_id=claim.bot_id,
                delivery_id=claim.delivery_id,
                worker_id=self._worker_id,
                lease_expires_at=self._now() + self._lease_duration,
            )

    async def _mark_expired(self, claim: SignalDeliveryClaim) -> None:
        async with self._unit_of_work(
            self._sessions, workspace_id=self._workspace_id, bot_id=claim.bot_id
        ) as uow:
            await self._repository(uow.session).mark_expired(
                workspace_id=self._workspace_id,
                bot_id=claim.bot_id,
                delivery_id=claim.delivery_id,
                worker_id=self._worker_id,
            )

    async def _mark_failed(
        self, claim: SignalDeliveryClaim, error_code: str, retry_after: int | None
    ) -> None:
        if retry_after is None:
            delay = retry_delay_seconds(claim.attempts, base_seconds=2, max_seconds=300)
        else:
            delay = min(max(retry_after, 1), 86_400)
        async with self._unit_of_work(
            self._sessions, workspace_id=self._workspace_id, bot_id=claim.bot_id
        ) as uow:
            await self._repository(uow.session).mark_failed(
                workspace_id=self._workspace_id,
                bot_id=claim.bot_id,
                delivery_id=claim.delivery_id,
                worker_id=self._worker_id,
                available_at=self._now() + timedelta(seconds=delay),
                error_code=error_code,
                max_attempts=self._max_attempts,
                attempts=claim.attempts,
            )

    async def _pace_chat(self, chat_id: int) -> None:
        now = self._now()
        elapsed = (now - self._last_sent_by_chat[chat_id]).total_seconds()
        if elapsed < 1.1:
            await self._sleep(1.1 - elapsed)


def _error_code(error: BaseException) -> str:
    name = type(error).__name__.upper()
    return f"TELEGRAM_DELIVERY_{name[:80]}"


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Thời gian worker phải có múi giờ.")
