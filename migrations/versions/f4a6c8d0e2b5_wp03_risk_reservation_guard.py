"""Gia cố quyền invoker và search_path cho trigger reservation WP-03."""

from collections.abc import Sequence

from alembic import op

revision: str = "f4a6c8d0e2b5"
down_revision: str | None = "e0b2d4f6a8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD_ROLE = "crypidian_risk_reservation_guard"
FUNCTION = "public.protect_risk_reservation_transition()"


def upgrade() -> None:
    # Role sở hữu hàm là đối tượng cấp cluster; chỉ chấp nhận cấu hình không thể đăng nhập
    # hoặc nâng quyền.
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{GUARD_ROLE}') THEN
            CREATE ROLE {GUARD_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                NOCREATEDB NOCREATEROLE NOREPLICATION;
        ELSIF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = '{GUARD_ROLE}'
            AND (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolreplication
                OR r.rolcreaterole OR r.rolcreatedb OR r.rolinherit
                OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                            WHERE m.roleid = r.oid OR m.member = r.oid))
        ) THEN
            RAISE EXCEPTION 'WP03_RISK_GUARD_ROLE_UNSAFE';
        END IF;
    END $$""")
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {GUARD_ROLE}")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.protect_risk_reservation_transition() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'risk reservation cannot be deleted';
            END IF;
            IF (OLD.id, OLD.workspace_id, OLD.account_id, OLD.idempotency_key, OLD.plan_hash,
                OLD.risk_decision_id, OLD.symbol, OLD.reserved_risk, OLD.reserved_notional,
                OLD.reserved_initial_margin, OLD.created_at)
               IS DISTINCT FROM
               (NEW.id, NEW.workspace_id, NEW.account_id, NEW.idempotency_key, NEW.plan_hash,
                NEW.risk_decision_id, NEW.symbol, NEW.reserved_risk, NEW.reserved_notional,
                NEW.reserved_initial_margin, NEW.created_at) THEN
                RAISE EXCEPTION 'risk reservation identity and budget are immutable';
            END IF;
            IF OLD.status = NEW.status THEN
                IF OLD.resolution_reason IS DISTINCT FROM NEW.resolution_reason THEN
                    RAISE EXCEPTION 'risk reservation resolution reason is immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'UNKNOWN'
               AND NEW.resolution_reason IS NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason IN (
                   'EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT',
                   'EXPIRED_NO_FILL', 'CANCELLED_NO_FILL'
               ) THEN
                IF EXISTS (
                    SELECT 1 FROM public.venue_order_intent
                     WHERE workspace_id = OLD.workspace_id
                       AND bot_id = OLD.bot_id
                       AND account_id = OLD.account_id
                       AND reservation_id = OLD.id
                       AND state IN (
                           'SUBMITTING', 'UNKNOWN', 'ACKNOWLEDGED',
                           'PARTIALLY_FILLED', 'FILLED'
                       )
                ) THEN
                    RAISE EXCEPTION 'WP03_VENUE_ORDER_RESERVATION_IN_USE';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'TRANSFERRED'
               AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN
                IF EXISTS (
                    SELECT 1 FROM public.venue_order_intent
                     WHERE workspace_id = OLD.workspace_id
                       AND bot_id = OLD.bot_id
                       AND account_id = OLD.account_id
                       AND reservation_id = OLD.id
                ) THEN
                    RAISE EXCEPTION 'WP03_VENUE_ORDER_TRANSFER_NEEDS_OPEN_RISK_RECORD';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason = 'ORDER_ABSENT' THEN
                IF EXISTS (
                    SELECT 1 FROM public.venue_order_intent
                     WHERE workspace_id = OLD.workspace_id
                       AND bot_id = OLD.bot_id
                       AND account_id = OLD.account_id
                       AND reservation_id = OLD.id
                ) THEN
                    RAISE EXCEPTION USING MESSAGE =
                        'WP03_VENUE_ORDER_RESERVATION_AUTHENTICATED_RECONCILIATION_REQUIRED';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'TRANSFERRED'
               AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM public.venue_order_intent
                     WHERE workspace_id = OLD.workspace_id
                       AND bot_id = OLD.bot_id
                       AND account_id = OLD.account_id
                       AND reservation_id = OLD.id
                ) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'WP03_VENUE_ORDER_TRANSFER_NEEDS_OPEN_RISK_RECORD';
            END IF;
            RAISE EXCEPTION 'invalid risk reservation status transition';
        END;
        $$
        """
    )
    # CREATE OR REPLACE giữ ACL cũ. Thu hồi mọi EXECUTE ngoài owner hiện tại,
    # kể cả role tích hợp không thuộc danh sách process role đã biết.
    op.execute(
        """
        DO $$
        DECLARE
            acl_grantee record;
            grantee_sql text;
        BEGIN
            FOR acl_grantee IN
                SELECT DISTINCT privilege.grantee, role.rolname
                  FROM pg_catalog.pg_proc AS function
                  CROSS JOIN LATERAL pg_catalog.aclexplode(
                      COALESCE(function.proacl,
                               pg_catalog.acldefault('f', function.proowner))
                  ) AS privilege
                  LEFT JOIN pg_catalog.pg_roles AS role
                    ON role.oid = privilege.grantee
                 WHERE function.oid =
                       'public.protect_risk_reservation_transition()'::regprocedure
                   AND privilege.privilege_type = 'EXECUTE'
                   AND privilege.grantee <> function.proowner
            LOOP
                IF acl_grantee.grantee = 0 THEN
                    grantee_sql := 'PUBLIC';
                ELSIF acl_grantee.rolname IS NULL THEN
                    RAISE EXCEPTION 'WP03_RISK_GUARD_ACL_UNKNOWN_GRANTEE';
                ELSE
                    grantee_sql := format('%I', acl_grantee.rolname);
                END IF;
                EXECUTE format(
                    'REVOKE EXECUTE ON FUNCTION public.protect_risk_reservation_transition() '
                    'FROM %s CASCADE',
                    grantee_sql
                );
            END LOOP;
        END
        $$
        """
    )
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {GUARD_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {GUARD_ROLE}")


def downgrade() -> None:
    raise RuntimeError(
        "WP03_RISK_GUARD_DOWNGRADE_BLOCKED: giữ SECURITY INVOKER, RLS và EXECUTE đã thu hẹp; "
        "không khôi phục định nghĩa lịch sử thiếu an toàn."
    )
