"""SIG-0: lưu số giao dịch và khối lượng mua chủ động (taker buy) của kline Binance USDⓈ-M.

Các trường ``[8]`` (number of trades), ``[9]`` (taker buy base asset volume) và ``[10]``
(taker buy quote asset volume) đều nullable: dữ liệu cũ giữ NULL, không ước lượng. Ràng buộc
không âm và taker buy không vượt tổng khối lượng tương ứng. Downgrade bị chặn khi đã có giá trị.
Nguồn: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
(kiểm 15/11/2026).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3a5c7e9f1b4"
down_revision: str | None = "d2e4f6a8b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINTS = (
    ("market_bar_trade_count_nonnegative", "trade_count IS NULL OR trade_count >= 0"),
    (
        "market_bar_taker_buy_base_within_volume",
        "taker_buy_base_volume IS NULL OR "
        "(taker_buy_base_volume >= 0 AND taker_buy_base_volume <= volume)",
    ),
    (
        "market_bar_taker_buy_quote_within_quote_volume",
        "taker_buy_quote_volume IS NULL OR (taker_buy_quote_volume >= 0 AND "
        "(quote_volume IS NULL OR taker_buy_quote_volume <= quote_volume))",
    ),
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.market_bar "
        "ADD COLUMN trade_count BIGINT, "
        "ADD COLUMN taker_buy_base_volume NUMERIC(38, 18), "
        "ADD COLUMN taker_buy_quote_volume NUMERIC(38, 18)"
    )
    for name, expression in CONSTRAINTS:
        op.execute(f"ALTER TABLE public.market_bar ADD CONSTRAINT {name} CHECK ({expression})")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM public.market_bar WHERE trade_count IS NOT NULL "
        "OR taker_buy_base_volume IS NOT NULL OR taker_buy_quote_volume IS NOT NULL) THEN "
        "RAISE EXCEPTION 'SIG0_MARKET_BAR_TAKER_FLOW_DOWNGRADE_REQUIRES_EMPTY_COLUMNS'; "
        "END IF; END $$"
    )
    for name, _ in reversed(CONSTRAINTS):
        op.execute(f"ALTER TABLE public.market_bar DROP CONSTRAINT IF EXISTS {name}")
    op.execute(
        "ALTER TABLE public.market_bar "
        "DROP COLUMN taker_buy_quote_volume, "
        "DROP COLUMN taker_buy_base_volume, "
        "DROP COLUMN trade_count"
    )
