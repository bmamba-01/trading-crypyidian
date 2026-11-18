"""WP06: ghim lineage MarketBar của từng BotDecision, không lưu OHLCV."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e7f9a1c3b6"
down_revision: str | None = "c7e9a1b3d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER = "crypidian_market_signal_worker"


def upgrade() -> None:
    """Tạo ledger tenant-scoped, append-only cho bar nguồn của quyết định."""
    op.create_table(
        "bot_decision_source_bar",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("bot_decision_id", sa.Uuid(), nullable=False),
        sa.Column("decision_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("feature_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("market_bar_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.String(length=80), nullable=False),
        sa.Column("source_event_digest", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("timeframe_bar_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_timeframe_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "decision_snapshot_id ~ '^[0-9a-f]{64}$'",
            name="bot_decision_source_bar_snapshot_hash_valid",
        ),
        sa.CheckConstraint(
            "feature_snapshot_id ~ '^[0-9a-f]{64}$'",
            name="bot_decision_source_bar_feature_hash_valid",
        ),
        sa.CheckConstraint(
            "source_event_id ~ '^market-bar:[0-9a-f]{64}$'",
            name="bot_decision_source_bar_event_id_valid",
        ),
        sa.CheckConstraint(
            "source_event_digest ~ '^[0-9a-f]{64}$'",
            name="bot_decision_source_bar_event_digest_valid",
        ),
        sa.CheckConstraint("ordinal >= 0", name="bot_decision_source_bar_ordinal_nonnegative"),
        sa.CheckConstraint(
            "timeframe_bar_count > 0",
            name="bot_decision_source_bar_timeframe_count_positive",
        ),
        sa.CheckConstraint(
            "snapshot_timeframe_count > 0",
            name="bot_decision_source_bar_snapshot_count_positive",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_source_bar_decision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "market_bar_id"],
            ["market_bar.workspace_id", "market_bar.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_source_bar_market_bar",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_decision_source_bar")),
    )
    op.create_index(
        "uq_bot_decision_source_bar_ordinal",
        "bot_decision_source_bar",
        ["workspace_id", "bot_decision_id", "timeframe", "ordinal"],
        unique=True,
    )
    op.create_index(
        "uq_bot_decision_source_bar_market_bar",
        "bot_decision_source_bar",
        ["workspace_id", "bot_decision_id", "timeframe", "market_bar_id"],
        unique=True,
    )
    op.create_index(
        "ix_bot_decision_source_bar_decision",
        "bot_decision_source_bar",
        ["workspace_id", "bot_id", "bot_decision_id", "timeframe", "ordinal"],
    )
    op.execute("ALTER TABLE public.bot_decision_source_bar ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.bot_decision_source_bar FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY market_signal_source_bar_scope ON public.bot_decision_source_bar "
        f"FOR ALL TO {WORKER} USING ("
        "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid "
        "AND bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid) "
        "WITH CHECK ("
        "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid "
        "AND bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid)"
    )
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE public.bot_decision_source_bar "
        f"FROM PUBLIC, crypidian_app, crypidian_trading_runtime, crypidian_admin_web, {WORKER}"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.bot_decision_source_bar TO {WORKER}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) TO {WORKER}")
    _create_insert_guard()
    _create_complete_lineage_guard()
    op.execute(
        "CREATE TRIGGER bot_decision_source_bar_append_only "
        "BEFORE UPDATE OR DELETE ON public.bot_decision_source_bar "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_reject_bot_decision_source_bar_mutation()"
    )


def _create_insert_guard() -> None:
    """Xác nhận decision, workspace, thời điểm và identity bar ngay khi ghi."""
    op.execute(
        """CREATE FUNCTION public.wp06_validate_bot_decision_source_bar()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            decision_row public.bot_decision;
            evidence_row public.shared_signal_evidence;
            bar_row public.market_bar;
            canonical_event text;
            canonical_timestamp text;
        BEGIN
            IF NEW.workspace_id IS DISTINCT FROM NULLIF(
                    pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR NEW.bot_id IS DISTINCT FROM NULLIF(
                    pg_catalog.current_setting('app.bot_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_SCOPE_INVALID';
            END IF;
            SELECT * INTO decision_row
              FROM public.bot_decision AS decision
             WHERE decision.workspace_id = NEW.workspace_id
               AND decision.bot_id = NEW.bot_id
               AND decision.id = NEW.bot_decision_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_DECISION_SNAPSHOT_MISMATCH';
            END IF;
            IF decision_row.payload->>'decision_snapshot_id'
                    IS DISTINCT FROM NEW.decision_snapshot_id
               OR decision_row.evaluated_at IS NULL THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_DECISION_SNAPSHOT_MISMATCH';
            END IF;
            SELECT * INTO evidence_row
              FROM public.shared_signal_evidence AS evidence
             WHERE evidence.workspace_id = NEW.workspace_id
               AND evidence.id = decision_row.evidence_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_EVIDENCE_CUTOFF_MISSING';
            END IF;
            IF evidence_row.evidence_hash IS DISTINCT FROM decision_row.evidence_hash
               OR evidence_row.symbol IS NULL THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_EVIDENCE_CUTOFF_MISSING';
            END IF;
            SELECT * INTO bar_row
              FROM public.market_bar AS bar
             WHERE bar.workspace_id = NEW.workspace_id
               AND bar.id = NEW.market_bar_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_IDENTITY_NOT_CURRENT_AT_DECISION';
            END IF;
            IF bar_row.symbol IS NULL
               OR bar_row.symbol = ''
               OR bar_row.symbol IS DISTINCT FROM evidence_row.symbol
               OR bar_row.timeframe IS DISTINCT FROM NEW.timeframe
               OR bar_row.open_time > evidence_row.observed_at
               OR bar_row.available_at > evidence_row.observed_at
               OR bar_row.event_at > evidence_row.observed_at
               OR NOT bar_row.is_closed
               OR EXISTS (
                    SELECT 1
                      FROM public.market_bar AS newer
                     WHERE newer.workspace_id = bar_row.workspace_id
                       AND newer.logical_key = bar_row.logical_key
                       AND newer.revision > bar_row.revision
                       AND newer.available_at <= evidence_row.observed_at
               ) THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_IDENTITY_NOT_CURRENT_AT_DECISION';
            END IF;
            canonical_timestamp := pg_catalog.to_char(
                bar_row.open_time AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US'
            ) || 'Z';
            canonical_event := 'market-bar:' || pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(pg_catalog.jsonb_build_object(
                        'mode', 'market_bar_source_identity_v2',
                        'workspace_id', bar_row.workspace_id::text,
                        'record_id', bar_row.id::text,
                        'logical_key', bar_row.logical_key,
                        'revision', bar_row.revision,
                        'supersedes_record_id', bar_row.supersedes_id::text,
                        'checksum', bar_row.checksum,
                        'source', bar_row.source,
                        'raw_reference', bar_row.raw_reference_id::text,
                        'symbol', bar_row.symbol,
                        'timeframe', bar_row.timeframe,
                        'open_time', canonical_timestamp
                    )), 'UTF8')), 'hex');
            IF canonical_event IS DISTINCT FROM NEW.source_event_id THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_EVENT_ID_MISMATCH';
            END IF;
            RETURN NEW;
        END; $$"""
    )
    op.execute("REVOKE ALL ON FUNCTION public.wp06_validate_bot_decision_source_bar() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER bot_decision_source_bar_validate "
        "BEFORE INSERT ON public.bot_decision_source_bar FOR EACH ROW "
        "EXECUTE FUNCTION public.wp06_validate_bot_decision_source_bar()"
    )


def _create_complete_lineage_guard() -> None:
    """Tại commit kiểm đủ thứ tự, số nến và digest lineage theo timeframe."""
    op.execute(
        """CREATE FUNCTION public.wp06_assert_complete_bot_decision_source_bar_set()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            group_row record;
            expected_digest text;
            expected_timeframes integer;
            actual_timeframes integer;
            actual_snapshot_timeframes integer;
        BEGIN
            IF NEW.ordinal <> 0 AND EXISTS (
                SELECT 1 FROM public.bot_decision_source_bar AS first_row
                 WHERE first_row.workspace_id = NEW.workspace_id
                   AND first_row.bot_decision_id = NEW.bot_decision_id
                   AND first_row.timeframe = NEW.timeframe
                   AND first_row.ordinal = 0
            ) THEN
                RETURN NULL;
            END IF;
            FOR group_row IN
                SELECT timeframe, count(*)::integer AS actual_count,
                       min(ordinal) AS min_ordinal, max(ordinal) AS max_ordinal,
                       min(timeframe_bar_count) AS minimum_expected_count,
                       max(timeframe_bar_count) AS maximum_expected_count,
                       min(snapshot_timeframe_count) AS minimum_expected_timeframes,
                       max(snapshot_timeframe_count) AS maximum_expected_timeframes,
                       count(DISTINCT source_event_digest)::integer AS digest_count,
                       min(source_event_digest) AS source_event_digest
                  FROM public.bot_decision_source_bar
                 WHERE workspace_id = NEW.workspace_id
                   AND bot_decision_id = NEW.bot_decision_id
                 GROUP BY timeframe
            LOOP
                IF group_row.actual_count <> group_row.minimum_expected_count
                   OR group_row.minimum_expected_count <> group_row.maximum_expected_count
                   OR group_row.min_ordinal <> 0
                   OR group_row.max_ordinal <> group_row.actual_count - 1
                   OR group_row.minimum_expected_timeframes <> group_row.maximum_expected_timeframes
                   OR group_row.digest_count <> 1 THEN
                    RAISE EXCEPTION 'WP06_SOURCE_BAR_LINEAGE_INCOMPLETE';
                END IF;
                SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(pg_catalog.jsonb_build_object(
                        'timeframe', group_row.timeframe,
                        'source_event_ids', (
                            SELECT pg_catalog.jsonb_agg(item.source_event_id ORDER BY item.ordinal)
                              FROM public.bot_decision_source_bar AS item
                             WHERE item.workspace_id = NEW.workspace_id
                               AND item.bot_decision_id = NEW.bot_decision_id
                               AND item.timeframe = group_row.timeframe
                        ),
                        'source_bar_ids', (
                            SELECT pg_catalog.jsonb_agg(
                                'market-bar-record:' || item.market_bar_id::text
                                ORDER BY item.ordinal
                            )
                              FROM public.bot_decision_source_bar AS item
                             WHERE item.workspace_id = NEW.workspace_id
                               AND item.bot_decision_id = NEW.bot_decision_id
                               AND item.timeframe = group_row.timeframe
                        )
                    )), 'UTF8')), 'hex')
                  INTO expected_digest;
                IF expected_digest IS DISTINCT FROM group_row.source_event_digest THEN
                    RAISE EXCEPTION 'WP06_SOURCE_BAR_LINEAGE_DIGEST_MISMATCH';
                END IF;
            END LOOP;
            SELECT count(DISTINCT timeframe)::integer,
                   min(snapshot_timeframe_count), max(snapshot_timeframe_count)
              INTO actual_timeframes, expected_timeframes, actual_snapshot_timeframes
              FROM public.bot_decision_source_bar
             WHERE workspace_id = NEW.workspace_id
               AND bot_decision_id = NEW.bot_decision_id;
            IF actual_timeframes < 1
               OR expected_timeframes <> actual_snapshot_timeframes
               OR actual_timeframes <> expected_timeframes THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_TIMEFRAME_SET_INCOMPLETE';
            END IF;
            RETURN NULL;
        END; $$"""
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_assert_complete_bot_decision_source_bar_set() "
        "FROM PUBLIC"
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER bot_decision_source_bar_complete
        AFTER INSERT ON public.bot_decision_source_bar DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.wp06_assert_complete_bot_decision_source_bar_set()"""
    )
    op.execute(
        """CREATE FUNCTION public.wp06_reject_bot_decision_source_bar_mutation()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'WP06_SOURCE_BAR_APPEND_ONLY';
        END; $$"""
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_reject_bot_decision_source_bar_mutation() FROM PUBLIC"
    )


