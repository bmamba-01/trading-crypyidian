from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f8e0a2c4d6bd_wp06_report_source_disclosure_gate.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration recheck quyền report WP06."
    spec = importlib.util.spec_from_file_location("wp06_report_source_disclosure_gate", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(operation: str) -> list[str]:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    getattr(module, operation)()
    return statements


def test_report_rights_migration_follows_current_consumer_head() -> None:
    migration = _migration()
    assert migration.revision == "f8e0a2c4d6bd"
    assert migration.down_revision == "f8e0a2c4d6bc"


def test_report_rechecks_use_persisted_scope_and_do_not_accept_caller_scope() -> None:
    sql = "\n".join(_statements("upgrade"))

    assert "wp06_recheck_learning_evaluation_report_rights(uuid,uuid,text)" in sql
    assert "wp06_recheck_scorer_test_report_source_rights(uuid,uuid)" in sql
    assert "session_user <> 'crypidian_learning_evaluator'" in sql
    assert "session_user <> 'crypidian_admin_web'" in sql
    assert "target_access_kind := 'evaluation'" in sql
    assert "target_access_kind := 'report_read'" in sql
    assert "TO crypidian_admin_web, crypidian_learning_evaluator" in sql
    assert sql.count("PERFORM public.wp06_check_replay_training_scope(") == 2
    assert "PERFORM public.wp06_check_replay_training_scope(uuid,text," not in sql
    assert "target_manifest" not in sql
    assert "target_scope" not in sql
    assert "target_access_kind" in sql
    assert "'report_read'" in sql
    assert sql.count("persist_receipts boolean") == 0
    assert "'evaluation', access_row.evaluation_run_id, false" in sql
    assert "target_access_kind, target_access_id, false" in sql
    for table, columns in _migration().RIGHTS_READER_COLUMNS.items():
        assert f"ON public.{table} FOR SELECT TO crypidian_learning_source_rights_reader" in sql
        assert f"GRANT SELECT ({columns}) ON public.{table}" in sql
        assert f"REVOKE SELECT ({columns}) ON public.{table}" in "\n".join(_statements("downgrade"))
        assert "canonical_report" not in columns
        assert "report," not in columns


def test_scope_checker_extension_is_reversible_and_uses_distinct_dollar_quotes() -> None:
    upgrade_sql = "\n".join(_statements("upgrade"))
    downgrade_sql = "\n".join(_statements("downgrade"))

    assert "$old_guard$" in upgrade_sql
    assert "$new_guard$" in upgrade_sql
    assert "CASE WHEN target_access_kind IN ('evaluation', 'report_read')" in upgrade_sql
    assert "session_user <> 'crypidian_admin_web'" in upgrade_sql
    assert "CASE WHEN target_access_kind = 'evaluation'" in downgrade_sql
    assert "DROP FUNCTION public.wp06_recheck_learning_evaluation_report_rights" in downgrade_sql
    assert "DROP FUNCTION public.wp06_recheck_scorer_test_report_source_rights" in downgrade_sql
    assert "FROM PUBLIC" in upgrade_sql
    assert "OWNER TO crypidian_learning_source_rights_reader" in upgrade_sql

    source_checker = (
        ROOT / "migrations/versions/b7d9f1a3c5e8_wp06_training_license_scope.py"
    ).read_text(encoding="utf-8")
    assert "OR target_access_kind NOT IN ('normalization', 'evaluation', 'training')" in (
        source_checker
    )
    assert source_checker.count("CASE WHEN target_access_kind = 'evaluation'") == 2


def test_upgrade_rechecks_before_any_report_payload_projection() -> None:
    read_model_path = (
        ROOT / "src/trading_bot/adapters/database/repositories/learning_scorer_report_read_model.py"
    )
    read_model = read_model_path.read_text(encoding="utf-8")
    get_latest = read_model[read_model.index("async def get_latest(") :]
    assert get_latest.index("identity_statement") < get_latest.index(
        "wp06_recheck_scorer_test_report_source_rights"
    )
    assert get_latest.index("wp06_recheck_scorer_test_report_source_rights") < get_latest.index(
        "report_metadata ="
    )
    assert "canonical_report" not in get_latest

    report_application = (
        ROOT / "src/trading_bot/application/learning_scorer_report_read_model.py"
    ).read_text(encoding="utf-8")
    assert "snapshot_read_only=True" not in report_application
