"""Unit test cho migration d4e5f6a7b8c9_wp05_source_item_storage."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from trading_bot.adapters.database.models import (
    SourceItemRevisionRecord,
    SourceTakedownRecord,
)

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "d4e5f6a7b8c9"


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

    @staticmethod
    def f(name: str) -> str:
        return name

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


def test_source_item_storage_models_have_expected_constraints_and_foreign_keys() -> None:
    rev_table = SourceItemRevisionRecord.__table__
    td_table = SourceTakedownRecord.__table__

    assert rev_table.c.workspace_id.foreign_keys
    assert isinstance(
        rev_table.c.canonical_payload.type.dialect_impl(postgresql.dialect()),
        JSONB,
    )
    assert not {"api_key", "secret", "credential", "token"} & set(rev_table.c.keys())

    # Kiểm tra unique constraints trên revisions
    unique_col_sets = {
        tuple(sorted(col.name for col in constraint.columns))
        for constraint in rev_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("item_id", "revision_id", "source_id", "workspace_id") in unique_col_sets
    assert ("item_id", "sequence", "source_id", "workspace_id") in unique_col_sets

    # Kiểm tra check constraints trên revisions
    check_names = {
        constraint.name.replace("ck_research_source_item_revisions_", "")
        for constraint in rev_table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }
    assert {
        "source_item_revision_sequence_positive",
        "source_item_revision_source_id_valid",
        "source_item_revision_item_id_valid",
        "source_item_revision_revision_id_valid",
        "source_item_revision_policy_id_valid",
        "source_item_revision_hash_valid",
        "source_item_revision_predecessor_valid",
        "source_item_revision_available_not_before_observed",
        "source_item_revision_published_not_after_observed",
        "source_item_revision_edited_not_after_observed",
        "source_item_revision_edited_not_before_published",
        "source_item_revision_canonical_url_valid",
    } <= check_names

    # Kiểm tra RESTRICT ondelete foreign key trên revisions và takedowns
    rev_fk = next(
        constraint
        for constraint in rev_table.foreign_key_constraints
        if [element.parent.name for element in constraint.elements] == ["workspace_id"]
    )
    assert rev_fk.ondelete == "RESTRICT"

    td_fk = next(
        constraint
        for constraint in td_table.foreign_key_constraints
        if [element.parent.name for element in constraint.elements] == ["workspace_id"]
    )
    assert td_fk.ondelete == "RESTRICT"

    # Kiểm tra check constraints trên takedowns
    td_check_names = {
        constraint.name.replace("ck_research_source_takedowns_", "")
        for constraint in td_table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }
    assert {
        "source_takedown_source_id_valid",
        "source_takedown_item_id_valid",
        "source_takedown_reason_present",
        "source_takedown_requested_by_present",
        "source_takedown_effective_not_before_requested",
    } <= td_check_names


def test_upgrade_applies_workspace_rls_append_only_and_role_separation() -> None:
    statements = _run("upgrade")
    joined = "\n".join(statements)

    for table in ("research_source_item_revisions", "research_source_takedowns"):
        assert f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY' in joined
        assert f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY' in joined
        assert f'CREATE POLICY workspace_isolation ON "{table}"' in joined

    assert "current_setting('app.workspace_id', true)" in joined
    assert "TO crypidian_admin_web, crypidian_ingestion_worker" in joined

    # Kiểm tra trigger và function append-only
    assert "CREATE FUNCTION public.reject_source_item_revision_mutation()" in joined
    assert "WP05_SOURCE_ITEM_REVISION_IMMUTABLE" in joined
    assert (
        'CREATE TRIGGER "research_source_item_revisions_append_only" '
        'BEFORE UPDATE OR DELETE ON "research_source_item_revisions"'
    ) in joined
    assert (
        "REVOKE ALL ON FUNCTION public.reject_source_item_revision_mutation() FROM PUBLIC" in joined
    )

    # Kiểm tra phân quyền tối thiểu (least privilege)
    assert "REVOKE ALL PRIVILEGES ON TABLE" in joined
    assert (
        'GRANT SELECT, INSERT ON TABLE "research_source_item_revisions" '
        "TO crypidian_admin_web, crypidian_ingestion_worker"
    ) in joined
    assert (
        'GRANT SELECT, INSERT, UPDATE ON TABLE "research_source_takedowns" TO crypidian_admin_web'
    ) in joined
    assert (
        'GRANT SELECT, INSERT ON TABLE "research_source_takedowns" TO crypidian_ingestion_worker'
    ) in joined
    assert 'UPDATE ON TABLE "research_source_item_revisions"' not in joined
    assert 'DELETE ON TABLE "research_source_item_revisions"' not in joined


def test_downgrade_drops_policies_triggers_tables_and_indexes() -> None:
    statements = _run("downgrade")
    joined = "\n".join(statements)

    assert "WP05_SOURCE_ITEM_STORAGE_DOWNGRADE_BLOCKED" in joined
    assert 'DROP TRIGGER "research_source_item_revisions_append_only"' in joined
    assert 'DROP POLICY workspace_isolation ON "research_source_item_revisions"' in joined
    assert 'DROP POLICY workspace_isolation ON "research_source_takedowns"' in joined
    assert "DROP FUNCTION public.reject_source_item_revision_mutation()" in joined
    assert "DROP INDEX ix_research_source_takedowns_lookup" in joined
    assert "DROP INDEX uq_research_source_takedowns_active" in joined
    assert "DROP TABLE research_source_takedowns" in joined
    assert "DROP INDEX ix_research_source_item_revisions_available" in joined
    assert "DROP INDEX ix_research_source_item_revisions_lookup" in joined
    assert "DROP TABLE research_source_item_revisions" in joined
