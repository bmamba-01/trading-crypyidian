"""Lưu hợp đồng thị trường, chuỗi nến, cursor, gap và raw reference.

Revision ID: e18f35a7c902
Revises: d0b7e9a2416c
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e18f35a7c902"
down_revision: str | None = "d0b7e9a2416c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_TABLES = (
    "market_instrument",
    "market_raw_reference",
    "market_bar",
    "market_funding",
    "market_open_interest",
    "ingestion_cursor",
    "ingestion_gap",
)
_APPEND_ONLY_TABLES = (
    "market_instrument",
    "market_raw_reference",
    "market_bar",
    "market_funding",
    "market_open_interest",
)


def upgrade() -> None:
    op.create_table(
        "market_instrument",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("base_asset", sa.String(length=32), nullable=False),
        sa.Column("quote_asset", sa.String(length=32), nullable=False),
        sa.Column("tick_size", sa.Numeric(38, 18), nullable=False),
        sa.Column("quantity_step", sa.Numeric(38, 18), nullable=False),
        sa.Column("min_notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 1", name="market_instrument_revision_positive"),
        sa.CheckConstraint("tick_size > 0", name="market_instrument_tick_size_positive"),
        sa.CheckConstraint("quantity_step > 0", name="market_instrument_quantity_step_positive"),
        sa.CheckConstraint("min_notional >= 0", name="market_instrument_min_notional_nonnegative"),
        sa.CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name="market_instrument_timestamps_causal",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_instrument")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_market_instrument_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name="uq_market_instrument_idempotency"
        ),
        sa.UniqueConstraint(
            "workspace_id", "source", "symbol", "revision", name="uq_market_instrument_revision"
        ),
    )
    op.create_index(
        "ix_market_instrument_lookup",
        "market_instrument",
        ["workspace_id", "source", "symbol", "available_at"],
    )
    op.create_table(
        "market_raw_reference",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("byte_length >= 0", name="market_raw_reference_length_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_raw_reference")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_market_raw_reference_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "source",
            "idempotency_key",
            name="uq_market_raw_reference_idempotency",
        ),
    )
    op.create_index(
        "ix_market_raw_reference_checksum", "market_raw_reference", ["workspace_id", "checksum"]
    )
    op.create_table(
        "market_bar",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("logical_key", sa.String(length=240), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("raw_reference_id", sa.Uuid(), nullable=True),
        sa.Column("source_cursor", sa.Text(), nullable=True),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(38, 18), nullable=False),
        sa.Column("high", sa.Numeric(38, 18), nullable=False),
        sa.Column("low", sa.Numeric(38, 18), nullable=False),
        sa.Column("close", sa.Numeric(38, 18), nullable=False),
        sa.Column("volume", sa.Numeric(38, 18), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 1", name="market_bar_revision_positive"),
        sa.CheckConstraint("close_time >= open_time", name="market_bar_close_after_open"),
        sa.CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name="market_bar_timestamps_causal",
        ),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="market_bar_prices_positive",
        ),
        sa.CheckConstraint(
            "high >= low AND high >= open AND high >= close",
            name="market_bar_high_consistent",
        ),
        sa.CheckConstraint("low <= open AND low <= close", name="market_bar_low_consistent"),
        sa.CheckConstraint("volume >= 0", name="market_bar_volume_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"], ["market_bar.workspace_id", "market_bar.id"]
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "raw_reference_id"],
            ["market_raw_reference.workspace_id", "market_raw_reference.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_bar")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_market_bar_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name="uq_market_bar_idempotency"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "timeframe",
            "open_time",
            "source",
            "revision",
            name="uq_market_bar_revision",
        ),
    )
    op.create_index(
        "ix_market_bar_asof",
        "market_bar",
        ["workspace_id", "symbol", "timeframe", "open_time", "available_at"],
    )
    op.create_index(
        "ix_market_bar_lineage", "market_bar", ["workspace_id", "logical_key", "revision"]
    )
    op.create_table(
        "market_funding",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_rate", sa.Numeric(38, 18), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 1", name="market_funding_revision_positive"),
        sa.CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name="market_funding_timestamps_causal",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"],
            ["market_funding.workspace_id", "market_funding.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_funding")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_market_funding_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "source", "idempotency_key", name="uq_market_funding_idempotency"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "funding_time",
            "source",
            "revision",
            name="uq_market_funding_revision",
        ),
    )
    op.create_index(
        "ix_market_funding_asof",
        "market_funding",
        ["workspace_id", "instrument_id", "funding_time", "available_at"],
    )
    op.create_table(
        "market_open_interest",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 1", name="market_open_interest_revision_positive"),
        sa.CheckConstraint(
            "event_at <= received_at AND received_at <= available_at",
            name="market_open_interest_timestamps_causal",
        ),
        sa.CheckConstraint("quantity >= 0", name="market_open_interest_quantity_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["market_instrument.workspace_id", "market_instrument.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"],
            ["market_open_interest.workspace_id", "market_open_interest.id"],
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_open_interest")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_market_open_interest_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "source",
            "idempotency_key",
            name="uq_market_open_interest_idempotency",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "observed_at",
            "source",
            "revision",
            name="uq_market_open_interest_revision",
        ),
    )
    op.create_index(
        "ix_market_open_interest_asof",
        "market_open_interest",
        ["workspace_id", "instrument_id", "observed_at", "available_at"],
    )
    op.create_table(
        "ingestion_cursor",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("covered_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(token) > 0", name="ingestion_cursor_token_nonempty"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_cursor")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_ingestion_cursor_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "source", "symbol", "timeframe", name="uq_ingestion_cursor_stream"
        ),
    )
    op.create_index(
        "ix_ingestion_cursor_asof",
        "ingestion_cursor",
        ["workspace_id", "source", "symbol", "timeframe", "updated_at"],
    )
    op.create_table(
        "ingestion_gap",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("gap_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gap_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("gap_start < gap_end", name="ingestion_gap_range_valid"),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= detected_at",
            name="ingestion_gap_resolution_after_detection",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_gap")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_ingestion_gap_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "source",
            "symbol",
            "timeframe",
            "gap_start",
            "gap_end",
            name="uq_ingestion_gap_range",
        ),
    )
    op.create_index(
        "ix_ingestion_gap_open",
        "ingestion_gap",
        ["workspace_id", "source", "symbol", "timeframe", "gap_start", "gap_end", "resolved_at"],
    )

    policy = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    for table in _TENANT_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY workspace_isolation ON "{table}" USING ({policy}) WITH CHECK ({policy})'
        )
    op.execute(
        """
        CREATE FUNCTION reject_market_data_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'market source records are append-only';
        END;
        $$
        """
    )
    for table in _APPEND_ONLY_TABLES:
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION reject_market_data_mutation()"
        )
    op.execute(
        """
        CREATE FUNCTION protect_ingestion_gap_history() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               OR (OLD.workspace_id, OLD.source, OLD.symbol, OLD.timeframe, OLD.gap_start,
                   OLD.gap_end, OLD.detected_at, OLD.created_at)
                  IS DISTINCT FROM
                  (NEW.workspace_id, NEW.source, NEW.symbol, NEW.timeframe, NEW.gap_start,
                   NEW.gap_end, NEW.detected_at, NEW.created_at)
               OR (OLD.resolved_at IS NOT NULL AND OLD.resolved_at IS DISTINCT FROM NEW.resolved_at)
            THEN
                RAISE EXCEPTION 'ingestion gap history is append-only';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER ingestion_gap_history_immutable BEFORE UPDATE OR DELETE ON ingestion_gap "
        "FOR EACH ROW EXECUTE FUNCTION protect_ingestion_gap_history()"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        + ", ".join(_TENANT_TABLES)
        + " TO crypidian_app"
    )
    op.execute(
        """
        GRANT SELECT ON TABLE market_instrument, market_bar,
            market_funding, market_open_interest TO crypidian_trading_runtime
        """
    )
    op.execute(
        """
        GRANT SELECT ON TABLE market_instrument, market_bar,
            market_funding, market_open_interest TO crypidian_trainer_worker
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON TABLE market_instrument, market_raw_reference,
            market_bar, market_funding, market_open_interest, ingestion_cursor, ingestion_gap
            TO crypidian_ingestion_worker
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER ingestion_gap_history_immutable ON ingestion_gap")
    op.execute("DROP FUNCTION protect_ingestion_gap_history()")
    for table in reversed(_APPEND_ONLY_TABLES):
        op.execute(f'DROP TRIGGER {table}_immutable ON "{table}"')
    op.execute("DROP FUNCTION reject_market_data_mutation()")
    for table in reversed(_TENANT_TABLES):
        op.execute(f'DROP POLICY workspace_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE " + ", ".join(_TENANT_TABLES) + " FROM crypidian_app"
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON TABLE market_instrument, market_bar,
            market_funding, market_open_interest FROM crypidian_trading_runtime
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON TABLE market_instrument, market_bar,
            market_funding, market_open_interest FROM crypidian_trainer_worker
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON TABLE market_instrument, market_raw_reference,
            market_bar, market_funding, market_open_interest, ingestion_cursor, ingestion_gap
            FROM crypidian_ingestion_worker
        """
    )
    for table, indexes in (
        ("ingestion_gap", ("ix_ingestion_gap_open",)),
        ("ingestion_cursor", ("ix_ingestion_cursor_asof",)),
        ("market_open_interest", ("ix_market_open_interest_asof",)),
        ("market_funding", ("ix_market_funding_asof",)),
        ("market_bar", ("ix_market_bar_asof", "ix_market_bar_lineage")),
        ("market_raw_reference", ("ix_market_raw_reference_checksum",)),
        ("market_instrument", ("ix_market_instrument_lookup",)),
    ):
        for index in indexes:
            op.drop_index(index, table_name=table)
    for table in reversed(_TENANT_TABLES):
        op.drop_table(table)
