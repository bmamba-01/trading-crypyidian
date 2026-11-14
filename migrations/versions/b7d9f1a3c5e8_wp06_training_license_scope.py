"""WP06: quyền TRAINING exact-scope cho replay bundle và normalized sample."""

# ruff: noqa: E501  # Giữ nguyên từng câu SQL dài để đối chiếu trực tiếp với PostgreSQL.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d9f1a3c5e8"
down_revision: str | None = "a4c6e8f0b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
APP = "crypidian_app"
CANDIDATE_BUILDER = "crypidian_candidate_builder"
EVALUATOR = "crypidian_learning_evaluator"
EVALUATION_READER = "crypidian_learning_evaluation_reader"
TRAINER = "crypidian_trainer_worker"
TRAINER_READER = "crypidian_learning_batch_reader"
RIGHTS_READER = "crypidian_learning_source_rights_reader"
TABLE = "learning_replay_training_receipt"
GATE = "public.wp06_assert_training_source_rights"
SCOPE_CHECK = "public.wp06_check_replay_training_scope"
NORMALIZE_GATE = "public.wp06_record_replay_training_receipts"
WORKSPACE = "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
LINEAGE_METADATA_GRANTS = {
    "source_policy_revision": (
        "workspace_id, source_id, sequence, policy_id, state, allowed_purposes, valid_until, "
        "reviewed_at, actor_id, approved_by, provider, product, venue, contract, "
        "license_scope, license_evidence_id, license_digest, license_scope_hash"
    ),
    "source_license_evidence": (
        "workspace_id, id, source_id, policy_id, evidence_ref, digest, scope_version, license_scope, "
        "canonical_scope, scope_hash, byte_count"
    ),
    "dataset_manifest": (
        "workspace_id, id, manifest_hash, manifest, canonical_manifest, split_policy_version"
    ),
    "learning_normalized_dataset": (
        "workspace_id, id, manifest_hash, fold_plan_hash, split_policy_version, approval_hash"
    ),
    "backtest_trade_outcome": (
        "workspace_id, id, run_id, trial_record_id, manifest_hash, outcome_hash"
    ),
    "backtest_trial": "workspace_id, id, run_id, manifest_hash",
    "backtest_run": "workspace_id, id, manifest_hash, manifest, canonical_manifest",
    "backtest_run_dataset_rights": (
        "workspace_id, run_id, bundle_hash, child_dataset_hash, interval, provider, purpose, "
        "terms_url, policy_fingerprint, verified_at, checked_at, valid_until, evidence_json, "
        "canonical_evidence, evidence_hash"
    ),
    "backtest_dataset_replay_bundle": (
        "workspace_id, bundle_hash, schema_version, symbol, evaluation_start, range_end, "
        "rights_provider, rights_purpose, policy_fingerprint, bundle_json, canonical_bundle"
    ),
    "backtest_dataset_manifest": (
        "workspace_id, dataset_hash, status, symbol, interval, warmup_start, evaluation_start, "
        "range_end, prepared_at, policy_fingerprint, manifest_json, canonical_manifest"
    ),
    "learning_split_candidate": "workspace_id, manifest_hash, fold_plan_hash, trial_record_id",
    "market_bar": (
        "workspace_id, id, symbol, timeframe, source, logical_key, revision, supersedes_id, "
        "checksum, open_time, available_at, is_closed"
    ),
    "source_policy_observation": "workspace_id, source_id, sequence, policy_id, known_at",
    "outbox_event": "id, workspace_id, event_type, payload",
    "consumer_receipt": "workspace_id, outbox_event_id, consumer_name",
}
LEGACY_RIGHTS_READER_COLUMNS = {
    "source_policy_revision": (
        "workspace_id, source_id, sequence, policy_id, state, allowed_purposes, valid_until"
    ),
    "dataset_manifest": "workspace_id, id, manifest_hash",
    "source_policy_observation": "workspace_id, source_id, sequence, policy_id, known_at",
    "outbox_event": "id, workspace_id, event_type, payload",
    "consumer_receipt": "workspace_id, outbox_event_id, consumer_name",
}


def upgrade() -> None:
    """Ghim quyền TRAINING theo lineage đã lưu và không cấp quyền đọc ledger gốc."""
    _grant_candidate_builder_receipt_read()
    _preserve_legacy_functions()
    _create_receipt_table()
    _grant_lineage_metadata()
    _create_bundle_gate()
    _create_scope_check()
    _replace_training_rights_gate()
    _replace_trainer_batch_gateway()


def _grant_candidate_builder_receipt_read() -> None:
    """Cho proof builder đọc receipt quyền metadata trong workspace hiện hành."""
    op.execute(f"""
        CREATE POLICY wp06_candidate_builder_rights_receipt_read
        ON public.backtest_run_dataset_rights FOR SELECT
        TO {CANDIDATE_BUILDER}
        USING (workspace_id = NULLIF(
            pg_catalog.current_setting('app.workspace_id', true), '')::uuid)
    """)
    op.execute(f"GRANT SELECT ON TABLE public.backtest_run_dataset_rights TO {CANDIDATE_BUILDER}")


def _preserve_legacy_functions() -> None:
    """Giữ nguyên preimage để downgrade khôi phục đúng function đã triển khai."""
    op.execute(
        f"ALTER FUNCTION {GATE}(uuid,text,jsonb,uuid,text,text,uuid) "
        "RENAME TO wp06_assert_training_source_rights_pre_license_scope"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_assert_training_source_rights_pre_license_scope"
        f"(uuid,text,jsonb,uuid,text,text,uuid) FROM PUBLIC, {EVALUATION_READER}, {TRAINER_READER}"
    )
    op.execute(
        "ALTER FUNCTION public.wp06_claim_learning_training_batch(uuid,uuid) "
        "RENAME TO wp06_claim_learning_training_batch_pre_license_scope"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_claim_learning_training_batch_pre_license_scope"
        f"(uuid,uuid) FROM PUBLIC, {TRAINER}"
    )


