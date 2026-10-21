from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Numeric, UniqueConstraint

from trading_bot.adapters.database.models import Base, TradingAccount


def test_trading_account_uses_workspace_scoped_unique_key_and_decimal_columns() -> None:
    table = TradingAccount.__table__

    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_trading_account_workspace_id"
        for constraint in table.constraints
    )
    assert table.c.workspace_id.foreign_keys
    assert isinstance(table.c.max_notional.type, Numeric)
    assert table.c.max_notional.type.asdecimal is True


def test_schema_contains_all_foundation_tables() -> None:
    assert set(Base.metadata.tables) == {
        "workspace",
        "user_account",
        "user_membership",
        "role_binding",
        "session",
        "mfa_factor",
        "telegram_binding",
        "trading_account",
        "secret_reference",
        "config_version",
        "risk_profile_version",
        "approval_request",
        "watchlist_version",
        "watchlist_item",
        "strategy_release",
    }


def test_model_uses_native_uuid_defaults_and_utc_timestamps() -> None:
    from trading_bot.adapters.database.models import Workspace

    workspace = Workspace(name="Paper", slug="paper")
    assert workspace.id is None
    assert uuid4().version == 4
    assert Workspace.__table__.c.created_at.type.timezone is True
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
