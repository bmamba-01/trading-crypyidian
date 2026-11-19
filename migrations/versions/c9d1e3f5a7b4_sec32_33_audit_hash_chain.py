"""SEC-32/33: chuỗi băm bất biến trên audit_event, ngữ cảnh bảo mật và chặn TRUNCATE.

Revision ID: c9d1e3f5a7b4
Revises: c8d28da699c6
Create Date: 2026-11-19 08:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d1e3f5a7b4"
down_revision: str | Sequence[str] | None = "f2a4c6e8b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD_ROLE = "crypidian_audit_chain_guard"
INSERT_ROLES = (
    "crypidian_admin_web",
    "crypidian_trading_runtime",
    "crypidian_execution_gateway",
    "crypidian_ingestion_worker",
    "crypidian_trainer_worker",
)
COMPUTE_HASH_SIGNATURE = (
    "public.audit_event_compute_hash("
    "uuid, uuid, uuid, text, text, uuid, text, text, text, uuid, "
    "timestamp with time zone, bigint, text, text, timestamp with time zone, "
    "text, text, text, text)"
)


def upgrade() -> None:
    """Thêm cột chuỗi băm SEC-33, cột ngữ cảnh SEC-32, trigger BEFORE INSERT và chặn TRUNCATE."""
    op.add_column("audit_event", sa.Column("seq", sa.BigInteger(), nullable=True))
    op.add_column("audit_event", sa.Column("prev_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_event", sa.Column("row_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_event", sa.Column("db_role", sa.String(length=64), nullable=True))
    op.add_column(
        "audit_event",
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("audit_event", sa.Column("session_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_event", sa.Column("client_ip", sa.String(length=64), nullable=True))
    op.add_column("audit_event", sa.Column("user_agent", sa.String(length=160), nullable=True))
    op.add_column("audit_event", sa.Column("outcome", sa.String(length=24), nullable=True))

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{GUARD_ROLE}') THEN
                CREATE ROLE {GUARD_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
            END IF;
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.audit_event FROM {GUARD_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {GUARD_ROLE}")
    op.execute(f"GRANT SELECT ON TABLE public.audit_event TO {GUARD_ROLE}")
    op.execute(
        f"""
        CREATE POLICY audit_chain_guard_select ON public.audit_event
            AS PERMISSIVE
            FOR SELECT
            TO {GUARD_ROLE}
            USING (
                workspace_id = NULLIF(
                    pg_catalog.current_setting('app.audit_chain_workspace_id', true),
                    ''
                )::pg_catalog.uuid
            )
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.audit_event_compute_hash(
            p_id uuid,
            p_workspace_id uuid,
            p_actor_membership_id uuid,
            p_action text,
            p_target_type text,
            p_target_id uuid,
            p_reason text,
            p_before_hash text,
            p_after_hash text,
            p_request_id uuid,
            p_occurred_at timestamp with time zone,
            p_seq bigint,
            p_prev_hash text,
            p_db_role text,
            p_recorded_at timestamp with time zone,
            p_session_hash text,
            p_client_ip text,
            p_user_agent text,
            p_outcome text
        ) RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT pg_catalog.encode(
                pg_catalog.sha256(
                    pg_catalog.convert_to(
                        p_prev_hash || (
                            '{'
                            || '"action":'
                            || COALESCE(pg_catalog.to_json(p_action::text)::text, 'null')
                            || ',"actor_membership_id":'
                            || COALESCE(
                        pg_catalog.to_json(p_actor_membership_id::text)::text, 'null'
                    )
                            || ',"after_hash":'
                            || COALESCE(pg_catalog.to_json(p_after_hash::text)::text, 'null')
                            || ',"before_hash":'
                            || COALESCE(pg_catalog.to_json(p_before_hash::text)::text, 'null')
                            || ',"client_ip":'
                            || COALESCE(pg_catalog.to_json(p_client_ip::text)::text, 'null')
                            || ',"db_role":'
                            || COALESCE(pg_catalog.to_json(p_db_role::text)::text, 'null')
                            || ',"id":'
                            || COALESCE(pg_catalog.to_json(p_id::text)::text, 'null')
                            || ',"occurred_at":'
                            || pg_catalog.to_json(
                                pg_catalog.to_char(
                                    p_occurred_at AT TIME ZONE 'UTC',
                                    'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'
                                )
                            )::text
                            || ',"outcome":'
                            || COALESCE(pg_catalog.to_json(p_outcome::text)::text, 'null')
                            || ',"prev_hash":'
                            || COALESCE(pg_catalog.to_json(p_prev_hash::text)::text, 'null')
                            || ',"reason":'
                            || COALESCE(pg_catalog.to_json(p_reason::text)::text, 'null')
                            || ',"recorded_at":'
                            || pg_catalog.to_json(
                                pg_catalog.to_char(
                                    p_recorded_at AT TIME ZONE 'UTC',
                                    'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'
                                )
                            )::text
                            || ',"request_id":'
                            || COALESCE(pg_catalog.to_json(p_request_id::text)::text, 'null')
                            || ',"seq":'
                            || p_seq::text
                            || ',"session_hash":'
                            || COALESCE(pg_catalog.to_json(p_session_hash::text)::text, 'null')
                            || ',"target_id":'
                            || COALESCE(pg_catalog.to_json(p_target_id::text)::text, 'null')
                            || ',"target_type":'
                            || COALESCE(pg_catalog.to_json(p_target_type::text)::text, 'null')
                            || ',"user_agent":'
                            || COALESCE(pg_catalog.to_json(p_user_agent::text)::text, 'null')
                            || ',"workspace_id":'
                            || COALESCE(pg_catalog.to_json(p_workspace_id::text)::text, 'null')
                            || '}'
                        ),
                        'UTF8'
                    )
                ),
                'hex'
            );
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {COMPUTE_HASH_SIGNATURE} FROM PUBLIC")
    for role_name in INSERT_ROLES:
        op.execute(f"GRANT EXECUTE ON FUNCTION {COMPUTE_HASH_SIGNATURE} TO {role_name}")

    # Backfill các dòng có sẵn (nếu có) theo từng workspace trước khi khóa NOT NULL.
    op.execute(
        """
        DO $$
        DECLARE
            rec RECORD;
            v_prev_ws uuid := NULL;
            v_seq bigint := 0;
            v_prev_hash text := 'GENESIS';
            v_row_hash text;
        BEGIN
            IF EXISTS (SELECT 1 FROM public.audit_event) THEN
                ALTER TABLE public.audit_event DISABLE TRIGGER audit_event_append_only;
                FOR rec IN
                    SELECT
                        id,
                        workspace_id,
                        actor_membership_id,
                        action,
                        target_type,
                        target_id,
                        reason,
                        before_hash,
                        after_hash,
                        request_id,
                        occurred_at,
                        session_hash,
                        client_ip,
                        user_agent,
                        outcome
                    FROM public.audit_event
                    ORDER BY workspace_id ASC, occurred_at ASC, id ASC
                LOOP
                    IF v_prev_ws IS DISTINCT FROM rec.workspace_id THEN
                        v_prev_ws := rec.workspace_id;
                        v_seq := 1;
                        v_prev_hash := 'GENESIS';
                    ELSE
                        v_seq := v_seq + 1;
                    END IF;

                    v_row_hash := public.audit_event_compute_hash(
                        rec.id,
                        rec.workspace_id,
                        rec.actor_membership_id,
                        rec.action,
                        rec.target_type,
                        rec.target_id,
                        rec.reason,
                        rec.before_hash,
                        rec.after_hash,
                        rec.request_id,
                        rec.occurred_at,
                        v_seq,
                        v_prev_hash,
                        'migration_backfill',
                        rec.occurred_at,
                        rec.session_hash,
                        rec.client_ip,
                        rec.user_agent,
                        rec.outcome
                    );

                    UPDATE public.audit_event
                    SET
                        seq = v_seq,
                        prev_hash = v_prev_hash,
                        row_hash = v_row_hash,
                        db_role = 'migration_backfill',
                        recorded_at = rec.occurred_at
                    WHERE id = rec.id;

                    v_prev_hash := v_row_hash;
                END LOOP;
                ALTER TABLE public.audit_event ENABLE TRIGGER audit_event_append_only;
            END IF;
        END
        $$
        """
    )

    op.alter_column("audit_event", "seq", nullable=False)
    op.alter_column("audit_event", "prev_hash", nullable=False)
    op.alter_column("audit_event", "row_hash", nullable=False)
    op.alter_column("audit_event", "db_role", nullable=False)
    op.alter_column("audit_event", "recorded_at", nullable=False)

    op.create_unique_constraint(
        "uq_audit_event_workspace_seq",
        "audit_event",
        ["workspace_id", "seq"],
    )
    op.create_check_constraint(
        op.f("ck_audit_event_seq_positive"),
        "audit_event",
        "seq >= 1",
    )
    op.create_check_constraint(
        op.f("ck_audit_event_prev_hash_valid"),
        "audit_event",
        "prev_hash = 'GENESIS' OR length(prev_hash) = 64",
    )
    op.create_check_constraint(
        op.f("ck_audit_event_row_hash_sha256"),
        "audit_event",
        "length(row_hash) = 64",
    )
    op.create_check_constraint(
        op.f("ck_audit_event_db_role_nonempty"),
        "audit_event",
        "length(db_role) > 0",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.audit_event_next_chain_link(
            p_workspace_id uuid,
            OUT next_seq bigint,
            OUT prev_hash text
        ) RETURNS record
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_last_seq bigint;
            v_last_hash text;
            v_prev_guc text;
        BEGIN
            IF pg_catalog.pg_trigger_depth() < 1 THEN
                RAISE EXCEPTION 'audit_event_next_chain_link may only be invoked from trigger';
            END IF;

            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtext('audit_event_chain'),
                pg_catalog.hashtext(p_workspace_id::text)
            );

            v_prev_guc := pg_catalog.current_setting('app.audit_chain_workspace_id', true);
            PERFORM pg_catalog.set_config(
                'app.audit_chain_workspace_id',
                p_workspace_id::text,
                true
            );

            SELECT a.seq, a.row_hash
            INTO v_last_seq, v_last_hash
            FROM public.audit_event AS a
            WHERE a.workspace_id = p_workspace_id
            ORDER BY a.seq DESC
            LIMIT 1;

            PERFORM pg_catalog.set_config(
                'app.audit_chain_workspace_id',
                COALESCE(v_prev_guc, ''),
                true
            );

            IF v_last_seq IS NULL THEN
                next_seq := 1;
                prev_hash := 'GENESIS';
            ELSE
                next_seq := v_last_seq + 1;
                prev_hash := v_last_hash;
            END IF;
        END;
        $$
        """
    )
    op.execute(f"ALTER FUNCTION public.audit_event_next_chain_link(uuid) OWNER TO {GUARD_ROLE}")
    op.execute("REVOKE ALL ON FUNCTION public.audit_event_next_chain_link(uuid) FROM PUBLIC")
    for role_name in INSERT_ROLES:
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.audit_event_next_chain_link(uuid) TO {role_name}"
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.audit_event_assign_hash_chain()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_next_seq bigint;
            v_prev_hash text;
        BEGIN
            NEW.db_role := CURRENT_USER;
            NEW.recorded_at := pg_catalog.clock_timestamp();

            SELECT link.next_seq, link.prev_hash
            INTO v_next_seq, v_prev_hash
            FROM public.audit_event_next_chain_link(NEW.workspace_id) AS link;

            NEW.seq := v_next_seq;
            NEW.prev_hash := v_prev_hash;
            NEW.row_hash := public.audit_event_compute_hash(
                NEW.id,
                NEW.workspace_id,
                NEW.actor_membership_id,
                NEW.action,
                NEW.target_type,
                NEW.target_id,
                NEW.reason,
                NEW.before_hash,
                NEW.after_hash,
                NEW.request_id,
                NEW.occurred_at,
                NEW.seq,
                NEW.prev_hash,
                NEW.db_role,
                NEW.recorded_at,
                NEW.session_hash,
                NEW.client_ip,
                NEW.user_agent,
                NEW.outcome
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.audit_event_assign_hash_chain() FROM PUBLIC")
    for role_name in INSERT_ROLES:
        op.execute(
            f"GRANT EXECUTE ON FUNCTION public.audit_event_assign_hash_chain() TO {role_name}"
        )

    op.execute(
        """
        CREATE TRIGGER audit_event_assign_hash_chain
        BEFORE INSERT ON public.audit_event
        FOR EACH ROW EXECUTE FUNCTION public.audit_event_assign_hash_chain()
        """
    )

    op.execute(
        "REVOKE TRUNCATE ON TABLE public.audit_event FROM "
        "PUBLIC, crypidian_app, crypidian_admin_web, crypidian_trading_runtime, "
        "crypidian_execution_gateway, crypidian_ingestion_worker, crypidian_trainer_worker, "
        f"{GUARD_ROLE}"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.reject_audit_event_truncate()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only: TRUNCATE is forbidden';
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.reject_audit_event_truncate() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER audit_event_forbid_truncate
        BEFORE TRUNCATE ON public.audit_event
        FOR EACH STATEMENT EXECUTE FUNCTION public.reject_audit_event_truncate()
        """
    )


