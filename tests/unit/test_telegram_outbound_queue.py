"""Unit & integration test SEC-39: outbound rate limit, 429 retry_after, dead-letter,
bảo vệ nội dung nhạy cảm vào group và nạp/xoay token Telegram qua SecretProvider.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
from trading_bot.adapters.secrets.telegram_token import (
    DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE,
    CompositeSecretProvider,
    FileTelegramBotSecretProvider,
)
from trading_bot.adapters.telegram.bot_api_sender import (
    TelegramApiError,
    TelegramBotApiSender,
)
from trading_bot.adapters.telegram.outbound_guard import (
    GroupSensitivityGuard,
    TelegramSensitivityBlockedError,
)
from trading_bot.adapters.telegram.outbound_queue import (
    InMemoryDeadLetterStore,
    JsonlDeadLetterSink,
    RetryRule,
    TelegramFailureClass,
    TelegramOutboundLimiter,
    TelegramOutboundMessage,
    TelegramOutboundQueue,
    TelegramRateLimitPolicy,
    TelegramRetryMatrix,
    ThrottledTelegramSender,
)
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.app import _build_telegram_sender, create_app
from trading_bot.application.telegram_signals import (
    TelegramSignalCandidate,
    build_signal_message,
)
from trading_bot.application.telegram_trade_results import (
    BotSummaryRow,
    TradeNotificationKind,
    TradeResultView,
    build_paper_report_view,
    format_paper_report_text,
    format_trade_result_text,
)
from trading_bot.config.settings import AppSettings
from trading_bot.ports.secrets import SecretAccessError, SecretLease

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOT = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeClock:
    """Đồng hồ giả định tất định: ``sleep`` dịch chuyển thời gian và ghi lại mốc chờ."""

    def __init__(self, start: float = 0.0) -> None:
        self.now_seconds = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now_seconds

    def wall_now(self) -> datetime:
        return NOW + timedelta(seconds=self.now_seconds)

    async def sleep(self, seconds: float) -> None:
        delay = max(0.0, float(seconds))
        self.sleeps.append(delay)
        self.now_seconds += delay


class TimestampedSender:
    """Sender giả ghi lại thời điểm thực gửi cho từng tin nhắn."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.sent: list[tuple[float, str, int, str]] = []
        self.photos: list[tuple[float, int, str]] = []

    async def send_message(
        self, chat_id: int, text: str, *, message_thread_id: int | None = None
    ) -> None:
        del message_thread_id
        self.sent.append((self.clock(), "message", chat_id, text))

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
        del callback_data
        self.sent.append((self.clock(), "link", chat_id, text))

    async def answer_callback(self, callback_id: str, text: str) -> None:
        del callback_id
        self.sent.append((self.clock(), "callback", 0, text))

    async def send_photo(
        self,
        chat_id: int,
        png_bytes: bytes,
        caption: str,
        *,
        message_thread_id: int | None = None,
    ) -> None:
        del png_bytes, message_thread_id
        self.photos.append((self.clock(), chat_id, caption))


class RotatingFakeSecretProvider:
    """SecretProvider giả trong test hỗ trợ xoay token và kiểm tra fail-closed."""

    def __init__(self, tokens: list[str], *, ttl_seconds: int = 60) -> None:
        self._tokens = list(tokens)
        self._ttl = ttl_seconds
        self.calls: list[tuple[str, UUID | None]] = []

    def store_mfa_secret(self, workspace_id: UUID, value: str) -> str:
        del workspace_id, value
        raise SecretAccessError("Không cấp MFA")

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str:
        del workspace_id, reference
        raise SecretAccessError("Không cấp MFA")

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None:
        del workspace_id, reference
        raise SecretAccessError("Không cấp MFA")

    def resolve_for_gateway(self, workspace_id: UUID, reference: str) -> SecretLease:
        del workspace_id, reference
        raise SecretAccessError("Không cấp secret sàn")

    def resolve_telegram_bot_token(
        self, reference: str, *, workspace_id: UUID | None = None
    ) -> SecretLease:
        self.calls.append((reference, workspace_id))
        if not self._tokens:
            raise SecretAccessError("Thiếu token Telegram trong vault")
        token = self._tokens[0] if len(self._tokens) == 1 else self._tokens.pop(0)
        return SecretLease(
            reference=reference,
            workspace_id=workspace_id or UUID(int=0),
            expires_in_seconds=self._ttl,
            value=token,
        )


