"""Mở rộng aggregate workspace thành equity point-in-time (MB04.7)."""

from collections.abc import Sequence

from alembic import op

revision: str = "e4f6a8c0d2b5"
down_revision: str | None = "d9b2f4a6c8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
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
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.get_workspace_risk_equity_aggregate(uuid, timestamptz, integer)"
    )
