"""Hàng đợi outbound Telegram: giới hạn tốc độ 1/s, 20/m group, 30/s broadcast,
xử lý 429 retry_after, ma trận retry hữu hạn, dead-letter và bảo vệ dữ liệu group.

Tham chiếu giới hạn Bot API:
- https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this (kiểm 18/11/2026)
- https://core.telegram.org/bots/api#responseparameters (kiểm 18/11/2026)
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import heapq
import json
import logging
import math
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from trading_bot.adapters.telegram.errors import (
    TelegramApiError,
    TelegramSensitivityBlockedError,
)
from trading_bot.adapters.telegram.outbound_guard import (
    ContentSensitivity,
    GroupSensitivityGuard,
    is_group_chat,
)
from trading_bot.ports.telegram import TelegramMessageSender

logger = logging.getLogger(__name__)

OutboundOperation = Literal[
    "send_message",
    "send_photo",
    "send_link_confirmation",
    "answer_callback",
    "create_forum_topic",
]


@dataclass(frozen=True, slots=True)
class TelegramRateLimitPolicy:
    """Ngưỡng giới hạn tốc độ gửi tin Telegram outbound theo tài liệu Bot FAQ."""

    private_interval_seconds: float = 1.0
    group_max_per_minute: int = 20
    group_window_seconds: float = 60.0
    global_max_per_second: int = 30
    global_window_seconds: float = 1.0
    max_retry_after_seconds: float = 86_400.0

    def __post_init__(self) -> None:
        for name, value in (
            ("private_interval_seconds", self.private_interval_seconds),
            ("group_window_seconds", self.group_window_seconds),
            ("global_window_seconds", self.global_window_seconds),
            ("max_retry_after_seconds", self.max_retry_after_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} phải là số hữu hạn dương.")
        if self.group_max_per_minute < 1:
            raise ValueError("group_max_per_minute phải >= 1.")
        if self.global_max_per_second < 1:
            raise ValueError("global_max_per_second phải >= 1.")


class TelegramFailureClass(StrEnum):
    """Phân loại lỗi outbound để quyết định retry hay chuyển thẳng dead-letter."""

    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_SERVER = "TRANSIENT_SERVER"
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    SENSITIVE_CONTENT_BLOCKED = "SENSITIVE_CONTENT_BLOCKED"
    PERMANENT_CLIENT = "PERMANENT_CLIENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RetryRule:
    """Quy tắc retry cho một nhóm lỗi cụ thể."""

    retryable: bool
    max_attempts: int
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts phải >= 1.")
        if self.base_backoff_seconds <= 0 or self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("Ngưỡng backoff retry không hợp lệ.")


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Kết quả đánh giá ma trận retry cho một lần gửi thất bại."""

    should_retry: bool
    delay_seconds: float
    failure_class: TelegramFailureClass
    error_code: str
    retry_after: int | None = None


_DEFAULT_RULES: dict[TelegramFailureClass, RetryRule] = {
    TelegramFailureClass.RATE_LIMITED: RetryRule(
        retryable=True, max_attempts=5, base_backoff_seconds=1.0, max_backoff_seconds=120.0
    ),
    TelegramFailureClass.TRANSIENT_SERVER: RetryRule(
        retryable=True, max_attempts=4, base_backoff_seconds=1.0, max_backoff_seconds=30.0
    ),
    TelegramFailureClass.NETWORK_ERROR: RetryRule(
        retryable=True, max_attempts=4, base_backoff_seconds=1.0, max_backoff_seconds=30.0
    ),
    TelegramFailureClass.AUTH_ERROR: RetryRule(retryable=False, max_attempts=1),
    TelegramFailureClass.SENSITIVE_CONTENT_BLOCKED: RetryRule(retryable=False, max_attempts=1),
    TelegramFailureClass.PERMANENT_CLIENT: RetryRule(retryable=False, max_attempts=1),
    TelegramFailureClass.UNKNOWN: RetryRule(
        retryable=True, max_attempts=2, base_backoff_seconds=2.0, max_backoff_seconds=30.0
    ),
}


