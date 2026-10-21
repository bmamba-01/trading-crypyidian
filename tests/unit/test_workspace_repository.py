from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories import WorkspaceRepository


def test_tenant_query_requires_workspace_id() -> None:
    with pytest.raises(TypeError):
        WorkspaceRepository.trading_accounts_statement()  # type: ignore[call-arg]


def test_tenant_query_filters_by_workspace_id() -> None:
    workspace_id = uuid4()

    statement = WorkspaceRepository.trading_accounts_statement(workspace_id)
    compiled = statement.compile(dialect=postgresql.dialect())

    assert "WHERE trading_account.workspace_id = " in str(compiled)
    assert workspace_id in compiled.params.values()
