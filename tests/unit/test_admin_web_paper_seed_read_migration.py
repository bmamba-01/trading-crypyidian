"""Migration cho admin-web đọc vốn ban đầu PAPER: chỉ SELECT theo cột, dựa vào RLS sẵn có."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/d2e4f6a8b0c1_admin_web_paper_seed_read.py"


def _migration():
    assert MIGRATION.exists(), "Thiếu migration cấp quyền đọc vốn ban đầu cho admin-web"
    spec = importlib.util.spec_from_file_location("admin_web_paper_seed_read", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(step: str) -> list[str]:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: statements.append(" ".join(str(sql).split())))
    getattr(module, step)()
    return statements


def test_migration_chains_after_current_head_and_grants_only_safe_columns() -> None:
    migration = _migration()
    upgrade = _sql("upgrade")

    assert migration.down_revision == "c7e9a1b3d5f8"
    assert len(upgrade) == 1
    grant = upgrade[0]
    assert grant.startswith("GRANT SELECT (")
    assert grant.endswith("ON TABLE public.paper_account_seed TO crypidian_admin_web")
    for column in ("workspace_id", "bot_id", "quote_asset", "initial_equity", "created_at"):
        assert column in grant
    # Không mở cột định danh nội bộ/idempotency và không cấp ghi.
    for column in ("idempotency_key", "source_settings_hash", "trading_account_id"):
        assert column not in grant
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert privilege not in grant


def test_downgrade_revokes_exactly_the_granted_columns() -> None:
    upgrade = _sql("upgrade")[0]
    downgrade = _sql("downgrade")

    assert len(downgrade) == 1
    revoke = downgrade[0]
    assert revoke.startswith("REVOKE SELECT (")
    assert revoke.split("(", 1)[1].split(")", 1)[0] == upgrade.split("(", 1)[1].split(")", 1)[0]
    assert revoke.endswith("ON TABLE public.paper_account_seed FROM crypidian_admin_web")
