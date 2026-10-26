"""MB-05 Part B fix round 2: lock/statement timeout (55P03/57014) là lỗi tạm thời ở bootstrap.

Timeout phiên (fix round 1, I2) làm chờ khóa thất bại có giới hạn. Ở bootstrap, lỗi này không
được biến thành `RECOVERY_REQUIRED` (dính tới khi restart) hay `PAPER_BOOTSTRAP_FAILED` (503 dính):
bot về STANDBY và supervisor thử lại ở tick sau với backoff có trần; pha workspace thử lại.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.runtime_fakes import RecordingSession
from tests.unit.runtime_registry_fakes import NOW, World, database_error
from tests.unit.test_bot_runtime_registry import _service
from tests.unit.test_bot_runtime_supervisor import _Application

from trading_bot.application.bot_runtime_registry import BotRuntimeRegistry
from trading_bot.application.bot_runtime_supervisor import BotRuntimeSupervisor
from trading_bot.application.paper_runtime_bootstrap import (
    TRANSIENT_DATABASE_TIMEOUT,
    PaperAccountRuntimeStatus,
)
from trading_bot.domain.execution.runtime_control import BotReadiness, RuntimeControlError

TRANSIENT = ("55P03", "57014")


def _recovery_rows(world: World) -> list[dict]:
    return [state for state in world.states if state["readiness"] == "RECOVERY_REQUIRED"]


def _recovery_audits(world: World) -> list[dict]:
    return [a for a in world.audits if a["action"] == "bot.runtime.recovery_required"]


@pytest.mark.parametrize("sqlstate", TRANSIENT)
@pytest.mark.parametrize("times", [1, 2])
def test_lease_acquire_timeout_is_standby_not_recovery_required(sqlstate: str, times: int) -> None:
    """times=1: lần acquire thứ hai (ghi RECOVERY) sẽ thành công trên lease hết hạn — vẫn không
    được ghi RECOVERY_REQUIRED; times=2: cả hai lần đều timeout."""
    world = World(uuid4())
    slow, other = world.accounts
    world.fail_transient("acquire", world.bots[slow.id], times=times, sqlstate=sqlstate)

    result = asyncio.run(_service(world).bootstrap())

    assert result.account_states[slow.id].status is PaperAccountRuntimeStatus.STANDBY
    assert result.account_states[slow.id].reason_code == TRANSIENT_DATABASE_TIMEOUT
    assert result.bot_ids[slow.id] == world.bots[slow.id]  # supervisor cần bot_id để thử lại
    assert result.account_states[other.id].status is PaperAccountRuntimeStatus.READY
    assert _recovery_rows(world) == []
    assert _recovery_audits(world) == []


@pytest.mark.parametrize("sqlstate", TRANSIENT)
def test_hydrate_timeout_is_standby_not_recovery_required(sqlstate: str) -> None:
    world = World(uuid4(), account_count=1)
    (account,) = world.accounts
    world.fail_transient("hydrate", account.id, sqlstate=sqlstate)

    result = asyncio.run(_service(world).bootstrap())

    assert result.account_states[account.id].status is PaperAccountRuntimeStatus.STANDBY
    assert result.account_states[account.id].reason_code == TRANSIENT_DATABASE_TIMEOUT
    assert account.id not in result.active_accounts
    assert _recovery_rows(world) == []


def test_workspace_l1_timeout_is_retryable_not_bootstrap_failed() -> None:
    world = World(uuid4())
    world.fail_transient("l1", None)
    service = _service(world)

    first = asyncio.run(service.bootstrap())
    second = asyncio.run(service.bootstrap())

    assert (first.ready, first.database_ready, first.error_code) == (
        False,
        True,
        TRANSIENT_DATABASE_TIMEOUT,
    )
    assert second.ready is True
    assert {state.status for state in second.account_states.values()} == {
        PaperAccountRuntimeStatus.READY
    }


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


def _standby_supervisor(world: World, clock: _Clock):
    service = _service(world)
    result = asyncio.run(service.bootstrap())
    registry = BotRuntimeRegistry.from_bootstrap(result)
    application = _Application()
    supervisor = BotRuntimeSupervisor(
        registry=registry,
        application=application,
        bootstrap=service,
        sessions=object(),  # type: ignore[arg-type]
        workspace_id=world.workspace_id,
        lease_factory=world.lease,
        unit_of_work_factory=world.unit_of_work,
        clock=clock,
        tick_seconds=1,
        lease_ttl=timedelta(seconds=30),
    )
    return supervisor, registry, application


def test_supervisor_retries_transient_standby_on_next_tick_until_ready() -> None:
    world = World(uuid4(), account_count=1)
    (account,) = world.accounts
    world.fail_transient("acquire", world.bots[account.id], times=1)
    clock = _Clock()
    supervisor, registry, application = _standby_supervisor(world, clock)
    assert registry.entry(account.id).readiness is BotReadiness.STANDBY

    clock.now += timedelta(seconds=1)
    asyncio.run(supervisor.tick_once())

    assert registry.entry(account.id).readiness is BotReadiness.READY
    assert [item.account_id for item in application.published] == [account.id]
    assert _recovery_rows(world) == []


def test_supervisor_backs_off_repeated_transient_reacquire_with_cap() -> None:
    world = World(uuid4(), account_count=1)
    (account,) = world.accounts
    bot_id = world.bots[account.id]
    world.fail_transient("acquire", bot_id, times=2)
    clock = _Clock()
    supervisor, registry, _application = _standby_supervisor(world, clock)
    world.fail_transient("acquire", bot_id, times=100)
    attempts: list[object] = []
    original = supervisor._bootstrap.bootstrap_bot

    async def counting(account_id):
        attempts.append(clock.now)
        return await original(account_id)

    supervisor._bootstrap.bootstrap_bot = counting  # type: ignore[method-assign]
    for _ in range(40):  # 40 tick cách nhau 1s
        clock.now += timedelta(seconds=1)
        asyncio.run(supervisor.tick_once())

    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in zip(attempts, attempts[1:], strict=False)
    ]
    assert gaps[:4] == [2, 4, 8, 16]  # lũy thừa 2 theo tick
    assert max(gaps) <= 30  # trần = TTL lease
    assert registry.entry(account.id).readiness is BotReadiness.STANDBY
    assert registry.entry(account.id).reason_code == TRANSIENT_DATABASE_TIMEOUT
    assert _recovery_rows(world) == []

    world.fail_transient("acquire", bot_id, times=0)
    for _ in range(31):
        clock.now += timedelta(seconds=1)
        asyncio.run(supervisor.tick_once())
    assert registry.entry(account.id).readiness is BotReadiness.READY


def test_lease_flush_keeps_transient_database_error_for_retry_classification() -> None:
    from sqlalchemy.exc import DBAPIError

    from trading_bot.adapters.database.repositories.bot_runtime_leases import BotRuntimeLease

    session = RecordingSession()
    session.flush_error = database_error("55P03")
    with pytest.raises(DBAPIError) as transient:
        asyncio.run(BotRuntimeLease(session)._flush(default="LEASE_RENEW_REJECTED"))  # type: ignore[arg-type]
    assert not isinstance(transient.value, RuntimeControlError)

    session.flush_error = database_error("23514")
    with pytest.raises(RuntimeControlError):
        asyncio.run(BotRuntimeLease(session)._flush(default="LEASE_RENEW_REJECTED"))  # type: ignore[arg-type]


def test_is_transient_database_error_matches_lock_and_statement_timeouts_only() -> None:
    from trading_bot.adapters.database.errors import is_transient_database_error

    assert is_transient_database_error(database_error("55P03"))
    assert is_transient_database_error(database_error("57014"))
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = database_error("55P03")
    assert is_transient_database_error(wrapped)
    assert not is_transient_database_error(database_error("23505"))
    assert not is_transient_database_error(RuntimeError("x"))


def test_runtime_app_retries_transient_workspace_bootstrap_instead_of_sticky_503() -> None:
    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    workspace_id = uuid4()
    account_id = uuid4()
    calls: list[int] = []
    # Lần thử lại chỉ chạy sau khi đã đọc readiness lần đầu (tránh đua khi suite chạy chậm).
    first_read = threading.Event()

    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            calls.append(1)
            while len(calls) > 1 and not first_read.is_set():
                await asyncio.sleep(0.01)
            if len(calls) == 1:
                return SimpleNamespace(
                    workspace_id=workspace_id,
                    ready=False,
                    database_ready=True,
                    error_code=TRANSIENT_DATABASE_TIMEOUT,
                    account_states={},
                    active_accounts={},
                )
            return SimpleNamespace(
                workspace_id=workspace_id,
                ready=True,
                database_ready=True,
                error_code=None,
                account_states={account_id: SimpleNamespace(status="READY", reason_code=None)},
                active_accounts={account_id: SimpleNamespace(account_id=account_id)},
            )

    app = create_app(
        AppSettings(
            process_role="trading-runtime",
            runtime_workspace_id=workspace_id,
            runtime_supervisor_tick_seconds=0.05,
        ),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
    )
    with TestClient(app) as client:
        first = client.get("/health/ready")
        first_read.set()
        deadline = time.monotonic() + 5
        later = first
        while time.monotonic() < deadline and later.status_code != 200:
            time.sleep(0.05)
            later = client.get("/health/ready")

    assert first.status_code == 503
    assert first.json()["paper_runtime_error_code"] == TRANSIENT_DATABASE_TIMEOUT
    assert later.status_code == 200, later.json()
    assert len(calls) == 2
