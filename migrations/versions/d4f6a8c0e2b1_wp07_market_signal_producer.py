"""WP-07B: quyền riêng cho producer signal-only, tách Telegram delivery và PAPER."""

from collections.abc import Sequence

from alembic import op

revision: str = "d4f6a8c0e2b1"
down_revision: str | None = "a1c3e5f7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER = "crypidian_market_signal_worker"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_BOT_SCOPE = (
    f"{_WORKSPACE} AND bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid"
)
_EVIDENCE_EVENT = "signal.evidence.recorded.v1"
_EVIDENCE_CONSUMER = "bot-evidence-fanout"

_TABLES = (
    "market_bar",
    "feature_snapshot",
    "bot_instance",
    "trading_account",
    "bot_config_version",
    "workspace_settings_version",
    "risk_profile_version",
    "strategy_release",
    "workspace_runtime_control",
    "bot_runtime_control",
    "account_activity_incident",
    "shared_signal_evidence",
    "bot_decision_job",
    "bot_decision",
    "outbox_event",
    "consumer_receipt",
)
_FORBIDDEN_TABLES = (
    "trade_plan",
    "risk_decision",
    "risk_reservation",
    "order_intent",
    "exchange_order",
    "fill",
    "position_event",
    "trade",
    "trade_cost",
    "venue_order_intent",
    "venue_order_transition",
)


