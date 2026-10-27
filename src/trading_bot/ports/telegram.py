"""Port gửi tin Telegram (MB-07a). Chưa có adapter mạng: bản ghi bộ nhớ dùng cho PAPER/test."""

from typing import Protocol


class TelegramMessageSender(Protocol):
    """Gửi một tin văn bản tới chat riêng; nội dung không được chứa secret/token."""

    async def send_message(self, chat_id: int, text: str) -> None: ...
