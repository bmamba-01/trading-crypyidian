"""WP-07B: Telegram signal opt-in và delivery worker quyền tối thiểu.

Worker chỉ đọc candidate decision/subscription và ghi delivery state; không được chạm order,
fill, trade, reservation hoặc dữ liệu venue.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f7b9d2"
down_revision: str | None = "e4b7d9f1a3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
WORKER = "crypidian_signal_worker"
SUBSCRIPTIONS = "telegram_signal_subscription"
DELIVERIES = "telegram_signal_delivery"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_BOT_SCOPE = (
    f"{_WORKSPACE} AND bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid"
)
_MEMBER_SCOPE = (
    f"{_WORKSPACE} AND membership_id = NULLIF("
    "pg_catalog.current_setting('app.membership_id', true), '')::uuid"
)


def _create_worker_role() -> None:
    op.execute(
        f"""DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{WORKER}'
            ) THEN
                CREATE ROLE {WORKER} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT
                    NOCREATEDB NOCREATEROLE NOREPLICATION;
            ELSIF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = '{WORKER}'
                AND (r.rolsuper OR r.rolbypassrls OR r.rolreplication
                    OR r.rolcreaterole OR r.rolcreatedb OR r.rolinherit
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members m
                        WHERE m.roleid = r.oid OR m.member = r.oid
                    ))
            ) THEN
                RAISE EXCEPTION 'WP07_SIGNAL_WORKER_ROLE_UNSAFE';
            END IF;
        END $$"""
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {WORKER}")


def upgrade() -> None:
    _create_worker_role()
    op.create_table(
        SUBSCRIPTIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_binding_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("telegram_chat_id > 0", name="telegram_signal_chat_id_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["user_membership.workspace_id", "user_membership.id"],
            ondelete="CASCADE",
            name="fk_telegram_signal_subscription_member",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id"],
            ["bot_instance.workspace_id", "bot_instance.id"],
            ondelete="CASCADE",
            name="fk_telegram_signal_subscription_bot",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "telegram_binding_id"],
            ["telegram_binding.workspace_id", "telegram_binding.id"],
            ondelete="CASCADE",
            name="fk_telegram_signal_subscription_binding",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_signal_subscription")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_telegram_signal_subscription_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id", "bot_id", "id", name="uq_telegram_signal_subscription_workspace_bot_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "membership_id",
            "bot_id",
            name="uq_telegram_signal_subscription_member_bot",
        ),
    )
    op.create_index(
        "ix_telegram_signal_subscription_ready",
        SUBSCRIPTIONS,
        ["workspace_id", "bot_id", "enabled"],
    )
    op.create_table(
        DELIVERIES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SENT', 'EXPIRED', 'DEAD')",
            name="telegram_signal_delivery_status_valid",
        ),
        sa.CheckConstraint("attempts >= 0", name="telegram_signal_delivery_attempts_nonnegative"),
        sa.CheckConstraint(
            "(status = 'SENT') = (sent_at IS NOT NULL)",
            name="telegram_signal_delivery_sent_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "subscription_id"],
            [
                f"{SUBSCRIPTIONS}.workspace_id",
                f"{SUBSCRIPTIONS}.bot_id",
                f"{SUBSCRIPTIONS}.id",
            ],
            ondelete="CASCADE",
            name="fk_telegram_signal_delivery_subscription",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["bot_decision.workspace_id", "bot_decision.id"],
            ondelete="CASCADE",
            name="fk_telegram_signal_delivery_decision",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_signal_delivery")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_telegram_signal_delivery_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "subscription_id",
            "decision_id",
            name="uq_telegram_signal_delivery_decision",
        ),
    )
    op.create_index(
        "ix_telegram_signal_delivery_ready",
        DELIVERIES,
        ["workspace_id", "status", "available_at"],
    )

    for table in (SUBSCRIPTIONS, DELIVERIES):
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY')

    op.execute(
        f"CREATE POLICY telegram_signal_subscription_admin_select ON public.{SUBSCRIPTIONS} "
        f"FOR SELECT TO {ADMIN} USING ({_MEMBER_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY telegram_signal_subscription_admin_insert ON public.{SUBSCRIPTIONS} "
        f"FOR INSERT TO {ADMIN} WITH CHECK ({_MEMBER_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY telegram_signal_subscription_admin_update ON public.{SUBSCRIPTIONS} "
        f"FOR UPDATE TO {ADMIN} USING ({_MEMBER_SCOPE}) WITH CHECK ({_MEMBER_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY telegram_signal_subscription_worker_select ON public.{SUBSCRIPTIONS} "
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY telegram_signal_binding_worker_select ON public.telegram_binding "
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE} AND verified_at IS NOT NULL "
        "AND revoked_at IS NULL AND EXISTS (SELECT 1 FROM public.telegram_signal_subscription s "
        "WHERE s.workspace_id = telegram_binding.workspace_id "
        "AND s.telegram_binding_id = telegram_binding.id AND s.enabled IS TRUE))"
    )
    op.execute(
        "CREATE POLICY telegram_signal_membership_worker_select ON public.user_membership "
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE} AND status = 'ACTIVE' AND EXISTS "
        "(SELECT 1 FROM public.telegram_signal_subscription s "
        "WHERE s.workspace_id = user_membership.workspace_id "
        "AND s.membership_id = user_membership.id AND s.enabled IS TRUE))"
    )
    op.execute(
        f"CREATE POLICY telegram_signal_delivery_admin_select ON public.{DELIVERIES} "
        f"FOR SELECT TO {ADMIN} USING ({_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY telegram_signal_delivery_worker_select ON public.{DELIVERIES} "
        f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY telegram_signal_delivery_worker_insert ON public.{DELIVERIES} "
        f"FOR INSERT TO {WORKER} WITH CHECK ({_BOT_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY telegram_signal_delivery_worker_update ON public.{DELIVERIES} "
        f"FOR UPDATE TO {WORKER} USING ({_BOT_SCOPE}) WITH CHECK ({_BOT_SCOPE})"
    )
    op.execute(
        "CREATE POLICY telegram_signal_decision_worker_select ON public.bot_decision "
        f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE} AND status = 'CANDIDATE')"
    )
    op.execute(
        "CREATE POLICY telegram_signal_feature_snapshot_worker_select "
        f"ON public.feature_snapshot FOR SELECT TO {WORKER} USING ({_WORKSPACE} AND EXISTS "
        "(SELECT 1 FROM public.bot_decision d "
        "WHERE d.workspace_id = feature_snapshot.workspace_id "
        "AND d.bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid "
        "AND d.status = 'CANDIDATE' "
        "AND d.payload ->> 'feature_snapshot_id' = feature_snapshot.snapshot_id))"
    )

    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{SUBSCRIPTIONS} TO {ADMIN}")
    op.execute(
        f"GRANT UPDATE (enabled, updated_at, telegram_binding_id, telegram_chat_id) "
        f"ON TABLE public.{SUBSCRIPTIONS} TO {ADMIN}"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{DELIVERIES} TO {ADMIN}")
    op.execute(
        f"GRANT SELECT (id, workspace_id, membership_id, bot_id, telegram_binding_id, "
        f"telegram_chat_id, enabled) ON TABLE public.{SUBSCRIPTIONS} TO {WORKER}"
    )
    op.execute(
        "GRANT SELECT (id, workspace_id, membership_id, verified_at, revoked_at) "
        f"ON TABLE public.telegram_binding TO {WORKER}"
    )
    op.execute(
        f"GRANT SELECT (id, workspace_id, status) ON TABLE public.user_membership TO {WORKER}"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{DELIVERIES} TO {WORKER}")
    op.execute(f"GRANT INSERT ON TABLE public.{DELIVERIES} TO {WORKER}")
    op.execute(
        f"GRANT UPDATE (status, attempts, available_at, lease_owner, lease_expires_at, "
        f"sent_at, last_error_code) ON TABLE public.{DELIVERIES} TO {WORKER}"
    )
    op.execute(
        "GRANT SELECT (id, workspace_id, bot_id, status, reason_code, decision_hash, "
        f"payload, created_at) ON TABLE public.bot_decision TO {WORKER}"
    )
    op.execute(
        "GRANT SELECT (workspace_id, snapshot_id, timeframe) "
        f"ON TABLE public.feature_snapshot TO {WORKER}"
    )
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM unnest(ARRAY[
                    'order_intent', 'fill', 'trade', 'trade_cost', 'position_event',
                    'risk_decision', 'risk_reservation', 'venue_order_intent',
                    'venue_order_transition', 'exchange_order'
                ]) AS t(table_name)
                CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'])
                    AS p(privilege)
                WHERE pg_catalog.has_table_privilege(
                    'crypidian_signal_worker', t.table_name, p.privilege)
            ) THEN
                RAISE EXCEPTION 'WP07_SIGNAL_WORKER_HAS_ORDER_PRIVILEGE';
            END IF;
        END $$"""
    )


