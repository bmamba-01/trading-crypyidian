"""Webhook Telegram cho lệnh điều khiển bot (MB-07a, ruling 37–39, 41; SEC-35/36/37/38).

Thứ tự kiểm (ruling 39): chưa cấu hình → 404; thiếu/sai `X-Telegram-Bot-Api-Secret-Token` (so
sánh hằng thời gian) → 401; body quá lớn → 413; JSON/`update_id` sai → 400 — các bước này không
chạm DB. Sau đó claim `update_id` bền vững (trùng → `duplicate`), chỉ nhận `message` văn bản bắt
đầu bằng `/` trong private chat có `chat.id == from.id`; update giả mạo bị từ chối + audit; binding
→ principal (không MFA) → throttle → `BotCommandHandler` → audit → trả lời qua port gửi tin.

Phản hồi HTTP cho Telegram chỉ có `{"ok": true, "status": …}`. Không log/audit text tin nhắn,
secret hay Telegram user id.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.telegram import (
    TelegramBindingIdentity,
    TelegramBindingRepository,
    TelegramUpdateReceiptRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.ports.telegram import TelegramMessageSender
from trading_bot.telegram.bot_commands import (
    BotCommandHandler,
    BotCommandParseError,
    parse_bot_command,
    reject,
    telegram_principal,
)

__all__ = [
    "SECRET_HEADER",
    "DatabaseTelegramStore",
    "TelegramBindingIdentity",
    "TelegramCommandThrottle",
    "TelegramWebhookService",
    "load_webhook_secret",
    "router",
    "update_request_id",
]

logger = logging.getLogger(__name__)
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_BODY_BYTES = 64 * 1024
# Cột `update_id` là BIGINT có dấu (fix round 1 minor 5).
MAX_UPDATE_ID = 2**63 - 1
UNAVAILABLE = "RUNTIME_CONTROL_UNAVAILABLE"
_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_FORGED_FIELDS = {
    "sender_chat": "TELEGRAM_SENDER_CHAT",
    "via_bot": "TELEGRAM_VIA_BOT",
    "forward_origin": "TELEGRAM_FORWARDED",
    "guest_bot_caller_user": "TELEGRAM_GUEST_BOT",
}

router = APIRouter(prefix="/api/v1/telegram", tags=["telegram"])


def load_webhook_secret(path: Path) -> str:
    """Đọc secret webhook từ file; 32–256 ký tự `A-Za-z0-9_-` (Telegram cho phép 1–256)."""
    value = path.read_text(encoding="utf-8").strip()
    _require_valid_secret(value)
    return value


def update_request_id(workspace_id: UUID, update_id: int) -> UUID:
    """Correlation ổn định cho một update (audit), không chứa dữ liệu người dùng."""
    return uuid5(NAMESPACE_URL, f"crypidian:telegram:{workspace_id}:{update_id}")


class TelegramStore(Protocol):
    async def claim_update(self, workspace_id: UUID, update_id: int) -> bool: ...

    async def resolve_binding(
        self, workspace_id: UUID, telegram_user_id: str
    ) -> TelegramBindingIdentity | None: ...

    async def record_audit(self, workspace_id: UUID, **values: Any) -> None: ...


class DatabaseTelegramStore:
    """Mỗi thao tác một transaction ngắn dưới role admin-web (RLS theo workspace webhook)."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim_update(self, workspace_id: UUID, update_id: int) -> bool:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            return await TelegramUpdateReceiptRepository(uow.session).claim(workspace_id, update_id)

    async def resolve_binding(
        self, workspace_id: UUID, telegram_user_id: str
    ) -> TelegramBindingIdentity | None:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            return await TelegramBindingRepository(uow.session).resolve(
                workspace_id, telegram_user_id
            )

    async def record_audit(self, workspace_id: UUID, **values: Any) -> None:
        async with UnitOfWork(
            self._sessions,
            workspace_id=workspace_id,
            actor_membership_id=values["actor_membership_id"],
            request_id=values["request_id"],
        ) as uow:
            uow.record_audit(
                action=values["action"],
                target_type=values["target_type"],
                target_id=values.get("target_id"),
                reason=values.get("reason"),
            )


