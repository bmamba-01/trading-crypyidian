"""Kiểm DDL và đặc quyền mà migration ledger gửi cho PostgreSQL."""

import re
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

_TABLE = "public.learning_scorer_paper_entry_block"
_SCORE_EVENT = "public.learning_score_event"


def _migration_sql(action: str = "upgrade") -> str:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    scripts = ScriptDirectory(Path(__file__).resolve().parents[2] / "migrations")
    script = scripts.get_revision("f6c8e0a2b4d9")
    assert script is not None
    assert script.down_revision == "f6c8e0a2b4d8"
    with Operations.context(context):
        getattr(script.module, action)()
    return " ".join(output.getvalue().split())


def test_learning_block_reader_acl_matches_only_required_history_columns() -> None:
    sql = _migration_sql()
    reader = "crypidian_scorer_history"

    grants = [stmt.strip() for stmt in sql.split(";") if stmt.strip().startswith("GRANT")]
    assert [stmt for stmt in grants if stmt.endswith(f" TO {reader}")] == [
        "GRANT SELECT (workspace_id, bot_decision_id, score_event_id, actor_user_id, "
        f"mfa_verified_at, reason, created_at) ON TABLE {_TABLE} TO {reader}",
        f"GRANT SELECT (bot_id, bot_decision_id) ON TABLE {_SCORE_EVENT} TO {reader}",
    ]
    assert f"GRANT SELECT ON TABLE {_SCORE_EVENT} TO {reader}" not in grants
    assert f"GRANT SELECT, INSERT ON TABLE {_TABLE} TO crypidian_admin_web" in grants
    assert (
        "GRANT SELECT (workspace_id, bot_decision_id) "
        f"ON TABLE {_TABLE} TO crypidian_trading_runtime" in grants
    )


def test_learning_block_migration_enforces_workspace_rls_and_immutable_writes() -> None:
    sql = _migration_sql()

    assert f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY" in sql
    workspace = (
        "workspace_id = NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid"
    )
    assert f"AS RESTRICTIVE FOR ALL TO PUBLIC USING ({workspace}) WITH CHECK ({workspace})" in sql
    assert f"BEFORE UPDATE OR DELETE ON {_TABLE}" in sql
    assert f"BEFORE TRUNCATE ON {_TABLE}" in sql
    assert "UNIQUE (workspace_id, bot_decision_id)" in sql
    assert "UNIQUE (workspace_id, idempotency_key)" in sql


def test_downgrade_checks_unfiltered_rows_under_exclusive_lock() -> None:
    sql = _migration_sql("downgrade")

    lock = sql.index(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE")
    fail_closed = sql.index("SET LOCAL row_security = off")
    check = sql.index(f"IF EXISTS (SELECT 1 FROM {_TABLE})")
    drop = sql.index(f"DROP TABLE {_TABLE}")
    assert lock < fail_closed < check < drop
    assert (
        f"REVOKE SELECT (bot_id, bot_decision_id) ON TABLE {_SCORE_EVENT} FROM "
        "crypidian_scorer_history"
    ) in sql


def test_learning_block_rejects_zero_uuid_in_all_fields() -> None:
    sql = _migration_sql()
    column_lhs = (
        "id",
        "workspace_id",
        "bot_id",
        "bot_decision_id",
        "score_event_id",
        "deployment_event_id",
        "registry_id",
        "scorer_artifact_id",
        "actor_user_id",
        "idempotency_key",
    )
    check = re.search(r"'00000000-0000-0000-0000-000000000000'::uuid NOT IN \(([^)]+)\)", sql)
    assert check is not None
    assert {column.strip() for column in check[1].split(",")} == set(column_lhs)