def _create_receipt_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("access_kind", sa.String(16), nullable=False),
        sa.Column("access_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("fold_plan_hash", sa.String(64), nullable=False),
        sa.Column("sample_id", sa.String(128), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("child_dataset_hash", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("product", sa.String(128), nullable=False),
        sa.Column("venue", sa.String(128), nullable=False),
        sa.Column("contract", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(128), nullable=False),
        sa.Column("interval", sa.String(16), nullable=False),
        sa.Column("data_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bar_identity_hash", sa.String(64), nullable=False),
        sa.Column("policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("current_policy_fingerprint", sa.String(64), nullable=False),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("policy_id", sa.String(160), nullable=False),
        sa.Column("policy_sequence", sa.Integer(), nullable=False),
        sa.Column("license_evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("license_scope_hash", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "access_kind IN ('normalization', 'evaluation', 'training')",
            name="learning_replay_training_receipt_access_kind_valid",
        ),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$' AND fold_plan_hash ~ '^[0-9a-f]{64}$' "
            "AND bundle_hash ~ '^[0-9a-f]{64}$' "
            "AND child_dataset_hash ~ '^[0-9a-f]{64}$' "
            "AND bar_identity_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND current_policy_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND scope_hash ~ '^[0-9a-f]{64}$' "
            "AND evidence_digest ~ '^[0-9a-f]{64}$' "
            "AND license_scope_hash ~ '^[0-9a-f]{64}$'",
            name="learning_replay_training_receipt_hashes_valid",
        ),
        sa.CheckConstraint(
            "purpose = 'training' AND policy_sequence > 0 "
            "AND data_start < data_end AND checked_at IS NOT NULL",
            name="learning_replay_training_receipt_scope_valid",
        ),
        sa.CheckConstraint(
            "length(sample_id) BETWEEN 1 AND 128 "
            "AND length(source_id) BETWEEN 1 AND 128 "
            "AND length(provider) BETWEEN 1 AND 128 "
            "AND length(product) BETWEEN 1 AND 128 "
            "AND length(venue) BETWEEN 1 AND 128 "
            "AND length(contract) BETWEEN 1 AND 128 "
            "AND length(symbol) BETWEEN 1 AND 128 "
            "AND length(interval) BETWEEN 1 AND 16 "
            "AND length(policy_id) BETWEEN 1 AND 160",
            name="learning_replay_training_receipt_identifiers_valid",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manifest_hash", "fold_plan_hash", "normalized_dataset_id"],
            [
                "learning_normalized_dataset.workspace_id",
                "learning_normalized_dataset.manifest_hash",
                "learning_normalized_dataset.fold_plan_hash",
                "learning_normalized_dataset.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_replay_training_receipt_dataset",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "license_evidence_id"],
            [
                "source_license_evidence.workspace_id",
                "source_license_evidence.source_id",
                "source_license_evidence.id",
            ],
            ondelete="RESTRICT",
            name="fk_learning_replay_training_receipt_evidence",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_replay_training_receipt")),
        sa.UniqueConstraint(
            "workspace_id",
            "access_kind",
            "access_id",
            "normalized_dataset_id",
            "sample_id",
            "child_dataset_hash",
            name="uq_learning_replay_training_receipt_access_child",
        ),
    )
    op.create_index(
        "ix_learning_replay_training_receipt_manifest",
        TABLE,
        ["workspace_id", "manifest_hash", "fold_plan_hash", "sample_id"],
    )
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"REVOKE ALL ON TABLE public.{TABLE} FROM PUBLIC, {APP}, {ADMIN}, {TRAINER}, {EVALUATOR}, {RIGHTS_READER}"
    )
    op.execute(
        f"CREATE POLICY wp06_replay_training_receipt_reader ON public.{TABLE} "
        f"FOR ALL TO {RIGHTS_READER} USING ({WORKSPACE}) WITH CHECK ({WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY wp06_replay_training_receipt_admin_read ON public.{TABLE} "
        f"FOR SELECT TO {ADMIN} USING ({WORKSPACE})"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{TABLE} TO {RIGHTS_READER}")
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ADMIN}")
    op.execute(
        "CREATE FUNCTION public.wp06_reject_replay_training_receipt_mutation() "
        "RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$ "
        "BEGIN RAISE EXCEPTION 'WP06_REPLAY_TRAINING_RECEIPT_IMMUTABLE'; END $$"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_reject_replay_training_receipt_mutation() FROM PUBLIC"
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE OR DELETE ON public.{TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION public.wp06_reject_replay_training_receipt_mutation()"
    )


def _grant_lineage_metadata() -> None:
    """Chỉ mở cột metadata cần để verifier dựng lại lineage có thẩm quyền."""
    grants = LINEAGE_METADATA_GRANTS
    for table, columns in grants.items():
        predicate = WORKSPACE
        if table == "outbox_event":
            predicate += " AND event_type = 'research.rights_changed.v1'"
        elif table == "consumer_receipt":
            predicate += " AND consumer_name = 'wp06.dataset-rights-eligibility.v1'"
        op.execute(
            f"CREATE POLICY wp06_replay_scope_reader_select ON public.{table} "
            f"FOR SELECT TO {RIGHTS_READER} USING ({predicate})"
        )
        op.execute(f"GRANT SELECT ({columns}) ON public.{table} TO {RIGHTS_READER}")
    # Composite fingerprint cần đọc trọn snapshot policy; chỉ NOLOGIN RIGHTS_READER
    # được cấp quyền này, còn RLS giới hạn hàng trong workspace hiện hành.
    op.execute(f"GRANT SELECT ON public.source_policy_revision TO {RIGHTS_READER}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_sha256_canonical_json(json) TO {RIGHTS_READER}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_compact_canonical_json(json) TO {RIGHTS_READER}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_canonical_utc_timestamp_json(timestamptz) "
        f"TO {RIGHTS_READER}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) TO {RIGHTS_READER}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.wp06_license_scope_valid(jsonb) TO {RIGHTS_READER}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_license_source_policy_fingerprint("
        "public.source_policy_revision) "
        f"TO {RIGHTS_READER}"
    )


def _create_bundle_gate() -> None:
    """Cấp receipt metadata theo bundle sau khi DB tự dựng đúng scope từng child."""
    op.execute("""
        CREATE FUNCTION public.wp06_authorize_replay_training_bundle(
            target_workspace uuid,
            target_bundle_hash text,
            target_operation text
        ) RETURNS TABLE (
            workspace_id uuid, bundle_hash text, child_dataset_hash text, "interval" text,
            symbol text, bars_content_hash text, policy_id text, policy_fingerprint text,
            bundle_policy_fingerprint text, terms_url text, verified_at timestamptz,
            checked_at timestamptz, valid_until timestamptz, purpose text, provider text,
            license_evidence_id uuid, license_scope_hash text,
            current_policy_fingerprint text, evidence_digest text, scope_hash text,
            data_start timestamptz, data_end timestamptz, product text, venue text, contract text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        #variable_conflict use_variable
        DECLARE
            bundle_row record;
            child_row record;
            policy_row record;
            evidence_row record;
            component jsonb;
            source_id text;
            provider text;
            market_source text;
            use_outputs jsonb;
            use_derived jsonb;
            use_scope json;
            use_scope_hash text;
            identity_hash text;
            identity_count integer;
            expected_children integer;
            current_fingerprint text;
            checked_at timestamptz;
            child_hash text;
            child_interval text;
            child_symbol text;
            data_start timestamptz;
            data_end timestamptz;
            source_key text;
        BEGIN
            IF (target_operation = 'training' AND session_user <> 'crypidian_candidate_builder')
               OR (target_operation = 'evaluation' AND session_user <> 'crypidian_learning_evaluator')
               OR target_operation NOT IN ('training', 'evaluation') THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_BUNDLE_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_BUNDLE_WORKSPACE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_READ_COMMITTED_REQUIRED';
            END IF;
            IF target_bundle_hash IS NULL OR length(target_bundle_hash) <> 64
               OR target_bundle_hash !~ '^[0-9a-f]+$' THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_BUNDLE_HASH_INVALID';
            END IF;
            SELECT bundle.workspace_id, bundle.bundle_hash, bundle.schema_version,
                   bundle.symbol, bundle.evaluation_start, bundle.range_end,
                   bundle.rights_provider, bundle.rights_purpose, bundle.policy_fingerprint,
                   bundle.bundle_json, bundle.canonical_bundle
              INTO bundle_row
              FROM public.backtest_dataset_replay_bundle AS bundle
             WHERE bundle.workspace_id = target_workspace
               AND bundle.bundle_hash = target_bundle_hash;
            IF NOT FOUND OR bundle_row.bundle_hash <> target_bundle_hash
               OR bundle_row.rights_purpose <> 'BACKTEST_RESEARCH'
               OR bundle_row.bundle_json IS DISTINCT FROM bundle_row.canonical_bundle::jsonb
               OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(bundle_row.bundle_json)) <> 9
               OR bundle_row.bundle_json->>'schema_version' <> bundle_row.schema_version::text
               OR bundle_row.bundle_json->>'workspace_id' <> target_workspace::text
               OR bundle_row.bundle_json->>'symbol' <> bundle_row.symbol
               OR (bundle_row.bundle_json->>'evaluation_start')::timestamptz
                    IS DISTINCT FROM bundle_row.evaluation_start
               OR (bundle_row.bundle_json->>'range_end')::timestamptz
                    IS DISTINCT FROM bundle_row.range_end
               OR bundle_row.bundle_json->>'rights_provider' <> bundle_row.rights_provider
               OR bundle_row.bundle_json->>'rights_purpose' <> bundle_row.rights_purpose
               OR bundle_row.bundle_json->>'policy_fingerprint' <> bundle_row.policy_fingerprint
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    bundle_row.canonical_bundle, 'UTF8')), 'hex') IS DISTINCT FROM target_bundle_hash
               OR pg_catalog.jsonb_typeof(bundle_row.bundle_json->'components') <> 'array'
               OR pg_catalog.jsonb_array_length(bundle_row.bundle_json->'components') = 0 THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_BUNDLE_CANONICAL_MISMATCH';
            END IF;
            SELECT count(*)::integer INTO expected_children
              FROM pg_catalog.jsonb_array_elements(bundle_row.bundle_json->'components') AS item(value);
            IF expected_children <> (
                SELECT count(DISTINCT item.value->>'dataset_hash')
                  FROM pg_catalog.jsonb_array_elements(bundle_row.bundle_json->'components') AS item(value)
            ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_BUNDLE_CHILD_DUPLICATE';
            END IF;
            IF target_operation = 'evaluation' THEN
                use_outputs := '["aggregate_report"]'::jsonb;
                use_derived := '["aggregate_reporting"]'::jsonb;
            ELSE
                use_outputs := '["derived_features","trained_model"]'::jsonb;
                use_derived := '["feature_extraction","model_training"]'::jsonb;
            END IF;

            FOR source_key IN SELECT DISTINCT bundle_row.rights_provider ORDER BY 1 LOOP
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_key, 0));
            END LOOP;
            checked_at := pg_catalog.clock_timestamp();

            FOR component IN
                SELECT item.value
                  FROM pg_catalog.jsonb_array_elements(bundle_row.bundle_json->'components') AS item(value)
                 ORDER BY item.value->>'dataset_hash' COLLATE "C"
            LOOP
                child_hash := component->>'dataset_hash';
                child_interval := component->>'interval';
                IF child_hash IS NULL OR length(child_hash) <> 64
                   OR child_hash !~ '^[0-9a-f]+$' OR child_interval IS NULL
                   OR length(child_interval) > 8 THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_COMPONENT_INVALID';
                END IF;
                SELECT child.workspace_id, child.dataset_hash, child.status, child.symbol,
                       child.interval, child.warmup_start, child.evaluation_start,
                       child.range_end, child.prepared_at,
                       child.policy_fingerprint, child.manifest_json, child.canonical_manifest
                  INTO child_row
                  FROM public.backtest_dataset_manifest AS child
                 WHERE child.workspace_id = target_workspace
                   AND child.dataset_hash = child_hash;
                IF NOT FOUND OR child_row.status <> 'READY'
                   OR child_row.interval <> child_interval
                   OR child_row.symbol <> bundle_row.symbol
                   OR child_row.evaluation_start <> bundle_row.evaluation_start
                   OR child_row.range_end <> bundle_row.range_end
                   OR child_row.manifest_json IS DISTINCT FROM child_row.canonical_manifest::jsonb
                   OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(child_row.manifest_json)) <> 28
                   OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                        public.wp06_license_canonical_json(
                            child_row.manifest_json - 'dataset_hash'
                        ), 'UTF8')), 'hex') IS DISTINCT FROM child_hash
                   OR child_row.manifest_json->>'schema_version' <> '4'
                   OR child_row.manifest_json->>'dataset_hash' <> child_hash
                   OR child_row.manifest_json->>'status' <> child_row.status
                   OR child_row.manifest_json->>'symbol' <> child_row.symbol
                   OR child_row.manifest_json->>'interval' <> child_row.interval
                   OR (child_row.manifest_json->>'warmup_start')::timestamptz
                        IS DISTINCT FROM child_row.warmup_start
                   OR (child_row.manifest_json->>'evaluation_start')::timestamptz
                        IS DISTINCT FROM child_row.evaluation_start
                   OR (child_row.manifest_json->>'range_end')::timestamptz
                        IS DISTINCT FROM child_row.range_end
                   OR (child_row.manifest_json->>'prepared_at')::timestamptz
                        IS DISTINCT FROM child_row.prepared_at
                   OR child_row.manifest_json->>'policy_fingerprint'
                        IS DISTINCT FROM child_row.policy_fingerprint
                   OR child_row.manifest_json->>'bar_count'
                        IS DISTINCT FROM child_row.manifest_json->>'expected_bar_count'
                   OR child_row.manifest_json->>'provider' <> bundle_row.rights_provider
                   OR child_row.manifest_json->>'policy_fingerprint'
                        IS DISTINCT FROM bundle_row.policy_fingerprint
                   OR child_row.manifest_json->>'rights_workspace_id' IS DISTINCT FROM target_workspace::text
                   OR child_row.manifest_json->>'purpose' IS DISTINCT FROM 'BACKTEST_RESEARCH'
                   OR child_row.manifest_json->>'bar_identity_hash' IS NULL
                   OR (component->>'bars_content_hash')
                        IS DISTINCT FROM child_row.manifest_json->>'bars_content_hash'
                   OR NOT EXISTS (
                        SELECT 1 FROM public.backtest_run_dataset_rights AS rights
                         WHERE rights.workspace_id = target_workspace
                           AND rights.bundle_hash = target_bundle_hash
                           AND rights.child_dataset_hash = child_hash
                           AND rights.interval = child_interval
                           AND rights.provider = bundle_row.rights_provider
                           AND rights.purpose = 'BACKTEST_RESEARCH'
                           AND rights.policy_fingerprint = bundle_row.policy_fingerprint
                   ) THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CHILD_MANIFEST_MISMATCH';
                END IF;
                IF bundle_row.rights_provider = 'BINANCE_USDM_REST' THEN
                    market_source := 'BINANCE_USDM_REST_BACKFILL';
                ELSIF bundle_row.rights_provider = 'CRYPIDIAN_FIRST_PARTY_QA' THEN
                    market_source := 'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE';
                ELSE
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_PROVIDER_DENIED';
                END IF;
                SELECT pg_catalog.count(DISTINCT bar.id)::integer,
                       pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                           public.wp06_license_canonical_json(
                               pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                               'workspace_id', bar.workspace_id::text,
                               'record_id', bar.id::text,
                               'logical_key', bar.logical_key,
                               'revision', bar.revision,
                               'supersedes_record_id', bar.supersedes_id::text,
                               'checksum', bar.checksum,
                               'source', bar.source,
                               'symbol', bar.symbol,
                               'timeframe', bar.timeframe,
                               'open_time', public.wp06_canonical_utc_timestamp_json(bar.open_time)
                               ) ORDER BY bar.open_time, bar.source, bar.symbol, bar.timeframe)
                           ), 'UTF8')), 'hex')
                  INTO identity_count, identity_hash
                  FROM (
                    SELECT DISTINCT ON (candidate.logical_key)
                           candidate.workspace_id, candidate.id, candidate.logical_key,
                           candidate.revision, candidate.supersedes_id, candidate.checksum,
                           candidate.source, candidate.symbol, candidate.timeframe,
                           candidate.open_time
                      FROM public.market_bar AS candidate
                     WHERE candidate.workspace_id = target_workspace
                       AND candidate.source = market_source
                       AND candidate.symbol = child_row.symbol
                       AND candidate.timeframe = child_row.interval
                       AND candidate.open_time >= child_row.warmup_start
                       AND candidate.open_time < child_row.range_end
                       AND candidate.available_at <= child_row.prepared_at
                       AND candidate.is_closed
                     ORDER BY candidate.logical_key, candidate.revision DESC
                  ) AS bar;
                IF identity_count < 1
                   OR identity_count <> (child_row.manifest_json->>'bar_count')::integer
                   OR identity_count <> (child_row.manifest_json->>'expected_bar_count')::integer
                   OR identity_hash IS DISTINCT FROM child_row.manifest_json->>'bar_identity_hash' THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MARKET_BAR_SET_MISMATCH';
                END IF;

                source_id := bundle_row.rights_provider;
                provider := bundle_row.rights_provider;
                child_symbol := child_row.symbol;
                data_start := child_row.warmup_start;
                data_end := child_row.range_end;
                SELECT policy.workspace_id, policy.source_id, policy.sequence, policy.policy_id,
                       policy.state, policy.allowed_purposes, policy.valid_until, policy.reviewed_at,
                       policy.actor_id, policy.approved_by, policy.provider, policy.product,
                       policy.venue, policy.contract, policy.license_scope,
                       policy.license_evidence_id, policy.license_digest, policy.license_scope_hash,
                       policy.evidence_ref
                  INTO policy_row
                  FROM public.source_policy_revision AS policy
                 WHERE policy.workspace_id = target_workspace AND policy.source_id = source_id
                 ORDER BY policy.sequence DESC LIMIT 1;
                IF NOT FOUND OR policy_row.state <> 'verified'
                   OR policy_row.provider <> provider OR policy_row.product IS NULL
                   OR policy_row.venue IS NULL OR policy_row.contract IS NULL
                   OR policy_row.evidence_ref NOT LIKE 'https://%'
                   OR pg_catalog.split_part(
                        pg_catalog.split_part(policy_row.evidence_ref, '/', 3), '@', 1)
                        IS DISTINCT FROM pg_catalog.split_part(policy_row.evidence_ref, '/', 3)
                   OR pg_catalog.strpos(policy_row.evidence_ref, '#') > 0
                   OR policy_row.license_scope IS NULL OR policy_row.license_evidence_id IS NULL
                   OR policy_row.license_digest IS NULL OR policy_row.license_scope_hash IS NULL
                   OR policy_row.reviewed_at IS NULL OR policy_row.reviewed_at > checked_at
                   OR policy_row.approved_by IS NULL OR policy_row.approved_by = policy_row.actor_id
                   OR (policy_row.valid_until IS NOT NULL AND policy_row.valid_until <= checked_at)
                   OR NOT policy_row.allowed_purposes @> '["training"]'::jsonb
                   OR NOT EXISTS (
                        SELECT 1 FROM public.source_policy_observation AS observation
                         WHERE observation.workspace_id = target_workspace
                           AND observation.source_id = source_id
                           AND observation.sequence = policy_row.sequence
                           AND observation.policy_id = policy_row.policy_id
                           AND observation.known_at <= checked_at
                   ) OR EXISTS (
                        SELECT 1 FROM public.outbox_event AS event
                         WHERE event.workspace_id = target_workspace
                           AND event.event_type = 'research.rights_changed.v1'
                           AND event.payload->>'source_id' = source_id
                           AND NOT EXISTS (
                                SELECT 1 FROM public.consumer_receipt AS receipt
                                 WHERE receipt.workspace_id = event.workspace_id
                                   AND receipt.outbox_event_id = event.id
                                   AND receipt.consumer_name = 'wp06.dataset-rights-eligibility.v1'
                           )
                   ) THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CURRENT_POLICY_DENIED';
                END IF;
                SELECT evidence.workspace_id, evidence.id, evidence.source_id,
                       evidence.policy_id, evidence.evidence_ref, evidence.digest, evidence.license_scope,
                       evidence.canonical_scope, evidence.scope_hash
                  INTO evidence_row
                  FROM public.source_license_evidence AS evidence
                 WHERE evidence.workspace_id = target_workspace
                   AND evidence.source_id = source_id
                   AND evidence.id = policy_row.license_evidence_id
                   AND EXISTS (
                        SELECT 1 FROM public.source_policy_revision AS pending
                         WHERE pending.workspace_id = target_workspace
                           AND pending.source_id = source_id
                           AND pending.policy_id = evidence.policy_id
                           AND pending.state = 'rights_pending'
                           AND pending.evidence_ref = evidence.evidence_ref
                           AND pending.license_scope = evidence.license_scope
                   );
                IF NOT FOUND OR evidence_row.digest IS DISTINCT FROM policy_row.license_digest
                   OR evidence_row.evidence_ref IS DISTINCT FROM policy_row.evidence_ref
                   OR evidence_row.scope_hash IS DISTINCT FROM policy_row.license_scope_hash
                   OR evidence_row.license_scope IS DISTINCT FROM policy_row.license_scope
                   OR evidence_row.scope_hash IS DISTINCT FROM encode(pg_catalog.sha256(
                        pg_catalog.convert_to(evidence_row.canonical_scope, 'UTF8')), 'hex')
                   OR evidence_row.canonical_scope IS DISTINCT FROM
                        public.wp06_license_canonical_json(evidence_row.license_scope)
                   OR NOT public.wp06_license_scope_valid(evidence_row.license_scope) THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_EVIDENCE_DENIED';
                END IF;
                SELECT public.wp06_license_source_policy_fingerprint(policy)
                  INTO current_fingerprint
                  FROM public.source_policy_revision AS policy
                 WHERE policy.workspace_id = target_workspace
                   AND policy.source_id = source_id
                   AND policy.sequence = policy_row.sequence;
                IF current_fingerprint IS NULL OR length(current_fingerprint) <> 64 THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_POLICY_FINGERPRINT_INVALID';
                END IF;
                use_scope := pg_catalog.json_build_object(
                    'source_id', source_id, 'provider', provider,
                    'product', policy_row.product, 'venue', policy_row.venue,
                    'contract', policy_row.contract,
                    'symbols', pg_catalog.json_build_array(child_symbol),
                    'intervals', pg_catalog.json_build_array(child_interval),
                    'data_start', public.wp06_canonical_utc_timestamp_json(data_start),
                    'data_end', public.wp06_canonical_utc_timestamp_json(data_end),
                    'purpose', 'training', 'outputs', use_outputs,
                    'derived_use', use_derived, 'retention_days', NULL
                );
                use_scope_hash := public.wp06_sha256_canonical_json(use_scope);
                IF policy_row.license_scope->>'source_id' IS DISTINCT FROM source_id
                   OR policy_row.license_scope->>'provider' IS DISTINCT FROM provider
                   OR policy_row.license_scope->>'product' IS DISTINCT FROM policy_row.product
                   OR policy_row.license_scope->>'venue' IS DISTINCT FROM policy_row.venue
                   OR policy_row.license_scope->>'contract' IS DISTINCT FROM policy_row.contract
                   OR NOT (policy_row.license_scope->'symbols' @> pg_catalog.jsonb_build_array(child_symbol))
                   OR NOT (policy_row.license_scope->'intervals' @> pg_catalog.jsonb_build_array(child_interval))
                   OR (policy_row.license_scope->>'data_start')::timestamptz > data_start
                   OR (policy_row.license_scope->>'data_end')::timestamptz < data_end
                   OR NOT (policy_row.license_scope->'allowed_purposes' @> '["training"]'::jsonb)
                   OR NOT (policy_row.license_scope->'allowed_outputs' @> use_outputs)
                   OR NOT (policy_row.license_scope->'derived_use' @> use_derived)
                   OR policy_row.license_scope->'max_retention_days' <> 'null'::jsonb THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_SCOPE_NOT_COVERED';
                END IF;
                RETURN QUERY SELECT target_workspace, target_bundle_hash, child_hash,
                    child_interval, child_symbol, component->>'bars_content_hash',
                    policy_row.policy_id::text, current_fingerprint,
                    bundle_row.policy_fingerprint::text,
                    policy_row.evidence_ref::text, policy_row.reviewed_at, checked_at,
                    LEAST(checked_at + interval '5 minutes',
                        COALESCE(policy_row.valid_until, checked_at + interval '5 minutes')),
                    'training'::text, provider, policy_row.license_evidence_id,
                    policy_row.license_scope_hash::text, current_fingerprint,
                    policy_row.license_digest::text, use_scope_hash, data_start, data_end,
                    policy_row.product::text, policy_row.venue::text,
                    policy_row.contract::text;
            END LOOP;
            RETURN;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_authorize_replay_training_bundle(uuid,text,text) "
        "OWNER TO crypidian_learning_source_rights_reader"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_authorize_replay_training_bundle(uuid,text,text) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_authorize_replay_training_bundle(uuid,text,text) "
        "TO crypidian_candidate_builder, crypidian_learning_evaluator"
    )


def _create_scope_check() -> None:
    """Tạo helper sẽ kiểm lineage đã seal và quyền hiện hành dưới source lock."""
    op.execute("""
        CREATE FUNCTION public.wp06_check_replay_training_scope(
            target_workspace uuid,
            target_manifest_hash text,
            target_manifest jsonb,
            target_normalized_dataset_id uuid,
            target_fold_plan_hash text,
            target_access_kind text,
            target_access_id uuid,
            persist_receipts boolean
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        #variable_conflict use_variable
        DECLARE
            stored_manifest jsonb;
            stored_canonical text;
            sample_entry jsonb;
            lineage_entry jsonb;
            component_entry jsonb;
            source_key text;
            sample_id text;
            outcome_id text;
            outcome_hash text;
            bundle_hash text;
            source_bundle_hash text;
            source_lineage_set_hash text;
            child_hash text;
            source_id text;
            provider text;
            symbol text;
            interval_value text;
            data_start timestamptz;
            data_end timestamptz;
            bar_identity_hash text;
            policy_fingerprint text;
            run_id uuid;
            trial_id uuid;
            run_manifest_hash text;
            manifest_id uuid;
            bundle_symbol text;
            bundle_provider text;
            bundle_workspace_id uuid;
            bundle_schema_version integer;
            bundle_evaluation_start timestamptz;
            bundle_range_end timestamptz;
            bundle_purpose text;
            bundle_fingerprint text;
            bundle_components jsonb;
            bundle_json jsonb;
            bundle_canonical text;
            current_policy record;
            current_evidence record;
            child_manifest record;
            outcome_record record;
            run_record record;
            trial_record record;
            mapped_market_source text;
            identity_members json;
            identity_count integer;
            supplied_count integer;
            component_count integer;
            rights_count integer;
            source_sample_count integer := 0;
            expected_receipt_count integer := 0;
            stored_receipt_count integer := 0;
            checked_at timestamptz;
            use_outputs jsonb;
            use_derived jsonb;
            use_scope json;
            use_scope_hash text;
            current_policy_fingerprint text;
            evidence_digest text;
            license_scope_hash text;
            license_evidence_id uuid;
            bar_uuid uuid;
            exact_bar_ids uuid[];
            supplied_bar_ids uuid[];
            current_policy_sequence integer;
            current_policy_id text;
        BEGIN
            IF (target_access_kind = 'normalization' AND session_user <> 'crypidian_candidate_builder')
               OR (target_access_kind = 'evaluation' AND session_user <> 'crypidian_learning_evaluator')
               OR (target_access_kind = 'training' AND session_user <> 'crypidian_trainer_worker')
               OR target_access_kind NOT IN ('normalization', 'evaluation', 'training') THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_WORKSPACE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_READ_COMMITTED_REQUIRED';
            END IF;
            IF target_normalized_dataset_id IS NULL OR target_access_id IS NULL
               OR target_manifest_hash IS NULL OR length(target_manifest_hash) <> 64
               OR target_manifest_hash !~ '^[0-9a-f]+$'
               OR target_fold_plan_hash IS NULL OR length(target_fold_plan_hash) <> 64
               OR target_fold_plan_hash !~ '^[0-9a-f]+$' THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_SCOPE_INVALID';
            END IF;

            SELECT record.id, record.manifest, record.canonical_manifest
              INTO manifest_id, stored_manifest, stored_canonical
              FROM public.dataset_manifest AS record
             WHERE record.workspace_id = target_workspace
               AND record.manifest_hash = target_manifest_hash;
            IF NOT FOUND OR stored_manifest IS DISTINCT FROM target_manifest
               OR stored_manifest->>'workspace_id' IS DISTINCT FROM target_workspace::text
               OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(stored_canonical, 'UTF8')), 'hex')
                    IS DISTINCT FROM target_manifest_hash
               OR public.wp06_compact_canonical_json(stored_canonical::json) IS DISTINCT FROM stored_canonical
               OR stored_manifest IS DISTINCT FROM stored_canonical::jsonb
               OR pg_catalog.jsonb_typeof(stored_manifest->'samples') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MANIFEST_CANONICAL_MISMATCH';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.learning_normalized_dataset AS dataset
                 WHERE dataset.workspace_id = target_workspace
                   AND dataset.id = target_normalized_dataset_id
                   AND dataset.manifest_hash = target_manifest_hash
                   AND dataset.fold_plan_hash = target_fold_plan_hash
            ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_NORMALIZED_DATASET_MISMATCH';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM pg_catalog.jsonb_array_elements(stored_manifest->'samples') AS sample(value)
                 WHERE COALESCE((sample.value->>'source_dependent')::boolean, false)
            ) THEN
                IF stored_manifest->>'manifest_version' <> 'dataset-manifest-v5'
                   OR pg_catalog.jsonb_typeof(stored_manifest->'source_use_lineage')
                        IS DISTINCT FROM 'array'
                   OR pg_catalog.jsonb_array_length(stored_manifest->'source_use_lineage') = 0 THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_LEGACY_LINEAGE_DENIED';
                END IF;
                source_lineage_set_hash := public.wp06_sha256_canonical_json(
                    (stored_canonical::json)->'source_use_lineage'
                );
            ELSIF stored_manifest->>'manifest_version' = 'dataset-manifest-v5'
               AND (
                    pg_catalog.jsonb_typeof(stored_manifest->'source_use_lineage')
                        IS DISTINCT FROM 'array'
                    OR pg_catalog.jsonb_array_length(stored_manifest->'source_use_lineage') <> 0
               ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_LINEAGE_SET_HASH_MISMATCH';
            END IF;

            FOR sample_entry IN
                SELECT sample.value
                  FROM pg_catalog.jsonb_array_elements(stored_manifest->'samples') AS sample(value)
            LOOP
                sample_id := sample_entry->>'sample_id';
                outcome_id := sample_entry->>'outcome_id';
                outcome_hash := sample_entry->>'outcome_hash';
                IF pg_catalog.jsonb_typeof(sample_entry) IS DISTINCT FROM 'object'
                   OR sample_id IS NULL OR length(sample_id) NOT BETWEEN 1 AND 128 THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_SAMPLE_INVALID';
                END IF;

                IF NOT COALESCE((sample_entry->>'source_dependent')::boolean, false) THEN
                    IF pg_catalog.jsonb_array_length(
                            COALESCE(sample_entry->'source_references', '[]'::jsonb)) <> 0
                       OR pg_catalog.jsonb_array_length(
                            COALESCE(sample_entry->'source_rights_hashes', '[]'::jsonb)) <> 0
                       OR pg_catalog.jsonb_array_length(
                            COALESCE(sample_entry->'source_revision_refs', '[]'::jsonb)) <> 0
                       OR pg_catalog.jsonb_array_length(
                            COALESCE(sample_entry->'source_use_lineage', '[]'::jsonb)) <> 0
                       OR sample_entry->>'source_replay_bundle_hash' IS NOT NULL
                       OR sample_entry->>'source_use_lineage_set_hash' IS NOT NULL
                       OR EXISTS (
                            SELECT 1 FROM public.backtest_trade_outcome AS outcome
                             WHERE outcome.workspace_id = target_workspace
                               AND outcome.id::text = outcome_id
                       ) THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_SOURCE_FREE_FORGED';
                    END IF;
                    CONTINUE;
                END IF;

                IF stored_manifest->>'manifest_version' <> 'dataset-manifest-v5'
                   OR sample_entry->>'source_replay_bundle_hash' IS NULL
                   OR length(sample_entry->>'source_replay_bundle_hash') <> 64
                   OR sample_entry->>'source_replay_bundle_hash' !~ '^[0-9a-f]+$'
                   OR sample_entry->>'source_use_lineage_set_hash'
                        IS DISTINCT FROM source_lineage_set_hash
                   OR length(sample_entry->>'source_use_lineage_set_hash') <> 64
                   OR sample_entry->>'source_use_lineage_set_hash' !~ '^[0-9a-f]+$'
                   OR pg_catalog.jsonb_array_length(
                        COALESCE(sample_entry->'source_use_lineage', '[]'::jsonb)) <> 0
                   OR pg_catalog.jsonb_array_length(
                        COALESCE(sample_entry->'source_references', '[]'::jsonb)) <> 0
                   OR pg_catalog.jsonb_array_length(
                        COALESCE(sample_entry->'source_rights_hashes', '[]'::jsonb)) <> 0
                   OR pg_catalog.jsonb_array_length(
                        COALESCE(sample_entry->'source_revision_refs', '[]'::jsonb)) <> 0 THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_LINEAGE_INVALID';
                END IF;
                bundle_hash := sample_entry->>'source_replay_bundle_hash';
                IF source_bundle_hash IS NULL THEN
                    source_bundle_hash := bundle_hash;
                ELSIF source_bundle_hash IS DISTINCT FROM bundle_hash THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MIXED_BUNDLE';
                END IF;
                source_sample_count := source_sample_count + 1;

                SELECT outcome.workspace_id, outcome.id, outcome.run_id,
                       outcome.trial_record_id, outcome.manifest_hash, outcome.outcome_hash
                  INTO outcome_record
                  FROM public.backtest_trade_outcome AS outcome
                 WHERE outcome.workspace_id = target_workspace
                   AND outcome.id::text = outcome_id
                   AND outcome.outcome_hash = outcome_hash;
                IF NOT FOUND OR outcome_record.workspace_id IS DISTINCT FROM target_workspace THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_OUTCOME_MISSING';
                END IF;
                SELECT trial.workspace_id, trial.id, trial.run_id, trial.manifest_hash
                  INTO trial_record
                  FROM public.backtest_trial AS trial
                 WHERE trial.workspace_id = target_workspace
                   AND trial.id = outcome_record.trial_record_id
                   AND trial.run_id = outcome_record.run_id
                   AND trial.manifest_hash = outcome_record.manifest_hash;
                IF NOT FOUND THEN RAISE EXCEPTION 'WP06_TRAINING_REPLAY_TRIAL_MISMATCH'; END IF;
                SELECT run.workspace_id, run.id, run.manifest_hash, run.manifest,
                       run.canonical_manifest
                  INTO run_record
                  FROM public.backtest_run AS run
                 WHERE run.workspace_id = target_workspace
                   AND run.id = trial_record.run_id
                   AND run.manifest_hash = trial_record.manifest_hash;
                IF NOT FOUND
                   OR run_record.manifest IS DISTINCT FROM run_record.canonical_manifest::jsonb
                   OR pg_catalog.encode(pg_catalog.sha256(
                        pg_catalog.convert_to(run_record.canonical_manifest, 'UTF8')), 'hex')
                        IS DISTINCT FROM run_record.manifest_hash
                   OR run_record.manifest->>'dataset_hash' IS DISTINCT FROM bundle_hash
                   OR NOT EXISTS (
                        SELECT 1 FROM public.learning_split_candidate AS candidate
                         WHERE candidate.workspace_id = target_workspace
                           AND candidate.manifest_hash = target_manifest_hash
                           AND candidate.trial_record_id = trial_record.id
                   ) THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_RUN_LINEAGE_MISMATCH';
                END IF;
                IF run_id IS NULL THEN
                    run_id := run_record.id;
                    trial_id := trial_record.id;
                    run_manifest_hash := run_record.manifest_hash;
                ELSIF run_id IS DISTINCT FROM run_record.id
                   OR trial_id IS DISTINCT FROM trial_record.id
                   OR run_manifest_hash IS DISTINCT FROM run_record.manifest_hash THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MIXED_RUN';
                END IF;

                IF source_sample_count = 1 THEN
                bundle_hash := source_bundle_hash;
                SELECT bundle.workspace_id, bundle.bundle_hash, bundle.schema_version,
                       bundle.symbol, bundle.evaluation_start, bundle.range_end,
                       bundle.rights_provider, bundle.rights_purpose, bundle.policy_fingerprint,
                       bundle.bundle_json->'components', bundle.bundle_json, bundle.canonical_bundle
                  INTO bundle_workspace_id, bundle_hash,
                       bundle_schema_version, bundle_symbol, bundle_evaluation_start,
                       bundle_range_end, bundle_provider, bundle_purpose,
                       bundle_fingerprint, bundle_components, bundle_json, bundle_canonical
                  FROM public.backtest_dataset_replay_bundle AS bundle
                 WHERE bundle.workspace_id = target_workspace
                   AND bundle.bundle_hash = bundle_hash;
                IF NOT FOUND
                   OR bundle_workspace_id IS DISTINCT FROM target_workspace
                   OR bundle_hash IS DISTINCT FROM sample_entry->>'source_replay_bundle_hash'
                   OR bundle_schema_version <> 1
                   OR bundle_purpose <> 'BACKTEST_RESEARCH'
                   OR pg_catalog.encode(pg_catalog.sha256(
                        pg_catalog.convert_to(bundle_canonical, 'UTF8')), 'hex')
                        IS DISTINCT FROM bundle_hash
                   OR bundle_json IS DISTINCT FROM bundle_canonical::jsonb
                   OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(bundle_json)) <> 9
                   OR bundle_json->>'schema_version' <> bundle_schema_version::text
                   OR bundle_json->>'workspace_id' <> target_workspace::text
                   OR bundle_json->>'symbol' <> bundle_symbol
                   OR (bundle_json->>'evaluation_start')::timestamptz
                        IS DISTINCT FROM bundle_evaluation_start
                   OR (bundle_json->>'range_end')::timestamptz
                        IS DISTINCT FROM bundle_range_end
                   OR bundle_json->>'rights_provider' <> bundle_provider
                   OR bundle_json->>'rights_purpose' <> bundle_purpose
                   OR bundle_json->>'policy_fingerprint' <> bundle_fingerprint THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_BUNDLE_CANONICAL_MISMATCH';
                END IF;

                SELECT count(*)::integer INTO component_count
                  FROM pg_catalog.jsonb_array_elements(bundle_components) AS component(value);
                IF component_count < 1
                   OR pg_catalog.jsonb_array_length(stored_manifest->'source_use_lineage') <> component_count
                   OR component_count <> (
                        SELECT count(DISTINCT component.value->>'dataset_hash')
                          FROM pg_catalog.jsonb_array_elements(bundle_components) AS component(value)
                   )
                   OR EXISTS (
                        SELECT 1
                          FROM pg_catalog.jsonb_array_elements(bundle_components) AS component(value)
                         WHERE NOT EXISTS (
                            SELECT 1
                              FROM pg_catalog.jsonb_array_elements(
                                  stored_manifest->'source_use_lineage') AS lineage(value)
                             WHERE lineage.value->>'child_dataset_hash' = component.value->>'dataset_hash'
                               AND lineage.value->>'interval' = component.value->>'interval'
                         )
                   ) THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CHILD_COUNT_MISMATCH';
                END IF;
                SELECT count(*)::integer INTO rights_count
                  FROM public.backtest_run_dataset_rights AS receipt
                 WHERE receipt.workspace_id = target_workspace
                   AND receipt.run_id = run_record.id;
                IF component_count < 1 OR rights_count <> component_count
                   OR EXISTS (
                        SELECT 1
                          FROM pg_catalog.jsonb_array_elements(bundle_components) AS component(value)
                         WHERE NOT EXISTS (
                            SELECT 1 FROM public.backtest_run_dataset_rights AS receipt
                             WHERE receipt.workspace_id = target_workspace
                               AND receipt.run_id = run_record.id
                               AND receipt.bundle_hash = bundle_hash
                               AND receipt.child_dataset_hash = component.value->>'dataset_hash'
                               AND receipt.interval = component.value->>'interval'
                               AND receipt.provider = bundle_provider
                               AND receipt.purpose = 'BACKTEST_RESEARCH'
                               AND receipt.policy_fingerprint = bundle_fingerprint
                               AND receipt.evidence_json IS NOT DISTINCT FROM receipt.canonical_evidence::jsonb
                               AND (SELECT count(*) FROM pg_catalog.jsonb_object_keys(
                                    receipt.evidence_json)) = 13
                               AND receipt.canonical_evidence = public.wp06_compact_canonical_json(
                                    receipt.canonical_evidence::json)
                               AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                                    receipt.canonical_evidence, 'UTF8')), 'hex') = receipt.evidence_hash
                               AND receipt.evidence_json->>'schema_version' = '1'
                               AND receipt.evidence_json->>'workspace_id' = target_workspace::text
                               AND receipt.evidence_json->>'run_id' = run_record.id::text
                               AND receipt.evidence_json->>'bundle_hash' = bundle_hash
                               AND receipt.evidence_json->>'child_dataset_hash' = component.value->>'dataset_hash'
                               AND receipt.evidence_json->>'interval' = component.value->>'interval'
                               AND receipt.evidence_json->>'provider' = bundle_provider
                               AND receipt.evidence_json->>'purpose' = 'BACKTEST_RESEARCH'
                               AND receipt.evidence_json->>'terms_url' = receipt.terms_url
                               AND receipt.evidence_json->>'policy_fingerprint' = bundle_fingerprint
                               AND (receipt.evidence_json->>'verified_at')::timestamptz = receipt.verified_at
                               AND (receipt.evidence_json->>'checked_at')::timestamptz = receipt.checked_at
                               AND (receipt.evidence_json->>'valid_until')::timestamptz = receipt.valid_until
                               AND receipt.verified_at <= receipt.checked_at
                               AND receipt.checked_at < receipt.valid_until
                         )
                   ) THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_RIGHTS_CHILD_SET_MISMATCH';
                END IF;

                FOR lineage_entry IN
                    SELECT lineage.value
                      FROM pg_catalog.jsonb_array_elements(
                          stored_manifest->'source_use_lineage') AS lineage(value)
                LOOP
                    IF pg_catalog.jsonb_typeof(lineage_entry) IS DISTINCT FROM 'object'
                       OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(lineage_entry)) <> 11
                       OR lineage_entry->>'bundle_hash' IS DISTINCT FROM bundle_hash
                       OR lineage_entry->>'source_id' IS DISTINCT FROM bundle_provider
                       OR lineage_entry->>'provider' IS DISTINCT FROM bundle_provider
                       OR lineage_entry->>'policy_fingerprint' IS DISTINCT FROM bundle_fingerprint
                       OR length(lineage_entry->>'child_dataset_hash') <> 64
                       OR length(lineage_entry->>'bar_identity_hash') <> 64
                       OR pg_catalog.jsonb_typeof(lineage_entry->'market_bar_ids') IS DISTINCT FROM 'array'
                       OR pg_catalog.jsonb_array_length(lineage_entry->'market_bar_ids') = 0 THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_LINEAGE_INVALID';
                    END IF;
                    child_hash := lineage_entry->>'child_dataset_hash';
                    source_id := lineage_entry->>'source_id';
                    provider := lineage_entry->>'provider';
                    symbol := lineage_entry->>'symbol';
                    interval_value := lineage_entry->>'interval';
                    bar_identity_hash := lineage_entry->>'bar_identity_hash';
                    policy_fingerprint := lineage_entry->>'policy_fingerprint';
                    data_start := (lineage_entry->>'data_start')::timestamptz;
                    data_end := (lineage_entry->>'data_end')::timestamptz;
                    IF source_id <> provider OR symbol <> bundle_symbol
                       OR data_start >= data_end
                       OR EXISTS (
                            SELECT 1 FROM pg_catalog.jsonb_array_elements(bundle_components) AS component(value)
                             WHERE component.value->>'dataset_hash' = child_hash
                               AND component.value->>'interval' = interval_value
                       ) IS NOT TRUE
                       OR (SELECT count(*) FROM pg_catalog.jsonb_array_elements(
                            stored_manifest->'source_use_lineage') AS item(value)
                            WHERE item.value->>'child_dataset_hash' = child_hash) <> 1 THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_LINEAGE_CHILD_MISMATCH';
                    END IF;
                    SELECT child.workspace_id, child.dataset_hash, child.status, child.symbol,
                           child.interval, child.warmup_start, child.evaluation_start,
                           child.range_end, child.prepared_at,
                           child.policy_fingerprint, child.manifest_json, child.canonical_manifest
                      INTO child_manifest
                      FROM public.backtest_dataset_manifest AS child
                     WHERE child.workspace_id = target_workspace
                       AND child.dataset_hash = child_hash;
                    IF NOT FOUND OR child_manifest.status <> 'READY'
                       OR child_manifest.symbol <> symbol OR child_manifest.interval <> interval_value
                       OR child_manifest.warmup_start <> data_start OR child_manifest.range_end <> data_end
                       OR child_manifest.policy_fingerprint <> policy_fingerprint
                       OR child_manifest.manifest_json IS DISTINCT FROM child_manifest.canonical_manifest::jsonb
                       OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(
                            child_manifest.manifest_json)) <> 28
                       OR child_manifest.manifest_json->>'schema_version' <> '4'
                       OR child_manifest.manifest_json->>'dataset_hash' <> child_hash
                       OR child_manifest.manifest_json->>'status' <> child_manifest.status
                       OR child_manifest.manifest_json->>'symbol' <> child_manifest.symbol
                       OR child_manifest.manifest_json->>'interval' <> child_manifest.interval
                       OR (child_manifest.manifest_json->>'warmup_start')::timestamptz
                            IS DISTINCT FROM child_manifest.warmup_start
                       OR (child_manifest.manifest_json->>'evaluation_start')::timestamptz
                            IS DISTINCT FROM child_manifest.evaluation_start
                       OR child_manifest.evaluation_start IS DISTINCT FROM bundle_evaluation_start
                       OR (child_manifest.manifest_json->>'range_end')::timestamptz
                            IS DISTINCT FROM child_manifest.range_end
                       OR child_manifest.range_end IS DISTINCT FROM bundle_range_end
                       OR (child_manifest.manifest_json->>'prepared_at')::timestamptz
                            IS DISTINCT FROM child_manifest.prepared_at
                       OR child_manifest.manifest_json->>'policy_fingerprint'
                            IS DISTINCT FROM child_manifest.policy_fingerprint
                       OR child_manifest.manifest_json->>'bar_count'
                            IS DISTINCT FROM child_manifest.manifest_json->>'expected_bar_count'
                       OR child_manifest.manifest_json->>'rights_workspace_id' IS DISTINCT FROM target_workspace::text
                       OR child_manifest.manifest_json->>'purpose' IS DISTINCT FROM 'BACKTEST_RESEARCH'
                       OR child_manifest.manifest_json->>'bar_identity_hash' <> bar_identity_hash
                       OR child_manifest.manifest_json->>'provider' <> bundle_provider
                       OR NOT EXISTS (
                            SELECT 1
                              FROM public.backtest_run_dataset_rights AS rights
                             WHERE rights.workspace_id = target_workspace
                               AND rights.run_id = run_record.id
                               AND rights.bundle_hash = bundle_hash
                               AND rights.child_dataset_hash = child_hash
                               AND rights.interval = interval_value
                               AND rights.provider = bundle_provider
                               AND rights.purpose = 'BACKTEST_RESEARCH'
                               AND rights.policy_fingerprint = bundle_fingerprint
                               AND rights.terms_url = child_manifest.manifest_json->>'terms_url'
                               AND rights.verified_at =
                                    (child_manifest.manifest_json->>'rights_verified_at')::timestamptz
                       )
                       OR NOT EXISTS (
                            SELECT 1
                              FROM pg_catalog.jsonb_array_elements(bundle_components) AS component(value)
                             WHERE component.value->>'dataset_hash' = child_hash
                               AND component.value->>'interval' = interval_value
                               AND component.value->>'bars_content_hash' =
                                    child_manifest.manifest_json->>'bars_content_hash'
                       )
                       OR pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                            public.wp06_license_canonical_json(
                                child_manifest.manifest_json - 'dataset_hash'
                            ), 'UTF8')), 'hex')
                            IS DISTINCT FROM child_hash THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CHILD_MANIFEST_MISMATCH';
                    END IF;

                    IF bundle_provider = 'BINANCE_USDM_REST' THEN
                        mapped_market_source := 'BINANCE_USDM_REST_BACKFILL';
                    ELSIF bundle_provider = 'CRYPIDIAN_FIRST_PARTY_QA' THEN
                        mapped_market_source := 'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE';
                    ELSE
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_PROVIDER_DENIED';
                    END IF;
                    IF pg_catalog.jsonb_typeof(lineage_entry->'market_bar_ids') <> 'array'
                       OR EXISTS (
                            SELECT 1 FROM pg_catalog.jsonb_array_elements_text(
                                lineage_entry->'market_bar_ids') AS ids(value)
                             WHERE value !~ '^market-bar-record:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                       ) THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MARKET_BAR_ID_INVALID';
                    END IF;
                    SELECT array_agg(substring(ids.value FROM 19)::uuid ORDER BY ids.ordinality)
                      INTO supplied_bar_ids
                      FROM pg_catalog.jsonb_array_elements_text(
                          lineage_entry->'market_bar_ids') WITH ORDINALITY AS ids(value, ordinality);
                    SELECT pg_catalog.count(DISTINCT bar.id)::integer,
                           array_agg(bar.id ORDER BY bar.open_time, bar.source, bar.symbol, bar.timeframe),
                           pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                               public.wp06_license_canonical_json(
                                   pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                   'workspace_id', bar.workspace_id::text,
                                   'record_id', bar.id::text,
                                   'logical_key', bar.logical_key,
                                   'revision', bar.revision,
                                   'supersedes_record_id', bar.supersedes_id::text,
                                   'checksum', bar.checksum,
                                   'source', bar.source,
                                   'symbol', bar.symbol,
                                   'timeframe', bar.timeframe,
                                   'open_time', public.wp06_canonical_utc_timestamp_json(bar.open_time)
                                   ) ORDER BY bar.open_time, bar.source, bar.symbol, bar.timeframe)
                               ), 'UTF8')), 'hex')
                      INTO identity_count, exact_bar_ids, use_scope_hash
                      FROM public.market_bar AS bar
                     WHERE bar.workspace_id = target_workspace
                       AND bar.id = ANY(supplied_bar_ids)
                       AND bar.source = mapped_market_source
                       AND bar.symbol = symbol
                       AND bar.timeframe = interval_value
                       AND bar.open_time >= data_start AND bar.open_time < data_end
                       AND bar.available_at <= child_manifest.prepared_at
                       AND bar.is_closed
                       AND bar.revision = (
                            SELECT max(candidate.revision)
                              FROM public.market_bar AS candidate
                             WHERE candidate.workspace_id = bar.workspace_id
                               AND candidate.source = bar.source
                               AND candidate.logical_key = bar.logical_key
                               AND candidate.available_at <= child_manifest.prepared_at
                               AND candidate.is_closed
                       );
                    SELECT pg_catalog.jsonb_array_length(lineage_entry->'market_bar_ids')
                      INTO supplied_count;
                    IF identity_count <> supplied_count
                       OR cardinality(exact_bar_ids) <> supplied_count
                       OR exact_bar_ids IS DISTINCT FROM supplied_bar_ids
                       OR identity_count <>
                            (child_manifest.manifest_json->>'bar_count')::integer
                       OR identity_count <>
                            (child_manifest.manifest_json->>'expected_bar_count')::integer
                       OR use_scope_hash IS DISTINCT FROM bar_identity_hash THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MARKET_BAR_SET_MISMATCH';
                    END IF;

                END LOOP;
                END IF;
            END LOOP;

            FOR source_key IN
                SELECT DISTINCT lineage.value->>'source_id'
                  FROM pg_catalog.jsonb_array_elements(
                      stored_manifest->'source_use_lineage') AS lineage(value)
                 ORDER BY 1
            LOOP
                IF source_key IS NULL OR source_key !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]*$' THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_SOURCE_ID_INVALID';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || source_key, 0));
            END LOOP;
            checked_at := pg_catalog.clock_timestamp();
            FOR sample_entry IN
                SELECT sample.value FROM pg_catalog.jsonb_array_elements(stored_manifest->'samples') AS sample(value)
                 WHERE COALESCE((sample.value->>'source_dependent')::boolean, false)
            LOOP
                sample_id := sample_entry->>'sample_id';
                bundle_hash := sample_entry->>'source_replay_bundle_hash';
                FOR lineage_entry IN
                    SELECT lineage.value FROM pg_catalog.jsonb_array_elements(
                        stored_manifest->'source_use_lineage') AS lineage(value)
                LOOP
                    source_id := lineage_entry->>'source_id';
                    provider := lineage_entry->>'provider';
                    symbol := lineage_entry->>'symbol';
                    interval_value := lineage_entry->>'interval';
                    child_hash := lineage_entry->>'child_dataset_hash';
                    bar_identity_hash := lineage_entry->>'bar_identity_hash';
                    data_start := (lineage_entry->>'data_start')::timestamptz;
                    data_end := (lineage_entry->>'data_end')::timestamptz;
                    SELECT policy.workspace_id, policy.source_id, policy.sequence, policy.policy_id,
                           policy.state, policy.allowed_purposes, policy.valid_until, policy.reviewed_at,
                           policy.actor_id, policy.approved_by, policy.provider, policy.product,
                           policy.venue, policy.contract, policy.evidence_ref, policy.license_scope,
                           policy.license_evidence_id, policy.license_digest, policy.license_scope_hash
                      INTO current_policy
                      FROM public.source_policy_revision AS policy
                     WHERE policy.workspace_id = target_workspace AND policy.source_id = source_id
                     ORDER BY policy.sequence DESC LIMIT 1;
                    IF NOT FOUND OR current_policy.state <> 'verified'
                       OR current_policy.source_id <> source_id OR current_policy.provider <> provider
                       OR current_policy.provider IS NULL OR current_policy.product IS NULL
                       OR current_policy.venue IS NULL OR current_policy.contract IS NULL
                       OR current_policy.license_scope IS NULL OR current_policy.license_evidence_id IS NULL
                       OR current_policy.license_digest IS NULL OR current_policy.license_scope_hash IS NULL
                       OR current_policy.reviewed_at IS NULL OR current_policy.reviewed_at > checked_at
                       OR current_policy.evidence_ref NOT LIKE 'https://%'
                       OR pg_catalog.split_part(
                            pg_catalog.split_part(current_policy.evidence_ref, '/', 3), '@', 1)
                            IS DISTINCT FROM pg_catalog.split_part(current_policy.evidence_ref, '/', 3)
                       OR pg_catalog.strpos(current_policy.evidence_ref, '#') > 0
                       OR current_policy.approved_by IS NULL
                       OR current_policy.approved_by = current_policy.actor_id
                       OR (current_policy.valid_until IS NOT NULL AND current_policy.valid_until <= checked_at)
                       OR NOT current_policy.allowed_purposes @> '["training"]'::jsonb
                       OR NOT EXISTS (
                            SELECT 1 FROM public.source_policy_observation AS observation
                             WHERE observation.workspace_id = target_workspace
                               AND observation.source_id = source_id
                               AND observation.sequence = current_policy.sequence
                               AND observation.policy_id = current_policy.policy_id
                               AND observation.known_at <= checked_at
                       ) OR EXISTS (
                            SELECT 1 FROM public.outbox_event AS event
                             WHERE event.workspace_id = target_workspace
                               AND event.event_type = 'research.rights_changed.v1'
                               AND event.payload->>'source_id' = source_id
                               AND NOT EXISTS (
                                    SELECT 1 FROM public.consumer_receipt AS receipt
                                     WHERE receipt.workspace_id = event.workspace_id
                                       AND receipt.outbox_event_id = event.id
                                       AND receipt.consumer_name = 'wp06.dataset-rights-eligibility.v1'
                               )
                       ) THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CURRENT_POLICY_DENIED';
                    END IF;
                    SELECT evidence.workspace_id, evidence.id, evidence.source_id,
                           evidence.policy_id, evidence.evidence_ref, evidence.digest, evidence.license_scope,
                           evidence.canonical_scope, evidence.scope_hash
                      INTO current_evidence
                      FROM public.source_license_evidence AS evidence
                     WHERE evidence.workspace_id = target_workspace
                       AND evidence.source_id = source_id
                       AND evidence.id = current_policy.license_evidence_id
                       AND EXISTS (
                            SELECT 1 FROM public.source_policy_revision AS pending
                             WHERE pending.workspace_id = target_workspace
                               AND pending.source_id = source_id
                               AND pending.policy_id = evidence.policy_id
                               AND pending.state = 'rights_pending'
                               AND pending.evidence_ref = evidence.evidence_ref
                               AND pending.license_scope = evidence.license_scope
                       );
                    IF NOT FOUND OR current_evidence.digest IS DISTINCT FROM current_policy.license_digest
                       OR current_evidence.evidence_ref IS DISTINCT FROM current_policy.evidence_ref
                       OR current_evidence.scope_hash IS DISTINCT FROM current_policy.license_scope_hash
                       OR current_evidence.license_scope IS DISTINCT FROM current_policy.license_scope
                       OR current_evidence.scope_hash IS DISTINCT FROM encode(pg_catalog.sha256(
                            pg_catalog.convert_to(current_evidence.canonical_scope, 'UTF8')), 'hex')
                       OR current_evidence.canonical_scope IS DISTINCT FROM
                            public.wp06_license_canonical_json(current_evidence.license_scope)
                       OR NOT public.wp06_license_scope_valid(current_evidence.license_scope) THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_EVIDENCE_DENIED';
                    END IF;
                    SELECT public.wp06_license_source_policy_fingerprint(policy)
                      INTO current_policy_fingerprint
                      FROM public.source_policy_revision AS policy
                     WHERE policy.workspace_id = target_workspace
                       AND policy.source_id = source_id
                       AND policy.sequence = current_policy.sequence;
                    IF current_policy_fingerprint IS NULL
                       OR length(current_policy_fingerprint) <> 64
                       OR current_policy_fingerprint !~ '^[0-9a-f]+$' THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_POLICY_FINGERPRINT_INVALID';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_catalog.jsonb_array_elements(
                            bundle_components) AS component(value)
                         WHERE component.value->>'dataset_hash' = child_hash
                           AND component.value->>'interval' = interval_value
                    ) THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_COMPONENT_MISSING';
                    END IF;
                    use_outputs := CASE WHEN target_access_kind = 'evaluation'
                        THEN '["aggregate_report"]'::jsonb
                        ELSE '["derived_features","trained_model"]'::jsonb END;
                    use_derived := CASE WHEN target_access_kind = 'evaluation'
                        THEN '["aggregate_reporting"]'::jsonb
                        ELSE '["feature_extraction","model_training"]'::jsonb END;
                    use_scope := pg_catalog.json_build_object(
                        'source_id', source_id, 'provider', provider,
                        'product', current_policy.product, 'venue', current_policy.venue,
                        'contract', current_policy.contract, 'symbols', pg_catalog.json_build_array(symbol),
                        'intervals', pg_catalog.json_build_array(interval_value),
                        'data_start', public.wp06_canonical_utc_timestamp_json(data_start),
                        'data_end', public.wp06_canonical_utc_timestamp_json(data_end),
                        'purpose', 'training', 'outputs', use_outputs,
                        'derived_use', use_derived, 'retention_days', NULL
                    );
                    use_scope_hash := public.wp06_sha256_canonical_json(use_scope);
                    IF current_policy.license_scope->>'source_id' IS DISTINCT FROM source_id
                       OR current_policy.license_scope->>'provider' IS DISTINCT FROM provider
                       OR current_policy.license_scope->>'product' IS DISTINCT FROM current_policy.product
                       OR current_policy.license_scope->>'venue' IS DISTINCT FROM current_policy.venue
                       OR current_policy.license_scope->>'contract' IS DISTINCT FROM current_policy.contract
                       OR NOT (current_policy.license_scope->'symbols' @> pg_catalog.jsonb_build_array(symbol))
                       OR NOT (current_policy.license_scope->'intervals' @> pg_catalog.jsonb_build_array(interval_value))
                       OR (current_policy.license_scope->>'data_start')::timestamptz > data_start
                       OR (current_policy.license_scope->>'data_end')::timestamptz < data_end
                       OR NOT (current_policy.license_scope->'allowed_purposes' @> '["training"]'::jsonb)
                       OR NOT (current_policy.license_scope->'allowed_outputs' @> use_outputs)
                       OR NOT (current_policy.license_scope->'derived_use' @> use_derived)
                       OR current_policy.license_scope->'max_retention_days' <> 'null'::jsonb THEN
                        RAISE EXCEPTION 'WP06_TRAINING_REPLAY_SCOPE_NOT_COVERED';
                    END IF;
                    license_evidence_id := current_policy.license_evidence_id;
                    evidence_digest := current_policy.license_digest;
                    license_scope_hash := current_policy.license_scope_hash;
                    current_policy_id := current_policy.policy_id;
                    current_policy_sequence := current_policy.sequence;
                    policy_fingerprint := (lineage_entry->>'policy_fingerprint');
                    expected_receipt_count := expected_receipt_count + 1;
                    IF persist_receipts THEN
                        INSERT INTO public.learning_replay_training_receipt(
                            id, workspace_id, access_kind, access_id, normalized_dataset_id,
                            manifest_hash, fold_plan_hash, sample_id, bundle_hash,
                            child_dataset_hash, source_id, provider, product, venue, contract,
                            symbol, interval, data_start, data_end, bar_identity_hash,
                            policy_fingerprint, current_policy_fingerprint, scope_hash,
                            policy_id, policy_sequence, license_evidence_id, evidence_digest,
                            license_scope_hash, purpose, checked_at
                        ) VALUES (
                            pg_catalog.gen_random_uuid(), target_workspace, target_access_kind,
                            target_access_id, target_normalized_dataset_id, target_manifest_hash,
                            target_fold_plan_hash, sample_id, bundle_hash, child_hash, source_id,
                            provider, current_policy.product, current_policy.venue,
                            current_policy.contract, symbol, interval_value, data_start, data_end,
                            bar_identity_hash, policy_fingerprint, current_policy_fingerprint,
                            use_scope_hash, current_policy_id, current_policy_sequence,
                            license_evidence_id, evidence_digest, license_scope_hash,
                            'training', checked_at
                        ) ON CONFLICT DO NOTHING;
                        IF NOT EXISTS (
                            SELECT 1 FROM public.learning_replay_training_receipt AS receipt
                             WHERE receipt.workspace_id = target_workspace
                               AND receipt.access_kind = target_access_kind
                               AND receipt.access_id = target_access_id
                               AND receipt.normalized_dataset_id = target_normalized_dataset_id
                               AND receipt.manifest_hash = target_manifest_hash
                               AND receipt.fold_plan_hash = target_fold_plan_hash
                               AND receipt.sample_id = sample_id
                               AND receipt.bundle_hash = bundle_hash
                               AND receipt.child_dataset_hash = child_hash
                               AND receipt.source_id = source_id AND receipt.provider = provider
                               AND receipt.product = current_policy.product
                               AND receipt.venue = current_policy.venue
                               AND receipt.contract = current_policy.contract
                               AND receipt.symbol = symbol AND receipt.interval = interval_value
                               AND receipt.data_start = data_start AND receipt.data_end = data_end
                               AND receipt.bar_identity_hash = bar_identity_hash
                               AND receipt.policy_fingerprint = policy_fingerprint
                               AND receipt.current_policy_fingerprint = current_policy_fingerprint
                               AND receipt.scope_hash = use_scope_hash
                               AND receipt.policy_id = current_policy_id
                               AND receipt.policy_sequence = current_policy_sequence
                               AND receipt.license_evidence_id = license_evidence_id
                               AND receipt.evidence_digest = evidence_digest
                               AND receipt.license_scope_hash = license_scope_hash
                               AND receipt.purpose = 'training'
                        ) THEN
                            RAISE EXCEPTION 'WP06_TRAINING_REPLAY_RECEIPT_MISMATCH';
                        END IF;
                    END IF;
                END LOOP;
            END LOOP;

            IF source_sample_count = 0 THEN
                RETURN;
            END IF;
            IF persist_receipts THEN
                SELECT count(*)::integer INTO stored_receipt_count
                  FROM public.learning_replay_training_receipt AS receipt
                 WHERE receipt.workspace_id = target_workspace
                   AND receipt.access_kind = target_access_kind
                   AND receipt.access_id = target_access_id
                   AND receipt.normalized_dataset_id = target_normalized_dataset_id
                   AND receipt.manifest_hash = target_manifest_hash
                   AND receipt.fold_plan_hash = target_fold_plan_hash;
                IF stored_receipt_count <> expected_receipt_count THEN
                    RAISE EXCEPTION 'WP06_TRAINING_REPLAY_RECEIPT_COUNT_MISMATCH';
                END IF;
            END IF;
        END $$
    """)
    op.execute(
        f"ALTER FUNCTION {SCOPE_CHECK}(uuid,text,jsonb,uuid,text,text,uuid,boolean) OWNER TO {RIGHTS_READER}"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCOPE_CHECK}(uuid,text,jsonb,uuid,text,text,uuid,boolean) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCOPE_CHECK}(uuid,text,jsonb,uuid,text,text,uuid,boolean) "
        f"TO {RIGHTS_READER}, {TRAINER_READER}"
    )


def _replace_training_rights_gate() -> None:
    """Thay gate cũ; không nhận manifest JSON caller làm bằng chứng."""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {GATE}(
            target_workspace uuid,
            target_manifest_hash text,
            target_manifest jsonb,
            target_normalized_dataset_id uuid,
            target_fold_plan_hash text,
            target_access_kind text,
            target_access_id uuid
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            PERFORM {SCOPE_CHECK}(
                target_workspace, target_manifest_hash, target_manifest,
                target_normalized_dataset_id, target_fold_plan_hash,
                target_access_kind, target_access_id, true
            );
        END $$
    """)
    op.execute(
        f"ALTER FUNCTION {GATE}(uuid,text,jsonb,uuid,text,text,uuid) OWNER TO {RIGHTS_READER}"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {GATE}(uuid,text,jsonb,uuid,text,text,uuid) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {GATE}(uuid,text,jsonb,uuid,text,text,uuid) "
        f"TO {EVALUATION_READER}, {TRAINER_READER}"
    )
    op.execute(f"""
        CREATE FUNCTION {NORMALIZE_GATE}(
            target_workspace uuid,
            target_manifest_hash text,
            target_normalized_dataset_id uuid,
            target_fold_plan_hash text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE stored_manifest jsonb;
        BEGIN
            IF session_user <> '{CANDIDATE_BUILDER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_REPLAY_CALLER_DENIED';
            END IF;
            SELECT manifest.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest
             WHERE manifest.workspace_id = target_workspace
               AND manifest.manifest_hash = target_manifest_hash;
            IF NOT FOUND THEN RAISE EXCEPTION 'WP06_TRAINING_REPLAY_MANIFEST_MISSING'; END IF;
            PERFORM {SCOPE_CHECK}(
                target_workspace, target_manifest_hash, stored_manifest,
                target_normalized_dataset_id, target_fold_plan_hash,
                'normalization', target_normalized_dataset_id, true
            );
        END $$
    """)
    op.execute(f"ALTER FUNCTION {NORMALIZE_GATE}(uuid,text,uuid,text) OWNER TO {RIGHTS_READER}")
    op.execute(f"REVOKE ALL ON FUNCTION {NORMALIZE_GATE}(uuid,text,uuid,text) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {NORMALIZE_GATE}(uuid,text,uuid,text) TO {CANDIDATE_BUILDER}"
    )


