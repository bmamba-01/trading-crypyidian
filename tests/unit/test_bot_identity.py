from uuid import uuid4

import pytest

from trading_bot.adapters.database.models import BotGrant, BotInstance
from trading_bot.auth.bot_policy import BotPermission, role_allows_bot_permission
from trading_bot.auth.policy import Role

ACCOUNT_SCOPED_TABLES = {
    "paper_account_seed",
    "config_version",
    "approval_request",
    "trade_plan",
    "risk_decision",
    "risk_reservation",
    "order_intent",
    "exchange_order",
    "fill",
    "position_event",
    "trade",
    "trade_cost",
    "paper_execution_checkpoint",
}


def test_every_account_scoped_record_has_bot_account_composite_fk() -> None:
    from trading_bot.adapters.database.models import Base

    for name in ACCOUNT_SCOPED_TABLES:
        table = Base.metadata.tables[name]
        account_column = (
            "trading_account_id"
            if name in {"paper_account_seed", "config_version", "approval_request"}
            else "account_id"
        )
        assert "bot_id" in table.columns, name
        assert table.c.bot_id.nullable is (name == "approval_request")
        assert any(
            tuple(column.name for column in constraint.columns)
            == ("workspace_id", "bot_id", account_column)
            and constraint.referred_table.name == "bot_instance"
            for constraint in table.foreign_key_constraints
        ), name


def test_every_bot_scope_has_index_for_rls_predicate_and_composite_fk() -> None:
    """PostgreSQL không tự tạo index cho cột FK phía con; RLS lọc theo workspace+bot."""
    from trading_bot.adapters.database.models import Base

    for name in ACCOUNT_SCOPED_TABLES:
        table = Base.metadata.tables[name]
        account_column = (
            "trading_account_id"
            if name in {"paper_account_seed", "config_version", "approval_request"}
            else "account_id"
        )
        assert any(
            index.name == f"ix_{name}_bot_scope"
            and tuple(column.name for column in index.columns)
            == ("workspace_id", "bot_id", account_column)
            for index in table.indexes
        ), name


def test_bot_model_binds_one_account_with_composite_identity() -> None:
    table = BotInstance.__table__
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("workspace_id", "trading_account_id") in unique_columns
    assert ("workspace_id", "id", "trading_account_id") in unique_columns
    assert "mode" not in table.columns
    assert (
        BotInstance(workspace_id=uuid4(), trading_account_id=uuid4(), name="PAPER").name == "PAPER"
    )


def test_bot_grant_has_partial_unique_active_permission() -> None:
    index = next(index for index in BotGrant.__table__.indexes if index.unique)
    assert tuple(column.name for column in index.columns) == (
        "workspace_id",
        "bot_id",
        "membership_id",
        "permission",
    )
    assert str(index.dialect_options["postgresql"]["where"]) == "revoked_at IS NULL"


def test_permission_strings_and_persisted_roles_are_case_sensitive() -> None:
    assert BotPermission.READ.value == "bot:read"
    assert Role.OWNER.value == "owner"
    with pytest.raises(ValueError, match="is not a valid"):
        BotPermission("bot:unknown")
    with pytest.raises(ValueError, match="is not a valid"):
        Role("OWNER")
    assert role_allows_bot_permission(frozenset({Role.OWNER}), BotPermission.CONFIG_PUBLISH)
    assert not role_allows_bot_permission(frozenset({Role.OPERATOR}), BotPermission.CONFIG_PUBLISH)
    assert role_allows_bot_permission(frozenset({Role.STRATEGY_ADMIN}), BotPermission.CONFIG_WRITE)
    assert not role_allows_bot_permission(
        frozenset({Role.STRATEGY_ADMIN}), BotPermission.RUNTIME_RESUME
    )


@pytest.mark.asyncio
async def test_create_for_account_resolves_trigger_mapping_without_app_insert() -> None:
    """Identity do trigger DB tạo; role ứng dụng không có quyền INSERT bot_instance."""
    from types import SimpleNamespace

    from trading_bot.adapters.database.repositories.bot_instances import BotInstanceRepository

    account = SimpleNamespace(
        id=uuid4(), workspace_id=uuid4(), name="PAPER", mode="PAPER", status="ENABLED"
    )

    class Session:
        def __init__(self, bot):
            self.results = [account, bot]
            self.added = []

        async def scalar(self, statement):
            return self.results.pop(0)

        def add(self, row):
            self.added.append(row)

        async def flush(self):
            return None

    missing = Session(None)
    with pytest.raises(ValueError, match="trigger"):
        await BotInstanceRepository(missing).create_for_account(account=account)
    assert not missing.added
    bot = SimpleNamespace(id=uuid4())
    found = Session(bot)
    assert await BotInstanceRepository(found).create_for_account(account=account) is bot
    assert not found.added
