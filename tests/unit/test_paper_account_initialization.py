from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.bot_scope_fakes import BotScopeSession
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.adapters.database.models import Base
from trading_bot.adapters.database.repositories.paper_accounts import (
    PaperAccountSeedConflict,
    PaperAccountSeedRepository,
)
from trading_bot.application.paper_account_initialization import (
    PaperAccountInitializationError,
    PaperAccountInitializationService,
)
from trading_bot.application.paper_execution_profile import (
    default_paper_execution_profile_catalog,
    resolve_paper_execution_profile,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


class SeedSession(BotScopeSession):
    def __init__(self, rows: list[object | None]) -> None:
        self.rows = iter(rows)
        self.statements: list[str] = []
        self.parameters: list[object] = []
        self.added: list[object] = []
        self.flush_count = 0

    async def scalar(self, statement: Any) -> object | None:
        if self.is_bot_query(statement):
            return self.bot_identity(statement)
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.rows)

    async def execute(self, statement: Any, params: object = None) -> SimpleNamespace:
        self.statements.append(str(statement))
        self.parameters.append(params)
        return SimpleNamespace()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1


class SeedUnitOfWork:
    def __init__(self, workspace_id: object, session: SeedSession) -> None:
        self.workspace_id = workspace_id
        self.session = session
        self.request_id = uuid4()
        self.audits: list[dict[str, Any]] = []
        self.events: list[object] = []

    def record_audit(self, **values: Any) -> None:
        self.audits.append(values)

    def enqueue(self, event: object) -> None:
        self.events.append(event)


def _published(
    *,
    workspace_id=None,
    settings_version_id=None,
    starting_equity: str = "10000",
    quote_asset: str = "USDT",
    effective_at: datetime | None = NOW - timedelta(minutes=1),
) -> PublishedWorkspaceSettings:
    payload = valid_payload()
    payload["paper_execution"]["starting_equity"] = starting_equity
    payload["paper_execution"]["quote_asset"] = quote_asset
    workspace_id = workspace_id or uuid4()
    return PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=settings_version_id or uuid4(),
        settings_hash="a" * 64,
        settings=WorkspaceSettings.model_validate(payload),
        effective_at=effective_at,
    )


def _profile(published: PublishedWorkspaceSettings):
    return resolve_paper_execution_profile(published, default_paper_execution_profile_catalog())


@pytest.mark.asyncio
async def test_initializes_synthetic_account_and_immutable_seed_once() -> None:
    workspace_id = uuid4()
    published = _published(workspace_id=workspace_id)
    session = SeedSession([None, None])
    uow = SeedUnitOfWork(workspace_id, session)

    result = await PaperAccountInitializationService().ensure_initialized(
        uow,
        published_settings=published,
        execution_profile=_profile(published),
        max_notional=Decimal("25000"),
        evaluated_at=NOW,
    )

    assert result.created is True
    assert result.account.workspace_id == workspace_id
    assert result.account.name == "paper-default"
    assert result.account.mode == "PAPER"
    assert result.account.status == "ENABLED"
    assert result.account.max_notional == Decimal("25000")
    assert result.seed.workspace_id == workspace_id
    assert result.seed.trading_account_id == result.account.id
    assert result.seed.quote_asset == "USDT"
    assert result.seed.initial_equity == Decimal("10000")
    assert result.seed.source_settings_version_id == published.settings_version_id
    assert result.seed.source_settings_hash == published.settings_hash
    assert not hasattr(result.account, "secret_reference_id")
    assert session.flush_count == 2
    assert len(session.added) == 2
    assert len(uow.audits) == len(uow.events) == 1
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "FOR UPDATE" in session.statements[1]


@pytest.mark.asyncio
async def test_retry_reuses_seed_and_does_not_reset_equity_for_later_settings_version() -> None:
    workspace_id, account_id = uuid4(), uuid4()
    original = _published(workspace_id=workspace_id)
    later = _published(
        workspace_id=workspace_id,
        starting_equity="10000",
        settings_version_id=uuid4(),
    )
    account = SimpleNamespace(
        id=account_id,
        workspace_id=workspace_id,
        name="paper-default",
        mode="PAPER",
        venue="binance_usdm",
        environment="local",
        status="ENABLED",
    )
    seed = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        trading_account_id=account_id,
        quote_asset="USDT",
        initial_equity=Decimal("10000"),
        source_settings_version_id=original.settings_version_id,
        source_settings_hash=original.settings_hash,
        idempotency_key=f"paper-seed:{account_id}",
    )
    session = SeedSession([account, seed])
    uow = SeedUnitOfWork(workspace_id, session)

    result = await PaperAccountInitializationService().ensure_initialized(
        uow,
        published_settings=later,
        execution_profile=_profile(later),
        max_notional=Decimal("30000"),
        evaluated_at=NOW,
    )

    assert result.created is False
    assert result.account.id == account_id
    assert result.seed is seed
    assert result.seed.initial_equity == Decimal("10000")
    assert result.seed.source_settings_version_id == original.settings_version_id
    assert not session.added
    assert session.flush_count == 0
    assert not uow.audits
    assert not uow.events
    assert "FOR UPDATE" not in session.statements[2]


