"""Điều phối vòng đời phiên: idle timeout, last_seen_at, xoay và thu hồi (SEC-09/10/11)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import Session

DEFAULT_SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
SESSION_ABSOLUTE_LIFETIME = timedelta(hours=12)
LAST_SEEN_REFRESH_INTERVAL = timedelta(seconds=60)


def hash_session_token(token: str) -> str:
    """Băm token phiên hoặc CSRF bằng SHA-256 trước khi lưu hoặc đối chiếu DB."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def effective_last_seen_at(session_row: Session) -> datetime | None:
    """Trả mốc hoạt động gần nhất của phiên (fallback về created_at/mfa_verified_at cho stub)."""
    last_seen = getattr(session_row, "last_seen_at", None)
    if last_seen is not None:
        return last_seen
    created_at = getattr(session_row, "created_at", None)
    if created_at is not None:
        return created_at
    return getattr(session_row, "mfa_verified_at", None)


def is_session_absolute_expired(session_row: Session, *, now: datetime) -> bool:
    """Kiểm tra phiên đã chạm hạn tuyệt đối 12 giờ hay chưa."""
    return bool(session_row.expires_at <= now)


def is_session_idle_expired(
    session_row: Session,
    *,
    now: datetime,
    idle_timeout: timedelta = DEFAULT_SESSION_IDLE_TIMEOUT,
) -> bool:
    """Kiểm tra phiên đã vượt ngưỡng không hoạt động (idle timeout) hay chưa."""
    last_seen = effective_last_seen_at(session_row)
    if last_seen is None:
        return True
    return bool(now - last_seen >= idle_timeout)


def is_session_expired(
    session_row: Session,
    *,
    now: datetime,
    idle_timeout: timedelta = DEFAULT_SESSION_IDLE_TIMEOUT,
) -> bool:
    """Kiểm tra phiên không còn hiệu lực do thu hồi, hạn tuyệt đối hoặc idle timeout."""
    if session_row.revoked_at is not None:
        return True
    if is_session_absolute_expired(session_row, now=now):
        return True
    return is_session_idle_expired(session_row, now=now, idle_timeout=idle_timeout)


def should_refresh_last_seen(
    session_row: Session,
    *,
    now: datetime,
    refresh_interval: timedelta = LAST_SEEN_REFRESH_INTERVAL,
) -> bool:
    """Giới hạn ghi `last_seen_at` tối đa một lần mỗi `refresh_interval` (mặc định 60 giây)."""
    last_seen = getattr(session_row, "last_seen_at", None)
    if last_seen is None:
        return True
    return bool(now - last_seen >= refresh_interval)


async def revoke_membership_sessions(
    database: AsyncSession,
    *,
    workspace_id: UUID,
    membership_id: UUID,
    now: datetime,
    exclude_session_id: UUID | None = None,
) -> list[UUID]:
    """Thu hồi toàn bộ phiên đang mở của một thành viên trong cùng workspace."""
    conditions = [
        Session.workspace_id == workspace_id,
        Session.membership_id == membership_id,
        Session.revoked_at.is_(None),
    ]
    if exclude_session_id is not None:
        conditions.append(Session.id != exclude_session_id)
    rows = (await database.scalars(select(Session).where(*conditions))).all()
    revoked_ids: list[UUID] = []
    for row in rows:
        row.revoked_at = now
        revoked_ids.append(row.id)
    return revoked_ids


def issue_rotated_session(
    database: AsyncSession,
    old_session: Session,
    *,
    now: datetime,
    mfa_verified_at: datetime | None = None,
    mfa_method: str | None = None,
    preserve_csrf: bool = True,
) -> tuple[Session, str, str | None]:
    """Thu hồi phiên cũ và phát hành phiên mới cùng token ngẫu nhiên (SEC-10)."""
    if old_session.revoked_at is None:
        old_session.revoked_at = now

    new_raw_token = secrets.token_urlsafe(32)
    raw_csrf: str | None = None
    if preserve_csrf and old_session.csrf_token_hash is not None:
        csrf_hash = old_session.csrf_token_hash
    else:
        raw_csrf = secrets.token_urlsafe(32)
        csrf_hash = hash_session_token(raw_csrf)

    new_session = Session(
        id=uuid4(),
        workspace_id=old_session.workspace_id,
        membership_id=old_session.membership_id,
        token_hash=hash_session_token(new_raw_token),
        csrf_token_hash=csrf_hash,
        expires_at=old_session.expires_at,
        mfa_verified_at=mfa_verified_at or old_session.mfa_verified_at,
        mfa_method=mfa_method if mfa_method is not None else old_session.mfa_method,
        last_seen_at=now,
    )
    database.add(new_session)
    return new_session, new_raw_token, raw_csrf
