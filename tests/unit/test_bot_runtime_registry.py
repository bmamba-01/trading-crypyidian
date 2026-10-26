"""MB05.5: bootstrap theo bot (pha workspace + pha bot) và registry runtime."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from tests.unit.runtime_registry_fakes import World

from trading_bot.application.bot_runtime_registry import BotRuntimeRegistry
from trading_bot.application.paper_runtime_bootstrap import (
    PaperAccountRuntimeStatus,
    PaperRuntimeBootstrapService,
    new_runtime_owner_id,
)
from trading_bot.domain.execution.runtime_control import BotReadiness


def _service(world: World, *, owner_id: str = "runtime-1") -> PaperRuntimeBootstrapService:
    return PaperRuntimeBootstrapService(
        sessions=object(),  # type: ignore[arg-type]
        workspace_id=world.workspace_id,
        owner_id=owner_id,
        lease_ttl=timedelta(seconds=30),
        unit_of_work_factory=world.unit_of_work,
        account_repository_factory=world.account_repository,
        settings_repository_factory=world.settings_repository,
        gateway_factory=world.gateway,  # type: ignore[arg-type]
        coordinator_factory=world.coordinator,  # type: ignore[arg-type]
        lease_factory=world.lease,
        state_repository_factory=world.state_repository,
        bot_config_loader=world.bot_config_loader,
    )


def _bootstrap(world: World, **kwargs: object):
    return asyncio.run(_service(world, **kwargs).bootstrap())  # type: ignore[arg-type]


def test_sibling_checkpoint_failure_only_marks_that_bot_recovery_required() -> None:
    world = World(uuid4())
    good, broken = world.accounts
    world.failing_hydrate.add(broken.id)

    result = _bootstrap(world)

    assert result.ready is True
    assert result.account_states[good.id].status is PaperAccountRuntimeStatus.READY
    assert result.account_states[broken.id].status is PaperAccountRuntimeStatus.RECOVERY_REQUIRED
    assert result.account_states[broken.id].reason_code == "PAPER_RECOVERY_CONFLICT"
    assert set(result.active_accounts) == {good.id}
    # Bot lỗi vẫn giữ lease để ghi state + audit có fence; sibling hydrate và công bố gateway.
    broken_states = [s for s in world.states if s["account_id"] == broken.id]
    assert [s["readiness"] for s in broken_states] == [BotReadiness.RECOVERY_REQUIRED]
    assert broken_states[0]["reason_code"] == "PAPER_RECOVERY_CONFLICT"
    assert any(
        a["action"] == "bot.runtime.recovery_required" and a["target_id"] == world.bots[broken.id]
        for a in world.audits
    )
    good_states = [s for s in world.states if s["account_id"] == good.id]
    assert [s["readiness"] for s in good_states] == [BotReadiness.READY]
    published = [gateway for gateway in world.gateways if gateway.published]
    assert len(published) == 1


def test_lease_held_elsewhere_puts_bot_in_standby_without_hydration_or_writes() -> None:
    world = World(uuid4())
    held, free = world.accounts
    world.held_elsewhere.add(world.bots[held.id])

    result = _bootstrap(world)

    assert result.account_states[held.id].status is PaperAccountRuntimeStatus.STANDBY
    assert result.account_states[held.id].reason_code == "LEASE_HELD_ELSEWHERE"
    assert result.account_states[free.id].status is PaperAccountRuntimeStatus.READY
    assert held.id not in result.active_accounts
    assert all(state["account_id"] != held.id for state in world.states)


def test_workspace_condition_failure_fails_whole_workspace() -> None:
    world = World(uuid4())
    world.settings_unavailable = True

    result = _bootstrap(world)

    assert result.ready is False
    assert result.error_code == "SETTINGS_NOT_EFFECTIVE"
    assert result.active_accounts == {}
    assert world.tokens == {}
    assert {state.status for state in result.account_states.values()} == {
        PaperAccountRuntimeStatus.BLOCKED_BY_WORKSPACE
    }


def test_disabled_bot_is_not_hydrated_nor_leased() -> None:
    world = World(uuid4(), account_count=1)
    disabled = SimpleNamespace(
        id=uuid4(), workspace_id=world.workspace_id, mode="PAPER", status="DISABLED"
    )
    world.bots[disabled.id] = uuid4()
    # Repository list_enabled chỉ trả account/bot ENABLED (bot C của QA không xuất hiện).

    result = _bootstrap(world)

    assert disabled.id not in result.account_states
    assert world.bots[disabled.id] not in world.tokens


def test_each_bot_uses_own_unit_of_work_without_allow_switch() -> None:
    world = World(uuid4(), account_count=3)

    _bootstrap(world)

    bot_uows = [uow for uow in world.uows if uow.bound]
    assert len(bot_uows) == 3
    assert all(len(uow.bound) == 1 for uow in bot_uows)
    assert all(not any(uow.allow_switch_calls) for uow in world.uows)
    assert sorted(world.config_loads) == sorted(account.id for account in world.accounts)


def test_each_bot_has_own_gateway_coordinator_fence_and_config_lineage() -> None:
    world = World(uuid4())
    first, second = world.accounts
    version_id = uuid4()
    world.bot_configs[first.id] = SimpleNamespace(bot_config_version_id=version_id)

    result = _bootstrap(world, owner_id="runtime-xyz")

    runtime_first = result.active_accounts[first.id]
    runtime_second = result.active_accounts[second.id]
    assert runtime_first.gateway is not runtime_second.gateway
    assert runtime_first.coordinator is not runtime_second.coordinator
    assert runtime_first.owner_id == "runtime-xyz"
    assert int(runtime_first.fencing_token) == 1
    assert runtime_first.bot_config_version_id == version_id
    assert runtime_second.bot_config_version_id is None
    assert result.owner_id == "runtime-xyz"


def test_gateway_is_published_only_after_hydration_commit() -> None:
    world = World(uuid4(), account_count=1)
    world.failing_hydrate.add(world.accounts[0].id)

    _bootstrap(world)

    assert all(gateway.published is False for gateway in world.gateways)


def test_restart_with_new_owner_acquires_new_token_and_rehydrates() -> None:
    world = World(uuid4(), account_count=1)
    account = world.accounts[0]
    first = _bootstrap(world, owner_id="boot-1")
    second = _bootstrap(world, owner_id="boot-2")

    assert int(first.active_accounts[account.id].fencing_token) == 1
    assert int(second.active_accounts[account.id].fencing_token) == 2
    assert any(a["action"] == "bot.runtime.lease_acquired" for a in world.audits)


def test_runtime_owner_id_is_unique_per_process_boot() -> None:
    owners = {new_runtime_owner_id() for _ in range(50)}

    assert len(owners) == 50
    assert all(0 < len(owner) <= 120 for owner in owners)


def test_registry_tracks_bot_statuses_and_aggregate_counts_without_ids() -> None:
    world = World(uuid4(), account_count=3)
    ready, broken, standby = world.accounts
    world.failing_hydrate.add(broken.id)
    world.held_elsewhere.add(world.bots[standby.id])
    result = _bootstrap(world)

    registry = BotRuntimeRegistry.from_bootstrap(result)
    summary = registry.summary()

    assert summary["total"] == 3
    assert summary["ready"] == 1
    assert summary["recovery_required"] == 1
    assert summary["standby"] == 1
    assert summary["blocked_by_workspace"] == 0
    assert registry.status() == "degraded"
    registry.mark_standby(ready.id, "STALE_FENCING_TOKEN")
    assert registry.summary()["ready"] == 0
    assert registry.entry(ready.id).reason_code == "STALE_FENCING_TOKEN"
    assert str(ready.id) not in str(summary)
