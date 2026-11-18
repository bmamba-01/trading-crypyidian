"""Kiểm hợp đồng migration source-scope trước khi chạy Compose serially."""

from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f8e0a2c4d6b9_wp06_scorer_job_source_scope.py"


def test_scope_completeness_mirrors_the_pinned_provider_selection_contract() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "LIMIT 256" not in sql
    assert "pg_catalog.jsonb_object_length" not in sql
    assert "max_bars_per_stream" in sql
    assert "job_row.context_hash IS DISTINCT FROM" in sql
    assert "context_schema_version := job_row.context_payload #>> '{context,schema_version}'" in sql
    assert "WHEN 'crypidian-bot-decision-context-v1' THEN" in sql
    assert "max_bars_per_stream := 256;" in sql
    assert "WHEN 'crypidian-bot-decision-context-v2'" in sql
    assert "selected.event_at <= evidence_row.observed_at" in sql
    assert "feature_row.as_of IS DISTINCT FROM evidence_row.observed_at" in sql
    assert "feature_row.computed_at IS DISTINCT FROM evidence_row.observed_at" in sql
    assert "LIMIT max_bars_per_stream" in sql
    assert "DISTINCT ON (bar.open_time)" in sql
    assert "bar.available_at <= evidence_row.observed_at" in sql
    assert "bar.close_time <= evidence_row.observed_at" in sql
    assert "selected.event_at <= evidence_row.observed_at" in sql
    for required in (
        "public.feature_snapshot AS feature",
        "feature.canonical_json",
        "feature_row.snapshot_id IS DISTINCT FROM group_row.feature_snapshot_id",
        "feature_row.content_hash IS DISTINCT FROM group_row.feature_snapshot_id",
        "feature_row.input_event_ids IS DISTINCT FROM staged_input_event_ids",
        "wp06_license_canonical_json(lineage_document)",
    ):
        assert required in sql

    # Builder lọc event_at sau khi provider đã giới hạn thời điểm mở và chọn revision.
    migration_function = sql[
        sql.index("CREATE FUNCTION public.wp06_assert_complete_bot_decision_job_source_bar_set(") :
    ]
    open_time_limit = migration_function.index("LIMIT max_bars_per_stream")
    revision_choice = migration_function.index("DISTINCT ON (bar.open_time)")
    builder_cutoff = migration_function.index("selected.event_at <= evidence_row.observed_at")
    assert open_time_limit < revision_choice < builder_cutoff


def test_feature_snapshot_input_bar_ids_are_bound_to_the_selected_market_bar_set() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    migration_function = sql[
        sql.index("CREATE FUNCTION public.wp06_assert_complete_bot_decision_job_source_bar_set(") :
    ]

    assert "expected_feature_input_bar_ids jsonb;" in migration_function
    assert "feature_row.input_bar_ids IS DISTINCT FROM expected_feature_input_bar_ids" in (
        migration_function
    )
    assert "'open_time', pg_catalog.to_char(" in migration_function
    assert "'symbol', bar.symbol" in migration_function
    assert "'timeframe', bar.timeframe" in migration_function
    assert "bar.id = ANY(expected_market_bar_ids)" in migration_function
    assert "pg_catalog.jsonb_agg(" in migration_function
    assert "ORDER BY bar.open_time" in migration_function


def test_runtime_context_limit_is_persisted_and_downgrade_revokes_only_added_columns() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "GRANT SELECT (workspace_id,id,bot_id,account_id,evidence_id,evidence_hash," in sql
    assert "context_hash,bot_config_version_id,context_payload,evaluated_at,status)" in sql
    assert "open_time,close_time,event_at," in sql
    assert "available_at,is_closed)" in sql
    assert "REVOKE SELECT (workspace_id,id,bot_id,account_id,evidence_id,evidence_hash," in sql
    assert "context_hash,bot_config_version_id,context_payload,evaluated_at,status)" in sql


def test_metadata_locks_precede_source_rights_locking() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    metadata_start = sql.index("def _create_runtime_metadata_gate()")
    metadata_end = sql.index("def _create_decision_cleanup()", metadata_start)
    metadata = sql[metadata_start:metadata_end]
    source_start = sql.index("def _create_source_scope_gate()")
    source_end = sql.index("def _create_job_bound_artifact_resolver()", source_start)
    source_gate = sql[source_start:source_end]

    assert (
        metadata.index("learning-scoring-deployment:")
        < metadata.index("learning-model-registry:")
        < metadata.index("registry-entry")
    )
    wrapper_start = source_gate.index("for public_function, check_snapshot in (")
    wrappers = source_gate[wrapper_start:]
    assert wrappers.index("wp06_resolve_scorer_runtime_metadata(target_workspace)") < (
        wrappers.index("wp06_check_scorer_job_source_scope(")
    )


def test_rights_reader_grants_and_downgrade_are_column_scoped() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE POLICY scorer_job_source_rights_feature_snapshot_select" in sql
    assert "canonical_json) ON public.feature_snapshot" in sql
    assert sql.count("scorer_job_source_rights_feature_snapshot_select") >= 2
    assert "REVOKE SELECT (provider,product,venue,contract,license_scope," in sql
    assert "GRANT SELECT (provider,product,venue,contract,license_scope,license_evidence_id," in sql
    assert "REVOKE SELECT ON public.source_policy_revision" not in sql
    assert "DROP FUNCTION public.wp06_preflight_scorer_job_source_rights(uuid,uuid)" in sql
    assert "DROP FUNCTION public.wp06_check_scorer_job_source_scope(uuid,uuid,boolean)" in sql
