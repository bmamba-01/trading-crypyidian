"""Supervisor runtime PAPER theo tick (MB05.5): lease, control, activity và job theo bot.

Mỗi tick, với từng bot của registry: gia hạn hoặc lấy lại lease (re-acquire thì hydrate lại và
công bố instance mới), áp control mới (pause/kill/resume) qua command có fence, kiểm activity
account và xử lý decision job pending. Exception của một bot chỉ đánh dấu bot đó; bot khác vẫn
được xử lý trong cùng tick. Đồng hồ ứng dụng tiêm vào (unit test), hạn lease theo đồng hồ DB.
Dừng sạch khi lifespan kết thúc và trả lease đang giữ để process kế tiếp tiếp quản ngay.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.errors import is_transient_database_error
from trading_bot.adapters.database.repositories.bot_runtime_leases import BotRuntimeLease
from trading_bot.application.bot_runtime_registry import BotRuntimeRegistry
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.execution.runtime_control import (
    BotReadiness,
    RuntimeControlError,
)
from trading_bot.ports.account_activity import ActivityVerdict

logger = logging.getLogger(__name__)

TICK_FAILED = "SUPERVISOR_TICK_FAILED"
TRANSIENT_DATABASE_TIMEOUT = "TRANSIENT_DATABASE_TIMEOUT"
_LEASE_LOSS = frozenset(
    {"STALE_FENCING_TOKEN", "LEASE_EXPIRED", "LEASE_NOT_HELD", "LEASE_SCOPE_MISMATCH"}
)


class SupervisedApplication(Protocol):
    async def apply_runtime_control(self, account_id: UUID) -> Any: ...

    def remove_account(self, account_id: UUID) -> None: ...

    def publish_account(self, account: Any) -> None: ...


class BotRuntimeSupervisor:
    """Vòng `asyncio` điều phối từng bot; không giữ khóa DB giữa các tick."""

    def __init__(
        self,
        *,
        registry: BotRuntimeRegistry,
        application: SupervisedApplication,
        bootstrap: Any,
        sessions: async_sessionmaker[AsyncSession] | None,
        workspace_id: UUID,
        lease_factory: Callable[[Any], Any] = BotRuntimeLease,
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        activity_guard: Any | None = None,
        job_processor: Callable[[UUID, datetime], Awaitable[Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        tick_seconds: float = 5.0,
        lease_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        if tick_seconds <= 0:
            raise ValueError("tick_seconds phải dương.")
        self._registry = registry
        self._application = application
        self._bootstrap = bootstrap
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._lease_factory = lease_factory
        self._unit_of_work_factory = unit_of_work_factory
        self._activity_guard = activity_guard
        self._job_processor = job_processor
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tick_seconds = tick_seconds
        self._lease_ttl = lease_ttl
        self._stopping = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        # account -> (số lần timeout liên tiếp, thời điểm được thử lại); fix round 2.
        self._retry: dict[UUID, tuple[int, datetime]] = {}

    async def tick_once(self) -> None:
        """Một lượt cho mọi bot; lỗi của bot nào chỉ ghi nhận cho bot đó."""
        async with self._tick_lock:
            for account_id in self._registry.account_ids():
                if self._stopping.is_set():
                    return
                try:
                    await self._tick_bot(account_id)
                except Exception as error:
                    logger.error(
                        "Supervisor tick lỗi cho một bot; exception_type=%s reason_code=%s",
                        type(error).__name__,
                        getattr(error, "reason_code", None),
                    )
                    self._registry.record_error(account_id, TICK_FAILED)

    async def run(self) -> None:
        """Lặp tick tới khi `stop()`; không để exception thoát vòng lặp."""
        while not self._stopping.is_set():
            await self.tick_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._tick_seconds)

    async def stop(self) -> None:
        """Dừng vòng lặp rồi trả lease đang giữ (lần acquire sau phải tăng token)."""
        self._stopping.set()
        async with self._tick_lock:
            for account_id in self._registry.account_ids():
                entry = self._registry.entry(account_id)
                if not entry.holds_lease or entry.bot_id is None or entry.fence is None:
                    continue
                try:
                    async with self._unit_of_work_factory(
                        self._sessions, workspace_id=self._workspace_id, bot_id=entry.bot_id
                    ) as uow:
                        await self._lease_factory(uow.session).release_if_owner(
                            self._workspace_id,
                            entry.bot_id,
                            entry.fence.owner_id,
                            entry.fence.fencing_token,
                        )
                except Exception as error:
                    logger.warning(
                        "Không trả được lease khi dừng; exception_type=%s", type(error).__name__
                    )

    async def _tick_bot(self, account_id: UUID) -> None:
        entry = self._registry.entry(account_id)
        if entry.blocked_by_workspace:
            return
        if entry.bot_id is None and entry.readiness is not BotReadiness.STANDBY:
            return
        await self._audit_pending_lease_loss(account_id)
        if entry.holds_lease:
            await self._renew(account_id)
            entry = self._registry.entry(account_id)
        if entry.readiness is BotReadiness.STANDBY and not self._backing_off(account_id):
            await self._reacquire(account_id)
            entry = self._registry.entry(account_id)
        if entry.readiness is not BotReadiness.READY:
            return
        try:
            await self._ready_steps(account_id)
        except RuntimeControlError as error:
            # Fence của state/incident từ chối giữa tick (lease hết hạn/bị tiếp quản): hạ STANDBY
            # đúng bot đó thay vì để lỗi lặp lại mỗi tick.
            if error.reason_code in _LEASE_LOSS:
                await self._demote(account_id, error.reason_code)
                return
            raise

    async def _ready_steps(self, account_id: UUID) -> None:
        control = await self._application.apply_runtime_control(account_id)
        self._registry.set_control_flags(
            account_id,
            bot_paused=bool(getattr(control, "bot_paused", False)),
            workspace_killed=bool(getattr(control, "workspace_killed", False)),
        )
        if self._activity_guard is not None:
            classification = await self._activity_guard.inspect_once(account_id)
            self._registry.set_activity_blocked(
                account_id, classification.verdict is not ActivityVerdict.CLEAN
            )
        if self._job_processor is not None:
            await self._job_processor(account_id, self._clock())
        self._registry.record_error(account_id, None)

    async def _renew(self, account_id: UUID) -> None:
        entry = self._registry.entry(account_id)
        if entry.fence is None or entry.bot_id is None:  # pragma: no cover - holds_lease đã kiểm
            return
        try:
            async with self._unit_of_work_factory(
                self._sessions, workspace_id=self._workspace_id, bot_id=entry.bot_id
            ) as uow:
                lease = self._lease_factory(uow.session)
                now = await lease.database_now()
                await lease.renew(
                    self._workspace_id,
                    entry.bot_id,
                    entry.fence.owner_id,
                    entry.fence.fencing_token,
                    now + self._lease_ttl,
                )
        except RuntimeControlError as error:
            await self._demote(account_id, error.reason_code)

    async def _demote(self, account_id: UUID, reason_code: str) -> None:
        """Mất lease: gỡ gateway khỏi application, hạ STANDBY, audit (không cần fence)."""
        self._application.remove_account(account_id)
        self._registry.mark_standby(account_id, reason_code)
        await self._audit_lease_lost(account_id, reason_code)

    async def _audit_pending_lease_loss(self, account_id: UUID) -> None:
        """Audit mất lease do application phát hiện qua command (M1); lỗi ghi thì giữ lại."""
        reason_code = self._registry.take_pending_lease_loss(account_id)
        if reason_code is None:
            return
        try:
            await self._audit_lease_lost(account_id, reason_code)
        except Exception:
            self._registry.restore_pending_lease_loss(account_id, reason_code)
            raise

    async def _audit_lease_lost(self, account_id: UUID, reason_code: str) -> None:
        entry = self._registry.entry(account_id)
        async with self._unit_of_work_factory(
            self._sessions, workspace_id=self._workspace_id, bot_id=entry.bot_id
        ) as uow:
            uow.record_audit(
                action="bot.runtime.lease_lost",
                target_type="bot_instance",
                target_id=entry.bot_id,
                reason=reason_code,
            )

    async def _reacquire(self, account_id: UUID) -> None:
        try:
            outcome = await self._bootstrap.bootstrap_bot(account_id)
        except Exception as error:
            if not is_transient_database_error(error):
                raise
            self._schedule_retry(account_id)
            self._registry.mark_standby(account_id, TRANSIENT_DATABASE_TIMEOUT)
            return
        status = str(getattr(outcome.status, "value", outcome.status))
        if status == "READY" and outcome.account is not None:
            self._retry.pop(account_id, None)
            self._application.publish_account(outcome.account)
            self._registry.mark_ready(account_id, outcome.account.fence, outcome.account.bot_id)
        elif status == "RECOVERY_REQUIRED":
            self._retry.pop(account_id, None)
            self._registry.mark_recovery(account_id, outcome.reason_code or TICK_FAILED)
        elif outcome.reason_code == TRANSIENT_DATABASE_TIMEOUT:
            self._schedule_retry(account_id)
            self._registry.mark_standby(account_id, TRANSIENT_DATABASE_TIMEOUT)
        else:
            self._retry.pop(account_id, None)

    def _backing_off(self, account_id: UUID) -> bool:
        retry = self._retry.get(account_id)
        return retry is not None and self._clock() < retry[1]

    def _schedule_retry(self, account_id: UUID) -> None:
        """Backoff lũy thừa 2 theo tick, trần = TTL lease (fix round 2).

        Timeout tạm thời (lock/statement) khi re-acquire: bot ở STANDBY và được thử lại; không dồn
        thêm tải khóa lên workspace đang tranh chấp mỗi tick, nhưng cũng không chờ quá một TTL.
        """
        attempts = self._retry.get(account_id, (0, self._clock()))[0] + 1
        delay = min(
            timedelta(seconds=self._tick_seconds * (2**attempts)),
            max(self._lease_ttl, timedelta(seconds=self._tick_seconds)),
        )
        self._retry[account_id] = (attempts, self._clock() + delay)
