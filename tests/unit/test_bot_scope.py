from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.adapters.database.bot_scope import bind_account_bot
from trading_bot.application.outbox import UnitOfWork


class Session:
    def __init__(self, bot=None):
        self.info = {}
        self.bot = bot
        self.calls = []

    async def scalar(self, statement):
        self.calls.append(str(statement))
        return self.bot

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))

    async def begin(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_uow_sets_all_context_transaction_local_and_clears_missing_context() -> None:
    workspace, bot, member = uuid4(), uuid4(), uuid4()
    session = Session()
    async with UnitOfWork(
        lambda: session, workspace_id=workspace, bot_id=bot, actor_membership_id=member
    ):
        pass
    statement, parameters = session.calls[0]
    assert parameters == {
        "workspace_id": str(workspace),
        "bot_id": str(bot),
        "membership_id": str(member),
    }
    assert statement.count(", true)") == 3
    async with UnitOfWork(lambda: session, workspace_id=workspace):
        pass
    assert session.calls[1][1]["bot_id"] == ""
    assert session.calls[1][1]["membership_id"] == ""


@pytest.mark.asyncio
async def test_bind_rejects_missing_mapping_and_mismatched_explicit_bot() -> None:
    workspace, account, bot = uuid4(), uuid4(), uuid4()
    session = Session()
    with pytest.raises(ValueError, match="chưa có bot"):
        await bind_account_bot(session, workspace_id=workspace, account_id=account)
    session.bot = SimpleNamespace(id=bot)
    session.info["bot_id"] = uuid4()
    with pytest.raises(ValueError, match="khác scope"):
        await bind_account_bot(session, workspace_id=workspace, account_id=account)
    assert not any(isinstance(call, tuple) for call in session.calls)


@pytest.mark.asyncio
async def test_binding_reuses_same_account_and_bootstrap_switch_is_explicit() -> None:
    workspace, account, bot = uuid4(), uuid4(), uuid4()
    session = Session(SimpleNamespace(id=bot))
    assert await bind_account_bot(session, workspace_id=workspace, account_id=account) == bot
    call_count = len(session.calls)
    assert await bind_account_bot(session, workspace_id=workspace, account_id=account) == bot
    # Không lookup lại mapping nhưng luôn đặt lại GUC: savepoint rollback có thể đã reset nó.
    new_calls = session.calls[call_count:]
    assert len(new_calls) == 1
    assert isinstance(new_calls[0], tuple)
    assert new_calls[0][1] == {"bot_id": str(bot)}
    session.bot = SimpleNamespace(id=uuid4())
    with pytest.raises(ValueError, match="khác scope"):
        await bind_account_bot(session, workspace_id=workspace, account_id=uuid4())
    assert (
        await bind_account_bot(
            session, workspace_id=workspace, account_id=uuid4(), allow_switch=True
        )
        == session.bot.id
    )
