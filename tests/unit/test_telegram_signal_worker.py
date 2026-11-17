"""Kiểm các chuyển trạng thái gửi Telegram signal qua worker độc lập."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from trading_bot.adapters.database.repositories.telegram_signals import (
    SignalDeliveryClaim,
    SignalSubscriptionTarget,
)
from trading_bot.adapters.telegram.bot_api_sender import TelegramApiError
from trading_bot.app import create_app
from trading_bot.application.telegram_signals import (
    TelegramSignalCandidate,
    TelegramSignalFilters,
)
from trading_bot.config.settings import AppSettings
from trading_bot.ports.telegram import TelegramMessageSender
from trading_bot.workers.telegram_signal_worker import TelegramSignalDeliveryWorker

NOW = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOT = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SUBSCRIPTION = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
DECISION = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
DELIVERY = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


def _candidate(**overrides: object) -> TelegramSignalCandidate:
    values: dict[str, object] = {
        "decision_id": DECISION,
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "timeframe": "5m",
        "strategy_id": "H03",
        "strategy_release_id": "h03-v1",
        "reference_price": Decimal("68000"),
        "signal_at": NOW - timedelta(seconds=5),
        "expires_at": NOW + timedelta(minutes=1),
        "reason_code": "H03_BREAKOUT",
        "detail_vi": "Đóng cửa vượt khỏi kênh giá.",
    }
    values.update(overrides)
    return TelegramSignalCandidate(**values)  # type: ignore[arg-type]


class FakeUow:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.session = object()

    async def __aenter__(self) -> FakeUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeRepository:
    def __init__(
        self,
        *,
        candidate: TelegramSignalCandidate,
        targets: tuple[SignalSubscriptionTarget, ...] | None = None,
        claim_filtered: bool = False,
    ) -> None:
        self.candidate = candidate
        self.targets = targets or (SignalSubscriptionTarget(SUBSCRIPTION, BOT, 987654321),)
        self.claim_filtered = claim_filtered
        self.enqueued: list[tuple[UUID, UUID]] = []
        self.claimed = False
        self.renew_lease = True
        self.renewed_until: datetime | None = None
        self.sent: list[UUID] = []
        self.filtered: list[UUID] = []
        self.failed: list[tuple[str, datetime, int]] = []
        self.expired: list[UUID] = []

    async def list_enabled_subscriptions(
        self, *, workspace_id: UUID
    ) -> tuple[SignalSubscriptionTarget, ...]:
        assert workspace_id == WORKSPACE
        return self.targets

    async def recent_candidates(self, **_kwargs: object) -> tuple[TelegramSignalCandidate, ...]:
        return (self.candidate,)

    async def enqueue_delivery(self, **kwargs: object) -> bool:
        self.enqueued.append(
            (cast(UUID, kwargs["subscription_id"]), cast(UUID, kwargs["decision_id"]))
        )
        return True

    async def claim_one(self, **_kwargs: object) -> SignalDeliveryClaim | None:
        if not self.enqueued:
            return None
        if self.claimed:
            return None
        self.claimed = True
        if self.claim_filtered:
            self.filtered.append(DELIVERY)
        return SignalDeliveryClaim(
            delivery_id=DELIVERY,
            subscription_id=SUBSCRIPTION,
            bot_id=BOT,
            decision_id=DECISION,
            attempts=1,
            chat_id=987654321,
            candidate=self.candidate,
            filtered=self.claim_filtered,
        )

    async def mark_sent(self, **kwargs: object) -> bool:
        self.sent.append(cast(UUID, kwargs["delivery_id"]))
        return True

    async def mark_filtered(self, **kwargs: object) -> bool:
        self.filtered.append(cast(UUID, kwargs["delivery_id"]))
        return True

    async def renew_claim(self, **kwargs: object) -> bool:
        self.renewed_until = cast(datetime, kwargs["lease_expires_at"])
        return self.renew_lease

    async def mark_failed(self, **kwargs: object) -> bool:
        self.failed.append(
            (
                str(kwargs["error_code"]),
                cast(datetime, kwargs["available_at"]),
                cast(int, kwargs["attempts"]),
            )
        )
        return True

    async def mark_expired(self, **kwargs: object) -> bool:
        self.expired.append(cast(UUID, kwargs["delivery_id"]))
        return True


class FakeSender:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.messages.append((chat_id, text))

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
        del chat_id, text, callback_data

    async def answer_callback(self, callback_id: str, text: str) -> None:
        del callback_id, text


def _worker(
    repository: FakeRepository,
    sender: FakeSender,
) -> TelegramSignalDeliveryWorker:
    return TelegramSignalDeliveryWorker(
        session_factory=object(),  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        sender=cast(TelegramMessageSender, sender),
        worker_id="test-signal-worker",
        now=lambda: NOW,
        sleep=lambda _seconds: _no_sleep(),
        unit_of_work=cast(Any, FakeUow),
        repository=cast(Any, lambda _session: repository),
    )


async def _no_sleep() -> None:
    return None


@pytest.mark.asyncio
async def test_worker_queues_stable_candidate_sends_signal_and_marks_delivery_sent() -> None:
    repository = FakeRepository(candidate=_candidate())
    sender = FakeSender()

    sent_count = await _worker(repository, sender).tick()

    assert sent_count == 1
    assert repository.enqueued == [(SUBSCRIPTION, DECISION)]
    assert repository.renewed_until == NOW + timedelta(seconds=45)
    assert repository.sent == [DELIVERY]
    assert sender.messages[0][0] == 987654321
    assert "KHÔNG PHẢI LỆNH" in sender.messages[0][1]


@pytest.mark.asyncio
async def test_worker_drops_stale_candidate_before_creating_delivery() -> None:
    repository = FakeRepository(
        candidate=_candidate(
            signal_at=NOW - timedelta(minutes=3), expires_at=NOW + timedelta(minutes=1)
        )
    )
    sender = FakeSender()

    sent_count = await _worker(repository, sender).tick()

    assert sent_count == 0
    assert repository.enqueued == []
    assert sender.messages == []


@pytest.mark.asyncio
async def test_worker_persists_retry_after_and_delivery_error_code() -> None:
    repository = FakeRepository(candidate=_candidate())
    sender = FakeSender(TelegramApiError(code="TELEGRAM_HTTP_429", retry_after=17))

    sent_count = await _worker(repository, sender).tick()

    assert sent_count == 0
    assert repository.sent == []
    assert repository.failed == [("TELEGRAM_HTTP_429", NOW + timedelta(seconds=17), 1)]


@pytest.mark.asyncio
async def test_worker_does_not_send_when_claim_lease_cannot_be_renewed() -> None:
    repository = FakeRepository(candidate=_candidate())
    repository.renew_lease = False
    sender = FakeSender()

    sent_count = await _worker(repository, sender).tick()

    assert sent_count == 0
    assert repository.renewed_until == NOW + timedelta(seconds=45)
    assert repository.sent == []
    assert sender.messages == []


@pytest.mark.asyncio
async def test_worker_does_not_enqueue_candidate_outside_subscription_filters() -> None:
    repository = FakeRepository(
        candidate=_candidate(),
        targets=(
            SignalSubscriptionTarget(
                SUBSCRIPTION,
                BOT,
                987654321,
                TelegramSignalFilters(symbols=("ETHUSDT",)),
            ),
        ),
    )
    sender = FakeSender()

    sent_count = await _worker(repository, sender).tick()

    assert sent_count == 0
    assert repository.enqueued == []
    assert sender.messages == []


@pytest.mark.asyncio
async def test_worker_skips_delivery_filtered_again_at_claim_time() -> None:
    repository = FakeRepository(candidate=_candidate(), claim_filtered=True)
    sender = FakeSender()

    sent_count = await _worker(repository, sender).tick()

    assert sent_count == 0
    assert repository.filtered == [DELIVERY]
    assert sender.messages == []


def test_signal_worker_process_exposes_only_liveness_and_readiness_routes() -> None:
    app = create_app(
        AppSettings(
            app_env="test",
            process_role="signal-worker",
            signal_workspace_id=WORKSPACE,
            telegram_bot_token_file=Path("/run/secrets/telegram_bot_token"),
        )
    )

    assert {getattr(route, "path", None) for route in app.routes} == {
        "/health/live",
        "/health/ready",
    }


@pytest.mark.asyncio
async def test_worker_passes_throttle_policy_and_skips_suppressed_claim() -> None:
    from trading_bot.application.telegram_signals import SignalThrottlePolicy

    repository = FakeRepository(candidate=_candidate(), claim_filtered=True)
    captured: dict[str, object] = {}
    original = repository.claim_one

    async def claim_one(**kwargs: object) -> SignalDeliveryClaim | None:
        captured.update(kwargs)
        return await original(**kwargs)

    repository.claim_one = claim_one  # type: ignore[method-assign]
    sender = FakeSender()

    assert await _worker(repository, sender).tick() == 0
    assert sender.messages == []
    assert captured["throttle"] == SignalThrottlePolicy()


@pytest.mark.asyncio
async def test_worker_uses_injected_signal_sender_for_image_cards() -> None:
    repository = FakeRepository(candidate=_candidate())
    sender = FakeSender()
    sent: list[tuple[int, object]] = []

    async def card_send(_sender: object, chat_id: int, candidate: object) -> None:
        sent.append((chat_id, candidate))

    worker = TelegramSignalDeliveryWorker(
        session_factory=object(),  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        sender=cast(TelegramMessageSender, sender),
        worker_id="test-signal-worker",
        now=lambda: NOW,
        sleep=lambda _seconds: _no_sleep(),
        unit_of_work=cast(Any, FakeUow),
        repository=cast(Any, lambda _session: repository),
        signal_send=card_send,
    )
    assert await worker.tick() == 1
    assert sent == [(987654321, repository.candidate)]
    assert sender.messages == []
    assert repository.sent == [DELIVERY]


class ThreadedClaimRepository(FakeRepository):
    """Claim mang ``thread_id`` topic tín hiệu của binding (TG-4)."""

    def __init__(self, *, thread_id: int | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.thread_id = thread_id

    async def claim_one(self, **kwargs: object) -> SignalDeliveryClaim | None:
        claim = await super().claim_one(**kwargs)
        if claim is None:
            return None
        from dataclasses import replace

        return replace(claim, thread_id=self.thread_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, 77])
async def test_worker_routes_signal_into_signal_topic_when_configured(
    thread_id: int | None,
) -> None:
    from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender

    repository = ThreadedClaimRepository(candidate=_candidate(), thread_id=thread_id)
    sender = RecordingTelegramSender()
    worker = TelegramSignalDeliveryWorker(
        session_factory=object(),  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        sender=sender,
        worker_id="test-signal-worker",
        now=lambda: NOW,
        sleep=lambda _seconds: _no_sleep(),
        unit_of_work=cast(Any, FakeUow),
        repository=cast(Any, lambda _session: repository),
    )
    assert await worker.tick() == 1
    # Chưa cấu hình topic → chat chính như trước; có topic → đúng topic tín hiệu.
    assert sender.threads == [("message", 987654321, thread_id)]


@pytest.mark.asyncio
async def test_signal_card_text_fallback_stays_in_signal_topic(monkeypatch: Any) -> None:
    from trading_bot.adapters.telegram import card_delivery
    from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender

    def broken(_view: object) -> bytes:
        raise ValueError("render lỗi")

    monkeypatch.setattr(card_delivery, "render_signal_card", broken)
    repository = ThreadedClaimRepository(candidate=_candidate(), thread_id=91)
    sender = RecordingTelegramSender()
    worker = TelegramSignalDeliveryWorker(
        session_factory=object(),  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        sender=sender,
        worker_id="test-signal-worker",
        now=lambda: NOW,
        sleep=lambda _seconds: _no_sleep(),
        unit_of_work=cast(Any, FakeUow),
        repository=cast(Any, lambda _session: repository),
        signal_send=card_delivery.send_signal_card,
    )
    assert await worker.tick() == 1
    assert sender.threads == [("message", 987654321, 91)]