def _public_signal_candidate() -> TelegramSignalCandidate:
    return TelegramSignalCandidate(
        decision_id=UUID("11111111-2222-4333-8444-555555555555"),
        symbol="BTCUSDT",
        direction="LONG",
        timeframe="1h",
        strategy_id="H03",
        strategy_release_id="h03-donchian-v1",
        reference_price=Decimal("91500.5"),
        signal_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        reason_code="DONCHIAN_BREAKOUT",
        detail_vi="Giá đóng nến vượt đỉnh kênh 20 kỳ.",
        invalidation="Đóng nến dưới 89.500",
        card={
            "entry_zone": {"low": "91400", "high": "91600"},
            "stop": "89500",
            "targets": [{"label": "TP1", "price": "95500", "r_multiple": "2.0"}],
            "checklist": [
                {
                    "label_vi": "Xu hướng 4h",
                    "actual": "Tăng",
                    "operator": "==",
                    "threshold": "Tăng",
                    "passed": True,
                }
            ],
            "checklist_passed": 1,
            "checklist_total": 1,
            "confidence": {"label": "Trung bình"},
        },
    )


@pytest.mark.asyncio
async def test_private_chat_rate_limit_spaces_messages_one_per_second() -> None:
    clock = FakeClock()
    inner = TimestampedSender(clock)
    limiter = TelegramOutboundLimiter(clock=clock, sleep=clock.sleep)
    sender = ThrottledTelegramSender(inner, limiter=limiter, clock=clock.wall_now)

    for index in range(4):
        await sender.send_message(1001, f"Tin {index}")

    timestamps = [item[0] for item in inner.sent]
    assert timestamps == pytest.approx([0.0, 1.0, 2.0, 3.0])


@pytest.mark.asyncio
async def test_private_chat_concurrent_sends_preserve_spacing() -> None:
    clock = FakeClock()
    inner = TimestampedSender(clock)
    limiter = TelegramOutboundLimiter(clock=clock, sleep=clock.sleep)
    sender = ThrottledTelegramSender(inner, limiter=limiter, clock=clock.wall_now)

    await asyncio.gather(*(sender.send_message(2002, f"Tin {i}") for i in range(4)))

    timestamps = sorted(item[0] for item in inner.sent)
    assert len(timestamps) == 4
    assert all(b - a >= 1.0 - 1e-9 for a, b in itertools.pairwise(timestamps))


@pytest.mark.asyncio
async def test_group_rate_limit_enforces_twenty_messages_per_minute() -> None:
    clock = FakeClock()
    inner = TimestampedSender(clock)
    limiter = TelegramOutboundLimiter(clock=clock, sleep=clock.sleep)
    sender = ThrottledTelegramSender(inner, limiter=limiter, clock=clock.wall_now)

    group_chat_id = -100987654321
    for index in range(22):
        await sender.send_message(group_chat_id, f"Thông báo công khai #{index}")

    timestamps = [item[0] for item in inner.sent]
    assert len(timestamps) == 22
    assert all(ts == pytest.approx(0.0) for ts in timestamps[:20])
    assert timestamps[20] == pytest.approx(60.0)
    assert timestamps[21] == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_global_broadcast_limit_enforces_thirty_messages_per_second() -> None:
    clock = FakeClock()
    inner = TimestampedSender(clock)
    limiter = TelegramOutboundLimiter(clock=clock, sleep=clock.sleep)
    sender = ThrottledTelegramSender(inner, limiter=limiter, clock=clock.wall_now)

    for index in range(65):
        await sender.send_message(10_000 + index, f"Broadcast {index}")

    timestamps = [item[0] for item in inner.sent]
    assert sum(1 for ts in timestamps if ts < 1.0) == 30
    assert sum(1 for ts in timestamps if 1.0 <= ts < 2.0) == 30
    assert sum(1 for ts in timestamps if 2.0 <= ts < 3.0) == 5