class TelegramRetryMatrix:
    """Ma trận retry có giới hạn theo từng loại lỗi Telegram Bot API."""

    def __init__(
        self,
        *,
        rules: Mapping[TelegramFailureClass, RetryRule] | None = None,
        max_retry_after_seconds: float = 86_400.0,
    ) -> None:
        merged = dict(_DEFAULT_RULES)
        if rules is not None:
            merged.update(rules)
        self._rules = merged
        self._max_retry_after_seconds = max_retry_after_seconds

    def rule_for(self, failure_class: TelegramFailureClass) -> RetryRule:
        return self._rules[failure_class]

    def classify(self, error: BaseException) -> tuple[TelegramFailureClass, str, int | None]:
        """Phân loại exception thành mã lỗi an toàn; không lưu nội dung nhạy cảm."""
        if isinstance(error, TelegramSensitivityBlockedError):
            return TelegramFailureClass.SENSITIVE_CONTENT_BLOCKED, error.code, None
        if isinstance(error, ValueError):
            return TelegramFailureClass.PERMANENT_CLIENT, "TELEGRAM_VALIDATION_ERROR", None
        if isinstance(error, TelegramApiError):
            return self._classify_api_error(error)
        if isinstance(error, TimeoutError | OSError):
            return (
                TelegramFailureClass.NETWORK_ERROR,
                f"TELEGRAM_{type(error).__name__.upper()[:60]}",
                None,
            )
        return (
            TelegramFailureClass.UNKNOWN,
            f"TELEGRAM_{type(error).__name__.upper()[:60]}",
            None,
        )

    def _classify_api_error(
        self, error: TelegramApiError
    ) -> tuple[TelegramFailureClass, str, int | None]:
        status = error.status_code
        code = error.code
        if status == 429 or code == "TELEGRAM_HTTP_429":
            return TelegramFailureClass.RATE_LIMITED, code, error.retry_after
        if status == 401 or code == "TELEGRAM_HTTP_401":
            return TelegramFailureClass.AUTH_ERROR, code, None
        if status is not None and 400 <= status < 500:
            return TelegramFailureClass.PERMANENT_CLIENT, code, None
        if status is not None and 500 <= status < 600:
            return TelegramFailureClass.TRANSIENT_SERVER, code, None
        if code in {"TELEGRAM_NETWORK_ERROR", "TELEGRAM_INVALID_RESPONSE"}:
            return TelegramFailureClass.NETWORK_ERROR, code, None
        return TelegramFailureClass.UNKNOWN, code, error.retry_after

    def decide(self, error: BaseException, attempt: int) -> RetryDecision:
        """Quyết định có retry nữa hay chuyển vào dead-letter dựa trên ``attempt`` hiện tại."""
        failure_class, error_code, retry_after = self.classify(error)
        rule = self._rules[failure_class]
        if not rule.retryable or attempt >= rule.max_attempts:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                failure_class=failure_class,
                error_code=error_code,
                retry_after=retry_after,
            )
        if failure_class is TelegramFailureClass.RATE_LIMITED and retry_after is not None:
            if retry_after > self._max_retry_after_seconds:
                return RetryDecision(
                    should_retry=False,
                    delay_seconds=0.0,
                    failure_class=failure_class,
                    error_code=error_code,
                    retry_after=retry_after,
                )
            delay = float(max(1, retry_after))
        else:
            shift = min(max(attempt - 1, 0), 30)
            delay = min(rule.base_backoff_seconds * (2**shift), rule.max_backoff_seconds)
        return RetryDecision(
            should_retry=True,
            delay_seconds=delay,
            failure_class=failure_class,
            error_code=error_code,
            retry_after=retry_after,
        )


DEFAULT_RETRY_MATRIX = TelegramRetryMatrix()


@dataclass(frozen=True, slots=True)
class TelegramDeadLetter:
    """Bản ghi dead-letter không chứa token hay nội dung văn bản nhạy cảm thô."""

    message_id: UUID
    operation: OutboundOperation
    chat_id: int
    message_thread_id: int | None
    is_group: bool
    attempts: int
    error_code: str
    failure_class: str
    first_enqueued_at: datetime
    failed_at: datetime
    last_retry_after: int | None
    payload_digest: str
    correlation_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": str(self.message_id),
            "operation": self.operation,
            "chat_id": self.chat_id,
            "message_thread_id": self.message_thread_id,
            "is_group": self.is_group,
            "attempts": self.attempts,
            "error_code": self.error_code,
            "failure_class": self.failure_class,
            "first_enqueued_at": self.first_enqueued_at.isoformat(),
            "failed_at": self.failed_at.isoformat(),
            "last_retry_after": self.last_retry_after,
            "payload_digest": self.payload_digest,
            "correlation_id": self.correlation_id,
        }


