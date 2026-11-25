"""WP06.14: projection risk admin chỉ trả aggregate và số đếm PAPER."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f0a2c4e6b8d9"
down_revision: str | None = "f0a2c4e6b8d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
ADMIN_SAFE = "crypidian_admin_web_safe"
AGGREGATOR = "crypidian_risk_aggregator"
MEMBERSHIP_POLICY = "wp06_admin_risk_projection_membership_select"
ROLE_POLICY = "wp06_admin_risk_projection_role_select"
INTENT_POLICY = "wp06_admin_risk_projection_intent_select"
ASSERT_SCOPE = "wp06_admin_risk_projection_assert_scope"
CAPACITY_SNAPSHOT = "wp06_admin_risk_capacity_snapshot"
PAPER_POLICY_SNAPSHOT = "wp06_admin_paper_policy_snapshot"
ASSERT_SIGNATURE = "uuid,uuid"
CAPACITY_SIGNATURE = "uuid,uuid,timestamptz,integer,text,text[]"
PAPER_POLICY_SIGNATURE = "uuid,uuid"

_ASSERT_SCOPE_SQL = f"""
CREATE FUNCTION public.{ASSERT_SCOPE}(
    target_workspace uuid,
    target_membership uuid
) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = on
AS $$
BEGIN
    IF session_user <> '{ADMIN}' OR target_workspace IS NULL OR target_membership IS NULL
       OR target_workspace IS DISTINCT FROM NULLIF(
            pg_catalog.current_setting('app.workspace_id', true), '')::uuid
       OR target_membership IS DISTINCT FROM NULLIF(
            pg_catalog.current_setting('app.membership_id', true), '')::uuid
       THEN
        RAISE EXCEPTION 'WP06_ADMIN_RISK_PROJECTION_SCOPE_DENIED';
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
           AND binding.role IN ('owner', 'strategy_admin', 'risk_admin')
    ) THEN
        RAISE EXCEPTION 'WP06_ADMIN_RISK_PROJECTION_PERMISSION_DENIED';
    END IF;
