"""Bảng Telegram của MB-07a (migration `f7b9d1e3a5c8`)."""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class TelegramUpdateReceipt(UuidPrimaryKey, CreatedAt, Base):
    """`update_id` đã nhận theo workspace: dedup bền vững (SEC-36, ruling 38).

    Không UPDATE; chỉ xóa được hàng quá 7 ngày (policy `receipt_purge`, retention).
    """

    __tablename__ = "telegram_update_receipt"
    __table_args__ = (
        UniqueConstraint("workspace_id", "update_id", name="uq_telegram_update_receipt_update"),
        CheckConstraint("update_id > 0", name="update_id_positive"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        # Retention 7 ngày quét theo thời điểm nhận (fix round 1 minor 3).
        Index("ix_telegram_update_receipt_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
