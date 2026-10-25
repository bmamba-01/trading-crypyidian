"""Kiểm tra tĩnh migration MB-03; hành vi thật được chứng minh bằng Compose E2E."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "e3c5a7b9d2f4_shared_signal_evidence.py"
)
BOT_SCOPED = ("bot_decision_job", "bot_decision")
ALL_TABLES = ("shared_signal_evidence", *BOT_SCOPED)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mb03_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain_follows_mb02_bot_config_versions() -> None:
    module = _module()

    assert module.revision == "e3c5a7b9d2f4"
    # Tích hợp: MB-03 nối sau MB-02 để FK tới bot_config_version tồn tại.
    assert module.down_revision == "d2b4f6a8c1e3"


def test_every_new_table_enables_and_forces_rls_with_immutability_guard() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    module = _module()

    assert tuple(module.TABLES) == ALL_TABLES
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    for table in ALL_TABLES:
        assert f"{table}_immutable" in source or f"{table}_transition_guard" in source, table


def test_bot_scoped_policies_are_per_role_and_grants_are_least_privilege() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "TO crypidian_trading_runtime" in source
    assert "TO crypidian_admin_web" in source
    assert "UPDATE (status, completed_at) ON bot_decision_job TO crypidian_trading_runtime" in (
        source
    )
    assert "FROM PUBLIC, crypidian_app" in source
    assert not re.search(r"GRANT [^;\"]*TO crypidian_app", source)
    assert "crypidian_execution_gateway" not in source
    assert "crypidian_ingestion_worker" not in source


def test_observation_identity_is_unique_without_timestamps() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert '"workspace_id", "content_hash", name="uq_shared_signal_evidence_content"' in source


def test_downgrade_refuses_to_drop_decision_history() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "MB03_DOWNGRADE_REQUIRES_EXPORT_OF_DECISIONS" in source


def test_job_and_decision_reference_bot_config_version_of_same_bot() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    for table in ("bot_decision_job", "bot_decision"):
        assert f'name="fk_{table}_bot_config_version"' in source, table
    # FK ghép cùng workspace + bot: config của bot khác không thể được ghim vào job/quyết định.
    assert source.count('["workspace_id", "bot_id", "bot_config_version_id"]') == 2