def downgrade() -> None:
    """Gỡ trigger/cột chuỗi băm SEC-33 và ngữ cảnh SEC-32 trên audit_event."""
    op.execute("DROP TRIGGER IF EXISTS audit_event_forbid_truncate ON public.audit_event")
    op.execute("DROP FUNCTION IF EXISTS public.reject_audit_event_truncate()")
    op.execute("DROP TRIGGER IF EXISTS audit_event_assign_hash_chain ON public.audit_event")
    op.execute("DROP FUNCTION IF EXISTS public.audit_event_assign_hash_chain()")
    op.execute("DROP FUNCTION IF EXISTS public.audit_event_next_chain_link(uuid)")
    op.execute(f"DROP FUNCTION IF EXISTS {COMPUTE_HASH_SIGNATURE}")
    op.execute("DROP POLICY IF EXISTS audit_chain_guard_select ON public.audit_event")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.audit_event FROM {GUARD_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {GUARD_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {GUARD_ROLE}")

    op.drop_constraint(op.f("ck_audit_event_db_role_nonempty"), "audit_event", type_="check")
    op.drop_constraint(op.f("ck_audit_event_row_hash_sha256"), "audit_event", type_="check")
    op.drop_constraint(op.f("ck_audit_event_prev_hash_valid"), "audit_event", type_="check")
    op.drop_constraint(op.f("ck_audit_event_seq_positive"), "audit_event", type_="check")
    op.drop_constraint("uq_audit_event_workspace_seq", "audit_event", type_="unique")

    for col in (
        "outcome",
        "user_agent",
        "client_ip",
        "session_hash",
        "recorded_at",
        "db_role",
        "row_hash",
        "prev_hash",
        "seq",
    ):
        op.drop_column("audit_event", col)
