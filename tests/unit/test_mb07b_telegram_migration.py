"""Kiểm tra schema Telegram MB-07b: TTL ledger, RLS và unique index active."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import Index

from trading_bot.adapters.database.models import (
    TelegramBinding,
    TelegramBindingChallenge,
    TelegramLinkRequest,
)

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "e2c4a6f8b1d3"


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

    def drop_constraint(self, name: str, table: str, **kwargs: Any) -> None:
        del kwargs
        self.statements.append(f"DROP CONSTRAINT {name} ON {table}")

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> None:
        del columns, kwargs
        self.statements.append(f"CREATE TABLE {name}")

    def create_index(self, name: str, table: str, *args: Any, **kwargs: Any) -> None:
        del args
        self.statements.append(f"CREATE INDEX {name} ON {table} {kwargs}")

    def drop_index(self, name: str, **kwargs: Any) -> None:
        del kwargs
        self.statements.append(f"DROP INDEX {name}")

    def drop_table(self, name: str, **kwargs: Any) -> None:
        del kwargs
        self.statements.append(f"DROP TABLE {name}")

    def create_unique_constraint(self, name: str, table: str, columns: list[str]) -> None:
        self.statements.append(f"CREATE UNIQUE {name} ON {table} ({','.join(columns)})")


def _run(name: str) -> list[str]:
    module = _load()
    recorder = RecordingOp()
    original = module.op
    module.op = recorder
    try:
        getattr(module, name)()
    finally:
        module.op = original
    return recorder.statements


def test_binding_has_only_active_partial_uniques_and_keeps_revoked_history() -> None:
    indexes = {index.name: index for index in TelegramBinding.__table__.indexes}

    assert set(indexes) == {
        "uq_telegram_binding_active_user",
        "uq_telegram_binding_active_membership",
    }
    assert all(isinstance(index, Index) and index.unique for index in indexes.values())
    assert all(
        str(index.dialect_options["postgresql"]["where"]) == "revoked_at IS NULL"
        for index in indexes.values()
    )
    assert "revoked_at" in TelegramBinding.__table__.c


def test_link_and_callback_ledgers_store_digests_and_have_expiry_fields() -> None:
    assert {"token_digest", "expires_at", "consumed_at"} <= set(
        TelegramLinkRequest.__table__.c.keys()
    )
    assert {"callback_digest", "telegram_user_id", "expires_at", "consumed_at"} <= set(
        TelegramBindingChallenge.__table__.c.keys()
    )
    assert "token" not in TelegramLinkRequest.__table__.c
    assert "callback_nonce" not in TelegramBindingChallenge.__table__.c


def test_upgrade_scopes_admin_grants_and_enables_rebind_with_rls() -> None:
    joined = "\n".join(_run("upgrade"))

    assert "DROP CONSTRAINT uq_telegram_binding_user ON telegram_binding" in joined
    assert "CREATE INDEX uq_telegram_binding_active_user ON telegram_binding" in joined
    assert "CREATE INDEX uq_telegram_binding_active_membership ON telegram_binding" in joined
    for table in ("telegram_link_request", "telegram_binding_challenge"):
        assert f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY' in joined
        assert f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY' in joined
        assert f'CREATE POLICY workspace_isolation ON "{table}" TO crypidian_admin_web' in joined
    assert "GRANT UPDATE (revoked_at) ON TABLE telegram_binding TO crypidian_admin_web" in joined
    assert "GRANT UPDATE ON TABLE telegram_binding TO crypidian_admin_web" not in joined
    assert "crypidian_trading_runtime" not in joined


def test_upgrade_limits_telegram_ledger_updates_to_lifecycle_columns() -> None:
    joined = "\n".join(_run("upgrade"))

    assert (
        "GRANT UPDATE (expires_at, consumed_at) ON TABLE telegram_link_request "
        "TO crypidian_admin_web"
    ) in joined
    assert (
        "GRANT UPDATE (consumed_at) ON TABLE telegram_binding_challenge TO crypidian_admin_web"
    ) in joined
    assert 'GRANT SELECT, INSERT, UPDATE ON TABLE "telegram_link_request"' not in joined
    assert 'GRANT SELECT, INSERT, UPDATE ON TABLE "telegram_binding_challenge"' not in joined


def test_downgrade_refuses_to_delete_rebind_history_before_restoring_unique_constraint() -> None:
    statements = _run("downgrade")
    joined = "\n".join(statements)

    assert "MB07B_TELEGRAM_REBIND_HISTORY_PREVENTS_DOWNGRADE" in joined
    assert joined.index("MB07B_TELEGRAM_REBIND_HISTORY_PREVENTS_DOWNGRADE") < joined.index(
        "CREATE UNIQUE uq_telegram_binding_user"
    )
