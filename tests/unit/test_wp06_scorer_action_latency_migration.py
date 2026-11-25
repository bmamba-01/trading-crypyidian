from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "f0a2c4e6b8d2_wp06_scorer_action_latency.py"
)


def _source() -> str:
    assert MIGRATION.is_file(), "thiếu migration aggregate latency scorer"
    return MIGRATION.read_text(encoding="utf-8")


def test_action_latency_migration_is_single_head_child_of_proof_v3() -> None:
    source = _source()
    assert 'revision: str = "f0a2c4e6b8d2"' in source
    assert 'down_revision: str | None = "f0a2c4e6b8d1"' in source


def test_latency_gateway_is_aggregate_only_and_uses_current_source_gate() -> None:
    source = _source()

    assert "CREATE FUNCTION public.wp06_aggregate_scorer_paper_action_latency" in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog, pg_temp" in source
    assert "wp06_authorize_paper_performance_decision_source_scope" in source
    assert "wp06_paper_performance_decision_source_ids" in source
    assert "candidate_no_intent_count" in source
    assert "future_intent_count" in source
    assert "percentile_cont(0.5)" in source
    assert "percentile_cont(0.95)" in source
    assert "valid_sample_count, 0) >= 100" in source
    assert "order_intent_id" not in source
    assert "decision_id uuid, intent" not in source
    assert "CREATE TABLE public.learning_scorer_action_latency" not in source


def test_latency_gateway_acl_grants_only_the_aggregate_entry_point() -> None:
    source = _source()

    assert 'EVALUATOR = "crypidian_learning_paper_performance_evaluator"' in source
    assert "ALTER FUNCTION {FUNCTION} OWNER TO {EVALUATOR}" in source
    assert "FUNCTION = (" in source
    assert "REVOKE ALL ON FUNCTION {FUNCTION}" in source
    assert "FROM PUBLIC, {ADMIN}, {HISTORY}" in source
    assert "GRANT EXECUTE ON FUNCTION {FUNCTION} TO {ADMIN}" in source
    assert 'HISTORY = "crypidian_scorer_history"' in source
    assert "GRANT SELECT (workspace_id,id,created_at,available_at)" in source
