from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models import BotGrant, BotInstance, UserMembership
from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role


class Session:
    def __init__(self, result=None):
        self.result = result
        self.queries = []

    async def scalar(self, query):
        self.queries.append(str(query))
        return self.result


def principal(role):
    return Principal(uuid4(), uuid4(), uuid4(), frozenset({role}), datetime.now(UTC))


@pytest.mark.asyncio
async def test_operator_requires_active_permission_scoped_to_bot_and_workspace() -> None:
    actor = principal(Role.OPERATOR)
    session = Session()
    repository = BotGrantRepository(session)
    with pytest.raises(PermissionError):
        await repository.require(actor, bot_id=uuid4(), permission=BotPermission.RUNTIME_PAUSE)
    assert all(
        field in session.queries[0]
        for field in ("workspace_id", "bot_id", "membership_id", "permission", "revoked_at IS NULL")
    )
    session.result = SimpleNamespace(id=uuid4())
    await repository.require(actor, bot_id=uuid4(), permission=BotPermission.RUNTIME_PAUSE)


@pytest.mark.asyncio
async def test_grant_does_not_expand_role_and_workspace_admin_bypass_is_limited() -> None:
    session = Session(SimpleNamespace(id=uuid4()))
    repository = BotGrantRepository(session)
    with pytest.raises(PermissionError):
        await repository.require(
            principal(Role.OPERATOR), bot_id=uuid4(), permission=BotPermission.CONFIG_WRITE
        )
    with pytest.raises(PermissionError):
        await repository.require(
            principal(Role.STRATEGY_ADMIN), bot_id=uuid4(), permission=BotPermission.RUNTIME_RESUME
        )
    assert not session.queries
    await repository.require(
        principal(Role.OWNER), bot_id=uuid4(), permission=BotPermission.CONFIG_PUBLISH
    )
    assert not session.queries


@pytest.mark.asyncio
async def test_grant_revoke_reissue_keeps_history_and_rejects_non_owner() -> None:
    actor = principal(Role.OWNER)
    target = Principal(
        uuid4(), uuid4(), actor.workspace_id, frozenset({Role.OPERATOR}), datetime.now(UTC)
    )
    bot_id = uuid4()

    class GrantSession:
        def __init__(self):
            self.rows = []

        async def scalar(self, query):
            entity = query.column_descriptions[0]["entity"]
            if entity is BotInstance:
                return SimpleNamespace(id=bot_id)
            if entity is UserMembership:
                return SimpleNamespace(id=target.membership_id)
            assert entity is BotGrant
            return next((row for row in self.rows if row.revoked_at is None), None)

        def add(self, row):
            self.rows.append(row)

        async def flush(self):
            return None

    session = GrantSession()
    repository = BotGrantRepository(session)
    args = {
        "bot_id": bot_id,
        "membership_id": target.membership_id,
        "permission": BotPermission.READ,
    }
    first = await repository.grant(actor, **args)
    assert await repository.grant(actor, **args) is first
    await repository.require(target, bot_id=bot_id, permission=BotPermission.READ)
    assert await repository.revoke(actor, **args)
    assert not await repository.revoke(actor, **args)
    with pytest.raises(PermissionError, match="Thiếu grant"):
        await repository.require(target, bot_id=bot_id, permission=BotPermission.READ)
    second = await repository.grant(actor, **args)
    assert first.id != second.id
    assert first.revoked_at is not None
    assert second.revoked_at is None
    assert len(session.rows) == 2
    with pytest.raises(PermissionError, match="owner"):
        await repository.grant(target, **args)


@pytest.mark.asyncio
async def test_mixed_roles_do_not_borrow_workspace_bypass_for_operator_permission() -> None:
    """strategy_admin+operator không được pause mọi bot chỉ nhờ quyền runtime của operator."""
    actor = Principal(
        uuid4(),
        uuid4(),
        uuid4(),
        frozenset({Role.STRATEGY_ADMIN, Role.OPERATOR}),
        datetime.now(UTC),
    )
    session = Session()
    repository = BotGrantRepository(session)
    with pytest.raises(PermissionError, match="Thiếu grant"):
        await repository.require(actor, bot_id=uuid4(), permission=BotPermission.RUNTIME_PAUSE)
    assert session.queries
    session.queries.clear()
    await repository.require(actor, bot_id=uuid4(), permission=BotPermission.CONFIG_WRITE)
    assert not session.queries
