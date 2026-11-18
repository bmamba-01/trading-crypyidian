"""Port gửi tin Telegram (MB-07a; topic TG-4). Adapter: Bot API HTTPS hoặc bản ghi bộ nhớ (test)."""

from typing import Protocol


class TelegramMessageSender(Protocol):
    """Gửi một tin văn bản tới chat riêng; nội dung không được chứa secret/token."""

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        """``message_thread_id`` (TG-4): topic đích; ``reply_to_message_id``: reply tin gốc."""
        ...

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None: ...

    async def answer_callback(self, callback_id: str, text: str) -> None: ...

    async def send_photo(
        self,
        chat_id: int,
        png_bytes: bytes,
        caption: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        """Gửi ảnh PNG (thẻ TG-3) kèm caption văn bản thuần ≤ 1024 ký tự, không parse mode."""
        ...


class TelegramTopicCreator(Protocol):
    """Tạo topic trong chat riêng của bot (TG-4); trả ``message_thread_id``."""

    async def create_forum_topic(self, chat_id: int, name: str) -> int: ...
