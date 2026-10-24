from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.paper_runtime_accounts import (
    PaperRuntimeAccountRepository,
)


class _FakeScalars:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, rows: tuple[object, ...] = ()) -> None:
        self.rows = rows
        self.statement = None

    async def scalars(self, statement: object) -> _FakeScalars:
        self.statement = statement
        return _FakeScalars(self.rows)


@pytest.mark.asyncio
async def test_account_enumerator_filters_deployment_workspace_paper_and_enabled() -> None:
    workspace_id = uuid4()
    account = object()
    session = _FakeSession((account,))
    repository = PaperRuntimeAccountRepository(session)  # type: ignore[arg-type]

    result = await repository.list_enabled(workspace_id=workspace_id)

    compiled = session.statement.compile(dialect=postgresql.dialect())
    params = compiled.params
    assert result == [account]
    assert "trading_account.workspace_id =" in str(compiled)
    assert "trading_account.mode =" in str(compiled)
    assert "trading_account.status =" in str(compiled)
    assert workspace_id in params.values()
    assert "PAPER" in params.values()
    assert "ENABLED" in params.values()


@pytest.mark.asyncio
async def test_seed_lookup_requires_both_workspace_and_account_ids() -> None:
    workspace_id = uuid4()
    account_id = uuid4()
    seed = object()
    session = _FakeSession()

    async def scalar(statement: object) -> object:
        session.statement = statement
        return seed

    session.scalar = scalar  # type: ignore[attr-defined]
    repository = PaperRuntimeAccountRepository(session)  # type: ignore[arg-type]

    result = await repository.load_seed(workspace_id=workspace_id, account_id=account_id)

    compiled = session.statement.compile(dialect=postgresql.dialect())
    assert result is seed
    assert "paper_account_seed.workspace_id =" in str(compiled)
    assert "paper_account_seed.trading_account_id =" in str(compiled)
    assert workspace_id in compiled.params.values()
    assert account_id in compiled.params.values()