class DeadLetterSink(Protocol):
    """Cổng lưu vết các tin nhắn thất bại quá ngưỡng retry."""

    def record(self, entry: TelegramDeadLetter) -> None: ...


class InMemoryDeadLetterStore:
    """Kho dead-letter trong RAM có giới hạn dung lượng để chẩn đoán và kiểm thử."""

    def __init__(self, capacity: int = 1000) -> None:
        if capacity < 1:
            raise ValueError("capacity phải >= 1.")
        self._entries: deque[TelegramDeadLetter] = deque(maxlen=capacity)

    def record(self, entry: TelegramDeadLetter) -> None:
        self._entries.append(entry)

    @property
    def entries(self) -> tuple[TelegramDeadLetter, ...]:
        return tuple(self._entries)

    def clear(self) -> None:
        self._entries.clear()


class JsonlDeadLetterSink:
    """Ghi bền vững dead-letter ra JSONL (chỉ metadata + digest, không ghi plaintext nhạy cảm)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, entry: TelegramDeadLetter) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.as_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


class TelegramOutboundLimiter:
    """Bộ điều tiết tốc độ gửi tin: ≈1 tin/s/chat riêng, 20 tin/phút/group, ≈30 tin/s toàn cục."""

    def __init__(
        self,
        *,
        policy: TelegramRateLimitPolicy | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._policy = policy or TelegramRateLimitPolicy()
        self._clock = clock or time.monotonic
        self._sleep = sleep
        self._private_next_slot: dict[int, float] = {}
        self._group_slots: dict[int, list[float]] = defaultdict(list)
        self._global_slots: list[float] = []
        self._chat_cooldown_until: dict[int, float] = {}
        self._global_cooldown_until: float = float("-inf")

    @property
    def policy(self) -> TelegramRateLimitPolicy:
        return self._policy

    def record_retry_after(
        self, chat_id: int, delay_seconds: float, *, is_global: bool = False
    ) -> None:
        """Ghi nhận cửa sổ chờ bắt buộc từ HTTP 429 để mọi tác vụ sau đều tôn trọng."""
        bounded = min(max(float(delay_seconds), 0.0), self._policy.max_retry_after_seconds)
        until = self._clock() + bounded
        if is_global:
            self._global_cooldown_until = max(self._global_cooldown_until, until)
        else:
            prev = self._chat_cooldown_until.get(chat_id, float("-inf"))
            self._chat_cooldown_until[chat_id] = max(prev, until)

    def earliest_ready_at(self, chat_id: int, *, is_group: bool | None = None) -> float:
        """Thời điểm sớm nhất (theo clock) tin cho ``chat_id`` đủ điều kiện gửi."""
        now = self._clock()
        group_flag = is_group_chat(chat_id, is_group=is_group)
        ready = max(
            now,
            self._global_cooldown_until,
            self._chat_cooldown_until.get(chat_id, now),
        )
        if group_flag:
            q = self._group_slots.get(chat_id)
            if q and len(q) >= self._policy.group_max_per_minute:
                ready = max(ready, q[0] + self._policy.group_window_seconds)
        else:
            ready = max(ready, self._private_next_slot.get(chat_id, now))
        if len(self._global_slots) >= self._policy.global_max_per_second:
            ready = max(ready, self._global_slots[0] + self._policy.global_window_seconds)
        return ready

    async def acquire(self, chat_id: int, *, is_group: bool | None = None) -> float:
        """Chờ đủ cả hạn mức chat/group/retry_after và hạn mức broadcast toàn cục rồi giữ slot."""
        group_flag = is_group_chat(chat_id, is_group=is_group)
        now1, slot1 = self._reserve_chat_stage(chat_id, group_flag)
        total_delay = 0.0
        if slot1 > now1:
            delay1 = slot1 - now1
            await self._sleep(delay1)
            total_delay += delay1
        delay2 = self._reserve_global_stage(chat_id, group_flag)
        if delay2 > 0:
            await self._sleep(delay2)
            total_delay += delay2
        return total_delay

    def _reserve_chat_stage(self, chat_id: int, is_group: bool) -> tuple[float, float]:
        now = self._clock()
        slot = max(
            now,
            self._global_cooldown_until,
            self._chat_cooldown_until.get(chat_id, now),
        )
        if is_group:
            slots = self._group_slots[chat_id]
            if len(slots) >= self._policy.group_max_per_minute:
                slot = max(slot, slots[0] + self._policy.group_window_seconds)
                slots.pop(0)
            bisect.insort(slots, slot)
        else:
            slot = max(slot, self._private_next_slot.get(chat_id, now))
            self._private_next_slot[chat_id] = slot + self._policy.private_interval_seconds
        return now, slot

    def _reserve_global_stage(self, chat_id: int, is_group: bool) -> float:
        now = self._clock()
        slot = max(now, self._global_cooldown_until)
        if len(self._global_slots) >= self._policy.global_max_per_second:
            slot = max(slot, self._global_slots[0] + self._policy.global_window_seconds)
            self._global_slots.pop(0)
        bisect.insort(self._global_slots, slot)
        if not is_group:
            self._private_next_slot[chat_id] = max(
                self._private_next_slot.get(chat_id, slot),
                slot + self._policy.private_interval_seconds,
            )
        return max(0.0, slot - now)


@dataclass(frozen=True, slots=True)
class TelegramOutboundMessage:
    """Thông điệp xếp hàng gửi ra Telegram Bot API."""

    chat_id: int
    text: str = ""
    operation: OutboundOperation = "send_message"
    message_thread_id: int | None = None
    png_bytes: bytes | None = None
    callback_data: str | None = None
    callback_id: str | None = None
    topic_name: str | None = None
    is_group: bool | None = None
    sensitivity: ContentSensitivity = "auto"
    correlation_id: str | None = None
    priority: int = 0
    message_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class TelegramDeliveryOutcome:
    """Kết quả xử lý một phần tử trong hàng đợi outbound."""

    message_id: UUID
    status: Literal["SENT", "DEAD_LETTERED"]
    attempts: int
    error_code: str | None = None
    failure_class: str | None = None
    topic_thread_id: int | None = None


@dataclass(slots=True)
class TelegramOutboundStats:
    """Số liệu thống kê vận hành hàng đợi outbound."""

    enqueued: int = 0
    sent: int = 0
    retried: int = 0
    dead_lettered: int = 0
    blocked_sensitive: int = 0


@dataclass(order=True, slots=True)
class _ScheduledEntry:
    ready_at: float
    priority: int
    seq: int
    attempt: int = field(compare=False)
    enqueued_at: datetime = field(compare=False)
    message: TelegramOutboundMessage = field(compare=False)
    future: asyncio.Future[TelegramDeliveryOutcome] | None = field(default=None, compare=False)


class TelegramOutboundQueue:
    """Hàng đợi ưu tiên có giới hạn dung lượng, điều tiết tốc độ, retry 429 và dead-letter."""

    def __init__(
        self,
        sender: TelegramMessageSender,
        *,
        limiter: TelegramOutboundLimiter | None = None,
        retry_matrix: TelegramRetryMatrix | None = None,
        sensitivity_guard: GroupSensitivityGuard | None = None,
        dead_letter_sinks: Sequence[DeadLetterSink] = (),
        max_queue_size: int = 10_000,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size phải >= 1.")
        self._sender = sender
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._limiter = limiter or TelegramOutboundLimiter(clock=self._clock, sleep=self._sleep)
        self._retry_matrix = retry_matrix or DEFAULT_RETRY_MATRIX
        self._sensitivity_guard = sensitivity_guard or GroupSensitivityGuard()
        self._in_memory_dead_letters = InMemoryDeadLetterStore()
        self._dead_letter_sinks: tuple[DeadLetterSink, ...] = (
            self._in_memory_dead_letters,
            *dead_letter_sinks,
        )
        self._max_queue_size = max_queue_size
        self._heap: list[_ScheduledEntry] = []
        self._seq = 0
        self._stats = TelegramOutboundStats()
        self._wake_event = asyncio.Event()

    @property
    def pending_count(self) -> int:
        return len(self._heap)

    @property
    def stats(self) -> TelegramOutboundStats:
        return self._stats

    @property
    def dead_letters(self) -> tuple[TelegramDeadLetter, ...]:
        return self._in_memory_dead_letters.entries

    def enqueue(self, message: TelegramOutboundMessage) -> UUID:
        """Đưa tin vào hàng đợi không chặn; ném ``RuntimeError`` nếu hàng đợi đầy."""
        self._push_entry(
            message=message,
            ready_at=self._clock(),
            attempt=1,
            enqueued_at=self._wall_clock(),
            future=None,
        )
        self._stats.enqueued += 1
        return message.message_id

    async def submit(self, message: TelegramOutboundMessage) -> TelegramDeliveryOutcome:
        """Đưa tin vào hàng đợi và xử lý tới khi gửi xong hoặc vào dead-letter."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TelegramDeliveryOutcome] = loop.create_future()
        self._push_entry(
            message=message,
            ready_at=self._clock(),
            attempt=1,
            enqueued_at=self._wall_clock(),
            future=future,
        )
        self._stats.enqueued += 1
        while not future.done():
            await self.process_next()
        return await future

    async def process_next(self) -> TelegramDeliveryOutcome | None:
        """Lấy một phần tử sẵn sàng sớm nhất để gửi, retry hoặc chuyển vào dead-letter."""
        if not self._heap:
            return None
        while self._heap:
            now = self._clock()
            head = self._heap[0]
            gate_ready = max(
                head.ready_at,
                self._limiter.earliest_ready_at(
                    head.message.chat_id, is_group=head.message.is_group
                ),
            )
            if gate_ready > now:
                ready_idx, soonest_ready = self._find_soonest_entry(now)
                if soonest_ready > now:
                    await self._sleep(soonest_ready - now)
                    continue
                entry = self._pop_at(ready_idx)
            else:
                entry = heapq.heappop(self._heap)
            return await self._deliver_entry(entry)
        return None

    async def drain(self) -> list[TelegramDeliveryOutcome]:
        """Xử lý toàn bộ phần tử hiện có (bao gồm retry) cho tới khi hàng đợi rỗng."""
        outcomes: list[TelegramDeliveryOutcome] = []
        while self._heap:
            outcome = await self.process_next()
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    async def run(self, stopping: asyncio.Event, *, idle_poll_seconds: float = 0.25) -> None:
        """Vòng lặp worker nền đọc và gửi hàng đợi tới khi nhận tín hiệu dừng."""
        while not stopping.is_set() or self._heap:
            if not self._heap:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=idle_poll_seconds)
                except TimeoutError:
                    continue
                continue
            await self.process_next()

    def _find_soonest_entry(self, now: float) -> tuple[int, float]:
        best_idx = 0
        best_ready = float("inf")
        best_order: tuple[int, int] | None = None
        for idx, entry in enumerate(self._heap):
            ready = max(
                entry.ready_at,
                self._limiter.earliest_ready_at(
                    entry.message.chat_id, is_group=entry.message.is_group
                ),
            )
            order = (entry.priority, entry.seq)
            if ready < best_ready or (
                ready == best_ready and (best_order is None or order < best_order)
            ):
                best_idx = idx
                best_ready = ready
                best_order = order
            if ready <= now and best_ready <= now:
                continue
        return best_idx, best_ready

    def _pop_at(self, index: int) -> _ScheduledEntry:
        entry = self._heap[index]
        last = self._heap.pop()
        if index < len(self._heap):
            self._heap[index] = last
            heapq.heapify(self._heap)
        return entry

    def _push_entry(
        self,
        *,
        message: TelegramOutboundMessage,
        ready_at: float,
        attempt: int,
        enqueued_at: datetime,
        future: asyncio.Future[TelegramDeliveryOutcome] | None,
    ) -> None:
        if len(self._heap) >= self._max_queue_size:
            raise RuntimeError("Hàng đợi Telegram outbound đã đầy.")
        self._seq += 1
        heapq.heappush(
            self._heap,
            _ScheduledEntry(
                ready_at=ready_at,
                priority=message.priority,
                seq=self._seq,
                attempt=attempt,
                enqueued_at=enqueued_at,
                message=message,
                future=future,
            ),
        )
        self._wake_event.set()

    async def _deliver_entry(self, entry: _ScheduledEntry) -> TelegramDeliveryOutcome | None:
        msg = entry.message
        group_flag = is_group_chat(msg.chat_id, is_group=msg.is_group)
        try:
            safe_text, safe_caption = self._guard_message(msg, group_flag)
            await self._limiter.acquire(msg.chat_id, is_group=group_flag)
            thread_id = await self._dispatch_sender(msg, safe_text, safe_caption)
        except Exception as error:
            decision = self._retry_matrix.decide(error, entry.attempt)
            if isinstance(error, TelegramSensitivityBlockedError):
                self._stats.blocked_sensitive += 1
            if decision.failure_class is TelegramFailureClass.RATE_LIMITED:
                cooldown = (
                    float(decision.retry_after)
                    if decision.retry_after is not None
                    else decision.delay_seconds
                )
                if cooldown > 0:
                    self._limiter.record_retry_after(msg.chat_id, cooldown)
            if decision.should_retry:
                self._stats.retried += 1
                self._push_entry(
                    message=msg,
                    ready_at=self._clock() + decision.delay_seconds,
                    attempt=entry.attempt + 1,
                    enqueued_at=entry.enqueued_at,
                    future=entry.future,
                )
                return None
            dead_entry = _build_dead_letter(
                msg=msg,
                is_group=group_flag,
                attempts=entry.attempt,
                error_code=decision.error_code,
                failure_class=decision.failure_class.value,
                enqueued_at=entry.enqueued_at,
                failed_at=self._wall_clock(),
                retry_after=decision.retry_after,
            )
            _emit_dead_letter(self._dead_letter_sinks, dead_entry)
            self._stats.dead_lettered += 1
            outcome = TelegramDeliveryOutcome(
                message_id=msg.message_id,
                status="DEAD_LETTERED",
                attempts=entry.attempt,
                error_code=decision.error_code,
                failure_class=decision.failure_class.value,
            )
            if entry.future is not None and not entry.future.done():
                entry.future.set_result(outcome)
            return outcome

        self._stats.sent += 1
        outcome = TelegramDeliveryOutcome(
            message_id=msg.message_id,
            status="SENT",
            attempts=entry.attempt,
            topic_thread_id=thread_id,
        )
        if entry.future is not None and not entry.future.done():
            entry.future.set_result(outcome)
        return outcome

    def _guard_message(self, msg: TelegramOutboundMessage, group_flag: bool) -> tuple[str, str]:
        if msg.operation == "send_link_confirmation" and (group_flag or msg.chat_id <= 0):
            raise ValueError("Xác nhận liên kết Telegram chỉ hỗ trợ chat riêng.")
        if msg.operation == "send_photo":
            safe_caption = self._sensitivity_guard.inspect_photo_caption(
                msg.chat_id,
                msg.text,
                is_group=group_flag,
                sensitivity=msg.sensitivity,
            )
            return "", safe_caption
        if msg.operation in {"send_message", "send_link_confirmation"}:
            safe_text = self._sensitivity_guard.inspect_text(
                msg.chat_id,
                msg.text,
                is_group=group_flag,
                sensitivity=msg.sensitivity,
            )
            return safe_text, ""
        return msg.text, ""

    async def _dispatch_sender(
        self, msg: TelegramOutboundMessage, safe_text: str, safe_caption: str
    ) -> int | None:
        if msg.operation == "send_message":
            await self._sender.send_message(
                msg.chat_id, safe_text, message_thread_id=msg.message_thread_id
            )
            return None
        if msg.operation == "send_photo":
            if msg.png_bytes is None:
                raise ValueError("send_photo yêu cầu png_bytes.")
            await self._sender.send_photo(
                msg.chat_id,
                msg.png_bytes,
                safe_caption,
                message_thread_id=msg.message_thread_id,
            )
            return None
        if msg.operation == "send_link_confirmation":
            if not msg.callback_data:
                raise ValueError("send_link_confirmation yêu cầu callback_data.")
            await self._sender.send_link_confirmation(msg.chat_id, safe_text, msg.callback_data)
            return None
        if msg.operation == "answer_callback":
            if not msg.callback_id:
                raise ValueError("answer_callback yêu cầu callback_id.")
            await self._sender.answer_callback(msg.callback_id, msg.text)
            return None
        if msg.operation == "create_forum_topic":
            creator = getattr(self._sender, "create_forum_topic", None)
            if not callable(creator):
                raise ValueError("Sender không hỗ trợ create_forum_topic.")
            if not msg.topic_name:
                raise ValueError("create_forum_topic yêu cầu topic_name.")
            result = await creator(msg.chat_id, msg.topic_name)
            return int(result)
        raise ValueError(f"Thao tác Telegram không hợp lệ: {msg.operation}")


