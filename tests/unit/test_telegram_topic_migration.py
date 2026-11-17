"""TG-4: hợp đồng migration 60a62c705a89 — cột topic trên binding và quyền tối thiểu."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "60a62c705a89_tg4_telegram_topic_threads.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tg4_topic_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(monkeypatch: pytest.MonkeyPatch, step: str) -> list[tuple[str, Any]]:
    module = _load()
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        module.op, "execute", lambda sql: calls.append(("sql", re.sub(r"\s+", " ", str(sql))))
    )
    for name in ("add_column", "create_check_constraint", "drop_constraint", "drop_column"):
        monkeypatch.setattr(module.op, name, lambda *args, _n=name, **_kw: calls.append((_n, args)))
    getattr(module, step)()
    return calls


def test_revision_chains_after_run_report_head() -> None:
    module = _load()
    assert module.revision == "60a62c705a89"
    assert module.down_revision == "ad99c4f520d0"


def test_upgrade_adds_nullable_positive_thread_columns_and_column_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, "upgrade")
    added = [args for name, args in calls if name == "add_column"]
    assert [(table, column.name, column.nullable) for table, column in added] == [
        ("telegram_binding", "signal_thread_id", True),
        ("telegram_binding", "report_thread_id", True),
    ]
    checks = [args for name, args in calls if name == "create_check_constraint"]
    assert [args[2] for args in checks] == [
        "signal_thread_id IS NULL OR signal_thread_id > 0",
        "report_thread_id IS NULL OR report_thread_id > 0",
    ]
    sql = [args for name, args in calls if name == "sql"]
    assert sql == [
        "GRANT UPDATE (signal_thread_id, report_thread_id) ON TABLE public.telegram_binding "
        "TO crypidian_admin_web",
        "GRANT SELECT (signal_thread_id, report_thread_id) ON TABLE public.telegram_binding "
        "TO crypidian_signal_worker",
    ]
    # Không cấp quyền mức bảng hay quyền ghi cho worker.
    assert not any("ALL" in item or ("UPDATE" in item and "signal_worker" in item) for item in sql)


def test_downgrade_revokes_then_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch, "downgrade")
    names = [name for name, _ in calls]
    assert names == [
        "sql",
        "sql",
        "drop_constraint",
        "drop_column",
        "drop_constraint",
        "drop_column",
    ]
    assert all(item.startswith("REVOKE") for name, item in calls if name == "sql")