def downgrade() -> None:
    op.execute(
        f"""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.{DELIVERIES})
                OR EXISTS (SELECT 1 FROM public.{SUBSCRIPTIONS}) THEN
                RAISE EXCEPTION 'WP07_SIGNAL_DOWNGRADE_REQUIRES_EXPORT';
            END IF;
        END $$"""
    )
    op.execute("DROP POLICY telegram_signal_decision_worker_select ON public.bot_decision")
    op.execute(
        "DROP POLICY telegram_signal_feature_snapshot_worker_select ON public.feature_snapshot"
    )
    op.execute("DROP POLICY telegram_signal_binding_worker_select ON public.telegram_binding")
    op.execute("DROP POLICY telegram_signal_membership_worker_select ON public.user_membership")
    for policy in (
        "telegram_signal_delivery_worker_update",
        "telegram_signal_delivery_worker_insert",
        "telegram_signal_delivery_worker_select",
        "telegram_signal_delivery_admin_select",
    ):
        op.execute(f"DROP POLICY {policy} ON public.{DELIVERIES}")
    for policy in (
        "telegram_signal_subscription_worker_select",
        "telegram_signal_subscription_admin_update",
        "telegram_signal_subscription_admin_insert",
        "telegram_signal_subscription_admin_select",
    ):
        op.execute(f"DROP POLICY {policy} ON public.{SUBSCRIPTIONS}")
    for table in (SUBSCRIPTIONS, DELIVERIES):
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {ADMIN}, {WORKER}, PUBLIC")
    op.execute("REVOKE SELECT ON TABLE public.bot_decision FROM " + WORKER)
    op.execute("REVOKE SELECT ON TABLE public.feature_snapshot FROM " + WORKER)
    op.execute(
        "REVOKE SELECT (id, workspace_id, membership_id, verified_at, revoked_at) "
        f"ON TABLE public.telegram_binding FROM {WORKER}"
    )
    op.execute(
        f"REVOKE SELECT (id, workspace_id, status) ON TABLE public.user_membership FROM {WORKER}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {WORKER}")
    op.drop_index("ix_telegram_signal_delivery_ready", table_name=DELIVERIES)
    op.drop_table(DELIVERIES)
    op.drop_index("ix_telegram_signal_subscription_ready", table_name=SUBSCRIPTIONS)
    op.drop_table(SUBSCRIPTIONS)
    op.execute(f"ALTER ROLE {WORKER} NOLOGIN PASSWORD NULL")
    op.execute(f"DROP ROLE {WORKER}")
