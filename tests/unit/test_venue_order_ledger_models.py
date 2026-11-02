"""Hợp đồng schema cho venue order ledger bền vững."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import String, Uuid

from trading_bot.adapters.database.models import (
    RiskReservationRecord,
    VenueOrderIntentRecord,
    VenueOrderTransitionRecord,
)
from trading_bot.adapters.database.repositories.venue_order_ledger import (
    VenueOrderLedgerConflict,
    VenueOrderLedgerRepository,
)
from trading_bot.domain.execution.venue_orders import VenueOrderIntent


def test_venue_order_intent_schema_scopes_idempotency_and_reservation() -> None:
    table = VenueOrderIntentRecord.__table__
    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    foreign_keys = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "ForeignKeyConstraint"
    }

    assert "mode" in table.c
    assert all(isinstance(table.c[key].type, String) for key in ("mode", "state"))
    assert unique_constraints["uq_venue_order_intent_workspace_account_idempotency"] == (
        "workspace_id",
        "account_id",
        "idempotency_key",
    )
    assert unique_constraints["uq_venue_order_intent_workspace_account_client_id"] == (
        "workspace_id",
        "account_id",
        "client_order_id",
    )
    assert foreign_keys["fk_venue_order_intent_reservation"] == (
        "workspace_id",
        "bot_id",
        "account_id",
        "reservation_id",
    )
    assert foreign_keys["fk_venue_order_intent_bot_account"] == (
        "workspace_id",
        "bot_id",
        "account_id",
    )
    assert foreign_keys["fk_venue_order_intent_settings"] == (
        "workspace_id",
        "settings_version_id",
    )
    assert isinstance(table.c.settings_version_id.type, Uuid)
    assert table.c.last_event_at.nullable is True
    retry_deadline = next(
        constraint.sqltext.text
        for constraint in table.constraints
        if constraint.name.endswith("venue_order_intent_retry_deadline_consistent")
    )
    assert "retry_not_before IS NOT NULL" in retry_deadline
    reservation_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in RiskReservationRecord.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("workspace_id", "account_id", "id") in reservation_uniques


def test_venue_order_transition_schema_is_append_only_and_ordered() -> None:
    table = VenueOrderTransitionRecord.__table__
    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert unique_constraints["uq_venue_order_transition_sequence"] == (
        "workspace_id",
        "intent_id",
        "sequence",
    )
    assert unique_constraints["uq_venue_order_transition_event_key"] == (
        "workspace_id",
        "event_key",
    )
    assert {"actor_id", "source", "occurred_at", "evidence_hash"}.issubset(table.c.keys())


def test_migration_preserves_paper_reconciliation_and_fails_closed_for_venue_unknown() -> None:
    migration = (
        Path(__file__).parents[2] / "migrations/versions/a7c9e2f4b6d8_wp03_venue_order_ledger.py"
    ).read_text(encoding="utf-8")

    assert "NEW.resolution_reason = 'ORDER_ABSENT'" in migration
    assert "NEW.resolution_reason = 'OPEN_RISK_RECORDED'" in migration
    assert "WP03_VENUE_ORDER_RESERVATION_AUTHENTICATED_RECONCILIATION_REQUIRED" in migration
    assert "WP03_VENUE_ORDER_TRANSFER_NEEDS_OPEN_RISK_RECORD" in migration
    assert "WP03_VENUE_ORDER_AUTHENTICATED_RECONCILIATION_REQUIRED" in migration
    assert (
        "WHEN 'UNKNOWN' THEN NEW.state IN (\n                    'UNKNOWN', 'ACKNOWLEDGED'"
        in migration
    )


@pytest.mark.asyncio
async def test_repository_rejects_non_uuid_settings_reference_before_database_write() -> None:
    workspace_id = uuid4()
    account_id = uuid4()
    reservation_id = uuid4()
    intent = VenueOrderIntent(
        intent_id=uuid4(),
        workspace_id=str(workspace_id),
        account_id=str(account_id),
        idempotency_key="venue-intent-key",
        reservation_id=reservation_id,
        plan_hash="a" * 64,
        settings_version="settings-v4",
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.01"),
        client_order_id="demo-client-order",
        created_at=datetime.now(UTC),
    )
    repository = VenueOrderLedgerRepository(None)  # type: ignore[arg-type]

    with pytest.raises(VenueOrderLedgerConflict, match="UUID settings đã publish"):
        await repository.create_intent(
            workspace_id=workspace_id,
            account_id=account_id,
            reservation_id=reservation_id,
            intent=intent,
            actor_id="runtime-test",
        )
