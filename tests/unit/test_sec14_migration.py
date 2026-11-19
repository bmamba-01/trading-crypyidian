"""Kiểm tra schema ORM và migration Alembic cho bảng mfa_recovery_code (SEC-14)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from trading_bot.adapters.database.models import MfaRecoveryCode

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "f2a4c6e8b0d1"


def _load() -> ModuleType:
    path = next(VERSIONS.glob(f"{REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(" ".join(str(sql).split()))

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> None:
        del columns, kwargs
        self.statements.append(f"CREATE TABLE {name}")

    def create_index(self, name: str, table: str, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.statements.append(f"CREATE INDEX {name} ON {table}")

    def drop_index(self, name: str, **kwargs: Any) -> None:
        del kwargs
        self.statements.append(f"DROP INDEX {name}")

    def drop_table(self, name: str, **kwargs: Any) -> None:
        del kwargs
        self.statements.append(f"DROP TABLE {name}")


def _run(function: str) -> tuple[ModuleType, list[str]]:
    module = _load()
    recorder = RecordingOp()
    original = module.op
    module.op = recorder
    try:
        getattr(module, function)()
    finally:
        module.op = original
    return module, recorder.statements


def test_mfa_recovery_code_model_stores_only_salted_hash_never_plaintext() -> None:
    cols = set(MfaRecoveryCode.__table__.c.keys())
    assert {
        "id",
        "created_at",
        "workspace_id",
        "user_id",
        "membership_id",
        "batch_id",
        "code_salt",
        "code_hash",
        "used_at",
        "revoked_at",
    } <= cols
    assert "code" not in cols
    assert "plaintext" not in cols
    assert "recovery_code" not in cols


def test_migration_chains_from_current_head_and_enforces_rls_and_grants() -> None:
    module, statements = _run("upgrade")
    joined = "\n".join(statements)

    assert module.down_revision == "f0912a3b4c5d"
    assert "CREATE TABLE mfa_recovery_code" in joined
    assert "ALTER TABLE mfa_recovery_code ENABLE ROW LEVEL SECURITY" in joined
    assert "ALTER TABLE mfa_recovery_code FORCE ROW LEVEL SECURITY" in joined
    assert "app.workspace_id" in joined
    assert "REVOKE ALL ON TABLE mfa_recovery_code FROM PUBLIC, crypidian_app" in joined
    assert "GRANT SELECT, INSERT ON TABLE mfa_recovery_code TO crypidian_admin_web" in joined
    assert (
        "GRANT UPDATE (used_at, revoked_at) ON TABLE mfa_recovery_code TO crypidian_admin_web"
        in joined
    )
    assert "GRANT UPDATE ON TABLE mfa_recovery_code TO crypidian_admin_web" not in joined
    assert "crypidian_trading_runtime" not in joined


def test_downgrade_revokes_grants_and_drops_table() -> None:
    _, statements = _run("downgrade")
    joined = "\n".join(statements)

    assert "REVOKE ALL ON TABLE mfa_recovery_code FROM crypidian_admin_web" in joined
    assert "DROP POLICY IF EXISTS mfa_recovery_code_admin_scope ON mfa_recovery_code" in joined
    assert "DROP TABLE mfa_recovery_code" in joined
