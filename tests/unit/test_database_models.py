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
        "bot_instance",
        "bot_grant",
        "bot_config_version",
        "bot_config_request",
        "workspace",
        "user_account",
        "user_membership",
        "role_binding",
        "session",
        "mfa_factor",
        "telegram_binding",
        "trading_account",
        "paper_account_seed",
        "secret_reference",
        "config_version",
        "risk_profile_version",
        "approval_request",
        "watchlist_version",
        "watchlist_item",
        "strategy_release",
        "outbox_event",
        "consumer_receipt",
        "audit_event",
        "workspace_settings_version",
        "market_instrument",
        "market_raw_reference",
        "market_bar",
        "market_funding",
        "market_open_interest",
        "ingestion_cursor",
        "ingestion_gap",
        "trade_plan",
        "risk_decision",
        "risk_reservation",
        "feature_snapshot",
        "order_intent",
        "exchange_order",
        "fill",
        "position_event",
        "trade",
        "trade_cost",
        "backtest_run",
        "backtest_trial",
        "backtest_report",
        "backtest_scenario",
        "backtest_trade_outcome",
        "paper_execution_checkpoint",
    }


def test_risk_reservation_requires_persisted_decision_and_positive_initial_margin() -> None:
    table = Base.metadata.tables["risk_reservation"]
    foreign_keys = list(table.foreign_key_constraints)

    assert table.c.risk_decision_id.nullable is False
    assert table.c.risk_decision_id.foreign_keys
    assert isinstance(table.c.reserved_initial_margin.type, Numeric)
    assert table.c.reserved_initial_margin.type.asdecimal is True
    assert any(
        {element.parent.name for element in constraint.elements}
        == {"workspace_id", "account_id", "risk_decision_id"}
        for constraint in foreign_keys
    )
    assert any(
        getattr(constraint, "name", None)
        == "ck_risk_reservation_risk_reservation_initial_margin_positive"
        for constraint in table.constraints
    )


def test_workspace_settings_versions_are_tenant_scoped_and_store_versioned_hashes() -> None:
    table = Base.metadata.tables["workspace_settings_version"]

    assert table.c.workspace_id.foreign_keys
    assert table.c.settings_json.type.__class__.__name__ == "JSONB"
    assert table.c.settings_hash.type.length == 64
    assert table.c.revision.nullable is False
    assert any(
        isinstance(constraint, UniqueConstraint)
        and set(constraint.columns.keys()) == {"workspace_id", "version"}
        for constraint in table.constraints
    )


def test_model_uses_native_uuid_defaults_and_utc_timestamps() -> None:
    from trading_bot.adapters.database.models import Workspace

    workspace = Workspace(name="Paper", slug="paper")
    assert workspace.id is None
    assert uuid4().version == 4
    assert Workspace.__table__.c.created_at.type.timezone is True
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")


def test_authentication_models_store_csrf_mfa_freshness_and_totp_replay_state() -> None:
    session_columns = Base.metadata.tables["session"].c
    factor_columns = Base.metadata.tables["mfa_factor"].c

    assert session_columns.csrf_token_hash.nullable is True
    assert session_columns.mfa_verified_at.type.timezone is True
    assert factor_columns.last_used_counter.nullable is True


def test_audit_and_outbox_schema_have_tenant_keys_and_consumer_deduplication() -> None:
    outbox = Base.metadata.tables["outbox_event"]
    receipt = Base.metadata.tables["consumer_receipt"]
    audit = Base.metadata.tables["audit_event"]

    assert outbox.c.workspace_id.foreign_keys
    assert receipt.c.workspace_id.foreign_keys
    assert receipt.c.outbox_event_id.foreign_keys
    assert any(
        isinstance(constraint, UniqueConstraint)
        and set(constraint.columns.keys()) == {"workspace_id", "outbox_event_id", "consumer_name"}
        for constraint in receipt.constraints
    )
    assert audit.c.actor_membership_id.nullable is True
    assert audit.c.before_hash.type.length == 64
