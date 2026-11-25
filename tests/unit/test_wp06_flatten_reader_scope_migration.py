from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "f0a2c4e6b8d6_wp06_flatten_reader_scope.py"
)


def test_flatten_reader_scope_migration_chains_after_admin_telemetry() -> None:
    assert MIGRATION.is_file(), "Thiếu migration giới hạn PAPER checkpoint cho report-reader."
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "f0a2c4e6b8d6"' in source
    assert 'down_revision: str | None = "f0a2c4e6b8d5"' in source


def test_flatten_reader_scope_is_read_only_and_uses_current_bot_authorization() -> None:
    assert MIGRATION.is_file(), "Thiếu migration giới hạn PAPER checkpoint cho report-reader."
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'TABLE = "paper_execution_checkpoint"' in source
    assert 'READER = "crypidian_admin_report_reader"' in source
    assert 'op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {READER}")' in source
    assert 'f"FOR SELECT TO {READER} USING ("' in source
    assert "wp06_admin_report_reader_can_access(workspace_id, bot_id)" in source
    assert "GRANT INSERT" not in source
    assert "GRANT UPDATE" not in source
    assert "GRANT DELETE" not in source


def test_admin_login_never_receives_checkpoint_table_privilege() -> None:
    acl_migration = MIGRATION.parents[0] / "f0a2c4e6b8d4_wp06_admin_web_least_privilege.py"
    assert acl_migration.is_file(), "Thiếu migration tách quyền database admin-web."
    source = acl_migration.read_text(encoding="utf-8")

    assert '"paper_execution_checkpoint"' in source
    assert "public.paper_execution_checkpoint" in source
    assert "has_table_privilege" in source