@pytest.mark.asyncio
async def test_refuses_equity_or_quote_change_instead_of_mutating_seed() -> None:
    workspace_id, account_id = uuid4(), uuid4()
    published = _published(workspace_id=workspace_id, starting_equity="12000")
    account = SimpleNamespace(
        id=account_id,
        workspace_id=workspace_id,
        name="paper-default",
        mode="PAPER",
        venue="binance_usdm",
        environment="local",
        status="ENABLED",
    )
    existing_seed = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        trading_account_id=account_id,
        quote_asset="USDT",
        initial_equity=Decimal("10000"),
        source_settings_version_id=uuid4(),
        source_settings_hash="b" * 64,
        idempotency_key=f"paper-seed:{account_id}",
    )
    session = SeedSession([account, existing_seed])
    uow = SeedUnitOfWork(workspace_id, session)

    with pytest.raises(PaperAccountSeedConflict, match="vốn khởi tạo"):
        await PaperAccountInitializationService().ensure_initialized(
            uow,
            published_settings=published,
            execution_profile=_profile(published),
            max_notional=Decimal("30000"),
            evaluated_at=NOW,
        )

    assert existing_seed.initial_equity == Decimal("10000")
    assert not session.added
    assert not uow.audits
    assert not uow.events


@pytest.mark.asyncio
async def test_service_rejects_foreign_settings_and_not_yet_effective_settings() -> None:
    workspace_id = uuid4()
    foreign = _published(workspace_id=uuid4())
    session = SeedSession([])
    uow = SeedUnitOfWork(workspace_id, session)
    service = PaperAccountInitializationService()

    with pytest.raises(PaperAccountInitializationError, match="Workspace"):
        await service.ensure_initialized(
            uow,
            published_settings=foreign,
            execution_profile=_profile(foreign),
            max_notional=Decimal("1000"),
            evaluated_at=NOW,
        )
    assert not session.statements

    future = _published(workspace_id=workspace_id, effective_at=NOW + timedelta(minutes=1))
    with pytest.raises(PaperAccountInitializationError, match="hiệu lực"):
        await service.ensure_initialized(
            uow,
            published_settings=future,
            execution_profile=_profile(future),
            max_notional=Decimal("1000"),
            evaluated_at=NOW,
        )
    assert not session.statements


def test_paper_account_seed_schema_is_tenant_scoped_and_one_per_account() -> None:
    table = Base.metadata.tables["paper_account_seed"]

    assert table.c.initial_equity.type.precision == 38
    assert table.c.initial_equity.type.scale == 18
    assert any(
        constraint.name == "uq_paper_account_seed_workspace_account"
        for constraint in table.constraints
    )
    assert any(
        constraint.name == "fk_paper_account_seed_account" for constraint in table.constraints
    )
    assert any(
        constraint.name == "fk_paper_account_seed_settings" for constraint in table.constraints
    )


@pytest.mark.asyncio
async def test_repository_rejects_disabled_or_non_paper_existing_account() -> None:
    workspace_id, account_id = uuid4(), uuid4()
    published = _published(workspace_id=workspace_id)
    account = SimpleNamespace(
        id=account_id,
        workspace_id=workspace_id,
        name="paper-default",
        mode="DEMO",
        venue="binance_usdm",
        environment="local",
        status="ENABLED",
    )
    session = SeedSession([account])

    with pytest.raises(PaperAccountSeedConflict, match="PAPER"):
        await PaperAccountSeedRepository(session).ensure_seed(
            workspace_id=workspace_id,
            account_name="paper-default",
            quote_asset="USDT",
            initial_equity=Decimal("10000"),
            max_notional=Decimal("20000"),
            source_settings_version_id=published.settings_version_id,
            source_settings_hash=published.settings_hash,
        )  # type: ignore[arg-type]
