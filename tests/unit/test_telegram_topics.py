"""TG-4: tách tin tín hiệu và PAPER report vào hai topic trong chat riêng của bot.

Bot API 9.3+ (https://core.telegram.org/bots/api, kiểm 17/11/2026) hỗ trợ ``message_thread_id``
cho ``sendMessage``/``sendPhoto`` trong chat riêng có bật chế độ topic và ``createForumTopic``.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from trading_bot.adapters.telegram.bot_api_sender import TelegramApiError, TelegramBotApiSender
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.adapters.telegram.topic_sender import TopicBoundSender, bind_topic

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _ok_sender(requests: list[httpx2.Request], result: object = None) -> TelegramBotApiSender:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"ok": True, "result": result or {"message_id": 1}})

    return TelegramBotApiSender("123456:secret", transport=httpx2.MockTransport(handler))


@pytest.mark.asyncio
async def test_send_message_adds_thread_only_when_given() -> None:
    requests: list[httpx2.Request] = []
    sender = _ok_sender(requests)
    try:
        await sender.send_message(987, "Không topic")
        await sender.send_message(987, "Có topic", message_thread_id=55)
    finally:
        await sender.aclose()
    assert json.loads(requests[0].read()) == {"chat_id": 987, "text": "Không topic"}
    assert json.loads(requests[1].read()) == {
        "chat_id": 987,
        "text": "Có topic",
        "message_thread_id": 55,
    }


@pytest.mark.asyncio
async def test_send_photo_adds_thread_as_form_field() -> None:
    requests: list[httpx2.Request] = []
    sender = _ok_sender(requests)
    try:
        await sender.send_photo(987, PNG, "Thẻ", message_thread_id=77)
        await sender.send_photo(987, PNG, "Thẻ")
    finally:
        await sender.aclose()
    first, second = (request.read() for request in requests)
    assert b'name="message_thread_id"\r\n\r\n77' in first
    assert b"message_thread_id" not in second


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -3, True, "5", 2**53])
async def test_invalid_thread_id_is_rejected_before_network(bad: object) -> None:
    requests: list[httpx2.Request] = []
    sender = _ok_sender(requests)
    try:
        with pytest.raises(ValueError, match="message_thread_id"):
            await sender.send_message(987, "x", message_thread_id=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="message_thread_id"):
            await sender.send_photo(987, PNG, "x", message_thread_id=bad)  # type: ignore[arg-type]
    finally:
        await sender.aclose()
    assert requests == []


@pytest.mark.asyncio
async def test_create_forum_topic_returns_thread_id() -> None:
    requests: list[httpx2.Request] = []
    sender = _ok_sender(requests, {"message_thread_id": 321, "name": "Tín hiệu", "icon_color": 1})
    try:
        thread_id = await sender.create_forum_topic(987, "Tín hiệu")
    finally:
        await sender.aclose()
    assert thread_id == 321
    assert requests[0].url.path.endswith("/createForumTopic")
    assert json.loads(requests[0].read()) == {"chat_id": 987, "name": "Tín hiệu"}


@pytest.mark.asyncio
async def test_create_forum_topic_errors_do_not_leak_token() -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: topics disabled"},
        )

    sender = TelegramBotApiSender("123456:never-log", transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(TelegramApiError) as captured:
            await sender.create_forum_topic(987, "PAPER report")
    finally:
        await sender.aclose()
    assert captured.value.code == "TELEGRAM_HTTP_400"
    assert "never-log" not in str(captured.value)
    assert "topics disabled" not in str(captured.value)


@pytest.mark.asyncio
async def test_create_forum_topic_rejects_missing_thread_in_result() -> None:
    requests: list[httpx2.Request] = []
    sender = _ok_sender(requests, {"name": "x"})
    try:
        with pytest.raises(TelegramApiError, match="TELEGRAM_INVALID_RESPONSE"):
            await sender.create_forum_topic(987, "x")
        with pytest.raises(ValueError, match="Tên topic"):
            await sender.create_forum_topic(987, "")
        with pytest.raises(ValueError, match="Tên topic"):
            await sender.create_forum_topic(987, "a" * 129)
    finally:
        await sender.aclose()


@pytest.mark.asyncio
async def test_recording_sender_records_threads_and_creates_topics() -> None:
    sender = RecordingTelegramSender()
    await sender.send_message(5, "a")
    await sender.send_message(5, "b", message_thread_id=9)
    await sender.send_photo(5, PNG, "c", message_thread_id=10)
    first = await sender.create_forum_topic(5, "Tín hiệu")
    second = await sender.create_forum_topic(5, "PAPER report")
    # Hình dạng cũ của messages/photos giữ nguyên cho test hiện có.
    assert sender.messages == [(5, "a"), (5, "b")]
    assert [photo[0] for photo in sender.photos] == [5]
    assert sender.threads == [("message", 5, None), ("message", 5, 9), ("photo", 5, 10)]
    assert first != second
    assert first > 0
    assert second > 0
    assert sender.topics == [(5, "Tín hiệu", first), (5, "PAPER report", second)]


@pytest.mark.asyncio
async def test_bind_topic_routes_text_and_photo_into_thread() -> None:
    inner = RecordingTelegramSender()
    assert bind_topic(inner, None) is inner
    bound = bind_topic(inner, 42)
    assert isinstance(bound, TopicBoundSender)
    await bound.send_message(5, "tin")
    await bound.send_photo(5, PNG, "ảnh")
    await bound.send_link_confirmation(5, "xác nhận", "bind:x")
    await bound.answer_callback("cb", "ok")
    assert inner.threads == [("message", 5, 42), ("photo", 5, 42)]
    assert list(inner.confirmations) == [(5, "xác nhận", "bind:x")]
    assert list(inner.callback_answers) == [("cb", "ok")]
    with pytest.raises(ValueError, match="message_thread_id"):
        bind_topic(inner, 0)


def test_report_destinations_prefers_topic_and_dedupes_chats() -> None:
    from trading_bot.adapters.telegram.topic_sender import report_destinations, split_destination

    rows = [(5, None), (5, 66), (7, None), (5, None), (9, 12)]
    assert report_destinations(rows) == [(5, 66), (7, None), (9, 12)]
    assert split_destination(42) == (42, None)
    assert split_destination([42, 66]) == (42, 66)
    assert split_destination([42, None]) == (42, None)
    with pytest.raises(ValueError, match="Đích Telegram"):
        split_destination("42")


def test_ai_analysis_cards_go_to_report_topic(tmp_path: object) -> None:
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[2] / "scripts" / "run_ai_analyst.py"
    spec = importlib.util.spec_from_file_location("run_ai_analyst_tg4", script)
    assert spec is not None
    assert spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    sender = RecordingTelegramSender()
    records = [
        {
            "valid": True,
            "topic_key": "H03",
            "topic_report": {},
            "structured_output": {"summary_vi": "x"},
        }
    ]
    assert runner.send_telegram(records, [[5, 66], 7], sender=sender, pause=0) == 2
    assert [(chat, thread) for _, chat, thread in sender.threads] == [(5, 66), (7, None)]
