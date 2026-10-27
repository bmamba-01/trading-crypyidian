"""MB07a.4: migration duy nhất MB-07a — grant cột status (ruling 29/34), bảng dedup Telegram."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from trading_bot.adapters.database.models import Base, TelegramUpdateReceipt

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "f7b9d1e3a5c8"


def _load(name: str) -> ModuleType:
    path = next(VERSIONS.glob(f"{name}_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    @staticmethod
    def f(name: str) -> str:
        return name

    def execute(self, sql: str) -> None:
        self.statements.append(" ".join(str(sql).split()))

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> None:
        del columns, kwargs
        self.statements.append(f"CREATE TABLE {name}")

    def drop_table(self, name: str, **kwargs: Any) -> None:
        del kwargs
        self.statements.append(f"DROP TABLE {name}")

    def create_index(self, name: str, table: str, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.statements.append(f"CREATE INDEX {name} ON {table}")

    def drop_index(self, name: str, **kwargs: Any) -> None:
        del kwargs
        self.statements.append(f"DROP INDEX {name}")


def _run(function: str) -> list[str]:
    module = _load(REVISION)
    recorder = RecordingOp()
    original = module.op
    module.op = recorder
    try:
        getattr(module, function)()
    finally:
        module.op = original
    return recorder.statements


def test_single_migration_chains_after_mb05_head_and_is_the_only_head() -> None:
    module = _load(REVISION)
    parents = [_load(path.stem.split("_")[0]).down_revision for path in VERSIONS.glob("*.py")]

    assert module.down_revision == "e1f3a5c7b9d2"
    assert parents.count("e1f3a5c7b9d2") == 1
    assert REVISION not in parents


def test_upgrade_grants_only_status_column_and_creates_append_only_receipt_table() -> None:
    statements = _run("upgrade")
    joined = "\n".join(statements)

    assert 'GRANT UPDATE (status) ON TABLE "trading_account" TO crypidian_admin_web' in joined
    assert "CREATE TABLE telegram_update_receipt" in joined
    assert "ALTER TABLE telegram_update_receipt ENABLE ROW LEVEL SECURITY" in joined
    assert "ALTER TABLE telegram_update_receipt FORCE ROW LEVEL SECURITY" in joined
    assert "REVOKE ALL ON TABLE telegram_update_receipt FROM PUBLIC, crypidian_app" in joined
    assert "GRANT SELECT, INSERT ON TABLE telegram_update_receipt TO crypidian_admin_web" in joined
    policy = next(line for line in statements if "CREATE POLICY" in line)
    assert "TO crypidian_admin_web" in policy
    assert "current_setting('app.workspace_id', true)" in policy
    assert "WITH CHECK" in policy
    for forbidden in ("UPDATE ON TABLE telegram", "DELETE", "crypidian_trading_runtime"):
        assert forbidden not in joined


def test_downgrade_reverses_grant_and_table() -> None:
    joined = "\n".join(_run("downgrade"))

    assert 'REVOKE UPDATE (status) ON TABLE "trading_account" FROM crypidian_admin_web' in joined
    assert "DROP TABLE telegram_update_receipt" in joined


def test_receipt_model_matches_unique_workspace_update_key() -> None:
    table = TelegramUpdateReceipt.__table__

    assert table.name in Base.metadata.tables
    uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert uniques["uq_telegram_update_receipt_update"] == ("workspace_id", "update_id")
    assert table.c.update_id.type.__class__.__name__ == "BigInteger"
