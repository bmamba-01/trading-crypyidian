from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from tests.unit.test_paper_execution import NOW, _bar, _plan
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.application.paper_execution_profile import (
    default_paper_execution_profile_catalog,
    resolve_paper_execution_profile,
)
from trading_bot.application.paper_runtime_bootstrap import (
    PaperAccountRuntimeStatus,
    PaperRuntimeBootstrapService,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperExecutionGateway


def _published(workspace_id: UUID) -> PublishedWorkspaceSettings:
    return PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=uuid4(),
        settings_hash="a" * 64,
        settings=WorkspaceSettings.model_validate(valid_payload()),
        effective_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


class _FakeUnitOfWork:
    def __init__(self, _sessions: object, *, workspace_id: UUID) -> None:
        self.workspace_id = workspace_id
        self.session = object()
        self.callbacks: list[Callable[[], None]] = []
        self.committed = False

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        if exc_type is None:
            self.committed = True
            for callback in self.callbacks:
                callback()

    async def bind_account(self, account_id, *, allow_switch=False):
        return account_id

    def after_commit(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)


class _FakeAccountRepository:
    def __init__(
        self,
        accounts: tuple[SimpleNamespace, ...],
        seeds: dict[UUID, SimpleNamespace],
    ) -> None:
        self.accounts = accounts
        self.seeds = seeds

    async def list_enabled(self, *, workspace_id: UUID) -> list[SimpleNamespace]:
        return [account for account in self.accounts if account.workspace_id == workspace_id]

    async def load_seed(self, *, workspace_id: UUID, account_id: UUID) -> SimpleNamespace | None:
        seed = self.seeds.get(account_id)
        if seed is None or seed.workspace_id != workspace_id:
            return None
        return seed


class _FakeSettingsRepository:
    def __init__(self, published: PublishedWorkspaceSettings | None) -> None:
        self.published = published

    async def load_effective_config(
        self, *, workspace_id: UUID, as_of: datetime
    ) -> PublishedWorkspaceSettings | None:
        del as_of
        if self.published is None or self.published.workspace_id != workspace_id:
            return None
        return self.published

    async def lock_workspace(self, _workspace_id: UUID) -> None:
        return None


class _FakeGateway:
    def __init__(self, policy: object) -> None:
        self.policy = policy
        self.published = False


class _FakeCoordinator:
    def __init__(self, gateway: _FakeGateway, failing_ids: set[UUID]) -> None:
        self.gateway = gateway
        self.failing_ids = failing_ids

    async def hydrate_account(self, uow: _FakeUnitOfWork, *, account_id: UUID) -> None:
        if account_id in self.failing_ids:
            raise RuntimeError("synthetic checkpoint recovery failure")
        uow.after_commit(lambda: setattr(self.gateway, "published", True))


def _account(workspace_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), workspace_id=workspace_id, mode="PAPER", status="ENABLED")


