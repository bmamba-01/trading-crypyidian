"""WP06: opportunity BASE bất biến, entry links và schema proof v2 tương thích v1."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2d4f6a8c0e1"
down_revision: str | None = "d3a5c7e9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "backtest_decision_opportunity"
WRITER = "crypidian_replay_writer"
BUILDER = "crypidian_candidate_builder"
LOCKER = "crypidian_candidate_locker"
WORKSPACE = "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
PROOF_FIELDS = (
    "opportunity_count",
    "no_candidate_count",
    "not_run_count",
    "input_excluded_count",
    "unlabeled_candidate_count",
    "outcome_link_count",
    "source_scope_hash",
    "completeness_verified",
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("trial_record_id", sa.Uuid(), nullable=False),
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("scenario_key", sa.String(120), nullable=False),
        sa.Column("query_version", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("strategy_release_id", sa.String(80), nullable=False),
        sa.Column("strategy_content_hash", sa.String(64), nullable=False),
        sa.Column("decision_timeframe", sa.String(16), nullable=False),
        sa.Column("event_id", sa.String(240), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("source_refs_digest", sa.String(64), nullable=False),
        sa.Column("input_state", sa.String(24), nullable=False),
        sa.Column("strategy_state", sa.String(24), nullable=False),
        sa.Column("decision_outcome", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("evaluation_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runtime_snapshot_id", sa.String(64), nullable=True),
        sa.Column("feature_schema_hash", sa.String(64), nullable=True),
        sa.Column("vector_hash", sa.String(64), nullable=True),
        sa.Column("feature_values_hash", sa.String(64), nullable=True),
        sa.Column("candidate_id", sa.String(64), nullable=True),
        sa.Column("decision_ordinal", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("source_event_digests", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'crypidian-replay-decision-opportunity-v3'",
            name="opportunity_schema_valid",
        ),
        sa.Column("feature_snapshot_ids", postgresql.JSONB(), nullable=False),
        sa.Column("canonical_opportunity", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "query_version",
            "event_id",
            name="uq_opportunity_trial_query_event",
        ),
        sa.UniqueConstraint(
            "workspace_id", "trial_record_id", "event_id", name="uq_opportunity_trial_event"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "trial_record_id",
            "decision_ordinal",
            name="uq_opportunity_trial_ordinal",
        ),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_opportunity_idempotency"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["backtest_run.workspace_id", "backtest_run.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trial_record_id"],
            ["backtest_trial.workspace_id", "backtest_trial.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["backtest_scenario.workspace_id", "backtest_scenario.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "query_version = 'base-decision-opportunity-universe-v1'",
            name="opportunity_query_valid",
        ),
        sa.CheckConstraint(
            (
                "evaluation_start <= decision_at AND decision_at < range_end AND event_"
                "at <= available_at AND available_at <= decision_at"
            ),
            name="opportunity_time_valid",
        ),
        sa.CheckConstraint("decision_ordinal >= 0", name="opportunity_ordinal_valid"),
        sa.CheckConstraint(
            (
                "input_state IN ('EVALUATED','INPUT_EXCLUDED') AND strategy_state IN ('"
                "NO_CANDIDATE','CANDIDATE','NOT_RUN') AND decision_outcome IN ('NO_TRAD"
                "E','PLAN_REJECTED','RISK_REJECTED','RISK_APPROVED','PAPER_INTENT_CREAT"
                "ED') AND (input_state <> 'INPUT_EXCLUDED' OR strategy_state = 'NOT_RUN"
                "') AND ((strategy_state = 'CANDIDATE' AND candidate_id IS NOT NULL) OR"
                " (strategy_state <> 'CANDIDATE' AND candidate_id IS NULL AND decision_"
                "outcome = 'NO_TRADE'))"
            ),
            name="opportunity_states_valid",
        ),
        sa.CheckConstraint(
            (
                "(feature_schema_hash IS NULL) = (vector_hash IS NULL) AND ((input_state"
                " = 'INPUT_EXCLUDED' AND feature_values_hash IS NULL) OR (input_state ="
                " 'EVALUATED' AND runtime_snapshot_id IS NOT NULL AND feature_schema_hash"
                " IS NOT NULL AND vector_hash IS NOT NULL AND feature_values_hash IS NOT NULL))"
            ),
            name="opportunity_vector_valid",
        ),
        sa.CheckConstraint(
            (
                "manifest_hash ~ '^[0-9a-f]{64}$' AND bundle_hash ~ '^[0-9a-f]{64}$' AN"
                "D strategy_content_hash ~ '^[0-9a-f]{64}$' AND snapshot_id ~ '^[0-9a-f"
                "]{64}$' AND source_refs_digest ~ '^[0-9a-f]{64}$' AND content_hash ~ '"
                "^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$' AND (runtime_sn"
                "apshot_id IS NULL OR runtime_snapshot_id ~ '^[0-9a-f]{64}$') AND (feat"
                "ure_schema_hash IS NULL OR feature_schema_hash ~ '^[0-9a-f]{64}$') AND"
                " (vector_hash IS NULL OR vector_hash ~ '^[0-9a-f]{64}$') AND (feature_"
                "values_hash IS NULL OR feature_values_hash ~ '^[0-9a-f]{64}$') AND (candidat"
                "e_id IS NULL OR candidate_id ~ '^[0-9a-f]{64}$')"
            ),
            name="opportunity_hashes_valid",
        ),
        sa.CheckConstraint(
            (
                "length(btrim(reason_code)) > 0 AND length(btrim(symbol)) > 0 AND jsonb"
                "_typeof(feature_snapshot_ids) = 'array' AND jsonb_array_length(feature"
                "_snapshot_ids) > 0"
            ),
            name="opportunity_metadata_valid",
        ),
    )
    op.create_index(
        "ix_opportunity_workspace_trial",
        TABLE,
        ["workspace_id", "trial_record_id", "decision_ordinal"],
    )
    for name, size in (
        ("entry_opportunity_event_id", 240),
        ("entry_candidate_id", 64),
        ("entry_vector_hash", 64),
    ):
        op.add_column("backtest_trade_outcome", sa.Column(name, sa.String(size), nullable=True))
    op.create_foreign_key(
        "fk_outcome_entry_opportunity",
        "backtest_trade_outcome",
        TABLE,
        ["workspace_id", "trial_record_id", "entry_opportunity_event_id"],
        ["workspace_id", "trial_record_id", "event_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_backtest_outcome_entry_candidate",
        "backtest_trade_outcome",
        ["workspace_id", "trial_record_id", "entry_candidate_id"],
    )
    op.create_check_constraint(
        "outcome_opportunity_link_valid",
        "backtest_trade_outcome",
        (
            "(entry_opportunity_event_id IS NULL AND entry_candidate_id IS NULL AND"
            " entry_vector_hash IS NULL) OR (entry_opportunity_event_id IS NOT NULL"
            " AND length(btrim(entry_opportunity_event_id)) > 0 AND entry_candidate"
            "_id IS NOT NULL AND entry_vector_hash IS NOT NULL AND entry_decision_s"
            "napshot_id IS NOT NULL AND entry_candidate_id ~ '^[0-9a-f]{64}$' AND e"
            "ntry_vector_hash ~ '^[0-9a-f]{64}$')"
        ),
    )
    for name in PROOF_FIELDS:
        kind = (
            sa.String(64)
            if name == "source_scope_hash"
            else sa.Boolean()
            if name == "completeness_verified"
            else sa.Integer()
        )
        op.add_column("candidate_universe_proof", sa.Column(name, kind, nullable=True))
    op.drop_constraint(
        "candidate_universe_proof_query_valid", "candidate_universe_proof", type_="check"
    )
    op.create_check_constraint(
        "candidate_universe_proof_query_valid",
        "candidate_universe_proof",
        (
            "query_version IN ('base-closed-trade-universe-v1','base-decision-oppor"
            "tunity-universe-v1')"
        ),
    )
    op.create_check_constraint(
        "candidate_universe_proof_v2_fields_valid",
        "candidate_universe_proof",
        (
            "(query_version = 'base-closed-trade-universe-v1' AND opportunity_count"
            " IS NULL AND no_candidate_count IS NULL AND not_run_count IS NULL AND "
            "input_excluded_count IS NULL AND unlabeled_candidate_count IS NULL AND"
            " outcome_link_count IS NULL AND source_scope_hash IS NULL AND complete"
            "ness_verified IS NULL) OR (query_version = 'base-decision-opportunity-"
            "universe-v1' AND opportunity_count IS NOT NULL AND no_candidate_count "
            "IS NOT NULL AND not_run_count IS NOT NULL AND input_excluded_count IS "
            "NOT NULL AND unlabeled_candidate_count IS NOT NULL AND outcome_link_co"
            "unt IS NOT NULL AND source_scope_hash IS NOT NULL AND completeness_ver"
            "ified IS NOT NULL AND opportunity_count >= 0 AND no_candidate_count >="
            " 0 AND not_run_count >= 0 AND input_excluded_count >= 0 AND unlabeled_"
            "candidate_count >= 0 AND outcome_link_count >= 0 AND opportunity_count"
            " = candidate_count + no_candidate_count + not_run_count AND input_excl"
            "uded_count <= not_run_count AND unlabeled_candidate_count <= exclusion"
            "_count AND outcome_link_count <= candidate_count AND source_scope_hash"
            " ~ '^[0-9a-f]{64}$' AND (NOT completeness_verified OR input_excluded_c"
            "ount = 0))"
        ),
    )
    _security()
    _guards()


def _security() -> None:
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"REVOKE ALL ON TABLE public.{TABLE} FROM PUBLIC, crypidian_app, "
        f"crypidian_admin_web, crypidian_trainer_worker, "
        f"crypidian_trading_runtime, crypidian_execution_gateway, "
        f"crypidian_ingestion_worker, crypidian_backtest_worker, "
        f"crypidian_signal_worker, crypidian_market_signal_worker, "
        f"crypidian_learning_evaluator, crypidian_learning_shadow_observer, "
        f"{WRITER}, {BUILDER}, {LOCKER}"
    )
    op.execute(
        f"CREATE POLICY opportunity_select ON public.{TABLE} FOR SELECT TO "
        f"{WRITER}, {BUILDER}, {LOCKER} USING ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY opportunity_insert ON public.{TABLE} FOR INSERT TO "
        f"{WRITER} WITH CHECK ({WORKSPACE})"
    )
    op.execute(f"GRANT INSERT ON TABLE public.{TABLE} TO {WRITER}")
    op.execute(
        f"GRANT SELECT (id,created_at,workspace_id,trial_record_id,query_version,"
        f"event_id,canonical_opportunity) ON TABLE public.{TABLE} TO {WRITER}"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {BUILDER}")
    op.execute(
        f"GRANT SELECT (workspace_id,run_id,trial_record_id,manifest_hash,"
        f"event_id,input_state,strategy_state,decision_outcome,candidate_id,"
        f"runtime_snapshot_id,symbol,decision_at,feature_schema_hash,vector_hash,"
        f"feature_values_hash) "
        f"ON TABLE public.{TABLE} TO {LOCKER}"
    )


def _guards() -> None:
    # Hàm thuần chỉ canonical hóa metadata phẳng; không SELECT dữ liệu hoặc nâng quyền.
    op.execute(r"""
        CREATE FUNCTION public.wp06_opportunity_json(document jsonb) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
        DECLARE source_text text; output_text text := ''; character text;
        in_string boolean := false; escaped boolean := false; position integer;
        BEGIN
          SELECT json_object_agg(key,value ORDER BY key COLLATE "C")::text INTO source_text
          FROM jsonb_each(document);
          FOR position IN 1..char_length(source_text) LOOP
            character := substr(source_text,position,1);
            IF in_string THEN
              output_text := output_text || character;
              IF escaped THEN escaped := false;
              ELSIF character=chr(92) THEN escaped := true;
              ELSIF character='"' THEN in_string := false; END IF;
            ELSIF character='"' THEN in_string := true; output_text := output_text || character;
            ELSIF character NOT IN (' ',E'\t',E'\n',E'\r') THEN
              output_text := output_text || character;
            END IF;
          END LOOP;
          RETURN output_text;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_opportunity_json(jsonb) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.wp06_opportunity_json(jsonb) TO {WRITER}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.wp06_opportunity_json(jsonb) TO {LOCKER}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.wp06_entry_features_valid(jsonb) TO {LOCKER}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_canonical_float_json(double precision) TO {LOCKER}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_canonical_utc_timestamp_json(timestamptz) "
        f"TO {LOCKER}"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION public.wp06_compact_canonical_json(json) TO {LOCKER}")
    op.execute(r"""
        CREATE FUNCTION public.wp06_entry_feature_values_hash(
          snapshot_id text, decision_at timestamptz, schema_hash text, features jsonb
        ) RETURNS text LANGUAGE plpgsql STABLE STRICT
        SET search_path=pg_catalog,pg_temp AS $$
        DECLARE normalized_features json; document json; canonical text;
        BEGIN
          IF public.wp06_entry_features_valid(features) IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'WP06_ENTRY_FEATURES_INVALID'; END IF;
          SELECT json_agg(json_build_array(
            item.value->>0,
            public.wp06_canonical_float_json((item.value->>1)::double precision)
          ) ORDER BY (item.value->>0) COLLATE "C") INTO normalized_features
            FROM jsonb_array_elements(features) AS item(value);
          document := json_build_object(
            'entry_decision_at',public.wp06_canonical_utc_timestamp_json(decision_at),
            'entry_decision_snapshot_id',snapshot_id,
            'entry_feature_schema_hash',schema_hash,
            'entry_features',normalized_features);
          canonical := public.wp06_compact_canonical_json(document);
          RETURN encode(sha256(convert_to(canonical,'UTF8')),'hex');
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_entry_feature_values_hash(text,timestamptz,text,jsonb) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_entry_feature_values_hash("
        "text,timestamptz,text,jsonb) "
        f"TO {LOCKER}"
    )
    op.execute(r"""
        CREATE FUNCTION public.wp06_trade_outcome_json(document jsonb) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
        DECLARE field_name text; field_value jsonb; field_text text;
        parts text[] := ARRAY[]::text[];
        BEGIN
          IF jsonb_typeof(document)<>'object' THEN
            RAISE EXCEPTION 'WP06_OUTCOME_SCHEMA_MISMATCH'; END IF;
          FOR field_name,field_value IN
            SELECT key,value FROM jsonb_each(document) ORDER BY key COLLATE "C"
          LOOP
            IF field_name='entry_features' THEN
              IF public.wp06_entry_features_valid(field_value) IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION 'WP06_ENTRY_FEATURES_INVALID'; END IF;
              SELECT '[' || string_agg(
                '[' || to_json(item.value->>0)::text || ',' ||
                public.wp06_canonical_float_json(
                  (item.value->>1)::double precision
                )::text || ']', ',' ORDER BY (item.value->>0) COLLATE "C"
              ) || ']' INTO field_text
                FROM jsonb_array_elements(field_value) AS item(value);
            ELSE
              field_text := field_value::text;
            END IF;
            parts := array_append(parts,to_json(field_name)::text || ':' || field_text);
          END LOOP;
          RETURN '{' || array_to_string(parts,',') || '}';
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_trade_outcome_json(jsonb) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.wp06_trade_outcome_json(jsonb) TO {LOCKER}")
    op.execute("""
        CREATE FUNCTION public.wp06_validate_decision_opportunity() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE expected jsonb; document jsonb; expected_key text;
        pair jsonb; frames text[] := ARRAY[]::text[]; digest_frames text[] := ARRAY[]::text[];
        refs_text text;
        BEGIN
          IF NEW.workspace_id IS DISTINCT FROM
             NULLIF(current_setting('app.workspace_id',true),'')::uuid THEN
            RAISE EXCEPTION 'WP06_OPPORTUNITY_WORKSPACE_MISMATCH';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.backtest_run r
            JOIN public.backtest_trial t ON t.workspace_id=r.workspace_id AND t.run_id=r.id
            JOIN public.backtest_scenario s ON s.workspace_id=t.workspace_id
              AND s.trial_record_id=t.id AND s.run_id=r.id
            WHERE r.workspace_id=NEW.workspace_id AND r.id=NEW.run_id
              AND t.id=NEW.trial_record_id AND s.id=NEW.scenario_id
              AND r.manifest_hash=NEW.manifest_hash AND t.manifest_hash=NEW.manifest_hash
              AND s.manifest_hash=NEW.manifest_hash AND s.kind='BASE'
              AND s.scenario_id=NEW.scenario_key AND t.status='COMPLETED'
              AND NEW.decision_ordinal<t.observation_count
              AND r.manifest->>'learning_intent'='TRAINING_BASE'
              AND r.manifest->>'dataset_hash'=NEW.bundle_hash
              AND r.manifest->>'strategy_release_id'=NEW.strategy_release_id
              AND r.manifest->>'strategy_content_hash'=NEW.strategy_content_hash
              AND (r.manifest->>'range_start')::timestamptz=NEW.evaluation_start
              AND (r.manifest->>'range_end')::timestamptz=NEW.range_end
          ) THEN RAISE EXCEPTION 'WP06_OPPORTUNITY_SCOPE_MISMATCH'; END IF;
          IF EXISTS (SELECT 1 FROM public.backtest_trade_outcome o
            WHERE o.workspace_id=NEW.workspace_id AND o.trial_record_id=NEW.trial_record_id
            AND o.entry_opportunity_event_id IS NULL) THEN
            RAISE EXCEPTION 'WP06_OPPORTUNITY_TRIAL_HAS_UNLINKED_OUTCOME';
          END IF;
          IF jsonb_typeof(NEW.feature_snapshot_ids) <> 'array'
             OR jsonb_typeof(NEW.source_event_digests) <> 'array' THEN
            RAISE EXCEPTION 'WP06_OPPORTUNITY_FEATURE_IDS_INVALID'; END IF;
          FOR pair IN SELECT value FROM jsonb_array_elements(NEW.feature_snapshot_ids) LOOP
            IF jsonb_typeof(pair)<>'array' THEN
              RAISE EXCEPTION 'WP06_OPPORTUNITY_FEATURE_IDS_INVALID'; END IF;
            IF jsonb_array_length(pair)<>2 THEN
              RAISE EXCEPTION 'WP06_OPPORTUNITY_FEATURE_IDS_INVALID'; END IF;
            IF jsonb_typeof(pair->0)<>'string' OR jsonb_typeof(pair->1)<>'string'
              OR (pair->>0)!~'^[1-9][0-9]*[mhdwM]$' OR (pair->>1)!~'^[0-9a-f]{64}$'
              OR (pair->>0)=ANY(frames) THEN
              RAISE EXCEPTION 'WP06_OPPORTUNITY_FEATURE_IDS_INVALID'; END IF;
            frames := array_append(frames,pair->>0);
          END LOOP;
          FOR pair IN SELECT value FROM jsonb_array_elements(NEW.source_event_digests) LOOP
            IF jsonb_typeof(pair)<>'array' THEN
              RAISE EXCEPTION 'WP06_OPPORTUNITY_DIGEST_MAP_INVALID'; END IF;
            IF jsonb_array_length(pair)<>2 THEN
              RAISE EXCEPTION 'WP06_OPPORTUNITY_DIGEST_MAP_INVALID'; END IF;
            IF jsonb_typeof(pair->0)<>'string' OR jsonb_typeof(pair->1)<>'string'
              OR (pair->>1)!~'^[0-9a-f]{64}$' OR (pair->>0)=ANY(digest_frames) THEN
              RAISE EXCEPTION 'WP06_OPPORTUNITY_DIGEST_MAP_INVALID'; END IF;
            digest_frames := array_append(digest_frames,pair->>0);
          END LOOP;
          IF cardinality(frames)=0 OR frames IS DISTINCT FROM digest_frames THEN
            RAISE EXCEPTION 'WP06_OPPORTUNITY_DIGEST_MAP_KEYS_MISMATCH'; END IF;
          SELECT '[' || string_agg(public.wp06_opportunity_json(jsonb_build_object(
            'timeframe',value->>0,'source_event_digest',value->>1)),',' ORDER BY ord) || ']'
            INTO refs_text FROM jsonb_array_elements(NEW.source_event_digests)
            WITH ORDINALITY AS refs(value,ord);
          IF NEW.source_refs_digest IS DISTINCT FROM
            encode(sha256(convert_to(refs_text,'UTF8')),'hex') THEN
            RAISE EXCEPTION 'WP06_OPPORTUNITY_SOURCE_REFS_DIGEST_MISMATCH'; END IF;
          document := NEW.canonical_opportunity::jsonb;
          expected := to_jsonb(NEW) - ARRAY['id','created_at','run_id','trial_record_id',
            'scenario_id','scenario_key','canonical_opportunity','content_hash','idempotency_key']
            || jsonb_build_object('scenario_id',NEW.scenario_key,
              'evaluation_start',to_char(NEW.evaluation_start AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
              'range_end',to_char(NEW.range_end AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
              'event_at',to_char(NEW.event_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
              'available_at',to_char(NEW.available_at AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
              'decision_at',to_char(NEW.decision_at AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
          IF document IS DISTINCT FROM expected OR
             NEW.canonical_opportunity IS DISTINCT FROM public.wp06_opportunity_json(expected)
             OR NEW.content_hash IS DISTINCT FROM
             encode(sha256(convert_to(NEW.canonical_opportunity,'UTF8')),'hex') THEN
            RAISE EXCEPTION 'WP06_OPPORTUNITY_CANONICAL_MISMATCH';
          END IF;
          expected_key := public.wp06_opportunity_json(jsonb_build_object(
             'query_version',NEW.query_version,'schema_version',NEW.schema_version,
             'workspace_id',NEW.workspace_id,
             'manifest_hash',NEW.manifest_hash,'scenario_id',NEW.scenario_key,'event_id',
             NEW.event_id));
          IF NEW.idempotency_key IS DISTINCT FROM
            encode(sha256(convert_to(expected_key,'UTF8')),'hex') THEN
            RAISE EXCEPTION 'WP06_OPPORTUNITY_IDEMPOTENCY_MISMATCH';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.wp06_validate_decision_opportunity() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER a_wp06_opportunity_insert_fence BEFORE INSERT ON "
        f"public.{TABLE} FOR EACH ROW EXECUTE FUNCTION "
        f"public.wp06_guard_outcome_insert()"
    )
    op.execute(
        f"CREATE TRIGGER wp06_opportunity_validate BEFORE INSERT ON "
        f"public.{TABLE} FOR EACH ROW EXECUTE FUNCTION "
        f"public.wp06_validate_decision_opportunity()"
    )
    op.execute("""
        CREATE FUNCTION public.wp06_validate_outcome_opportunity() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE expected_features_hash text; expected_keys text[]; actual_key_count integer;
        BEGIN
          IF NEW.workspace_id IS DISTINCT FROM
             NULLIF(current_setting('app.workspace_id',true),'')::uuid THEN
            RAISE EXCEPTION 'WP06_OUTCOME_OPPORTUNITY_WORKSPACE_MISMATCH';
          END IF;
          IF jsonb_typeof(NEW.outcome)<>'object' THEN
            RAISE EXCEPTION 'WP06_OUTCOME_SCHEMA_MISMATCH'; END IF;
          IF NEW.entry_opportunity_event_id IS NOT NULL THEN
            expected_keys := ARRAY['trade_id','opened_at','closed_at','gross_pnl','fees',
              'funding','slippage','exposure','entry_decision_snapshot_id','entry_decision_at',
              'entry_feature_schema_hash','entry_features','entry_features_hash','entry_symbol',
              'entry_opportunity_event_id','entry_candidate_id','entry_vector_hash'];
          ELSIF NEW.entry_decision_snapshot_id IS NOT NULL THEN
            expected_keys := ARRAY['trade_id','opened_at','closed_at','gross_pnl','fees',
              'funding','slippage','exposure','entry_decision_snapshot_id','entry_decision_at',
              'entry_feature_schema_hash','entry_features','entry_features_hash','entry_symbol'];
          ELSE
            expected_keys := ARRAY['trade_id','opened_at','closed_at','gross_pnl','fees',
              'funding','slippage','exposure'];
          END IF;
          SELECT count(*) INTO actual_key_count FROM jsonb_object_keys(NEW.outcome);
          IF actual_key_count<>cardinality(expected_keys)
             OR NOT (NEW.outcome ?& expected_keys)
             OR NEW.outcome IS DISTINCT FROM NEW.canonical_json::jsonb
             OR NEW.canonical_json IS DISTINCT FROM public.wp06_trade_outcome_json(NEW.outcome)
             OR NEW.outcome_hash IS DISTINCT FROM
                encode(sha256(convert_to(NEW.canonical_json,'UTF8')),'hex') THEN
            RAISE EXCEPTION 'WP06_OUTCOME_ENTRY_CANONICAL_MISMATCH'; END IF;
          IF NEW.entry_opportunity_event_id IS NULL THEN
            IF EXISTS (SELECT 1 FROM public.backtest_decision_opportunity p
              WHERE p.workspace_id=NEW.workspace_id AND p.trial_record_id=NEW.trial_record_id)
            THEN RAISE EXCEPTION 'WP06_OUTCOME_ENTRY_LINK_REQUIRED'; END IF;
            IF NEW.entry_decision_snapshot_id IS NOT NULL AND
               public.wp06_entry_feature_values_hash(
                 NEW.entry_decision_snapshot_id,NEW.entry_decision_at,
                 NEW.entry_feature_schema_hash,NEW.entry_features
               ) IS DISTINCT FROM NEW.entry_features_hash THEN
              RAISE EXCEPTION 'WP06_OUTCOME_ENTRY_FEATURES_HASH_MISMATCH'; END IF;
            RETURN NEW;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM public.backtest_decision_opportunity p
            WHERE p.workspace_id=NEW.workspace_id AND p.run_id=NEW.run_id
              AND p.trial_record_id=NEW.trial_record_id AND p.manifest_hash=NEW.manifest_hash
              AND p.event_id=NEW.entry_opportunity_event_id AND p.input_state='EVALUATED'
              AND p.strategy_state='CANDIDATE' AND p.decision_outcome='PAPER_INTENT_CREATED'
              AND p.candidate_id=NEW.entry_candidate_id
              AND p.runtime_snapshot_id=NEW.entry_decision_snapshot_id
              AND p.symbol=NEW.entry_symbol AND p.decision_at=NEW.entry_decision_at
              AND p.feature_schema_hash=NEW.entry_feature_schema_hash
              AND p.vector_hash=NEW.entry_vector_hash
          ) THEN RAISE EXCEPTION 'WP06_OUTCOME_OPPORTUNITY_MISMATCH'; END IF;
          IF public.wp06_entry_feature_values_hash(
               NEW.entry_decision_snapshot_id,NEW.entry_decision_at,
               NEW.entry_feature_schema_hash,NEW.entry_features
             ) IS DISTINCT FROM NEW.entry_features_hash THEN
            RAISE EXCEPTION 'WP06_OUTCOME_ENTRY_FEATURES_HASH_MISMATCH'; END IF;
          SELECT p.feature_values_hash INTO expected_features_hash
            FROM public.backtest_decision_opportunity p
            WHERE p.workspace_id=NEW.workspace_id AND p.run_id=NEW.run_id
              AND p.trial_record_id=NEW.trial_record_id AND p.manifest_hash=NEW.manifest_hash
              AND p.event_id=NEW.entry_opportunity_event_id;
          IF NOT FOUND OR expected_features_hash IS DISTINCT FROM NEW.entry_features_hash THEN
            RAISE EXCEPTION 'WP06_OUTCOME_ENTRY_FEATURE_VALUES_MISMATCH'; END IF;
          IF NEW.outcome->>'entry_opportunity_event_id' IS DISTINCT FROM
             NEW.entry_opportunity_event_id
             OR NEW.outcome->>'entry_candidate_id' IS DISTINCT FROM NEW.entry_candidate_id
             OR NEW.outcome->>'entry_vector_hash' IS DISTINCT FROM NEW.entry_vector_hash
             OR NEW.outcome->>'entry_decision_snapshot_id' IS DISTINCT FROM
             NEW.entry_decision_snapshot_id
             OR (NEW.outcome->>'entry_decision_at')::timestamptz IS DISTINCT FROM
             NEW.entry_decision_at
             OR NEW.outcome->>'entry_feature_schema_hash' IS DISTINCT FROM
             NEW.entry_feature_schema_hash
             OR NEW.outcome->>'entry_symbol' IS DISTINCT FROM NEW.entry_symbol
             OR NEW.outcome->>'entry_features_hash' IS DISTINCT FROM NEW.entry_features_hash
             OR NEW.outcome->'entry_features' IS DISTINCT FROM NEW.entry_features THEN
            RAISE EXCEPTION 'WP06_OUTCOME_ENTRY_CANONICAL_MISMATCH';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(f"ALTER FUNCTION public.wp06_validate_outcome_opportunity() OWNER TO {LOCKER}")
    op.execute("REVOKE ALL ON FUNCTION public.wp06_validate_outcome_opportunity() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER wp06_outcome_opportunity_validate BEFORE INSERT ON publ"
        "ic.backtest_trade_outcome FOR EACH ROW EXECUTE FUNCTION public.wp06_va"
        "lidate_outcome_opportunity()"
    )
    op.execute("""
        CREATE FUNCTION public.reject_wp06_opportunity_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
          RAISE EXCEPTION 'WP06_OPPORTUNITY_IMMUTABLE';
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.reject_wp06_opportunity_mutation() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER wp06_opportunity_immutable BEFORE UPDATE OR DELETE ON "
        f"public.{TABLE} FOR EACH ROW EXECUTE FUNCTION "
        f"public.reject_wp06_opportunity_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.backtest_decision_opportunity, public.backtest_trade"
        "_outcome, public.candidate_universe_proof IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM public.backtest_decision_opportunity)
        OR EXISTS (SELECT 1 FROM public.backtest_trade_outcome WHERE
        entry_opportunity_event_id IS NOT NULL)
        OR EXISTS (SELECT 1 FROM public.candidate_universe_proof WHERE
        query_version='base-decision-opportunity-universe-v1')
      THEN RAISE EXCEPTION 'WP06_OPPORTUNITY_DOWNGRADE_REQUIRES_EMPTY_V2'; END IF;
    END $$""")
    op.execute("DROP TRIGGER wp06_outcome_opportunity_validate ON public.backtest_trade_outcome")
    op.execute("DROP FUNCTION public.wp06_validate_outcome_opportunity()")
    op.execute("DROP TRIGGER wp06_opportunity_immutable ON public.backtest_decision_opportunity")
    op.execute("DROP TRIGGER wp06_opportunity_validate ON public.backtest_decision_opportunity")
    op.execute(
        "DROP TRIGGER a_wp06_opportunity_insert_fence ON public.backtest_decision_opportunity"
    )
    op.execute("DROP FUNCTION public.reject_wp06_opportunity_mutation()")
    op.execute("DROP FUNCTION public.wp06_validate_decision_opportunity()")
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.wp06_opportunity_json(jsonb) FROM {LOCKER}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.wp06_trade_outcome_json(jsonb) FROM {LOCKER}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.wp06_entry_features_valid(jsonb) FROM {LOCKER}")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_canonical_float_json(double precision) "
        f"FROM {LOCKER}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_canonical_utc_timestamp_json(timestamptz) "
        f"FROM {LOCKER}"
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.wp06_compact_canonical_json(json) FROM {LOCKER}")
    op.execute("DROP FUNCTION public.wp06_entry_feature_values_hash(text,timestamptz,text,jsonb)")
    op.execute("DROP FUNCTION public.wp06_trade_outcome_json(jsonb)")
    op.execute("DROP FUNCTION public.wp06_opportunity_json(jsonb)")
    op.drop_constraint("fk_outcome_entry_opportunity", "backtest_trade_outcome", type_="foreignkey")
    op.drop_constraint(
        "uq_backtest_outcome_entry_candidate", "backtest_trade_outcome", type_="unique"
    )
    op.drop_constraint("outcome_opportunity_link_valid", "backtest_trade_outcome", type_="check")
    for name in ("entry_opportunity_event_id", "entry_candidate_id", "entry_vector_hash"):
        op.drop_column("backtest_trade_outcome", name)
    op.drop_table(TABLE)
    op.drop_constraint(
        "candidate_universe_proof_v2_fields_valid", "candidate_universe_proof", type_="check"
    )
    op.drop_constraint(
        "candidate_universe_proof_query_valid", "candidate_universe_proof", type_="check"
    )
    op.create_check_constraint(
        "candidate_universe_proof_query_valid",
        "candidate_universe_proof",
        "query_version = 'base-closed-trade-universe-v1'",
    )
    for name in PROOF_FIELDS:
        op.drop_column("candidate_universe_proof", name)
