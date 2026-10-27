"""Bảng Telegram của MB-07a (migration `f7b9d1e3a5c8`)."""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class TelegramUpdateReceipt(UuidPrimaryKey, CreatedAt, Base):
    """`update_id` đã nhận theo workspace: dedup bền vững, append-only (SEC-36, ruling 38)."""

    __tablename__ = "telegram_update_receipt"
    __table_args__ = (
        UniqueConstraint("workspace_id", "update_id", name="uq_telegram_update_receipt_update"),
        CheckConstraint("update_id > 0", name="update_id_positive"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
