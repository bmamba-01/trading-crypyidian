"""Kiểm tra hợp đồng migration tách quyền DB admin-web toàn ứng dụng."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

ROOT = Path(__file__).parents[2]
MIGRATION_PATH = ROOT / "migrations/versions/f0a2c4e6b8d4_wp06_admin_web_least_privilege.py"


def _migration():
    spec = importlib.util.spec_from_file_location("wp06_admin_web_least_privilege", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_role_split_migration_uses_a_narrow_non_login_group() -> None:
    migration = _migration()

    assert migration.revision == "f0a2c4e6b8d4"
    assert migration.down_revision == "f0a2c4e6b8d3"
    assert migration.ADMIN == "crypidian_admin_web"
    assert migration.APP_ROLE == "crypidian_app"
    assert migration.SAFE_ROLE == "crypidian_admin_web_safe"
    assert migration.MANUAL_REPORT_READER == "crypidian_report_reader"
    assert migration.RAW_LEDGER_RELATIONS >= {
        "trade",
        "order_intent",
        "bot_decision",
        "bot_decision_job",
        "shared_signal_evidence",
        "learning_score_event",
        "venue_order_intent",
        "venue_order_transition",
        "run_report",
        "analyst_run",
        "paper_candidate_outcome",
        "paper_execution_checkpoint",
        "signal_outcome",
        "telegram_signal_delivery",
    }
    assert "class_probabilities" in migration.RAW_LEDGER_COLUMNS["learning_score_event"]
    assert "payload" in migration.RAW_LEDGER_COLUMNS["bot_decision"]
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    raw_access_guard = source
    raw_access_guard = raw_access_guard[
        raw_access_guard.index("def _assert_denied_raw_admin_reads") : raw_access_guard.index(
            "def upgrade()"
        )
    ]
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert f"public.telegram_signal_delivery', '{privilege}'" in raw_access_guard
    assert "GRANT SELECT (workspace_id, id, status)" in source
    assert "ON public.trading_account TO {MANUAL_REPORT_READER}" in source
    assert "CREATE POLICY wp06_manual_report_reader_account_select" in source
    assert (
        "FOR SELECT TO crypidian_report_reader"
        in migration._MANUAL_REPORT_READER_ACCOUNT_POLICY_SQL
    )
    assert "DROP POLICY wp06_manual_report_reader_account_select" in source
    assert "REVOKE SELECT (workspace_id, id, status)" in source

    upgrade_sql = "\n".join(migration._upgrade_statements())
    assert "CREATE ROLE crypidian_admin_web_safe NOLOGIN" in upgrade_sql
    assert (
        "NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        in upgrade_sql
    )
    assert "GRANT crypidian_admin_web_safe TO crypidian_admin_web" in upgrade_sql
    assert "INHERIT TRUE" in upgrade_sql
    assert "SET FALSE" in upgrade_sql
    assert "ADMIN FALSE" in upgrade_sql
    assert "REVOKE crypidian_app FROM crypidian_admin_web" in upgrade_sql


def test_admin_acl_snapshot_covers_direct_and_column_acl_for_downgrade() -> None:
    migration = _migration()
    upgrade_sql = "\n".join(migration._upgrade_statements())
    downgrade_sql = "\n".join(migration._downgrade_statements())
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "wp06_admin_web_acl_snapshot" in upgrade_sql
    assert "aclexplode" in source
    assert "attacl" in source
    assert "pg_default_acl" in source
    assert "_assert_snapshot_manifest(connection)" in source
    assert "DROP ROLE crypidian_admin_web_safe" in downgrade_sql
    assert "DROP POLICY" not in upgrade_sql
    assert "ALTER POLICY" not in upgrade_sql


def test_admin_acl_manifest_is_positive_and_names_consumer_and_operation() -> None:
    migration = _migration()

    manifest = migration.ADMIN_SAFE_ACL_MANIFEST
    assert manifest
    assert all(entry["consumer"].strip() for entry in manifest.values())
    assert all(entry["operation"].strip() for entry in manifest.values())
    assert all(entry["is_grantable"] is False for entry in manifest.values())
    assert {entry["disposition"] for entry in manifest.values()} == {
        "transfer",
        "revoke_only",
    }
    assert all(
        entry["disposition"] != "revoke_only" or entry["safe_grant_sql"] is None
        for entry in manifest.values()
    )
    raw_entries = tuple(
        entry
        for entry in manifest.values()
        if entry["kind"] in {"relation", "column"}
        and entry["object_identity"].split(".", maxsplit=1)[0] in migration.RAW_LEDGER_RELATIONS
    )
    assert raw_entries
    assert all(entry["disposition"] == "revoke_only" for entry in raw_entries)


def test_admin_acl_preflight_covers_all_postgresql_18_grantable_object_classes() -> None:
    migration = _migration()
    catalog_queries = "\n".join(str(query) for query in migration._acl_queries())

    for catalog in (
        "pg_class",
        "pg_attribute",
        "pg_proc",
        "pg_namespace",
        "pg_type",
        "pg_database",
        "pg_largeobject_metadata",
        "pg_language",
        "pg_foreign_data_wrapper",
        "pg_foreign_server",
        "pg_tablespace",
        "pg_parameter_acl",
    ):
        assert catalog in catalog_queries
    assert "defaclrole" in str(migration._DEFAULT_ACL_QUERY)
    assert "x.grantee" in str(migration._DEFAULT_ACL_QUERY)
    assert "pg_shdepend" in str(migration._OWNED_OBJECT_QUERY)
    assert "attnum <> 0" in str(migration._COLUMN_ACL_SQL)
    assert "attisdropped" not in str(migration._COLUMN_ACL_SQL)
    assert "current_database()" not in str(migration._DATABASE_ACL_SQL)


def test_admin_acl_preflight_rejects_unclassified_grants_and_role_flags() -> None:
    migration = _migration()

    unknown = {
        "grant_sql": "GRANT SELECT ON TABLE public.wp06_unclassified TO crypidian_admin_web",
        "kind": "relation",
        "is_grantable": False,
        "safe_grant_sql": "GRANT SELECT ON TABLE public.wp06_unclassified "
        "TO crypidian_admin_web_safe",
        "safe_revoke_sql": "REVOKE SELECT ON TABLE public.wp06_unclassified "
        "FROM crypidian_admin_web_safe",
    }
    with pytest.raises(RuntimeError, match="WP06_ADMIN_ACL_UNCLASSIFIED"):
        migration._validate_admin_acl_rows((unknown,))

    with pytest.raises(RuntimeError, match="WP06_ADMIN_ROLE_MEMBERSHIP_OPTIONS_UNEXPECTED"):
        migration._assert_expected_membership_options(
            (
                {
                    "rolname": migration.APP_ROLE,
                    "admin_option": False,
                    "inherit_option": True,
                    "set_option": False,
                    "grantor_name": "crypidian_admin",
                },
            )
        )


def test_report_reader_is_scoped_by_current_membership_and_report_grant() -> None:
    migration = _migration()
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert migration.REPORT_SCOPE_ROLE == "crypidian_admin_report_scope_owner"
    assert migration.REPORT_READER_RELATIONS >= {
        "trade",
        "trade_cost",
        "trade_plan",
        "order_intent",
        "fill",
        "position_event",
        "bot_decision",
        "shared_signal_evidence",
        "outbox_event",
        "proposal_backtest",
        "analyst_run",
        "run_report",
        "market_bar",
        "market_mark_price",
    }
    assert "wp06_admin_report_reader_can_access" in source
    assert "wp06_admin_report_reader_can_read_workspace" in source
    assert "wp06_admin_report_scope_membership_read" in source
    assert "wp06_admin_report_scope_role_read" in source
    assert "wp06_admin_report_scope_grant_read" in source
    assert "bot:report:read" in source
    assert "payload" not in migration._OUTBOX_READER_COLUMNS
    assert 'elif table == "proposal_backtest"' in source
    assert "wp06_admin_report_reader_can_read_workspace_page" in source
    workspace_page_sql = migration._REPORT_WORKSPACE_READ_HELPER_SQL
    assert "r.role IN ('owner', 'strategy_admin', 'risk_admin')" in workspace_page_sql
    assert "operator" not in workspace_page_sql
    assert "analyst_reviewer" not in workspace_page_sql
    assert "raw_stdout" not in migration._ANALYST_RUN_READER_COLUMNS
    assert "GRANT SELECT (workspace_id, id, analyst_run_id" in source
    assert "ON public.analyst_run TO {REPORT_READER}" in source
    assert "ON public.run_report TO {REPORT_READER}" in source
    assert 'f"CREATE POLICY wp06_admin_report_reader_{table}_select ON public.{table} "' in source
    assert "m.status = 'ACTIVE'" in source
    assert "g.permission = 'bot:report:read'" in source
    assert "g.revoked_at IS NULL" in source
    assert "g.expires_at IS NULL OR g.expires_at > clock_timestamp()" in source
    assert "SET search_path = pg_catalog, public" in source
    assert "SET row_security = on" in source
    assert "wp06_admin_report_reader_can_access(workspace_id, bot_id)" in source


def test_newer_migrations_do_not_grant_direct_privileges_to_admin_login() -> None:
    scripts = ScriptDirectory.from_config(_alembic_config())
    descendants = tuple(scripts.iterate_revisions("heads", "f0a2c4e6b8d4"))

    for revision in descendants:
        if revision.revision == "f0a2c4e6b8d4":
            continue
        source = Path(revision.path).read_text(encoding="utf-8")
        assert "TO crypidian_admin_web" not in source, revision.revision
        assert "crypidian_app TO crypidian_admin_web" not in source, revision.revision


def _alembic_config():
    from alembic.config import Config

    return Config(str(ROOT / "alembic.ini"))


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "consumer", "grant_sql", "safe_revoke_sql"]
)
def test_downgrade_requires_every_approved_snapshot_field_and_row(change: str) -> None:
    migration = _migration()
    rows = [dict(row) for row in migration.ADMIN_SAFE_ACL_MANIFEST.values()]
    rows.append(dict(migration.MEMBERSHIP_SNAPSHOT))
    migration._validate_snapshot_rows(rows)
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows.append(rows[0])
    else:
        rows[0][change] = "fixture"
    with pytest.raises(RuntimeError, match="WP06_SNAPSHOT_MANIFEST_UNEXPECTED"):
        migration._validate_snapshot_rows(rows)


def test_classification_requires_an_explicit_transfer_or_revoke_decision(monkeypatch) -> None:
    migration = _migration()
    entry = next(iter(migration.ADMIN_SAFE_ACL_MANIFEST.values()))
    monkeypatch.setitem(entry, "disposition", "fixture")
    observed = {**entry, "privilege_type": entry["operation"]}
    with pytest.raises(RuntimeError, match="WP06_ADMIN_ACL_DISPOSITION_UNEXPECTED"):
        migration._validate_admin_acl_rows([observed])
