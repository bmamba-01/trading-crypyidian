"""Probe seed/check/cleanup cho transaction fail-closed quyền admin-web."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROLE = "crypidian_admin_web"
TABLE = "wp06_unclassified_acl_probe"
SNAPSHOT = "wp06_admin_web_acl_snapshot"
MANIFEST_PATH = Path("/app/migrations/wp06_admin_safe_acl_manifest.json")


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    password_path = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
    return psycopg.connect(
        host=os.environ["TRADING_BOT_DATABASE_HOST"],
        dbname=os.environ["TRADING_BOT_DATABASE_NAME"],
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password_path.read_text(encoding="utf-8").strip(),
        autocommit=autocommit,
        row_factory=dict_row,
    )


def seed() -> None:
    with connect(autocommit=True) as connection:
        connection.execute(f"CREATE TABLE public.{TABLE} (id integer)")
        connection.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO {ROLE}")


def check() -> None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                pg_catalog.pg_has_role(%s, 'crypidian_app', 'USAGE') AS app_membership,
                EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s)
                    AS safe_role_exists,
                pg_catalog.to_regclass(%s) IS NOT NULL AS snapshot_exists,
                EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) AS acl
                    WHERE n.nspname = 'public'
                      AND c.relname = %s
                      AND acl.grantee = (
                          SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                      )
                      AND acl.privilege_type = 'SELECT'
                ) AS direct_grant_preserved,
                (SELECT version_num FROM public.alembic_version) AS revision
            """,
            (
                ROLE,
                "crypidian_admin_web_safe",
                f"public.{SNAPSHOT}",
                TABLE,
                ROLE,
            ),
        ).fetchone()
    print(json.dumps(row, sort_keys=True))


def cleanup() -> None:
    with connect(autocommit=True) as connection:
        connection.execute(f"REVOKE SELECT ON TABLE public.{TABLE} FROM {ROLE}")
        connection.execute(f"DROP TABLE public.{TABLE}")


def verify_snapshot() -> None:
    manifest = [
        row
        for row in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if row["kind"] != "membership"
    ]
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT kind, object_identity, grantor_name, consumer, operation, disposition,
                   grant_sql, safe_grant_sql, safe_revoke_sql
            FROM public.{SNAPSHOT}
            WHERE kind <> 'membership'
            """
        ).fetchall()

    actual = {row["grant_sql"]: row for row in rows}
    expected = {entry["grant_sql"]: entry for entry in manifest}
    fields = (
        "kind",
        "object_identity",
        "grantor_name",
        "consumer",
        "operation",
        "disposition",
        "grant_sql",
        "safe_grant_sql",
        "safe_revoke_sql",
    )
    if actual.keys() != expected.keys():
        raise AssertionError("Snapshot ACL không khớp tập manifest đã duyệt.")
    for grant_sql, expected_row in expected.items():
        if any(actual[grant_sql][field] != expected_row.get(field) for field in fields):
            raise AssertionError(f"Snapshot ACL không khớp manifest: {grant_sql}")

    with connect() as connection:
        membership = connection.execute(
            f"""
            SELECT kind, object_identity, grantor_name, consumer, operation, disposition,
                   grant_sql, safe_grant_sql, safe_revoke_sql
            FROM public.{SNAPSHOT}
            WHERE kind = 'membership'
            """
        ).fetchone()
    expected_membership = {
        "kind": "membership",
        "object_identity": "crypidian_app",
        "grantor_name": "crypidian_admin",
        "consumer": "Quyền kế thừa ứng dụng admin-web trước khi tách role",
        "operation": "ROLE_MEMBERSHIP",
        "disposition": "revoke_membership",
        "grant_sql": "GRANT crypidian_app TO crypidian_admin_web "
        "WITH ADMIN FALSE, INHERIT TRUE, SET TRUE",
        "safe_grant_sql": None,
        "safe_revoke_sql": None,
    }
    if membership is None or any(
        membership[field] != expected for field, expected in expected_membership.items()
    ):
        raise AssertionError("Snapshot membership không khớp policy trước khi tách role.")
    counts = {
        "entries": len(actual),
        "transferred": sum(entry["disposition"] == "transfer" for entry in manifest),
        "revoke_only": sum(entry["disposition"] == "revoke_only" for entry in manifest),
        "membership_snapshotted": True,
    }
    print(json.dumps(counts, sort_keys=True))


def main() -> None:
    actions = {"seed": seed, "check": check, "cleanup": cleanup, "snapshot": verify_snapshot}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        raise SystemExit("Hành động probe phải là seed, check, cleanup hoặc snapshot.")
    actions[sys.argv[1]]()


if __name__ == "__main__":
    main()
