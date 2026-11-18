"""Adapter Telegram Bot API dùng HTTP có giới hạn, hỗ trợ xoay token và không lộ secret."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2

from trading_bot.adapters.secrets.telegram_token import (
    DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE,
    validate_telegram_bot_token,
)
from trading_bot.adapters.telegram.errors import (
    TelegramApiError,
    TelegramSensitivityBlockedError,
)
from trading_bot.adapters.telegram.outbound_guard import GroupSensitivityGuard
from trading_bot.ports.secrets import SecretAccessError, SecretLease, TelegramBotSecretProvider

logger = logging.getLogger(__name__)
_BOT_URL_TOKEN_RE = re.compile(r"(https?://api\.telegram\.org/)bot[^/\s?#]+")


class _TelegramUrlRedactingFilter(logging.Filter):
    """Che token trong URL ``api.telegram.org/bot<token>/`` trước khi logger httpx2 phát log."""

    @staticmethod
    def _redact_text(text: str) -> str:
        return _BOT_URL_TOKEN_RE.sub(r"\1bot[REDACTED]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "api.telegram.org/bot" in record.msg:
            record.msg = self._redact_text(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                redacted: list[Any] = []
                changed = False
                for arg in record.args:
                    text = str(arg)
                    if "api.telegram.org/bot" in text:
                        redacted.append(self._redact_text(text))
                        changed = True
                    else:
                        redacted.append(arg)
                if changed:
                    record.args = tuple(redacted)
            elif isinstance(record.args, dict):
                for key, arg in list(record.args.items()):
                    text = str(arg)
                    if "api.telegram.org/bot" in text:
                        record.args[key] = self._redact_text(text)
        return True


def _install_http_log_redactor() -> None:
    http_logger = logging.getLogger("httpx2")
    if not any(isinstance(item, _TelegramUrlRedactingFilter) for item in http_logger.filters):
        http_logger.addFilter(_TelegramUrlRedactingFilter())


_install_http_log_redactor()

_MAX_MESSAGE_CHARS = 4096
_MAX_CAPTION_CHARS = 1024
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_TOPIC_NAME_CHARS = 128
# message_thread_id là số nguyên dương; chặn trên để không gửi số quá cỡ BIGINT/JSON an toàn.
_MAX_THREAD_ID = 2**53 - 1
_MIN_CHAT_ID = -(2**63)
_MAX_CHAT_ID = 2**63 - 1


def load_telegram_bot_token(path: Path) -> str:
    """Đọc token từ secret file và loại khoảng trắng thừa quanh giá trị."""
    token = path.read_text(encoding="utf-8")
    try:
        return validate_telegram_bot_token(token)
    except ValueError as error:
        raise ValueError("Tệp token Telegram không có giá trị hợp lệ.") from error


class TelegramBotApiSender:
    """Gửi tin nhắn văn bản, ảnh thẻ PNG, xác nhận liên kết và callback qua Bot API HTTPS."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        sensitivity_guard: GroupSensitivityGuard | None = None,
        secret_provider: TelegramBotSecretProvider | None = None,
        secret_reference: str = DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE,
        workspace_id: UUID | None = None,
        lease_ttl_seconds: int | None = None,
        clock: Callable[[], float] | None = None,
        refresh_on_unauthorized: bool = True,
        min_unauthorized_refresh_interval_seconds: float = 5.0,
    ) -> None:
        clean_token = validate_telegram_bot_token(token)
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds phải trong khoảng 1–60 giây.")
        if min_unauthorized_refresh_interval_seconds < 0:
            raise ValueError("min_unauthorized_refresh_interval_seconds không được âm.")
        _install_http_log_redactor()
        self._token = clean_token
        self._sensitivity_guard = (
            sensitivity_guard if sensitivity_guard is not None else GroupSensitivityGuard()
        )
        self._secret_provider = secret_provider
        self._secret_reference = secret_reference
        self._workspace_id = workspace_id
        self._clock = clock or time.monotonic
        self._refresh_on_unauthorized = refresh_on_unauthorized
        self._min_unauthorized_refresh_interval = min_unauthorized_refresh_interval_seconds
        self._last_forced_refresh_at = float("-inf")
        self._lease_expires_at: float | None = (
            self._clock() + lease_ttl_seconds
            if secret_provider is not None and lease_ttl_seconds is not None
            else None
        )
        self._client = httpx2.AsyncClient(
            base_url=self._bot_base_url(clean_token),
            transport=transport,
            timeout=httpx2.Timeout(timeout_seconds, connect=min(timeout_seconds, 3.0)),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "Crypidian/0.1"},
        )

    @classmethod
    def from_secret_provider(
        cls,
        secret_provider: TelegramBotSecretProvider,
        *,
        reference: str = DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE,
        workspace_id: UUID | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        sensitivity_guard: GroupSensitivityGuard | None = None,
        clock: Callable[[], float] | None = None,
        refresh_on_unauthorized: bool = True,
        min_unauthorized_refresh_interval_seconds: float = 5.0,
    ) -> TelegramBotApiSender:
        """Khởi tạo sender từ SecretProvider (fail-closed nếu chưa có token hợp lệ)."""
        lease = cls._resolve_valid_lease(secret_provider, reference, workspace_id)
        return cls(
            lease.value,
            transport=transport,
            timeout_seconds=timeout_seconds,
            sensitivity_guard=sensitivity_guard,
            secret_provider=secret_provider,
            secret_reference=reference,
            workspace_id=workspace_id,
            lease_ttl_seconds=max(1, lease.expires_in_seconds),
            clock=clock,
            refresh_on_unauthorized=refresh_on_unauthorized,
            min_unauthorized_refresh_interval_seconds=min_unauthorized_refresh_interval_seconds,
        )

    def __repr__(self) -> str:
        return f"TelegramBotApiSender(reference={self._secret_reference!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TelegramBotApiSender:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def refresh_token(self, *, force: bool = False) -> bool:
        """Làm mới token từ SecretProvider nếu có; trả True khi token thực sự thay đổi."""
        if self._secret_provider is None:
            return False
        now = self._clock()
        if not force and self._lease_expires_at is not None and now < self._lease_expires_at:
            return False
        lease = self._resolve_valid_lease(
            self._secret_provider, self._secret_reference, self._workspace_id
        )
        self._lease_expires_at = now + max(1, lease.expires_in_seconds)
        if force:
            self._last_forced_refresh_at = now
        if lease.value == self._token:
            return False
        self._token = lease.value
        self._client.base_url = self._bot_base_url(lease.value)
        logger.info("Đã xoay vòng token Telegram từ SecretProvider.")
        return True

    async def send_message(
        self, chat_id: int, text: str, *, message_thread_id: int | None = None
    ) -> None:
        """Gửi văn bản thuần; ``message_thread_id`` đưa tin vào một topic (TG-4).

        Theo https://core.telegram.org/bots/api#sendmessage (kiểm 17/11/2026), tham số này dùng
        cho supergroup forum và chat riêng của bot đã bật chế độ topic (Bot API 9.3+).
        """
        _validate_message(chat_id, text)
        safe_text = self._sensitivity_guard.inspect_text(chat_id, text)
        payload: dict[str, Any] = {"chat_id": chat_id, "text": safe_text}
        if message_thread_id is not None:
            payload["message_thread_id"] = validate_thread_id(message_thread_id)
        await self._request("sendMessage", payload)

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        """Tạo topic trong chat riêng với người dùng và trả ``message_thread_id`` (TG-4).

        https://core.telegram.org/bots/api#createforumtopic (kiểm 17/11/2026): tên 1–128 ký tự,
        trả về ``ForumTopic``. Chat riêng phải bật chế độ topic cho bot trong @BotFather.
        """
        _validate_chat_id(chat_id)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= _MAX_TOPIC_NAME_CHARS:
            raise ValueError("Tên topic Telegram phải có từ 1 đến 128 ký tự.")
        result = await self._post("createForumTopic", json={"chat_id": chat_id, "name": name})
        thread_id = result.get("message_thread_id") if isinstance(result, dict) else None
        if (
            not isinstance(thread_id, int)
            or isinstance(thread_id, bool)
            or not 0 < thread_id <= _MAX_THREAD_ID
        ):
            raise TelegramApiError(code="TELEGRAM_INVALID_RESPONSE")
        return thread_id

    async def send_link_confirmation(self, chat_id: int, text: str, callback_data: str) -> None:
        # SEC-37: xác nhận liên kết chỉ cho phép gửi trong chat riêng (chat_id dương).
        _validate_message(chat_id, text)
        if chat_id <= 0:
            raise ValueError("Xác nhận liên kết Telegram chỉ hỗ trợ chat riêng.")
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

    async def send_photo(
        self,
        chat_id: int,
        png_bytes: bytes,
        caption: str,
        *,
        message_thread_id: int | None = None,
    ) -> None:
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
        safe_caption = self._sensitivity_guard.inspect_photo_caption(chat_id, caption)
        data = {"chat_id": str(chat_id)}
        if message_thread_id is not None:
            data["message_thread_id"] = str(validate_thread_id(message_thread_id))
        if safe_caption:
            data["caption"] = safe_caption
        await self._post(
            "sendPhoto",
            data=data,
            files={"photo": ("card.png", bytes(png_bytes), "image/png")},
        )

    async def _request(self, method: str, payload: dict[str, Any]) -> None:
        await self._post(method, json=payload)

    async def _post(self, method: str, **kwargs: Any) -> Any:
        """Gọi Bot API; tự làm mới token khi lease hết hạn hoặc gặp 401 sau xoay vòng."""
        self._ensure_fresh_token()
        try:
            return await self._post_once(method, **kwargs)
        except TelegramApiError as error:
            if (
                error.status_code == 401
                and self._can_force_refresh_on_unauthorized()
                and self._try_rotate_on_unauthorized()
            ):
                return await self._post_once(method, **kwargs)
            raise

    async def _post_once(self, method: str, **kwargs: Any) -> Any:
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
        return body.get("result")

    def _ensure_fresh_token(self) -> None:
        if self._secret_provider is None:
            return
        try:
            self.refresh_token(force=False)
        except SecretAccessError:
            raise TelegramApiError(code="TELEGRAM_SECRET_UNAVAILABLE") from None

    def _can_force_refresh_on_unauthorized(self) -> bool:
        if self._secret_provider is None or not self._refresh_on_unauthorized:
            return False
        return (
            self._clock() - self._last_forced_refresh_at
        ) >= self._min_unauthorized_refresh_interval

    def _try_rotate_on_unauthorized(self) -> bool:
        try:
            return self.refresh_token(force=True)
        except SecretAccessError:
            return False

    @staticmethod
    def _bot_base_url(token: str) -> str:
        return f"https://api.telegram.org/bot{token}/"

    @staticmethod
    def _resolve_valid_lease(
        secret_provider: TelegramBotSecretProvider,
        reference: str,
        workspace_id: UUID | None,
    ) -> SecretLease:
        lease = secret_provider.resolve_telegram_bot_token(reference, workspace_id=workspace_id)
        validate_telegram_bot_token(lease.value)
        return lease


def validate_thread_id(thread_id: int) -> int:
    """``message_thread_id`` hợp lệ: số nguyên dương (không bool), trong giới hạn an toàn."""
    if (
        not isinstance(thread_id, int)
        or isinstance(thread_id, bool)
        or not 0 < thread_id <= _MAX_THREAD_ID
    ):
        raise ValueError("Telegram message_thread_id phải là số nguyên dương.")
    return thread_id


def _validate_chat_id(chat_id: int) -> None:
    if (
        not isinstance(chat_id, int)
        or isinstance(chat_id, bool)
        or chat_id == 0
        or not _MIN_CHAT_ID <= chat_id <= _MAX_CHAT_ID
    ):
        raise ValueError("Telegram chat_id phải là số nguyên khác 0.")


def _validate_message(chat_id: int, text: str) -> None:
    _validate_chat_id(chat_id)
    if not isinstance(text, str) or not 1 <= len(text) <= _MAX_MESSAGE_CHARS:
        raise ValueError("Tin nhắn Telegram phải có từ 1 đến 4096 ký tự.")


__all__ = [
    "TelegramApiError",
    "TelegramBotApiSender",
    "TelegramSensitivityBlockedError",
    "load_telegram_bot_token",
    "validate_thread_id",
]
