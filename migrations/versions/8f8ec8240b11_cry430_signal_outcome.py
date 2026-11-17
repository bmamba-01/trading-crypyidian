"""CRY-430: bảng kết quả tín hiệu ``signal_outcome`` (17/11/2026).

Mỗi tín hiệu (``bot_decision`` CANDIDATE) được chấm tại các horizon cố định bằng nến đã đóng
point-in-time; một hàng cho mỗi (tín hiệu, phương pháp chấm, horizon).

- Append-only: trigger chặn UPDATE/DELETE với mọi role (``SIGNAL_OUTCOME_IMMUTABLE``); đổi
  phương pháp chấm thì ghi hàng mới với ``method_version`` mới, không sửa hàng cũ.
- FK tới ``bot_decision`` theo (workspace, bot, id) nên không ghi được kết quả cho bot khác.
- RLS bắt buộc: ``crypidian_market_signal_worker`` (worker chấm) SELECT/INSERT theo scope bot như
  ``bot_decision``; ``crypidian_admin_web`` chỉ SELECT theo workspace.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f8ec8240b11"
down_revision: str | None = "d4f6a8b0c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "signal_outcome"
WORKER = "crypidian_market_signal_worker"
ADMIN_WEB = "crypidian_admin_web"
_WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
_BOT_SCOPE = (
    f"{_WORKSPACE} AND bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid"
)


def _price() -> sa.Numeric:
    return sa.Numeric(38, 18)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("method_version", sa.String(40), nullable=False),
        sa.Column("label_version", sa.String(40), nullable=False),
        sa.Column("horizon_bars", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("strategy_id", sa.String(80), nullable=False),
        sa.Column("strategy_release_id", sa.String(80), nullable=False),
        sa.Column("signal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", _price(), nullable=True),
        sa.Column("stop_price", _price(), nullable=True),
        sa.Column("tp1_price", _price(), nullable=True),
        sa.Column("tp2_price", _price(), nullable=True),
        sa.Column("horizon_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_touch", sa.String(8), nullable=True),
        sa.Column("same_bar_conflict", sa.Boolean(), nullable=True),
        sa.Column("tp1_hit", sa.Boolean(), nullable=True),
        sa.Column("tp2_hit", sa.Boolean(), nullable=True),
        sa.Column("invalidated", sa.Boolean(), nullable=True),
        sa.Column("bars_to_first_touch", sa.Integer(), nullable=True),
        sa.Column("seconds_to_first_touch", sa.Integer(), nullable=True),
        sa.Column("mfe_r", sa.Numeric(20, 4), nullable=True),
        sa.Column("mae_r", sa.Numeric(20, 4), nullable=True),
        sa.Column("close_r", sa.Numeric(20, 4), nullable=True),
        sa.Column("outcome_r", sa.Numeric(20, 4), nullable=True),
        sa.Column("label", sa.SmallInteger(), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("detail_code", sa.String(80), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "decision_id",
            "method_version",
            "horizon_bars",
            name="uq_signal_outcome_decision_horizon",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="CASCADE",
            name="fk_signal_outcome_decision",
        ),
        sa.CheckConstraint(
            "status IN ('SCORED', 'DATA_GAP', 'INVALID_SIGNAL')", name="signal_outcome_status_valid"
        ),
        sa.CheckConstraint("horizon_bars >= 1", name="signal_outcome_horizon_positive"),
        sa.CheckConstraint("direction IN ('long', 'short')", name="signal_outcome_direction_valid"),
        sa.CheckConstraint(
            "(status = 'SCORED') = (first_touch IS NOT NULL AND outcome_r IS NOT NULL "
            "AND label IS NOT NULL AND input_hash IS NOT NULL)",
            name="signal_outcome_scored_complete",
        ),
        sa.CheckConstraint(
            "first_touch IS NULL OR first_touch IN ('TP1', 'SL', 'NONE')",
            name="signal_outcome_first_touch_valid",
        ),
        sa.CheckConstraint(
            "label IS NULL OR label IN (-1, 0, 1)", name="signal_outcome_label_valid"
        ),
        sa.CheckConstraint(
            "input_hash IS NULL OR input_hash ~ '^[0-9a-f]{64}$'",
            name="signal_outcome_input_hash_sha256",
        ),
    )
    op.create_index("ix_signal_outcome_bot_signal", TABLE, ["workspace_id", "bot_id", "signal_at"])
    op.create_index("ix_signal_outcome_symbol", TABLE, ["workspace_id", "symbol", "timeframe"])
    op.execute(
        """
        CREATE FUNCTION public.signal_outcome_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'SIGNAL_OUTCOME_IMMUTABLE';
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.signal_outcome_reject_mutation() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER signal_outcome_immutable BEFORE UPDATE OR DELETE ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.signal_outcome_reject_mutation()"
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_worker_select ON public.{TABLE} "
        f"FOR SELECT TO {WORKER} USING ({_BOT_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY {TABLE}_worker_insert ON public.{TABLE} "
        f"FOR INSERT TO {WORKER} WITH CHECK ({_BOT_SCOPE})"
    )
    op.execute(
        f"CREATE POLICY {TABLE}_admin_web_select ON public.{TABLE} "
        f"FOR SELECT TO {ADMIN_WEB} USING ({_WORKSPACE})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {WORKER}")
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN_WEB}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {ADMIN_WEB}")
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {WORKER}")
    for policy in ("worker_select", "worker_insert", "admin_web_select"):
        op.execute(f"DROP POLICY {TABLE}_{policy} ON public.{TABLE}")
    op.execute(f"DROP TRIGGER signal_outcome_immutable ON public.{TABLE}")
    op.execute("DROP FUNCTION public.signal_outcome_reject_mutation()")
    op.drop_index("ix_signal_outcome_symbol", table_name=TABLE)
    op.drop_index("ix_signal_outcome_bot_signal", table_name=TABLE)
    op.drop_table(TABLE)
