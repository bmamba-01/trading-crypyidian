"""TG-3: worker gửi kết quả trade PAPER và tin tổng hợp định kỳ qua Telegram.

Chạy trong process ``signal-worker`` cạnh worker tín hiệu, dùng cùng role DB
``crypidian_signal_worker`` (chỉ đọc theo cột ledger PAPER, ghi delivery ledger riêng).

Bảo đảm giao nhận giống tin tín hiệu:

- Delivery bền vững, khóa duy nhất ``(subscription, trade, OPENED|CLOSED)`` và
  ``(chat, window_start)`` → khởi động lại hay nhiều replica không gửi trùng.
- Nhận lease ``FOR UPDATE SKIP LOCKED``, gia hạn lease ngay trước HTTP, retry theo backoff hoặc
  ``retry_after`` của Telegram, quá ``max_attempts`` thì ``DEAD``.
- Giãn cách ≥ 1,1 giây mỗi chat qua ``ChatPacer`` dùng chung với worker tín hiệu.
- Không áp chống spam TG-2 (đây không phải tín hiệu).

Đường gửi gọi đúng một hàm cho mỗi loại tin (``send_trade_result``/``send_paper_report``) nhận
view model, nên có thể thay bằng bộ gửi ảnh thẻ mà không đổi worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.telegram_signals import (
    SignalSubscriptionTarget,
    TelegramSignalRepository,
)
from trading_bot.adapters.database.repositories.telegram_trade_results import (
    PaperReportClaim,
    TelegramTradeResultRepository,
    TradeNotificationClaim,
)
from trading_bot.adapters.telegram.bot_api_sender import TelegramApiError
from trading_bot.adapters.telegram.topic_sender import bind_topic
from trading_bot.application.outbox import UnitOfWork, retry_delay_seconds
from trading_bot.application.telegram_trade_results import (
    PaperReportView,
    TradeResultView,
    build_paper_report_view,
    build_trade_result_view,
    format_paper_report_text,
    format_trade_result_text,
    report_window,
)
from trading_bot.ports.telegram import TelegramMessageSender

logger = logging.getLogger(__name__)

CHAT_MIN_INTERVAL_SECONDS = 1.1

TradeResultSend = Callable[[TelegramMessageSender, int, TradeResultView], Awaitable[None]]
PaperReportSend = Callable[[TelegramMessageSender, int, PaperReportView], Awaitable[None]]


async def send_trade_result(
    sender: TelegramMessageSender, chat_id: int, view: TradeResultView
) -> None:
    """Gửi một kết quả trade; hiện là văn bản thuần (caption/fallback của ảnh thẻ sau này)."""
    await sender.send_message(chat_id, format_trade_result_text(view))


async def send_paper_report(
    sender: TelegramMessageSender, chat_id: int, view: PaperReportView
) -> None:
    """Gửi một tin tổng hợp PAPER; hiện là văn bản thuần."""
    await sender.send_message(chat_id, format_paper_report_text(view))


class ChatPacer:
    """Giãn cách tin mỗi chat ≥ ``interval`` giây, dùng chung giữa các worker cùng process.

    ``wait`` giữ chỗ khe gửi kế tiếp trước khi ngủ (không có ``await`` giữa đọc và ghi) nên hai
    task asyncio không cùng lấy một khe; ``mark_sent`` đẩy khe theo thời điểm gửi thực tế.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = CHAT_MIN_INTERVAL_SECONDS,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Khoảng giãn cách chat phải dương.")
        self._interval = timedelta(seconds=interval_seconds)
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._next_slot: dict[int, datetime] = {}

    async def wait(self, chat_id: int) -> None:
        now = self._now()
        slot = max(now, self._next_slot.get(chat_id, now))
        self._next_slot[chat_id] = slot + self._interval
        delay = (slot - now).total_seconds()
        if delay > 0:
            await self._sleep(delay)

    def mark_sent(self, chat_id: int) -> None:
        after = self._now() + self._interval
        self._next_slot[chat_id] = max(self._next_slot.get(chat_id, after), after)


