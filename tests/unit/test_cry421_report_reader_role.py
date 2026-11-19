"""Kiểm thử đơn vị CRY-421: role crypidian_report_reader, compose, migration và run_ai_analyst."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _load_migration() -> Any:
    path = ROOT / "migrations/versions/12e554aace5d_cry421_report_reader_role.py"
    spec = importlib.util.spec_from_file_location("cry421_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provision_roles_and_local_secrets_include_report_reader() -> None:
    """Role crypidian_report_reader phải nằm trong danh sách provision và NOINHERIT."""
    provision_src = (ROOT / "scripts/provision_database_roles.py").read_text(encoding="utf-8")
    secrets_src = (ROOT / "scripts/prepare_local_secrets.py").read_text(encoding="utf-8")
    compose_src = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert '"crypidian_report_reader"' in provision_src
    assert "/run/secrets/database_report_reader_password" in provision_src
    assert '"report_reader"' in secrets_src
    assert "report-reader:" in compose_src
    assert "TRADING_BOT_DATABASE_USER: crypidian_report_reader" in compose_src
    assert "database_report_reader_password" in compose_src


def test_run_ai_analyst_uses_report_reader_without_migrator_or_update() -> None:
    """scripts/run_ai_analyst.py dùng service report-reader và không UPDATE analyst_session."""
    script_src = (ROOT / "scripts/run_ai_analyst.py").read_text(encoding="utf-8")

    assert '"report-reader"' in script_src
    assert '"migrator"' not in script_src
    assert "update(AnalystSessionRecord)" not in script_src


def test_cry421_migration_grants_only_select_and_four_append_tables() -> None:
    """Migration 12e554aace5d chỉ cấp SELECT danh sách cho phép và INSERT 4 bảng."""
    migration = _load_migration()
    statements: list[str] = []

    class _FakeOp:
        @staticmethod
        def execute(sql: str) -> None:
            statements.append(sql)

    original_op = migration.op
    migration.op = _FakeOp
    try:
        migration.upgrade()
    finally:
        migration.op = original_op

    joined = "\n".join(statements)
    assert "CREATE ROLE crypidian_report_reader LOGIN NOINHERIT NOBYPASSRLS" in joined
    assert "GRANT USAGE ON SCHEMA public TO crypidian_report_reader" in joined
    assert "GRANT UPDATE" not in joined
    assert "GRANT DELETE" not in joined
    assert "user_account" not in joined
    assert "user_session" not in joined
    for table in ("run_report", "analyst_run", "analyst_session", "proposal_backtest"):
        assert f"GRANT INSERT ON TABLE public.{table} TO crypidian_report_reader" in joined
        assert f"CREATE POLICY crypidian_report_reader_{table}_insert" in joined
    for table in ("bot_instance", "trade", "bot_decision", "market_bar", "macro_calendar_fetch"):
        assert f"GRANT SELECT ON TABLE public.{table} TO crypidian_report_reader" in joined
        assert f"GRANT INSERT ON TABLE public.{table} TO crypidian_report_reader" not in joined
