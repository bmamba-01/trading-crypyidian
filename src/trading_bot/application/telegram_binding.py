"""Điều phối liên kết và thu hồi Telegram với mã chỉ phát một lần."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from trading_bot.auth.policy import Principal, Role

LINK_TTL = timedelta(minutes=10)
RECENT_MFA_TTL = timedelta(minutes=15)
_BOT_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")


@dataclass(frozen=True, slots=True)
class TelegramLink:
    """Mã và deep link chỉ trả về trong response tạo mới."""

    code: str
    url: str
    expires_at: datetime


class TelegramBindingRepository(Protocol):
    async def create_link_request(
        self,
        workspace_id: UUID,
        membership_id: UUID,
        created_by: UUID,
        digest: str,
        expires_at: datetime,
        reason: str,
    ) -> None: ...

    async def revoke(
        self, workspace_id: UUID, membership_id: UUID, actor_membership_id: UUID, reason: str
    ) -> int: ...

    async def list_bindings(self, workspace_id: UUID) -> list[dict[str, str]]: ...


class TelegramBindingService:
    """Áp quyền/MFA và không chuyển mã thô xuống repository hoặc audit."""

    def __init__(
        self,
        repository: TelegramBindingRepository,
        *,
        bot_username: str,
        now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not _BOT_USERNAME.fullmatch(bot_username):
            raise ValueError("Tên bot Telegram cấu hình không hợp lệ.")
        self._repository = repository
        self._bot_username = bot_username
        self._now = now or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    async def create_link(self, principal: Principal, *, reason: str) -> TelegramLink:
        self._require_owner(principal)
        self._require_recent_mfa(principal)
        clean_reason = self._reason(reason)
        code = self._token_factory()
        if not 43 <= len(code) <= 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
            raise ValueError("Bộ sinh mã Telegram không tạo mã an toàn.")
        expires_at = self._now() + LINK_TTL
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        await self._repository.create_link_request(
            principal.workspace_id,
            principal.membership_id,
            principal.membership_id,
            digest,
            expires_at,
            clean_reason,
        )
        return TelegramLink(
            code=code,
            url=f"https://t.me/{self._bot_username}?start={code}",
            expires_at=expires_at,
        )

    async def revoke(self, principal: Principal, membership_id: UUID, *, reason: str) -> int:
        self._require_owner(principal)
        self._require_recent_mfa(principal)
        clean_reason = self._reason(reason)
        return await self._repository.revoke(
            principal.workspace_id, membership_id, principal.membership_id, clean_reason
        )

    async def list_bindings(self, principal: Principal) -> list[dict[str, str]]:
        self._require_owner(principal)
        return await self._repository.list_bindings(principal.workspace_id)

    @staticmethod
    def _require_owner(principal: Principal) -> None:
        if Role.OWNER not in principal.roles:
            raise ValueError("Chỉ owner được quản lý liên kết Telegram.")

    def _require_recent_mfa(self, principal: Principal) -> None:
        now = self._now()
        if principal.mfa_verified_at.tzinfo is None or principal.mfa_verified_at > now:
            raise ValueError("Thời điểm xác thực MFA không hợp lệ.")
        if now - principal.mfa_verified_at > RECENT_MFA_TTL:
            raise ValueError("Cần xác thực MFA gần đây.")

    @staticmethod
    def _reason(reason: str) -> str:
        clean = reason.strip()
        if not 1 <= len(clean) <= 500:
            raise ValueError("Cần nhập lý do từ 1 đến 500 ký tự.")
        return clean
