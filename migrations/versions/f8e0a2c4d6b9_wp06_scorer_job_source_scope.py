"""WP06: kiểm quyền PAPER trên bar lineage của candidate trong transaction job."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8e0a2c4d6b9"
down_revision: str | None = "d5e7f9a1c3b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER = "crypidian_market_signal_worker"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"


def upgrade() -> None:
    """Tạo staging transaction-only, exact-scope gateway và dọn staging khi có decision."""
    op.execute("""
        CREATE TABLE public.bot_decision_job_source_bar (
            id uuid PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            workspace_id uuid NOT NULL,
            bot_id uuid NOT NULL,
            job_id uuid NOT NULL,
            evidence_id uuid NOT NULL,
            evidence_hash text NOT NULL,
            decision_snapshot_id text NOT NULL,
            decision_time timestamptz NOT NULL,
            timeframe text NOT NULL,
            feature_snapshot_id text NOT NULL,
            market_bar_id uuid NOT NULL,
            source_event_id text NOT NULL,
            source_event_digest text NOT NULL,
            ordinal integer NOT NULL,
            timeframe_bar_count integer NOT NULL,
            snapshot_timeframe_count integer NOT NULL,
            CONSTRAINT fk_bot_decision_job_source_bar_job
                FOREIGN KEY (workspace_id, job_id)
                REFERENCES public.bot_decision_job(workspace_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_bot_decision_job_source_bar_evidence
                FOREIGN KEY (workspace_id, evidence_id)
                REFERENCES public.shared_signal_evidence(workspace_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_bot_decision_job_source_bar_market_bar
                FOREIGN KEY (workspace_id, market_bar_id)
                REFERENCES public.market_bar(workspace_id, id) ON DELETE RESTRICT,
            CONSTRAINT ck_bot_decision_job_source_bar_evidence_hash
                CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_bot_decision_job_source_bar_snapshot_hash
                CHECK (decision_snapshot_id ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_bot_decision_job_source_bar_feature_hash
                CHECK (feature_snapshot_id ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_bot_decision_job_source_bar_event_id
                CHECK (source_event_id ~ '^market-bar:[0-9a-f]{64}$'),
            CONSTRAINT ck_bot_decision_job_source_bar_event_digest
                CHECK (source_event_digest ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_bot_decision_job_source_bar_ordinal
                CHECK (ordinal >= 0),
            CONSTRAINT ck_bot_decision_job_source_bar_counts
                CHECK (timeframe_bar_count > 0 AND snapshot_timeframe_count > 0)
        )
    """)
    _create_source_rights_receipt_tables()
    op.execute(
        "ALTER TABLE public.learning_score_event "
        "ADD COLUMN decision_source_rights_fingerprint varchar(64), "
        "ADD COLUMN decision_scope_fingerprint varchar(64), "
        "ADD CONSTRAINT ck_learning_score_event_decision_rights_hash "
        "CHECK (decision_source_rights_fingerprint IS NULL OR "
        "decision_source_rights_fingerprint ~ '^[0-9a-f]{64}$'), "
        "ADD CONSTRAINT ck_learning_score_event_decision_scope_hash "
        "CHECK (decision_scope_fingerprint IS NULL OR "
        "decision_scope_fingerprint ~ '^[0-9a-f]{64}$')"
    )
    op.create_index(
        "uq_bot_decision_job_source_bar_ordinal",
        "bot_decision_job_source_bar",
        ["workspace_id", "job_id", "timeframe", "ordinal"],
        unique=True,
    )
    op.create_index(
        "uq_bot_decision_job_source_bar_market_bar",
        "bot_decision_job_source_bar",
        ["workspace_id", "job_id", "market_bar_id"],
        unique=True,
    )
    op.create_index(
        "ix_bot_decision_job_source_bar_job",
        "bot_decision_job_source_bar",
        ["workspace_id", "bot_id", "job_id", "timeframe", "ordinal"],
    )
    op.execute("ALTER TABLE public.bot_decision_job_source_bar ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.bot_decision_job_source_bar FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY market_signal_job_source_bar_scope
        ON public.bot_decision_job_source_bar FOR ALL TO {WORKER}
        USING (
            workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
            AND bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid
        )
        WITH CHECK (
            workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
            AND bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid
        )
    """)
    op.execute(f"""
        CREATE POLICY scorer_job_source_bar_select
        ON public.bot_decision_job_source_bar FOR SELECT TO {RIGHTS_ROLE}
        USING ({WORKSPACE})
    """)
    op.execute(f"""
        CREATE POLICY scorer_job_source_bar_delete
        ON public.bot_decision_job_source_bar FOR DELETE TO {RIGHTS_ROLE}
        USING ({WORKSPACE})
    """)
    op.execute(
        "REVOKE ALL ON TABLE public.bot_decision_job_source_bar "
        f"FROM PUBLIC, crypidian_app, crypidian_trading_runtime, crypidian_admin_web, {WORKER}, "
        "crypidian_trainer_worker, crypidian_learning_source_rights_reader"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.bot_decision_job_source_bar TO {WORKER}")
    op.execute(f"GRANT SELECT, DELETE ON TABLE public.bot_decision_job_source_bar TO {RIGHTS_ROLE}")
    _grant_source_rights_receipt_access()
    _grant_rights_metadata()
    _create_validation_functions()
    _create_runtime_metadata_gate()
    _create_source_scope_gate()
    _create_job_bound_artifact_resolver()
    _create_decision_cleanup()
    _extend_score_event_guard()


def _create_source_rights_receipt_tables() -> None:
    """Tách proof rights transaction-only khỏi receipt bất biến theo quyết định."""
    op.create_table(
        "bot_decision_job_source_rights_auth",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("decision_snapshot_id", sa.String(64), nullable=False),
        sa.Column("decision_lineage_fingerprint", sa.String(64), nullable=False),
        sa.Column("decision_source_rights_fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$' AND decision_snapshot_id ~ '^[0-9a-f]{64}$' "
            "AND decision_lineage_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND decision_source_rights_fingerprint ~ '^[0-9a-f]{64}$'",
            name="bot_decision_job_source_rights_auth_hashes_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["bot_decision_job.workspace_id", "bot_decision_job.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_job_source_rights_auth_job",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["shared_signal_evidence.workspace_id", "shared_signal_evidence.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_job_source_rights_auth_evidence",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_decision_job_source_rights_auth")),
        sa.UniqueConstraint(
            "workspace_id", "job_id", name="uq_bot_decision_job_source_rights_auth_job"
        ),
    )
    op.create_table(
        "bot_decision_source_rights_receipt",
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
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("decision_snapshot_id", sa.String(64), nullable=False),
        sa.Column("decision_lineage_fingerprint", sa.String(64), nullable=False),
        sa.Column("decision_source_rights_fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$' AND decision_snapshot_id ~ '^[0-9a-f]{64}$' "
            "AND decision_lineage_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND decision_source_rights_fingerprint ~ '^[0-9a-f]{64}$'",
            name="bot_decision_source_rights_receipt_hashes_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "bot_decision_id"],
            ["bot_decision.workspace_id", "bot_decision.bot_id", "bot_decision.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_source_rights_receipt_decision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["bot_decision_job.workspace_id", "bot_decision_job.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_source_rights_receipt_job",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["shared_signal_evidence.workspace_id", "shared_signal_evidence.id"],
            ondelete="RESTRICT",
            name="fk_bot_decision_source_rights_receipt_evidence",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_decision_source_rights_receipt")),
        sa.UniqueConstraint(
            "workspace_id",
            "bot_decision_id",
            name="uq_bot_decision_source_rights_receipt_decision",
        ),
        sa.UniqueConstraint(
            "workspace_id", "job_id", name="uq_bot_decision_source_rights_receipt_job"
        ),
    )
    for table in ("bot_decision_job_source_rights_auth", "bot_decision_source_rights_receipt"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_rights_reader ON public.{table} FOR ALL TO {RIGHTS_ROLE} "
            f"USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
        )
        op.execute(
            f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, crypidian_app, "
            "crypidian_trading_runtime, crypidian_admin_web, crypidian_market_signal_worker, "
            "crypidian_trainer_worker, crypidian_learning_source_rights_reader"
        )
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON TABLE public.bot_decision_job_source_rights_auth "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON TABLE public.bot_decision_source_rights_receipt TO {RIGHTS_ROLE}"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_reject_bot_decision_source_rights_receipt_mutation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'WP06_SOURCE_RIGHTS_RECEIPT_APPEND_ONLY';
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_reject_bot_decision_source_rights_receipt_mutation() "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.wp06_reject_bot_decision_source_rights_receipt_mutation() FROM PUBLIC"
    )
    op.execute("""
        CREATE TRIGGER bot_decision_source_rights_receipt_append_only
        BEFORE UPDATE OR DELETE ON public.bot_decision_source_rights_receipt
        FOR EACH ROW EXECUTE FUNCTION
            public.wp06_reject_bot_decision_source_rights_receipt_mutation()
    """)
    op.execute("""
        CREATE FUNCTION public.wp06_assert_bot_decision_job_source_rights_auth_cleanup()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.bot_decision_job_source_rights_auth AS auth
                 WHERE auth.workspace_id = NEW.workspace_id AND auth.job_id = NEW.job_id
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_AUTH_REMAINS';
            END IF;
            RETURN NULL;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_assert_bot_decision_job_source_rights_auth_cleanup() "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.wp06_assert_bot_decision_job_source_rights_auth_cleanup() FROM PUBLIC"
    )
    op.execute("""
        CREATE CONSTRAINT TRIGGER bot_decision_job_source_rights_auth_cleanup
        AFTER INSERT ON public.bot_decision_job_source_rights_auth
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.wp06_assert_bot_decision_job_source_rights_auth_cleanup()
    """)


def _grant_rights_metadata() -> None:
    """Cấp cho NOLOGIN rights reader đúng các cột metadata cần để kiểm scope."""
    op.execute(f"""
        CREATE POLICY scorer_job_source_rights_market_bar_select
        ON public.market_bar FOR SELECT TO {RIGHTS_ROLE} USING ({WORKSPACE})
    """)
    op.execute(
        "GRANT SELECT (workspace_id,id,logical_key,revision,supersedes_id,checksum,"
        "raw_reference_id,source,symbol,timeframe,open_time,close_time,event_at,"
        "available_at,is_closed) "
        f"ON public.market_bar TO {RIGHTS_ROLE}"
    )
    op.execute(f"""
        CREATE POLICY scorer_job_source_rights_feature_snapshot_select
        ON public.feature_snapshot FOR SELECT TO {RIGHTS_ROLE} USING ({WORKSPACE})
    """)
    op.execute(
        "GRANT SELECT (workspace_id,snapshot_id,content_hash,symbol,timeframe,as_of,"
        "computed_at,schema_hash,formula_hash,code_hash,input_bar_ids,input_event_ids,"
        "features,warmup,quality_flags,data_freshness,data_coverage,regime,is_usable,"
        "canonical_json) ON public.feature_snapshot "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(f"""
        CREATE POLICY scorer_job_source_rights_job_select
        ON public.bot_decision_job FOR SELECT TO {RIGHTS_ROLE} USING ({WORKSPACE})
    """)
    op.execute(
        "GRANT SELECT (workspace_id,id,bot_id,account_id,evidence_id,evidence_hash,"
        "context_hash,bot_config_version_id,context_payload,evaluated_at,status) "
        f"ON public.bot_decision_job TO {RIGHTS_ROLE}"
    )
    for table, policy in (
        ("bot_config_version", "scorer_job_source_rights_bot_config_select"),
        ("workspace_settings_version", "scorer_job_source_rights_root_settings_select"),
    ):
        op.execute(f"""
            CREATE POLICY {policy}
            ON public.{table} FOR SELECT TO {RIGHTS_ROLE} USING ({WORKSPACE})
        """)
    op.execute(
        "GRANT SELECT (workspace_id,id,bot_id,account_id,overlay_json,overlay_hash,"
        "base_settings_version_id,base_settings_hash,status,effective_at) "
        f"ON public.bot_config_version TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT SELECT (workspace_id,id,settings_json,settings_hash,status,effective_at) "
        f"ON public.workspace_settings_version TO {RIGHTS_ROLE}"
    )
    op.execute(f"""
        CREATE POLICY scorer_job_source_rights_evidence_select
        ON public.shared_signal_evidence FOR SELECT TO {RIGHTS_ROLE} USING ({WORKSPACE})
    """)
    op.execute(
        "GRANT SELECT (workspace_id,id,symbol,observed_at,evidence_hash) "
        f"ON public.shared_signal_evidence TO {RIGHTS_ROLE}"
    )
    op.execute(f"""
        CREATE POLICY scorer_job_source_rights_license_evidence_select
        ON public.source_license_evidence FOR SELECT TO {RIGHTS_ROLE} USING ({WORKSPACE})
    """)
    op.execute(
        "GRANT SELECT (workspace_id,source_id,id,policy_id,evidence_ref,digest,license_scope,"
        "canonical_scope,scope_hash) ON public.source_license_evidence "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(
        "GRANT SELECT (provider,product,venue,contract,license_scope,license_evidence_id,"
        "license_digest,license_scope_hash) ON public.source_policy_revision "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute(f"""
        CREATE POLICY scorer_job_source_rights_decision_select
        ON public.bot_decision FOR SELECT TO {RIGHTS_ROLE} USING ({WORKSPACE})
    """)
    op.execute(
        f"GRANT SELECT (workspace_id,bot_id,job_id,payload) ON public.bot_decision TO {RIGHTS_ROLE}"
    )


def _grant_source_rights_receipt_access() -> None:
    """Cho guard đọc đúng lineage cuối để đối soát receipt scorer bất biến."""
    op.execute(f"""
        CREATE POLICY scorer_job_source_rights_final_bars_select
        ON public.bot_decision_source_bar FOR SELECT TO {RIGHTS_ROLE}
        USING ({WORKSPACE})
    """)
    op.execute(
        "GRANT SELECT (workspace_id,bot_id,bot_decision_id,decision_snapshot_id,timeframe,"
        "feature_snapshot_id,market_bar_id,source_event_id,source_event_digest,"
        "ordinal,timeframe_bar_count,"
        "snapshot_timeframe_count) ON public.bot_decision_source_bar "
        f"TO {RIGHTS_ROLE}"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_verify_bot_decision_source_scope(
            target_workspace uuid, target_decision uuid
        ) RETURNS TABLE(
            decision_snapshot_id text,
            timeframe_snapshot_ids jsonb,
            source_event_digests jsonb,
            decision_scope_fingerprint text,
            decision_lineage_fingerprint text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            snapshot_count integer;
            timeframe_count integer;
            expected_timeframes integer;
            snapshot_id text;
            timeframe_ids jsonb;
            event_digests jsonb;
            scope_document jsonb;
            lineage_document jsonb;
            lineage_fingerprint text;
        BEGIN
            IF target_workspace IS NULL OR target_decision IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SOURCE_RIGHTS_DECISION_SCOPE_DENIED';
            END IF;
            SELECT count(DISTINCT source.decision_snapshot_id)::integer,
                   count(DISTINCT source.timeframe)::integer,
                   min(source.snapshot_timeframe_count)
              INTO snapshot_count, timeframe_count, expected_timeframes
              FROM public.bot_decision_source_bar AS source
             WHERE source.workspace_id = target_workspace
               AND source.bot_decision_id = target_decision;
            IF snapshot_count <> 1 OR timeframe_count = 0
               OR timeframe_count <> expected_timeframes OR EXISTS (
                    SELECT 1 FROM public.bot_decision_source_bar AS source
                     WHERE source.workspace_id = target_workspace
                       AND source.bot_decision_id = target_decision
                     GROUP BY source.timeframe
                    HAVING count(*) <> min(source.timeframe_bar_count)
                       OR min(source.timeframe_bar_count) <> max(source.timeframe_bar_count)
                       OR min(source.ordinal) <> 0
                       OR max(source.ordinal) <> count(*) - 1
                       OR count(DISTINCT source.feature_snapshot_id) <> 1
                       OR count(DISTINCT source.source_event_digest) <> 1
                       OR count(DISTINCT source.decision_snapshot_id) <> 1
                       OR min(source.snapshot_timeframe_count)
                            <> max(source.snapshot_timeframe_count)
               ) THEN
                RAISE EXCEPTION 'WP06_SOURCE_RIGHTS_DECISION_LINEAGE_INVALID';
            END IF;
            SELECT min(source.decision_snapshot_id),
                   pg_catalog.jsonb_agg(
                       pg_catalog.jsonb_build_array(source.timeframe, source.feature_snapshot_id)
                       ORDER BY source.timeframe
                   ),
                   pg_catalog.jsonb_agg(
                       pg_catalog.jsonb_build_array(source.timeframe, source.source_event_digest)
                       ORDER BY source.timeframe
                   )
              INTO snapshot_id, timeframe_ids, event_digests
              FROM (
                    SELECT source.timeframe,
                           min(source.decision_snapshot_id) AS decision_snapshot_id,
                           min(source.feature_snapshot_id) AS feature_snapshot_id,
                           min(source.source_event_digest) AS source_event_digest
                      FROM public.bot_decision_source_bar AS source
                     WHERE source.workspace_id = target_workspace
                       AND source.bot_decision_id = target_decision
                     GROUP BY source.timeframe
              ) AS source;
            scope_document := pg_catalog.jsonb_build_object(
                'decision_snapshot_id', snapshot_id,
                'source_event_digests', event_digests,
                'timeframe_snapshot_ids', timeframe_ids
            );
            SELECT pg_catalog.jsonb_build_object(
                       'decision_snapshot_id', snapshot_id,
                       'timeframes', pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'timeframe', grouped.timeframe,
                               'feature_snapshot_id', grouped.feature_snapshot_id,
                               'bars', grouped.bars
                           ) ORDER BY grouped.timeframe
                       )
                   )
              INTO lineage_document
              FROM (
                    SELECT source.timeframe,
                           min(source.feature_snapshot_id) AS feature_snapshot_id,
                           pg_catalog.jsonb_agg(pg_catalog.jsonb_build_array(
                               source.ordinal, source.market_bar_id::text,
                               source.source_event_id, source.source_event_digest,
                               source.timeframe_bar_count, source.snapshot_timeframe_count
                           ) ORDER BY source.ordinal) AS bars
                      FROM public.bot_decision_source_bar AS source
                     WHERE source.workspace_id = target_workspace
                       AND source.bot_decision_id = target_decision
                     GROUP BY source.timeframe
              ) AS grouped;
            lineage_fingerprint := pg_catalog.encode(pg_catalog.sha256(
                pg_catalog.convert_to(
                    public.wp06_license_canonical_json(lineage_document), 'UTF8'
                )), 'hex');
            RETURN QUERY SELECT snapshot_id, timeframe_ids, event_digests,
                pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(scope_document), 'UTF8')), 'hex'),
                lineage_fingerprint;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_verify_bot_decision_source_scope(uuid,uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_verify_bot_decision_source_scope(uuid,uuid) FROM PUBLIC"
    )


def _create_validation_functions() -> None:
    """Xác thực job/evidence/bar, đủ mảng timeframe và cơ chế dọn staging có điều kiện."""
    op.execute("""
        CREATE FUNCTION public.wp06_validate_bot_decision_job_source_bar()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            job_row record;
            evidence_row record;
            bar_row record;
            canonical_event text;
            canonical_timestamp text;
        BEGIN
            IF NEW.workspace_id IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR NEW.bot_id IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_SCOPE_DENIED';
            END IF;
            SELECT job.workspace_id, job.id, job.bot_id, job.evidence_id,
                   job.evidence_hash, job.status
              INTO job_row FROM public.bot_decision_job AS job
             WHERE job.workspace_id = NEW.workspace_id
               AND job.id = NEW.job_id
               AND job.bot_id = NEW.bot_id
               AND job.evidence_id = NEW.evidence_id
               AND job.evidence_hash = NEW.evidence_hash
               AND job.status = 'PENDING';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT evidence.workspace_id, evidence.id, evidence.symbol,
                   evidence.observed_at, evidence.evidence_hash
              INTO evidence_row FROM public.shared_signal_evidence AS evidence
             WHERE evidence.workspace_id = NEW.workspace_id
               AND evidence.id = NEW.evidence_id
               AND evidence.evidence_hash = NEW.evidence_hash;
            IF NOT FOUND OR evidence_row.symbol IS NULL
               OR NEW.decision_time IS DISTINCT FROM evidence_row.observed_at THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            IF NEW.decision_time IS DISTINCT FROM evidence_row.observed_at THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT bar.workspace_id, bar.id, bar.logical_key, bar.revision,
                   bar.supersedes_id, bar.checksum, bar.raw_reference_id, bar.source,
                   bar.symbol, bar.timeframe, bar.open_time, bar.close_time,
                   bar.available_at, bar.is_closed
              INTO bar_row FROM public.market_bar AS bar
             WHERE bar.workspace_id = NEW.workspace_id AND bar.id = NEW.market_bar_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            IF bar_row.source NOT IN (
                    'BINANCE_USDM','BINANCE_USDM_REST_BACKFILL',
                    'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE')
               OR bar_row.symbol IS DISTINCT FROM evidence_row.symbol
               OR bar_row.timeframe IS DISTINCT FROM NEW.timeframe
               OR bar_row.open_time > evidence_row.observed_at
               OR bar_row.close_time <= bar_row.open_time
               OR bar_row.close_time > evidence_row.observed_at
               OR bar_row.available_at > evidence_row.observed_at
               OR NOT bar_row.is_closed
               OR EXISTS (
                    SELECT 1 FROM public.market_bar AS newer
                     WHERE newer.workspace_id = bar_row.workspace_id
                       AND newer.logical_key = bar_row.logical_key
                       AND newer.revision > bar_row.revision
                       AND newer.available_at <= evidence_row.observed_at
               ) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.bot_decision_job_source_bar AS previous
                 WHERE previous.workspace_id = NEW.workspace_id
                   AND previous.job_id = NEW.job_id
                   AND previous.decision_snapshot_id <> NEW.decision_snapshot_id
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            canonical_timestamp := pg_catalog.to_char(
                bar_row.open_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'
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
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            RETURN NEW;
        END; $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_validate_bot_decision_job_source_bar() OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_validate_bot_decision_job_source_bar() FROM PUBLIC"
    )
    op.execute("""
        CREATE TRIGGER bot_decision_job_source_bar_validate
        BEFORE INSERT ON public.bot_decision_job_source_bar FOR EACH ROW
        EXECUTE FUNCTION public.wp06_validate_bot_decision_job_source_bar()
    """)

    op.execute("""
        CREATE FUNCTION public.wp06_assert_complete_bot_decision_job_source_bar_set(
            target_workspace uuid, target_job uuid
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            job_row record;
            config_row record;
            settings_row record;
            evidence_row record;
            stage_count integer;
            snapshot_count integer;
            actual_timeframes integer;
            expected_timeframes text[];
            staged_timeframes text[];
            expected_release text;
            staged_input_event_ids jsonb;
            group_row record;
            feature_row record;
            expected_digest text;
            snapshot_id text;
            lineage_document jsonb;
            lineage_fingerprint text;
            context_schema_version text;
            max_bars_per_stream integer;
            expected_market_bar_ids uuid[];
            staged_market_bar_ids uuid[];
            expected_feature_input_bar_ids jsonb;
        BEGIN
            SELECT count(*)::integer,
                   count(DISTINCT decision_snapshot_id)::integer,
                   count(DISTINCT timeframe)::integer
              INTO stage_count, snapshot_count, actual_timeframes
              FROM public.bot_decision_job_source_bar
             WHERE workspace_id = target_workspace AND job_id = target_job;
            IF stage_count = 0 OR snapshot_count <> 1 OR actual_timeframes = 0 THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT job.workspace_id, job.id, job.bot_id, job.account_id,
                   job.evidence_id, job.evidence_hash, job.bot_config_version_id,
                   job.context_hash, job.context_payload, job.evaluated_at, job.status
              INTO job_row
              FROM public.bot_decision_job AS job
             WHERE job.workspace_id = target_workspace
               AND job.id = target_job AND job.status = 'PENDING';
            IF NOT FOUND
               OR job_row.context_payload->>'job_id' IS DISTINCT FROM job_row.id::text
               OR job_row.context_payload->>'evidence_id' IS DISTINCT FROM job_row.evidence_id::text
               OR job_row.context_payload->>'evidence_hash' IS DISTINCT FROM job_row.evidence_hash
               OR job_row.context_payload #>> '{context,workspace_id}'
                    IS DISTINCT FROM target_workspace::text
               OR job_row.context_payload #>> '{context,bot_id}'
                    IS DISTINCT FROM job_row.bot_id::text
               OR job_row.context_payload #>> '{context,account_id}'
                    IS DISTINCT FROM job_row.account_id::text
               OR job_row.context_payload #>> '{context,bot_config_version_id}'
                    IS DISTINCT FROM job_row.bot_config_version_id::text
               OR job_row.context_hash IS DISTINCT FROM pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to(
                        public.wp06_license_canonical_json(
                            job_row.context_payload->'context'), 'UTF8')), 'hex')
               OR NULLIF(job_row.context_payload->>'evaluated_at', '')::timestamptz
                    IS DISTINCT FROM job_row.evaluated_at THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            context_schema_version := job_row.context_payload #>> '{context,schema_version}';
            CASE context_schema_version
                WHEN 'crypidian-bot-decision-context-v1' THEN
                    IF job_row.context_payload #> '{context,max_bars_per_stream}' IS NOT NULL THEN
                        RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
                    END IF;
                    max_bars_per_stream := 256;
                WHEN 'crypidian-bot-decision-context-v2' THEN
                    IF pg_catalog.jsonb_typeof(
                            job_row.context_payload #> '{context,max_bars_per_stream}')
                            IS DISTINCT FROM 'number'
                       OR job_row.context_payload #>> '{context,max_bars_per_stream}'
                            !~ '^[0-9]{3,4}$' THEN
                        RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
                    END IF;
                    max_bars_per_stream := (
                        job_row.context_payload #>> '{context,max_bars_per_stream}')::integer;
                    IF max_bars_per_stream NOT BETWEEN 200 AND 2000 THEN
                        RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END CASE;
            SELECT config.workspace_id, config.id, config.bot_id, config.account_id,
                   config.overlay_json, config.overlay_hash, config.base_settings_version_id,
                   config.base_settings_hash, config.status, config.effective_at
              INTO config_row
              FROM public.bot_config_version AS config
             WHERE config.workspace_id = target_workspace
               AND config.id = job_row.bot_config_version_id
               AND config.bot_id = job_row.bot_id
               AND config.account_id = job_row.account_id;
            IF NOT FOUND
               OR config_row.status IS DISTINCT FROM 'PUBLISHED'
               OR config_row.effective_at > job_row.evaluated_at
               OR job_row.context_payload #>> '{context,bot_config_hash}'
                    IS DISTINCT FROM config_row.overlay_hash
               OR job_row.context_payload #>> '{context,root_settings_version_id}'
                    IS DISTINCT FROM config_row.base_settings_version_id::text
               OR job_row.context_payload #>> '{context,root_settings_hash}'
                    IS DISTINCT FROM config_row.base_settings_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT settings.workspace_id, settings.id, settings.settings_json,
                   settings.settings_hash, settings.status, settings.effective_at
              INTO settings_row
              FROM public.workspace_settings_version AS settings
             WHERE settings.workspace_id = target_workspace
               AND settings.id = config_row.base_settings_version_id;
            IF NOT FOUND OR settings_row.status IS DISTINCT FROM 'PUBLISHED'
               OR settings_row.effective_at > job_row.evaluated_at
               OR settings_row.settings_hash IS DISTINCT FROM config_row.base_settings_hash
               OR job_row.context_payload #>> '{context,root_settings_hash}'
                    IS DISTINCT FROM settings_row.settings_hash THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT evidence.workspace_id, evidence.id, evidence.symbol, evidence.observed_at
              INTO evidence_row
              FROM public.shared_signal_evidence AS evidence
             WHERE evidence.workspace_id = target_workspace
               AND evidence.id = job_row.evidence_id
               AND evidence.evidence_hash = job_row.evidence_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            IF (job_row.context_payload #> '{context,symbols}'
                    @> pg_catalog.jsonb_build_array(evidence_row.symbol)) IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            expected_release := COALESCE(
                config_row.overlay_json #>> '{strategy,release_id}',
                settings_row.settings_json #>> '{strategy,default_release}'
            );
            IF expected_release IS NULL
               OR job_row.context_payload #>> '{context,strategy_release_id}'
                    IS DISTINCT FROM expected_release THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            expected_timeframes := CASE expected_release
                WHEN 'h01-v1' THEN ARRAY['1h','4h']::text[]
                WHEN 'h03-v1' THEN ARRAY['1h']::text[]
                WHEN 'h06-v1' THEN ARRAY['15m']::text[]
                ELSE NULL
            END;
            IF expected_timeframes IS NULL THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT array_agg(DISTINCT staged.timeframe ORDER BY staged.timeframe)
              INTO staged_timeframes
              FROM public.bot_decision_job_source_bar AS staged
             WHERE staged.workspace_id = target_workspace AND staged.job_id = target_job;
            IF staged_timeframes IS DISTINCT FROM (
                    SELECT array_agg(value ORDER BY value)
                      FROM unnest(expected_timeframes) AS timeframes(value)
               ) OR actual_timeframes <> cardinality(expected_timeframes) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            FOR group_row IN
                SELECT timeframe, count(*)::integer AS actual_count,
                       min(ordinal) AS min_ordinal, max(ordinal) AS max_ordinal,
                       min(timeframe_bar_count) AS min_expected_count,
                       max(timeframe_bar_count) AS max_expected_count,
                       min(snapshot_timeframe_count) AS min_expected_timeframes,
                       max(snapshot_timeframe_count) AS max_expected_timeframes,
                       min(source_event_digest) AS event_digest,
                       count(DISTINCT source_event_digest)::integer AS digest_count,
                       count(DISTINCT feature_snapshot_id)::integer AS feature_count,
                       min(feature_snapshot_id) AS feature_snapshot_id
                  FROM public.bot_decision_job_source_bar
                 WHERE workspace_id = target_workspace AND job_id = target_job
                 GROUP BY timeframe
            LOOP
                IF group_row.actual_count <> group_row.min_expected_count
                   OR group_row.min_expected_count <> group_row.max_expected_count
                   OR group_row.min_ordinal <> 0
                   OR group_row.max_ordinal <> group_row.actual_count - 1
                   OR group_row.min_expected_timeframes <> group_row.max_expected_timeframes
                   OR group_row.min_expected_timeframes <> actual_timeframes
                   OR group_row.digest_count <> 1
                   OR group_row.feature_count <> 1 THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
                END IF;
                SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(pg_catalog.jsonb_build_object(
                        'timeframe', group_row.timeframe,
                        'source_event_ids', (
                            SELECT pg_catalog.jsonb_agg(item.source_event_id ORDER BY item.ordinal)
                              FROM public.bot_decision_job_source_bar AS item
                             WHERE item.workspace_id = target_workspace
                               AND item.job_id = target_job
                               AND item.timeframe = group_row.timeframe
                        ),
                        'source_bar_ids', (
                            SELECT pg_catalog.jsonb_agg(
                                'market-bar-record:' || item.market_bar_id::text
                                ORDER BY item.ordinal)
                              FROM public.bot_decision_job_source_bar AS item
                             WHERE item.workspace_id = target_workspace
                               AND item.job_id = target_job
                               AND item.timeframe = group_row.timeframe
                        )
                    )), 'UTF8')), 'hex') INTO expected_digest;
                IF expected_digest IS DISTINCT FROM group_row.event_digest THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
                END IF;
                SELECT pg_catalog.jsonb_agg(staged.source_event_id ORDER BY staged.ordinal)
                  INTO staged_input_event_ids
                  FROM public.bot_decision_job_source_bar AS staged
                 WHERE staged.workspace_id = target_workspace
                   AND staged.job_id = target_job
                   AND staged.timeframe = group_row.timeframe;
                WITH candidate_open_times AS (
                    SELECT DISTINCT bar.open_time
                      FROM public.market_bar AS bar
                     WHERE bar.workspace_id = target_workspace
                       AND bar.symbol = evidence_row.symbol
                       AND bar.timeframe = group_row.timeframe
                       AND bar.source IN ('BINANCE_USDM','BINANCE_USDM_REST_BACKFILL')
                       AND bar.is_closed
                       AND bar.close_time <= evidence_row.observed_at
                       AND bar.available_at <= evidence_row.observed_at
                     ORDER BY bar.open_time DESC
                     LIMIT max_bars_per_stream
                ), selected_revisions AS (
                    SELECT DISTINCT ON (bar.open_time)
                           bar.id, bar.open_time, bar.event_at
                      FROM public.market_bar AS bar
                      JOIN candidate_open_times AS selected_open
                        ON selected_open.open_time = bar.open_time
                     WHERE bar.workspace_id = target_workspace
                       AND bar.symbol = evidence_row.symbol
                       AND bar.timeframe = group_row.timeframe
                       AND bar.source IN ('BINANCE_USDM','BINANCE_USDM_REST_BACKFILL')
                       AND bar.is_closed
                       AND bar.close_time <= evidence_row.observed_at
                       AND bar.available_at <= evidence_row.observed_at
                     ORDER BY bar.open_time,
                       CASE bar.source
                           WHEN 'BINANCE_USDM' THEN 0
                           WHEN 'BINANCE_USDM_REST_BACKFILL' THEN 1
                           ELSE 2
                       END ASC,
                       bar.available_at DESC, bar.revision DESC, bar.id DESC
                ), builder_visible_bars AS (
                    SELECT selected.id, selected.open_time
                      FROM selected_revisions AS selected
                     WHERE selected.event_at <= evidence_row.observed_at
                )
                SELECT pg_catalog.array_agg(visible.id ORDER BY visible.open_time)
                  INTO expected_market_bar_ids
                  FROM builder_visible_bars AS visible;
                SELECT pg_catalog.array_agg(staged.market_bar_id ORDER BY staged.ordinal)
                  INTO staged_market_bar_ids
                  FROM public.bot_decision_job_source_bar AS staged
                 WHERE staged.workspace_id = target_workspace
                   AND staged.job_id = target_job
                   AND staged.timeframe = group_row.timeframe;
                IF expected_market_bar_ids IS NULL
                   OR expected_market_bar_ids IS DISTINCT FROM staged_market_bar_ids THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
                END IF;
                SELECT pg_catalog.jsonb_agg(
                           pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                               public.wp06_license_canonical_json(
                                   pg_catalog.jsonb_build_object(
                                       'open_time', pg_catalog.to_char(
                                           bar.open_time AT TIME ZONE 'UTC',
                                           'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                                       'symbol', bar.symbol,
                                       'timeframe', bar.timeframe
                                   )
                               ), 'UTF8'
                           )), 'hex') ORDER BY bar.open_time
                       )
                  INTO expected_feature_input_bar_ids
                  FROM public.market_bar AS bar
                 WHERE bar.workspace_id = target_workspace
                   AND bar.id = ANY(expected_market_bar_ids);
                IF expected_feature_input_bar_ids IS NULL THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
                END IF;
                SELECT feature.workspace_id, feature.snapshot_id, feature.content_hash,
                       feature.symbol, feature.timeframe, feature.as_of, feature.computed_at,
                       feature.schema_hash, feature.formula_hash, feature.code_hash,
                       feature.input_bar_ids, feature.input_event_ids, feature.features,
                       feature.warmup, feature.quality_flags, feature.data_freshness,
                       feature.data_coverage, feature.regime, feature.is_usable,
                       feature.canonical_json
                  INTO feature_row
                  FROM public.feature_snapshot AS feature
                 WHERE feature.workspace_id = target_workspace
                   AND feature.snapshot_id = group_row.feature_snapshot_id;
                IF NOT FOUND
                   OR feature_row.workspace_id IS DISTINCT FROM target_workspace
                   OR feature_row.snapshot_id IS DISTINCT FROM group_row.feature_snapshot_id
                   OR feature_row.content_hash IS DISTINCT FROM group_row.feature_snapshot_id
                   OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                        feature_row.canonical_json, 'UTF8')), 'hex')
                        IS DISTINCT FROM group_row.feature_snapshot_id
                   OR feature_row.symbol IS DISTINCT FROM evidence_row.symbol
                   OR feature_row.timeframe IS DISTINCT FROM group_row.timeframe
                   OR feature_row.as_of IS DISTINCT FROM evidence_row.observed_at
                   OR feature_row.computed_at IS DISTINCT FROM evidence_row.observed_at
                   OR feature_row.input_bar_ids IS DISTINCT FROM expected_feature_input_bar_ids
                   OR feature_row.input_event_ids IS DISTINCT FROM staged_input_event_ids
                   OR pg_catalog.jsonb_typeof(feature_row.canonical_json::jsonb)
                        IS DISTINCT FROM 'object'
                   OR feature_row.canonical_json::jsonb IS DISTINCT FROM
                        pg_catalog.jsonb_build_object(
                            'as_of', pg_catalog.to_char(
                                feature_row.as_of AT TIME ZONE 'UTC',
                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                            'code_hash', feature_row.code_hash,
                            'data_coverage', feature_row.data_coverage,
                            'data_freshness', feature_row.data_freshness,
                            'features', feature_row.features,
                            'formula_hash', feature_row.formula_hash,
                            'input_bar_ids', feature_row.input_bar_ids,
                            'input_event_ids', feature_row.input_event_ids,
                            'is_usable', feature_row.is_usable,
                            'quality_flags', feature_row.quality_flags,
                            'regime', feature_row.regime,
                            'schema_hash', feature_row.schema_hash,
                            'symbol', feature_row.symbol,
                            'timeframe', feature_row.timeframe,
                            'warmup', feature_row.warmup,
                            'workspace_id', target_workspace::text
                        ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
                END IF;
            END LOOP;
            SELECT min(staged.decision_snapshot_id) INTO snapshot_id
              FROM public.bot_decision_job_source_bar AS staged
             WHERE staged.workspace_id = target_workspace AND staged.job_id = target_job;
            SELECT pg_catalog.jsonb_build_object(
                       'decision_snapshot_id', snapshot_id,
                       'timeframes', pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'timeframe', grouped.timeframe,
                               'feature_snapshot_id', grouped.feature_snapshot_id,
                               'bars', grouped.bars
                           ) ORDER BY grouped.timeframe
                       )
                   )
              INTO lineage_document
              FROM (
                    SELECT staged.timeframe,
                           min(staged.feature_snapshot_id) AS feature_snapshot_id,
                           pg_catalog.jsonb_agg(pg_catalog.jsonb_build_array(
                               staged.ordinal, staged.market_bar_id::text,
                               staged.source_event_id, staged.source_event_digest,
                               staged.timeframe_bar_count, staged.snapshot_timeframe_count
                           ) ORDER BY staged.ordinal) AS bars
                      FROM public.bot_decision_job_source_bar AS staged
                     WHERE staged.workspace_id = target_workspace AND staged.job_id = target_job
                     GROUP BY staged.timeframe
              ) AS grouped;
            lineage_fingerprint := pg_catalog.encode(pg_catalog.sha256(
                pg_catalog.convert_to(
                    public.wp06_license_canonical_json(lineage_document), 'UTF8'
                )), 'hex');
            RETURN lineage_fingerprint;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_assert_complete_bot_decision_job_source_bar_set(uuid,uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.wp06_assert_complete_bot_decision_job_source_bar_set(uuid,uuid) "
        "FROM PUBLIC"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_assert_bot_decision_job_source_bar_cleanup()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.bot_decision_job_source_bar AS staged
                 WHERE staged.workspace_id = NEW.workspace_id AND staged.job_id = NEW.job_id
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_STAGING_REMAINS';
            END IF;
            RETURN NULL;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_assert_bot_decision_job_source_bar_cleanup() "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_assert_bot_decision_job_source_bar_cleanup() "
        "FROM PUBLIC"
    )
    op.execute("""
        CREATE CONSTRAINT TRIGGER bot_decision_job_source_bar_cleanup
        AFTER INSERT ON public.bot_decision_job_source_bar
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.wp06_assert_bot_decision_job_source_bar_cleanup()
    """)

    op.execute("""
        CREATE FUNCTION public.wp06_reject_bot_decision_job_source_bar_mutation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND pg_catalog.pg_trigger_depth() > 1
               AND OLD.workspace_id IS NOT DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               AND EXISTS (
                    SELECT 1 FROM public.bot_decision AS decision
                     WHERE decision.workspace_id = OLD.workspace_id
                       AND decision.bot_id = OLD.bot_id
                       AND decision.job_id = OLD.job_id
                       AND decision.payload->>'decision_snapshot_id'
                            = OLD.decision_snapshot_id
               ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_BAR_APPEND_ONLY';
        END; $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_reject_bot_decision_job_source_bar_mutation() "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_reject_bot_decision_job_source_bar_mutation() "
        "FROM PUBLIC"
    )
    op.execute("""
        CREATE TRIGGER bot_decision_job_source_bar_append_only
        BEFORE UPDATE OR DELETE ON public.bot_decision_job_source_bar FOR EACH ROW
        EXECUTE FUNCTION public.wp06_reject_bot_decision_job_source_bar_mutation()
    """)


def _create_source_scope_gate() -> None:
    """Đọc đúng MarketBar metadata, khóa từng source và trả về fingerprint duy nhất."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_check_scorer_job_source_scope(
            target_workspace uuid, target_job uuid, require_feature_snapshot boolean
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            job_row record;
            evidence_row record;
            policy_row record;
            evidence_rights record;
            group_row record;
            observation_row record;
            provider_id text;
            source_ids text[];
            current_scope jsonb;
            current_scope_hash text;
            policy_fingerprint text;
            fingerprint_document jsonb := '[]'::jsonb;
            checked_at timestamptz;
            stage_count integer;
            actual_timeframes integer;
            expected_timeframes integer;
            observed_snapshot_count integer;
            decision_rights_fingerprint text;
            decision_lineage_fingerprint text;
            staged_snapshot_id text;
        BEGIN
            IF target_workspace IS NULL OR target_job IS NULL
               OR require_feature_snapshot IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_SCOPE_DENIED';
            END IF;
            IF NULLIF(pg_catalog.current_setting('app.bot_id', true), '') IS NULL THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_SCOPE_DENIED';
            END IF;
            SELECT job.workspace_id, job.id, job.bot_id, job.evidence_id, job.evidence_hash,
                   job.status
              INTO job_row
              FROM public.bot_decision_job AS job
             WHERE job.workspace_id = target_workspace
               AND job.id = target_job
               AND job.bot_id = NULLIF(pg_catalog.current_setting('app.bot_id', true), '')::uuid
               AND job.status = 'PENDING';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT evidence.workspace_id, evidence.id, evidence.symbol, evidence.observed_at,
                   evidence.evidence_hash
              INTO evidence_row
              FROM public.shared_signal_evidence AS evidence
             WHERE evidence.workspace_id = target_workspace
               AND evidence.id = job_row.evidence_id
               AND evidence.evidence_hash = job_row.evidence_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT count(*)::integer,
                   count(DISTINCT decision_snapshot_id)::integer,
                   count(DISTINCT timeframe)::integer,
                   min(snapshot_timeframe_count)
              INTO stage_count, observed_snapshot_count, actual_timeframes, expected_timeframes
              FROM public.bot_decision_job_source_bar AS staged
             WHERE staged.workspace_id = target_workspace
               AND staged.bot_id = job_row.bot_id
               AND staged.job_id = target_job
               AND staged.evidence_id = job_row.evidence_id
               AND staged.evidence_hash = job_row.evidence_hash;
            IF stage_count = 0 OR observed_snapshot_count <> 1
               OR actual_timeframes = 0 OR actual_timeframes <> expected_timeframes THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            IF EXISTS (
                SELECT 1 FROM (
                    SELECT staged.timeframe, count(*)::integer AS actual_count,
                           min(staged.ordinal) AS min_ordinal,
                           max(staged.ordinal) AS max_ordinal,
                           min(staged.timeframe_bar_count) AS min_expected,
                           max(staged.timeframe_bar_count) AS max_expected
                      FROM public.bot_decision_job_source_bar AS staged
                     WHERE staged.workspace_id = target_workspace
                       AND staged.job_id = target_job
                     GROUP BY staged.timeframe
                ) AS groups
                WHERE groups.actual_count <> groups.min_expected
                   OR groups.min_expected <> groups.max_expected
                   OR groups.min_ordinal <> 0
                   OR groups.max_ordinal <> groups.actual_count - 1
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.bot_decision_job_source_bar AS staged
                JOIN public.market_bar AS bar
                  ON bar.workspace_id = staged.workspace_id AND bar.id = staged.market_bar_id
                WHERE staged.workspace_id = target_workspace AND staged.job_id = target_job
                  AND (bar.symbol IS DISTINCT FROM evidence_row.symbol
                       OR bar.timeframe IS DISTINCT FROM staged.timeframe
                       OR staged.decision_time IS DISTINCT FROM evidence_row.observed_at
                       OR bar.open_time > evidence_row.observed_at
                       OR bar.close_time <= bar.open_time
                       OR bar.close_time > evidence_row.observed_at
                       OR bar.available_at > evidence_row.observed_at
                       OR NOT bar.is_closed)
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            SELECT pg_catalog.array_agg(provider.source_id ORDER BY provider.source_id)
              INTO source_ids
              FROM (
                    SELECT DISTINCT CASE bar.source
                        WHEN 'BINANCE_USDM' THEN 'BINANCE_USDM_REST'
                        WHEN 'BINANCE_USDM_REST_BACKFILL' THEN 'BINANCE_USDM_REST'
                        WHEN 'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE'
                            THEN 'CRYPIDIAN_FIRST_PARTY_QA'
                        ELSE NULL END AS source_id
                      FROM public.bot_decision_job_source_bar AS staged
                      JOIN public.market_bar AS bar
                        ON bar.workspace_id = staged.workspace_id
                       AND bar.id = staged.market_bar_id
                     WHERE staged.workspace_id = target_workspace AND staged.job_id = target_job
              ) AS provider;
            IF source_ids IS NULL OR array_position(source_ids, NULL) IS NOT NULL THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_UNKNOWN_PROVIDER';
            END IF;
            FOREACH provider_id IN ARRAY source_ids LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || provider_id, 0));
            END LOOP;
            checked_at := pg_catalog.clock_timestamp();
            IF 'CRYPIDIAN_FIRST_PARTY_QA' = ANY(source_ids) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_QA_DENIED';
            END IF;
            IF require_feature_snapshot THEN
                decision_lineage_fingerprint :=
                    public.wp06_assert_complete_bot_decision_job_source_bar_set(
                        target_workspace, target_job);
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.bot_decision_job_source_bar AS staged
                  JOIN public.market_bar AS bar
                    ON bar.workspace_id = staged.workspace_id AND bar.id = staged.market_bar_id
                  JOIN public.market_bar AS newer
                    ON newer.workspace_id = bar.workspace_id
                   AND newer.logical_key = bar.logical_key
                   AND newer.revision > bar.revision
                   AND newer.available_at <= evidence_row.observed_at
                 WHERE staged.workspace_id = target_workspace AND staged.job_id = target_job
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;

            FOR group_row IN
                SELECT provider.source_id, bar.symbol, bar.timeframe,
                       min(bar.open_time) AS data_start,
                       max(bar.open_time) + INTERVAL '1 microsecond' AS data_end
                  FROM public.bot_decision_job_source_bar AS staged
                  JOIN public.market_bar AS bar
                    ON bar.workspace_id = staged.workspace_id
                   AND bar.id = staged.market_bar_id
                  CROSS JOIN LATERAL (SELECT CASE bar.source
                    WHEN 'BINANCE_USDM' THEN 'BINANCE_USDM_REST'
                    WHEN 'BINANCE_USDM_REST_BACKFILL' THEN 'BINANCE_USDM_REST'
                    WHEN 'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE'
                        THEN 'CRYPIDIAN_FIRST_PARTY_QA'
                    ELSE NULL END AS source_id) AS provider
                 WHERE staged.workspace_id = target_workspace AND staged.job_id = target_job
                 GROUP BY provider.source_id, bar.symbol, bar.timeframe
                 ORDER BY provider.source_id, bar.symbol, bar.timeframe
            LOOP
                IF group_row.source_id IS NULL OR group_row.data_end <= group_row.data_start THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_UNKNOWN_PROVIDER';
                END IF;
                SELECT policy.workspace_id, policy.source_id, policy.sequence, policy.policy_id,
                       policy.state, policy.allowed_purposes, policy.provider, policy.product,
                       policy.venue, policy.contract, policy.reviewed_at, policy.valid_until,
                       policy.evidence_ref, policy.created_at, policy.license_scope,
                       policy.license_evidence_id, policy.license_digest,
                       policy.license_scope_hash,
                       public.wp06_license_source_policy_fingerprint(
                           pg_catalog.jsonb_populate_record(
                               NULL::public.source_policy_revision,
                               pg_catalog.jsonb_build_object(
                                   'source_id', policy.source_id,
                                   'sequence', policy.sequence,
                                   'policy_id', policy.policy_id,
                                   'state', policy.state,
                                   'allowed_purposes', policy.allowed_purposes,
                                   'evidence_ref', policy.evidence_ref,
                                   'reviewed_at', policy.reviewed_at,
                                   'valid_until', policy.valid_until,
                                   'approved_by', policy.approved_by,
                                   'publisher', policy.publisher,
                                   'endpoint_url', policy.endpoint_url,
                                   'access_method', policy.access_method,
                                   'auth_scope', policy.auth_scope,
                                   'attribution', policy.attribution,
                                   'retention_ref', policy.retention_ref,
                                   'quota_window_seconds', policy.quota_window_seconds,
                                   'quota_max_requests', policy.quota_max_requests,
                                   'quota_max_bytes', policy.quota_max_bytes,
                                   'quota_max_cost', policy.quota_max_cost,
                                   'quota_cost_unit', policy.quota_cost_unit,
                                   'created_at', policy.created_at,
                                   'license_evidence_id', policy.license_evidence_id,
                                   'license_digest', policy.license_digest,
                                   'license_scope_hash', policy.license_scope_hash
                               )
                           )
                       ) AS canonical_policy_fingerprint
                  INTO policy_row
                  FROM public.source_policy_revision AS policy
                 WHERE policy.workspace_id = target_workspace
                   AND policy.source_id = group_row.source_id
                 ORDER BY policy.sequence DESC LIMIT 1;
                IF NOT FOUND OR policy_row.state IS DISTINCT FROM 'verified'
                   OR policy_row.provider IS DISTINCT FROM group_row.source_id
                   OR policy_row.product IS NULL OR policy_row.venue IS NULL
                   OR policy_row.contract IS NULL
                   OR NOT (policy_row.allowed_purposes @> '["paper_demo"]'::jsonb)
                   OR policy_row.reviewed_at IS NULL OR policy_row.reviewed_at > checked_at
                   OR (policy_row.valid_until IS NOT NULL
                       AND policy_row.valid_until <= checked_at)
                   OR policy_row.evidence_ref IS NULL
                   OR policy_row.evidence_ref !~ '^https://[^/?#]+([/?#].*)?$'
                   OR policy_row.license_scope IS NULL
                   OR policy_row.license_evidence_id IS NULL
                   OR policy_row.license_digest IS NULL
                   OR policy_row.license_digest !~ '^[0-9a-f]{64}$'
                   OR policy_row.license_scope_hash IS NULL
                   OR policy_row.license_scope_hash !~ '^[0-9a-f]{64}$'
                   OR NOT public.wp06_license_scope_valid(policy_row.license_scope)
                   OR policy_row.license_scope_hash IS DISTINCT FROM encode(
                        pg_catalog.sha256(pg_catalog.convert_to(
                            public.wp06_license_canonical_json(policy_row.license_scope), 'UTF8')),
                        'hex')
                   OR EXISTS (
                        SELECT 1 FROM public.outbox_event AS event
                         WHERE event.workspace_id = target_workspace
                           AND event.event_type = 'research.rights_changed.v1'
                           AND event.payload->>'source_id' = group_row.source_id
                           AND NOT EXISTS (
                                SELECT 1 FROM public.consumer_receipt AS receipt
                                 WHERE receipt.workspace_id = event.workspace_id
                                   AND receipt.outbox_event_id = event.id
                                   AND receipt.consumer_name =
                                        'wp06.dataset-rights-eligibility.v1'
                           )
                   ) THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED';
                END IF;
                policy_fingerprint := policy_row.canonical_policy_fingerprint;
                SELECT observation.policy_id, observation.known_at
                  INTO observation_row
                  FROM public.source_policy_observation AS observation
                 WHERE observation.workspace_id = target_workspace
                   AND observation.source_id = group_row.source_id
                   AND observation.sequence = policy_row.sequence
                 LIMIT 1;
                IF NOT FOUND OR observation_row.policy_id IS DISTINCT FROM policy_row.policy_id
                   OR observation_row.known_at < policy_row.created_at
                   OR observation_row.known_at < policy_row.reviewed_at
                   OR observation_row.known_at >= checked_at THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED';
                END IF;
                SELECT evidence.workspace_id, evidence.source_id, evidence.id,
                       evidence.policy_id, evidence.evidence_ref, evidence.digest,
                       evidence.license_scope, evidence.canonical_scope, evidence.scope_hash
                  INTO evidence_rights
                  FROM public.source_license_evidence AS evidence
                 WHERE evidence.workspace_id = target_workspace
                   AND evidence.source_id = group_row.source_id
                   AND evidence.id = policy_row.license_evidence_id
                   AND EXISTS (
                        SELECT 1 FROM public.source_policy_revision AS pending
                         WHERE pending.workspace_id = target_workspace
                           AND pending.source_id = group_row.source_id
                           AND pending.policy_id = evidence.policy_id
                           AND pending.state = 'rights_pending'
                           AND pending.evidence_ref = evidence.evidence_ref
                           AND pending.license_scope = evidence.license_scope
                   );
                IF NOT FOUND
                   OR evidence_rights.evidence_ref IS DISTINCT FROM policy_row.evidence_ref
                   OR evidence_rights.digest IS DISTINCT FROM policy_row.license_digest
                   OR evidence_rights.license_scope IS DISTINCT FROM policy_row.license_scope
                   OR evidence_rights.scope_hash IS DISTINCT FROM policy_row.license_scope_hash
                   OR evidence_rights.canonical_scope IS DISTINCT FROM
                        public.wp06_license_canonical_json(evidence_rights.license_scope)
                   OR evidence_rights.scope_hash IS DISTINCT FROM pg_catalog.encode(
                        pg_catalog.sha256(pg_catalog.convert_to(
                            evidence_rights.canonical_scope, 'UTF8')), 'hex')
                   OR public.wp06_license_canonical_json(evidence_rights.license_scope)
                        IS DISTINCT FROM
                            public.wp06_license_canonical_json(policy_row.license_scope)
                   OR public.wp06_license_scope_valid(evidence_rights.license_scope)
                        IS DISTINCT FROM true THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED';
                END IF;
                IF policy_row.license_scope->>'source_id' IS DISTINCT FROM group_row.source_id
                   OR policy_row.license_scope->>'provider' IS DISTINCT FROM policy_row.provider
                   OR policy_row.license_scope->>'product' IS DISTINCT FROM policy_row.product
                   OR policy_row.license_scope->>'venue' IS DISTINCT FROM policy_row.venue
                   OR policy_row.license_scope->>'contract' IS DISTINCT FROM policy_row.contract
                   OR NOT (policy_row.license_scope->'symbols'
                        @> pg_catalog.jsonb_build_array(group_row.symbol))
                   OR NOT (policy_row.license_scope->'intervals'
                        @> pg_catalog.jsonb_build_array(group_row.timeframe))
                   OR (policy_row.license_scope->>'data_start')::timestamptz > group_row.data_start
                   OR (policy_row.license_scope->>'data_end')::timestamptz < group_row.data_end
                   OR NOT (policy_row.license_scope->'allowed_purposes'
                        @> '["paper_demo"]'::jsonb)
                   OR NOT (policy_row.license_scope->'allowed_outputs'
                        @> '["paper_inference"]'::jsonb)
                   OR NOT (policy_row.license_scope->'derived_use'
                        @> '["paper_inference"]'::jsonb)
                   OR policy_row.license_scope->'max_retention_days' <> 'null'::jsonb THEN
                    RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED';
                END IF;
                current_scope := pg_catalog.jsonb_build_object(
                    'source_id', group_row.source_id,
                    'provider', policy_row.provider,
                    'product', policy_row.product,
                    'venue', policy_row.venue,
                    'contract', policy_row.contract,
                    'symbols', pg_catalog.jsonb_build_array(group_row.symbol),
                    'intervals', pg_catalog.jsonb_build_array(group_row.timeframe),
                    'data_start', pg_catalog.to_char(
                        group_row.data_start AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                    'data_end', pg_catalog.to_char(
                        group_row.data_end AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                    'purpose', 'paper_demo',
                    'outputs', pg_catalog.jsonb_build_array('paper_inference'),
                    'derived_use', pg_catalog.jsonb_build_array('paper_inference'),
                    'retention_days', NULL
                );
                current_scope_hash := pg_catalog.encode(pg_catalog.sha256(
                    pg_catalog.convert_to(
                        public.wp06_license_canonical_json(current_scope), 'UTF8')
                ), 'hex');
                fingerprint_document := fingerprint_document ||
                    pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
                        'source_id', group_row.source_id,
                        'policy_id', policy_row.policy_id,
                        'sequence', policy_row.sequence,
                        'policy_fingerprint', policy_fingerprint,
                        'license_evidence_id', policy_row.license_evidence_id::text,
                        'license_digest', policy_row.license_digest,
                        'license_scope_hash', policy_row.license_scope_hash,
                        'use_scope_hash', current_scope_hash,
                        'symbol', group_row.symbol,
                        'interval', group_row.timeframe,
                        'data_start', pg_catalog.to_char(
                            group_row.data_start AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                        'data_end', pg_catalog.to_char(
                            group_row.data_end AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                    ));
            END LOOP;
            IF fingerprint_document = '[]'::jsonb THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING';
            END IF;
            decision_rights_fingerprint := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(
                    public.wp06_license_canonical_json(fingerprint_document), 'UTF8')),
                'hex');
            IF require_feature_snapshot THEN
                SELECT min(staged.decision_snapshot_id) INTO staged_snapshot_id
                  FROM public.bot_decision_job_source_bar AS staged
                 WHERE staged.workspace_id = target_workspace AND staged.job_id = target_job;
                DELETE FROM public.bot_decision_job_source_rights_auth AS auth
                 WHERE auth.workspace_id = target_workspace AND auth.job_id = target_job;
                INSERT INTO public.bot_decision_job_source_rights_auth (
                    id, workspace_id, bot_id, job_id, evidence_id, evidence_hash,
                    decision_snapshot_id, decision_lineage_fingerprint,
                    decision_source_rights_fingerprint
                ) VALUES (
                    pg_catalog.gen_random_uuid(), target_workspace, job_row.bot_id, target_job,
                    job_row.evidence_id, job_row.evidence_hash, staged_snapshot_id,
                    decision_lineage_fingerprint,
                    decision_rights_fingerprint
                );
            END IF;
            RETURN decision_rights_fingerprint;
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_check_scorer_job_source_scope(uuid,uuid,boolean) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_check_scorer_job_source_scope(uuid,uuid,boolean) "
        "FROM PUBLIC"
    )
    for public_function, check_snapshot in (
        ("wp06_authorize_scorer_job_source_scope", "true"),
        ("wp06_preflight_scorer_job_source_rights", "false"),
    ):
        op.execute(f"""
            CREATE FUNCTION public.{public_function}(
                target_workspace uuid, target_job uuid
            ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp AS $$
            DECLARE metadata_row record;
            BEGIN
                SELECT metadata.workspace_id
                  INTO metadata_row
                  FROM public.wp06_resolve_scorer_runtime_metadata(target_workspace) AS metadata;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'WP06_SCORER_RUNTIME_METADATA_STALE';
                END IF;
                RETURN public.wp06_check_scorer_job_source_scope(
                    target_workspace, target_job, {check_snapshot}
                );
            END;
            $$
        """)
        op.execute(f"ALTER FUNCTION public.{public_function}(uuid,uuid) OWNER TO {RIGHTS_ROLE}")
        op.execute(f"REVOKE ALL ON FUNCTION public.{public_function}(uuid,uuid) FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{public_function}(uuid,uuid) TO {WORKER}")


def _create_job_bound_artifact_resolver() -> None:
    """Chỉ trả artifact sau khi job cụ thể đã qua current source-scope gate."""
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_resolve_scorer_runtime_target("
        "uuid,text,jsonb,uuid,integer,boolean) FROM " + WORKER
    )
    op.execute("""
        CREATE FUNCTION public.wp06_resolve_scorer_runtime_target_for_job(
            target_workspace uuid, target_job uuid
        ) RETURNS TABLE(
            workspace_id uuid,
            mode text,
            deployment_event_id uuid,
            deployment_revision integer,
            registry_id uuid,
            registry_revision integer,
            active_pointer_version integer,
            scorer_artifact_id uuid,
            scorer_artifact_hash text,
            base_artifact_id uuid,
            base_artifact_hash text,
            base_artifact_canonical text,
            scorer_artifact_canonical text,
            evaluation_report_id uuid,
            evaluation_report_hash text,
            evaluation_status text,
            candidate_completeness_verified boolean,
            scored_candidate_coverage_verified boolean,
            report_scope_hash text,
            proof_id uuid,
            proof_hash text,
            proof_scope_hash text,
            policy_id text,
            policy_hash text,
            current_rights_receipt_hash text,
            source_rights_fingerprint text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            metadata_row record;
            job_row record;
            current_source_rights_fingerprint text;
        BEGIN
            IF session_user <> 'crypidian_market_signal_worker'
               OR target_workspace IS NULL OR target_job IS NULL
               OR target_workspace IS DISTINCT FROM
                    NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed'
               OR NULLIF(pg_catalog.current_setting('app.bot_id', true), '') IS NULL THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_JOB_SCOPE_DENIED';
            END IF;
            SELECT metadata.workspace_id, metadata.mode,
                   metadata.deployment_event_id, metadata.deployment_revision,
                   metadata.active_pointer_version
              INTO metadata_row
              FROM public.wp06_resolve_scorer_runtime_metadata(target_workspace) AS metadata;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT job.workspace_id, job.id, job.bot_id, job.status
              INTO job_row
              FROM public.bot_decision_job AS job
             WHERE job.workspace_id = target_workspace
               AND job.id = target_job
               AND job.bot_id = NULLIF(
                    pg_catalog.current_setting('app.bot_id', true), '')::uuid
               AND job.status = 'PENDING';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_JOB_SCOPE_DENIED';
            END IF;
            current_source_rights_fingerprint :=
                public.wp06_authorize_scorer_job_source_scope(target_workspace, target_job);
            IF NOT EXISTS (
                SELECT 1 FROM public.bot_decision_job_source_rights_auth AS auth
                 WHERE auth.workspace_id = target_workspace
                   AND auth.bot_id = job_row.bot_id
                   AND auth.job_id = target_job
                   AND auth.decision_source_rights_fingerprint
                        = current_source_rights_fingerprint
            ) THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_JOB_AUTHORIZATION_MISSING';
            END IF;
            RETURN QUERY
                SELECT * FROM public.wp06_resolve_scorer_runtime_target(
                    target_workspace, NULL::text, NULL::jsonb, NULL::uuid,
                    NULL::integer, false
                );
        END; $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_resolve_scorer_runtime_target_for_job(uuid,uuid) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_resolve_scorer_runtime_target_for_job(uuid,uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_resolve_scorer_runtime_target_for_job(uuid,uuid) "
        f"TO {WORKER}"
    )


def _create_runtime_metadata_gate() -> None:
    """Kiểm tra mode và pointer mà không trả artifact hoặc payload scorer."""
    op.execute("""
        CREATE FUNCTION public.wp06_resolve_scorer_runtime_metadata(
            target_workspace uuid
        ) RETURNS TABLE(
            workspace_id uuid,
            mode text,
            deployment_event_id uuid,
            deployment_revision integer,
            active_pointer_version integer
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            pointer_row record;
            event_row record;
            model_pointer_row record;
            target_registry uuid;
            target_model_pointer_version integer;
        BEGIN
            IF target_workspace IS NULL OR target_workspace IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid
               OR pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_METADATA_SCOPE_DENIED';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-scoring-deployment:' || target_workspace::text ||
                chr(58) || 'current-pointer', 0
            ));
            SELECT pointer.workspace_id, pointer.event_id, pointer.revision,
                   pointer.active_pointer_version, pointer.mode
              INTO pointer_row
              FROM public.learning_scoring_deployment_pointer AS pointer
             WHERE pointer.workspace_id = target_workspace;
            IF NOT FOUND OR pointer_row.mode = 'DISABLED' THEN
                RETURN;
            END IF;
            IF pointer_row.mode NOT IN ('SHADOW','PAPER_GATE')
               OR pointer_row.event_id IS NULL THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_METADATA_STALE';
            END IF;
            SELECT event.workspace_id, event.id, event.revision, event.mode,
                   event.active_pointer_version, event.event_payload
              INTO event_row
              FROM public.learning_scoring_deployment_event AS event
             WHERE event.workspace_id = target_workspace
               AND event.id = pointer_row.event_id;
            IF NOT FOUND
               OR event_row.revision IS DISTINCT FROM pointer_row.revision
               OR event_row.mode IS DISTINCT FROM pointer_row.mode
               OR event_row.active_pointer_version
                    IS DISTINCT FROM pointer_row.active_pointer_version THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_METADATA_STALE';
            END IF;
            target_registry := NULLIF(
                event_row.event_payload #>> '{target,registry_id}', '')::uuid;
            target_model_pointer_version := NULLIF(
                event_row.event_payload #>> '{target,active_pointer_version}', '')::integer;
            IF target_registry IS NULL OR target_model_pointer_version IS NULL THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_METADATA_STALE';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-model-registry:' || target_workspace::text ||
                chr(58) || 'active-pointer', 0
            ));
            SELECT pointer.workspace_id, pointer.registry_id, pointer.version
              INTO model_pointer_row
              FROM public.learning_model_active_pointer AS pointer
             WHERE pointer.workspace_id = target_workspace;
            IF NOT FOUND OR model_pointer_row.registry_id IS DISTINCT FROM target_registry
               OR model_pointer_row.version IS DISTINCT FROM target_model_pointer_version THEN
                RAISE EXCEPTION 'WP06_SCORER_RUNTIME_METADATA_STALE';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'learning-model-registry:' || target_workspace::text ||
                chr(58) || 'registry-entry' || chr(58) || target_registry::text, 0
            ));
            RETURN QUERY SELECT target_workspace, pointer_row.mode, pointer_row.event_id,
                                pointer_row.revision, pointer_row.active_pointer_version;
        END; $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_resolve_scorer_runtime_metadata(uuid) OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_resolve_scorer_runtime_metadata(uuid) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_resolve_scorer_runtime_metadata(uuid) TO {WORKER}"
    )


def _create_decision_cleanup() -> None:
    """Dọn hàng staging ngay khi BotDecision đã nhận cùng job/snapshot trong transaction."""
    op.execute("""
        CREATE FUNCTION public.wp06_cleanup_bot_decision_job_source_bars()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            staged_snapshot text;
            auth_row record;
        BEGIN
            SELECT min(staged.decision_snapshot_id) INTO staged_snapshot
              FROM public.bot_decision_job_source_bar AS staged
             WHERE staged.workspace_id = NEW.workspace_id
               AND staged.bot_id = NEW.bot_id AND staged.job_id = NEW.job_id;
            IF staged_snapshot IS NULL THEN
                RETURN NEW;
            END IF;
            IF staged_snapshot IS DISTINCT FROM NEW.payload->>'decision_snapshot_id'
               OR EXISTS (
                    SELECT 1 FROM public.bot_decision_job_source_bar AS staged
                     WHERE staged.workspace_id = NEW.workspace_id
                       AND staged.bot_id = NEW.bot_id AND staged.job_id = NEW.job_id
                       AND staged.decision_snapshot_id
                            IS DISTINCT FROM NEW.payload->>'decision_snapshot_id'
               ) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_DECISION_MISMATCH';
            END IF;
            SELECT auth.workspace_id, auth.bot_id, auth.job_id, auth.evidence_id,
                   auth.evidence_hash, auth.decision_snapshot_id,
                   auth.decision_lineage_fingerprint,
                   auth.decision_source_rights_fingerprint
              INTO auth_row
              FROM public.bot_decision_job_source_rights_auth AS auth
             WHERE auth.workspace_id = NEW.workspace_id AND auth.job_id = NEW.job_id;
            IF NOT FOUND OR auth_row.bot_id IS DISTINCT FROM NEW.bot_id
               OR auth_row.evidence_id IS DISTINCT FROM NEW.evidence_id
               OR auth_row.evidence_hash IS DISTINCT FROM NEW.evidence_hash
               OR auth_row.decision_snapshot_id IS DISTINCT FROM staged_snapshot
               OR auth_row.decision_lineage_fingerprint IS DISTINCT FROM
                    public.wp06_assert_complete_bot_decision_job_source_bar_set(
                        NEW.workspace_id, NEW.job_id)
               OR auth_row.decision_source_rights_fingerprint !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_AUTHORIZATION_MISSING';
            END IF;
            INSERT INTO public.bot_decision_source_rights_receipt (
                id, workspace_id, bot_id, bot_decision_id, job_id, evidence_id,
                evidence_hash, decision_snapshot_id, decision_lineage_fingerprint,
                decision_source_rights_fingerprint
            ) VALUES (
                pg_catalog.gen_random_uuid(), NEW.workspace_id, NEW.bot_id, NEW.id,
                NEW.job_id, NEW.evidence_id, NEW.evidence_hash, staged_snapshot,
                auth_row.decision_lineage_fingerprint,
                auth_row.decision_source_rights_fingerprint
            );
            DELETE FROM public.bot_decision_job_source_bar AS staged
             WHERE staged.workspace_id = NEW.workspace_id
               AND staged.bot_id = NEW.bot_id AND staged.job_id = NEW.job_id;
            DELETE FROM public.bot_decision_job_source_rights_auth AS auth
             WHERE auth.workspace_id = NEW.workspace_id AND auth.job_id = NEW.job_id;
            RETURN NEW;
        END; $$
    """)
    op.execute(
        f"ALTER FUNCTION public.wp06_cleanup_bot_decision_job_source_bars() OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_cleanup_bot_decision_job_source_bars() FROM PUBLIC"
    )
    op.execute("""
        CREATE TRIGGER bot_decision_cleanup_job_source_bars
        AFTER INSERT ON public.bot_decision FOR EACH ROW
        EXECUTE FUNCTION public.wp06_cleanup_bot_decision_job_source_bars()
    """)


def _extend_score_event_guard() -> None:
    """Giữ hợp đồng D8 và thêm receipt quyền quyết định cùng kiểm lineage cuối."""
    bind = op.get_bind()

    def function_definition(signature: str) -> str:
        definition = bind.execute(
            sa.text("SELECT pg_catalog.pg_get_functiondef(pg_catalog.to_regprocedure(:signature))"),
            {"signature": signature},
        ).scalar_one()
        if not isinstance(definition, str):
            raise RuntimeError(f"Không đọc được định nghĩa SQL cho {signature}.")
        return definition

    def replace_once(value: str, old: str, new: str, name: str, *, count: int = 1) -> str:
        actual = value.count(old)
        if actual != count:
            raise RuntimeError(f"D8 contract {name} drifted: mong {count} anchor, nhận {actual}.")
        return value.replace(old, new)

    payload_guard = function_definition("public.wp06_score_event_payload_has_exact_schema(jsonb)")
    payload_guard = replace_once(
        payload_guard,
        "'source_rights_fingerprint','timeframe_snapshot_ids'",
        "'source_rights_fingerprint','decision_source_rights_fingerprint',"
        "'decision_scope_fingerprint','timeframe_snapshot_ids'",
        "payload exact key set",
    )
    payload_guard = replace_once(
        payload_guard,
        "'scorer_artifact_id','source_rights_fingerprint','vector_hash'",
        "'scorer_artifact_id','source_rights_fingerprint',"
        "'decision_source_rights_fingerprint','decision_scope_fingerprint','vector_hash'",
        "payload string types",
    )
    op.execute(payload_guard)

    score_guard = function_definition("public.wp06_learning_score_event_guard()")
    score_guard = replace_once(
        score_guard,
        "        DECLARE rights_row record;",
        "        DECLARE rights_row record;\n"
        "        DECLARE source_receipt_row record;\n"
        "        DECLARE source_scope_row record;",
        "score guard receipt declarations",
    )
    score_guard = replace_once(
        score_guard,
        (
            "               OR NEW.event_payload->>'source_rights_fingerprint' "
            "IS DISTINCT FROM NEW.source_rights_fingerprint"
        ),
        (
            "               OR NEW.event_payload->>'source_rights_fingerprint' "
            "IS DISTINCT FROM NEW.source_rights_fingerprint\n"
            "               OR NEW.event_payload->>'decision_source_rights_fingerprint' "
            "IS DISTINCT FROM NEW.decision_source_rights_fingerprint\n"
            "               OR NEW.event_payload->>'decision_scope_fingerprint' "
            "IS DISTINCT FROM NEW.decision_scope_fingerprint"
        ),
        "score event fingerprint columns",
    )
    ending = "            RETURN NEW;"
    verification = """            IF NEW.decision_source_rights_fingerprint IS NULL
               OR NEW.decision_scope_fingerprint IS NULL THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DECISION_SOURCE_RIGHTS_MISSING';
            END IF;
            SELECT receipt.workspace_id, receipt.bot_id, receipt.bot_decision_id,
                   receipt.job_id, receipt.evidence_id, receipt.evidence_hash,
                   receipt.decision_snapshot_id, receipt.decision_lineage_fingerprint,
                   receipt.decision_source_rights_fingerprint
              INTO source_receipt_row
              FROM public.bot_decision_source_rights_receipt AS receipt
             WHERE receipt.workspace_id = NEW.workspace_id
               AND receipt.bot_id = NEW.bot_id
               AND receipt.bot_decision_id = NEW.bot_decision_id;
            IF NOT FOUND OR source_receipt_row.decision_snapshot_id
                    IS DISTINCT FROM NEW.decision_snapshot_id
               OR source_receipt_row.decision_source_rights_fingerprint
                    IS DISTINCT FROM NEW.decision_source_rights_fingerprint THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DECISION_SOURCE_RIGHTS_MISMATCH';
            END IF;
            SELECT scope.decision_snapshot_id, scope.timeframe_snapshot_ids,
                   scope.source_event_digests, scope.decision_scope_fingerprint,
                   scope.decision_lineage_fingerprint
              INTO source_scope_row
              FROM public.wp06_verify_bot_decision_source_scope(
                  NEW.workspace_id, NEW.bot_decision_id
              ) AS scope;
            IF NOT FOUND
               OR source_scope_row.decision_snapshot_id IS DISTINCT FROM NEW.decision_snapshot_id
               OR source_scope_row.timeframe_snapshot_ids
                    IS DISTINCT FROM NEW.timeframe_snapshot_ids
               OR source_scope_row.source_event_digests IS DISTINCT FROM NEW.source_event_digests
               OR source_scope_row.decision_scope_fingerprint
                    IS DISTINCT FROM NEW.decision_scope_fingerprint
               OR source_receipt_row.decision_lineage_fingerprint
                    IS DISTINCT FROM source_scope_row.decision_lineage_fingerprint THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DECISION_SOURCE_SCOPE_MISMATCH';
            END IF;
            RETURN NEW;"""
    score_guard = replace_once(score_guard, ending, verification, "score guard final return")
    op.execute(score_guard)


def _restore_score_event_guard() -> None:
    """Khôi phục chính xác helper và trigger D8 trước khi bỏ cột của migration này."""
    bind = op.get_bind()

    def function_definition(signature: str) -> str:
        definition = bind.execute(
            sa.text("SELECT pg_catalog.pg_get_functiondef(pg_catalog.to_regprocedure(:signature))"),
            {"signature": signature},
        ).scalar_one()
        if not isinstance(definition, str):
            raise RuntimeError(f"Không đọc được định nghĩa SQL cho {signature}.")
        return definition

    def replace_once(value: str, old: str, new: str, name: str, *, count: int = 1) -> str:
        actual = value.count(old)
        if actual != count:
            raise RuntimeError(
                f"WP06 score-event downgrade {name} drifted: mong {count}, nhận {actual}."
            )
        return value.replace(old, new)

    payload_guard = function_definition("public.wp06_score_event_payload_has_exact_schema(jsonb)")
    payload_guard = replace_once(
        payload_guard,
        "'source_rights_fingerprint','decision_source_rights_fingerprint',"
        "'decision_scope_fingerprint','timeframe_snapshot_ids'",
        "'source_rights_fingerprint','timeframe_snapshot_ids'",
        "payload exact key set",
    )
    payload_guard = replace_once(
        payload_guard,
        "'scorer_artifact_id','source_rights_fingerprint',"
        "'decision_source_rights_fingerprint','decision_scope_fingerprint','vector_hash'",
        "'scorer_artifact_id','source_rights_fingerprint','vector_hash'",
        "payload string types",
    )
    op.execute(payload_guard)

    score_guard = function_definition("public.wp06_learning_score_event_guard()")
    score_guard = replace_once(
        score_guard,
        "        DECLARE source_receipt_row record;\n        DECLARE source_scope_row record;\n",
        "",
        "score guard receipt declarations",
    )
    score_guard = replace_once(
        score_guard,
        (
            "               OR NEW.event_payload->>'decision_source_rights_fingerprint' "
            "IS DISTINCT FROM NEW.decision_source_rights_fingerprint\n"
            "               OR NEW.event_payload->>'decision_scope_fingerprint' "
            "IS DISTINCT FROM NEW.decision_scope_fingerprint"
        ),
        "",
        "score event fingerprint columns",
    )
    verification = """            IF NEW.decision_source_rights_fingerprint IS NULL
               OR NEW.decision_scope_fingerprint IS NULL THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DECISION_SOURCE_RIGHTS_MISSING';
            END IF;
            SELECT receipt.workspace_id, receipt.bot_id, receipt.bot_decision_id,
                   receipt.job_id, receipt.evidence_id, receipt.evidence_hash,
                   receipt.decision_snapshot_id, receipt.decision_lineage_fingerprint,
                   receipt.decision_source_rights_fingerprint
              INTO source_receipt_row
              FROM public.bot_decision_source_rights_receipt AS receipt
             WHERE receipt.workspace_id = NEW.workspace_id
               AND receipt.bot_id = NEW.bot_id
               AND receipt.bot_decision_id = NEW.bot_decision_id;
            IF NOT FOUND OR source_receipt_row.decision_snapshot_id
                    IS DISTINCT FROM NEW.decision_snapshot_id
               OR source_receipt_row.decision_source_rights_fingerprint
                    IS DISTINCT FROM NEW.decision_source_rights_fingerprint THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DECISION_SOURCE_RIGHTS_MISMATCH';
            END IF;
            SELECT scope.decision_snapshot_id, scope.timeframe_snapshot_ids,
                   scope.source_event_digests, scope.decision_scope_fingerprint,
                   scope.decision_lineage_fingerprint
              INTO source_scope_row
              FROM public.wp06_verify_bot_decision_source_scope(
                  NEW.workspace_id, NEW.bot_decision_id
              ) AS scope;
            IF NOT FOUND
               OR source_scope_row.decision_snapshot_id IS DISTINCT FROM NEW.decision_snapshot_id
               OR source_scope_row.timeframe_snapshot_ids
                    IS DISTINCT FROM NEW.timeframe_snapshot_ids
               OR source_scope_row.source_event_digests IS DISTINCT FROM NEW.source_event_digests
               OR source_scope_row.decision_scope_fingerprint
                    IS DISTINCT FROM NEW.decision_scope_fingerprint
               OR source_receipt_row.decision_lineage_fingerprint
                    IS DISTINCT FROM source_scope_row.decision_lineage_fingerprint THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_DECISION_SOURCE_SCOPE_MISMATCH';
            END IF;
"""
    score_guard = replace_once(
        score_guard,
        verification,
        "",
        "score guard final receipt check",
    )
    op.execute(score_guard)


def downgrade() -> None:
    """Chỉ hạ khi không còn staging và scorer active; không xóa final decision lineage."""
    op.execute(
        "LOCK TABLE public.bot_decision_job_source_bar, "
        "public.bot_decision_job_source_rights_auth, "
        "public.bot_decision_source_rights_receipt, public.learning_score_event, "
        "public.learning_scoring_deployment_event IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.bot_decision_job_source_bar)
               OR EXISTS (SELECT 1 FROM public.bot_decision_job_source_rights_auth)
               OR EXISTS (SELECT 1 FROM public.bot_decision_source_rights_receipt)
               OR EXISTS (SELECT 1 FROM public.learning_score_event
                           WHERE decision_source_rights_fingerprint IS NOT NULL
                              OR decision_scope_fingerprint IS NOT NULL) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_DOWNGRADE_NONEMPTY';
            END IF;
            IF EXISTS (SELECT 1 FROM public.learning_scoring_deployment_event
                        WHERE mode IN ('SHADOW','PAPER_GATE')) THEN
                RAISE EXCEPTION 'WP06_SCORER_JOB_SOURCE_DOWNGRADE_ACTIVE';
            END IF;
        END $$
    """)
    _restore_score_event_guard()
    op.execute("DROP TRIGGER bot_decision_cleanup_job_source_bars ON public.bot_decision")
    op.execute("DROP FUNCTION public.wp06_cleanup_bot_decision_job_source_bars()")
    op.execute("DROP FUNCTION public.wp06_verify_bot_decision_source_scope(uuid,uuid)")
    op.execute(
        "DROP TRIGGER bot_decision_job_source_rights_auth_cleanup "
        "ON public.bot_decision_job_source_rights_auth"
    )
    op.execute("DROP FUNCTION public.wp06_assert_bot_decision_job_source_rights_auth_cleanup()")
    op.execute(
        "DROP TRIGGER bot_decision_source_rights_receipt_append_only "
        "ON public.bot_decision_source_rights_receipt"
    )
    op.execute("DROP FUNCTION public.wp06_reject_bot_decision_source_rights_receipt_mutation()")
    op.execute("DROP FUNCTION public.wp06_resolve_scorer_runtime_target_for_job(uuid,uuid)")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_resolve_scorer_runtime_target("
        "uuid,text,jsonb,uuid,integer,boolean) TO " + WORKER
    )
    op.execute("DROP FUNCTION public.wp06_resolve_scorer_runtime_metadata(uuid)")
    op.execute("DROP FUNCTION public.wp06_authorize_scorer_job_source_scope(uuid,uuid)")
    op.execute("DROP FUNCTION public.wp06_preflight_scorer_job_source_rights(uuid,uuid)")
    op.execute("DROP FUNCTION public.wp06_check_scorer_job_source_scope(uuid,uuid,boolean)")
    op.execute(
        "DROP TRIGGER bot_decision_job_source_bar_append_only ON public.bot_decision_job_source_bar"
    )
    op.execute("DROP FUNCTION public.wp06_reject_bot_decision_job_source_bar_mutation()")
    op.execute(
        "DROP TRIGGER bot_decision_job_source_bar_cleanup ON public.bot_decision_job_source_bar"
    )
    op.execute("DROP FUNCTION public.wp06_assert_bot_decision_job_source_bar_cleanup()")
    op.execute(
        "DROP FUNCTION public.wp06_assert_complete_bot_decision_job_source_bar_set(uuid,uuid)"
    )
    op.execute(
        "DROP TRIGGER bot_decision_job_source_bar_validate ON public.bot_decision_job_source_bar"
    )
    op.execute("DROP FUNCTION public.wp06_validate_bot_decision_job_source_bar()")
    for table, policy in (
        ("market_bar", "scorer_job_source_rights_market_bar_select"),
        ("feature_snapshot", "scorer_job_source_rights_feature_snapshot_select"),
        ("bot_decision_job", "scorer_job_source_rights_job_select"),
        ("bot_config_version", "scorer_job_source_rights_bot_config_select"),
        ("workspace_settings_version", "scorer_job_source_rights_root_settings_select"),
        ("shared_signal_evidence", "scorer_job_source_rights_evidence_select"),
        ("source_license_evidence", "scorer_job_source_rights_license_evidence_select"),
        ("bot_decision", "scorer_job_source_rights_decision_select"),
    ):
        op.execute(f"DROP POLICY {policy} ON public.{table}")
    op.execute(
        "DROP POLICY scorer_job_source_rights_final_bars_select ON public.bot_decision_source_bar"
    )
    for table in (
        "bot_decision_job_source_rights_auth",
        "bot_decision_source_rights_receipt",
    ):
        op.execute(f"DROP POLICY {table}_rights_reader ON public.{table}")
    op.execute("DROP POLICY scorer_job_source_bar_delete ON public.bot_decision_job_source_bar")
    op.execute("DROP POLICY scorer_job_source_bar_select ON public.bot_decision_job_source_bar")
    op.execute(
        "DROP POLICY market_signal_job_source_bar_scope ON public.bot_decision_job_source_bar"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,logical_key,revision,supersedes_id,checksum,"
        "raw_reference_id,source,symbol,timeframe,open_time,close_time,event_at,"
        "available_at,is_closed) "
        f"ON public.market_bar FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,snapshot_id,content_hash,symbol,timeframe,as_of,"
        "computed_at,schema_hash,formula_hash,code_hash,input_bar_ids,input_event_ids,"
        "features,warmup,quality_flags,data_freshness,data_coverage,regime,is_usable,"
        "canonical_json) ON public.feature_snapshot "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,bot_id,account_id,evidence_id,evidence_hash,"
        "context_hash,bot_config_version_id,context_payload,evaluated_at,status) "
        f"ON public.bot_decision_job FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,bot_id,account_id,overlay_json,overlay_hash,"
        "base_settings_version_id,base_settings_hash,status,effective_at) "
        f"ON public.bot_config_version FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,settings_json,settings_hash,status,effective_at) "
        f"ON public.workspace_settings_version FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,id,symbol,observed_at,evidence_hash) "
        f"ON public.shared_signal_evidence FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,source_id,id,policy_id,evidence_ref,digest,license_scope,"
        f"canonical_scope,scope_hash) ON public.source_license_evidence FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (provider,product,venue,contract,license_scope,"
        "license_evidence_id,license_digest,license_scope_hash) "
        "ON public.source_policy_revision "
        f"FROM {RIGHTS_ROLE}"
    )
    op.execute(f"REVOKE SELECT (job_id,payload) ON public.bot_decision FROM {RIGHTS_ROLE}")
    op.execute(
        "GRANT SELECT (workspace_id,bot_id,id,status,candidate_id,decision_hash) "
        f"ON public.bot_decision TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE SELECT (workspace_id,bot_id,bot_decision_id,decision_snapshot_id,timeframe,"
        "feature_snapshot_id,market_bar_id,source_event_id,source_event_digest,"
        "ordinal,timeframe_bar_count,"
        f"snapshot_timeframe_count) ON public.bot_decision_source_bar FROM {RIGHTS_ROLE}"
    )
    op.execute(
        "ALTER TABLE public.learning_score_event "
        "DROP CONSTRAINT ck_learning_score_event_decision_rights_hash, "
        "DROP CONSTRAINT ck_learning_score_event_decision_scope_hash, "
        "DROP COLUMN decision_source_rights_fingerprint, "
        "DROP COLUMN decision_scope_fingerprint"
    )
    op.drop_table("bot_decision_source_rights_receipt")
    op.drop_table("bot_decision_job_source_rights_auth")
    op.drop_index("ix_bot_decision_job_source_bar_job", table_name="bot_decision_job_source_bar")
    op.drop_index(
        "uq_bot_decision_job_source_bar_market_bar", table_name="bot_decision_job_source_bar"
    )
    op.drop_index(
        "uq_bot_decision_job_source_bar_ordinal", table_name="bot_decision_job_source_bar"
    )
    op.drop_table("bot_decision_job_source_bar")
