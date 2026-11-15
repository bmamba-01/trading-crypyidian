"""Adapter Telegram Bot API dùng HTTP có giới hạn và không lộ token qua lỗi."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx2

_TOKEN = re.compile(r"^[^\s/?#]{8,512}$")
_MAX_MESSAGE_CHARS = 4096
_MAX_CAPTION_CHARS = 1024
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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


def load_telegram_bot_token(path: Path) -> str:
    """Đọc token từ secret file và loại khoảng trắng thừa quanh giá trị."""
    token = path.read_text(encoding="utf-8").strip()
    if not _TOKEN.fullmatch(token):
        raise ValueError("Tệp token Telegram không có giá trị hợp lệ.")
    return token


class TelegramBotApiSender:
    """Gửi tin nhắn văn bản, ảnh thẻ PNG, xác nhận liên kết và callback qua Bot API HTTPS."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not _TOKEN.fullmatch(token):
            raise ValueError("Token Telegram không hợp lệ.")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds phải trong khoảng 1–60 giây.")
        self._token = token
        self._client = httpx2.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/",
            transport=transport,
            timeout=httpx2.Timeout(timeout_seconds, connect=min(timeout_seconds, 3.0)),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "Crypidian/0.1"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TelegramBotApiSender:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def send_message(self, chat_id: int, text: str) -> None:
        _validate_message(chat_id, text)
        await self._request(
            "sendMessage",
            {"chat_id": chat_id, "text": text},
        )

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
        _validate_message(chat_id, text)
        if not callback_data or len(callback_data.encode("utf-8")) > 64:
            raise ValueError("callback_data Telegram không hợp lệ.")
        await self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Xác nhận liên kết", "callback_data": callback_data}]
                    ]
                },
            },
        )

    async def answer_callback(self, callback_id: str, text: str) -> None:
        if not callback_id or len(callback_id) > 256 or len(text) > 200:
            raise ValueError("Thông tin callback Telegram không hợp lệ.")
        await self._request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def send_photo(self, chat_id: int, png_bytes: bytes, caption: str) -> None:
        """Tải ảnh PNG bằng multipart/form-data (``sendPhoto``); caption thuần, không parse mode.

        Giới hạn theo https://core.telegram.org/bots/api#sendphoto (kiểm 15/11/2026): ảnh ≤ 10 MB,
        caption 0–1024 ký tự. Kích thước ảnh (rộng + cao ≤ 10000) do renderer bảo đảm.
        """
        _validate_chat_id(chat_id)
        if not isinstance(png_bytes, bytes | bytearray) or not png_bytes.startswith(_PNG_SIGNATURE):
            raise ValueError("Ảnh Telegram phải là PNG hợp lệ.")
        if len(png_bytes) > _MAX_PHOTO_BYTES:
            raise ValueError("Ảnh Telegram vượt giới hạn 10 MB.")
        if not isinstance(caption, str) or len(caption) > _MAX_CAPTION_CHARS:
            raise ValueError("Caption Telegram phải có tối đa 1024 ký tự.")
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        await self._post(
            "sendPhoto",
            data=data,
            files={"photo": ("card.png", bytes(png_bytes), "image/png")},
        )

    async def _request(self, method: str, payload: dict[str, Any]) -> None:
        await self._post(method, json=payload)

    async def _post(self, method: str, **kwargs: Any) -> None:
        try:
            response = await self._client.post(method, **kwargs)
        except httpx2.HTTPError:
            raise TelegramApiError(code="TELEGRAM_NETWORK_ERROR") from None
        try:
            body = response.json()
        except json.JSONDecodeError, ValueError:
            raise TelegramApiError(
                code="TELEGRAM_INVALID_RESPONSE", status_code=response.status_code
            ) from None
        if not isinstance(body, dict) or body.get("ok") is not True or not response.is_success:
            error_code = body.get("error_code") if isinstance(body, dict) else None
            parameters = body.get("parameters") if isinstance(body, dict) else None
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            safe_retry_after = (
                retry_after
                if isinstance(retry_after, int)
                and not isinstance(retry_after, bool)
                and retry_after > 0
                else None
            )
            code = (
                f"TELEGRAM_HTTP_{error_code}"
                if isinstance(error_code, int) and not isinstance(error_code, bool)
                else "TELEGRAM_API_REJECTED"
            )
            raise TelegramApiError(
                code=code,
                status_code=response.status_code,
                retry_after=safe_retry_after,
            )


def _validate_chat_id(chat_id: int) -> None:
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
        raise ValueError("Telegram chat_id phải là số nguyên dương.")


def _validate_message(chat_id: int, text: str) -> None:
    _validate_chat_id(chat_id)
    if not isinstance(text, str) or not 1 <= len(text) <= _MAX_MESSAGE_CHARS:
        raise ValueError("Tin nhắn Telegram phải có từ 1 đến 4096 ký tự.")
