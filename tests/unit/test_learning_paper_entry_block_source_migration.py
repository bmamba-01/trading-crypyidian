"""Kiểm hợp đồng cột và quyền của hàm đọc dữ liệu chặn PAPER."""

from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

REVISION = "f6c8e0a2b4da"
GUARD = "crypidian_paper_entry_block_reader"
FUNCTION = "public.wp06_read_paper_entry_block_source(uuid,uuid)"


def migration_sql(action: str = "upgrade") -> str:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    scripts = ScriptDirectory(Path(__file__).resolve().parents[2] / "migrations")
    script = scripts.get_revision(REVISION)
    assert script is not None
    assert script.down_revision == "f6c8e0a2b4d9"
    with Operations.context(context):
        getattr(script.module, action)()
    return output.getvalue()


def test_source_migration_preserves_runtime_admin_and_history_table_acls() -> None:
    sql = " ".join(migration_sql().split())
    grants = [stmt.strip() for stmt in sql.split(";") if stmt.strip().startswith("GRANT")]

    assert [grant for grant in grants if grant.endswith(" TO crypidian_admin_web")] == [
        f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO crypidian_admin_web"
    ]
    assert not any(
        grant.endswith((" TO crypidian_trading_runtime", " TO crypidian_scorer_history"))
        for grant in grants
    )
    assert [grant for grant in grants if grant.startswith("GRANT SELECT")] == [
        "GRANT SELECT (workspace_id, bot_id, id, status, candidate_id, decision_hash) "
        f"ON TABLE public.bot_decision TO {GUARD}",
        "GRANT SELECT (workspace_id, bot_id, bot_decision_id, bot_decision_hash, candidate_id, "
        "id, event_hash, mode, decision, deployment_event_id, deployment_revision, registry_id, "
        "registry_revision, active_pointer_version, scorer_artifact_id, scorer_artifact_hash, "
        f"source_rights_fingerprint) ON TABLE public.learning_score_event TO {GUARD}",
    ]
    assert f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC" in sql
    assert f"ALTER FUNCTION {FUNCTION} OWNER TO {GUARD}" in sql


def test_source_downgrade_removes_only_its_metadata_access() -> None:
    sql = " ".join(migration_sql("downgrade").split())
    assert sql.index(f"DROP FUNCTION {FUNCTION}") < sql.index(f"DROP ROLE {GUARD}")
    assert sql.count("DROP POLICY") == 4
    assert "DROP TABLE" not in sql
