"""FX-3 lát 1: lưu mark price và loại kỳ của mức funding đã chốt (CRY-198).

Binance ``GET /fapi/v1/fundingRate`` trả ``markPrice`` — mark price dùng tính phí funding của kỳ
đó — và ``rateType`` (``Regular``/``Special``). Ledger PAPER cần mark price của đúng kỳ để tính
funding = mark price × khối lượng × rate, nên ``market_funding`` thêm hai cột cho phép NULL (bản
ghi cũ và kỳ mà sàn không trả ``markPrice``). Quyền bảng không đổi: ingestion-worker ghi, runtime
đọc theo cấp quyền mức bảng hiện có.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1b8e93fa47f"
down_revision: str | None = "60a62c705a89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("market_funding", sa.Column("mark_price", sa.Numeric(38, 18), nullable=True))
    op.add_column("market_funding", sa.Column("rate_type", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        "market_funding_mark_price_positive",
        "market_funding",
        "mark_price IS NULL OR mark_price > 0",
    )
    op.create_check_constraint(
        "market_funding_rate_type_known",
        "market_funding",
        "rate_type IS NULL OR rate_type IN ('Regular', 'Special')",
    )


def downgrade() -> None:
    op.drop_constraint("market_funding_rate_type_known", "market_funding", type_="check")
    op.drop_constraint("market_funding_mark_price_positive", "market_funding", type_="check")
    op.drop_column("market_funding", "rate_type")
    op.drop_column("market_funding", "mark_price")
