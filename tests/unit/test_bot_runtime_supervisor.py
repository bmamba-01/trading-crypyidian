"""MB05.5: supervisor theo tick (lease, control, activity, job); lỗi một bot không lan."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from tests.unit.runtime_registry_fakes import NOW, World

from trading_bot.application.bot_runtime_registry import BotRuntimeRegistry
from trading_bot.application.bot_runtime_supervisor import BotRuntimeSupervisor
from trading_bot.domain.execution.runtime_control import (
    BotReadiness,
    FencingToken,
    RuntimeFence,
)
from trading_bot.ports.account_activity import ActivityClassification, ActivityVerdict


def _registry(world: World, owner: str = "runtime-1") -> BotRuntimeRegistry:
    registry = BotRuntimeRegistry(workspace_id=world.workspace_id, owner_id=owner)
    for account in world.accounts:
        bot_id = world.bots[account.id]
        world.owners[bot_id] = owner
        world.tokens[bot_id] = 1
        registry.register(
            account.id,
            bot_id,
            BotReadiness.READY,
            fence=RuntimeFence(owner, FencingToken(1)),
        )
    return registry


class _Application:
    def __init__(self, failing: set[UUID] | None = None) -> None:
        self.failing = failing or set()
        self.applied: list[UUID] = []
        self.removed: list[UUID] = []
        self.published: list[object] = []

    async def apply_runtime_control(self, account_id: UUID) -> SimpleNamespace:
        if account_id in self.failing:
            raise RuntimeError("synthetic apply failure")
        self.applied.append(account_id)
        return SimpleNamespace(bot_paused=False, workspace_killed=False, changed=False)

    def remove_account(self, account_id: UUID) -> None:
        self.removed.append(account_id)

    def publish_account(self, account: object) -> None:
        self.published.append(account)


class _Bootstrap:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[UUID] = []

    async def bootstrap_bot(self, account_id: UUID) -> SimpleNamespace:
        self.calls.append(account_id)
        bot_id = self.world.bots[account_id]
        self.world.owners[bot_id] = "runtime-1"
        self.world.tokens[bot_id] = self.world.tokens.get(bot_id, 0) + 1
        account = SimpleNamespace(
            account_id=account_id,
            bot_id=bot_id,
            fence=RuntimeFence("runtime-1", FencingToken(self.world.tokens[bot_id])),
        )
        return SimpleNamespace(status="READY", reason_code=None, account=account)


class _ActivityGuard:
    def __init__(self, blocked: set[UUID] | None = None) -> None:
        self.blocked = blocked or set()
        self.inspected: list[UUID] = []

    async def inspect_once(self, account_id: UUID) -> ActivityClassification:
        self.inspected.append(account_id)
        if account_id in self.blocked:
            return ActivityClassification(
                ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED, "EXTERNAL_ACTIVITY_UNRECONCILED"
            )
        return ActivityClassification(ActivityVerdict.CLEAN, None)


def _supervisor(
    world: World,
    registry: BotRuntimeRegistry,
    application: _Application,
    *,
    activity: _ActivityGuard | None = None,
    jobs: list[UUID] | None = None,
) -> tuple[BotRuntimeSupervisor, _Bootstrap]:
    bootstrap = _Bootstrap(world)

    async def process_jobs(account_id: UUID, now: object) -> None:
        assert now == NOW
        if jobs is not None:
            jobs.append(account_id)

    supervisor = BotRuntimeSupervisor(
        registry=registry,
        application=application,
        bootstrap=bootstrap,
        sessions=object(),  # type: ignore[arg-type]
        workspace_id=world.workspace_id,
        lease_factory=world.lease,
        unit_of_work_factory=world.unit_of_work,
        activity_guard=activity,
        job_processor=process_jobs if jobs is not None else None,
        clock=lambda: NOW,
        tick_seconds=0.01,
        lease_ttl=timedelta(seconds=30),
    )
    return supervisor, bootstrap


def test_tick_renews_lease_applies_control_inspects_activity_and_runs_jobs_per_bot() -> None:
    world = World(uuid4())
    registry = _registry(world)
    application = _Application()
    activity = _ActivityGuard()
    jobs: list[UUID] = []
    supervisor, _ = _supervisor(world, registry, application, activity=activity, jobs=jobs)

    asyncio.run(supervisor.tick_once())

    account_ids = [account.id for account in world.accounts]
    assert sorted(application.applied) == sorted(account_ids)
    assert sorted(activity.inspected) == sorted(account_ids)
    assert sorted(jobs) == sorted(account_ids)
    assert registry.summary()["ready"] == 2


def test_failure_of_one_bot_does_not_stop_sibling_processing() -> None:
    world = World(uuid4())
    broken, healthy = world.accounts
    registry = _registry(world)
    application = _Application(failing={broken.id})
    supervisor, _ = _supervisor(world, registry, application, activity=_ActivityGuard())

    asyncio.run(supervisor.tick_once())

    assert application.applied == [healthy.id]
    assert registry.entry(broken.id).last_error_code == "SUPERVISOR_TICK_FAILED"
    assert registry.entry(healthy.id).last_error_code is None
    assert registry.status() == "degraded"


def test_lost_lease_demotes_only_that_bot_then_reacquires_and_republishes() -> None:
    world = World(uuid4())
    lost, kept = world.accounts
    registry = _registry(world)
    world.owners[world.bots[lost.id]] = "other-process"  # takeover xảy ra trong lúc "ngủ"
    application = _Application()
    supervisor, bootstrap = _supervisor(world, registry, application)

    asyncio.run(supervisor.tick_once())

    assert application.removed == [lost.id]
    assert any(
        audit["action"] == "bot.runtime.lease_lost" and audit["reason"] == "STALE_FENCING_TOKEN"
        for audit in world.audits
    )
    assert bootstrap.calls == [lost.id]
    assert application.published
    assert application.published[0].account_id == lost.id
    assert registry.entry(lost.id).readiness is BotReadiness.READY
    assert registry.entry(kept.id).fence == RuntimeFence("runtime-1", FencingToken(1))


def test_fence_rejection_mid_tick_demotes_bot_to_standby() -> None:
    from trading_bot.domain.execution.runtime_control import RuntimeControlError

    world = World(uuid4(), account_count=1)
    registry = _registry(world)
    account_id = world.accounts[0].id

    class _ExpiringGuard(_ActivityGuard):
        async def inspect_once(self, account_id: UUID) -> ActivityClassification:
            raise RuntimeControlError("LEASE_EXPIRED")

    application = _Application()
    supervisor, bootstrap = _supervisor(world, registry, application, activity=_ExpiringGuard())
    bootstrap.bootstrap_bot = lambda _account: _standby()  # type: ignore[method-assign]

    asyncio.run(supervisor.tick_once())

    assert application.removed == [account_id]
    assert registry.entry(account_id).readiness is BotReadiness.STANDBY
    assert registry.entry(account_id).last_error_code is None


async def _standby() -> SimpleNamespace:
    return SimpleNamespace(status="STANDBY", reason_code="LEASE_HELD_ELSEWHERE", account=None)


def test_activity_block_is_reflected_in_registry_counts() -> None:
    world = World(uuid4())
    blocked = world.accounts[0]
    registry = _registry(world)
    supervisor, _ = _supervisor(
        world, registry, _Application(), activity=_ActivityGuard(blocked={blocked.id})
    )

    asyncio.run(supervisor.tick_once())

    assert registry.summary()["activity_blocked"] == 1


def test_run_loop_stops_cleanly_and_releases_held_leases() -> None:
    world = World(uuid4())
    registry = _registry(world)
    application = _Application()
    supervisor, _ = _supervisor(world, registry, application)

    async def scenario() -> None:
        task = asyncio.create_task(supervisor.run())
        await asyncio.sleep(0.05)
        await supervisor.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())

    assert len(application.applied) >= 2
    assert set(world.owners.values()) == {"released"}
