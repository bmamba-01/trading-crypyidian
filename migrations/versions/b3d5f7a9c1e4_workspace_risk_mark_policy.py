"""MB-04 fix round 2 (I2/R3): mark chỉ từ nguồn/timeframe cấu hình, tươi theo close_time.

Revision ID: b3d5f7a9c1e4
Revises: f7a9c1e3b5d8

Re-review: cửa sổ tươi "độ dài nến + max_mark_age" lấy độ dài nến từ dữ liệu
(`close_time − open_time`) nên không bị chặn: nến 1d/1w mới nhất qua được tới ~1 ngày/1 tuần,
nến hỏng có `open_time` quá sớm qua được ở mọi tuổi; LATERAL không lọc timeframe/source. Sửa:

- Hai hàm nhận thêm `p_mark_source text` và `p_mark_timeframes text[]` (lấy từ root settings:
  `market_data.venue` và `market_data.timeframes`); LATERAL chỉ xét nến `is_closed` đúng nguồn và
  timeframe được phép.
- Tươi khi `as_of − close_time ≤ max_mark_age` (bỏ phần nới theo độ dài nến — contract §5).
- Nến mark phải đúng hình dạng: `close_time − open_time` ∈ (độ dài danh định − 1 giây, độ dài danh
  định] theo bảng timeframe đã biết (Binance đóng nến ở `open + interval − 1ms`); timeframe lạ
  hoặc nến hỏng → unknown (fail-closed).

Owner/grant/search_path giữ như `f7a9c1e3b5d8`. Downgrade dựng lại nguyên văn hai hàm của
`f7a9c1e3b5d8` với cùng owner/grant.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "b3d5f7a9c1e4"
down_revision = "f7a9c1e3b5d8"
branch_labels = None
depends_on = None

AGGREGATOR = "crypidian_risk_aggregator"
RUNTIME = "crypidian_trading_runtime"
ADMIN = "crypidian_admin_web"
OLD_STATE = "public.workspace_account_risk_state(uuid, timestamptz, integer)"
OLD_AGGREGATE = "public.get_workspace_risk_equity_aggregate(uuid, timestamptz, integer)"
STATE = "public.workspace_account_risk_state(uuid, timestamptz, integer, text, text[])"
AGGREGATE = "public.get_workspace_risk_equity_aggregate(uuid, timestamptz, integer, text, text[])"

_ARGUMENT_GUARD = """
    IF p_workspace_id IS NULL OR
       NULLIF(current_setting('app.workspace_id', true), '')::uuid
       IS DISTINCT FROM p_workspace_id THEN
        RAISE EXCEPTION 'MB04_WORKSPACE_CONTEXT_MISMATCH';
    END IF;
    IF p_as_of IS NULL OR p_max_mark_age_seconds IS NULL OR p_max_mark_age_seconds <= 0
       OR p_mark_source IS NULL OR length(trim(p_mark_source)) = 0
       OR p_mark_timeframes IS NULL OR cardinality(p_mark_timeframes) = 0 THEN
        RAISE EXCEPTION 'MB04_AGGREGATE_ARGUMENT_INVALID';
    END IF;