END;
$$
"""

_CAPACITY_SNAPSHOT_SQL = f"""
CREATE FUNCTION public.{CAPACITY_SNAPSHOT}(
    target_workspace uuid,
    target_membership uuid,
    target_as_of timestamptz,
    target_max_mark_age_seconds integer,
    target_mark_source text,
    target_mark_timeframes text[]
) RETURNS TABLE (
    bot_id uuid,
    account_id uuid,
    is_enabled boolean,
    equity_usdt numeric,
    has_unknown_equity boolean,
    open_risk_usdt numeric,
    pending_risk_usdt numeric,
    has_unknown_exposure boolean
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = on
AS $$
BEGIN
    PERFORM public.{ASSERT_SCOPE}(target_workspace, target_membership);
    IF target_as_of IS NULL OR target_max_mark_age_seconds IS NULL
       OR target_max_mark_age_seconds <= 0 OR target_mark_source IS NULL
       OR length(trim(target_mark_source)) = 0 OR target_mark_timeframes IS NULL
       OR cardinality(target_mark_timeframes) = 0
       THEN
        RAISE EXCEPTION 'WP06_ADMIN_RISK_PROJECTION_ARGUMENT_INVALID';
    END IF;

    RETURN QUERY
    SELECT state.bot_id, state.account_id, state.is_enabled, state.equity_usdt,
           state.has_unknown_equity, state.open_risk_usdt, state.pending_risk_usdt,
           state.has_unknown_exposure
      FROM public.workspace_account_risk_state(
          target_workspace, target_as_of, target_max_mark_age_seconds,
          target_mark_source, target_mark_timeframes
      ) AS state;
END;
$$
"""

_PAPER_POLICY_SNAPSHOT_SQL = f"""
CREATE FUNCTION public.{PAPER_POLICY_SNAPSHOT}(
    target_workspace uuid,
    target_membership uuid
) RETURNS TABLE (
    active_intents integer,
    open_positions integer,
    active_reservations integer
)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = on
AS $$
BEGIN
    PERFORM public.{ASSERT_SCOPE}(target_workspace, target_membership);
    RETURN QUERY
    SELECT
        (SELECT count(*)::integer
           FROM public.order_intent AS intent
          WHERE intent.workspace_id = target_workspace AND intent.mode = 'PAPER'
            AND intent.status IN ('ACCEPTED', 'PARTIALLY_FILLED', 'UNKNOWN')),
        (SELECT count(*)::integer
           FROM public.trade AS trade
          WHERE trade.workspace_id = target_workspace AND trade.mode = 'PAPER'
            AND trade.status = 'OPEN'),
        (SELECT count(*)::integer
           FROM public.risk_reservation AS reservation
           JOIN public.trading_account AS account
             ON account.workspace_id = reservation.workspace_id
            AND account.id = reservation.account_id
          WHERE reservation.workspace_id = target_workspace AND account.mode = 'PAPER'
            AND reservation.status IN ('RESERVED', 'UNKNOWN'));
END;
$$
"""


def _functions() -> tuple[tuple[str, str], ...]:
    """Tập hàm migration để khóa ACL execute trong test."""
    return (
        (ASSERT_SCOPE, ASSERT_SIGNATURE),
        (CAPACITY_SNAPSHOT, CAPACITY_SIGNATURE),
        (PAPER_POLICY_SNAPSHOT, PAPER_POLICY_SIGNATURE),
    )


def upgrade() -> None:
    """Tạo quyền đọc workspace hẹp cho role aggregate và hai cổng projection admin."""
    op.execute(
        f"GRANT SELECT (workspace_id, id, status) ON TABLE public.user_membership TO {AGGREGATOR}"
    )
    op.execute(
        f"GRANT SELECT (workspace_id, membership_id, role) ON TABLE public.role_binding "
        f"TO {AGGREGATOR}"
    )
    op.execute(
        f"GRANT SELECT (workspace_id, mode, status) ON TABLE public.order_intent TO {AGGREGATOR}"
    )
    op.execute("ALTER TABLE public.user_membership ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.role_binding ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.order_intent ENABLE ROW LEVEL SECURITY")
    for table, policy in (
        ("user_membership", MEMBERSHIP_POLICY),
        ("role_binding", ROLE_POLICY),
        ("order_intent", INTENT_POLICY),
    ):
        op.execute(
            f"CREATE POLICY {policy} ON public.{table} FOR SELECT TO {AGGREGATOR} "
            "USING (workspace_id = NULLIF("
            "pg_catalog.current_setting('app.workspace_id', true), '')::uuid)"
        )

    op.execute(_ASSERT_SCOPE_SQL)
    op.execute(_CAPACITY_SNAPSHOT_SQL)
    op.execute(_PAPER_POLICY_SNAPSHOT_SQL)
    for function, signature in _functions():
        op.execute(f"ALTER FUNCTION public.{function}({signature}) OWNER TO {AGGREGATOR}")
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}({signature}) FROM PUBLIC")
    for function, signature in (
        (CAPACITY_SNAPSHOT, CAPACITY_SIGNATURE),
        (PAPER_POLICY_SNAPSHOT, PAPER_POLICY_SIGNATURE),
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{function}({signature}) "
            f"FROM {ADMIN}, {ADMIN_SAFE}, {AGGREGATOR}"
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function}({signature}) TO {ADMIN_SAFE}")


def downgrade() -> None:
    """Thu hồi projection và ACL tường minh do revision này bổ sung."""
    for function, signature in (
        (CAPACITY_SNAPSHOT, CAPACITY_SIGNATURE),
        (PAPER_POLICY_SNAPSHOT, PAPER_POLICY_SIGNATURE),
        (ASSERT_SCOPE, ASSERT_SIGNATURE),
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{function}({signature}) "
            f"FROM PUBLIC, {ADMIN}, {ADMIN_SAFE}, {AGGREGATOR}"
        )
    op.execute(f"DROP FUNCTION public.{PAPER_POLICY_SNAPSHOT}({PAPER_POLICY_SIGNATURE})")
    op.execute(f"DROP FUNCTION public.{CAPACITY_SNAPSHOT}({CAPACITY_SIGNATURE})")
    op.execute(f"DROP FUNCTION public.{ASSERT_SCOPE}({ASSERT_SIGNATURE})")
    for table, policy in (
        ("order_intent", INTENT_POLICY),
        ("role_binding", ROLE_POLICY),
        ("user_membership", MEMBERSHIP_POLICY),
    ):
        op.execute(f"DROP POLICY {policy} ON public.{table}")
    op.execute(
        f"REVOKE SELECT (workspace_id, mode, status) ON TABLE public.order_intent FROM {AGGREGATOR}"
    )
    op.execute(
        f"REVOKE SELECT (workspace_id, membership_id, role) ON TABLE public.role_binding "
        f"FROM {AGGREGATOR}"
    )
    op.execute(
        f"REVOKE SELECT (workspace_id, id, status) ON TABLE public.user_membership "
        f"FROM {AGGREGATOR}"
    )
