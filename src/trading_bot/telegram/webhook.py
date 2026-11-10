"""Webhook Telegram cho lệnh điều khiển bot (MB-07a, ruling 37–39, 41; SEC-35/36/37/38).

Thứ tự kiểm (ruling 39): chưa cấu hình → 404; thiếu/sai `X-Telegram-Bot-Api-Secret-Token` (so
sánh hằng thời gian) → 401; body quá lớn → 413; JSON/`update_id` sai → 400 — các bước này không
chạm DB. Throttle theo Telegram sender ID (pause có ngân sách riêng) chạy trước claim; bị throttle
thì trả lời + audit gộp mẫu. Sau đó claim `update_id` bền vững (trùng → `duplicate`), chỉ nhận
`message` văn bản bắt đầu bằng `/` trong private chat có `chat.id == from.id`; update giả mạo bị từ
chối + audit; binding → principal (không MFA) → `BotCommandHandler` → audit → trả lời qua port gửi
tin. Lỗi sau claim được catch-all phân loại (tạm thời / lỗi hệ thống) và không báo sai lệnh đã chạy.

Phản hồi HTTP cho Telegram chỉ có `{"ok": true, "status": …}`. Không log/audit text tin nhắn,
secret hay Telegram user id.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.errors import database_sqlstate, is_transient_database_error
from trading_bot.adapters.database.repositories.telegram import (
    TelegramBindingIdentity,
    TelegramBindingRepository,
    TelegramUpdateReceiptRepository,
)
from trading_bot.adapters.database.repositories.telegram_signals import TelegramSignalRepository
from trading_bot.application.outbox import UnitOfWork
from trading_bot.ports.telegram import TelegramMessageSender
from trading_bot.telegram.bot_commands import (
    BotCommand,
    BotCommandHandler,
    BotCommandParseError,
    BotCommandResult,
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
# Lỗi không tạm thời sau claim (bug/không xác định): mã riêng, không bảo người dùng thử lại.
COMMAND_FAILED = "TELEGRAM_COMMAND_FAILED"
THROTTLED = "TELEGRAM_THROTTLED"
# Trần số khóa (sender) mỗi bộ đếm giữ trong bộ nhớ (fix round 2).
MAX_THROTTLE_KEYS = 10_000
_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_FORGED_FIELDS = {
    "sender_chat": "TELEGRAM_SENDER_CHAT",
    "via_bot": "TELEGRAM_VIA_BOT",
    "forward_origin": "TELEGRAM_FORWARDED",
    "guest_bot_caller_user": "TELEGRAM_GUEST_BOT",
}
_LINK_START = re.compile(r"^/start(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{43,64})$")
_LINK_CALLBACK = re.compile(r"^bind:([A-Za-z0-9_-]{43,64})$")

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

    async def consume_link_code(
        self, workspace_id: UUID, code: str, telegram_user_id: str, request_id: UUID
    ) -> str | None: ...

    async def confirm_link_callback(
        self, workspace_id: UUID, nonce: str, telegram_user_id: str, request_id: UUID
    ) -> bool: ...

    async def set_signal_notifications(
        self, workspace_id: UUID, membership_id: UUID, *, enabled: bool
    ) -> int: ...


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

    async def create_link_request(
        self,
        workspace_id: UUID,
        membership_id: UUID,
        created_by: UUID,
        digest: str,
        expires_at: Any,
        reason: str,
    ) -> None:
        async with UnitOfWork(
            self._sessions, workspace_id=workspace_id, actor_membership_id=created_by
        ) as uow:
            await TelegramBindingRepository(uow.session).create_link_request(
                workspace_id, membership_id, created_by, digest, expires_at, reason
            )
            uow.record_audit(
                action="telegram.binding.link_requested",
                target_type="user_membership",
                target_id=membership_id,
                reason=reason,
            )

    async def revoke(
        self,
        workspace_id: UUID,
        membership_id: UUID,
        actor_membership_id: UUID,
        reason: str,
    ) -> int:
        async with UnitOfWork(
            self._sessions, workspace_id=workspace_id, actor_membership_id=actor_membership_id
        ) as uow:
            count = await TelegramBindingRepository(uow.session).revoke(workspace_id, membership_id)
            uow.record_audit(
                action="telegram.binding.revoked",
                target_type="user_membership",
                target_id=membership_id,
                reason=reason,
            )
            return count

    async def list_bindings(self, workspace_id: UUID) -> list[dict[str, str]]:
        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as uow:
            return await TelegramBindingRepository(uow.session).list_active(workspace_id)

    async def consume_link_code(
        self, workspace_id: UUID, code: str, telegram_user_id: str, request_id: UUID
    ) -> str | None:
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        async with UnitOfWork(
            self._sessions, workspace_id=workspace_id, request_id=request_id
        ) as uow:
            result = await TelegramBindingRepository(uow.session).create_link_challenge(
                workspace_id, digest, telegram_user_id
            )
            if result is None:
                uow.record_audit(
                    action="telegram.binding.link_rejected",
                    target_type="telegram_update",
                    reason="TELEGRAM_LINK_INVALID",
                )
                return None
            nonce, membership_id = result
            uow.record_audit(
                action="telegram.binding.challenge_created",
                target_type="user_membership",
                target_id=membership_id,
                reason="TELEGRAM_LINK_CONFIRMATION_REQUIRED",
            )
            return nonce

    async def confirm_link_callback(
        self, workspace_id: UUID, nonce: str, telegram_user_id: str, request_id: UUID
    ) -> bool:
        digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        async with UnitOfWork(
            self._sessions, workspace_id=workspace_id, request_id=request_id
        ) as uow:
            accepted, membership_id = await TelegramBindingRepository(
                uow.session
            ).confirm_link_challenge(workspace_id, digest, telegram_user_id)
            uow.record_audit(
                action=(
                    "telegram.binding.verified" if accepted else "telegram.binding.confirm_rejected"
                ),
                target_type="user_membership" if membership_id else "telegram_update",
                target_id=membership_id,
                reason=(
                    "TELEGRAM_LINK_CONFIRMED" if accepted else "TELEGRAM_LINK_CONFIRMATION_INVALID"
                ),
            )
            return accepted

    async def set_signal_notifications(
        self, workspace_id: UUID, membership_id: UUID, *, enabled: bool
    ) -> int:
        """Thay đổi sở thích alert của binding đã được webhook xác thực."""
        async with UnitOfWork(
            self._sessions, workspace_id=workspace_id, actor_membership_id=membership_id
        ) as uow:
            return await TelegramSignalRepository(uow.session).set_membership_enabled(
                workspace_id=workspace_id, membership_id=membership_id, enabled=enabled
            )


class TelegramCommandThrottle:
    """Cửa sổ trượt trong bộ nhớ theo khóa (Telegram sender ID), có giới hạn bộ nhớ (SEC-36).

    Fix round 2: khóa đến từ sender ID tùy ý nên phải bị dọn. Khóa sắp theo lần truy cập gần
    nhất; mỗi lần gọi dọn các khóa ở đầu hàng đợi đã hết cửa sổ, và nếu vẫn vượt `max_keys` thì
    loại khóa ít dùng nhất (LRU). Bị loại chỉ làm reset bộ đếm của khóa đó — chấp nhận được vì
    trần lớn hơn nhiều số người dùng thật của một workspace PAPER.
    """

    def __init__(
        self,
        limit: int = 20,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_THROTTLE_KEYS,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("Thông số throttle không hợp lệ.")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._hits)

    def allow(self, key: str) -> bool:
        now = self._clock()
        self._evict(now)
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self.max_keys:
                self._hits.popitem(last=False)
            hits = self._hits[key] = deque()
        else:
            self._hits.move_to_end(key)
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    def _evict(self, now: float) -> None:
        while self._hits:
            hits = next(iter(self._hits.values()))
            if hits and now - hits[-1] < self._window:
                return
            self._hits.popitem(last=False)


@dataclass
class _Progress:
    """Tiến độ xử lý một update đã claim, để catch-all không báo sai (fix round 2 item 4)."""

    sender_id: int | None = None
    binding_resolved: bool = False
    result: BotCommandResult | None = None
    reason: str | None = None
    actor: UUID | None = None
    audited: bool = False
    sent: bool = False


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
        pause_throttle: TelegramCommandThrottle | None = None,
        throttle_notice_sampler: TelegramCommandThrottle | None = None,
    ) -> None:
        # Fix round 1 minor (4): không chạy với secret rỗng/yếu (header thiếu = b"" sẽ khớp).
        _require_valid_secret(secret)
        self._workspace_id = workspace_id
        self._secret = secret.encode("utf-8")
        self._store = store
        self._handler = handler
        self._sender = sender
        # Throttle theo Telegram sender ID, áp TRƯỚC mọi việc DB/audit (fix round 1 minor 2).
        self._throttle = throttle if throttle is not None else TelegramCommandThrottle()
        # Từ chối "chưa liên kết": audit + trả lời tối đa 1 lần/10 phút/sender (gộp mẫu).
        self._unbound_sampler = (
            unbound_sampler
            if unbound_sampler is not None
            else TelegramCommandThrottle(limit=1, window_seconds=600)
        )
        # `/bot pause` (giảm rủi ro, idempotent theo trạng thái — SEC-22) có ngân sách riêng để
        # lệnh khác không làm cạn và chặn mất pause; vẫn có trần (fix round 2 item 2).
        self._pause_throttle = (
            pause_throttle
            if pause_throttle is not None
            else TelegramCommandThrottle(limit=10, window_seconds=60)
        )
        # Bị throttle: trả lời + audit gộp mẫu tối đa 1 lần/phút/sender, không lookup binding.
        self._throttle_notice_sampler = (
            throttle_notice_sampler
            if throttle_notice_sampler is not None
            else TelegramCommandThrottle(limit=1, window_seconds=60)
        )

    def verify_secret(self, header: str | None) -> bool:
        """So sánh hằng thời gian trên bytes; thiếu header là sai."""
        supplied = (header or "").encode("utf-8")
        return hmac.compare_digest(supplied, self._secret)

    async def process(self, update: Mapping[str, Any]) -> str:
        update_id = int(update["update_id"])
        sender_key = _sender_key(update)
        if sender_key is not None and not self._bucket(update).allow(sender_key):
            await self._notify_throttled(update, sender_key, update_id)
            return "throttled"
        if not await self._store.claim_update(self._workspace_id, update_id):
            return "duplicate"
        request_id = update_request_id(self._workspace_id, update_id)
        progress = _Progress()
        try:
            return await self._process_claimed(update, request_id, progress)
        except Exception as error:
            return await self._recover(error, request_id, progress)

    def _bucket(self, update: Mapping[str, Any]) -> TelegramCommandThrottle:
        message = update.get("message")
        text = message.get("text") if isinstance(message, Mapping) else None
        if isinstance(text, str):
            try:
                if parse_bot_command(text).command is BotCommand.PAUSE:
                    return self._pause_throttle
            except BotCommandParseError:
                pass
        return self._throttle

    async def _notify_throttled(
        self, update: Mapping[str, Any], sender_key: str, update_id: int
    ) -> None:
        """Không im lặng (fix round 2 item 2): trả lời + audit gộp mẫu, không claim/lookup DB."""
        if not self._throttle_notice_sampler.allow(sender_key):
            return
        await self._safe_audit(update_request_id(self._workspace_id, update_id), THROTTLED)
        message = update.get("message")
        if isinstance(message, Mapping) and _forgery_code(message) is None:
            await self._safe_send(
                int(message["from"]["id"]),
                "Quá nhiều lệnh; lệnh này CHƯA được thực hiện. Hãy gửi lại sau ít phút.",
            )

    async def _recover(self, error: Exception, request_id: UUID, progress: _Progress) -> str:
        """Catch-all sau claim: Telegram không giao lại update đã claim (ruling 38)."""
        transient = is_transient_database_error(error)
        code = UNAVAILABLE if transient else COMMAND_FAILED
        # ERROR có SQLSTATE + file:dòng của frame ném lỗi; không stack, không giá trị/params.
        logger.error(
            "Telegram update lỗi sau claim; code=%s sqlstate=%s at=%s",
            code,
            database_sqlstate(error),
            _raise_site(error),
        )
        if progress.result is not None:
            # Lệnh đã chạy (ví dụ pause đã áp): chỉ bù phần còn thiếu, không báo "bị từ chối".
            if not progress.audited:
                await self._safe_audit(
                    request_id,
                    progress.reason or code,
                    _command_action(progress.result),
                    actor=progress.actor,
                    target=progress.result.target_bot_id,
                )
            if not progress.sent and progress.sender_id is not None:
                await self._safe_send(progress.sender_id, progress.result.message)
            return "processed"
        await self._safe_audit(request_id, code, "telegram.command.rejected", actor=progress.actor)
        sender_id = progress.sender_id
        if sender_id is not None and (
            progress.binding_resolved or self._unbound_sampler.allow(str(sender_id))
        ):
            text = (
                reject(UNAVAILABLE).message
                if transient
                else "Lệnh không xử lý được do lỗi hệ thống; đã ghi nhận để kiểm tra."
            )
            await self._safe_send(sender_id, text)
        return "unavailable" if transient else "failed"

    async def _process_claimed(
        self, update: Mapping[str, Any], request_id: UUID, progress: _Progress
    ) -> str:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            return await self._process_link_callback(callback, request_id, progress)
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
        progress.sender_id = sender_id
        link_match = _LINK_START.fullmatch(text)
        if link_match is not None:
            nonce = await self._store.consume_link_code(
                self._workspace_id, link_match.group(1), str(sender_id), request_id
            )
            if nonce is None:
                await self._safe_send(
                    sender_id,
                    "Mã liên kết không hợp lệ hoặc đã hết hạn. Hãy tạo mã mới trong Admin Web.",
                )
                return "link_invalid"
            await self._sender.send_link_confirmation(
                sender_id,
                "Xác nhận liên kết tài khoản Telegram này với Crypidian.",
                f"bind:{nonce}",
            )
            return "link_confirmation_required"
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
        progress.binding_resolved = True
        progress.actor = identity.membership_id
        simple_command = _simple_signal_command(text)
        if simple_command is not None:
            enabled = simple_command == "start"
            changed = await self._store.set_signal_notifications(
                self._workspace_id, identity.membership_id, enabled=enabled
            )
            if enabled and changed:
                message = "Đã bật lại các đăng ký nhận tín hiệu đã chọn."
                result_code = "SIGNALS_ENABLED"
                audit_action = "telegram.signal_notifications.enabled"
                response_status = "signal_notifications_enabled"
            elif enabled:
                message = "Chưa có đăng ký tín hiệu nào. Hãy bật bot muốn theo dõi trong Admin Web."
                result_code = "SIGNALS_NOT_CONFIGURED"
                audit_action = "telegram.signal_notifications.start_without_opt_in"
                response_status = "signal_notifications_not_configured"
            else:
                message = "Đã tắt toàn bộ thông báo tín hiệu Telegram."
                result_code = "SIGNALS_DISABLED"
                audit_action = "telegram.signal_notifications.disabled"
                response_status = "signal_notifications_disabled"
            progress.result = BotCommandResult(
                True,
                result_code,
                message,
            )
            progress.reason = f"telegram.signal_notifications.{simple_command}"
            await self._audit(
                request_id,
                audit_action,
                identity.membership_id,
                None,
                progress.reason,
            )
            progress.audited = True
            await self._sender.send_message(sender_id, message)
            progress.sent = True
            return response_status
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
        progress.result, progress.reason = result, reason
        await self._audit(
            request_id,
            _command_action(result),
            identity.membership_id,
            result.target_bot_id,
            reason,
        )
        progress.audited = True
        logger.info("Telegram command đã xử lý; ok=%s code=%s", result.ok, result.code)
        await self._sender.send_message(sender_id, result.message)
        progress.sent = True
        return "processed"

    async def _process_link_callback(
        self, callback: Mapping[str, Any], request_id: UUID, progress: _Progress
    ) -> str:
        sender = callback.get("from")
        message = callback.get("message")
        data = callback.get("data")
        callback_id = callback.get("id")
        chat = message.get("chat") if isinstance(message, Mapping) else None
        sender_id = sender.get("id") if isinstance(sender, Mapping) else None
        if (
            not isinstance(sender, Mapping)
            or not isinstance(sender_id, int)
            or isinstance(sender_id, bool)
            or sender_id <= 0
            or sender.get("is_bot") is not False
            or not isinstance(chat, Mapping)
            or chat.get("type") != "private"
            or chat.get("id") != sender_id
            or not isinstance(callback_id, str)
            or not isinstance(data, str)
        ):
            await self._audit(
                request_id, "telegram.update.rejected", None, None, "TELEGRAM_CALLBACK_INVALID"
            )
            return "rejected"
        progress.sender_id = sender_id
        match = _LINK_CALLBACK.fullmatch(data)
        accepted = False
        if match is not None:
            accepted = await self._store.confirm_link_callback(
                self._workspace_id, match.group(1), str(sender_id), request_id
            )
        answer = "Đã liên kết Telegram." if accepted else "Xác nhận không hợp lệ hoặc đã hết hạn."
        await self._sender.answer_callback(callback_id, answer)
        await self._safe_send(sender_id, answer)
        return "linked" if accepted else "link_confirmation_invalid"

    async def _safe_audit(
        self,
        request_id: UUID,
        reason: str,
        action: str | None = None,
        *,
        actor: UUID | None = None,
        target: UUID | None = None,
    ) -> None:
        try:
            await self._audit(
                request_id,
                action or "telegram.update.rejected",
                actor,
                target,
                reason,
            )
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


def _command_action(result: BotCommandResult) -> str:
    return "telegram.command.accepted" if result.ok else "telegram.command.rejected"


def _raise_site(error: BaseException) -> str:
    """`file:dòng` của frame trong cùng ném lỗi; chỉ tên file, không giá trị biến."""
    traceback = error.__traceback__
    if traceback is None:
        return "unknown"
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    return f"{Path(traceback.tb_frame.f_code.co_filename).name}:{traceback.tb_lineno}"


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


def _simple_signal_command(text: str) -> str | None:
    """Nhận `/start` và `/stop` trần; link code `/start <mã>` được xử lý riêng trước đó."""
    parts = text.strip().split()
    if len(parts) != 1 or not parts[0].startswith("/"):
        return None
    name = parts[0][1:].split("@", 1)[0].lower()
    return name if name in {"start", "stop"} else None


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
