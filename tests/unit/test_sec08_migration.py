"""Kiểm tra schema, quyền tối thiểu và chuỗi migration cho auth_rate_limit_state (SEC-08)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from trading_bot.adapters.database.models import AuthRateLimitState

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "d9e1f3a5b7c8"
PREVIOUS_HEAD = "0094c0fdba7f"


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

    def f(self, name: str) -> str:
        return name

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


def test_auth_rate_limit_state_model_has_expected_columns_and_no_pii() -> None:
    cols = set(AuthRateLimitState.__table__.c.keys())
    assert {
        "scope_key",
        "scope_type",
        "failures",
        "window_started_at",
        "locked_until",
        "updated_at",
    } <= cols
    for forbidden in ("email", "ip", "client_ip", "workspace_slug", "user_id", "password"):
        assert forbidden not in cols
    pk_cols = [col.name for col in AuthRateLimitState.__table__.primary_key.columns]
    assert pk_cols == ["scope_key"]


def test_migration_chains_from_head_and_grants_admin_web_without_rls() -> None:
    module, statements = _run("upgrade")
    joined = "\n".join(statements)

    assert module.down_revision == PREVIOUS_HEAD
    assert "CREATE TABLE auth_rate_limit_state" in joined
    assert "CREATE INDEX ix_auth_rate_limit_state_updated_at ON auth_rate_limit_state" in joined
    assert "REVOKE ALL ON TABLE auth_rate_limit_state FROM PUBLIC, crypidian_app" in joined
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth_rate_limit_state TO crypidian_admin_web"
    ) in joined
    assert "ENABLE ROW LEVEL SECURITY" not in joined
    assert "crypidian_trading_runtime" not in joined


def test_downgrade_revokes_grants_and_drops_table() -> None:
    _, statements = _run("downgrade")
    joined = "\n".join(statements)

    assert "REVOKE ALL ON TABLE auth_rate_limit_state FROM crypidian_admin_web" in joined
    assert "DROP INDEX ix_auth_rate_limit_state_updated_at" in joined
    assert "DROP TABLE auth_rate_limit_state" in joined
