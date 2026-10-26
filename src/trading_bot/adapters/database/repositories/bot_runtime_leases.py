"""Lease một writer theo bot/account và fence tầng application (MB05.3, ruling 8–11).

- Lease là một hàng DB; đồng hồ DB (`clock_timestamp()`) là nguồn thời gian. Transaction lease
  chỉ khóa đúng một hàng lease (`FOR UPDATE`), không lấy advisory lock nào (ruling 7).
- `require_current_fence` chạy ngay sau L1 `workspace-settings:{ws}` trong mọi command thay đổi
  state PAPER: `SELECT … FOR SHARE` hàng lease rồi kiểm owner/token/hạn theo đồng hồ DB; khóa
  giữ tới commit nên takeover phải chờ. Trigger DB là lớp fence thứ hai.
- Repository không tự commit/rollback; lỗi mang reason code an toàn.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import Select, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.errors import is_transient_database_error
from trading_bot.adapters.database.models import BotInstance, BotRuntimeLeaseRecord
from trading_bot.domain.execution.runtime_control import (
    MAX_LEASE_TTL,
    FencingToken,
    LeaseGrant,
    LeaseSnapshot,
    RuntimeControlError,
    RuntimeFence,
    decide_lease_acquire,
    decide_lease_renew,
    verify_fence,
)

_DB_REASON = re.compile(r"MB05_([A-Z0-9_]+)")


def runtime_db_reason(error: BaseException, default: str) -> str:
    """Rút reason code `MB05_*` do trigger DB raise; không lộ chi tiết SQL/stack."""
    match = _DB_REASON.search(str(getattr(error, "orig", None) or error))
    return match.group(1) if match else default


class _UnitOfWorkLike(Protocol):
    workspace_id: UUID

    @property
    def session(self) -> AsyncSession: ...


def _snapshot(record: BotRuntimeLeaseRecord | None) -> LeaseSnapshot | None:
    if record is None:
        return None
    return LeaseSnapshot(
        owner_id=record.owner_id,
        fencing_token=FencingToken(int(record.fencing_token)),
        expires_at=record.expires_at,
    )


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(text("SELECT clock_timestamp()"))
    if not isinstance(value, datetime):
        raise RuntimeControlError("DATABASE_CLOCK_UNAVAILABLE")
    return value


def _lease_query(workspace_id: UUID, bot_id: UUID) -> Select[BotRuntimeLeaseRecord]:
    return select(BotRuntimeLeaseRecord).where(
        BotRuntimeLeaseRecord.workspace_id == workspace_id,
        BotRuntimeLeaseRecord.bot_id == bot_id,
    )


class BotRuntimeLease:
    """Compare-and-set lease theo bot; chỉ một chủ thắng, token không bao giờ giảm."""

    def __init__(self, session: AsyncSession, *, max_ttl: timedelta = MAX_LEASE_TTL) -> None:
        self._session = session
        self._max_ttl = max_ttl

    async def database_now(self) -> datetime:
        """Giờ thực của PostgreSQL để caller tính `expires_at` cùng đồng hồ với trigger."""
        return await _database_now(self._session)

    async def current(self, workspace_id: UUID, bot_id: UUID) -> BotRuntimeLeaseRecord | None:
        """Đọc hàng lease (không khóa) trong scope RLS của transaction hiện tại."""
        return await self._session.scalar(_lease_query(workspace_id, bot_id))

    async def _locked(
        self, workspace_id: UUID, bot_id: UUID
    ) -> tuple[BotRuntimeLeaseRecord | None, datetime]:
        """Khóa hàng lease rồi mới đọc đồng hồ DB: chờ khóa xong thì `now` vẫn hiện hành."""
        record = await self._session.scalar(_lease_query(workspace_id, bot_id).with_for_update())
        return record, await _database_now(self._session)

    async def acquire_grant(
        self, workspace_id: UUID, bot_id: UUID, owner_id: str, expires_at: datetime
    ) -> LeaseGrant:
        """Lấy hoặc giữ lease; `rehydrate_required` khi token mới (phải hydrate trước khi ghi)."""
        record, now = await self._locked(workspace_id, bot_id)
        grant = decide_lease_acquire(
            _snapshot(record),
            owner_id=owner_id,
            now=now,
            expires_at=expires_at,
            max_ttl=self._max_ttl,
        )
        if record is None:
            account_id = await self._session.scalar(
                select(BotInstance.trading_account_id).where(
                    BotInstance.workspace_id == workspace_id, BotInstance.id == bot_id
                )
            )
            if account_id is None:
                raise RuntimeControlError("BOT_NOT_FOUND")
            self._session.add(
                BotRuntimeLeaseRecord(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    bot_id=bot_id,
                    account_id=account_id,
                    owner_id=owner_id,
                    fencing_token=int(grant.fencing_token),
                    expires_at=expires_at,
                )
            )
        else:
            record.owner_id = owner_id
            record.fencing_token = int(grant.fencing_token)
            record.expires_at = expires_at
        await self._flush(default="LEASE_HELD_ELSEWHERE")
        return grant

    async def acquire(
        self, workspace_id: UUID, bot_id: UUID, owner_id: str, expires_at: datetime
    ) -> FencingToken:
        """Interface WBS/contract: trả fencing token hiện hành của lease vừa lấy."""
        grant = await self.acquire_grant(workspace_id, bot_id, owner_id, expires_at)
        return grant.fencing_token

    async def renew(
        self,
        workspace_id: UUID,
        bot_id: UUID,
        owner_id: str,
        fencing_token: FencingToken,
        expires_at: datetime,
    ) -> LeaseGrant:
        """Gia hạn khi cùng chủ/token và còn hạn; hết hạn phải `acquire` lại (token mới)."""
        record, now = await self._locked(workspace_id, bot_id)
        grant = decide_lease_renew(
            _snapshot(record),
            owner_id=owner_id,
            fencing_token=fencing_token,
            now=now,
            expires_at=expires_at,
            max_ttl=self._max_ttl,
        )
        if record is None:  # pragma: no cover - decide_lease_renew đã từ chối
            raise RuntimeControlError("LEASE_NOT_HELD")
        record.expires_at = expires_at
        await self._flush(default="LEASE_RENEW_REJECTED")
        return grant

    async def release_if_owner(
        self, workspace_id: UUID, bot_id: UUID, owner_id: str, fencing_token: FencingToken
    ) -> bool:
        """Trả lease khi dừng sạch; chỉ đúng chủ, đúng token và lease còn hiệu lực."""
        record, now = await self._locked(workspace_id, bot_id)
        try:
            verify_fence(_snapshot(record), owner_id=owner_id, fencing_token=fencing_token, now=now)
        except RuntimeControlError:
            return False
        if record is None:  # pragma: no cover - verify_fence đã từ chối
            return False
        # Trigger chuẩn hóa expires_at = clock_timestamp(); lần acquire sau phải tăng token.
        record.expires_at = now
        await self._flush(default="LEASE_RELEASE_REJECTED")
        return True

    async def _flush(self, *, default: str) -> None:
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise RuntimeControlError(runtime_db_reason(error, default)) from None
        except DBAPIError as error:
            if is_transient_database_error(error):
                # Lock/statement timeout: giữ nguyên để caller thử lại (fix round 2), không
                # đổi thành mã từ chối lease (renew bị hạ STANDBY, acquire ghi recovery).
                raise
            raise RuntimeControlError(runtime_db_reason(error, default)) from None


class RuntimeFenceGuard:
    """Fence application dùng chung cho mọi command thay đổi state PAPER/state/incident."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def require(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        fence: RuntimeFence | None,
    ) -> None:
        """Kiểm token hiện hành; thiếu fence luôn bị từ chối (M3 đóng ở MB05.5).

        Trước MB05.5 `fence=None` được nhận khi bot chưa từng có lease (chế độ chuyển tiếp, kiểm
        không khóa hàng). Registry giờ luôn lấy lease khi bootstrap nên không runtime writer nào
        chạy mà không token; NULL token chỉ còn ở checkpoint genesis (trigger DB `genesis_null`).
        """
        if fence is None:
            raise RuntimeControlError("FENCING_TOKEN_REQUIRED")
        await _require_current(
            self._session,
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            owner_id=fence.owner_id,
            token=fence.fencing_token,
        )


