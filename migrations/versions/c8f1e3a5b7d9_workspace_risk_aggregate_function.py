"""Thêm SQL aggregate function get_workspace_risk_aggregate (MB04.4).

Revision ID: c8f1e3a5b7d9
Revises: b7e9d1a3c5f8
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c8f1e3a5b7d9"
down_revision: str | None = "b7e9d1a3c5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
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
    )
    op.execute(
        """
        REVOKE ALL ON FUNCTION public.get_workspace_risk_aggregate(uuid, timestamptz)
        FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.get_workspace_risk_aggregate(uuid, timestamptz)
        TO crypidian_trading_runtime, crypidian_admin_web;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.get_workspace_risk_aggregate(uuid, timestamptz);")