class TelegramTradeResultWorker:
    """Phát hiện trade PAPER mở/đóng, ghi delivery idempotent rồi gửi; gửi tổng hợp định kỳ.

    Không nhận trading gateway, exchange credential, planner hay port ghi ledger.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: UUID,
        sender: TelegramMessageSender,
        worker_id: str,
        tick_seconds: float = 5.0,
        trade_results_enabled: bool = True,
        max_event_age: timedelta = timedelta(minutes=60),
        report_interval: timedelta | None = timedelta(hours=4),
        lease_duration: timedelta = timedelta(seconds=45),
        max_attempts: int = 5,
        batch_size: int = 100,
        pacer: ChatPacer | None = None,
        now: Callable[[], datetime] | None = None,
        unit_of_work: Callable[..., UnitOfWork] = UnitOfWork,
        signal_repository: Callable[[AsyncSession], TelegramSignalRepository] = (
            TelegramSignalRepository
        ),
        repository: Callable[[AsyncSession], TelegramTradeResultRepository] = (
            TelegramTradeResultRepository
        ),
        trade_result_send: TradeResultSend = send_trade_result,
        paper_report_send: PaperReportSend = send_paper_report,
    ) -> None:
        if not worker_id.strip() or not 0.1 <= tick_seconds <= 300:
            raise ValueError("Thông số Telegram trade-result worker không hợp lệ.")
        if max_event_age <= timedelta(0) or lease_duration <= timedelta(0):
            raise ValueError("Cửa sổ phát hiện và lease phải dương.")
        if report_interval is not None and report_interval < timedelta(minutes=1):
            raise ValueError("Chu kỳ tổng hợp tối thiểu 1 phút.")
        if max_attempts < 1 or batch_size < 1:
            raise ValueError("Số lần thử và batch_size phải dương.")
        self._sessions = session_factory
        self._workspace_id = workspace_id
        self._sender = sender
        self._worker_id = worker_id
        self._tick_seconds = tick_seconds
        self._trade_results_enabled = trade_results_enabled
        self._max_event_age = max_event_age
        self._report_interval = report_interval
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._batch_size = batch_size
        self._now = now or (lambda: datetime.now(UTC))
        self._pacer = pacer or ChatPacer(now=self._now)
        self._unit_of_work = unit_of_work
        self._signal_repository = signal_repository
        self._repository = repository
        self._trade_result_send = trade_result_send
        self._paper_report_send = paper_report_send
        self.last_tick_succeeded = False
        self.last_tick_at: datetime | None = None
        self.last_error_code: str | None = None

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
                logger.warning(
                    "Telegram trade-result worker tick lỗi; code=%s", self.last_error_code
                )
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                continue

    async def tick(self) -> int:
        """Một vòng: ghi delivery mới rồi gửi các delivery tới hạn; trả số tin đã gửi."""
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Thời gian worker phải có múi giờ.")
        if not self._trade_results_enabled and self._report_interval is None:
            return 0
        async with self._unit_of_work(self._sessions, workspace_id=self._workspace_id) as uow:
            targets = await self._signal_repository(uow.session).list_enabled_subscriptions(
                workspace_id=self._workspace_id
            )
        sent = 0
        if self._trade_results_enabled:
            by_bot: dict[UUID, list[SignalSubscriptionTarget]] = defaultdict(list)
            for target in targets:
                by_bot[target.bot_id].append(target)
            for bot_id, bot_targets in by_bot.items():
                await self._enqueue_trade_events(bot_id, bot_targets, now)
            for bot_id in by_bot:
                sent += await self._deliver_trade_results(bot_id, now)
        if self._report_interval is not None:
            sent += await self._run_reports(targets, now, self._report_interval)
        return sent

    # --- kết quả từng trade ------------------------------------------------------------------

    async def _enqueue_trade_events(
        self, bot_id: UUID, targets: list[SignalSubscriptionTarget], now: datetime
    ) -> None:
        async with self._unit_of_work(
            self._sessions, workspace_id=self._workspace_id, bot_id=bot_id
        ) as uow:
            repository = self._repository(uow.session)
            events = await repository.recent_trade_events(
                workspace_id=self._workspace_id,
                bot_id=bot_id,
                since=now - self._max_event_age,
                limit=self._batch_size,
            )
            for event in events:
                for target in targets:
                    # Lọc symbol của subscription (nếu có); hướng/chiến lược không áp cho kết quả.
                    symbols = target.filters.symbols
                    if symbols and event.symbol.upper() not in {s.upper() for s in symbols}:
                        continue
                    await repository.enqueue_trade_notification(
                        workspace_id=self._workspace_id,
                        bot_id=bot_id,
                        subscription_id=target.subscription_id,
                        trade_id=event.trade_id,
                        kind=event.kind,
                        available_at=now,
                    )

    async def _deliver_trade_results(self, bot_id: UUID, now: datetime) -> int:
        sent = 0
        for _ in range(self._batch_size):
            async with self._unit_of_work(
                self._sessions, workspace_id=self._workspace_id, bot_id=bot_id
            ) as uow:
                claim = await self._repository(uow.session).claim_trade_notification(
                    workspace_id=self._workspace_id,
                    bot_id=bot_id,
                    worker_id=self._worker_id,
                    now=now,
                    lease_duration=self._lease_duration,
                    max_attempts=self._max_attempts,
                )
            if claim is None:
                break
            try:
                view = build_trade_result_view(claim.source)
            except ValueError:
                await self._mark_failed(
                    claim.notification_id,
                    claim.attempts,
                    "TRADE_RESULT_VIEW_INVALID",
                    None,
                    bot_id=bot_id,
                    dead=True,
                )
                continue
            if await self._send(
                claim.chat_id,
                claim.notification_id,
                claim.attempts,
                self._trade_result_delivery(view, claim.thread_id),
                bot_id=bot_id,
            ):
                sent += 1
        return sent

    # --- tổng hợp định kỳ --------------------------------------------------------------------

    async def _run_reports(
        self, targets: tuple[SignalSubscriptionTarget, ...], now: datetime, interval: timedelta
    ) -> int:
        window_start, window_end = report_window(now, interval)
        chats = {target.chat_id for target in targets}
        async with self._unit_of_work(self._sessions, workspace_id=self._workspace_id) as uow:
            repository = self._repository(uow.session)
            for chat_id in sorted(chats):
                await repository.enqueue_report(
                    workspace_id=self._workspace_id,
                    chat_id=chat_id,
                    window_start=window_start,
                    window_end=window_end,
                    available_at=now,
                )
        sent = 0
        for _ in range(self._batch_size):
            async with self._unit_of_work(self._sessions, workspace_id=self._workspace_id) as uow:
                claim = await self._repository(uow.session).claim_report(
                    workspace_id=self._workspace_id,
                    worker_id=self._worker_id,
                    now=now,
                    lease_duration=self._lease_duration,
                    max_attempts=self._max_attempts,
                )
            if claim is None:
                break
            # Chỉ các bot mà chat này đang bật nhận tin (đã kiểm binding/membership ACTIVE).
            bot_ids = sorted({t.bot_id for t in targets if t.chat_id == claim.chat_id}, key=str)
            if not bot_ids:
                await self._mark_report_filtered(claim)
                continue
            view = await self._build_report_view(claim, bot_ids)
            # TG-4: topic PAPER report của binding ứng với chat này (cấu hình hiện hành).
            thread_id = next(
                (
                    t.report_thread_id
                    for t in targets
                    if t.chat_id == claim.chat_id and t.report_thread_id is not None
                ),
                None,
            )
            if await self._send(
                claim.chat_id,
                claim.report_id,
                claim.attempts,
                self._paper_report_delivery(view, thread_id),
                bot_id=None,
            ):
                sent += 1
        return sent

    async def _build_report_view(
        self, claim: PaperReportClaim, bot_ids: list[UUID]
    ) -> PaperReportView:
        rows = []
        for bot_id in bot_ids:
            async with self._unit_of_work(
                self._sessions,
                workspace_id=self._workspace_id,
                bot_id=bot_id,
                snapshot_read_only=True,
            ) as uow:
                rows.append(
                    await self._repository(uow.session).bot_summary(
                        workspace_id=self._workspace_id,
                        bot_id=bot_id,
                        window_start=claim.window_start,
                        window_end=claim.window_end,
                    )
                )
        return build_paper_report_view(
            window_start=claim.window_start, window_end=claim.window_end, rows=rows
        )

    def _trade_result_delivery(
        self, view: TradeResultView, thread_id: int | None
    ) -> Callable[[int], Awaitable[None]]:
        async def deliver(chat_id: int) -> None:
            await self._trade_result_send(bind_topic(self._sender, thread_id), chat_id, view)

        return deliver

    def _paper_report_delivery(
        self, view: PaperReportView, thread_id: int | None
    ) -> Callable[[int], Awaitable[None]]:
        async def deliver(chat_id: int) -> None:
            await self._paper_report_send(bind_topic(self._sender, thread_id), chat_id, view)

        return deliver

    async def _mark_report_filtered(self, claim: PaperReportClaim) -> None:
        async with self._unit_of_work(self._sessions, workspace_id=self._workspace_id) as uow:
            await self._repository(uow.session).mark_filtered(
                workspace_id=self._workspace_id,
                delivery_id=claim.report_id,
                worker_id=self._worker_id,
                error_code="REPORT_NO_ACTIVE_SUBSCRIPTION",
                report=True,
            )

    # --- gửi và chuyển trạng thái dùng chung -------------------------------------------------

    async def _send(
        self,
        chat_id: int,
        delivery_id: UUID,
        attempts: int,
        deliver: Callable[[int], Awaitable[None]],
        *,
        bot_id: UUID | None,
    ) -> bool:
        await self._pacer.wait(chat_id)
        if not await self._renew(delivery_id, bot_id=bot_id):
            return False
        try:
            await deliver(chat_id)
        except TelegramApiError as error:
            await self._mark_failed(
                delivery_id, attempts, error.code, error.retry_after, bot_id=bot_id
            )
            return False
        except Exception as error:
            await self._mark_failed(delivery_id, attempts, _error_code(error), None, bot_id=bot_id)
            return False
        self._pacer.mark_sent(chat_id)
        await self._mark_sent(delivery_id, bot_id=bot_id)
        return True

    def _scope(self, bot_id: UUID | None) -> UnitOfWork:
        return self._unit_of_work(self._sessions, workspace_id=self._workspace_id, bot_id=bot_id)

    async def _renew(self, delivery_id: UUID, *, bot_id: UUID | None) -> bool:
        async with self._scope(bot_id) as uow:
            return await self._repository(uow.session).renew(
                workspace_id=self._workspace_id,
                delivery_id=delivery_id,
                worker_id=self._worker_id,
                lease_expires_at=self._now() + self._lease_duration,
                report=bot_id is None,
            )

    async def _mark_sent(self, delivery_id: UUID, *, bot_id: UUID | None) -> None:
        async with self._scope(bot_id) as uow:
            await self._repository(uow.session).mark_sent(
                workspace_id=self._workspace_id,
                delivery_id=delivery_id,
                worker_id=self._worker_id,
                sent_at=self._now(),
                report=bot_id is None,
            )

    async def _mark_failed(
        self,
        delivery_id: UUID,
        attempts: int,
        error_code: str,
        retry_after: int | None,
        *,
        bot_id: UUID | None,
        dead: bool = False,
    ) -> None:
        if retry_after is None:
            delay = retry_delay_seconds(max(attempts, 1), base_seconds=2, max_seconds=300)
        else:
            delay = min(max(retry_after, 1), 86_400)
        async with self._scope(bot_id) as uow:
            await self._repository(uow.session).mark_failed(
                workspace_id=self._workspace_id,
                delivery_id=delivery_id,
                worker_id=self._worker_id,
                available_at=self._now() + timedelta(seconds=delay),
                error_code=error_code,
                dead=dead or attempts >= self._max_attempts,
                report=bot_id is None,
            )


def _error_code(error: BaseException) -> str:
    name = type(error).__name__.upper()
    return f"TELEGRAM_RESULT_{name[:80]}"


__all__ = [
    "ChatPacer",
    "TelegramTradeResultWorker",
    "TradeNotificationClaim",
    "send_paper_report",
    "send_trade_result",
]