def _create_role() -> None:
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
                RAISE EXCEPTION 'WP07_MARKET_SIGNAL_ROLE_UNSAFE';
            END IF;
        END $$"""
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {WORKER}")


def _create_policies() -> None:
    op.execute(
        "CREATE POLICY market_signal_bot_select ON public.bot_instance FOR SELECT "
        f"TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY market_signal_account_select ON public.trading_account FOR SELECT "
        f"TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY market_signal_workspace_settings_select "
        "ON public.workspace_settings_version FOR SELECT "
        f"TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY market_signal_risk_profile_select ON public.risk_profile_version "
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY market_signal_strategy_release_select ON public.strategy_release "
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY market_signal_bot_config_select ON public.bot_config_version "
        f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE} AND status = 'PUBLISHED')"
    )
    op.execute(
        "CREATE POLICY market_signal_workspace_control_select "
        "ON public.workspace_runtime_control FOR SELECT "
        f"TO {WORKER} USING ({_WORKSPACE})"
    )
    op.execute(
        "CREATE POLICY market_signal_bot_control_select ON public.bot_runtime_control "
        f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE})"
    )
    op.execute(
        "CREATE POLICY market_signal_incident_select ON public.account_activity_incident "
        f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE})"
    )
    op.execute(
        "CREATE POLICY market_signal_job_select ON public.bot_decision_job "
        f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE})"
    )
    op.execute(
        "CREATE POLICY market_signal_job_insert ON public.bot_decision_job "
        f"FOR INSERT TO {WORKER} WITH CHECK ({_BOT_SCOPE})"
    )
    op.execute(
        "CREATE POLICY market_signal_job_update ON public.bot_decision_job "
        f"FOR UPDATE TO {WORKER} USING ({_BOT_SCOPE}) WITH CHECK ({_BOT_SCOPE})"
    )
    op.execute(
        "CREATE POLICY market_signal_decision_select ON public.bot_decision "
        f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE})"
    )
    op.execute(
        "CREATE POLICY market_signal_decision_insert ON public.bot_decision "
        f"FOR INSERT TO {WORKER} WITH CHECK ({_BOT_SCOPE})"
    )
    op.execute(
        "CREATE POLICY market_signal_evidence_outbox_select ON public.outbox_event "
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE} AND event_type = '{_EVIDENCE_EVENT}')"
    )
    op.execute(
        "CREATE POLICY market_signal_evidence_outbox_insert ON public.outbox_event "
        f"FOR INSERT TO {WORKER} WITH CHECK ({_WORKSPACE} AND event_type = '{_EVIDENCE_EVENT}')"
    )
    op.execute(
        "CREATE POLICY market_signal_evidence_outbox_update ON public.outbox_event "
        f"FOR UPDATE TO {WORKER} USING ({_WORKSPACE} AND event_type = '{_EVIDENCE_EVENT}') "
        f"WITH CHECK ({_WORKSPACE} AND event_type = '{_EVIDENCE_EVENT}')"
    )
    op.execute(
        "CREATE POLICY market_signal_evidence_receipt_select ON public.consumer_receipt "
        f"FOR SELECT TO {WORKER} USING ({_WORKSPACE} AND consumer_name = '{_EVIDENCE_CONSUMER}')"
    )
    op.execute(
        "CREATE POLICY market_signal_evidence_receipt_insert ON public.consumer_receipt "
        f"FOR INSERT TO {WORKER} WITH CHECK ({_WORKSPACE} "
        f"AND consumer_name = '{_EVIDENCE_CONSUMER}')"
    )


def _grant_privileges() -> None:
    op.execute(f"GRANT SELECT ON TABLE public.market_bar TO {WORKER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.feature_snapshot TO {WORKER}")
    for table in (
        "bot_instance",
        "trading_account",
        "bot_config_version",
        "workspace_settings_version",
        "risk_profile_version",
        "strategy_release",
        "workspace_runtime_control",
        "bot_runtime_control",
        "account_activity_incident",
    ):
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {WORKER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.shared_signal_evidence TO {WORKER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.bot_decision_job TO {WORKER}")
    op.execute(f"GRANT UPDATE (status, completed_at) ON TABLE public.bot_decision_job TO {WORKER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.bot_decision TO {WORKER}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.outbox_event TO {WORKER}")
    op.execute(
        f"GRANT UPDATE (status, attempts, available_at, lease_owner, lease_expires_at, "
        f"completed_at, last_error_code) ON TABLE public.outbox_event TO {WORKER}"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.consumer_receipt TO {WORKER}")
    _assert_role_boundary()


def _assert_role_boundary() -> None:
    forbidden = ", ".join(f"'{table}'" for table in _FORBIDDEN_TABLES)
    op.execute(
        f"""DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM unnest(ARRAY[{forbidden}]) AS t(table_name)
                CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'])
                    AS p(privilege)
                WHERE pg_catalog.has_table_privilege('{WORKER}', t.table_name, p.privilege)
                    OR (p.privilege IN ('SELECT', 'INSERT', 'UPDATE') AND
                        pg_catalog.has_any_column_privilege(
                            '{WORKER}', t.table_name, p.privilege))
            ) THEN
                RAISE EXCEPTION 'WP07_MARKET_SIGNAL_WORKER_HAS_EXECUTION_PRIVILEGE';
            END IF;
        END $$"""
    )


def upgrade() -> None:
    _create_role()
    _create_policies()
    _grant_privileges()


def downgrade() -> None:
    for policy in (
        "market_signal_evidence_receipt_insert",
        "market_signal_evidence_receipt_select",
    ):
        op.execute(f"DROP POLICY {policy} ON public.consumer_receipt")
    for policy in (
        "market_signal_evidence_outbox_update",
        "market_signal_evidence_outbox_insert",
        "market_signal_evidence_outbox_select",
    ):
        op.execute(f"DROP POLICY {policy} ON public.outbox_event")
    for policy in ("market_signal_decision_insert", "market_signal_decision_select"):
        op.execute(f"DROP POLICY {policy} ON public.bot_decision")
    for policy in (
        "market_signal_job_update",
        "market_signal_job_insert",
        "market_signal_job_select",
    ):
        op.execute(f"DROP POLICY {policy} ON public.bot_decision_job")
    for policy, table in (
        ("market_signal_incident_select", "account_activity_incident"),
        ("market_signal_bot_control_select", "bot_runtime_control"),
        ("market_signal_workspace_control_select", "workspace_runtime_control"),
        ("market_signal_bot_config_select", "bot_config_version"),
        ("market_signal_account_select", "trading_account"),
        ("market_signal_workspace_settings_select", "workspace_settings_version"),
        ("market_signal_risk_profile_select", "risk_profile_version"),
        ("market_signal_strategy_release_select", "strategy_release"),
        ("market_signal_bot_select", "bot_instance"),
    ):
        op.execute(f"DROP POLICY {policy} ON public.{table}")
    for table in _TABLES:
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM {WORKER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {WORKER}")