class ThrottledTelegramSender:
    """Lớp bọc tương thích ``TelegramMessageSender`` áp throttle, guard group và retry/DLQ."""

    def __init__(
        self,
        inner: TelegramMessageSender,
        *,
        limiter: TelegramOutboundLimiter | None = None,
        retry_matrix: TelegramRetryMatrix | None = None,
        sensitivity_guard: GroupSensitivityGuard | None = None,
        dead_letter_sinks: Sequence[DeadLetterSink] = (),
        retry_on_error: bool = True,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._limiter = limiter or TelegramOutboundLimiter(sleep=sleep)
        self._retry_matrix = retry_matrix or DEFAULT_RETRY_MATRIX
        self._sensitivity_guard = sensitivity_guard or GroupSensitivityGuard()
        self._in_memory_dead_letters = InMemoryDeadLetterStore()
        self._dead_letter_sinks: tuple[DeadLetterSink, ...] = (
            self._in_memory_dead_letters,
            *dead_letter_sinks,
        )
        self._retry_on_error = retry_on_error
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep

    @property
    def limiter(self) -> TelegramOutboundLimiter:
        return self._limiter

    @property
    def dead_letters(self) -> tuple[TelegramDeadLetter, ...]:
        return self._in_memory_dead_letters.entries

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if callable(close):
            await close()

    async def __aenter__(self) -> ThrottledTelegramSender:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def refresh_token(self, *, force: bool = False) -> bool:
        refresh = getattr(self._inner, "refresh_token", None)
        if callable(refresh):
            return bool(refresh(force=force))
        return False

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        is_group: bool | None = None,
        sensitivity: ContentSensitivity = "auto",
    ) -> None:
        group_flag = is_group_chat(chat_id, is_group=is_group)
        msg = TelegramOutboundMessage(
            chat_id=chat_id,
            text=text,
            operation="send_message",
            message_thread_id=message_thread_id,
            is_group=group_flag,
            sensitivity=sensitivity,
        )
        try:
            safe_text = self._sensitivity_guard.inspect_text(
                chat_id, text, is_group=group_flag, sensitivity=sensitivity
            )
        except TelegramSensitivityBlockedError as error:
            self._record_dead_letter(msg, group_flag, attempts=1, error=error)
            raise
        await self._execute_with_policy(
            msg,
            group_flag=group_flag,
            call=lambda: self._inner.send_message(
                chat_id, safe_text, message_thread_id=message_thread_id
            ),
        )

    async def send_photo(
        self,
        chat_id: int,
        png_bytes: bytes,
        caption: str,
        *,
        message_thread_id: int | None = None,
        is_group: bool | None = None,
        sensitivity: ContentSensitivity = "auto",
    ) -> None:
        group_flag = is_group_chat(chat_id, is_group=is_group)
        msg = TelegramOutboundMessage(
            chat_id=chat_id,
            text=caption,
            operation="send_photo",
            message_thread_id=message_thread_id,
            png_bytes=png_bytes,
            is_group=group_flag,
            sensitivity=sensitivity,
        )
        try:
            safe_caption = self._sensitivity_guard.inspect_photo_caption(
                chat_id, caption, is_group=group_flag, sensitivity=sensitivity
            )
        except TelegramSensitivityBlockedError as error:
            self._record_dead_letter(msg, group_flag, attempts=1, error=error)
            raise
        await self._execute_with_policy(
            msg,
            group_flag=group_flag,
            call=lambda: self._inner.send_photo(
                chat_id, png_bytes, safe_caption, message_thread_id=message_thread_id
            ),
        )

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
        if chat_id <= 0:
            raise ValueError("Xác nhận liên kết Telegram chỉ hỗ trợ chat riêng.")
        msg = TelegramOutboundMessage(
            chat_id=chat_id,
            text=text,
            operation="send_link_confirmation",
            callback_data=callback_data,
            is_group=False,
            sensitivity="private_only",
        )
        await self._execute_with_policy(
            msg,
            group_flag=False,
            call=lambda: self._inner.send_link_confirmation(chat_id, text, callback_data),
        )

    async def answer_callback(self, callback_id: str, text: str) -> None:
        msg = TelegramOutboundMessage(
            chat_id=0,
            text=text,
            operation="answer_callback",
            callback_id=callback_id,
            is_group=False,
        )
        attempt = 0
        enqueued_at = self._clock()
        while True:
            attempt += 1
            try:
                await self._inner.answer_callback(callback_id, text)
                return
            except Exception as error:
                decision = self._retry_matrix.decide(error, attempt)
                if self._retry_on_error and decision.should_retry:
                    await self._sleep(decision.delay_seconds)
                    continue
                self._record_dead_letter(msg, False, attempts=attempt, error=error, at=enqueued_at)
                raise

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        creator = getattr(self._inner, "create_forum_topic", None)
        if not callable(creator):
            raise RuntimeError("Sender không hỗ trợ create_forum_topic.")
        group_flag = is_group_chat(chat_id)
        msg = TelegramOutboundMessage(
            chat_id=chat_id,
            text=name,
            operation="create_forum_topic",
            topic_name=name,
            is_group=group_flag,
        )
        result = await self._execute_with_policy(
            msg,
            group_flag=group_flag,
            call=lambda: creator(chat_id, name),
        )
        return int(result)

    async def _execute_with_policy(
        self,
        msg: TelegramOutboundMessage,
        *,
        group_flag: bool,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        attempt = 0
        enqueued_at = self._clock()
        while True:
            attempt += 1
            await self._limiter.acquire(msg.chat_id, is_group=group_flag)
            try:
                return await call()
            except Exception as error:
                decision = self._retry_matrix.decide(error, attempt)
                if decision.failure_class is TelegramFailureClass.RATE_LIMITED:
                    cooldown = (
                        float(decision.retry_after)
                        if decision.retry_after is not None
                        else decision.delay_seconds
                    )
                    if cooldown > 0:
                        self._limiter.record_retry_after(msg.chat_id, cooldown)
                if self._retry_on_error and decision.should_retry:
                    await self._sleep(decision.delay_seconds)
                    continue
                if self._retry_on_error or isinstance(error, TelegramSensitivityBlockedError):
                    self._record_dead_letter(
                        msg, group_flag, attempts=attempt, error=error, at=enqueued_at
                    )
                raise

    def _record_dead_letter(
        self,
        msg: TelegramOutboundMessage,
        group_flag: bool,
        *,
        attempts: int,
        error: BaseException,
        at: datetime | None = None,
    ) -> None:
        failure_class, error_code, retry_after = self._retry_matrix.classify(error)
        entry = _build_dead_letter(
            msg=msg,
            is_group=group_flag,
            attempts=attempts,
            error_code=error_code,
            failure_class=failure_class.value,
            enqueued_at=at or self._clock(),
            failed_at=self._clock(),
            retry_after=retry_after,
        )
        _emit_dead_letter(self._dead_letter_sinks, entry)


def _payload_digest(msg: TelegramOutboundMessage) -> str:
    raw = f"{msg.operation}:{msg.chat_id}:{msg.message_thread_id}:{len(msg.text)}:{msg.text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_dead_letter(
    *,
    msg: TelegramOutboundMessage,
    is_group: bool,
    attempts: int,
    error_code: str,
    failure_class: str,
    enqueued_at: datetime,
    failed_at: datetime,
    retry_after: int | None,
) -> TelegramDeadLetter:
    return TelegramDeadLetter(
        message_id=msg.message_id,
        operation=msg.operation,
        chat_id=msg.chat_id,
        message_thread_id=msg.message_thread_id,
        is_group=is_group,
        attempts=attempts,
        error_code=error_code,
        failure_class=failure_class,
        first_enqueued_at=enqueued_at,
        failed_at=failed_at,
        last_retry_after=retry_after,
        payload_digest=_payload_digest(msg),
        correlation_id=msg.correlation_id,
    )


def _emit_dead_letter(sinks: Sequence[DeadLetterSink], entry: TelegramDeadLetter) -> None:
    logger.warning(
        "Telegram outbound chuyển sang dead-letter; message_id=%s op=%s chat_id=%s "
        "error_code=%s failure_class=%s attempts=%d",
        entry.message_id,
        entry.operation,
        entry.chat_id,
        entry.error_code,
        entry.failure_class,
        entry.attempts,
    )
    for sink in sinks:
        sink.record(entry)


__all__ = [
    "DEFAULT_RETRY_MATRIX",
    "DeadLetterSink",
    "InMemoryDeadLetterStore",
    "JsonlDeadLetterSink",
    "OutboundOperation",
    "RetryDecision",
    "RetryRule",
    "TelegramDeadLetter",
    "TelegramDeliveryOutcome",
    "TelegramFailureClass",
    "TelegramOutboundLimiter",
    "TelegramOutboundMessage",
    "TelegramOutboundQueue",
    "TelegramOutboundStats",
    "TelegramRateLimitPolicy",
    "TelegramRetryMatrix",
    "ThrottledTelegramSender",
]