def _replace_trainer_batch_gateway() -> None:
    """Giữ claim flow hiện có và recheck quyền trước receipt lẫn trước payload."""
    op.execute(f"""
        CREATE FUNCTION public.wp06_claim_learning_training_batch(
            target_workspace uuid, target_attempt_id uuid
        ) RETURNS TABLE (
            attempt_id uuid, workspace_id uuid, normalized_dataset_id uuid,
            manifest_hash text, split_policy_version text, fold_plan_hash text,
            approval_hash text, fold_id text, seed integer, trainer_version text,
            dependency_fingerprint text, sample_id text, role text, outcome_id text,
            outcome_hash text, feature_snapshot_hash text, feature_schema_hash text,
            label_hash text, label_value integer, features jsonb, sample_hash text
        ) LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            attempt_row public.learning_training_attempt%ROWTYPE;
            stored_manifest jsonb;
            latest_state text;
            latest_actor text;
            latest_sequence integer;
            approved_hash text;
            stored_dataset_id uuid;
            train_count integer;
            validation_count integer;
            invalid_count integer;
            profile_train_count integer;
            profile_validation_count integer;
            profile_feature_count integer;
            profile_schema_hash text;
            profile_hash text;
        BEGIN
            IF session_user <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_CALLER_DENIED';
            END IF;
            IF target_workspace IS DISTINCT FROM
                NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_WORKSPACE_DENIED';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(target_attempt_id::text, 0)
            );
            SELECT attempt.* INTO attempt_row
              FROM public.learning_training_attempt AS attempt
             WHERE attempt.workspace_id = target_workspace
               AND attempt.id = target_attempt_id;
            IF NOT FOUND OR attempt_row.initial_state <> 'queued' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_FOUND';
            END IF;
            SELECT event.state, event.sequence, event.actor_principal
              INTO latest_state, latest_sequence, latest_actor
              FROM public.learning_attempt_event AS event
             WHERE event.workspace_id = target_workspace
               AND event.attempt_id = target_attempt_id
             ORDER BY event.sequence DESC LIMIT 1;
            IF NOT FOUND OR latest_state <> 'running' OR latest_sequence <> 2
               OR latest_actor <> '{TRAINER}' THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ATTEMPT_NOT_RUNNING';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_training_access_receipt AS receipt
                 WHERE receipt.workspace_id = target_workspace
                   AND receipt.attempt_id = target_attempt_id
            ) THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_ALREADY_CONSUMED';
            END IF;
            SELECT manifest_record.manifest INTO stored_manifest
              FROM public.dataset_manifest AS manifest_record
             WHERE manifest_record.workspace_id = target_workspace
               AND manifest_record.manifest_hash = attempt_row.manifest_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_MANIFEST_NOT_FOUND';
            END IF;
            SELECT approval.approval_hash INTO approved_hash
              FROM public.learning_split_approval AS approval
             WHERE approval.workspace_id = target_workspace
               AND approval.manifest_hash = attempt_row.manifest_hash
               AND approval.split_policy_version = attempt_row.split_policy_version
               AND approval.fold_plan_hash = attempt_row.fold_plan_hash;
            IF NOT FOUND OR approved_hash <> attempt_row.approval_hash THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_APPROVAL_MISMATCH';
            END IF;
            SELECT dataset.id INTO stored_dataset_id
              FROM public.learning_normalized_dataset AS dataset
             WHERE dataset.workspace_id = target_workspace
               AND dataset.id = attempt_row.normalized_dataset_id
               AND dataset.manifest_hash = attempt_row.manifest_hash
               AND dataset.split_policy_version = attempt_row.split_policy_version
               AND dataset.fold_plan_hash = attempt_row.fold_plan_hash
               AND dataset.approval_hash = attempt_row.approval_hash;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_DATASET_MISMATCH';
            END IF;

            PERFORM {SCOPE_CHECK}(
                target_workspace, attempt_row.manifest_hash, stored_manifest,
                stored_dataset_id, attempt_row.fold_plan_hash, 'training',
                target_attempt_id, true
            );

            SELECT profile.training_samples, profile.validation_samples, profile.feature_count,
                   profile.feature_schema_hash, profile.profile_hash
              INTO profile_train_count, profile_validation_count, profile_feature_count,
                   profile_schema_hash, profile_hash
              FROM public.learning_fold_resource_profile AS profile
             WHERE profile.workspace_id = target_workspace
               AND profile.normalized_dataset_id = stored_dataset_id
               AND profile.manifest_hash = attempt_row.manifest_hash
               AND profile.fold_plan_hash = attempt_row.fold_plan_hash
               AND profile.fold_id = attempt_row.fold_id;
            IF NOT FOUND
               OR profile_train_count <> (attempt_row.resource_request->>'training_samples')::integer
               OR profile_validation_count <> (attempt_row.resource_request->>'validation_samples')::integer
               OR profile_feature_count <> (attempt_row.resource_request->>'feature_count')::integer THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_PROFILE_MISMATCH';
            END IF;
            SELECT count(*) FILTER (WHERE assignment.role = 'train')::integer,
                   count(*) FILTER (WHERE assignment.role = 'validation')::integer,
                   count(*) FILTER (
                       WHERE sample.feature_schema_hash <> profile_schema_hash
                          OR pg_catalog.jsonb_typeof(sample.features) <> 'object'
                          OR sample.features = '{{}}'::jsonb
                          OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(sample.features))
                                <> profile_feature_count
                   )::integer
              INTO train_count, validation_count, invalid_count
              FROM public.learning_fold_assignment AS assignment
              JOIN public.learning_normalized_sample AS sample
                ON sample.workspace_id = assignment.workspace_id
               AND sample.manifest_hash = assignment.manifest_hash
               AND sample.fold_plan_hash = assignment.fold_plan_hash
               AND sample.sample_id = assignment.sample_id
             WHERE assignment.workspace_id = target_workspace
               AND assignment.manifest_hash = attempt_row.manifest_hash
               AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
               AND assignment.fold_id = attempt_row.fold_id
               AND assignment.role IN ('train', 'validation');
            IF train_count IS DISTINCT FROM profile_train_count
               OR validation_count IS DISTINCT FROM profile_validation_count
               OR invalid_count <> 0 THEN
                RAISE EXCEPTION 'WP06_TRAINING_BATCH_COUNTS_INVALID';
            END IF;
            INSERT INTO public.learning_training_access_receipt(
                id, workspace_id, attempt_id, normalized_dataset_id, manifest_hash,
                fold_plan_hash, fold_id, approval_hash, request_fingerprint, profile_hash,
                purpose, actor_principal, training_samples, validation_samples, sample_count
            ) VALUES (
                pg_catalog.gen_random_uuid(), target_workspace, target_attempt_id,
                stored_dataset_id, attempt_row.manifest_hash, attempt_row.fold_plan_hash,
                attempt_row.fold_id, attempt_row.approval_hash, attempt_row.content_hash,
                profile_hash, 'trainer-input', '{TRAINER}', train_count, validation_count,
                train_count + validation_count
            );

            PERFORM {SCOPE_CHECK}(
                target_workspace, attempt_row.manifest_hash, stored_manifest,
                stored_dataset_id, attempt_row.fold_plan_hash, 'training',
                target_attempt_id, false
            );
            RETURN QUERY
                SELECT attempt_row.id, attempt_row.workspace_id,
                       attempt_row.normalized_dataset_id,
                       attempt_row.manifest_hash::text,
                       attempt_row.split_policy_version::text,
                       attempt_row.fold_plan_hash::text, attempt_row.approval_hash::text,
                       attempt_row.fold_id::text, attempt_row.seed,
                       attempt_row.trainer_version::text,
                       attempt_row.dependency_fingerprint::text, sample.sample_id,
                       assignment.role::text, sample.outcome_id::text,
                       sample.outcome_hash::text, sample.feature_snapshot_hash::text,
                       sample.feature_schema_hash::text, sample.label_hash::text,
                       sample.label_value, sample.features, sample.sample_hash::text
                  FROM public.learning_fold_assignment AS assignment
                  JOIN public.learning_normalized_sample AS sample
                    ON sample.workspace_id = assignment.workspace_id
                   AND sample.manifest_hash = assignment.manifest_hash
                   AND sample.fold_plan_hash = assignment.fold_plan_hash
                   AND sample.sample_id = assignment.sample_id
                 WHERE assignment.workspace_id = target_workspace
                   AND assignment.manifest_hash = attempt_row.manifest_hash
                   AND assignment.fold_plan_hash = attempt_row.fold_plan_hash
                   AND assignment.fold_id = attempt_row.fold_id
                   AND assignment.role IN ('train', 'validation')
                 ORDER BY assignment.role, assignment.sample_id;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_claim_learning_training_batch(uuid,uuid) "
        f"OWNER TO {TRAINER_READER}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_claim_learning_training_batch(uuid,uuid) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_claim_learning_training_batch(uuid,uuid) "
        f"TO {TRAINER}"
    )


def downgrade() -> None:
    """Chặn hạ cấp khi đã có receipt hoặc nguồn phụ thuộc đã được huấn luyện."""
    connection = op.get_bind()
    if connection.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{TABLE})")).scalar_one():
        raise RuntimeError("WP06_REPLAY_TRAINING_RECEIPT_POPULATED_DOWNGRADE_DENIED")
    op.execute(
        "DROP POLICY wp06_candidate_builder_rights_receipt_read "
        "ON public.backtest_run_dataset_rights"
    )
    op.execute(
        f"REVOKE SELECT ON TABLE public.backtest_run_dataset_rights FROM {CANDIDATE_BUILDER}"
    )
    op.execute("DROP FUNCTION public.wp06_claim_learning_training_batch(uuid,uuid)")
    op.execute(f"DROP FUNCTION {NORMALIZE_GATE}(uuid,text,uuid,text)")
    op.execute(f"DROP FUNCTION {GATE}(uuid,text,jsonb,uuid,text,text,uuid)")
    op.execute(f"DROP FUNCTION {SCOPE_CHECK}(uuid,text,jsonb,uuid,text,text,uuid,boolean)")
    op.execute("DROP FUNCTION public.wp06_authorize_replay_training_bundle(uuid,text,text)")
    op.execute(
        "ALTER FUNCTION public.wp06_assert_training_source_rights_pre_license_scope"
        "(uuid,text,jsonb,uuid,text,text,uuid) RENAME TO wp06_assert_training_source_rights"
    )
    op.execute(
        "ALTER FUNCTION public.wp06_claim_learning_training_batch_pre_license_scope"
        "(uuid,uuid) RENAME TO wp06_claim_learning_training_batch"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {GATE}(uuid,text,jsonb,uuid,text,text,uuid) "
        f"TO {EVALUATION_READER}, {TRAINER_READER}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.wp06_claim_learning_training_batch(uuid,uuid) "
        f"TO {TRAINER}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_license_canonical_json(jsonb) FROM {RIGHTS_READER}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_license_scope_valid(jsonb) FROM {RIGHTS_READER}"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.wp06_license_source_policy_fingerprint"
        "(public.source_policy_revision) "
        f"FROM {RIGHTS_READER}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_sha256_canonical_json(json) FROM {RIGHTS_READER}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_compact_canonical_json(json) FROM {RIGHTS_READER}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.wp06_canonical_utc_timestamp_json(timestamptz) FROM {RIGHTS_READER}"
    )
    for table, columns in LINEAGE_METADATA_GRANTS.items():
        op.execute(f"DROP POLICY wp06_replay_scope_reader_select ON public.{table}")
        op.execute(f"REVOKE SELECT ({columns}) ON public.{table} FROM {RIGHTS_READER}")
    op.execute(f"REVOKE SELECT ON public.source_policy_revision FROM {RIGHTS_READER}")
    for table, columns in LEGACY_RIGHTS_READER_COLUMNS.items():
        op.execute(f"GRANT SELECT ({columns}) ON public.{table} TO {RIGHTS_READER}")
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM {ADMIN}, {RIGHTS_READER}")
    op.execute(f"DROP TRIGGER {TABLE}_immutable ON public.{TABLE}")
    op.execute("DROP FUNCTION public.wp06_reject_replay_training_receipt_mutation()")
    op.drop_index("ix_learning_replay_training_receipt_manifest", table_name=TABLE)
    op.drop_table(TABLE)
