"""Model, migration và repository cho phiên bản cấu hình bot bất biến."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint

from trading_bot.adapters.database.models import Base, BotConfigRequest, BotConfigVersion

MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "d2b4f6a8c1e3_bot_config_versions.py"
)


def _unique_columns(table: object) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, UniqueConstraint)
    }


def _fk(table: object, columns: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    for constraint in table.foreign_key_constraints:  # type: ignore[attr-defined]
        if tuple(column.name for column in constraint.columns) == columns:
            return constraint.referred_table.name, tuple(
                element.column.name for element in constraint.elements
            )
    return None


def test_bot_config_version_is_bot_scoped_with_composite_lineage() -> None:
    table = BotConfigVersion.__table__

    assert _fk(table, ("workspace_id", "bot_id", "account_id")) == (
        "bot_instance",
        ("workspace_id", "id", "trading_account_id"),
    )
    assert _fk(table, ("workspace_id", "bot_id", "parent_version_id")) == (
        "bot_config_version",
        ("workspace_id", "bot_id", "id"),
    )
    assert _fk(table, ("workspace_id", "bot_id", "source_version_id")) == (
        "bot_config_version",
        ("workspace_id", "bot_id", "id"),
    )
    assert _fk(table, ("workspace_id", "base_settings_version_id")) == (
        "workspace_settings_version",
        ("workspace_id", "id"),
    )
    for column in ("created_by", "updated_by", "approved_by"):
        assert _fk(table, ("workspace_id", column)) == (
            "user_membership",
            ("workspace_id", "id"),
        )
    unique = _unique_columns(table)
    assert ("workspace_id", "bot_id", "version") in unique
    assert ("workspace_id", "bot_id", "id") in unique
    assert not table.c.bot_id.nullable
    assert not table.c.account_id.nullable
    assert not table.c.overlay_hash.nullable
    assert not table.c.base_settings_version_id.nullable


def test_bot_config_version_has_indexes_for_rls_and_effective_lookup() -> None:
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in BotConfigVersion.__table__.indexes
    }

    assert indexes["ix_bot_config_version_bot_scope"] == ("workspace_id", "bot_id", "account_id")
    assert indexes["ix_bot_config_version_effective"] == (
        "workspace_id",
        "bot_id",
        "status",
        "effective_at",
    )


def test_bot_config_version_checks_status_lifecycle_and_hashes() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in BotConfigVersion.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "status IN ('DRAFT', 'PUBLISHED')" in checks["ck_bot_config_version_status_valid"]
    lifecycle = checks["ck_bot_config_version_publication_consistent"]
    assert "approved_by IS NOT NULL" in lifecycle
    assert "effective_at IS NULL" in lifecycle
    assert "overlay_hash" in checks["ck_bot_config_version_overlay_hash_sha256"]
    assert "base_settings_hash" in checks["ck_bot_config_version_base_hash_sha256"]


def test_bot_config_request_scopes_idempotency_key_per_bot_operation() -> None:
    table = BotConfigRequest.__table__

    assert ("workspace_id", "bot_id", "operation", "idempotency_key") in _unique_columns(table)
    assert _fk(table, ("workspace_id", "bot_id", "account_id")) == (
        "bot_instance",
        ("workspace_id", "id", "trading_account_id"),
    )
    assert _fk(table, ("workspace_id", "bot_id", "bot_config_version_id")) == (
        "bot_config_version",
        ("workspace_id", "bot_id", "id"),
    )
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "draft.create" in checks["ck_bot_config_request_operation_valid"]


def test_metadata_registers_new_bot_config_tables() -> None:
    assert {"bot_config_version", "bot_config_request"} <= set(Base.metadata.tables)


def test_migration_chains_after_mb01_and_protects_history() -> None:
    spec = importlib.util.spec_from_file_location("mb02_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = MIGRATION.read_text(encoding="utf-8")

    assert module.revision == "d2b4f6a8c1e3"
    assert module.down_revision == "c7a90d3f8e21"
    for statement in (
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "TO crypidian_trading_runtime",
        "TO crypidian_admin_web",
        "status = 'PUBLISHED'",
        "MB02_BOT_CONFIG_VERSION_IMMUTABLE",
        "MB02_BOT_CONFIG_REQUEST_APPEND_ONLY",
        "MB02_DOWNGRADE_REQUIRES_EXPORT_OF_BOT_CONFIG",
        "REVOKE ALL ON bot_config_version, bot_config_request FROM PUBLIC, crypidian_app",
    ):
        assert statement in source, statement
