"""Ledger bất biến cho khớp lệnh PAPER và kết quả giao dịch có thể phát lại."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey

_JSON_TYPE = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")
_AMOUNT = Numeric(38, 18, asdecimal=True)


class OrderIntentRecord(UuidPrimaryKey, CreatedAt, Base):
    """Ý định PAPER cùng cấu hình, tín hiệu và quyết định risk đã phê duyệt."""

    __tablename__ = "order_intent"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_order_intent_workspace_id"),
        UniqueConstraint(
            "workspace_id", "account_id", "idempotency_key", name="uq_order_intent_idempotency"
        ),
        UniqueConstraint("workspace_id", "account_id", "id", name="uq_order_intent_account_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "settings_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "strategy_release_id"],
            ["strategy_release.workspace_id", "strategy_release.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "plan_hash"],
            ["trade_plan.workspace_id", "trade_plan.account_id", "trade_plan.plan_hash"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "risk_decision_id"],
            ["risk_decision.workspace_id", "risk_decision.account_id", "risk_decision.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "risk_reservation_id"],
            ["risk_reservation.workspace_id", "risk_reservation.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "feature_snapshot_id"],
            ["feature_snapshot.workspace_id", "feature_snapshot.snapshot_id"],
        ),
        CheckConstraint("mode = 'PAPER'", name="order_intent_paper_only"),
        CheckConstraint("side IN ('LONG', 'SHORT')", name="order_intent_side_valid"),
        CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
            name="order_intent_quantity_valid",
        ),
        CheckConstraint(
            "(status = 'ACCEPTED' AND filled_quantity = 0) OR "
            "(status = 'PARTIALLY_FILLED' AND filled_quantity > 0 "
            "AND filled_quantity < quantity) OR "
            "(status = 'FILLED' AND filled_quantity = quantity) OR "
            "status IN ('CANCELLED', 'EXPIRED', 'UNKNOWN')",
            name="order_intent_status_quantity_consistent",
        ),
        CheckConstraint(
            "status IN ('ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', "
            "'EXPIRED', 'UNKNOWN')",
            name="order_intent_status_valid",
        ),
        CheckConstraint("length(candidate_id) = 64", name="order_intent_candidate_sha256"),
        CheckConstraint("length(strategy_release_hash) = 64", name="order_intent_release_sha256"),
        Index("ix_order_intent_workspace_account_status", "workspace_id", "account_id", "status"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PAPER")
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    settings_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    strategy_release_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    strategy_release_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_reservation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False, default=Decimal(0))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACCEPTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False, default=dict)


class ExchangeOrderRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lệnh giả lập gắn với một intent; không chứa khóa hoặc bí mật sàn."""

    __tablename__ = "exchange_order"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_exchange_order_workspace_id"),
        UniqueConstraint(
            "workspace_id", "account_id", "client_order_id", name="uq_exchange_order_client_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "intent_id"],
            ["order_intent.workspace_id", "order_intent.account_id", "order_intent.id"],
        ),
        CheckConstraint("mode = 'PAPER'", name="exchange_order_paper_only"),
        CheckConstraint(
            "status IN ('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'EXPIRED', 'UNKNOWN')",
            name="exchange_order_status_valid",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PAPER")
    intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(240), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="NEW")
    raw_reference: Mapped[str | None] = mapped_column(Text, nullable=True)


class FillRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một fill bất biến với tham chiếu nến và chính sách khớp đã pin."""

    __tablename__ = "fill"
    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", "fill_key", name="uq_fill_idempotency"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "intent_id"],
            ["order_intent.workspace_id", "order_intent.account_id", "order_intent.id"],
        ),
        CheckConstraint("mode = 'PAPER'", name="fill_paper_only"),
        CheckConstraint("fill_role IN ('ENTRY', 'EXIT')", name="fill_role_valid"),
        CheckConstraint("quantity > 0 AND price > 0", name="fill_amounts_positive"),
        CheckConstraint("fee_amount >= 0 AND slippage_amount >= 0", name="fill_costs_nonnegative"),
        Index("ix_fill_workspace_account_time", "workspace_id", "account_id", "occurred_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PAPER")
    intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fill_role: Mapped[str] = mapped_column(String(8), nullable=False, server_default="ENTRY")
    fill_key: Mapped[str] = mapped_column(String(240), nullable=False)
    bar_key: Mapped[str] = mapped_column(String(240), nullable=False)
    fill_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)
    price: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)
    slippage_amount: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lineage: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False, default=dict)


class PositionEventRecord(UuidPrimaryKey, CreatedAt, Base):
    """Sự kiện append-only về mở, tăng, giảm hoặc đóng vị thế PAPER."""

    __tablename__ = "position_event"
    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", "event_key", name="uq_position_event_key"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "intent_id"],
            ["order_intent.workspace_id", "order_intent.account_id", "order_intent.id"],
        ),
        CheckConstraint("mode = 'PAPER'", name="position_event_paper_only"),
        CheckConstraint(
            "event_type IN ('OPENED', 'INCREASED', 'REDUCED', 'CLOSED', 'STOP_UPDATED')",
            name="position_event_type_valid",
        ),
        CheckConstraint("quantity >= 0", name="position_event_quantity_nonnegative"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PAPER")
    intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_key: Mapped[str] = mapped_column(String(240), nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False, default=dict)
    lineage: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False, default=dict)


class TradeRecord(UuidPrimaryKey, CreatedAt, Base):
    """Bản ghi giao dịch đóng/mở được phát lại từ fill và chi phí."""

    __tablename__ = "trade"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_trade_workspace_id"),
        UniqueConstraint("workspace_id", "account_id", "id", name="uq_trade_account_id"),
        UniqueConstraint("workspace_id", "account_id", "intent_id", name="uq_trade_intent"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "intent_id"],
            ["order_intent.workspace_id", "order_intent.account_id", "order_intent.id"],
        ),
        CheckConstraint("mode = 'PAPER'", name="trade_paper_only"),
        CheckConstraint("status IN ('OPEN', 'CLOSED')", name="trade_status_valid"),
        CheckConstraint(
            "(status = 'OPEN' AND closed_at IS NULL) OR "
            "(status = 'CLOSED' AND closed_at IS NOT NULL)",
            name="trade_status_close_time_consistent",
        ),
        CheckConstraint("side IN ('LONG', 'SHORT')", name="trade_side_valid"),
        CheckConstraint(
            "total_cost = fee_total + funding_total + slippage_total", name="trade_cost_sum_exact"
        ),
        CheckConstraint("net_pnl = gross_pnl - total_cost", name="trade_net_pnl_exact"),
        CheckConstraint("fee_total >= 0 AND slippage_total >= 0", name="trade_cost_nonnegative"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PAPER")
    intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    settings_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    strategy_release_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    strategy_release_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_reservation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False, default=Decimal(0))
    fee_total: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False, default=Decimal(0))
    funding_total: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False, default=Decimal(0))
    slippage_total: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False, default=Decimal(0))
    total_cost: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False, default=Decimal(0))
    net_pnl: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False, default=Decimal(0))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lineage: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False, default=dict)


class TradeCostRecord(UuidPrimaryKey, CreatedAt, Base):
    """Chi phí giao dịch bất biến, dedupe theo nguồn sự kiện."""

    __tablename__ = "trade_cost"
    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", "cost_key", name="uq_trade_cost_key"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "trade_id"],
            ["trade.workspace_id", "trade.account_id", "trade.id"],
        ),
        CheckConstraint("mode = 'PAPER'", name="trade_cost_paper_only"),
        CheckConstraint(
            "cost_type IN ('FEE', 'FUNDING', 'SLIPPAGE')", name="trade_cost_type_valid"
        ),
        CheckConstraint(
            "amount >= 0 OR cost_type = 'FUNDING'", name="trade_cost_amount_nonnegative"
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PAPER")
    trade_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    cost_key: Mapped[str] = mapped_column(String(240), nullable=False)
    cost_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    lineage: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False, default=dict)


class PaperExecutionCheckpointRecord(UuidPrimaryKey, CreatedAt, Base):
    """Snapshot append-only của toàn bộ trạng thái gateway trong một PAPER account."""

    __tablename__ = "paper_execution_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "account_id", "revision", name="uq_paper_checkpoint_revision"
        ),
        UniqueConstraint(
            "workspace_id", "account_id", "event_key", name="uq_paper_checkpoint_event"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
            name="fk_paper_checkpoint_account",
        ),
        CheckConstraint("mode = 'PAPER'", name="paper_checkpoint_mode"),
        CheckConstraint("revision > 0", name="paper_checkpoint_revision_positive"),
        CheckConstraint("schema_version > 0", name="paper_checkpoint_schema_positive"),
        CheckConstraint("length(event_key) > 0", name="paper_checkpoint_event_key_nonempty"),
        CheckConstraint(
            "length(event_hash) = 64 AND event_hash = lower(event_hash) "
            "AND event_hash !~ '[^0-9a-f]'",
            name="paper_checkpoint_event_hash_sha256",
        ),
        CheckConstraint(
            "length(policy_hash) = 64 AND policy_hash = lower(policy_hash) "
            "AND policy_hash !~ '[^0-9a-f]'",
            name="paper_checkpoint_policy_hash_sha256",
        ),
        CheckConstraint(
            "length(snapshot_hash) = 64 AND snapshot_hash = lower(snapshot_hash) "
            "AND snapshot_hash !~ '[^0-9a-f]'",
            name="paper_checkpoint_snapshot_hash_sha256",
        ),
        Index(
            "ix_paper_checkpoint_workspace_account_revision",
            "workspace_id",
            "account_id",
            "revision",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PAPER")
    revision: Mapped[int] = mapped_column(nullable=False)
    event_key: Mapped[str] = mapped_column(String(240), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_scope: Mapped[str | None] = mapped_column(String(120), nullable=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_payload: Mapped[str] = mapped_column(Text, nullable=False)
