"""Dịch vụ dashboard trạng thái liên kết, lịch sử delivery và test message Telegram (CRY-347)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.entities import AuditEvent, TelegramBinding
from trading_bot.adapters.database.models.telegram_entities import (
    TelegramSignalDelivery,
    TelegramSignalSubscription,
)
from trading_bot.ports.telegram import TelegramMessageSender

_STATUS_LABELS_VI: dict[str, str] = {
    "SENT": "Đã gửi",
    "PENDING": "Đang chờ",
    "PROCESSING": "Đang xử lý",
    "EXPIRED": "Hết hạn",
    "DEAD": "Lỗi hẳn (Dead-letter)",
    "FILTERED": "Bị lọc theo cấu hình",
    "SUPPRESSED": "Bị chặn chống trùng (Dedupe/Throttle)",
}


async def list_telegram_delivery_dashboard(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    bot_id: UUID | None = None,
    allowed_bot_ids: frozenset[UUID] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Đọc trạng thái đăng ký và lịch sử delivery theo quyền, tuyệt đối không lộ chat ID/token."""
    safe_limit = min(max(1, limit), 200)

    # 1. Lấy danh sách bot được phép xem trong workspace
    bot_stmt = select(BotInstance.id, BotInstance.name).where(
        BotInstance.workspace_id == workspace_id
    )
    if bot_id is not None:
        bot_stmt = bot_stmt.where(BotInstance.id == bot_id)
    if allowed_bot_ids is not None:
        if not allowed_bot_ids:
            return _empty_dashboard_payload()
        bot_stmt = bot_stmt.where(BotInstance.id.in_(allowed_bot_ids))

    bot_rows = (await session.execute(bot_stmt.order_by(BotInstance.name))).all()
    bot_names = {row.id: str(row.name) for row in bot_rows}
    scoped_bot_ids = set(bot_names.keys())

    if bot_id is not None and bot_id not in scoped_bot_ids:
        return _empty_dashboard_payload()

    # 2. Đọc trạng thái binding & subscription (không trả chat_id hay telegram_user_id)
    bindings = (
        await session.scalars(
            select(TelegramBinding).where(TelegramBinding.workspace_id == workspace_id)
        )
    ).all()
    active_binding_ids = {
        b.id for b in bindings if b.revoked_at is None and b.verified_at is not None
    }

    sub_stmt = select(TelegramSignalSubscription).where(
        TelegramSignalSubscription.workspace_id == workspace_id
    )
    if bot_id is not None:
        sub_stmt = sub_stmt.where(TelegramSignalSubscription.bot_id == bot_id)
    elif allowed_bot_ids is not None:
        sub_stmt = sub_stmt.where(TelegramSignalSubscription.bot_id.in_(scoped_bot_ids))
    sub_rows = (await session.scalars(sub_stmt)).all()

    subscriptions = [
        {
            "subscription_id": str(sub.id),
            "bot_id": str(sub.bot_id),
            "bot_name": bot_names.get(sub.bot_id, str(sub.bot_id)),
            "enabled": bool(sub.enabled),
            "binding_active": sub.telegram_binding_id in active_binding_ids,
        }
        for sub in sub_rows
    ]

    # 3. Đọc lịch sử delivery gần nhất
    del_stmt = (
        select(TelegramSignalDelivery)
        .where(TelegramSignalDelivery.workspace_id == workspace_id)
        .order_by(TelegramSignalDelivery.created_at.desc())
        .limit(safe_limit)
    )
    if bot_id is not None:
        del_stmt = del_stmt.where(TelegramSignalDelivery.bot_id == bot_id)
    elif allowed_bot_ids is not None:
        del_stmt = del_stmt.where(TelegramSignalDelivery.bot_id.in_(scoped_bot_ids))

    deliveries = (await session.scalars(del_stmt)).all()

    sent_count = 0
    pending_count = 0
    dead_count = 0
    expired_count = 0
    filtered_count = 0
    suppressed_count = 0
    last_sent_at: datetime | None = None
    last_error_code: str | None = None

    items: list[dict[str, Any]] = []
    for item in deliveries:
        st = str(item.status)
        if st == "SENT":
            sent_count += 1
            if item.sent_at is not None and (last_sent_at is None or item.sent_at > last_sent_at):
                last_sent_at = item.sent_at
        elif st in {"PENDING", "PROCESSING"}:
            pending_count += 1
        elif st == "DEAD":
            dead_count += 1
        elif st == "EXPIRED":
            expired_count += 1
        elif st == "FILTERED":
            filtered_count += 1
        elif st == "SUPPRESSED":
            suppressed_count += 1

        if last_error_code is None and item.last_error_code:
            last_error_code = str(item.last_error_code)

        items.append(
            {
                "delivery_id": str(item.id),
                "bot_id": str(item.bot_id),
                "bot_name": bot_names.get(item.bot_id, str(item.bot_id)),
                "decision_id": str(item.decision_id),
                "status": st,
                "status_vi": _STATUS_LABELS_VI.get(st, st),
                "attempts": int(item.attempts or 0),
                "last_error_code": item.last_error_code,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "available_at": item.available_at.isoformat() if item.available_at else None,
                "sent_at": item.sent_at.isoformat() if item.sent_at else None,
            }
        )

    return {
        "bots": [{"bot_id": str(bid), "bot_name": name} for bid, name in bot_names.items()],
        "summary": {
            "total": len(items),
            "active_bindings": len(active_binding_ids),
            "enabled_subscriptions": sum(1 for s in subscriptions if s["enabled"]),
            "sent_count": sent_count,
            "pending_count": pending_count,
            "dead_count": dead_count,
            "expired_count": expired_count,
            "filtered_count": filtered_count,
            "suppressed_count": suppressed_count,
            "last_sent_at": last_sent_at.isoformat() if last_sent_at else None,
            "last_error_code": last_error_code,
        },
        "subscriptions": subscriptions,
        "deliveries": items,
    }