def _seed(workspace_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(workspace_id=workspace_id, quote_asset="USDT")


def _service(
    workspace_id: UUID,
    accounts: tuple[SimpleNamespace, ...],
    *,
    failing_ids: set[UUID] | None = None,
    published: PublishedWorkspaceSettings | None = None,
    extra_accounts: tuple[SimpleNamespace, ...] = (),
    seed_quote_overrides: dict[UUID, str] | None = None,
    settings_unavailable: bool = False,
) -> tuple[PaperRuntimeBootstrapService, list[_FakeGateway]]:
    seeds = {account.id: _seed(account.workspace_id) for account in (*accounts, *extra_accounts)}
    for account_id, quote_asset in (seed_quote_overrides or {}).items():
        seeds[account_id].quote_asset = quote_asset
    account_repository = _FakeAccountRepository((*accounts, *extra_accounts), seeds)
    settings_repository = _FakeSettingsRepository(
        None if settings_unavailable else (published or _published(workspace_id))
    )
    gateways: list[_FakeGateway] = []

    def make_gateway(policy: object) -> _FakeGateway:
        gateway = _FakeGateway(policy)
        gateways.append(gateway)
        return gateway

    service = PaperRuntimeBootstrapService(
        sessions=object(),
        workspace_id=workspace_id,
        unit_of_work_factory=_FakeUnitOfWork,
        account_repository_factory=lambda _session: account_repository,
        settings_repository_factory=lambda _session: settings_repository,
        gateway_factory=make_gateway,
        coordinator_factory=lambda gateway, _ledger, _checkpoints: _FakeCoordinator(
            gateway, failing_ids or set()
        ),
    )
    return service, gateways


@pytest.mark.asyncio
async def test_bootstrap_publishes_all_account_gateways_only_after_hydration_commit() -> None:
    workspace_id = uuid4()
    accounts = (_account(workspace_id), _account(workspace_id))
    service, gateways = _service(workspace_id, accounts)

    result = await service.bootstrap()

    assert result.ready is True
    assert set(result.active_accounts) == {account.id for account in accounts}
    assert all(gateway.published for gateway in gateways)
    assert all(
        result.account_states[account.id].status is PaperAccountRuntimeStatus.READY
        for account in accounts
    )


@pytest.mark.asyncio
async def test_strategy_only_publish_restarts_with_open_position_and_pinned_fill_policy() -> None:
    workspace_id = uuid4()
    account = _account(workspace_id)
    previous_settings = _published(workspace_id)
    previous_profile = resolve_paper_execution_profile(
        previous_settings, default_paper_execution_profile_catalog()
    )
    previous_gateway = PaperExecutionGateway(previous_profile.fill_policy)
    order = previous_gateway.submit(
        _plan(), "settings-v1-open-position", submitted_at=NOW, eligible_after=NOW
    )
    previous_gateway.apply_market_event(_bar("settings-v1-entry", 1))
    previous_snapshot = previous_gateway.export_state()
    assert previous_gateway.position(order.intent_id) is not None

    changed_payload = previous_settings.settings.model_dump(mode="python")
    changed_payload["strategy"]["default_release"] = "h02-v1"
    next_settings = PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=uuid4(),
        settings_hash="b" * 64,
        settings=WorkspaceSettings.model_validate(changed_payload),
        effective_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    settings_repository = _FakeSettingsRepository(next_settings)
    account_repository = _FakeAccountRepository((account,), {account.id: _seed(workspace_id)})
    recovered_gateways: list[PaperExecutionGateway] = []

    def make_gateway(policy: object) -> PaperExecutionGateway:
        gateway = PaperExecutionGateway(policy)  # type: ignore[arg-type]
        recovered_gateways.append(gateway)
        return gateway

    class RecoveringCoordinator:
        def __init__(self, gateway: PaperExecutionGateway) -> None:
            self.gateway = gateway

        async def hydrate_account(self, uow: _FakeUnitOfWork, *, account_id: UUID) -> None:
            del account_id
            self.gateway.replace_state(previous_snapshot)
            uow.after_commit(lambda: None)

    service = PaperRuntimeBootstrapService(
        sessions=object(),
        workspace_id=workspace_id,
        unit_of_work_factory=_FakeUnitOfWork,
        account_repository_factory=lambda _session: account_repository,
        settings_repository_factory=lambda _session: settings_repository,
        gateway_factory=make_gateway,
        coordinator_factory=lambda gateway, _ledger, _checkpoints: RecoveringCoordinator(gateway),
    )

    result = await service.bootstrap()

    assert result.ready is True
    active = result.active_accounts[account.id]
    assert active.execution_profile.settings_version_id == next_settings.settings_version_id
    assert active.execution_profile.profile_hash == previous_profile.profile_hash
    assert active.gateway.position(order.intent_id) == previous_gateway.position(order.intent_id)


@pytest.mark.asyncio
async def test_one_account_recovery_failure_blocks_every_gateway_publication() -> None:
    workspace_id = uuid4()
    accounts = (_account(workspace_id), _account(workspace_id))
    service, gateways = _service(workspace_id, accounts, failing_ids={accounts[1].id})

    result = await service.bootstrap()

    assert result.ready is False
    assert result.active_accounts == {}
    assert all(gateway.published is False for gateway in gateways)
    assert result.account_states[accounts[1].id].status is (
        PaperAccountRuntimeStatus.RECOVERY_REQUIRED
    )
    assert result.account_states[accounts[0].id].status is (
        PaperAccountRuntimeStatus.BLOCKED_BY_WORKSPACE
    )


@pytest.mark.asyncio
async def test_bootstrap_rejects_account_repository_result_outside_deployment_workspace() -> None:
    workspace_id = uuid4()
    foreign_account = _account(uuid4())
    service, gateways = _service(workspace_id, (), extra_accounts=(foreign_account,))

    result = await service.bootstrap()

    assert result.ready is False
    assert result.active_accounts == {}
    assert gateways == []
    assert result.error_code == "NO_ENABLED_PAPER_ACCOUNTS"


@pytest.mark.asyncio
async def test_bootstrap_fails_closed_when_effective_settings_are_unavailable() -> None:
    workspace_id = uuid4()
    account = _account(workspace_id)
    service, gateways = _service(workspace_id, (account,), settings_unavailable=True)

    result = await service.bootstrap()

    assert result.ready is False
    assert result.active_accounts == {}
    assert gateways == []
    assert result.account_states[account.id].status is (PaperAccountRuntimeStatus.RECOVERY_REQUIRED)


@pytest.mark.asyncio
async def test_bootstrap_rejects_account_seed_quote_asset_mismatch() -> None:
    workspace_id = uuid4()
    account = _account(workspace_id)
    service, gateways = _service(
        workspace_id, (account,), seed_quote_overrides={account.id: "USDC"}
    )

    result = await service.bootstrap()

    assert result.ready is False
    assert result.active_accounts == {}
    assert gateways == []
    assert result.account_states[account.id].status is (PaperAccountRuntimeStatus.RECOVERY_REQUIRED)
