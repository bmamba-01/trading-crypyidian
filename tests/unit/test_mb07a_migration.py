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


def test_upgrade_grants_only_status_column_and_creates_receipt_table_with_split_policies() -> None:
    statements = _run("upgrade")
    joined = "\n".join(statements)

    assert 'GRANT UPDATE (status) ON TABLE "trading_account" TO crypidian_admin_web' in joined
    assert "CREATE TABLE telegram_update_receipt" in joined
    assert "ALTER TABLE telegram_update_receipt ENABLE ROW LEVEL SECURITY" in joined
    assert "ALTER TABLE telegram_update_receipt FORCE ROW LEVEL SECURITY" in joined
    assert "REVOKE ALL ON TABLE telegram_update_receipt FROM PUBLIC, crypidian_app" in joined
    assert (
        "GRANT SELECT, INSERT, DELETE ON TABLE telegram_update_receipt TO crypidian_admin_web"
        in joined
    )
    policies = {line.split()[2]: line for line in statements if line.startswith("CREATE POLICY")}
    assert set(policies) == {"workspace_isolation", "receipt_insert", "receipt_purge"}
    workspace = "current_setting('app.workspace_id', true)"
    for line in policies.values():
        assert "TO crypidian_admin_web" in line
        assert workspace in line
    assert "FOR SELECT" in policies["workspace_isolation"]
    assert "FOR INSERT" in policies["receipt_insert"]
    assert "WITH CHECK" in policies["receipt_insert"]
    # Retention (minor 3): chỉ xóa được hàng cũ hơn 7 ngày; hàng gần đây vẫn bất biến.
    assert "FOR DELETE" in policies["receipt_purge"]
    assert "interval '7 days'" in policies["receipt_purge"]
    assert "CREATE INDEX ix_telegram_update_receipt_created ON telegram_update_receipt" in joined
    for forbidden in ("UPDATE ON TABLE telegram", "crypidian_trading_runtime"):
        assert forbidden not in joined


def test_upgrade_guards_account_status_transition_for_admin_web() -> None:
    """Minor 1: admin-web chỉ được ENABLED → DISABLED (lớp DB sau guard ứng dụng)."""
    joined = "\n".join(_run("upgrade"))

    assert "CREATE FUNCTION public.mb07a_guard_account_status()" in joined
    assert "SECURITY INVOKER" in joined
    assert "SET search_path = pg_catalog, pg_temp" in joined
    assert "current_user = 'crypidian_admin_web'" in joined
    assert "OLD.status = 'ENABLED' AND NEW.status = 'DISABLED'" in joined
    assert "MB07A_ACCOUNT_STATUS_TRANSITION_DENIED" in joined
    assert (
        "CREATE TRIGGER trading_account_status_guard BEFORE UPDATE OF status ON trading_account"
        in joined
    )
    assert "REVOKE ALL ON FUNCTION public.mb07a_guard_account_status() FROM PUBLIC" in joined


def test_downgrade_reverses_everything_in_order() -> None:
    statements = _run("downgrade")
    joined = "\n".join(statements)

    assert "DROP TRIGGER trading_account_status_guard ON trading_account" in joined
    assert "DROP FUNCTION public.mb07a_guard_account_status()" in joined
    assert 'REVOKE UPDATE (status) ON TABLE "trading_account" FROM crypidian_admin_web' in joined
    assert "DROP TABLE telegram_update_receipt" in joined
    trigger = statements.index("DROP TRIGGER trading_account_status_guard ON trading_account")
    function = statements.index("DROP FUNCTION public.mb07a_guard_account_status()")
    assert trigger < function


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
    indexes = {index.name: [column.name for column in index.columns] for index in table.indexes}
    assert indexes["ix_telegram_update_receipt_created"] == ["workspace_id", "created_at"]


def test_claim_purges_expired_receipts_before_insert_in_same_transaction() -> None:
    """Minor 3: retention 7 ngày (Telegram chọn update_id ngẫu nhiên sau ≥1 tuần im lặng)."""
    import asyncio
    from uuid import uuid4

    from tests.unit.runtime_fakes import RecordingSession

    from trading_bot.adapters.database.repositories.telegram import (
        RECEIPT_RETENTION,
        TelegramUpdateReceiptRepository,
    )

    session = RecordingSession(scalar_results=[uuid4()])
    claimed = asyncio.run(
        TelegramUpdateReceiptRepository(session).claim(uuid4(), 42)  # type: ignore[arg-type]
    )

    assert claimed is True
    assert RECEIPT_RETENTION.days == 7
    assert session.statements[0].startswith("DELETE FROM telegram_update_receipt")
    assert "created_at <" in session.statements[0]
    assert session.statements[1].startswith("INSERT INTO telegram_update_receipt")
    assert "ON CONFLICT" in session.statements[1]
