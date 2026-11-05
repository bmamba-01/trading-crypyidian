"""Mô hình ORM cho Bản kê Chiến lược Khai báo (Declarative Strategy Card).

Lưu trữ cấu trúc đồ thị tính toán L0-L6 và siêu dữ liệu chiến lược
theo nguyên tắc bất biến (append-only) và cách ly tenant (RLS).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey
from trading_bot.domain.strategy_card.schema import StrategyCard, compute_canonical_hash

if TYPE_CHECKING:
    pass


class StrategyCardRecord(UuidPrimaryKey, CreatedAt, Base):
    """Bản ghi Strategy Card lưu trữ bất biến (append-only), tenant-scoped theo workspace."""

    __tablename__ = "strategy_cards"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        UniqueConstraint(
            "workspace_id",
            "card_id",
            "version",
            name="uq_strategy_cards_workspace_card_version",
        ),
        UniqueConstraint(
            "workspace_id",
            "content_hash",
            name="uq_strategy_cards_workspace_content_hash",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_strategy_cards_workspace_id"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_strategy_cards_content_hash_hex",
        ),
        CheckConstraint(
            "length(card_id) > 0 AND length(version) > 0 AND length(name) > 0",
            name="ck_strategy_cards_identifiers_present",
        ),
        CheckConstraint(
            "jsonb_typeof(card_payload) = 'object'",
            name="ck_strategy_cards_payload_object",
        ),
        Index("ix_strategy_cards_workspace_active", "workspace_id", "is_active"),
        Index("ix_strategy_cards_workspace_card_id", "workspace_id", "card_id"),
        Index("ix_strategy_cards_content_hash", "content_hash"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    card_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    card_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    @classmethod
    def from_domain(
        cls,
        workspace_id: UUID,
        card: StrategyCard,
        is_active: bool = False,
    ) -> StrategyCardRecord:
        """Chuyển đổi từ StrategyCard domain model sang StrategyCardRecord ORM."""
        if not isinstance(card, StrategyCard):
            raise ValueError("card phải là đối tượng StrategyCard.")
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")

        computed_hash = compute_canonical_hash(card)
        payload = card.model_dump(mode="json")
        return cls(
            workspace_id=workspace_id,
            card_id=card.card_id,
            name=card.name,
            version=card.version,
            content_hash=computed_hash,
            is_active=is_active,
            card_payload=payload,
        )

    def to_domain(self) -> StrategyCard:
        """Tái tạo StrategyCard domain model và xác thực mã băm SHA-256 bảo đảm tính toàn vẹn."""
        if not isinstance(self.card_payload, dict):
            raise ValueError("card_payload phải là từ điển JSONB hợp lệ.")

        card = StrategyCard.model_validate(self.card_payload)
        computed_hash = compute_canonical_hash(card)
        if computed_hash != self.content_hash:
            raise ValueError(
                f"Mã băm nội dung bị sai lệch hoặc đã bị can thiệp trái phép! "
                f"Lưu trữ: {self.content_hash}, Tính toán: {computed_hash}"
            )
        if card.card_id != self.card_id:
            raise ValueError(
                f"card_id không khớp: chỉ mục '{self.card_id}' != payload '{card.card_id}'."
            )
        if card.version != self.version:
            raise ValueError(
                f"version không khớp: chỉ mục '{self.version}' != payload '{card.version}'."
            )
        return card
