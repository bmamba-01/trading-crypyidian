"""Kiểm downgrade và ràng buộc UUID của ledger trên PostgreSQL cô lập."""

import os
import subprocess
from pathlib import Path
from secrets import token_hex
from uuid import uuid4

import pytest

from tests.e2e.test_wp06_learning_feature_vector_parity import isolated_compose_resources
from tests.unit.test_learning_scorer_paper_entry_block_migration import _migration_sql

_OWNER = "wp06_block_owner"
_TABLE = "public.learning_scorer_paper_entry_block"
_ZERO = "00000000-0000-0000-0000-000000000000"
_UUID_COLUMNS = (
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
_CONSTRAINT = "ck_learning_scorer_paper_entry_block_nonzero_uuids"


@pytest.fixture(scope="module")
def database():
    root = Path(__file__).resolve().parents[2]
    with isolated_compose_resources(root) as resources:
        base = [
            "docker",
            "compose",
            "--project-name",
            resources.project,
            "--project-directory",
            str(root),
            "-f",
            str(resources.compose_file),
        ]
        owner_password = token_hex(24)
        admin_password = (
            (resources.compose_file.parent / ".secrets/postgres_password").read_text().strip()
        )

        def psql(sql: str, *, owner: bool = True, expected_error: str | None = None) -> str:
            result = subprocess.run(
                [
                    *base,
                    "exec",
                    "-T",
                    "-e",
                    "PGPASSWORD",
                    "postgres",
                    "psql",
                    "-h",
                    "localhost",
                    "-U",
                    _OWNER if owner else "crypidian_admin",
                    "-d",
                    "crypidian",
                    "-Atq",
                    "-v",
                    "ON_ERROR_STOP=1",
                ],
                cwd=root,
                env=os.environ | {"PGPASSWORD": owner_password if owner else admin_password},
                input=sql,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if expected_error is None:
                assert result.returncode == 0, result.stderr
            else:
                assert result.returncode != 0, result.stdout
                assert expected_error in result.stderr, result.stderr
            return result.stdout.strip()

        try:
            subprocess.run(
                [*base, "up", "-d", "--wait", "postgres"],
                cwd=root,
                check=True,
                capture_output=True,
                timeout=180,
            )
            psql(
                f"""
                CREATE ROLE {_OWNER} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{owner_password}';
                CREATE ROLE crypidian_admin_web NOLOGIN;
                CREATE ROLE crypidian_trading_runtime NOLOGIN;
                CREATE ROLE crypidian_scorer_history LOGIN NOINHERIT;
                GRANT CREATE ON SCHEMA public TO {_OWNER};
                """,
                owner=False,
            )
            # Chỉ dựng khóa cha mà DDL d9 cần, rồi nạp đúng SQL do Alembic phát.
            psql(
                """
                CREATE TABLE public.bot_decision (
                    workspace_id uuid, bot_id uuid, id uuid,
                    PRIMARY KEY (workspace_id, bot_id, id)
                );
                CREATE TABLE public.learning_score_event (
                    workspace_id uuid, id uuid, bot_id uuid, bot_decision_id uuid,
                    PRIMARY KEY (workspace_id, id)
                );
                CREATE TABLE public.user_membership (
                    workspace_id uuid, user_id uuid, PRIMARY KEY (workspace_id, user_id)
                );
                """
            )
            psql("BEGIN;\n" + _migration_sql() + "\nCOMMIT;")
            yield psql
        finally:
            subprocess.run(
                [*base, "down", "--volumes", "--remove-orphans"],
                cwd=root,
                check=True,
                capture_output=True,
                timeout=180,
            )


def _insert_sql(*, zero_column: str | None = None) -> str:
    values = ", ".join(
        f"'{_ZERO if column == zero_column else uuid4()}'" for column in _UUID_COLUMNS
    )
    return f"""
        INSERT INTO {_TABLE} (
            {", ".join(_UUID_COLUMNS)}, bot_decision_hash, candidate_id, score_event_hash,
            scorer_artifact_hash, deployment_revision, registry_revision, active_pointer_version,
            reason, mfa_verified_at, created_at
        ) VALUES (
            {values}, repeat('a', 64), repeat('b', 64), repeat('c', 64), repeat('d', 64),
            1, 1, 1, 'Lý do thử nghiệm', now(), now()
        )
    """


@pytest.mark.parametrize("column", _UUID_COLUMNS)
def test_postgres_rejects_zero_uuid_before_inserting_ledger_row(database, column: str) -> None:
    # Replica chỉ bỏ FK cho dữ liệu tổng hợp. PostgreSQL vẫn kiểm mọi CHECK.
    database(
        f"BEGIN; SET LOCAL session_replication_role = replica; {_insert_sql(zero_column=column)};"
        "ROLLBACK;",
        owner=False,
        expected_error=_CONSTRAINT,
    )


def test_postgres_owner_downgrade_preserves_populated_forced_rls_ledger(database) -> None:
    assert (
        database(
            "SELECT has_column_privilege('crypidian_scorer_history', "
            "'public.learning_score_event', 'bot_decision_id', 'SELECT');"
        )
        == "t"
    )
    assert (
        database(
            f"""
        SELECT session_user = '{_OWNER}' AND current_user = '{_OWNER}'
               AND NOT rolsuper AND NOT rolbypassrls
               AND pg_get_userbyid(relowner) = current_user AND relforcerowsecurity
        FROM pg_roles, pg_class
        WHERE rolname = current_user AND pg_class.oid::regclass = '{_TABLE}'::regclass;
        """
        )
        == "t"
    )
    downgrade = "BEGIN;\n" + _migration_sql("downgrade") + "\nCOMMIT;"
    assert (
        database(downgrade.replace("COMMIT;", "SELECT current_setting('row_security'); COMMIT;"))
        == "on"
    )
    assert database(f"SELECT to_regclass('{_TABLE}') IS NULL;") == "t"
    assert (
        database(
            "SELECT NOT has_column_privilege('crypidian_scorer_history', "
            "'public.learning_score_event', 'bot_decision_id', 'SELECT');"
        )
        == "t"
    )
    database("BEGIN;\n" + _migration_sql() + "\nCOMMIT;")
    database(
        f"BEGIN; SET LOCAL session_replication_role = replica; {_insert_sql()}; COMMIT;",
        owner=False,
    )
    assert database(f"SELECT count(*) FROM {_TABLE};") == "0"
    database(
        f"BEGIN; SET LOCAL row_security = off; SELECT EXISTS (SELECT 1 FROM {_TABLE});",
        expected_error="query would be affected by row-level security policy",
    )
    database(downgrade, expected_error="WP06_PAPER_ENTRY_BLOCK_DOWNGRADE_REQUIRES_EMPTY_TABLE")
    assert database(f"SELECT count(*) FROM {_TABLE};", owner=False) == "1"
    assert (
        database(f"SELECT relforcerowsecurity FROM pg_class WHERE oid = '{_TABLE}'::regclass;")
        == "t"
    )
