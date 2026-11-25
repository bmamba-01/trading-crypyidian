"""WP06.14: kiểm tra bot phẳng qua projection giới hạn cho admin-web."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f0a2c4e6b8d7"
down_revision: str | None = "f0a2c4e6b8d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
ADMIN_SAFE = "crypidian_admin_web_safe"
PROJECTION_ROLE = "crypidian_admin_telemetry_reader"
FUNCTION = "wp06_admin_bot_lifecycle_snapshot"
SIGNATURE = "uuid,uuid"
RESERVATION_POLICY = "wp06_admin_lifecycle_reservation_select"
OUTBOX_POLICY = "wp06_admin_safe_outbox_insert"
OUTBOX_RETURNING_POLICY = "wp06_admin_safe_outbox_returning_select"

_FUNCTION_SQL = f"""
CREATE FUNCTION public.{FUNCTION}(
    target_workspace uuid,
    target_bot uuid
) RETURNS TABLE (
    bot_exists boolean,
    open_positions integer,
    open_orders integer,
    held_reservations integer
) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = on
AS $$
DECLARE
    caller_workspace uuid;
    caller_membership uuid;
BEGIN
    caller_workspace := NULLIF(
        pg_catalog.current_setting('app.workspace_id', true), '')::uuid;
    caller_membership := NULLIF(
        pg_catalog.current_setting('app.membership_id', true), '')::uuid;
    IF session_user <> '{ADMIN}' OR target_workspace IS NULL OR target_bot IS NULL
       OR target_workspace IS DISTINCT FROM caller_workspace
       OR caller_membership IS NULL
       THEN
        RAISE EXCEPTION 'WP06_ADMIN_LIFECYCLE_SCOPE_DENIED';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.user_membership AS member
          JOIN public.role_binding AS binding
            ON binding.workspace_id = member.workspace_id
           AND binding.membership_id = member.id
         WHERE member.workspace_id = target_workspace
           AND member.id = caller_membership
           AND member.status = 'ACTIVE'
           AND binding.role = 'owner'
    ) THEN
        RAISE EXCEPTION 'WP06_ADMIN_LIFECYCLE_OWNER_REQUIRED';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.bot_instance AS bot
         WHERE bot.workspace_id = target_workspace AND bot.id = target_bot
    ) THEN
        RETURN QUERY SELECT false, 0, 0, 0;
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        true,
        (SELECT count(*)::integer
           FROM public.trade AS trade
          WHERE trade.workspace_id = target_workspace
            AND trade.bot_id = target_bot AND trade.status = 'OPEN'),
        (SELECT count(*)::integer
           FROM public.order_intent AS intent
          WHERE intent.workspace_id = target_workspace
            AND intent.bot_id = target_bot
            AND intent.status IN ('ACCEPTED', 'PARTIALLY_FILLED', 'UNKNOWN')),
        (SELECT count(*)::integer
           FROM public.risk_reservation AS reservation
          WHERE reservation.workspace_id = target_workspace
            AND reservation.bot_id = target_bot
            AND reservation.status IN ('RESERVED', 'UNKNOWN'));
END;
$$
"""


def upgrade() -> None:
    op.execute(
        f"CREATE POLICY {OUTBOX_POLICY} ON public.outbox_event "
        f"FOR INSERT TO {ADMIN} WITH CHECK (workspace_id = "
        "NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY {OUTBOX_RETURNING_POLICY} ON public.outbox_event "
        f"FOR SELECT TO {ADMIN} USING (workspace_id = "
        "NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute(f"GRANT INSERT ON TABLE public.outbox_event TO {ADMIN_SAFE}")
    op.execute(
        f"GRANT SELECT (available_at, created_at) ON TABLE public.outbox_event TO {ADMIN_SAFE}"
    )
    op.execute(
        f"CREATE POLICY {RESERVATION_POLICY} ON public.risk_reservation "
        f"FOR SELECT TO {PROJECTION_ROLE} USING (workspace_id = "
        "NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute(
        "GRANT SELECT (workspace_id, bot_id, status) "
        f"ON TABLE public.risk_reservation TO {PROJECTION_ROLE}"
    )
    op.execute(_FUNCTION_SQL)
    op.execute(f"ALTER FUNCTION public.{FUNCTION}({SIGNATURE}) OWNER TO {PROJECTION_ROLE}")
    op.execute(
        f"REVOKE ALL ON FUNCTION public.{FUNCTION}({SIGNATURE}) "
        f"FROM PUBLIC, {ADMIN}, {ADMIN_SAFE}, {PROJECTION_ROLE}"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{FUNCTION}({SIGNATURE}) TO {ADMIN_SAFE}")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.{FUNCTION}({SIGNATURE}) FROM {ADMIN_SAFE}")
    op.execute(f"DROP FUNCTION public.{FUNCTION}({SIGNATURE})")
    op.execute(f"DROP POLICY {RESERVATION_POLICY} ON public.risk_reservation")
    op.execute(
        "REVOKE SELECT (workspace_id, bot_id, status) "
        f"ON TABLE public.risk_reservation FROM {PROJECTION_ROLE}"
    )
    op.execute(f"DROP POLICY {OUTBOX_POLICY} ON public.outbox_event")
    op.execute(f"DROP POLICY {OUTBOX_RETURNING_POLICY} ON public.outbox_event")
    op.execute(f"REVOKE INSERT ON TABLE public.outbox_event FROM {ADMIN_SAFE}")
    op.execute(
        f"REVOKE SELECT (available_at, created_at) ON TABLE public.outbox_event FROM {ADMIN_SAFE}"
    )
