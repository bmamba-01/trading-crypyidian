"""Sender Telegram giả: ghi tin vào bộ nhớ có giới hạn, không gọi mạng (MB-07a)."""

from collections import deque

from trading_bot.adapters.telegram.bot_api_sender import validate_thread_id


class RecordingTelegramSender:
    """Giữ tối đa `capacity` tin gần nhất để kiểm thử/chẩn đoán; không bao giờ mở kết nối."""

    def __init__(self, capacity: int = 200) -> None:
        self._messages: deque[tuple[int, str]] = deque(maxlen=capacity)
        self.confirmations: deque[tuple[int, str, str]] = deque(maxlen=capacity)
        self.callback_answers: deque[tuple[str, str]] = deque(maxlen=capacity)
        # Ảnh thẻ TG-3: (chat_id, PNG, caption); giữ ít hơn tin chữ vì mỗi ảnh ~100–300 KB.
        self.photos: deque[tuple[int, bytes, str]] = deque(maxlen=max(1, min(capacity, 50)))
        # TG-4: (loại tin, chat_id, message_thread_id) theo thứ tự gửi, để kiểm định tuyến topic.
        self._threads: deque[tuple[str, int, int | None]] = deque(maxlen=capacity)
        self._topics: deque[tuple[int, str, int]] = deque(maxlen=capacity)
        # SIG-8: (loại tin, chat_id, message_id, reply_to_message_id) để kiểm reply cập nhật.
        self._replies: deque[tuple[str, int, int, int | None]] = deque(maxlen=capacity)
        self._next_thread_id = 1000
        self._next_message_id = 1

    @property
    def replies(self) -> list[tuple[str, int, int, int | None]]:
        return list(self._replies)

    @property
    def threads(self) -> list[tuple[str, int, int | None]]:
        return list(self._threads)

    @property
    def topics(self) -> list[tuple[int, str, int]]:
        return list(self._topics)

    @property
    def messages(self) -> list[tuple[int, str]]:
        return list(self._messages)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        if message_thread_id is not None:
            validate_thread_id(message_thread_id)
        if reply_to_message_id is not None:
            validate_thread_id(reply_to_message_id)
        msg_id = self._next_message_id
        self._next_message_id += 1
        self._messages.append((chat_id, text))
        self._threads.append(("message", chat_id, message_thread_id))
        self._replies.append(("message", chat_id, msg_id, reply_to_message_id))
        return msg_id

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
        self.confirmations.append((chat_id, text, callback_data))

    async def answer_callback(self, callback_id: str, text: str) -> None:
        self.callback_answers.append((callback_id, text))

    async def send_photo(
        self,
        chat_id: int,
        png_bytes: bytes,
        caption: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        if message_thread_id is not None:
            validate_thread_id(message_thread_id)
        if reply_to_message_id is not None:
            validate_thread_id(reply_to_message_id)
        msg_id = self._next_message_id
        self._next_message_id += 1
        self.photos.append((chat_id, bytes(png_bytes), caption))
        self._threads.append(("photo", chat_id, message_thread_id))
        self._replies.append(("photo", chat_id, msg_id, reply_to_message_id))
        return msg_id

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        """Giả lập ``createForumTopic``: cấp id tăng dần, không gọi mạng."""
        self._next_thread_id += 1
        self._topics.append((chat_id, name, self._next_thread_id))
        return self._next_thread_id
