"""Unit test cho ma trận retry/dead-letter Telegram Bot API (CRY-348)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.telegram_signals import SignalDeliveryClaim
from trading_bot.adapters.telegram.errors import (
    TelegramApiError,
    TelegramSensitivityBlockedError,
)
from trading_bot.adapters.telegram.outbound_queue import (
    RetryOutcomeAction,
    RetryRule,
    TelegramFailureClass,
    TelegramRetryMatrix,
)
from trading_bot.application.telegram_signals import TelegramSignalCandidate
from trading_bot.workers.telegram_signal_worker import TelegramSignalDeliveryWorker


def test_retry_matrix_classifies_all_bot_api_error_groups() -> None:
    matrix = TelegramRetryMatrix(max_retry_after_seconds=600.0)

    # 1. 429 retry_after trong ngưỡng -> retry đúng delay_seconds
    d429 = matrix.decide(
        TelegramApiError(code="TELEGRAM_HTTP_429", status_code=429, retry_after=35),
        attempt=1,
    )
    assert d429.should_retry is True
    assert d429.action is RetryOutcomeAction.RETRY
    assert d429.delay_seconds == 35.0
    assert d429.failure_class is TelegramFailureClass.RATE_LIMITED
    assert d429.ambiguous_duplicate_risk is False

    # 429 vượt quá max_retry_after_seconds -> dead_letter ngay
    d429_cap = matrix.decide(
        TelegramApiError(code="TELEGRAM_HTTP_429", status_code=429, retry_after=999),
        attempt=1,
    )
    assert d429_cap.should_retry is False
    assert d429_cap.action is RetryOutcomeAction.DEAD_LETTER

    # 2. 403 Forbidden & 400 Bad Request -> dead_letter ngay ở attempt 1
    for status, code in ((403, "TELEGRAM_HTTP_403"), (400, "TELEGRAM_HTTP_400")):
        d_perm = matrix.decide(TelegramApiError(code=code, status_code=status), attempt=1)
        assert d_perm.should_retry is False
        assert d_perm.action is RetryOutcomeAction.DEAD_LETTER
        assert d_perm.failure_class is TelegramFailureClass.PERMANENT_CLIENT
        assert d_perm.ambiguous_duplicate_risk is False

    # 3. 401 Unauthorized -> AUTH_ERROR dead_letter
    d401 = matrix.decide(TelegramApiError(code="TELEGRAM_HTTP_401", status_code=401), attempt=1)
    assert d401.should_retry is False
    assert d401.action is RetryOutcomeAction.DEAD_LETTER
    assert d401.failure_class is TelegramFailureClass.AUTH_ERROR

    # 4. 5xx Server Error -> retry hữu hạn rồi dead_letter
    d502_1 = matrix.decide(TelegramApiError(code="TELEGRAM_HTTP_502", status_code=502), attempt=1)
    d502_4 = matrix.decide(TelegramApiError(code="TELEGRAM_HTTP_502", status_code=502), attempt=4)
    assert d502_1.should_retry is True
    assert d502_1.action is RetryOutcomeAction.RETRY
    assert d502_1.delay_seconds == 1.0
    assert d502_1.ambiguous_duplicate_risk is False
    assert d502_4.should_retry is False
    assert d502_4.action is RetryOutcomeAction.DEAD_LETTER

    # 5. Timeout mơ hồ / network error -> retry hữu hạn và bật ambiguous_duplicate_risk
    d_timeout = matrix.decide(TimeoutError("read timed out"), attempt=1)
    assert d_timeout.should_retry is True
    assert d_timeout.action is RetryOutcomeAction.RETRY
    assert d_timeout.failure_class is TelegramFailureClass.NETWORK_ERROR
    assert d_timeout.ambiguous_duplicate_risk is True

    d_timeout_end = matrix.decide(TimeoutError("read timed out"), attempt=4)
    assert d_timeout_end.should_retry is False
    assert d_timeout_end.action is RetryOutcomeAction.DEAD_LETTER
    assert d_timeout_end.ambiguous_duplicate_risk is True

    # 6. Drop rule không cần dead-letter
    drop_matrix = TelegramRetryMatrix(
        rules={
            TelegramFailureClass.SENSITIVE_CONTENT_BLOCKED: RetryRule(
                retryable=False, max_attempts=1, drop_without_dead_letter=True
            )
        }
    )
    d_drop = drop_matrix.decide(
        TelegramSensitivityBlockedError(matched_rules=("balance",)), attempt=1
    )
    assert d_drop.should_retry is False
    assert d_drop.action is RetryOutcomeAction.DROP


class _FakeUow:
    def __init__(self, session: object, **_kwargs: object) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _RecordingRepo:
    def __init__(self, _session: object) -> None:
        pass

    failed_calls: list[dict[str, object]] = []

    async def list_enabled_subscriptions(self, **_kwargs: object) -> list[object]:
        return []

    async def claim_one(self, **_kwargs: object) -> None:
        return None

    async def mark_failed(self, **kwargs: object) -> bool:
        _RecordingRepo.failed_calls.append(kwargs)
        return True


@pytest.mark.asyncio
async def test_signal_worker_uses_retry_matrix_for_permanent_and_ambiguous_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _RecordingRepo.failed_calls.clear()
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    ws_id = uuid4()
    bot_id = uuid4()
    dec_id = uuid4()
    candidate = TelegramSignalCandidate(
        decision_id=dec_id,
        symbol="BTCUSDT",
        direction="LONG",
        timeframe="1h",
        strategy_id="H01",
        strategy_release_id="h01-v1",
        reference_price=Decimal("50000"),
        signal_at=now - timedelta(seconds=10),
        expires_at=now + timedelta(minutes=10),
        reason_code="H01_ENTRY",
        detail_vi="Tín hiệu thử nghiệm",
    )
    claim = SignalDeliveryClaim(
        delivery_id=uuid4(),
        subscription_id=uuid4(),
        bot_id=bot_id,
        decision_id=dec_id,
        attempts=1,
        chat_id=123456,
        candidate=candidate,
    )

    class _DummySender:
        async def send_message(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"ok": True}

    worker = TelegramSignalDeliveryWorker(
        session_factory=object(),  # type: ignore[arg-type]
        workspace_id=ws_id,
        sender=_DummySender(),  # type: ignore[arg-type]
        worker_id="w-1",
        max_attempts=5,
        now=lambda: now,
        unit_of_work=lambda _sf, **kw: _FakeUow(object(), **kw),  # type: ignore[arg-type]
        repository=_RecordingRepo,  # type: ignore[arg-type]
    )

    # 403 Forbidden -> ép max_attempts == claim.attempts (1) để mark_failed chuyển thẳng DEAD
    d_403 = worker._retry_matrix.decide(
        TelegramApiError(code="TELEGRAM_HTTP_403", status_code=403), attempt=1
    )
    await worker._mark_failed(claim, d_403)
    assert _RecordingRepo.failed_calls[-1]["max_attempts"] == 1
    assert _RecordingRepo.failed_calls[-1]["error_code"] == "TELEGRAM_HTTP_403"

    # TimeoutError mơ hồ -> log warning khả năng gửi trùng và hẹn retry
    _RecordingRepo.failed_calls.clear()
    with caplog.at_level(logging.WARNING):
        d_timeout = worker._retry_matrix.decide(TimeoutError("read timed out"), attempt=1)
        await worker._mark_failed(claim, d_timeout)
    assert _RecordingRepo.failed_calls[0]["max_attempts"] == 4
    assert any("khả năng gửi trùng" in rec.message for rec in caplog.records)
