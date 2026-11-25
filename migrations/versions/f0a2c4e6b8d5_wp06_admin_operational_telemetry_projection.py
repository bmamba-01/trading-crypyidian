"""WP06.14: chỉ cung cấp snapshot telemetry tổng hợp cho admin-web.

Revision ID: f0a2c4e6b8d5
Revises: f0a2c4e6b8d4
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f0a2c4e6b8d5"
down_revision: str | None = "f0a2c4e6b8d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
ADMIN_SAFE = "crypidian_admin_web_safe"
TELEMETRY_READER = "crypidian_admin_telemetry_reader"
FUNCTION = "wp06_admin_operational_telemetry_snapshot"
SIGNATURE = "uuid,uuid,uuid,timestamptz,integer,text"

_READ_TABLES: dict[str, str] = {
    "user_membership": "workspace_id,id,status",
    "role_binding": "workspace_id,membership_id,role",
    "bot_grant": "workspace_id,bot_id,membership_id,permission,expires_at,revoked_at",
    "bot_instance": "workspace_id,id",
    "paper_account_seed": "workspace_id,bot_id,initial_equity,created_at",
    "trade": (
        "workspace_id,id,bot_id,mode,status,closed_at,net_pnl,opened_at,symbol,quote_asset,"
        "side,intent_id,lineage"
    ),
    "order_intent": "workspace_id,bot_id,mode,status",
    "market_bar": "workspace_id,timeframe,is_closed,source,close_time",
    "outbox_event": "workspace_id,status,event_type,available_at",
    "bot_runtime_control": "workspace_id,bot_id,entry_state,changed_at",
    "trade_plan": "workspace_id,bot_id,plan_hash,payload",
    "position_event": "workspace_id,id,bot_id,intent_id,event_type,occurred_at,payload",
    "account_activity_incident": "workspace_id,bot_id,status",
}

_FUNCTION_SQL = f"""
CREATE FUNCTION public.{FUNCTION}(
    target_workspace uuid,
    target_membership uuid,
    target_bot uuid,
    target_as_of timestamptz,
    target_consecutive_api_errors integer,
    target_operation text
) RETURNS TABLE (
    feed_age_seconds integer,
    daily_loss numeric,
    drawdown_pct numeric,
    consecutive_api_errors integer,
    outbox_lag_seconds integer,
    clock_skew_ms integer,
    unprotected_exposure_count integer,
    reconciliation_discrepancy_count integer,
    unknown_order_count integer,
    daily_window_start timestamptz,
    daily_window_end timestamptz,
    rolling_window_start timestamptz,
    realized_loss_today numeric,
    realized_loss_rolling numeric,
    gross_loss_today numeric,
    gross_loss_rolling numeric,
    trade_count_today integer,
    trade_count_rolling integer,
    timezone_name text
) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = on
AS $$
DECLARE
    caller_membership uuid;
    daily_start timestamptz;
    daily_end timestamptz;
    rolling_start timestamptz;
