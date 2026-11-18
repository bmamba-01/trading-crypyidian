"""Lỗi an toàn cho lớp gửi Telegram outbound; tuyệt đối không mang token hoặc body thô."""

from __future__ import annotations


class TelegramApiError(RuntimeError):
    """Lỗi API an toàn; không giữ body, token, URL hay mô tả phản hồi của Telegram."""

    def __init__(
        self,
        *,
        code: str,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(code)


class TelegramSensitivityBlockedError(TelegramApiError):
    """Chặn gửi nội dung nhạy cảm (vị thế, số dư, PnL, khóa bí mật) vào group chat."""

    def __init__(self, *, matched_rules: tuple[str, ...]) -> None:
        self.matched_rules = tuple(matched_rules)
        super().__init__(code="TELEGRAM_GROUP_SENSITIVE_CONTENT_BLOCKED")


__all__ = ["TelegramApiError", "TelegramSensitivityBlockedError"]