"""

# Độ dài danh định của timeframe hợp lệ trong root settings (`Timeframe`).
_NOMINAL = """CASE mk.timeframe
                   WHEN '1m' THEN interval '1 minute' WHEN '3m' THEN interval '3 minutes'
                   WHEN '5m' THEN interval '5 minutes' WHEN '15m' THEN interval '15 minutes'
                   WHEN '30m' THEN interval '30 minutes' WHEN '1h' THEN interval '1 hour'
                   WHEN '2h' THEN interval '2 hours' WHEN '4h' THEN interval '4 hours'
                   WHEN '6h' THEN interval '6 hours' WHEN '8h' THEN interval '8 hours'
                   WHEN '12h' THEN interval '12 hours' WHEN '1d' THEN interval '1 day'
               END"""

ACCOUNT_STATE_FUNCTION = f"""
CREATE FUNCTION public.workspace_account_risk_state(
    p_workspace_id uuid, p_as_of timestamptz, p_max_mark_age_seconds integer,
    p_mark_source text, p_mark_timeframes text[]
) RETURNS TABLE (
    bot_id uuid,
    account_id uuid,
    is_enabled boolean,
    seed_equity_usdt numeric,
    realized_pnl_usdt numeric,
    open_position_pnl_usdt numeric,
    open_position_count integer,
    equity_usdt numeric,
    has_unknown_equity boolean,
    open_risk_usdt numeric,
    pending_risk_usdt numeric,
    has_unknown_exposure boolean
) LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
#variable_conflict use_column
BEGIN
{_ARGUMENT_GUARD}
    RETURN QUERY
    WITH accounts AS (
        SELECT b.id AS bot_id, a.id AS account_id,
               (b.status = 'ENABLED' AND a.status = 'ENABLED' AND a.mode = 'PAPER') AS is_enabled
          FROM public.bot_instance b
          JOIN public.trading_account a
            ON a.workspace_id = b.workspace_id AND a.id = b.trading_account_id
         WHERE b.workspace_id = p_workspace_id
    ), seeds AS (
        SELECT s.bot_id, s.trading_account_id AS account_id, count(*) AS seed_count,
               sum(s.initial_equity) AS initial_equity,
               bool_or(s.quote_asset <> 'USDT') AS bad_quote
          FROM public.paper_account_seed s
         WHERE s.workspace_id = p_workspace_id AND s.created_at <= p_as_of
         GROUP BY s.bot_id, s.trading_account_id
    ), realized AS (
        SELECT t.account_id, sum(t.net_pnl) AS pnl, bool_or(t.quote_asset <> 'USDT') AS bad_quote
          FROM public.trade t
         WHERE t.workspace_id = p_workspace_id AND t.mode = 'PAPER' AND t.status = 'CLOSED'
           AND t.closed_at <= p_as_of
         GROUP BY t.account_id
    ), open_trades AS (
        SELECT t.id, t.account_id, t.symbol, t.quote_asset, t.side, t.intent_id
          FROM public.trade t
         WHERE t.workspace_id = p_workspace_id AND t.mode = 'PAPER' AND t.status = 'OPEN'
           AND t.opened_at <= p_as_of
    ), fill_totals AS (
        SELECT ot.id AS trade_id,
               coalesce(sum(f.quantity) FILTER (WHERE f.fill_role = 'ENTRY'), 0) AS entry_qty,
               coalesce(sum(f.quantity * f.price) FILTER (WHERE f.fill_role = 'ENTRY'), 0)
                   AS entry_notional,
               coalesce(sum(f.quantity) FILTER (WHERE f.fill_role = 'EXIT'), 0) AS exit_qty,
               coalesce(sum(f.quantity * f.price) FILTER (WHERE f.fill_role = 'EXIT'), 0)
                   AS exit_notional,
               coalesce(sum(f.fee_amount + f.slippage_amount), 0) AS fill_cost
          FROM open_trades ot
          LEFT JOIN public.fill f
            ON f.workspace_id = p_workspace_id AND f.account_id = ot.account_id
           AND f.intent_id = ot.intent_id AND f.occurred_at <= p_as_of
         GROUP BY ot.id
    ), cost_totals AS (
        SELECT tc.trade_id, sum(tc.amount) AS total_cost,
               coalesce(sum(tc.amount) FILTER (WHERE tc.cost_type IN ('FEE', 'SLIPPAGE')), 0)
                   AS fill_cost,
               bool_or(tc.currency <> 'USDT') AS bad_currency
          FROM public.trade_cost tc
          JOIN open_trades ot ON ot.id = tc.trade_id AND ot.account_id = tc.account_id
         WHERE tc.workspace_id = p_workspace_id AND tc.occurred_at <= p_as_of
         GROUP BY tc.trade_id
    ), valued AS (
        SELECT ot.account_id,
               CASE ot.side
                   WHEN 'LONG' THEN ft.exit_notional - ft.entry_notional
                                    + mk.close * (ft.entry_qty - ft.exit_qty)
                   WHEN 'SHORT' THEN ft.entry_notional - ft.exit_notional
                                     - mk.close * (ft.entry_qty - ft.exit_qty)
               END - coalesce(ct.total_cost, 0) AS pnl,
               (ot.quote_asset <> 'USDT'
                OR ft.entry_qty <= 0 OR ft.entry_qty <= ft.exit_qty
                OR mk.close IS NULL OR mk.close <= 0
                OR p_as_of - mk.close_time > make_interval(secs => p_max_mark_age_seconds)
                OR {_NOMINAL} IS NULL
                OR mk.close_time - mk.open_time > {_NOMINAL}
                OR mk.close_time - mk.open_time <= {_NOMINAL} - interval '1 second'
                OR coalesce(ct.bad_currency, false)
                OR ft.fill_cost <> coalesce(ct.fill_cost, 0)) AS unknown
          FROM open_trades ot
          JOIN fill_totals ft ON ft.trade_id = ot.id
          LEFT JOIN cost_totals ct ON ct.trade_id = ot.id
          LEFT JOIN LATERAL (
              SELECT mb.close, mb.open_time, mb.close_time, mb.timeframe
                FROM public.market_bar mb
               WHERE mb.workspace_id = p_workspace_id AND mb.symbol = ot.symbol AND mb.is_closed
                 AND mb.source = p_mark_source AND mb.timeframe = ANY (p_mark_timeframes)
                 AND mb.close_time <= p_as_of AND mb.available_at <= p_as_of
               ORDER BY mb.close_time DESC, mb.available_at DESC, mb.revision DESC
               LIMIT 1
          ) mk ON true
    ), open_valuation AS (
        SELECT v.account_id, sum(v.pnl) AS pnl, count(*)::integer AS position_count,
               bool_or(v.unknown OR v.pnl IS NULL) AS unknown
          FROM valued v
         GROUP BY v.account_id
    ), open_exposure AS (
        SELECT t.account_id, sum(r.reserved_risk) AS risk,
               bool_or(r.id IS NULL OR r.reserved_risk IS NULL OR r.reserved_risk <= 0) AS unknown
          FROM public.trade t
          LEFT JOIN public.risk_reservation r
            ON r.workspace_id = t.workspace_id AND r.account_id = t.account_id
           AND r.id = t.risk_reservation_id
         WHERE t.workspace_id = p_workspace_id AND t.mode = 'PAPER' AND t.status = 'OPEN'
         GROUP BY t.account_id
    ), pending AS (
        SELECT r.account_id, sum(r.reserved_risk) AS risk,
               bool_or(r.status = 'UNKNOWN') AS has_unknown
          FROM public.risk_reservation r
         WHERE r.workspace_id = p_workspace_id
           AND (r.status = 'UNKNOWN'
                OR (r.status = 'RESERVED' AND r.expires_at > least(p_as_of, now())))
         GROUP BY r.account_id
    ), combined AS (
        SELECT a.bot_id, a.account_id, a.is_enabled, s.seed_count, s.bad_quote,
               s.initial_equity, coalesce(rz.pnl, 0) AS realized,
               coalesce(rz.bad_quote, false) AS realized_bad_quote,
               coalesce(ov.pnl, 0) AS open_pnl, coalesce(ov.position_count, 0) AS position_count,
               coalesce(ov.unknown, false) AS open_unknown,
               s.initial_equity + coalesce(rz.pnl, 0) + coalesce(ov.pnl, 0) AS equity,
               coalesce(oe.risk, 0) AS open_risk, coalesce(oe.unknown, false) AS open_risk_unknown,
               coalesce(p.risk, 0) AS pending_risk,
               coalesce(p.has_unknown, false) AS pending_unknown
          FROM accounts a
          LEFT JOIN seeds s ON s.account_id = a.account_id AND s.bot_id = a.bot_id
          LEFT JOIN realized rz ON rz.account_id = a.account_id
          LEFT JOIN open_valuation ov ON ov.account_id = a.account_id
          LEFT JOIN open_exposure oe ON oe.account_id = a.account_id
          LEFT JOIN pending p ON p.account_id = a.account_id
    )
    SELECT c.bot_id, c.account_id, c.is_enabled, c.initial_equity, c.realized, c.open_pnl,
           c.position_count, c.equity,
           (c.seed_count IS DISTINCT FROM 1 OR coalesce(c.bad_quote, true)
            OR c.realized_bad_quote OR c.open_unknown
            OR c.equity IS NULL OR c.equity < 0),
           c.open_risk, c.pending_risk,
           (c.open_risk_unknown OR c.pending_unknown)
      FROM combined c;
