from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / ("migrations/versions/e2f4a6c8b0d6_wp06_paper_performance_admin_insert_acl.py")


def _migration():
    assert MIGRATION.exists(), "Thiếu migration thu hồi quyền ghi báo cáo khỏi admin."
    spec = importlib.util.spec_from_file_location(
        "wp06_paper_performance_admin_insert_acl", MIGRATION
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql_for(operation_name: str) -> str:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    getattr(module, operation_name)()
    return "\n".join(statements)


def test_admin_report_insert_acl_migration_follows_exact_source_rights_gate() -> None:
    migration = _migration()

    assert migration.down_revision == "e2f4a6c8b0d5"


def test_admin_cannot_insert_aggregate_report_but_keeps_metadata_read_gateway() -> None:
    sql = _sql_for("upgrade")

    assert (
        "REVOKE INSERT ON TABLE public.learning_paper_performance_report "
        "FROM PUBLIC, crypidian_admin_web"
    ) in sql
    assert "wp06_aggregate_paper_performance" not in sql


def test_downgrade_restores_pre_migration_admin_insert_privilege() -> None:
    sql = _sql_for("downgrade")

    assert (
        "GRANT INSERT ON TABLE public.learning_paper_performance_report TO crypidian_admin_web"
    ) in sql
