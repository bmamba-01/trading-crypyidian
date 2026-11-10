"""WP-07C: lưu quote volume nguồn cho tín hiệu và lineage replay."""

from collections.abc import Sequence

from alembic import op

revision: str = "c9d7e5f3a1b2"
down_revision: str | None = "b1d3f5a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.market_bar ADD COLUMN IF NOT EXISTS quote_volume NUMERIC(38, 18)"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'market_bar_quote_volume_nonnegative') THEN "
        "ALTER TABLE public.market_bar ADD CONSTRAINT market_bar_quote_volume_nonnegative "
        "CHECK (quote_volume IS NULL OR quote_volume >= 0); "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM public.market_bar WHERE quote_volume IS NOT NULL) THEN "
        "RAISE EXCEPTION 'WP07_MARKET_BAR_QUOTE_VOLUME_DOWNGRADE_REQUIRES_EMPTY_COLUMN'; "
        "END IF; END $$"
    )
    op.execute(
        "ALTER TABLE public.market_bar DROP CONSTRAINT IF EXISTS "
        "market_bar_quote_volume_nonnegative"
    )
