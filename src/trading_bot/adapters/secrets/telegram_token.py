"""Adapter SecretProvider cho token Telegram Bot API từ secret file và bộ ghép provider."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from trading_bot.ports.secrets import (
    SecretAccessError,
    SecretLease,
    SecretProvider,
    TelegramBotSecretProvider,
)

DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE = "telegram-bot-token"
DEFAULT_TELEGRAM_TOKEN_LEASE_TTL_SECONDS = 300
_TOKEN_PATTERN = re.compile(r"^[^\s/?#]{8,512}$")


def validate_telegram_bot_token(raw_token: str) -> str:
    """Chuẩn hóa và kiểm định định dạng token Telegram; không lộ giá trị vào thông báo lỗi."""
    if not isinstance(raw_token, str):
        raise ValueError("Token Telegram không hợp lệ.")
    token = raw_token.strip()
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Token Telegram không hợp lệ.")
    return token


class FileTelegramBotSecretProvider:
    """Đọc token Telegram từ file secret (tương thích Docker secrets/local dev) theo lease."""

    def __init__(
        self,
        *,
        token_file: Path | None = None,
        references: Mapping[str, Path] | None = None,
        lease_ttl_seconds: int = DEFAULT_TELEGRAM_TOKEN_LEASE_TTL_SECONDS,
    ) -> None:
        if lease_ttl_seconds < 1 or lease_ttl_seconds > 86_400:
            raise ValueError("lease_ttl_seconds phải trong khoảng 1–86400 giây.")
        mapping: dict[str, Path] = {}
        if token_file is not None:
            mapping[DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE] = token_file
        if references is not None:
            for ref, path in references.items():
                clean_ref = ref.strip()
                if not clean_ref:
                    raise ValueError("Tham chiếu token Telegram không được rỗng.")
                mapping[clean_ref] = path
        if not mapping:
            raise ValueError("Cần ít nhất một secret file cho token Telegram.")
        self._references = mapping
        self._lease_ttl_seconds = lease_ttl_seconds

    def store_mfa_secret(self, workspace_id: UUID, value: str) -> str:
        del workspace_id, value
        raise SecretAccessError("Adapter Telegram token không hỗ trợ lưu bí mật MFA.")

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str:
        del workspace_id, reference
        raise SecretAccessError("Adapter Telegram token không hỗ trợ đọc bí mật MFA.")

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None:
        del workspace_id, reference
        raise SecretAccessError("Adapter Telegram token không hỗ trợ xóa bí mật MFA.")

    def resolve_for_gateway(self, workspace_id: UUID, reference: str) -> SecretLease:
        del workspace_id, reference
        raise SecretAccessError("Adapter Telegram token không cấp secret giao dịch cho gateway.")

    def resolve_telegram_bot_token(
        self, reference: str, *, workspace_id: UUID | None = None
    ) -> SecretLease:
        """Đọc lại token từ file mỗi lần resolve để hỗ trợ xoay token không cần sửa code."""
        path = self._references.get(reference)
        if path is None:
            raise SecretAccessError("Tham chiếu token Telegram không hợp lệ.")
        try:
            raw_value = path.read_text(encoding="utf-8")
            token = validate_telegram_bot_token(raw_value)
        except (OSError, ValueError) as error:
            raise SecretAccessError("Không thể đọc token Telegram từ tệp bí mật.") from error
        return SecretLease(
            reference=reference,
            workspace_id=workspace_id or UUID(int=0),
            expires_in_seconds=self._lease_ttl_seconds,
            value=token,
        )


class CompositeSecretProvider:
    """Ghép provider MFA local và provider token Telegram theo đúng tách biệt nhiệm vụ."""

    def __init__(
        self,
        *,
        mfa_provider: SecretProvider | None = None,
        telegram_provider: TelegramBotSecretProvider | None = None,
    ) -> None:
        self._mfa_provider = mfa_provider
        self._telegram_provider = telegram_provider

    def store_mfa_secret(self, workspace_id: UUID, value: str) -> str:
        if self._mfa_provider is None:
            raise SecretAccessError("Chưa cấu hình SecretProvider cho MFA.")
        return self._mfa_provider.store_mfa_secret(workspace_id, value)

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str:
        if self._mfa_provider is None:
            raise SecretAccessError("Chưa cấu hình SecretProvider cho MFA.")
        return self._mfa_provider.resolve_mfa_secret(workspace_id, reference)

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None:
        if self._mfa_provider is None:
            raise SecretAccessError("Chưa cấu hình SecretProvider cho MFA.")
        self._mfa_provider.delete_mfa_secret(workspace_id, reference)

    def resolve_for_gateway(self, workspace_id: UUID, reference: str) -> SecretLease:
        if self._mfa_provider is None:
            raise SecretAccessError("Chưa cấu hình SecretProvider cho gateway.")
        return self._mfa_provider.resolve_for_gateway(workspace_id, reference)

    def resolve_telegram_bot_token(
        self, reference: str, *, workspace_id: UUID | None = None
    ) -> SecretLease:
        if self._telegram_provider is None:
            raise SecretAccessError("Chưa cấu hình SecretProvider cho token Telegram.")
        return self._telegram_provider.resolve_telegram_bot_token(
            reference, workspace_id=workspace_id
        )


__all__ = [
    "CompositeSecretProvider",
    "DEFAULT_TELEGRAM_BOT_TOKEN_REFERENCE",
    "DEFAULT_TELEGRAM_TOKEN_LEASE_TTL_SECONDS",
    "FileTelegramBotSecretProvider",
    "validate_telegram_bot_token",
]
