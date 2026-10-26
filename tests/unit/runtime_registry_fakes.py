"""Fake dùng chung cho unit test bootstrap theo bot, registry và supervisor (MB05.5)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from tests.unit.test_workspace_settings import valid_payload

from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.runtime_control import (
    FencingToken,
    LeaseGrant,
    RuntimeControlError,
)

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"synthetic sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def database_error(sqlstate: str) -> Exception:
    """DBAPIError giả mang SQLSTATE như psycopg (55P03 lock_timeout, 57014 statement_timeout)."""
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT 1", {}, _PgError(sqlstate))


def published_settings(workspace_id: UUID) -> PublishedWorkspaceSettings:
    return PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=uuid4(),
        settings_hash="a" * 64,
        settings=WorkspaceSettings.model_validate(valid_payload()),
        effective_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


class World:
    """Trạng thái giả của một workspace: account, bot, lease, state, audit."""

    def __init__(self, workspace_id: UUID, account_count: int = 2) -> None:
        self.workspace_id = workspace_id
        self.accounts = [
            SimpleNamespace(id=uuid4(), workspace_id=workspace_id, mode="PAPER", status="ENABLED")
            for _ in range(account_count)
        ]
        self.bots = {account.id: uuid4() for account in self.accounts}
        self.seeds = {
            account.id: SimpleNamespace(workspace_id=workspace_id, quote_asset="USDT")
            for account in self.accounts
        }
        self.published: PublishedWorkspaceSettings | None = published_settings(workspace_id)
        self.held_elsewhere: set[UUID] = set()
        self.tokens: dict[UUID, int] = {}
        self.owners: dict[UUID, str] = {}
        self.failing_hydrate: set[UUID] = set()
        self.states: list[dict[str, Any]] = []
        self.audits: list[dict[str, Any]] = []
        self.uows: list[FakeUnitOfWork] = []
        self.config_loads: list[UUID] = []
        self.bot_configs: dict[UUID, object | None] = {}
        self.gateways: list[FakeGateway] = []
        self.settings_unavailable = False
        # (site, key) -> [số lần còn lỗi, sqlstate]; site: "l1", "acquire", "hydrate".
        self.transient: dict[tuple[str, object], list[Any]] = {}

    def fail_transient(self, site: str, key: object, times: int = 1, sqlstate: str = "55P03"):
        self.transient[(site, key)] = [times, sqlstate]

    def maybe_fail(self, site: str, key: object) -> None:
        entry = self.transient.get((site, key))
        if entry is not None and entry[0] > 0:
            entry[0] -= 1
            raise database_error(entry[1])

    # --- factories ---------------------------------------------------------------------
    def unit_of_work(self, _sessions: object, *, workspace_id: UUID, bot_id=None, **_: object):
        uow = FakeUnitOfWork(self, workspace_id=workspace_id, bot_id=bot_id)
        self.uows.append(uow)
        return uow

    def account_repository(self, _session: object) -> FakeAccountRepository:
        return FakeAccountRepository(self)

    def settings_repository(self, _session: object) -> FakeSettingsRepository:
        return FakeSettingsRepository(self)

    def lease(self, _session: object) -> FakeLease:
        return FakeLease(self)

    def state_repository(self, _session: object) -> FakeStateRepository:
        return FakeStateRepository(self)

    def gateway(self, policy: object) -> FakeGateway:
        gateway = FakeGateway(policy)
        self.gateways.append(gateway)
        return gateway

    def coordinator(self, gateway: FakeGateway, _ledger: object, _checkpoints: object):
        return FakeCoordinator(self, gateway)

    async def bot_config_loader(self, *, uow: Any, account_id: UUID, **_: object) -> object | None:
        self.config_loads.append(account_id)
        return self.bot_configs.get(account_id)


class FakeUnitOfWork:
    def __init__(self, world: World, *, workspace_id: UUID, bot_id: UUID | None) -> None:
        self.world = world
        self.workspace_id = workspace_id
        self.bot_id = bot_id
        self.session = SimpleNamespace(info={})
        self.bound: list[UUID] = []
        self.allow_switch_calls: list[bool] = []
        self.callbacks: list[Callable[[], None]] = []
        self.pending_states: list[dict[str, Any]] = []
        self.pending_audits: list[dict[str, Any]] = []
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        if exc_type is None:
            self.committed = True
            self.world.states.extend(self.pending_states)
            self.world.audits.extend(self.pending_audits)
            for callback in self.callbacks:
                callback()
        else:
            self.rolled_back = True

    async def bind_account(self, account_id: UUID, *, allow_switch: bool = False) -> UUID:
        self.allow_switch_calls.append(allow_switch)
        self.bound.append(account_id)
        bot_id = self.world.bots[account_id]
        if self.bot_id is not None and self.bot_id != bot_id and not allow_switch:
            raise ValueError("Account khác scope bot của transaction.")
        self.bot_id = bot_id
        return bot_id

    def after_commit(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)

    def record_audit(self, **values: Any) -> None:
        self.pending_audits.append(values)


class FakeAccountRepository:
    def __init__(self, world: World) -> None:
        self.world = world

    async def list_enabled(self, *, workspace_id: UUID) -> list[SimpleNamespace]:
        return [a for a in self.world.accounts if a.workspace_id == workspace_id]

    async def load_seed(self, *, workspace_id: UUID, account_id: UUID) -> SimpleNamespace | None:
        seed = self.world.seeds.get(account_id)
        return seed if seed is not None and seed.workspace_id == workspace_id else None


class FakeSettingsRepository:
    def __init__(self, world: World) -> None:
        self.world = world

    async def lock_workspace(self, _workspace_id: UUID) -> None:
        self.world.maybe_fail("l1", None)

    async def load_effective_config(self, *, workspace_id: UUID, **_: object):
        if self.world.settings_unavailable or self.world.published is None:
            return None
        return self.world.published


class FakeLease:
    def __init__(self, world: World) -> None:
        self.world = world

    async def database_now(self) -> datetime:
        return NOW

    async def acquire_grant(
        self, workspace_id: UUID, bot_id: UUID, owner_id: str, expires_at: datetime
    ) -> LeaseGrant:
        del workspace_id
        self.world.maybe_fail("acquire", bot_id)
        if bot_id in self.world.held_elsewhere:
            raise RuntimeControlError("LEASE_HELD_ELSEWHERE")
        previous_owner = self.world.owners.get(bot_id)
        token = self.world.tokens.get(bot_id, 0)
        rehydrate = previous_owner != owner_id
        if rehydrate:
            token += 1
        self.world.tokens[bot_id] = token
        self.world.owners[bot_id] = owner_id
        return LeaseGrant(owner_id, FencingToken(token), NOW, expires_at, rehydrate)

    async def renew(
        self,
        workspace_id: UUID,
        bot_id: UUID,
        owner_id: str,
        fencing_token: FencingToken,
        expires_at: datetime,
    ) -> LeaseGrant:
        del workspace_id
        if self.world.owners.get(bot_id) != owner_id or self.world.tokens.get(bot_id) != int(
            fencing_token
        ):
            raise RuntimeControlError("STALE_FENCING_TOKEN")
        return LeaseGrant(owner_id, fencing_token, NOW, expires_at, False)

    async def release_if_owner(
        self, workspace_id: UUID, bot_id: UUID, owner_id: str, fencing_token: FencingToken
    ) -> bool:
        del workspace_id, fencing_token
        if self.world.owners.get(bot_id) != owner_id:
            return False
        self.world.owners[bot_id] = "released"
        return True


class FakeStateRepository:
    def __init__(self, world: World) -> None:
        self.world = world

    async def upsert(self, **values: Any) -> SimpleNamespace:
        uow = self.world.uows[-1]
        uow.pending_states.append(values)
        return SimpleNamespace(**values)

    async def get(self, **_: object) -> None:
        return None


class FakeGateway:
    def __init__(self, policy: object) -> None:
        self.policy = policy
        self.published = False
        self.entries_paused = False

    def open_orders(self) -> tuple[()]:
        return ()

    def open_positions(self) -> tuple[()]:
        return ()


class FakeCoordinator:
    def __init__(self, world: World, gateway: FakeGateway) -> None:
        self.world = world
        self.gateway = gateway

    async def hydrate_account(self, uow: FakeUnitOfWork, *, account_id: UUID) -> None:
        self.world.maybe_fail("hydrate", account_id)
        if account_id in self.world.failing_hydrate:
            from trading_bot.application.paper_lifecycle import PaperLifecycleConflict

            raise PaperLifecycleConflict("synthetic checkpoint hash mismatch")
        uow.after_commit(lambda: setattr(self.gateway, "published", True))


def lease_expiry(ttl: timedelta = timedelta(seconds=30)) -> datetime:
    return NOW + ttl
