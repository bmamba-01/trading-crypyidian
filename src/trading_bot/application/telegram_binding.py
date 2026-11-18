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

from trading_bot.auth.action_challenge import (
    ACTION_TELEGRAM_BINDING_REVOKE,
    ACTION_TELEGRAM_LINK_CREATE,
    ActionChallengeError,
    ActionChallengeService,
    StepUpProof,
    telegram_link_payload,
    telegram_revoke_payload,
    verify_service_step_up,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.ports.telegram import TelegramTopicCreator

LINK_TTL = timedelta(minutes=10)
RECENT_MFA_TTL = timedelta(minutes=15)
_BOT_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")
# TG-4: tên hai topic trong chat riêng của bot (owner chọn 17/11/2026).
SIGNAL_TOPIC_NAME = "Tín hiệu"
REPORT_TOPIC_NAME = "PAPER report"
_SAFE_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


class TelegramTopicSetupError(RuntimeError):
    """Telegram từ chối/không tạo được topic; chỉ giữ mã an toàn, không body/token."""

    def __init__(self, code: str) -> None:
        self.code = code if _SAFE_ERROR_CODE.fullmatch(code) else "TELEGRAM_TOPIC_CREATE_FAILED"
        super().__init__(self.code)


class TelegramStepUpError(ValueError):
    """Lỗi step-up SEC-20 khi tạo/thu hồi liên kết Telegram."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TelegramTopics:
    signal_thread_id: int
    report_thread_id: int


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

    async def binding_chat(self, workspace_id: UUID, membership_id: UUID) -> int | None:
        """Chat riêng của binding đã xác minh, chưa thu hồi; None nếu không có."""
        ...

    async def set_topics(
        self,
        workspace_id: UUID,
        membership_id: UUID,
        actor_membership_id: UUID,
        *,
        signal_thread_id: int | None,
        report_thread_id: int | None,
    ) -> bool: ...


class TelegramBindingService:
    """Áp quyền/MFA và không chuyển mã thô xuống repository hoặc audit."""

    def __init__(
        self,
        repository: TelegramBindingRepository,
        *,
        bot_username: str,
        now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        topic_creator: Callable[[], TelegramTopicCreator | None] | None = None,
        action_challenge_service: ActionChallengeService | None = None,
        require_action_step_up: bool = False,
    ) -> None:
        if not _BOT_USERNAME.fullmatch(bot_username):
            raise ValueError("Tên bot Telegram cấu hình không hợp lệ.")
        self._repository = repository
        self._bot_username = bot_username
        self._now = now or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        # Sender được dựng sau service trong lifespan nên truyền dạng hàm lấy trễ.
        self._topic_creator = topic_creator or (lambda: None)
        self._action_challenge_service = action_challenge_service
        self._require_action_step_up = require_action_step_up

    async def _verify_step_up(
        self,
        principal: Principal,
        *,
        action: str,
        payload: dict[str, object],
        step_up: StepUpProof | None = None,
    ) -> None:
        try:
            await verify_service_step_up(
                principal,
                action_challenge_service=self._action_challenge_service,
                require_action_step_up=self._require_action_step_up,
                action=action,
                payload=payload,
                step_up=step_up,
                fallback_recent_mfa=lambda: self._require_recent_mfa(principal),
            )
        except ActionChallengeError as error:
            raise TelegramStepUpError(error.status_code, error.code, error.message) from error

    async def create_link(
        self, principal: Principal, *, reason: str, step_up: StepUpProof | None = None
    ) -> TelegramLink:
        self._require_owner(principal)
        clean_reason = self._reason(reason)
        await self._verify_step_up(
            principal,
            action=ACTION_TELEGRAM_LINK_CREATE,
            payload=telegram_link_payload(reason=clean_reason),
            step_up=step_up,
        )
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

    async def revoke(
        self,
        principal: Principal,
        membership_id: UUID,
        *,
        reason: str,
        step_up: StepUpProof | None = None,
    ) -> int:
        self._require_owner(principal)
        clean_reason = self._reason(reason)
        await self._verify_step_up(
            principal,
            action=ACTION_TELEGRAM_BINDING_REVOKE,
            payload=telegram_revoke_payload(membership_id=membership_id, reason=clean_reason),
            step_up=step_up,
        )
        return await self._repository.revoke(
            principal.workspace_id, membership_id, principal.membership_id, clean_reason
        )

    async def list_bindings(self, principal: Principal) -> list[dict[str, str]]:
        self._require_owner(principal)
        return await self._repository.list_bindings(principal.workspace_id)

    async def create_topics(self, principal: Principal, membership_id: UUID) -> TelegramTopics:
        """Tạo topic "Tín hiệu" và "PAPER report" trong chat riêng của binding rồi lưu id.

        Chỉ lưu khi tạo được cả hai topic; lỗi Telegram (vd. bot chưa bật chế độ topic trong
        @BotFather) → ``TelegramTopicSetupError`` và binding giữ nguyên cấu hình cũ.
        """
        self._require_owner(principal)
        self._require_recent_mfa(principal)
        chat_id = await self._repository.binding_chat(principal.workspace_id, membership_id)
        if chat_id is None:
            raise ValueError("Không có liên kết Telegram đang hoạt động cho thành viên này.")
        creator = self._topic_creator()
        if creator is None:
            raise TelegramTopicSetupError("TELEGRAM_SENDER_UNAVAILABLE")
        try:
            signal_thread_id = await creator.create_forum_topic(chat_id, SIGNAL_TOPIC_NAME)
            report_thread_id = await creator.create_forum_topic(chat_id, REPORT_TOPIC_NAME)
        except Exception as error:
            raise TelegramTopicSetupError(
                str(getattr(error, "code", "TELEGRAM_TOPIC_CREATE_FAILED"))
            ) from None
        saved = await self._repository.set_topics(
            principal.workspace_id,
            membership_id,
            principal.membership_id,
            signal_thread_id=signal_thread_id,
            report_thread_id=report_thread_id,
        )
        if not saved:
            raise ValueError("Liên kết Telegram đã bị thu hồi trong lúc tạo topic.")
        return TelegramTopics(signal_thread_id, report_thread_id)

    async def clear_topics(self, principal: Principal, membership_id: UUID) -> bool:
        """Bỏ cấu hình topic: tin quay về chat chính (không xóa topic trên Telegram)."""
        self._require_owner(principal)
        self._require_recent_mfa(principal)
        return await self._repository.set_topics(
            principal.workspace_id,
            membership_id,
            principal.membership_id,
            signal_thread_id=None,
            report_thread_id=None,
        )

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