class TelegramCommandThrottle:
    """Cửa sổ trượt trong bộ nhớ theo user Telegram (chống flood, SEC-36)."""

    def __init__(
        self,
        limit: int = 20,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


class TelegramWebhookService:
    """Xử lý một update đã qua secret; trả mã trạng thái ngắn cho phản hồi HTTP."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        secret: str,
        store: TelegramStore,
        handler: BotCommandHandler,
        sender: TelegramMessageSender,
        throttle: TelegramCommandThrottle | None = None,
        unbound_sampler: TelegramCommandThrottle | None = None,
    ) -> None:
        # Fix round 1 minor (4): không chạy với secret rỗng/yếu (header thiếu = b"" sẽ khớp).
        _require_valid_secret(secret)
        self._workspace_id = workspace_id
        self._secret = secret.encode("utf-8")
        self._store = store
        self._handler = handler
        self._sender = sender
        # Throttle theo Telegram sender ID, áp TRƯỚC mọi việc DB/audit (fix round 1 minor 2).
        self._throttle = throttle or TelegramCommandThrottle()
        # Từ chối "chưa liên kết": audit + trả lời tối đa 1 lần/10 phút/sender (gộp mẫu).
        self._unbound_sampler = unbound_sampler or TelegramCommandThrottle(
            limit=1, window_seconds=600
        )

    def verify_secret(self, header: str | None) -> bool:
        """So sánh hằng thời gian trên bytes; thiếu header là sai."""
        supplied = (header or "").encode("utf-8")
        return hmac.compare_digest(supplied, self._secret)

    async def process(self, update: Mapping[str, Any]) -> str:
        update_id = int(update["update_id"])
        sender_key = _sender_key(update)
        if sender_key is not None and not self._throttle.allow(sender_key):
            return "throttled"
        if not await self._store.claim_update(self._workspace_id, update_id):
            return "duplicate"
        request_id = update_request_id(self._workspace_id, update_id)
        try:
            return await self._process_claimed(update, request_id)
        except Exception as error:
            # Update đã claim nên Telegram không giao lại (ruling 38): không để lệnh biến mất
            # lặng lẽ — audit + trả lời người dùng, trả 200. Chỉ log tên lớp lỗi, không SQL.
            logger.warning(
                "Telegram update lỗi sau claim; code=%s error=%s",
                UNAVAILABLE,
                type(error).__name__,
            )
            await self._safe_audit(request_id, "telegram.command.rejected", UNAVAILABLE)
            message = update.get("message")
            if isinstance(message, Mapping) and _forgery_code(message) is None:
                await self._safe_send(int(message["from"]["id"]), reject(UNAVAILABLE).message)
            return "unavailable"

    async def _process_claimed(self, update: Mapping[str, Any], request_id: UUID) -> str:
        message = update.get("message")
        if not isinstance(message, Mapping):
            # edited_message, callback_query, channel_post… : đã claim, bỏ qua (ruling 38).
            return "ignored"
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return "ignored"
        forged = _forgery_code(message)
        if forged is not None:
            await self._audit(request_id, "telegram.update.rejected", None, None, forged)
            logger.info("Telegram update bị từ chối; code=%s", forged)
            return "rejected"
        sender_id = int(message["from"]["id"])
        identity = await self._store.resolve_binding(self._workspace_id, str(sender_id))
        if identity is None:
            if self._unbound_sampler.allow(str(sender_id)):
                await self._audit(
                    request_id, "telegram.update.rejected", None, None, "TELEGRAM_NOT_BOUND"
                )
                await self._sender.send_message(
                    sender_id, "Tài khoản Telegram này chưa được liên kết với workspace."
                )
            return "unbound"
        principal = telegram_principal(
            user_id=identity.user_id,
            membership_id=identity.membership_id,
            workspace_id=identity.workspace_id,
            roles=identity.roles,
        )
        try:
            parsed = parse_bot_command(text)
        except BotCommandParseError as error:
            result = reject(error.code)
            reason = error.code
        else:
            result = await self._handler.handle_bot_command(
                principal, parsed.command, parsed.bot_ref
            )
            reason = f"{parsed.command.value}:{result.code}"
        await self._audit(
            request_id,
            "telegram.command.accepted" if result.ok else "telegram.command.rejected",
            identity.membership_id,
            result.target_bot_id,
            reason,
        )
        logger.info("Telegram command đã xử lý; ok=%s code=%s", result.ok, result.code)
        await self._sender.send_message(sender_id, result.message)
        return "processed"

    async def _safe_audit(self, request_id: UUID, action: str, reason: str) -> None:
        try:
            await self._audit(request_id, action, None, None, reason)
        except Exception as error:
            logger.warning("Không ghi được audit Telegram; error=%s", type(error).__name__)

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self._sender.send_message(chat_id, text)
        except Exception as error:
            logger.warning("Không gửi được trả lời Telegram; error=%s", type(error).__name__)

    async def _audit(
        self,
        request_id: UUID,
        action: str,
        actor: UUID | None,
        target: UUID | None,
        reason: str,
    ) -> None:
        await self._store.record_audit(
            self._workspace_id,
            action=action,
            target_type="bot_instance" if target is not None else "telegram_update",
            target_id=target,
            reason=reason,
            actor_membership_id=actor,
            request_id=request_id,
        )


def _require_valid_secret(secret: str) -> None:
    if not isinstance(secret, str) or not _SECRET_PATTERN.fullmatch(secret):
        raise ValueError("Secret webhook Telegram phải gồm 32–256 ký tự [A-Za-z0-9_-].")


def _sender_key(update: Mapping[str, Any]) -> str | None:
    """Telegram sender ID của update (message/edited_message/callback…), không cần DB."""
    for value in update.values():
        if isinstance(value, Mapping):
            sender = value.get("from")
            if isinstance(sender, Mapping):
                sender_id = sender.get("id")
                if isinstance(sender_id, int) and not isinstance(sender_id, bool):
                    return str(sender_id)
    return None


def _forgery_code(message: Mapping[str, Any]) -> str | None:
    """SEC-37: chỉ private chat của chính người gửi, không bot/kênh/forward/inline/guest bot."""
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
        return "TELEGRAM_UPDATE_MALFORMED"
    if chat.get("type") != "private":
        return "TELEGRAM_NOT_PRIVATE_CHAT"
    sender_id, chat_id = sender.get("id"), chat.get("id")
    if (
        not isinstance(sender_id, int)
        or isinstance(sender_id, bool)
        or sender_id <= 0
        or chat_id != sender_id
    ):
        return "TELEGRAM_CHAT_MISMATCH"
    if sender.get("is_bot") is not False:
        return "TELEGRAM_FROM_BOT"
    for field, code in _FORGED_FIELDS.items():
        if message.get(field) is not None:
            return code
    return None


def _status(status_code: int, status: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": status_code == 200, "status": status},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/webhook", include_in_schema=False)
async def telegram_webhook(request: Request) -> JSONResponse:
    service = cast(
        TelegramWebhookService | None, getattr(request.app.state, "telegram_webhook", None)
    )
    if service is None:
        return _status(404, "not_configured")
    if not service.verify_secret(request.headers.get(SECRET_HEADER)):
        return _status(401, "unauthorized")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        return _status(413, "too_large")
    # Đọc body theo luồng có trần: chunked không có Content-Length (fix round 1 minor 5).
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return _status(413, "too_large")
    try:
        update = json.loads(bytes(body))
    except ValueError:
        return _status(400, "invalid")
    update_id = update.get("update_id") if isinstance(update, dict) else None
    if (
        not isinstance(update_id, int)
        or isinstance(update_id, bool)
        or not 0 < update_id <= MAX_UPDATE_ID
    ):
        return _status(400, "invalid")
    return _status(200, await service.process(update))
