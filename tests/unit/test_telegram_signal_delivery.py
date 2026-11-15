from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest

from trading_bot.adapters.telegram.bot_api_sender import (
    TelegramApiError,
    TelegramBotApiSender,
)
from trading_bot.application.telegram_signals import (
    TelegramSignalCandidate,
    build_signal_message,
    is_candidate_deliverable,
)

DECISION_ID = UUID("11111111-2222-4333-8444-555555555555")


def _candidate(**overrides: object) -> TelegramSignalCandidate:
    now = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
    values: dict[str, object] = {
        "decision_id": DECISION_ID,
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "timeframe": "5m",
        "strategy_id": "H03",
        "strategy_release_id": "h03-donchian-v1",
        "reference_price": Decimal("68000.25"),
        "signal_at": now,
        "expires_at": now + timedelta(minutes=5),
        "reason_code": "DONCHIAN_BREAKOUT_CONFIRMED",
        "detail_vi": "Giá đóng cửa vượt đỉnh kênh với khối lượng xác nhận.",
        "invalidation": "Nến đóng cửa trở lại dưới vùng phá vỡ.",
    }
    values.update(overrides)
    return TelegramSignalCandidate(**values)  # type: ignore[arg-type]


def test_signal_message_is_vietnamese_and_identifies_itself_as_non_order() -> None:
    message = build_signal_message(_candidate())

    assert "TÍN HIỆU THAM KHẢO — KHÔNG PHẢI LỆNH" in message
    assert str(DECISION_ID) in message
    assert "BTCUSDT" in message
    assert "LONG" in message
    assert "5m" in message
    assert "H03" in message
    assert "DONCHIAN_BREAKOUT_CONFIRMED" in message
    assert "68000.25" in message
    assert "Nến đóng cửa trở lại dưới vùng phá vỡ." in message


def test_signal_delivery_rejects_future_stale_and_expired_candidates() -> None:
    now = datetime(2026, 11, 3, 6, 2, tzinfo=UTC)

    assert is_candidate_deliverable(_candidate(), now=now, max_age=timedelta(minutes=3))
    assert not is_candidate_deliverable(
        _candidate(signal_at=now + timedelta(seconds=30)),
        now=now,
        max_age=timedelta(minutes=3),
    )
    assert not is_candidate_deliverable(
        _candidate(signal_at=now - timedelta(minutes=4)),
        now=now,
        max_age=timedelta(minutes=3),
    )
    assert not is_candidate_deliverable(
        _candidate(expires_at=now), now=now, max_age=timedelta(minutes=3)
    )


@pytest.mark.asyncio
async def test_bot_api_sender_sends_plain_text_without_parse_mode() -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 42}})

    transport = httpx2.MockTransport(handler)
    sender = TelegramBotApiSender("123456:secret", transport=transport)
    try:
        await sender.send_message(987654321, "Tín hiệu thử nghiệm")
    finally:
        await sender.aclose()

    assert len(requests) == 1
    assert requests[0].url.path.endswith("/bot123456:secret/sendMessage")
    payload = json.loads(requests[0].read())
    assert payload == {"chat_id": 987654321, "text": "Tín hiệu thử nghiệm"}
    assert "parse_mode" not in payload


@pytest.mark.asyncio
async def test_bot_api_sender_exposes_retry_after_without_leaking_token() -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
        )

    sender = TelegramBotApiSender("123456:never-log-this", transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(TelegramApiError) as captured:
            await sender.send_message(987654321, "Tín hiệu")
    finally:
        await sender.aclose()

    assert captured.value.retry_after == 17
    assert "never-log-this" not in str(captured.value)
    assert "Too Many Requests" not in str(captured.value)


@pytest.mark.asyncio
async def test_bot_api_sender_rejects_bad_chat_and_non_json_success() -> None:
    sender = TelegramBotApiSender(
        "123456:secret",
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, text="not-json")),
    )
    try:
        with pytest.raises(ValueError, match="chat_id"):
            await sender.send_message(0, "hello")
        with pytest.raises(TelegramApiError):
            await sender.send_message(123, "hello")
    finally:
        await sender.aclose()


def _card() -> dict[str, object]:
    from trading_bot.domain.strategy.signal_card import build_signal_card

    return build_signal_card(
        {
            "symbol": "BTCUSDT",
            "direction": "long",
            "strategy_id": "H03",
            "strategy_release_id": "h03-donchian-v1",
            "reason_code": "DONCHIAN_BREAKOUT_CONFIRMED",
            "signal_at": "2026-11-03T06:00:00+00:00",
            "expires_at": "2026-11-03T06:05:00+00:00",
            "reference_price": "68000.25",
            "atr_value": 400,
            "recipe": {"stop_atr_multiple": 2, "take_profit_atr_multiple": 3},
        },
        checklist=[
            {
                "key": "donchian_breakout",
                "label_vi": "Giá đóng phá kênh Donchian 55 nến (1h)",
                "actual": "đóng 68000.25",
                "operator": ">",
                "threshold": "67900",
                "passed": True,
            },
            {
                "key": "volume_above_median",
                "label_vi": "Quote volume > median 20 nến trước",
                "actual": "thiếu dữ liệu",
                "operator": ">",
                "threshold": "—",
                "passed": None,
            },
        ],
    )


def test_signal_message_includes_card_ladder_checklist_and_paper_label() -> None:
    message = build_signal_message(_candidate(card=_card()))

    assert "— THẺ TÍN HIỆU (PAPER) —" in message
    assert "Vùng vào: 68.000,25" in message
    assert "Cắt lỗ (SL): 67.200,25" in message
    assert "Mục tiêu recipe: 69.200,25 (1,5R)" in message
    assert "Mốc 1R (tham chiếu): 68.800,25 (1,0R)" in message
    assert "Điều kiện (1/2 đạt):" in message
    assert "✓ Giá đóng phá kênh Donchian 55 nến (1h)" in message
    assert "— Quote volume > median 20 nến trước: thiếu dữ liệu" in message
    assert "Mất hiệu lực khi:" in message
    assert "Độ tin cậy: Chưa hiệu chuẩn" in message
    assert message.rstrip().endswith("không phải khuyến nghị đầu tư.")
    assert len(message) <= 4096


def test_signal_message_without_card_keeps_basic_lines_only() -> None:
    message = build_signal_message(_candidate())

    assert "THẺ TÍN HIỆU" not in message
    assert "PAPER · mô phỏng nội bộ" in message


def test_card_checklist_is_truncated_to_keep_message_short() -> None:
    card = _card()
    item = card["checklist"][0]  # type: ignore[index]
    card["checklist"] = [dict(item, label_vi=f"Điều kiện {i}") for i in range(30)]
    message = build_signal_message(_candidate(card=card))

    assert "Điều kiện 7:" in message
    assert "Điều kiện 8:" not in message


def test_repository_builds_card_from_verified_decision_checklist() -> None:
    from tests.unit.test_sig3_signal_card import _decision_for

    from trading_bot.adapters.database.repositories.telegram_signals import _signal_card

    decision = _decision_for("h01-v1")
    card = _signal_card(json.loads(decision.candidate_json), decision)
    assert card is not None
    assert card["checklist_total"] == card["checklist_passed"] > 0
    broken = dict(json.loads(decision.candidate_json), atr_value="không phải số")
    assert _signal_card(broken, decision) is None