END;
$$
"""

AGGREGATE_FUNCTION = f"""
CREATE FUNCTION public.get_workspace_risk_equity_aggregate(
    p_workspace_id uuid, p_as_of timestamptz, p_max_mark_age_seconds integer,
    p_mark_source text, p_mark_timeframes text[]
) RETURNS TABLE (
    workspace_id uuid,
    total_equity_usdt numeric,
    total_open_risk_usdt numeric,
    total_pending_risk_usdt numeric,
    active_bot_count integer,
    has_unknown_exposure boolean,
    has_unknown_equity boolean
) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
#variable_conflict use_column
BEGIN
{_ARGUMENT_GUARD}
    RETURN QUERY
    SELECT p_workspace_id,
           coalesce(sum(s.equity_usdt) FILTER (WHERE s.is_enabled), 0),
           coalesce(sum(s.open_risk_usdt), 0),
           coalesce(sum(s.pending_risk_usdt), 0),
           (count(*) FILTER (WHERE s.is_enabled))::integer,
           coalesce(bool_or(s.has_unknown_exposure), false),
           coalesce(bool_or(s.has_unknown_equity) FILTER (WHERE s.is_enabled), false)
      FROM public.workspace_account_risk_state(
          p_workspace_id, p_as_of, p_max_mark_age_seconds, p_mark_source, p_mark_timeframes
      ) s;
END;
$$
"""


def _secure(state: str, aggregate: str) -> None:
    for signature in (state, aggregate):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {AGGREGATOR}")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {state} TO {RUNTIME}, {ADMIN}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {aggregate} TO {RUNTIME}")


def _previous_revision() -> ModuleType:
    """Nạp nguyên văn SQL của `f7a9c1e3b5d8` để downgrade trả lại đúng hàm cũ."""
    path = next(Path(__file__).parent.glob("f7a9c1e3b5d8_*.py"))
    spec = importlib.util.spec_from_file_location(f"_mb04_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("MB04_PREVIOUS_MIGRATION_NOT_FOUND")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    op.execute(f"DROP FUNCTION {OLD_AGGREGATE}")
    op.execute(f"DROP FUNCTION {OLD_STATE}")
    op.execute(ACCOUNT_STATE_FUNCTION)
    op.execute(AGGREGATE_FUNCTION)
    _secure(STATE, AGGREGATE)


def downgrade() -> None:
    previous = _previous_revision()
    op.execute(f"DROP FUNCTION {AGGREGATE}")
    op.execute(f"DROP FUNCTION {STATE}")
    op.execute(previous.ACCOUNT_STATE_FUNCTION)
    op.execute(previous.AGGREGATE_FUNCTION)
    _secure(OLD_STATE, OLD_AGGREGATE)
