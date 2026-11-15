"""Port gửi tin Telegram (MB-07a). Chưa có adapter mạng: bản ghi bộ nhớ dùng cho PAPER/test."""

from typing import Protocol


class TelegramMessageSender(Protocol):
    """Gửi một tin văn bản tới chat riêng; nội dung không được chứa secret/token."""

    async def send_message(self, chat_id: int, text: str) -> None: ...

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None: ...

    async def answer_callback(self, callback_id: str, text: str) -> None: ...

    async def send_photo(self, chat_id: int, png_bytes: bytes, caption: str) -> None:
        """Gửi ảnh PNG (thẻ TG-3) kèm caption văn bản thuần ≤ 1024 ký tự, không parse mode."""
        ...
