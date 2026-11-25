from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/f0a2c4e6b8d8_wp06_signal_outcome_reader.py"


def _migration():
    assert MIGRATION.is_file(), "Thiếu migration giới hạn reader cho kết quả tín hiệu."
    spec = importlib.util.spec_from_file_location("wp06_signal_outcome_reader", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(action: str) -> list[str]:
    statements: list[str] = []
    migration = _migration()
    migration.op = SimpleNamespace(execute=lambda statement: statements.append(str(statement)))
    getattr(migration, action)()
    return statements


def test_signal_outcome_reader_migration_has_narrow_current_rights_scope() -> None:
    migration = _migration()
    assert migration.revision == "f0a2c4e6b8d8"
    assert migration.down_revision == "f0a2c4e6b8d7"

    sql = "\n".join(_statements("upgrade"))
    assert "GRANT SELECT (" in sql
    assert "ON TABLE public.signal_outcome TO crypidian_admin_report_reader" in sql
    assert "ON TABLE public.telegram_signal_delivery TO crypidian_admin_report_reader" in sql
    assert "FOR SELECT TO crypidian_admin_report_reader USING (" in sql
    assert sql.count("wp06_admin_report_reader_can_access(workspace_id, bot_id)") == 2
    assert "ALTER TABLE public.signal_outcome ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE public.telegram_signal_delivery ENABLE ROW LEVEL SECURITY" in sql
    assert "entry_price" not in sql
    assert "bar_sources" not in sql
    assert "input_hash" not in sql
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql


def test_signal_outcome_reader_downgrade_removes_only_its_policy_and_columns() -> None:
    sql = "\n".join(_statements("downgrade"))
    assert "DROP POLICY wp06_admin_report_reader_signal_outcome_select" in sql
    assert "DROP POLICY wp06_admin_report_reader_telegram_signal_delivery_select" in sql
    assert "REVOKE SELECT (" in sql
    assert "FROM crypidian_admin_report_reader" in sql
