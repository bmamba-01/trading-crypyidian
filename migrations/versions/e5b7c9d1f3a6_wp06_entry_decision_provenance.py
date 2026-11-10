"""WP06: giữ bằng chứng feature tại quyết định entry trên outcome bất biến."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5b7c9d1f3a6"
down_revision: str | None = "f3a5c7d9e1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "backtest_trade_outcome"
CONSTRAINT = "entry_provenance_valid"
PROVENANCE_CHECK = (
    "(entry_decision_snapshot_id IS NULL AND entry_decision_at IS NULL AND "
    "entry_feature_schema_hash IS NULL AND entry_features IS NULL AND "
    "entry_features_hash IS NULL AND entry_symbol IS NULL) OR "
    "(entry_decision_snapshot_id IS NOT NULL AND "
    "length(btrim(entry_decision_snapshot_id)) > 0 AND entry_decision_at IS NOT NULL "
    "AND entry_feature_schema_hash IS NOT NULL AND entry_features IS NOT NULL AND "
    "entry_features_hash IS NOT NULL AND entry_symbol IS NOT NULL AND entry_symbol ~ "
    "'^[A-Z0-9_]{2,32}$' AND entry_feature_schema_hash ~ '^[0-9a-f]{64}$' AND "
    "entry_features_hash ~ '^[0-9a-f]{64}$' AND "
    "public.wp06_entry_features_valid(entry_features))"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.wp06_entry_features_valid(features jsonb) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            item jsonb;
            feature_key text;
            previous_key text := NULL;
        BEGIN
            IF jsonb_typeof(features) <> 'array' THEN RETURN FALSE; END IF;
            IF jsonb_array_length(features) = 0 THEN RETURN FALSE; END IF;
            FOR item IN SELECT value FROM jsonb_array_elements(features) LOOP
                IF jsonb_typeof(item) <> 'array' THEN RETURN FALSE; END IF;
                IF jsonb_array_length(item) <> 2 THEN RETURN FALSE; END IF;
                IF jsonb_typeof(item -> 0) <> 'string'
                   OR jsonb_typeof(item -> 1) <> 'number' THEN RETURN FALSE; END IF;
                feature_key := item ->> 0;
                IF length(btrim(feature_key)) = 0 OR btrim(feature_key) <> feature_key
                   OR feature_key ~ '^[[:space:]]|[[:space:]]$'
                   OR (previous_key IS NOT NULL
                       AND feature_key COLLATE "C" <= previous_key COLLATE "C")
                   OR abs((item ->> 1)::numeric) > 1.7976931348623157e308
                THEN RETURN FALSE; END IF;
                previous_key := feature_key;
            END LOOP;
            RETURN TRUE;
        END; $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.wp06_entry_features_valid(jsonb) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_entry_features_valid(jsonb) "
        "TO crypidian_app, crypidian_trainer_worker"
    )
    op.add_column(TABLE, sa.Column("entry_decision_snapshot_id", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("entry_decision_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("entry_feature_schema_hash", sa.String(64), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("entry_features", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.add_column(TABLE, sa.Column("entry_features_hash", sa.String(64), nullable=True))
    op.add_column(TABLE, sa.Column("entry_symbol", sa.String(32), nullable=True))
    op.create_check_constraint(CONSTRAINT, TABLE, PROVENANCE_CHECK)


def downgrade() -> None:
    # Khóa trước guard để writer không chèn provenance giữa phép kiểm và DROP COLUMN.
    op.execute("LOCK TABLE public.backtest_trade_outcome IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.backtest_trade_outcome "
        "WHERE entry_decision_snapshot_id IS NOT NULL OR entry_decision_at IS NOT NULL "
        "OR entry_feature_schema_hash IS NOT NULL OR entry_features IS NOT NULL "
        "OR entry_features_hash IS NOT NULL OR entry_symbol IS NOT NULL) THEN "
        "RAISE EXCEPTION 'WP06_ENTRY_PROVENANCE_DOWNGRADE_REQUIRES_NO_PROVENANCE'; "
        "END IF; END $$"
    )
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    for column in (
        "entry_symbol",
        "entry_features_hash",
        "entry_features",
        "entry_feature_schema_hash",
        "entry_decision_at",
        "entry_decision_snapshot_id",
    ):
        op.drop_column(TABLE, column)
    op.execute("DROP FUNCTION public.wp06_entry_features_valid(jsonb)")
