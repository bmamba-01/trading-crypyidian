"""WP-07C: lưu quote volume nguồn cho tín hiệu và lineage replay."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d7e5f3a1b2"
down_revision: str | None = "b1d3f5a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "market_bar",
        sa.Column("quote_volume", sa.Numeric(precision=38, scale=18), nullable=True),
    )
    op.create_check_constraint(
        "market_bar_quote_volume_nonnegative",
        "market_bar",
        "quote_volume IS NULL OR quote_volume >= 0",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM public.market_bar WHERE quote_volume IS NOT NULL) THEN "
        "RAISE EXCEPTION 'WP07_MARKET_BAR_QUOTE_VOLUME_DOWNGRADE_REQUIRES_EMPTY_COLUMN'; "
        "END IF; END $$"
    )
    op.drop_constraint("market_bar_quote_volume_nonnegative", "market_bar", type_="check")
    op.drop_column("market_bar", "quote_volume")
