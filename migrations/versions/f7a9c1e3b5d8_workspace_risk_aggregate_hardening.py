"""MB-04 fix: aggregate rủi ro workspace do role NOLOGIN sở hữu, một phép tính equity dùng chung.

Revision ID: f7a9c1e3b5d8
Revises: e1f3a5c7b9d2

Sửa các finding review độc lập MB-04:
- I1: hàm `SECURITY DEFINER` không còn do superuser migrator sở hữu. Role riêng
  `crypidian_risk_aggregator` (NOLOGIN, NOSUPERUSER, NOBYPASSRLS, NOINHERIT, không thành viên)
  sở hữu hàm; role chỉ có `SELECT` các bảng ledger cần đọc và policy RLS `FOR SELECT` giới hạn
  đúng `app.workspace_id`. `search_path` ghim `pg_catalog, pg_temp`; `REVOKE ALL FROM PUBLIC`.
- Hàm aggregate cũ `get_workspace_risk_aggregate` (không còn code gọi) bị xóa; aggregate
  `SECURITY DEFINER` chỉ grant cho `crypidian_trading_runtime` (admin-web không gọi nó).
- I2/I3: một hàm `SECURITY INVOKER` `workspace_account_risk_state` tính vốn/exposure từng account
  (dưới RLS của role gọi). Runtime dùng nó cho vốn account mục tiêu, admin-web (owner/risk_admin
  thấy mọi bot qua RLS) dùng cho đánh giá breach khi publish, aggregate dùng nó làm nguồn duy
  nhất. Mark lấy nến đóng mới nhất có `close_time`/`available_at <= as_of`; độ tươi đo theo thời
  điểm quan sát `close_time`: `as_of - close_time <= (close_time - open_time) + max_mark_age`.
  PnL vị thế mở tính không chia (exit notional − entry notional ± mark × phần còn lại − chi phí).
- C3: exposure gồm mọi trade OPEN (không lọc `opened_at <= as_of`); RESERVED còn giữ nếu
  `expires_at > least(as_of, now())` để `as_of` tương lai không làm rơi reservation còn hạn.
- Minor: kết quả `numeric` không giới hạn scale (không làm tròn half-up); vốn âm từng account là
  unknown (khớp domain).

Downgrade trả lại đúng hàm/grant của `e4f6a8c0d2b5` và `c8f1e3a5b7d9`, gỡ policy/grant và
xóa role nếu không database nào khác còn phụ thuộc.
"""

from alembic import op

revision = "f7a9c1e3b5d8"
down_revision = "e1f3a5c7b9d2"
branch_labels = None
depends_on = None

AGGREGATOR = "crypidian_risk_aggregator"
RUNTIME = "crypidian_trading_runtime"
ADMIN = "crypidian_admin_web"
# Bảng role aggregator phải đọc để tính vốn/exposure toàn workspace.
AGGREGATE_TABLES = (
    "bot_instance",
    "trading_account",
    "paper_account_seed",
    "trade",
    "fill",
    "trade_cost",
    "market_bar",
    "risk_reservation",
)
_WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
STATE_SIGNATURE = "public.workspace_account_risk_state(uuid, timestamptz, integer)"
AGGREGATE_SIGNATURE = "public.get_workspace_risk_equity_aggregate(uuid, timestamptz, integer)"
LEGACY_SIGNATURE = "public.get_workspace_risk_aggregate(uuid, timestamptz)"

