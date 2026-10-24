"""Bảng lưu reservation rủi ro theo workspace và tài khoản."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class TradePlanRecord(UuidPrimaryKey, CreatedAt, Base):
    """Kế hoạch giao dịch bất biến đã chuẩn hóa để có thể phát lại."""

    __tablename__ = "trade_plan"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_trade_plan_bot_account",
        ),
        Index("ix_trade_plan_bot_scope", "workspace_id", "bot_id", "account_id"),
        UniqueConstraint("workspace_id", "id", name="uq_trade_plan_workspace_id"),
        UniqueConstraint("workspace_id", "account_id", "plan_hash", name="uq_trade_plan_hash"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"], ["trading_account.workspace_id", "trading_account.id"]
        ),
        CheckConstraint("length(symbol) > 0", name="trade_plan_symbol_nonempty"),
        CheckConstraint("notional > 0", name="trade_plan_notional_positive"),
        Index(
            "ix_trade_plan_workspace_account_created", "workspace_id", "account_id", "created_at"
        ),
    )

    bot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        server_default=text("NULLIF(current_setting('app.bot_id', true), '')::uuid"),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class RiskDecisionRecord(UuidPrimaryKey, CreatedAt, Base):
    """Quyết định risk bất biến, kể cả quyết định bị từ chối."""

    __tablename__ = "risk_decision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_risk_decision_bot_account",
        ),
        Index("ix_risk_decision_bot_scope", "workspace_id", "bot_id", "account_id"),
        UniqueConstraint("workspace_id", "id", name="uq_risk_decision_workspace_id"),
        UniqueConstraint(
            "workspace_id", "account_id", "id", name="uq_risk_decision_workspace_account_id"
        ),
        UniqueConstraint("workspace_id", "account_id", "decision_key", name="uq_risk_decision_key"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"], ["trading_account.workspace_id", "trading_account.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "plan_hash"],
            ["trade_plan.workspace_id", "trade_plan.account_id", "trade_plan.plan_hash"],
        ),
        CheckConstraint("length(reason_code) > 0", name="risk_decision_reason_nonempty"),
        Index(
            "ix_risk_decision_workspace_account_created",
            "workspace_id",
            "account_id",
            "created_at",
        ),
    )

    bot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        server_default=text("NULLIF(current_setting('app.bot_id', true), '')::uuid"),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_key: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    detail_vi: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class RiskReservationRecord(UuidPrimaryKey, CreatedAt, Base):
    """Ngân sách risk đã giữ cho một intent và có trạng thái reconcile."""

    __tablename__ = "risk_reservation"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "bot_id", "account_id", "id", name="uq_risk_reservation_bot_account_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_risk_reservation_bot_account",
        ),
        Index("ix_risk_reservation_bot_scope", "workspace_id", "bot_id", "account_id"),
        UniqueConstraint("workspace_id", "id", name="uq_risk_reservation_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "idempotency_key",
            name="uq_risk_reservation_idempotency",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["trading_account.workspace_id", "trading_account.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id", "risk_decision_id"],
            [
                "risk_decision.workspace_id",
                "risk_decision.account_id",
                "risk_decision.id",
            ],
        ),
        CheckConstraint("reserved_risk > 0", name="risk_reservation_risk_positive"),
        CheckConstraint("reserved_notional > 0", name="risk_reservation_notional_positive"),
        CheckConstraint(
            "reserved_initial_margin > 0", name="risk_reservation_initial_margin_positive"
        ),
        CheckConstraint("length(symbol) > 0", name="risk_reservation_symbol_nonempty"),
        CheckConstraint("expires_at > created_at", name="risk_reservation_expiry_after_creation"),
        CheckConstraint(
            "status IN ('RESERVED', 'UNKNOWN', 'RELEASED', 'TRANSFERRED')",
            name="risk_reservation_status_valid",
        ),
        CheckConstraint(
            "resolution_reason IS NULL OR resolution_reason IN "
            "('EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', "
            "'EXPIRED_NO_FILL', 'CANCELLED_NO_FILL', "
            "'ORDER_ABSENT', 'OPEN_RISK_RECORDED')",
            name="risk_reservation_resolution_reason_valid",
        ),
        CheckConstraint(
            "(status IN ('RESERVED', 'UNKNOWN') AND resolution_reason IS NULL) OR "
            "(status = 'RELEASED' AND resolution_reason IN "
            "('EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', 'EXPIRED_NO_FILL', "
            "'CANCELLED_NO_FILL', 'ORDER_ABSENT')) OR "
            "(status = 'TRANSFERRED' AND resolution_reason = 'OPEN_RISK_RECORDED')",
            name="risk_reservation_status_reason_consistent",
        ),
        Index("ix_risk_reservation_active", "workspace_id", "account_id", "status"),
    )

    bot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        server_default=text("NULLIF(current_setting('app.bot_id', true), '')::uuid"),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_risk: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reserved_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reserved_initial_margin: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RESERVED")
    resolution_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
