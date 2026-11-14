from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/c7e9a1b3d5f8_wp06_paper_performance.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration chẩn đoán PAPER aggregate-only"
    spec = importlib.util.spec_from_file_location("wp06_paper_performance", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(
        execute=lambda sql: statements.append(str(sql)),
        create_table=lambda *args: statements.extend(str(item) for item in args),
        create_index=lambda *args, **kwargs: None,
        drop_index=lambda *args, **kwargs: None,
        drop_table=lambda *args, **kwargs: None,
        f=lambda name: name,
    )
    module.upgrade()
    return "\n".join(statements)


def test_migration_chains_after_typed_attribution_and_is_aggregate_only() -> None:
    migration = _migration()
    source = MIGRATION.read_text(encoding="utf-8")
    sql = _upgrade_sql()
    assert migration.down_revision == "b5d7f9a1c3e6"
    assert "learning_paper_performance_report" in source
    assert "learning_scorer_test_evaluation_report" in source
    for required in (
        "crypidian_learning_paper_performance_evaluator",
        "SECURITY DEFINER",
        "wp06_resolve_scorer_runtime_target",
        "FORCE ROW LEVEL SECURITY",
        "BEFORE UPDATE OR DELETE",
        "class_probabilities",
        "bot_decision_id",
        "closed_at",
        "net_pnl",
        "membership_digest",
        "WP06_PAPER_PERFORMANCE_TARGET_STALE",
        "WP06_PAPER_PERFORMANCE_CURRENT_RIGHTS_DENIED",
    ):
        assert required in source or required in sql
    assert "GRANT SELECT ON TABLE public.trade TO crypidian_admin_web" not in sql
    assert "GRANT SELECT ON TABLE public.order_intent TO crypidian_admin_web" not in sql