async def _require_current(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    bot_id: UUID,
    account_id: UUID,
    owner_id: str,
    token: FencingToken,
) -> None:
    # Khóa trước, đọc đồng hồ sau: hạn lease được so với thời điểm đã giữ row lock.
    record = await session.scalar(_lease_query(workspace_id, bot_id).with_for_update(read=True))
    now = await _database_now(session)
    if record is not None and record.account_id != account_id:
        raise RuntimeControlError("LEASE_SCOPE_MISMATCH")
    verify_fence(_snapshot(record), owner_id=owner_id, fencing_token=token, now=now)


async def _require_unleased(session: AsyncSession, *, workspace_id: UUID, bot_id: UUID) -> None:
    record = await session.scalar(_lease_query(workspace_id, bot_id))
    if record is not None:
        # Bot đã từng có lease: mọi writer phải trình token (giống trigger checkpoint).
        raise RuntimeControlError("FENCING_TOKEN_REQUIRED")


async def require_current_fence(
    uow: _UnitOfWorkLike,
    *,
    bot_id: UUID,
    account_id: UUID,
    owner_id: str,
    token: FencingToken,
) -> None:
    """Fence application (ruling 9a); gọi ngay sau L1 và trước L2–L4 trong cùng transaction."""
    await _require_current(
        uow.session,
        workspace_id=uow.workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        owner_id=owner_id,
        token=token,
    )


async def require_unleased(uow: _UnitOfWorkLike, *, bot_id: UUID) -> None:
    """Kiểm bot chưa từng có hàng lease (điều kiện ghi checkpoint genesis/seed, không phải runtime).

    Không runtime command nào dùng hàm này sau MB05.5; giữ để công cụ seed/kiểm thử kiểm điều
    kiện genesis cùng ngữ nghĩa với trigger DB.
    """
    await _require_unleased(uow.session, workspace_id=uow.workspace_id, bot_id=bot_id)
