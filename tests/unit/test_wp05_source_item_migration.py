"""Kiểm hợp đồng migration metadata/RLS; SQL thật được chạy ở Compose E2E."""

import importlib.util
from pathlib import Path

from sqlalchemy import UniqueConstraint

from trading_bot.adapters.database.models import SourceItemRevisionRecord, SourceItemTakedownRecord


def migration():
    path = next((Path(__file__).parents[2] / "migrations/versions").glob("c9e1f3a5b7d0_*.py"), None)
    assert path is not None, "Migration source-item metadata chưa được triển khai."
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_linear_and_metadata_only_schema_keeps_item_scope():
    assert migration().down_revision == "b8d0f1a3c5e7"
    table = SourceItemRevisionRecord.__table__
    assert not {"body", "excerpt", "embedding", "blob", "payload"} & set(table.c.keys())
    assert any(
        isinstance(constraint, UniqueConstraint)
        and set(constraint.columns.keys())
        == {"workspace_id", "source_id", "item_id", "revision_id"}
        for constraint in table.constraints
    )
    assert SourceItemTakedownRecord.__table__.c.workspace_id.foreign_keys


def test_upgrade_preserves_rls_and_separates_revision_and_takedown_writers():
    module = migration()
    statements = []

    class Recorder:
        def execute(self, sql):
            statements.append(str(sql))

    module.op = Recorder()
    module.upgrade()
    sql = "\n".join(statements)
    for table in ("source_item_revision", "source_item_takedown"):
        assert f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY' in sql
        assert f'BEFORE UPDATE OR DELETE ON "{table}"' in sql
    assert "FOR INSERT TO crypidian_ingestion_worker" in sql
    assert "FOR INSERT TO crypidian_admin_web" in sql
    assert "WP05_ITEM_PREDECESSOR_INVALID" in sql
    assert "WP05_ITEM_HASH_INVALID" in sql
    assert "WP05_ITEM_TAKEN_DOWN" in sql


def test_database_knowledge_timestamp_is_persisted_outside_source_metadata_hash():
    assert "recorded_at" in SourceItemRevisionRecord.__table__.columns
    assert SourceItemRevisionRecord.__table__.c.recorded_at.nullable is False


def test_recording_boundary_migration_is_linear():
    path = next((Path(__file__).parents[2] / "migrations/versions").glob("d0f2a4c6e8b1_*.py"), None)
    assert path is not None, "Migration thời điểm DB ghi nhận chưa được triển khai."
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "c9e1f3a5b7d0"
