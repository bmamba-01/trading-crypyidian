"""Sender Telegram giả: ghi tin vào bộ nhớ có giới hạn, không gọi mạng (MB-07a)."""

from collections import deque


class RecordingTelegramSender:
    """Giữ tối đa `capacity` tin gần nhất để kiểm thử/chẩn đoán; không bao giờ mở kết nối."""

    def __init__(self, capacity: int = 200) -> None:
        self._messages: deque[tuple[int, str]] = deque(maxlen=capacity)

    @property
    def messages(self) -> list[tuple[int, str]]:
        return list(self._messages)

    async def send_message(self, chat_id: int, text: str) -> None:
        self._messages.append((chat_id, text))