@pytest.mark.asyncio
async def test_http_429_retry_after_waits_exact_duration_and_recovers() -> None:
    clock = FakeClock()
    attempts = 0
    seen_tokens_in_urls: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        seen_tokens_in_urls.append(str(request.url))
        if attempts == 1:
            return httpx2.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 17",
                    "parameters": {"retry_after": 17},
                },
            )
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 99}})

    bot_sender = TelegramBotApiSender(
        "123456:secret-token-never-log",
        transport=httpx2.MockTransport(handler),
    )
    limiter = TelegramOutboundLimiter(clock=clock, sleep=clock.sleep)
    queue = TelegramOutboundQueue(
        bot_sender,
        limiter=limiter,
        clock=clock,
        wall_clock=clock.wall_now,
        sleep=clock.sleep,
    )
    try:
        outcome = await queue.submit(
            TelegramOutboundMessage(chat_id=987654321, text="Tín hiệu hợp lệ")
        )
    finally:
        await bot_sender.aclose()

    assert outcome.status == "SENT"
    assert outcome.attempts == 2
    assert attempts == 2
    assert clock.now_seconds == pytest.approx(17.0)
    assert len(queue.dead_letters) == 0


@pytest.mark.asyncio
async def test_queue_does_not_head_of_line_block_other_chats_on_429() -> None:
    clock = FakeClock()
    delivered: list[tuple[float, int, str]] = []
    chat_a_attempts = 0

    class Selective429Sender:
        async def send_message(
            self, chat_id: int, text: str, *, message_thread_id: int | None = None
        ) -> None:
            nonlocal chat_a_attempts
            del message_thread_id
            if chat_id == 101:
                chat_a_attempts += 1
                if chat_a_attempts == 1:
                    raise TelegramApiError(
                        code="TELEGRAM_HTTP_429", status_code=429, retry_after=15
                    )
            delivered.append((clock.now_seconds, chat_id, text))

        async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
            del chat_id, text, callback_data

        async def answer_callback(self, callback_id: str, text: str) -> None:
            del callback_id, text

        async def send_photo(
            self,
            chat_id: int,
            png_bytes: bytes,
            caption: str,
            *,
            message_thread_id: int | None = None,
        ) -> None:
            del chat_id, png_bytes, caption, message_thread_id

    queue = TelegramOutboundQueue(
        Selective429Sender(),
        limiter=TelegramOutboundLimiter(clock=clock, sleep=clock.sleep),
        clock=clock,
        wall_clock=clock.wall_now,
        sleep=clock.sleep,
    )
    queue.enqueue(TelegramOutboundMessage(chat_id=101, text="A1"))
    queue.enqueue(TelegramOutboundMessage(chat_id=202, text="B1"))

    outcomes = await queue.drain()

    assert [item.status for item in outcomes] == ["SENT", "SENT"]
    assert delivered == [
        (0.0, 202, "B1"),
        (15.0, 101, "A1"),
    ]