async def send_telegram_test_message(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    membership_id: UUID,
    sender: TelegramMessageSender | None,
    reason: str = "Kiểm tra kết nối Telegram từ Admin Web",
    request_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Gửi tin nhắn thử tới binding Telegram đang hoạt động, không tạo signal/order và có audit."""
    binding = await session.scalar(
        select(TelegramBinding)
        .where(
            TelegramBinding.workspace_id == workspace_id,
            TelegramBinding.membership_id == membership_id,
            TelegramBinding.revoked_at.is_(None),
            TelegramBinding.verified_at.is_not(None),
        )
        .limit(1)
    )
    if binding is None:
        raise ValueError("Thành viên hiện tại chưa có liên kết Telegram đang hoạt động.")
    if sender is None:
        raise RuntimeError("Kênh gửi Telegram chưa được cấu hình trên máy chủ.")

    current = now if now is not None else datetime.now(UTC)
    text = (
        "Crypidian: Tin nhắn thử kết nối Telegram từ Admin Web "
        "(chỉ kiểm tra đường truyền, không tạo tín hiệu hay lệnh giao dịch)."
    )
    await sender.send_message(
        int(binding.telegram_user_id),
        text,
        message_thread_id=binding.signal_thread_id,
    )

    raw_audit = f"{workspace_id}:{binding.id}:{current.isoformat()}".encode()
    digest = hashlib.sha256(raw_audit).hexdigest()
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_membership_id=membership_id,
            action="telegram.test_message.sent",
            target_type="telegram_binding",
            target_id=binding.id,
            reason=reason.strip() or "Kiểm tra kết nối Telegram từ Admin Web",
            after_hash=digest,
            request_id=request_id,
            occurred_at=current,
        )
    )
    return {
        "sent": True,
        "binding_id": str(binding.id),
        "sent_at": current.isoformat(),
        "message_vi": "Đã gửi tin nhắn thử Telegram thành công (không tạo tín hiệu hay lệnh).",
    }


def _empty_dashboard_payload() -> dict[str, Any]:
    return {
        "bots": [],
        "summary": {
            "total": 0,
            "active_bindings": 0,
            "enabled_subscriptions": 0,
            "sent_count": 0,
            "pending_count": 0,
            "dead_count": 0,
            "expired_count": 0,
            "filtered_count": 0,
            "suppressed_count": 0,
            "last_sent_at": None,
            "last_error_code": None,
        },
        "subscriptions": [],
        "deliveries": [],
    }


__all__ = ["list_telegram_delivery_dashboard", "send_telegram_test_message"]
