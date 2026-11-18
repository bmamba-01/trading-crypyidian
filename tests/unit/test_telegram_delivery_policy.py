"""Unit & worker-flow test cho chính sách giao nhận Telegram (WP-07B / CRY-346)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from trading_bot.application.telegram_signals import (
    SUPPRESSED_COOLDOWN,
    SUPPRESSED_DAILY_CAP,
    SUPPRESSED_HOURLY_CAP,
    SUPPRESSED_NOTIFICATION_TYPE,
    SUPPRESSED_QUIET_HOURS,
    RecentSignalDelivery,
    TelegramDeliveryPolicy,
    TelegramSignalCandidate,
    candidate_dedup_key,
    throttle_reason,
)
from trading_bot.workers.telegram_signal_worker import TelegramSignalDeliveryWorker


def _candidate(symbol: str = "BTCUSDT") -> TelegramSignalCandidate:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    return TelegramSignalCandidate(
        decision_id=uuid4(),
        symbol=symbol,
        direction="LONG",
        timeframe="15m",
        strategy_id="scalp_ema",
        strategy_release_id="rel-1",
        reference_price=Decimal("90000"),
        signal_at=now,
        expires_at=now + timedelta(minutes=30),
        reason_code="EMA_CROSS",
        detail_vi="Giá cắt lên EMA20.",
    )


def test_quiet_hours_respects_timezone_and_overnight_window() -> None:
    policy = TelegramDeliveryPolicy(
        quiet_hours_start="23:00",
        quiet_hours_end="07:00",
        timezone_name="Asia/Ho_Chi_Minh",
    )
    # 16:30 UTC == 23:30 Asia/Ho_Chi_Minh -> trong giờ yên lặng
    assert policy.is_quiet_at(datetime(2026, 11, 18, 16, 30, tzinfo=UTC)) is True
    # 23:59 UTC == 06:59 Asia/Ho_Chi_Minh -> trong giờ yên lặng
    assert policy.is_quiet_at(datetime(2026, 11, 18, 23, 59, tzinfo=UTC)) is True
    # 00:00 UTC == 07:00 Asia/Ho_Chi_Minh -> hết giờ yên lặng
    assert policy.is_quiet_at(datetime(2026, 11, 19, 0, 0, tzinfo=UTC)) is False
    # 08:00 UTC == 15:00 Asia/Ho_Chi_Minh -> ngoài giờ yên lặng
    assert policy.is_quiet_at(datetime(2026, 11, 18, 8, 0, tzinfo=UTC)) is False


def test_throttle_reason_checks_policy_rules() -> None:
    candidate = _candidate()
    key = candidate_dedup_key(candidate)
    now = datetime(2026, 11, 18, 16, 30, tzinfo=UTC)  # 23:30 Asia/Ho_Chi_Minh

    disabled_type = TelegramDeliveryPolicy(notification_types=("PAPER_TRADE_CLOSED",))
    assert (
        throttle_reason(candidate, [], now=now, policy=disabled_type)
        == SUPPRESSED_NOTIFICATION_TYPE
    )

    quiet_policy = TelegramDeliveryPolicy(
        quiet_hours_start="23:00",
        quiet_hours_end="07:00",
        timezone_name="Asia/Ho_Chi_Minh",
    )
    assert throttle_reason(candidate, [], now=now, policy=quiet_policy) == SUPPRESSED_QUIET_HOURS

    day_now = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)  # 15:00 Asia/Ho_Chi_Minh
    cap_policy = TelegramDeliveryPolicy(
        cooldown=timedelta(minutes=10),
        max_per_hour=2,
        max_per_day=3,
        timezone_name="Asia/Ho_Chi_Minh",
    )
    # Cooldown
    assert (
        throttle_reason(
            candidate,
            [RecentSignalDelivery(key, day_now - timedelta(minutes=5))],
            now=day_now,
            policy=cap_policy,
        )
        == SUPPRESSED_COOLDOWN
    )
    # Hourly cap
    assert (
        throttle_reason(
            candidate,
            [
                RecentSignalDelivery(
                    "ETHUSDT|LONG|scalp_ema|rel-1", day_now - timedelta(minutes=15)
                ),
                RecentSignalDelivery(
                    "SOLUSDT|LONG|scalp_ema|rel-1", day_now - timedelta(minutes=25)
                ),
            ],
            now=day_now,
            policy=cap_policy,
        )
        == SUPPRESSED_HOURLY_CAP
    )
    # Daily cap (các tin trước đó cách > 1h nhưng vẫn trong cùng ngày 2026-11-18 Asia/Ho_Chi_Minh)
    assert (
        throttle_reason(
            candidate,
            [
                RecentSignalDelivery("ETHUSDT|LONG|scalp_ema|rel-1", day_now - timedelta(hours=2)),
                RecentSignalDelivery("SOLUSDT|LONG|scalp_ema|rel-1", day_now - timedelta(hours=3)),
                RecentSignalDelivery("XRPUSDT|LONG|scalp_ema|rel-1", day_now - timedelta(hours=4)),
            ],
            now=day_now,
            policy=cap_policy,
        )
        == SUPPRESSED_DAILY_CAP
    )


def test_delivery_policy_roundtrip_and_validation() -> None:
    policy = TelegramDeliveryPolicy(
        cooldown=timedelta(minutes=45),
        max_per_hour=4,
        max_per_day=12,
        quiet_hours_start="22:30",
        quiet_hours_end="06:30",
        timezone_name="Asia/Ho_Chi_Minh",
        notification_types=("CANDIDATE", "INVALIDATION"),
    )
    restored = TelegramDeliveryPolicy.from_dict(policy.as_dict())
    assert restored == policy

    with pytest.raises(ValueError, match="Múi giờ"):
        TelegramDeliveryPolicy(timezone_name="Invalid/Timezone")
    with pytest.raises(ValueError, match="quiet hours"):
        TelegramDeliveryPolicy(quiet_hours_start="22:00", quiet_hours_end=None)


@pytest.mark.asyncio
async def test_worker_suppresses_quiet_hours_with_receipt_and_sends_nothing() -> None:
    """E2E worker boundary: trong giờ yên lặng, claim bị chặn có receipt và không gọi sender."""
    workspace_id = uuid4()
    bot_id = uuid4()
    sub_id = uuid4()
    quiet_now = datetime(2026, 11, 18, 16, 30, tzinfo=UTC)  # 23:30 Asia/Ho_Chi_Minh
    candidate = TelegramSignalCandidate(
        decision_id=uuid4(),
        symbol="BTCUSDT",
        direction="LONG",
        timeframe="15m",
        strategy_id="scalp_ema",
        strategy_release_id="rel-1",
        reference_price=Decimal("90000"),
        signal_at=quiet_now - timedelta(seconds=10),
        expires_at=quiet_now + timedelta(minutes=20),
        reason_code="EMA_CROSS",
        detail_vi="Giá cắt lên EMA20.",
    )
    target = SimpleNamespace(
        subscription_id=sub_id,
        bot_id=bot_id,
        chat_id=123456,
        filters=SimpleNamespace(matches=lambda _c: True),
    )
    suppressed_claim = SimpleNamespace(
        delivery_id=uuid4(),
        subscription_id=sub_id,
        bot_id=bot_id,
        decision_id=candidate.decision_id,
        attempts=0,
        chat_id=123456,
        candidate=candidate,
        filtered=True,
        suppressed_reason=SUPPRESSED_QUIET_HOURS,
        thread_id=None,
    )

    repo = MagicMock()
    repo.list_enabled_subscriptions = AsyncMock(return_value=(target,))
    repo.recent_candidates = AsyncMock(return_value=(candidate,))
    repo.enqueue_delivery = AsyncMock(return_value=True)
    repo.claim_one = AsyncMock(side_effect=[suppressed_claim, None])

    class _Uow:
        session = MagicMock()

        async def __aenter__(self) -> _Uow:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    sender = MagicMock()
    sender.send_message = AsyncMock()

    worker = TelegramSignalDeliveryWorker(
        session_factory=MagicMock(),
        workspace_id=workspace_id,
        sender=sender,
        worker_id="sig-quiet-test",
        now=lambda: quiet_now,
        unit_of_work=lambda *_a, **_kw: _Uow(),  # type: ignore[arg-type]
        repository=lambda _s: repo,
    )
    sent = await worker.tick()
    assert sent == 0
    sender.send_message.assert_not_awaited()
    assert suppressed_claim.suppressed_reason == SUPPRESSED_QUIET_HOURS
