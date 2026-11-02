"""Sổ lệnh venue Demo và nhật ký chuyển trạng thái bất biến."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class VenueOrderIntentRecord(UuidPrimaryKey, CreatedAt, Base):
    """Intent venue bền vững; không chứa secret, chữ ký hay payload raw."""

    __tablename__ = "venue_order_intent"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "bot_id",
            "account_id",
            "id",
            name="uq_venue_order_intent_bot_account_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "idempotency_key",
            name="uq_venue_order_intent_workspace_account_idempotency",
        ),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "client_order_id",
            name="uq_venue_order_intent_workspace_account_client_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_venue_order_intent_bot_account",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id", "reservation_id"],
            [
                "risk_reservation.workspace_id",
                "risk_reservation.bot_id",
                "risk_reservation.account_id",
                "risk_reservation.id",
            ],
            name="fk_venue_order_intent_reservation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "settings_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
            name="fk_venue_order_intent_settings",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="RESTRICT",
            name="fk_venue_order_intent_workspace",
        ),
        CheckConstraint("mode = 'DEMO'", name="venue_order_intent_demo_only"),
        CheckConstraint("side IN ('BUY', 'SELL')", name="venue_order_intent_side_valid"),
        CheckConstraint("order_type IN ('MARKET', 'LIMIT')", name="venue_order_intent_type_valid"),
        CheckConstraint(
            "(order_type = 'MARKET' AND limit_price IS NULL) OR "
            "(order_type = 'LIMIT' AND limit_price > 0)",
            name="venue_order_intent_limit_price_valid",
        ),
        CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
            name="venue_order_intent_quantity_valid",
        ),
        CheckConstraint(
            "state IN ('CREATED', 'SUBMITTING', 'RETRYABLE', 'ACKNOWLEDGED', "
            "'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN')",
            name="venue_order_intent_state_valid",
        ),
        CheckConstraint(
            "(state = 'RETRYABLE' AND retry_not_before IS NOT NULL) OR "
            "(state <> 'RETRYABLE' AND retry_not_before IS NULL)",
            name="venue_order_intent_retry_deadline_consistent",
        ),
        CheckConstraint(
            "(state = 'FILLED' AND filled_quantity = quantity) OR "
            "(state = 'PARTIALLY_FILLED' AND filled_quantity > 0 "
            "AND filled_quantity < quantity) OR "
            "(state IN ('CREATED', 'SUBMITTING', 'RETRYABLE', 'ACKNOWLEDGED') "
            "AND filled_quantity = 0) OR "
            "state IN ('CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN')",
            name="venue_order_intent_fill_state_consistent",
        ),
        CheckConstraint(
            "(state IN ('ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED') "
            "AND venue_order_id IS NOT NULL) OR "
            "(state IN ('SUBMITTING', 'RETRYABLE', 'REJECTED') AND venue_order_id IS NULL) OR "
            "state IN ('CREATED', 'UNKNOWN')",
            name="venue_order_intent_venue_id_consistent",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 255 AND transition_sequence >= 1",
            name="venue_order_intent_counters_valid",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 240 "
            "AND length(client_order_id) BETWEEN 1 AND 240",
            name="venue_order_intent_keys_valid",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$' AND plan_hash ~ '^[0-9a-f]{64}$'",
            name="venue_order_intent_hashes_valid",
        ),
        CheckConstraint("length(symbol) BETWEEN 1 AND 64", name="venue_order_intent_symbol_valid"),
        CheckConstraint(
            "transition_source IN ('APPLICATION', 'SUBMISSION', 'RECONCILIATION', "
            "'USER_STREAM', 'OPERATOR')",
            name="venue_order_intent_transition_source_valid",
        ),
        CheckConstraint(
            "length(transition_key) BETWEEN 1 AND 240 "
            "AND length(transition_actor) BETWEEN 1 AND 128",
            name="venue_order_intent_transition_metadata_valid",
        ),
        CheckConstraint(
            "transition_evidence_hash IS NULL OR transition_evidence_hash ~ '^[0-9a-f]{64}$'",
            name="venue_order_intent_evidence_hash_valid",
        ),
        Index(
            "ix_venue_order_intent_workspace_account_state",
            "workspace_id",
            "account_id",
            "state",
        ),
        Index(
            "ix_venue_order_intent_retry_due",
            "workspace_id",
            "state",
            "retry_not_before",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reservation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="DEMO")
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    settings_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="MARKET")
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(240), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default="CREATED")
    attempt_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    venue_order_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default="0"
    )
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transition_sequence: Mapped[int] = mapped_column(nullable=False, server_default="1")
    transition_key: Mapped[str] = mapped_column(String(240), nullable=False)
    transition_actor: Mapped[str] = mapped_column(String(128), nullable=False)
    transition_source: Mapped[str] = mapped_column(String(24), nullable=False)
    transition_evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class VenueOrderTransitionRecord(UuidPrimaryKey, Base):
    """Sự kiện append-only do trigger tạo cho mỗi trạng thái lệnh."""

    __tablename__ = "venue_order_transition"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "intent_id",
            "sequence",
            name="uq_venue_order_transition_sequence",
        ),
        UniqueConstraint("workspace_id", "event_key", name="uq_venue_order_transition_event_key"),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id", "intent_id"],
            [
                "venue_order_intent.workspace_id",
                "venue_order_intent.bot_id",
                "venue_order_intent.account_id",
                "venue_order_intent.id",
            ],
            ondelete="RESTRICT",
            name="fk_venue_order_transition_intent",
        ),
        CheckConstraint("sequence >= 1", name="venue_order_transition_sequence_positive"),
        CheckConstraint(
            "to_state IN ('CREATED', 'SUBMITTING', 'RETRYABLE', 'ACKNOWLEDGED', "
            "'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN')",
            name="venue_order_transition_state_valid",
        ),
        CheckConstraint(
            "source IN ('APPLICATION', 'SUBMISSION', 'RECONCILIATION', 'USER_STREAM', 'OPERATOR')",
            name="venue_order_transition_source_valid",
        ),
        CheckConstraint(
            "length(event_key) BETWEEN 1 AND 240", name="venue_order_transition_key_valid"
        ),
        CheckConstraint(
            "length(actor_id) BETWEEN 1 AND 128", name="venue_order_transition_actor_valid"
        ),
        CheckConstraint(
            "evidence_hash IS NULL OR evidence_hash ~ '^[0-9a-f]{64}$'",
            name="venue_order_transition_hash_valid",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$' AND attempt_count BETWEEN 0 AND 255 "
            "AND filled_quantity >= 0",
            name="venue_order_transition_payload_valid",
        ),
        Index(
            "ix_venue_order_transition_workspace_intent", "workspace_id", "intent_id", "sequence"
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    event_key: Mapped[str] = mapped_column(String(240), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    attempt_count: Mapped[int] = mapped_column(nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    retry_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    venue_order_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    venue_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
