"""Kiểm kê fixture PostgreSQL và transaction thực của migration d4 trong Compose."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import DBAPIError, OperationalError

ROLE = "crypidian_admin_web"
TABLE = "public.wp06_catalog_fixture"
SNAPSHOT = "public.wp06_admin_web_acl_snapshot"
sys.path.insert(0, "/app")
D4 = importlib.import_module("migrations.versions.f0a2c4e6b8d4_wp06_admin_web_least_privilege")


def engine() -> sa.Engine:
    return sa.create_engine(
        sa.URL.create(
            "postgresql+psycopg",
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip(),
            host=os.environ["TRADING_BOT_DATABASE_HOST"],
            database=os.environ["TRADING_BOT_DATABASE_NAME"],
        )
    )


def baseline(connection: sa.Connection, *, expected_count: int = 295, role: str = ROLE) -> str:
    """So grant, grantor, grant option và membership có thật ngay sau khi downgrade."""
    sources = (
        ("pg_class", "oid", "relacl"),
        ("pg_attribute", "(attrelid, attnum)", "attacl"),
        ("pg_proc", "oid", "proacl"),
        ("pg_database", "oid", "datacl"),
    )
    rows = []
    for catalog, identity, acl in sources:
        rows.extend(
            tuple(row)
            for row in connection.exec_driver_sql(
                f"SELECT '{catalog}', {identity}::text, x.grantor, x.privilege_type, "
                f"x.is_grantable FROM pg_catalog.{catalog}, "
                f"pg_catalog.aclexplode({acl}) x WHERE x.grantee = '{role}'::regrole"
            )
        )
    assert len(rows) == expected_count
    memberships = connection.exec_driver_sql(
        f"SELECT roleid, grantor, admin_option, inherit_option, set_option "
        f"FROM pg_catalog.pg_auth_members WHERE member = '{role}'::regrole ORDER BY roleid, grantor"
    ).all()
    return hashlib.sha256(
        json.dumps([sorted(rows), [tuple(row) for row in memberships]]).encode()
    ).hexdigest()


def assert_parent(connection: sa.Connection) -> None:
    assert connection.exec_driver_sql(
        f"SELECT pg_catalog.to_regrole('crypidian_admin_web_safe') IS NULL "
        f"AND pg_catalog.to_regclass('{SNAPSHOT}') IS NULL "
        f"AND pg_catalog.pg_has_role('{ROLE}', 'crypidian_app', 'USAGE') "
        "AND (SELECT version_num FROM public.alembic_version) = 'f0a2c4e6b8d3'"
    ).scalar_one()


def expect_refusal(operation: Callable[[], None], marker: str) -> None:
    try:
        operation()
    except RuntimeError as error:
        if not str(error).startswith(marker):
            raise
        return
    raise AssertionError(f"Migration chấp nhận fixture cần từ chối với {marker}")


def check_preflight(db: sa.Engine) -> None:
    cases = (
        (
            "system_column",
            f"GRANT SELECT (ctid) ON {TABLE} TO {ROLE}",
            "WP06_ADMIN_ACL_UNCLASSIFIED:column|public|wp06_catalog_fixture.ctid",
        ),
        (
            "other_database",
            f"GRANT CONNECT ON DATABASE postgres TO {ROLE}",
            "WP06_ADMIN_ACL_UNCLASSIFIED:database||postgres",
        ),
        (
            "collation_owner",
            f"ALTER COLLATION public.wp06_catalog_fixture OWNER TO {ROLE}",
            "WP06_ADMIN_OWNS_OBJECTS_UNEXPECTED",
        ),
        (
            "schema_default",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT UPDATE ON TABLES TO PUBLIC",
            "WP06_SNAPSHOT_DEFAULT_ACL_UNEXPECTED",
        ),
        (
            "global_default",
            "ALTER DEFAULT PRIVILEGES GRANT INSERT ON TABLES TO crypidian_app",
            "WP06_SNAPSHOT_DEFAULT_ACL_UNEXPECTED",
        ),
    )
    for name, setup, expected in cases:
        with db.connect() as conn:
            conn.exec_driver_sql(f"CREATE TABLE {TABLE} (id integer)")
            conn.exec_driver_sql('CREATE COLLATION public.wp06_catalog_fixture FROM "C"')
            conn.exec_driver_sql(setup)
            count = 296 if name in {"other_database", "system_column"} else 295
            before = baseline(conn, expected_count=count)
            savepoint = conn.begin_nested()
            with Operations.context(MigrationContext.configure(conn)):
                expect_refusal(D4.upgrade, expected)
                assert_parent(conn)
                assert baseline(conn, expected_count=count) == before
            savepoint.rollback()
            assert_parent(conn)
            assert baseline(conn, expected_count=count) == before


def check_foreign_grant(db: sa.Engine) -> None:
    foreign = sa.create_engine(db.url.set(database="postgres"))
    try:
        with foreign.begin() as other:
            other.exec_driver_sql(f"CREATE TABLE {TABLE} (id integer)")
            other.exec_driver_sql(f"GRANT SELECT ON {TABLE} TO {ROLE}")
        with db.connect() as conn, Operations.context(MigrationContext.configure(conn)):
            expect_refusal(D4.upgrade, "WP06_ADMIN_FOREIGN_OBJECT_ACL_UNEXPECTED")
            assert_parent(conn)
        with foreign.connect() as other:
            assert other.exec_driver_sql(
                f"SELECT has_table_privilege('{ROLE}', '{TABLE}', 'SELECT')"
            ).scalar_one()
    finally:
        with foreign.begin() as other:
            other.exec_driver_sql(f"DROP TABLE IF EXISTS {TABLE}")
        foreign.dispose()


def check_reader_membership(db: sa.Engine) -> None:
    with db.connect() as conn, Operations.context(MigrationContext.configure(conn)):
        conn.exec_driver_sql("CREATE ROLE wp06_fixture_reader_login LOGIN")
        conn.exec_driver_sql(
            "GRANT crypidian_admin_report_reader TO wp06_fixture_reader_login "
            "WITH INHERIT FALSE, SET TRUE"
        )
        expect_refusal(D4.upgrade, "WP06_ADMIN_REPORT_READER_MEMBERSHIP_UNEXPECTED")
        assert_parent(conn)


def check_global_writer(db: sa.Engine, *, downgrade: bool = False) -> None:
    with db.connect() as conn, Operations.context(MigrationContext.configure(conn)):
        conn.exec_driver_sql("CREATE ROLE wp06_fixture_group NOLOGIN")
        conn.exec_driver_sql("CREATE ROLE wp06_fixture_writer LOGIN")
        conn.exec_driver_sql(
            "GRANT pg_write_all_data TO wp06_fixture_group WITH INHERIT TRUE, SET FALSE"
        )
        conn.exec_driver_sql(
            "GRANT wp06_fixture_group TO wp06_fixture_writer WITH INHERIT FALSE, SET TRUE"
        )
        expect_refusal(
            D4.downgrade if downgrade else D4.upgrade, "WP06_SNAPSHOT_WRITE_ACL_UNEXPECTED"
        )
        if not downgrade:
            assert_parent(conn)


def check_partial_rollback(db: sa.Engine, *, downgrade: bool = False) -> None:
    safe = "crypidian_admin_web_safe"

    def state(conn):
        if not downgrade:
            assert_parent(conn)
            return baseline(conn)
        rows = (
            conn.exec_driver_sql(f"SELECT to_jsonb(s)::text FROM {SNAPSHOT} s ORDER BY grant_sql")
            .scalars()
            .all()
        )
        policies = conn.exec_driver_sql(
            "SELECT polrelid, polname FROM pg_catalog.pg_policy ORDER BY polrelid, polname"
        ).all()
        return (
            baseline(conn, expected_count=0),
            baseline(conn, expected_count=274, role=safe),
            rows,
            policies,
        )

    suffix = f" FROM {safe if downgrade else ROLE}"
    with db.connect() as observer, db.connect() as migrate:
        before = state(observer)
        observer.commit()

        def stop_after_revoke(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("REVOKE ") and statement.endswith(suffix):
                raise RuntimeError("WP06_FIXTURE_AFTER_REVOKE")

        sa.event.listen(migrate, "after_cursor_execute", stop_after_revoke)
        with Operations.context(MigrationContext.configure(migrate)):
            expect_refusal(D4.downgrade if downgrade else D4.upgrade, "WP06_FIXTURE_AFTER_REVOKE")
        migrate.rollback()
        assert state(observer) == before


def check_restore_grantor(db: sa.Engine) -> None:
    with db.connect() as conn, Operations.context(MigrationContext.configure(conn)):
        conn.exec_driver_sql("CREATE ROLE wp06_fixture_owner NOLOGIN")
        conn.exec_driver_sql("ALTER TABLE public.user_account OWNER TO wp06_fixture_owner")
        savepoint = conn.begin_nested()
        expect_refusal(D4.downgrade, "WP06_ADMIN_ACL_UNCLASSIFIED:relation|public|user_account|")
        savepoint.rollback()
        assert conn.exec_driver_sql(
            "SELECT to_regrole('crypidian_admin_web_safe') IS NOT NULL"
        ).scalar_one()


def check_isolation(db: sa.Engine) -> None:
    for level in ("READ UNCOMMITTED", "REPEATABLE READ", "SERIALIZABLE"):
        with db.connect().execution_options(isolation_level=level) as conn:
            with Operations.context(MigrationContext.configure(conn)):
                if level == "READ UNCOMMITTED":
                    D4.upgrade()
                else:
                    expect_refusal(D4.upgrade, "WP06_CATALOG_READ_COMMITTED_REQUIRED")
                    assert_parent(conn)


def check_barrier(db: sa.Engine) -> None:
    with db.begin() as setup:
        setup.exec_driver_sql(f"CREATE TABLE {TABLE} (id integer)")
    try:
        with db.connect() as writer, db.connect() as migrate:
            writer.exec_driver_sql(f"GRANT SELECT (ctid) ON {TABLE} TO {ROLE}")
            migrate.exec_driver_sql("SET LOCAL lock_timeout = '1200ms'")
            with Operations.context(MigrationContext.configure(migrate)):
                try:
                    D4.upgrade()
                except DBAPIError as error:
                    if error.orig.diag.message_primary != "WP06_CATALOG_WRITER_IN_PROGRESS":
                        raise
                else:
                    raise AssertionError("Preflight bỏ qua transaction catalog chưa commit.")
            migrate.rollback()
            # Đợi đúng lúc d4 đang chờ XID rồi commit, để lần kiểm kê kế tiếp phải thấy grant.
            started = Event()

            def release_writer():
                with db.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                    started.set()
                    deadline = monotonic() + 5
                    while monotonic() < deadline:
                        waiting = observer.exec_driver_sql(
                            "SELECT EXISTS (SELECT FROM pg_catalog.pg_stat_activity "
                            "WHERE pid = %s AND wait_event = 'PgSleep')",
                            (migration_pid,),
                        ).scalar_one()
                        if waiting:
                            writer.commit()
                            return
                        sleep(0.01)
                    raise AssertionError("Không thấy migration đợi XID trước kiểm kê.")

            migration_pid = migrate.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            with ThreadPoolExecutor(max_workers=1) as pool:
                released = pool.submit(release_writer)
                assert started.wait(5)
                with Operations.context(MigrationContext.configure(migrate)):
                    expect_refusal(
                        D4.upgrade,
                        "WP06_ADMIN_ACL_UNCLASSIFIED:column|public|wp06_catalog_fixture.ctid",
                    )
                    assert_parent(migrate)
                released.result()
            migrate.rollback()
            writer.exec_driver_sql(f"REVOKE SELECT (ctid) ON {TABLE} FROM {ROLE}")
            writer.commit()

        blocked = []
        with db.connect() as migrate:

            def check_locks(conn, cursor, statement, parameters, context, executemany):
                if not statement.startswith("CREATE ROLE crypidian_admin_web_safe "):
                    return
                for sql in (
                    f"GRANT SELECT (ctid) ON {TABLE} TO {ROLE}",
                    f"GRANT crypidian_app TO {ROLE} WITH SET FALSE",
                ):
                    with db.connect() as writer:
                        writer.exec_driver_sql("SET LOCAL lock_timeout = '300ms'")
                        try:
                            writer.exec_driver_sql(sql)
                        except OperationalError as error:
                            if error.orig.sqlstate != "55P03":
                                raise
                            blocked.append(sql)

            sa.event.listen(migrate, "before_cursor_execute", check_locks)
            with Operations.context(MigrationContext.configure(migrate)):
                D4.upgrade()
            assert len(blocked) == 2, "ACL hoặc membership thay đổi được sau preflight."
    finally:
        with db.begin() as cleanup:
            cleanup.exec_driver_sql(f"DROP TABLE {TABLE}")


def check_snapshot(db: sa.Engine) -> None:
    cases = (
        (f"UPDATE {SNAPSHOT} SET consumer = 'fixture' WHERE kind = 'membership'", "MANIFEST"),
        (f"DELETE FROM {SNAPSHOT} WHERE kind = 'membership'", "MANIFEST"),
        (
            f"INSERT INTO {SNAPSHOT} (kind, object_identity, grantor_name, consumer, operation, "
            "disposition, grant_sql) SELECT kind, object_identity, grantor_name, consumer, "
            f"operation, disposition, grant_sql || ' ' FROM {SNAPSHOT} WHERE kind = 'membership'",
            "MANIFEST",
        ),
        (f"GRANT UPDATE (consumer) ON {SNAPSHOT} TO crypidian_app", "WRITE_ACL"),
    )
    for setup, reason in cases:
        with db.connect() as conn:
            conn.exec_driver_sql(setup)
            with Operations.context(MigrationContext.configure(conn)):
                expect_refusal(D4.downgrade, f"WP06_SNAPSHOT_{reason}_UNEXPECTED")
                assert conn.exec_driver_sql(
                    f"SELECT pg_catalog.pg_has_role('{ROLE}', "
                    "'crypidian_admin_web_safe', 'USAGE') AND EXISTS "
                    "(SELECT FROM pg_catalog.pg_policy WHERE "
                    "polname = 'wp06_manual_report_reader_account_select')"
                ).scalar_one()


if __name__ == "__main__":
    db = engine()
    action = sys.argv[1]
    if action == "baseline":
        with db.connect() as connection:
            print(baseline(connection))
    else:
        {
            "preflight": check_preflight,
            "isolation": check_isolation,
            "barrier": check_barrier,
            "snapshot": check_snapshot,
            "foreign_grant": check_foreign_grant,
            "reader_membership": check_reader_membership,
            "global_writer": check_global_writer,
            "snapshot_writer": lambda database: check_global_writer(database, downgrade=True),
            "upgrade_fault": check_partial_rollback,
            "downgrade_fault": lambda database: check_partial_rollback(database, downgrade=True),
            "restore_grantor": check_restore_grantor,
        }[action](db)