def downgrade() -> None:
    """Chỉ bỏ ledger khi chưa có decision source lineage nào được ghi."""
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.bot_decision_source_bar) THEN
                RAISE EXCEPTION 'WP06_SOURCE_BAR_DOWNGRADE_NONEMPTY';
            END IF;
        END $$"""
    )
    op.execute(f"REVOKE SELECT, INSERT ON TABLE public.bot_decision_source_bar FROM {WORKER}")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) FROM {WORKER}"
    )
    op.execute("DROP TRIGGER bot_decision_source_bar_complete ON public.bot_decision_source_bar")
    op.execute("DROP FUNCTION public.wp06_assert_complete_bot_decision_source_bar_set()")
    op.execute("DROP TRIGGER bot_decision_source_bar_append_only ON public.bot_decision_source_bar")
    op.execute("DROP TRIGGER bot_decision_source_bar_validate ON public.bot_decision_source_bar")
    op.execute("DROP FUNCTION public.wp06_reject_bot_decision_source_bar_mutation()")
    op.execute("DROP FUNCTION public.wp06_validate_bot_decision_source_bar()")
    op.execute("DROP POLICY market_signal_source_bar_scope ON public.bot_decision_source_bar")
    op.drop_index("ix_bot_decision_source_bar_decision", table_name="bot_decision_source_bar")
    op.drop_index("uq_bot_decision_source_bar_market_bar", table_name="bot_decision_source_bar")
    op.drop_index("uq_bot_decision_source_bar_ordinal", table_name="bot_decision_source_bar")
    op.drop_table("bot_decision_source_bar")