BEGIN
    caller_membership := NULLIF(pg_catalog.current_setting('app.membership_id', true), '')::uuid;
    IF session_user <> '{ADMIN}' OR target_workspace IS NULL OR target_membership IS NULL
       OR target_workspace IS DISTINCT FROM NULLIF(
            pg_catalog.current_setting('app.workspace_id', true), '')::uuid
       OR target_membership IS DISTINCT FROM caller_membership
       OR target_as_of IS NULL OR target_consecutive_api_errors IS NULL
       OR target_consecutive_api_errors < 0
       OR target_operation NOT IN ('health_read', 'breaker_resume')
       THEN
        RAISE EXCEPTION 'WP06_ADMIN_TELEMETRY_SCOPE_DENIED';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.user_membership AS member
          JOIN public.role_binding AS binding
            ON binding.workspace_id = member.workspace_id
           AND binding.membership_id = member.id
         WHERE member.workspace_id = target_workspace
           AND member.id = target_membership
           AND member.status = 'ACTIVE'
           AND CASE target_operation
               WHEN 'health_read' THEN binding.role IN (
                    'owner', 'operator', 'strategy_admin', 'risk_admin', 'analyst_reviewer')
               WHEN 'breaker_resume' THEN binding.role IN ('owner', 'operator', 'risk_admin')
               ELSE FALSE
           END
    ) THEN
        RAISE EXCEPTION 'WP06_ADMIN_TELEMETRY_PERMISSION_DENIED';
    END IF;

    IF target_operation = 'health_read' AND EXISTS (
        SELECT 1
          FROM public.bot_instance AS bot
         WHERE bot.workspace_id = target_workspace
           AND NOT EXISTS (
               SELECT 1
                 FROM public.user_membership AS member
                 JOIN public.role_binding AS binding
                   ON binding.workspace_id = member.workspace_id
                  AND binding.membership_id = member.id
                WHERE member.workspace_id = target_workspace
                  AND member.id = target_membership
                  AND member.status = 'ACTIVE'
                  AND binding.role IN ('owner', 'strategy_admin', 'risk_admin')
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM public.bot_grant AS grant_row
                WHERE grant_row.workspace_id = target_workspace
                  AND grant_row.bot_id = bot.id
                  AND grant_row.membership_id = target_membership
                  AND grant_row.permission = 'bot:read'
                  AND grant_row.revoked_at IS NULL
                  AND (grant_row.expires_at IS NULL OR grant_row.expires_at >
                       pg_catalog.clock_timestamp())
           )
    ) THEN
        RAISE EXCEPTION 'WP06_ADMIN_TELEMETRY_BOT_SCOPE_DENIED';
    END IF;

    IF target_operation = 'breaker_resume' THEN
        IF target_bot IS NULL THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.role_binding AS binding
                 WHERE binding.workspace_id = target_workspace
                   AND binding.membership_id = target_membership
                   AND binding.role IN ('owner', 'risk_admin')
            ) THEN
                RAISE EXCEPTION 'WP06_ADMIN_TELEMETRY_RESUME_SCOPE_DENIED';
            END IF;
        ELSIF NOT EXISTS (
            SELECT 1 FROM public.bot_instance AS bot
             WHERE bot.workspace_id = target_workspace AND bot.id = target_bot
        ) OR (
            NOT EXISTS (
                SELECT 1 FROM public.role_binding AS binding
                 WHERE binding.workspace_id = target_workspace
                   AND binding.membership_id = target_membership
                   AND binding.role IN ('owner', 'risk_admin')
            ) AND NOT EXISTS (
                SELECT 1 FROM public.bot_grant AS grant_row
                 WHERE grant_row.workspace_id = target_workspace
                   AND grant_row.bot_id = target_bot
                   AND grant_row.membership_id = target_membership
                   AND grant_row.permission = 'bot:runtime:resume'
                   AND grant_row.revoked_at IS NULL
                   AND (grant_row.expires_at IS NULL OR grant_row.expires_at >
                        pg_catalog.clock_timestamp())
            )
        ) THEN
            RAISE EXCEPTION 'WP06_ADMIN_TELEMETRY_RESUME_SCOPE_DENIED';
        END IF;
    END IF;

    daily_start := pg_catalog.date_trunc('day', target_as_of AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    daily_end := daily_start + interval '1 day';
    rolling_start := target_as_of - interval '24 hours';

    RETURN QUERY
    WITH loss_totals AS (
        SELECT
            COALESCE(sum(t.net_pnl) FILTER (
                WHERE t.closed_at >= daily_start AND t.closed_at <= target_as_of), 0) AS net_today,
            COALESCE(sum(t.net_pnl) FILTER (
                WHERE t.closed_at >= rolling_start AND t.closed_at <= target_as_of), 0)
                AS net_rolling,
            COALESCE(sum(abs(t.net_pnl)) FILTER (
                WHERE t.closed_at >= daily_start AND t.closed_at <= target_as_of
                  AND t.net_pnl < 0), 0) AS gross_today,
            COALESCE(sum(abs(t.net_pnl)) FILTER (
                WHERE t.closed_at >= rolling_start AND t.closed_at <= target_as_of
                  AND t.net_pnl < 0), 0) AS gross_rolling,
            count(*) FILTER (
                WHERE t.closed_at >= daily_start AND t.closed_at <= target_as_of) AS count_today,
            count(*) FILTER (
                WHERE t.closed_at >= rolling_start AND t.closed_at <= target_as_of) AS count_rolling
          FROM public.trade AS t
         WHERE t.workspace_id = target_workspace
           AND t.mode = 'PAPER' AND t.status = 'CLOSED'
           AND t.closed_at IS NOT NULL
           AND t.closed_at <= target_as_of
           AND t.closed_at > target_as_of - interval '48 hours'
           AND (target_bot IS NULL OR t.bot_id = target_bot)
    ), latest_feed AS (
        SELECT max(bar.close_time) AS close_time
          FROM public.market_bar AS bar
         WHERE bar.workspace_id = target_workspace AND bar.timeframe = '1m'
           AND bar.is_closed
           AND bar.source = ANY(ARRAY['BINANCE_USDM', 'BINANCE_USDM_REST_BACKFILL']::text[])
    ), pending_outbox AS (
        SELECT min(event.available_at) AS oldest
          FROM public.outbox_event AS event
         WHERE event.workspace_id = target_workspace AND event.status = 'PENDING'
           AND event.event_type = ANY(ARRAY[
                'signal.evidence.recorded.v1', 'research.rights_changed.v1']::text[])
    ), seeds AS (
        SELECT DISTINCT ON (seed.bot_id) seed.bot_id, seed.initial_equity
          FROM public.paper_account_seed AS seed
         WHERE seed.workspace_id = target_workspace
           AND (target_bot IS NULL OR seed.bot_id = target_bot)
         ORDER BY seed.bot_id, seed.created_at DESC
    ), seed_total AS (
        SELECT COALESCE(sum(seed.initial_equity), 0) AS equity
          FROM seeds AS seed
    ), baseline AS (
        SELECT max(control.changed_at) AS changed_at
          FROM public.bot_runtime_control AS control
         WHERE control.workspace_id = target_workspace
           AND control.entry_state = 'ENABLED'
           AND control.changed_at <= target_as_of
           AND (target_bot IS NULL OR control.bot_id = target_bot)
    ), closed_curve AS (
        SELECT trade.closed_at, trade.id,
               sum(trade.net_pnl) OVER (ORDER BY trade.closed_at, trade.id
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_pnl
          FROM public.trade AS trade
         WHERE trade.workspace_id = target_workspace AND trade.mode = 'PAPER'
           AND trade.status = 'CLOSED' AND trade.closed_at <= target_as_of
           AND (target_bot IS NULL OR trade.bot_id = target_bot)
    ), equity_state AS (
        SELECT seed.equity + COALESCE(sum(trade.net_pnl), 0) AS current_equity
          FROM seed_total AS seed
          LEFT JOIN public.trade AS trade
            ON trade.workspace_id = target_workspace AND trade.mode = 'PAPER'
           AND trade.status = 'CLOSED' AND trade.closed_at <= target_as_of
           AND (target_bot IS NULL OR trade.bot_id = target_bot)
         GROUP BY seed.equity
    ), peak_state AS (
        SELECT CASE WHEN EXISTS (
                SELECT 1 FROM closed_curve AS curve, baseline AS base
                 WHERE base.changed_at IS NULL OR curve.closed_at > base.changed_at
            ) THEN GREATEST(
                seed.equity + COALESCE((
                    SELECT sum(trade.net_pnl) FROM public.trade AS trade, baseline AS base
                     WHERE trade.workspace_id = target_workspace AND trade.mode = 'PAPER'
                       AND trade.status = 'CLOSED' AND trade.closed_at <= target_as_of
                       AND base.changed_at IS NOT NULL AND trade.closed_at <= base.changed_at
                       AND (target_bot IS NULL OR trade.bot_id = target_bot)
                ), 0),
                COALESCE((
                    SELECT max(seed.equity + curve.cumulative_pnl)
                      FROM closed_curve AS curve, baseline AS base
                     WHERE base.changed_at IS NULL OR curve.closed_at > base.changed_at
                ), 0)
            ) ELSE state.current_equity END AS peak_equity
          FROM seed_total AS seed CROSS JOIN equity_state AS state
    ), drawdown AS (
        SELECT CASE WHEN seed.equity <= 0 THEN NULL
                    WHEN peak.peak_equity <= 0 THEN 100::numeric
                    ELSE GREATEST(0::numeric,
                         (peak.peak_equity - state.current_equity) / peak.peak_equity * 100)
               END AS drawdown_pct
          FROM seed_total AS seed CROSS JOIN equity_state AS state CROSS JOIN peak_state AS peak
    ), exposure AS (
        SELECT count(*)::integer AS unprotected_count
          FROM public.trade AS trade
          LEFT JOIN public.trade_plan AS plan
            ON plan.workspace_id = trade.workspace_id AND plan.bot_id = trade.bot_id
           AND plan.plan_hash = trade.lineage->>'plan_hash'
          LEFT JOIN LATERAL (
              SELECT event.payload->>'stop_price' AS stop_price
                FROM public.position_event AS event
               WHERE event.workspace_id = trade.workspace_id AND event.bot_id = trade.bot_id
                 AND event.intent_id = trade.intent_id AND event.event_type = 'STOP_UPDATED'
               ORDER BY event.occurred_at DESC, event.id DESC LIMIT 1
          ) AS latest_stop ON TRUE
         WHERE trade.workspace_id = target_workspace AND trade.mode = 'PAPER'
           AND trade.status = 'OPEN'
           AND (target_bot IS NULL OR trade.bot_id = target_bot)
           AND COALESCE(NULLIF(pg_catalog.btrim(latest_stop.stop_price), ''),
                        NULLIF(pg_catalog.btrim(plan.payload->>'stop_price'), '')) IS NULL
    ), unknown_orders AS (
        SELECT count(*)::integer AS unknown_count
          FROM public.order_intent AS intent
         WHERE intent.workspace_id = target_workspace AND intent.mode = 'PAPER'
           AND intent.status = 'UNKNOWN'
           AND (target_bot IS NULL OR intent.bot_id = target_bot)
    ), incidents AS (
        SELECT count(*)::integer AS open_count
          FROM public.account_activity_incident AS incident
         WHERE incident.workspace_id = target_workspace AND incident.status = 'OPEN'
           AND (target_bot IS NULL OR incident.bot_id = target_bot)
    )
    SELECT
        CASE WHEN feed.close_time IS NULL THEN NULL ELSE
            GREATEST(0, floor(extract(epoch FROM (target_as_of - feed.close_time)) - 60)::integer)
        END,
        GREATEST(0::numeric, -loss.net_today),
        dd.drawdown_pct,
        target_consecutive_api_errors,
        CASE WHEN outbox.oldest IS NULL THEN 0 ELSE
            GREATEST(0, floor(extract(epoch FROM (target_as_of - outbox.oldest)))::integer)
        END,
        abs(floor(extract(epoch FROM (
            pg_catalog.clock_timestamp() - target_as_of)) * 1000))::integer,
        exposure.unprotected_count,
        incidents.open_count,
        orders.unknown_count,
        daily_start,
        daily_end,
        rolling_start,
        GREATEST(0::numeric, -loss.net_today),
        GREATEST(0::numeric, -loss.net_rolling),
        loss.gross_today,
        loss.gross_rolling,
        loss.count_today::integer,
        loss.count_rolling::integer,
        'UTC'::text
      FROM loss_totals AS loss
      CROSS JOIN latest_feed AS feed
      CROSS JOIN pending_outbox AS outbox
      CROSS JOIN drawdown AS dd
      CROSS JOIN exposure
      CROSS JOIN unknown_orders AS orders
      CROSS JOIN incidents;
END;
$$
"""


def upgrade() -> None:
    for table in _READ_TABLES:
        op.execute(
            f"CREATE POLICY wp06_admin_telemetry_projection_read ON public.{table} "
            f"FOR SELECT TO {TELEMETRY_READER} USING (workspace_id = "
            "NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
        )
        op.execute(
            f"GRANT SELECT ({_READ_TABLES[table]}) ON TABLE public.{table} TO {TELEMETRY_READER}"
        )
    op.execute(_FUNCTION_SQL)
    op.execute(f"GRANT CREATE ON SCHEMA public TO {TELEMETRY_READER}")
    op.execute(f"ALTER FUNCTION public.{FUNCTION}({SIGNATURE}) OWNER TO {TELEMETRY_READER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {TELEMETRY_READER}")
    op.execute(
        f"REVOKE ALL ON FUNCTION public.{FUNCTION}({SIGNATURE}) "
        f"FROM PUBLIC, {ADMIN}, {TELEMETRY_READER}"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{FUNCTION}({SIGNATURE}) TO {ADMIN_SAFE}")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.{FUNCTION}({SIGNATURE}) FROM {ADMIN_SAFE}")
    op.execute(f"DROP FUNCTION public.{FUNCTION}({SIGNATURE})")
    for table in reversed(tuple(_READ_TABLES)):
        op.execute(f"DROP POLICY wp06_admin_telemetry_projection_read ON public.{table}")
        op.execute(
            f"REVOKE SELECT ({_READ_TABLES[table]}) ON TABLE public.{table} FROM {TELEMETRY_READER}"
        )
