"""Bọc sender để mọi tin văn bản/ảnh đi vào một topic cố định (TG-4).

Worker và hàm gửi thẻ giữ chữ ký ``(sender, chat_id, ...)``; định tuyến topic nằm ở lớp bọc này
nên đường dự phòng văn bản khi vẽ thẻ lỗi vẫn rơi vào đúng topic với tin gốc.
"""

from __future__ import annotations

from trading_bot.adapters.telegram.bot_api_sender import validate_thread_id
from trading_bot.ports.telegram import TelegramMessageSender


class TopicBoundSender:
    """Gắn ``message_thread_id`` cho ``send_message``/``send_photo``; thao tác khác giữ nguyên."""

    def __init__(self, inner: TelegramMessageSender, thread_id: int) -> None:
        self._inner = inner
        self.thread_id = validate_thread_id(thread_id)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        return await self._inner.send_message(
            chat_id,
            text,
            message_thread_id=message_thread_id or self.thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def send_photo(
        self,
        chat_id: int,
        png_bytes: bytes,
        caption: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        return await self._inner.send_photo(
            chat_id,
            png_bytes,
            caption,
            message_thread_id=message_thread_id or self.thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
        # Luồng liên kết diễn ra ở chat chính (trước khi có topic); không gắn thread.
        await self._inner.send_link_confirmation(chat_id, text, callback_data)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self._inner.answer_callback(callback_id, text)


def bind_topic(sender: TelegramMessageSender, thread_id: int | None) -> TelegramMessageSender:
    """Chưa cấu hình topic (None) → trả sender gốc: hành vi cũ, tin vào chat chính."""
    if thread_id is None:
        return sender
    return TopicBoundSender(sender, thread_id)


def report_destinations(rows: object) -> list[tuple[int, int | None]]:
    """Gộp ``(chat_id, report_thread_id)`` từ truy vấn binding thành danh sách đích duy nhất.

    Dùng cho script gửi thẻ AI/backtest (owner chọn 17/11/2026: vào topic PAPER report). Mỗi
    chat một đích; nếu chat có topic thì dùng topic đó.
    """
    best: dict[int, int | None] = {}
    for row in rows:  # type: ignore[attr-defined]
        chat_id, thread_id = (int(row), None) if isinstance(row, int) else (row[0], row[1])
        chat_id = int(chat_id)
        thread = int(thread_id) if thread_id is not None else None
        if best.get(chat_id) is None:
            best[chat_id] = thread
    return sorted(best.items())


def split_destination(item: object) -> tuple[int, int | None]:
    """Đích gửi là ``chat_id`` (cũ) hoặc ``[chat_id, thread_id]`` (JSON từ container)."""
    if isinstance(item, int) and not isinstance(item, bool):
        return item, None
    if isinstance(item, list | tuple) and len(item) == 2:
        chat_id, thread_id = item
        return int(chat_id), (int(thread_id) if thread_id is not None else None)
    raise ValueError("Đích Telegram không hợp lệ.")


__all__ = ["TopicBoundSender", "bind_topic", "report_destinations", "split_destination"]
