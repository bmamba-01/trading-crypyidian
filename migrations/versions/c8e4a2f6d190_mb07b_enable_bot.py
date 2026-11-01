"""MB-07b: cho phép application service bật lại account PAPER đã disable.

Chuyển DISABLED → ENABLED vẫn bị DB từ chối nếu service không đặt transaction-local marker sau khi
đã giữ L1 và kiểm tra readiness, activity reconciliation, incident, kill switch, config và MFA.
Marker chỉ sống trong transaction; account status tiếp tục được mirror sang bot_instance.

Revision ID: c8e4a2f6d190
Revises: b3d5f7a9c1e4
"""

from alembic import op

revision = "c8e4a2f6d190"
down_revision = "b3d5f7a9c1e4"
branch_labels = None
depends_on = None

_GUARD_FUNCTION = """CREATE OR REPLACE FUNCTION public.mb07a_guard_account_status() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF current_user = 'crypidian_admin_web' AND NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'ENABLED' AND NEW.status = 'DISABLED')
       AND NOT (OLD.status = 'DISABLED' AND NEW.status = 'ENABLED'
                AND COALESCE(current_setting('app.bot_enable_authorized', true), '') = 'on') THEN
        RAISE EXCEPTION 'MB07A_ACCOUNT_STATUS_TRANSITION_DENIED'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$"""

_DOWN_GUARD_FUNCTION = """CREATE OR REPLACE FUNCTION public.mb07a_guard_account_status()
RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF current_user = 'crypidian_admin_web' AND NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'ENABLED' AND NEW.status = 'DISABLED') THEN
        RAISE EXCEPTION 'MB07A_ACCOUNT_STATUS_TRANSITION_DENIED'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$"""


def upgrade() -> None:
    op.execute(_GUARD_FUNCTION)


def downgrade() -> None:
    op.execute(_DOWN_GUARD_FUNCTION)
