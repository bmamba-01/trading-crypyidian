from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import ProgrammingError

from trading_bot.adapters.database.repositories.admin_lifecycle_guard import (
    AdminLifecycleGuardRepository,
    AdminLifecycleScopeDenied,
)


class _Result:
    def mappings(self):
        return self

    def one(self):
        return {
            "bot_exists": True,
            "open_positions": 2,
            "open_orders": 1,
            "held_reservations": 3,
        }


@pytest.mark.asyncio
async def test_repository_calls_only_the_scoped_aggregate_function() -> None:
    session = SimpleNamespace(execute=AsyncMock(return_value=_Result()))
    repository = AdminLifecycleGuardRepository(session)  # type: ignore[arg-type]
    workspace_id, bot_id = uuid4(), uuid4()

    result = await repository.load_snapshot(workspace_id=workspace_id, bot_id=bot_id)

    statement, parameters = session.execute.await_args.args
    assert "wp06_admin_bot_lifecycle_snapshot" in str(statement)
    assert "FROM trade" not in str(statement)
    assert "FROM order_intent" not in str(statement)
    assert parameters == {"workspace_id": workspace_id, "bot_id": bot_id}
    assert (
        result.bot_exists,
        result.open_positions,
        result.open_orders,
        result.held_reservations,
    ) == (True, 2, 1, 3)


@pytest.mark.asyncio
async def test_repository_maps_only_known_database_scope_denials() -> None:
    class ScopeDeniedError(Exception):
        diag = SimpleNamespace(message_primary="WP06_ADMIN_LIFECYCLE_OWNER_REQUIRED")

    session = SimpleNamespace(
        execute=AsyncMock(side_effect=ProgrammingError("SELECT lifecycle", {}, ScopeDeniedError()))
    )
    repository = AdminLifecycleGuardRepository(session)  # type: ignore[arg-type]

    with pytest.raises(AdminLifecycleScopeDenied):
        await repository.load_snapshot(workspace_id=uuid4(), bot_id=uuid4())

    session.execute.side_effect = ProgrammingError(
        "SELECT lifecycle", {}, RuntimeError("database unavailable")
    )
    with pytest.raises(ProgrammingError, match="database unavailable"):
        await repository.load_snapshot(workspace_id=uuid4(), bot_id=uuid4())
