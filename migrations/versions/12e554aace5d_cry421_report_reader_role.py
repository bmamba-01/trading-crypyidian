"""CRY-421: role DB đọc báo cáo riêng crypidian_report_reader cho Run Report / AI Analyst."""

from collections.abc import Sequence

from alembic import op

revision: str = "12e554aace5d"
down_revision: str | None = "0094c0fdba7f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "crypidian_report_reader"
WORKSPACE_SCOPE = (
    "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
)

# Chỉ các bảng phục vụ load_run_report, bối cảnh AI-4 và phiên/lưu trữ AI Analyst.
SELECT_TABLES: tuple[str, ...] = (
    "bot_instance",
    "bot_config_version",
    "bot_decision",
    "order_intent",
    "paper_candidate_outcome",
    "signal_outcome",
    "trade",
    "trade_cost",
    "fill",
    "position_event",
    "risk_reservation",
    "market_bar",
    "telegram_signal_delivery",
    "telegram_signal_subscription",
    "telegram_binding",
    "macro_calendar_fetch",
    "derivatives_snapshot",
    "listing_snapshot",
    "run_report",
    "analyst_run",
    "analyst_session",
    "proposal_backtest",
)

# Chỉ cho phép append-only INSERT trên 4 bảng đầu ra của Run Report / AI Analyst.
INSERT_TABLES: tuple[str, ...] = (
    "run_report",
    "analyst_run",
    "analyst_session",
    "proposal_backtest",
)


def upgrade() -> None:
    """Cấp quyền đọc tối thiểu + INSERT append-only có RLS cho crypidian_report_reader."""
    op.execute(
        f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} LOGIN NOINHERIT NOBYPASSRLS;
            END IF;
        END $$"""
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")

    for table in SELECT_TABLES:
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO {ROLE}")
        op.execute(
            f"CREATE POLICY {ROLE}_{table}_select ON public.{table} "
            f"FOR SELECT TO {ROLE} USING ({WORKSPACE_SCOPE})"
        )

    for table in INSERT_TABLES:
        op.execute(f"GRANT INSERT ON TABLE public.{table} TO {ROLE}")
        op.execute(
            f"CREATE POLICY {ROLE}_{table}_insert ON public.{table} "
            f"FOR INSERT TO {ROLE} WITH CHECK ({WORKSPACE_SCOPE})"
        )


def downgrade() -> None:
    """Thu hồi toàn bộ policy và quyền của crypidian_report_reader."""
    for table in reversed(INSERT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {ROLE}_{table}_insert ON public.{table}")
        op.execute(f"REVOKE INSERT ON TABLE public.{table} FROM {ROLE}")

    for table in reversed(SELECT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {ROLE}_{table}_select ON public.{table}")
        op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM {ROLE}")

    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