@pytest.mark.asyncio
async def test_retry_matrix_dead_letters_exhausted_429_and_permanent_errors(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    dead_file = tmp_path / "telegram_dead_letter.jsonl"
    jsonl_sink = JsonlDeadLetterSink(dead_file)
    memory_sink = InMemoryDeadLetterStore()

    class AlwaysFailSender:
        def __init__(self, error: Exception) -> None:
            self.error = error
            self.calls = 0

        async def send_message(
            self, chat_id: int, text: str, *, message_thread_id: int | None = None
        ) -> None:
            del chat_id, text, message_thread_id
            self.calls += 1
            raise self.error

        async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
            del chat_id, text, callback_data

        async def answer_callback(self, callback_id: str, text: str) -> None:
            del callback_id, text

        async def send_photo(
            self,
            chat_id: int,
            png_bytes: bytes,
            caption: str,
            *,
            message_thread_id: int | None = None,
        ) -> None:
            del chat_id, png_bytes, caption, message_thread_id

    matrix = TelegramRetryMatrix(
        rules={
            TelegramFailureClass.RATE_LIMITED: RetryRule(
                retryable=True, max_attempts=3, base_backoff_seconds=2.0, max_backoff_seconds=30.0
            ),
            TelegramFailureClass.PERMANENT_CLIENT: RetryRule(retryable=False, max_attempts=1),
        }
    )

    # 1) 429 không có retry_after → backoff 2s, 4s rồi đẩy vào dead-letter ở lần 3
    rate_sender = AlwaysFailSender(TelegramApiError(code="TELEGRAM_HTTP_429", status_code=429))
    queue = TelegramOutboundQueue(
        rate_sender,
        limiter=TelegramOutboundLimiter(clock=clock, sleep=clock.sleep),
        retry_matrix=matrix,
        dead_letter_sinks=(memory_sink, jsonl_sink),
        clock=clock,
        wall_clock=clock.wall_now,
        sleep=clock.sleep,
    )
    queue.enqueue(
        TelegramOutboundMessage(
            chat_id=777,
            text="Tin mật không nên in ra log",
            correlation_id="corr-429",
        )
    )
    outcomes = await queue.drain()
    assert len(outcomes) == 1
    assert outcomes[0].status == "DEAD_LETTERED"
    assert rate_sender.calls == 3
    assert clock.now_seconds == pytest.approx(6.0)

    # 2) Lỗi 403 permanent → không retry mù, đẩy thẳng dead-letter sau 1 lần thử
    perm_sender = AlwaysFailSender(TelegramApiError(code="TELEGRAM_HTTP_403", status_code=403))
    queue2 = TelegramOutboundQueue(
        perm_sender,
        limiter=TelegramOutboundLimiter(clock=clock, sleep=clock.sleep),
        retry_matrix=matrix,
        dead_letter_sinks=(memory_sink, jsonl_sink),
        clock=clock,
        wall_clock=clock.wall_now,
        sleep=clock.sleep,
    )
    queue2.enqueue(
        TelegramOutboundMessage(chat_id=888, text="Bot bị chặn", correlation_id="corr-403")
    )
    outcomes2 = await queue2.drain()
    assert outcomes2[0].status == "DEAD_LETTERED"
    assert perm_sender.calls == 1

    records = memory_sink.entries
    assert len(records) == 2
    assert records[0].error_code == "TELEGRAM_HTTP_429"
    assert records[0].failure_class == "RATE_LIMITED"
    assert records[0].attempts == 3
    assert records[0].correlation_id == "corr-429"
    assert records[1].error_code == "TELEGRAM_HTTP_403"
    assert records[1].failure_class == "PERMANENT_CLIENT"
    assert records[1].attempts == 1

    raw_jsonl = dead_file.read_text(encoding="utf-8")
    assert "Tin mật không nên in ra log" not in raw_jsonl
    lines = [json.loads(line) for line in raw_jsonl.splitlines()]
    assert len(lines) == 2
    assert lines[0]["payload_digest"]
    assert lines[1]["error_code"] == "TELEGRAM_HTTP_403"


@pytest.mark.asyncio
async def test_group_sensitivity_guard_blocks_position_balance_and_allows_public_signal() -> None:
    clock = FakeClock()
    inner = TimestampedSender(clock)
    dead_letters = InMemoryDeadLetterStore()
    sender = ThrottledTelegramSender(
        inner,
        limiter=TelegramOutboundLimiter(clock=clock, sleep=clock.sleep),
        sensitivity_guard=GroupSensitivityGuard(mode="block"),
        dead_letter_sinks=(dead_letters,),
        clock=clock.wall_now,
    )

    group_id = -1001234567890
    private_id = 987654321

    # Tín hiệu công khai hợp lệ được phép gửi vào group
    public_text = build_signal_message(_public_signal_candidate())
    await sender.send_message(group_id, public_text)
    assert len(inner.sent) == 1

    # Chi tiết vị thế/PnL của Crypidian được phép gửi ở chat riêng, bị chặn ở group
    trade_view = TradeResultView(
        kind=TradeNotificationKind.CLOSED,
        bot_id=BOT,
        bot_name="Bot Alpha",
        trade_id=uuid4(),
        symbol="BTCUSDT",
        direction="LONG",
        quote_asset="USDT",
        entry_price=Decimal("90000"),
        entry_quantity=Decimal("0.25"),
        opened_at=NOW - timedelta(hours=2),
        exit_price=Decimal("92000"),
        exit_quantity=Decimal("0.25"),
        closed_at=NOW,
        exit_reason_code="TAKE_PROFIT",
        exit_reason_vi="Chạm mục tiêu chốt lời",
        net_pnl=Decimal("485.50"),
        risk_amount=Decimal("200"),
        r_multiple=Decimal("2.43"),
        hold_duration=timedelta(hours=2),
    )
    sensitive_trade_text = format_trade_result_text(trade_view)
    await sender.send_message(private_id, sensitive_trade_text)
    assert len(inner.sent) == 2

    for bad_payload in (
        sensitive_trade_text,
        "Số dư tài khoản: 25,400 USDT | Margin: 2,000 USDT",
        "Open position BTCUSDT size: 1.5 BTC, unrealized PnL: +1200 USD",
    ):
        with pytest.raises(TelegramSensitivityBlockedError):
            await sender.send_message(group_id, bad_payload)

    # Ảnh thẻ có caption nhạy cảm cũng bị chặn trước khi gửi ra group
    with pytest.raises(TelegramSensitivityBlockedError):
        await sender.send_photo(group_id, PNG_BYTES, "KẾT QUẢ PAPER: PnL ròng +485.5 USDT")

    assert len(inner.sent) == 2
    assert len(inner.photos) == 0
    assert len(dead_letters.entries) == 4
    assert all(
        entry.error_code == "TELEGRAM_GROUP_SENSITIVE_CONTENT_BLOCKED"
        for entry in dead_letters.entries
    )


@pytest.mark.asyncio
async def test_group_sensitivity_guard_redact_mode_sanitizes_text_but_blocks_photos() -> None:
    clock = FakeClock()
    inner = TimestampedSender(clock)
    sender = ThrottledTelegramSender(
        inner,
        limiter=TelegramOutboundLimiter(clock=clock, sleep=clock.sleep),
        sensitivity_guard=GroupSensitivityGuard(mode="redact"),
        clock=clock.wall_now,
    )

    group_id = -100555666777
    await sender.send_message(
        group_id,
        "Bản tin hệ thống BTCUSDT\nSố dư khả dụng: 99,000 USDT\nTrạng thái thị trường bình thường.",
    )
    assert len(inner.sent) == 1
    sent_text = inner.sent[0][3]
    assert "99,000" not in sent_text
    assert "ĐÃ CHE" in sent_text
    assert "Trạng thái thị trường bình thường." in sent_text

    # Ảnh PNG không thể che bằng regex text nên vẫn chặn tuyệt đối
    with pytest.raises(TelegramSensitivityBlockedError):
        await sender.send_photo(group_id, PNG_BYTES, "Số dư ví: 99,000 USDT")


@pytest.mark.asyncio
async def test_bot_api_sender_guards_group_chats_and_rotates_token_via_secret_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    requests: list[str] = []
    old_token = "111111:old-secret-token-never-leak"
    new_token = "222222:new-rotated-secret-never-leak"
    provider = RotatingFakeSecretProvider([old_token, new_token], ttl_seconds=60)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        requests.append(url)
        if old_token in url and len(requests) >= 2:
            # Mô phỏng token cũ bị BotFather /revoke trước khi TTL cục bộ hết hạn
            return httpx2.Response(401, json={"ok": False, "error_code": 401})
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    sender = TelegramBotApiSender.from_secret_provider(
        provider,
        reference=DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE,
        workspace_id=WORKSPACE,
        transport=httpx2.MockTransport(handler),
        clock=clock,
        min_unauthorized_refresh_interval_seconds=0.0,
    )
    try:
        with caplog.at_level(logging.INFO):
            await sender.send_message(987654321, "Tin trước khi xoay")
            # Lần 2 gặp 401 từ token cũ -> tự làm mới từ SecretProvider và gửi lại thành công
            await sender.send_message(987654321, "Tin ngay sau khi BotFather revoke")
            # Gửi vị thế nhạy cảm vào group bị chặn ngay tại sender
            with pytest.raises(TelegramSensitivityBlockedError):
                await sender.send_message(-100333444, "Vị thế mở BTCUSDT · Số dư: 5000 USDT")
            # Gửi tin công khai vào group thành công
            await sender.send_message(-100333444, build_signal_message(_public_signal_candidate()))
    finally:
        await sender.aclose()

    assert any(old_token in url for url in requests)
    assert any(new_token in url for url in requests)
    assert requests[-1].startswith(f"https://api.telegram.org/bot{new_token}/sendMessage")
    assert old_token not in caplog.text
    assert new_token not in caplog.text


def test_secret_provider_isolation_and_file_token_rotation(tmp_path: Path) -> None:
    key_file = tmp_path / "mfa.key"
    key_file.write_text(base64.urlsafe_b64encode(b"k" * 32).decode("ascii"), encoding="utf-8")
    mfa_provider = LocalMfaSecretProvider(key_file=key_file, storage_directory=tmp_path / "vault")

    with pytest.raises(SecretAccessError):
        mfa_provider.resolve_telegram_bot_token(DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE)

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("123456:token-v1-secret\n", encoding="utf-8")
    tg_provider = FileTelegramBotSecretProvider(token_file=token_file, lease_ttl_seconds=120)

    lease1 = tg_provider.resolve_telegram_bot_token(
        DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE, workspace_id=WORKSPACE
    )
    assert lease1.value == "123456:token-v1-secret"
    assert lease1.expires_in_seconds == 120
    assert "token-v1-secret" not in repr(lease1)

    # Xoay token trên đĩa: lần resolve kế tiếp lấy ngay token mới
    token_file.write_text("123456:token-v2-rotated\n", encoding="utf-8")
    composite = CompositeSecretProvider(mfa_provider=mfa_provider, telegram_provider=tg_provider)
    lease2 = composite.resolve_telegram_bot_token(
        DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE, workspace_id=WORKSPACE
    )
    assert lease2.value == "123456:token-v2-rotated"

    with pytest.raises(SecretAccessError):
        tg_provider.resolve_telegram_bot_token("unknown-ref")


def test_app_builds_telegram_sender_from_secret_provider_and_fails_closed_in_production(
    tmp_path: Path,
) -> None:
    # 1) Production thiếu SecretProvider / token cho signal-worker hoặc admin-web bật Telegram
    # phải fail-closed
    with pytest.raises(RuntimeError):
        _build_telegram_sender(
            AppSettings(
                app_env="production",
                process_role="signal-worker",
                signal_workspace_id=WORKSPACE,
                telegram_bot_token_reference="telegram/prod-bot",
            ),
            secret_provider=None,
            required=True,
        )

    webhook_secret = tmp_path / "webhook_secret"
    webhook_secret.write_text("a" * 40, encoding="utf-8")
    with pytest.raises(RuntimeError):
        _build_telegram_sender(
            AppSettings(
                app_env="production",
                process_role="admin-web",
                telegram_workspace_id=WORKSPACE,
                telegram_webhook_secret_file=webhook_secret,
            ),
            secret_provider=None,
            required=False,
        )

    # 2) Production với SecretProvider hợp lệ -> ThrottledTelegramSender bọc TelegramBotApiSender
    provider = RotatingFakeSecretProvider(["999999:prod-secret-token-xyz"])
    prod_sender = _build_telegram_sender(
        AppSettings(
            app_env="production",
            process_role="signal-worker",
            signal_workspace_id=WORKSPACE,
            telegram_bot_token_reference="telegram/prod-bot",
        ),
        secret_provider=provider,
        required=True,
    )
    assert isinstance(prod_sender, ThrottledTelegramSender)
    assert provider.calls == [("telegram/prod-bot", WORKSPACE)]

    # 3) Local dev fallback: token file rỗng trên admin-web giữ RecordingTelegramSender
    empty_file = tmp_path / "empty_token"
    empty_file.write_text("", encoding="utf-8")
    local_sender = _build_telegram_sender(
        AppSettings(
            app_env="local",
            process_role="admin-web",
            telegram_bot_token_file=empty_file,
        ),
        secret_provider=None,
        required=False,
    )
    assert isinstance(local_sender, RecordingTelegramSender)

    # 4) create_app nhận secret_provider và gắn sender qua SecretProvider cho admin-web
    provider_app = RotatingFakeSecretProvider(["888888:app-secret-token-xyz"])
    app = create_app(
        AppSettings(
            app_env="test",
            process_role="admin-web",
            telegram_bot_token_reference="telegram/test-bot",
        ),
        secret_provider=provider_app,
    )
    with TestClient(app) as client:
        assert isinstance(client.app.state.telegram_sender, ThrottledTelegramSender)
        assert provider_app.calls == [("telegram/test-bot", None)]


def test_settings_reject_telegram_token_reference_on_non_telegram_roles() -> None:
    with pytest.raises(ValidationError, match="Token Telegram"):
        AppSettings(
            process_role="market-signal-worker",
            market_signal_workspace_id=WORKSPACE,
            telegram_bot_token_reference="telegram/bot",
        )
    # signal-worker chấp nhận telegram_bot_token_reference mà không cần file
    settings = AppSettings(
        process_role="signal-worker",
        signal_workspace_id=WORKSPACE,
        telegram_bot_token_reference="telegram/bot",
    )
    assert settings.telegram_bot_token_reference == "telegram/bot"


def test_telegram_bot_token_rotation_runbook_exists_in_vietnamese() -> None:
    runbook = Path("docs/operations/telegram-token-rotation-runbook.md")
    assert runbook.is_file()
    content = runbook.read_text(encoding="utf-8")
    assert "BotFather" in content
    assert "/revoke" in content
    assert "SecretProvider" in content


@pytest.mark.asyncio
async def test_bot_api_sender_rotates_proactively_when_lease_expires() -> None:
    clock = FakeClock()
    seen_urls: list[str] = []
    token_v1 = "333333:lease-v1-secret"
    token_v2 = "333333:lease-v2-rotated"
    provider = RotatingFakeSecretProvider([token_v1, token_v2], ttl_seconds=30)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen_urls.append(str(request.url))
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    sender = TelegramBotApiSender.from_secret_provider(
        provider,
        transport=httpx2.MockTransport(handler),
        clock=clock,
    )
    try:
        await sender.send_message(987654321, "Trước khi hết lease")
        clock.now_seconds += 31.0
        await sender.send_message(987654321, "Sau khi lease hết hạn")
    finally:
        await sender.aclose()

    assert seen_urls[0].startswith(f"https://api.telegram.org/bot{token_v1}/sendMessage")
    assert seen_urls[1].startswith(f"https://api.telegram.org/bot{token_v2}/sendMessage")


@pytest.mark.asyncio
async def test_integration_throttled_sender_with_bot_api_429_and_signal_worker_mode() -> None:
    clock = FakeClock()
    attempts = 0
    calls: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        calls.append(json.loads(request.read()))
        if attempts == 1:
            return httpx2.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 4}},
            )
        if attempts == 3:
            return httpx2.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 9}},
            )
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": attempts}})

    # 1) Chế độ admin-web (retry_on_error=True): gặp 429 retry_after=4 -> tự chờ và gửi lại
    api_sender = TelegramBotApiSender(
        "444444:integration-secret", transport=httpx2.MockTransport(handler)
    )
    limiter = TelegramOutboundLimiter(
        policy=TelegramRateLimitPolicy(private_interval_seconds=1.0),
        clock=clock,
        sleep=clock.sleep,
    )
    admin_sender = ThrottledTelegramSender(
        api_sender,
        limiter=limiter,
        retry_on_error=True,
        clock=clock.wall_now,
        sleep=clock.sleep,
    )
    try:
        await admin_sender.send_message(555, "Trả lời lệnh webhook")
        assert attempts == 2
        assert clock.now_seconds == pytest.approx(4.0)

        # 2) Chế độ signal-worker (retry_on_error=False): đẩy TelegramApiError cho DB worker,
        # đồng thời vẫn ghi cooldown vào limiter và chặn tin tổng hợp PnL vào group chat.
        worker_sender = ThrottledTelegramSender(
            api_sender,
            limiter=limiter,
            retry_on_error=False,
            clock=clock.wall_now,
            sleep=clock.sleep,
        )
        with pytest.raises(TelegramApiError) as err:
            await worker_sender.send_message(555, "Tín hiệu mới")
        assert err.value.retry_after == 9
        assert limiter.earliest_ready_at(555) >= clock.now_seconds + 9.0 - 1e-9

        report = build_paper_report_view(
            window_start=NOW - timedelta(hours=4),
            window_end=NOW,
            rows=(
                BotSummaryRow(
                    bot_id=BOT,
                    bot_name="Bot Alpha",
                    quote_asset="USDT",
                    window_closed_count=3,
                    window_net_pnl=Decimal("120.5"),
                    total_closed_count=10,
                    total_winning_count=7,
                    total_net_pnl=Decimal("450.0"),
                    open_positions=1,
                    max_drawdown=Decimal("35.0"),
                ),
            ),
        )
        with pytest.raises(TelegramSensitivityBlockedError):
            await worker_sender.send_message(-100888999, format_paper_report_text(report))
    finally:
        await api_sender.aclose()


@pytest.mark.asyncio
async def test_outbound_queue_background_run_loop_and_policy_validation() -> None:
    with pytest.raises(ValueError, match="phải là số hữu hạn dương"):
        TelegramRateLimitPolicy(private_interval_seconds=0.0)
    with pytest.raises(ValueError, match="group_max_per_minute"):
        TelegramRateLimitPolicy(group_max_per_minute=0)

    clock = FakeClock()
    inner = TimestampedSender(clock)
    queue = TelegramOutboundQueue(
        inner,
        limiter=TelegramOutboundLimiter(clock=clock, sleep=clock.sleep),
        clock=clock,
        wall_clock=clock.wall_now,
        sleep=clock.sleep,
    )
    stopping = asyncio.Event()
    worker_task = asyncio.create_task(queue.run(stopping, idle_poll_seconds=0.01))
    queue.enqueue(TelegramOutboundMessage(chat_id=777, text="Tin nền 1"))
    queue.enqueue(TelegramOutboundMessage(chat_id=778, text="Tin nền 2"))
    stopping.set()
    await worker_task

    assert queue.stats.sent == 2
    assert len(inner.sent) == 2