ACCOUNT_STATE_FUNCTION = """
CREATE FUNCTION public.workspace_account_risk_state(
    p_workspace_id uuid, p_as_of timestamptz, p_max_mark_age_seconds integer
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
    IF p_workspace_id IS NULL OR
       NULLIF(current_setting('app.workspace_id', true), '')::uuid
       IS DISTINCT FROM p_workspace_id THEN
        RAISE EXCEPTION 'MB04_WORKSPACE_CONTEXT_MISMATCH';
    END IF;
    IF p_as_of IS NULL OR p_max_mark_age_seconds IS NULL OR p_max_mark_age_seconds <= 0 THEN
        RAISE EXCEPTION 'MB04_AGGREGATE_ARGUMENT_INVALID';
    END IF;
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
                OR mk.close IS NULL OR mk.close <= 0 OR mk.close_time <= mk.open_time
                OR p_as_of - mk.close_time
                   > (mk.close_time - mk.open_time) + make_interval(secs => p_max_mark_age_seconds)
                OR coalesce(ct.bad_currency, false)
                OR ft.fill_cost <> coalesce(ct.fill_cost, 0)) AS unknown
          FROM open_trades ot
          JOIN fill_totals ft ON ft.trade_id = ot.id
          LEFT JOIN cost_totals ct ON ct.trade_id = ot.id
          LEFT JOIN LATERAL (
              SELECT mb.close, mb.open_time, mb.close_time
                FROM public.market_bar mb
               WHERE mb.workspace_id = p_workspace_id AND mb.symbol = ot.symbol AND mb.is_closed
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

AGGREGATE_FUNCTION = """
CREATE FUNCTION public.get_workspace_risk_equity_aggregate(
    p_workspace_id uuid, p_as_of timestamptz, p_max_mark_age_seconds integer
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
    IF p_workspace_id IS NULL OR
       NULLIF(current_setting('app.workspace_id', true), '')::uuid
       IS DISTINCT FROM p_workspace_id THEN
        RAISE EXCEPTION 'MB04_WORKSPACE_CONTEXT_MISMATCH';
    END IF;
    IF p_as_of IS NULL OR p_max_mark_age_seconds IS NULL OR p_max_mark_age_seconds <= 0 THEN
        RAISE EXCEPTION 'MB04_AGGREGATE_ARGUMENT_INVALID';
    END IF;
    RETURN QUERY
    SELECT p_workspace_id,
           coalesce(sum(s.equity_usdt) FILTER (WHERE s.is_enabled), 0),
           coalesce(sum(s.open_risk_usdt), 0),
           coalesce(sum(s.pending_risk_usdt), 0),
           (count(*) FILTER (WHERE s.is_enabled))::integer,
           coalesce(bool_or(s.has_unknown_exposure), false),
           coalesce(bool_or(s.has_unknown_equity) FILTER (WHERE s.is_enabled), false)
      FROM public.workspace_account_risk_state(
          p_workspace_id, p_as_of, p_max_mark_age_seconds
      ) s;
END;
$$
"""

# --- Nguyên văn trạng thái trước (downgrade chính xác) -------------------------------------------
PREVIOUS_EQUITY_AGGREGATE = """
        CREATE FUNCTION public.get_workspace_risk_equity_aggregate(
            p_workspace_id uuid, p_as_of timestamptz, p_max_mark_age_seconds integer
        ) RETURNS TABLE (
            workspace_id uuid,
            total_equity_usdt numeric(38, 18),
            total_open_risk_usdt numeric(38, 18),
            total_pending_risk_usdt numeric(38, 18),
            active_bot_count integer,
            has_unknown_exposure boolean,
            has_unknown_equity boolean
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_equity numeric(38, 18) := 0;
            v_open numeric(38, 18) := 0;
            v_pending numeric(38, 18) := 0;
            v_count integer := 0;
            v_unknown_exposure boolean := false;
            v_unknown_equity boolean := false;
        BEGIN
            IF p_workspace_id IS NULL OR
               NULLIF(current_setting('app.workspace_id', true), '')::uuid
               IS DISTINCT FROM p_workspace_id THEN
                RAISE EXCEPTION 'MB04_WORKSPACE_CONTEXT_MISMATCH';
            END IF;
            IF p_as_of IS NULL OR p_max_mark_age_seconds IS NULL
               OR p_max_mark_age_seconds <= 0 THEN
                RAISE EXCEPTION 'MB04_AGGREGATE_ARGUMENT_INVALID';
            END IF;

            SELECT coalesce(sum(rr.reserved_risk), 0)::numeric(38, 18)
              INTO v_open
              FROM public.trade t
              JOIN public.risk_reservation rr
                ON t.workspace_id = rr.workspace_id AND t.risk_reservation_id = rr.id
             WHERE t.workspace_id = p_workspace_id AND t.mode = 'PAPER'
               AND t.status = 'OPEN' AND t.opened_at <= p_as_of;

            SELECT coalesce(sum(r.reserved_risk), 0)::numeric(38, 18)
              INTO v_pending
              FROM public.risk_reservation r
             WHERE r.workspace_id = p_workspace_id AND
               (r.status = 'UNKNOWN' OR (r.status = 'RESERVED' AND r.expires_at > p_as_of));

            SELECT count(*)::integer INTO v_count
              FROM public.bot_instance b
              JOIN public.trading_account a
                ON b.workspace_id = a.workspace_id AND b.trading_account_id = a.id
             WHERE b.workspace_id = p_workspace_id AND b.status = 'ENABLED'
               AND a.status = 'ENABLED' AND a.mode = 'PAPER';

            WITH enabled_accounts AS (
                SELECT b.id AS bot_id, a.id AS account_id
                  FROM public.bot_instance b
                  JOIN public.trading_account a
                    ON b.workspace_id = a.workspace_id AND b.trading_account_id = a.id
                 WHERE b.workspace_id = p_workspace_id AND b.status = 'ENABLED'
                   AND a.status = 'ENABLED' AND a.mode = 'PAPER'
            ), account_realized AS (
                SELECT t.account_id, sum(t.net_pnl) AS pnl
                  FROM public.trade t
                 WHERE t.workspace_id = p_workspace_id AND t.mode = 'PAPER'
                   AND t.status = 'CLOSED' AND t.closed_at <= p_as_of
                 GROUP BY t.account_id
            ), fill_costs AS (
                SELECT f.workspace_id, f.account_id, f.intent_id,
                       sum(f.fee_amount + f.slippage_amount)::numeric(38, 18) AS fill_cost_total
                  FROM public.fill f
                 WHERE f.workspace_id = p_workspace_id AND f.occurred_at <= p_as_of
                 GROUP BY f.workspace_id, f.account_id, f.intent_id
            ), trade_costs AS (
                SELECT tc.workspace_id, tc.account_id, tc.trade_id,
                       sum(tc.amount)::numeric(38, 18) AS total_cost,
                       coalesce(sum(tc.amount) FILTER (
                           WHERE tc.cost_type IN ('FEE', 'SLIPPAGE')
                       ), 0)::numeric(38, 18) AS fill_cost_total,
                       bool_or(tc.currency <> 'USDT') AS has_unsupported_currency
                  FROM public.trade_cost tc
                 WHERE tc.workspace_id = p_workspace_id AND tc.occurred_at <= p_as_of
                 GROUP BY tc.workspace_id, tc.account_id, tc.trade_id
            ), positions AS (
                SELECT t.id, t.account_id, t.symbol, t.quote_asset, t.side,
                       t.intent_id,
                       coalesce(
                           sum(f.quantity) FILTER (WHERE f.fill_role = 'ENTRY'), 0
                       ) AS entry_qty,
                       coalesce(
                           sum(f.quantity) FILTER (WHERE f.fill_role = 'EXIT'), 0
                       ) AS exit_qty,
                       CASE WHEN sum(f.quantity) FILTER (WHERE f.fill_role = 'ENTRY') > 0
                            THEN sum(f.quantity * f.price) FILTER (WHERE f.fill_role = 'ENTRY') /
                                 sum(f.quantity) FILTER (WHERE f.fill_role = 'ENTRY')
                            ELSE NULL END AS entry_price,
                       CASE WHEN sum(f.quantity) FILTER (WHERE f.fill_role = 'EXIT') > 0
                            THEN sum(f.quantity * f.price) FILTER (WHERE f.fill_role = 'EXIT') /
                                 sum(f.quantity) FILTER (WHERE f.fill_role = 'EXIT')
                            ELSE NULL END AS exit_price,
                       coalesce(fc.fill_cost_total, 0) AS fill_cost_total,
                       coalesce(tc.fill_cost_total, 0) AS recorded_fill_cost_total,
                       coalesce(tc.total_cost, 0) AS total_cost,
                       coalesce(tc.has_unsupported_currency, false) AS has_unsupported_cost,
                       mark.close AS mark_price, mark.available_at AS mark_available_at
                  FROM public.trade t
                  LEFT JOIN public.fill f ON f.workspace_id = t.workspace_id
                    AND f.account_id = t.account_id AND f.intent_id = t.intent_id
                    AND f.occurred_at <= p_as_of
                  LEFT JOIN fill_costs fc ON fc.workspace_id = t.workspace_id
                    AND fc.account_id = t.account_id AND fc.intent_id = t.intent_id
                  LEFT JOIN trade_costs tc ON tc.workspace_id = t.workspace_id
                    AND tc.account_id = t.account_id AND tc.trade_id = t.id
                  LEFT JOIN LATERAL (
                    SELECT mb.close, mb.available_at
                      FROM public.market_bar mb
                     WHERE mb.workspace_id = t.workspace_id AND mb.symbol = t.symbol
                       AND mb.is_closed AND mb.close_time <= p_as_of AND mb.available_at <= p_as_of
                     ORDER BY mb.close_time DESC, mb.available_at DESC, mb.revision DESC
                     LIMIT 1
                  ) mark ON true
                 WHERE t.workspace_id = p_workspace_id AND t.mode = 'PAPER'
                   AND t.status = 'OPEN' AND t.opened_at <= p_as_of
                 GROUP BY t.id, t.account_id, t.symbol, t.quote_asset, t.side, t.intent_id,
                          fc.fill_cost_total, tc.fill_cost_total, tc.total_cost,
                          tc.has_unsupported_currency,
                          mark.close, mark.available_at
            ), account_open_pnl AS (
                SELECT account_id,
                       sum(CASE WHEN side = 'LONG'
                                THEN (coalesce(exit_price, entry_price) - entry_price) * exit_qty
                                     + (mark_price - entry_price) * (entry_qty - exit_qty)
                                ELSE (entry_price - coalesce(exit_price, entry_price)) * exit_qty
                                     + (entry_price - mark_price) * (entry_qty - exit_qty)
                           END - total_cost) AS pnl
                  FROM positions
                 WHERE entry_qty > exit_qty AND entry_price IS NOT NULL AND mark_price IS NOT NULL
                   AND mark_available_at >= p_as_of - make_interval(secs => p_max_mark_age_seconds)
                   AND quote_asset = 'USDT'
                   AND NOT has_unsupported_cost
                   AND fill_cost_total = recorded_fill_cost_total
                 GROUP BY account_id
            )
            SELECT coalesce(sum(seed.initial_equity + coalesce(realized.pnl, 0)
                                + coalesce(opened.pnl, 0)), 0)::numeric(38, 18),
                   (EXISTS (
                       SELECT 1 FROM enabled_accounts ea
                       LEFT JOIN public.paper_account_seed s
                         ON s.workspace_id = p_workspace_id
                        AND s.trading_account_id = ea.account_id AND s.bot_id = ea.bot_id
                       WHERE s.id IS NULL OR s.created_at > p_as_of OR s.quote_asset <> 'USDT'
                   ) OR EXISTS (
                       SELECT 1 FROM public.trade t
                       JOIN enabled_accounts ea ON ea.account_id = t.account_id
                       WHERE t.workspace_id = p_workspace_id AND t.mode = 'PAPER'
                         AND t.status = 'CLOSED' AND t.closed_at <= p_as_of
                         AND t.quote_asset <> 'USDT'
                   ) OR EXISTS (
                       SELECT 1 FROM positions p
                       WHERE p.quote_asset <> 'USDT' OR p.entry_qty <= p.exit_qty
                          OR p.entry_price IS NULL OR p.mark_price IS NULL
                          OR p.has_unsupported_cost
                          OR p.fill_cost_total <> p.recorded_fill_cost_total
                          OR p.mark_available_at < p_as_of - make_interval(
                              secs => p_max_mark_age_seconds
                          )
                          OR p.mark_available_at > p_as_of
                   ))
              INTO v_equity, v_unknown_equity
              FROM enabled_accounts e
              JOIN public.paper_account_seed seed
                ON seed.workspace_id = p_workspace_id AND seed.trading_account_id = e.account_id
               AND seed.bot_id = e.bot_id AND seed.created_at <= p_as_of
              LEFT JOIN account_realized realized ON realized.account_id = e.account_id
              LEFT JOIN account_open_pnl opened ON opened.account_id = e.account_id;

            v_unknown_exposure := EXISTS (
                SELECT 1 FROM public.risk_reservation r
                 WHERE r.workspace_id = p_workspace_id AND r.status = 'UNKNOWN'
            ) OR EXISTS (
                SELECT 1 FROM public.trade t
                LEFT JOIN public.risk_reservation rr
                  ON t.workspace_id = rr.workspace_id AND t.risk_reservation_id = rr.id
                 WHERE t.workspace_id = p_workspace_id AND t.mode = 'PAPER'
                   AND t.status = 'OPEN' AND t.opened_at <= p_as_of
                   AND (rr.id IS NULL OR rr.reserved_risk IS NULL)
            );

            RETURN QUERY SELECT p_workspace_id, v_equity, v_open, v_pending, v_count,
                                v_unknown_exposure, v_unknown_equity;
        END;
        $$;
        REVOKE ALL ON FUNCTION
          public.get_workspace_risk_equity_aggregate(uuid, timestamptz, integer)
          FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION
          public.get_workspace_risk_equity_aggregate(uuid, timestamptz, integer)
          TO crypidian_trading_runtime, crypidian_admin_web;
        """

PREVIOUS_LEGACY_AGGREGATE = """
        CREATE OR REPLACE FUNCTION public.get_workspace_risk_aggregate(
            p_workspace_id uuid,
            p_as_of timestamptz
        )
        RETURNS TABLE (
            workspace_id uuid,
            total_open_risk_usdt numeric(38, 18),
            total_pending_risk_usdt numeric(38, 18),
            active_bot_count integer,
            has_unknown_exposure boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_total_open_risk numeric(38, 18) := 0;
            v_total_pending_risk numeric(38, 18) := 0;
            v_active_bot_count integer := 0;
            v_has_unknown_exposure boolean := false;
        BEGIN
            IF p_workspace_id IS NULL OR
               NULLIF(current_setting('app.workspace_id', true), '')::uuid
               IS DISTINCT FROM p_workspace_id THEN
                RAISE EXCEPTION 'MB04_WORKSPACE_CONTEXT_MISMATCH';
            END IF;
            IF p_as_of IS NULL THEN
                RAISE EXCEPTION 'MB04_AS_OF_REQUIRED';
            END IF;

            -- 1. Tổng open risk từ Trade OPEN
            SELECT coalesce(sum(rr.reserved_risk), 0)::numeric(38, 18)
            INTO v_total_open_risk
            FROM public.trade t
            JOIN public.risk_reservation rr
              ON t.workspace_id = rr.workspace_id AND t.risk_reservation_id = rr.id
            WHERE t.workspace_id = p_workspace_id
              AND t.mode = 'PAPER'
              AND t.status = 'OPEN'
              AND t.opened_at <= p_as_of;

            -- 2. Tổng pending risk từ reservation RESERVED (chưa hết hạn) và UNKNOWN
            SELECT coalesce(sum(r.reserved_risk), 0)::numeric(38, 18)
            INTO v_total_pending_risk
            FROM public.risk_reservation r
            WHERE r.workspace_id = p_workspace_id
              AND (
                  r.status = 'UNKNOWN'
                  OR (r.status = 'RESERVED' AND r.expires_at > p_as_of)
              );

            -- 3. Đếm active bots (bot ENABLED có account ENABLED)
            SELECT count(*)::integer
            INTO v_active_bot_count
            FROM public.bot_instance b
            JOIN public.trading_account a
              ON b.workspace_id = a.workspace_id AND b.trading_account_id = a.id
            WHERE b.workspace_id = p_workspace_id
              AND b.status = 'ENABLED'
              AND a.status = 'ENABLED';

            -- 4. Kiểm tra unknown exposure
            v_has_unknown_exposure := EXISTS (
                SELECT 1 FROM public.risk_reservation r
                WHERE r.workspace_id = p_workspace_id
                  AND r.status = 'UNKNOWN'
            ) OR EXISTS (
                SELECT 1 FROM public.trade t
                LEFT JOIN public.risk_reservation rr
                  ON t.workspace_id = rr.workspace_id AND t.risk_reservation_id = rr.id
                WHERE t.workspace_id = p_workspace_id
                  AND t.mode = 'PAPER'
                  AND t.status = 'OPEN'
                  AND t.opened_at <= p_as_of
                  AND (rr.id IS NULL OR rr.reserved_risk IS NULL)
            );

            RETURN QUERY SELECT
                p_workspace_id,
                v_total_open_risk,
                v_total_pending_risk,
                v_active_bot_count,
                v_has_unknown_exposure;
        END;
        $$;
        """


def upgrade() -> None:
    # Role cấp cluster: database khác cùng server có thể đã tạo; chỉ chấp nhận hình dạng an toàn.
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{AGGREGATOR}') THEN
            CREATE ROLE {AGGREGATOR} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (
            SELECT 1 FROM pg_roles r WHERE r.rolname = '{AGGREGATOR}'
            AND (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolreplication
                OR r.rolcreaterole OR r.rolcreatedb OR r.rolinherit
                OR EXISTS (SELECT 1 FROM pg_auth_members a WHERE a.roleid = r.oid)
                OR EXISTS (SELECT 1 FROM pg_auth_members a WHERE a.member = r.oid))
        ) THEN
            -- Role sở hữu hàm SECURITY DEFINER: login/thành viên/bypass sẽ mở đường vượt RLS.
            RAISE EXCEPTION 'MB04_RISK_AGGREGATOR_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {AGGREGATOR}")
    for table in AGGREGATE_TABLES:
        op.execute(f'GRANT SELECT ON TABLE public."{table}" TO {AGGREGATOR}')
        op.execute(
            f'CREATE POLICY risk_aggregate_read ON public."{table}" FOR SELECT '
            f"TO {AGGREGATOR} USING ({_WORKSPACE})"
        )
    op.execute(f"DROP FUNCTION {LEGACY_SIGNATURE}")
    op.execute(f"DROP FUNCTION {AGGREGATE_SIGNATURE}")
    op.execute(ACCOUNT_STATE_FUNCTION)
    op.execute(AGGREGATE_FUNCTION)
    for signature in (STATE_SIGNATURE, AGGREGATE_SIGNATURE):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {AGGREGATOR}")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    # Hàm invoker chạy dưới RLS của role gọi: runtime chỉ thấy bot của mình, admin-web thấy theo
    # membership/grant. Aggregate definer chỉ cho runtime (admin-web tự cộng dưới RLS của mình).
    op.execute(f"GRANT EXECUTE ON FUNCTION {STATE_SIGNATURE} TO {RUNTIME}, {ADMIN}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {AGGREGATE_SIGNATURE} TO {RUNTIME}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {AGGREGATE_SIGNATURE}")
    op.execute(f"DROP FUNCTION {STATE_SIGNATURE}")
    op.execute(PREVIOUS_EQUITY_AGGREGATE)
    op.execute(PREVIOUS_LEGACY_AGGREGATE)
    op.execute(
        f"""
        REVOKE ALL ON FUNCTION {LEGACY_SIGNATURE}
        FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION {LEGACY_SIGNATURE}
        TO crypidian_trading_runtime, crypidian_admin_web;
        """
    )
    for table in reversed(AGGREGATE_TABLES):
        op.execute(f'DROP POLICY risk_aggregate_read ON public."{table}"')
        op.execute(f'REVOKE SELECT ON TABLE public."{table}" FROM {AGGREGATOR}')
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {AGGREGATOR}")
    # Role cấp cluster: chỉ xóa khi database khác không còn phụ thuộc vào nó.
    op.execute(f"""DO $$ BEGIN
        DROP ROLE IF EXISTS {AGGREGATOR};
    EXCEPTION WHEN dependent_objects_still_exist THEN
        RAISE NOTICE 'MB04_RISK_AGGREGATOR_ROLE_KEPT_FOR_OTHER_DATABASES';
    END $$""")
