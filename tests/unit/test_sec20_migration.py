"""Kiểm tra schema, RLS, column grants và chain migration cho action_challenge (SEC-20)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from trading_bot.adapters.database.models import ActionChallengeRecord

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "e9a1c3f5b7d2"


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


def test_action_challenge_model_has_expected_columns_and_no_secret_fields() -> None:
    cols = set(ActionChallengeRecord.__table__.c.keys())
    assert {
        "id",
        "created_at",
        "workspace_id",
        "membership_id",
        "session_id",
        "action",
        "payload_hash",
        "status",
        "failed_attempts",
        "max_attempts",
        "verification_token_hash",
        "expires_at",
        "verified_at",
        "consumed_at",
        "cancelled_at",
    } <= cols
    assert "totp_code" not in cols
    assert "step_up_token" not in cols
    assert "payload" not in cols


def test_migration_chains_from_head_and_enforces_rls_and_column_grants() -> None:
    module, statements = _run("upgrade")
    joined = "\n".join(statements)

    assert module.down_revision == "d8e2f4a6b8c0"
    assert "CREATE TABLE action_challenge" in joined
    assert "ALTER TABLE action_challenge ENABLE ROW LEVEL SECURITY" in joined
    assert "ALTER TABLE action_challenge FORCE ROW LEVEL SECURITY" in joined
    assert "app.workspace_id" in joined
    assert "app.membership_id" in joined
    assert "REVOKE ALL ON TABLE action_challenge FROM PUBLIC, crypidian_app" in joined
    assert "GRANT SELECT, INSERT ON TABLE action_challenge TO crypidian_admin_web" in joined
    assert (
        "GRANT UPDATE (status, failed_attempts, verification_token_hash, verified_at, "
        "consumed_at, cancelled_at) ON TABLE action_challenge TO crypidian_admin_web"
    ) in joined
    assert "GRANT UPDATE ON TABLE action_challenge TO crypidian_admin_web" not in joined
    assert "crypidian_trading_runtime" not in joined


def test_downgrade_revokes_grants_and_drops_table() -> None:
    _, statements = _run("downgrade")
    joined = "\n".join(statements)

    assert "REVOKE ALL ON TABLE action_challenge FROM crypidian_admin_web" in joined
    assert "DROP POLICY IF EXISTS action_challenge_admin_scope ON action_challenge" in joined
    assert "DROP TABLE action_challenge" in joined
