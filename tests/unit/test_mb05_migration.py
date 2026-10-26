"""MB05.2: kiểm tĩnh migration registry runtime; hành vi thật chứng minh bằng Compose E2E."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
MIGRATION = VERSIONS / "d9b2f4a6c8e1_bot_runtime_registry.py"
RUNTIME = "crypidian_trading_runtime"
ADMIN = "crypidian_admin_web"
NEW_TABLES = (
    "workspace_runtime_control",
    "bot_runtime_control",
    "bot_runtime_lease",
    "bot_runtime_state",
    "account_activity_incident",
)
BOT_SCOPED = NEW_TABLES[1:]
# Ma trận kỳ vọng độc lập với migration (bảng "Kế hoạch dữ liệu" của WBS MB-05).
EXPECTED_GRANTS = {
    "workspace_runtime_control": {RUNTIME: {"SELECT"}, ADMIN: {"SELECT", "INSERT"}},
    "bot_runtime_control": {RUNTIME: {"SELECT"}, ADMIN: {"SELECT", "INSERT"}},
    "bot_runtime_lease": {
        RUNTIME: {
            "SELECT",
            "INSERT",
            "UPDATE (owner_id, fencing_token, acquired_at, renewed_at, expires_at)",
        },
        ADMIN: {"SELECT"},
    },
    "bot_runtime_state": {
        RUNTIME: {
            "SELECT",
            "INSERT",
            "UPDATE (readiness, reason_code, applied_bot_control_revision, "
            "applied_workspace_control_revision, last_checkpoint_revision, activity_cursor, "
            "activity_checked_at, activity_status, fanout_exclusion_code, heartbeat_at, "
            "fencing_token, updated_at)",
        },
        ADMIN: {"SELECT"},
    },
    "account_activity_incident": {
        RUNTIME: {"SELECT", "INSERT"},
        ADMIN: {
            "SELECT",
            "UPDATE (status, resolution_code, resolution_reason, resolved_by, resolved_at)",
        },
    },
}


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mb05_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _statements(module: ModuleType, step: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: recorded.append(str(statement)))
    for name in (
        "create_table",
        "create_index",
        "add_column",
        "drop_column",
        "drop_table",
        "drop_index",
        "drop_constraint",
        "create_check_constraint",
    ):
        monkeypatch.setattr(module.op, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(module.op, "f", lambda name: name)
    getattr(module, step)()
    return [re.sub(r"\s+", " ", statement).strip() for statement in recorded]


def test_revision_chains_after_merged_mb04_head_and_is_single_head() -> None:
    module = _module()

    assert module.revision == "d9b2f4a6c8e1"
    assert module.down_revision == "c8f1e3a5b7d9"
    others = [
        path
        for path in VERSIONS.glob("*.py")
        if path != MIGRATION and 'down_revision = "d9b2f4a6c8e1"' in path.read_text("utf-8")
    ]
    assert others == []


def test_new_tables_are_declared_and_bot_scoped_tables_use_composite_fk() -> None:
    module = _module()
    source = _source()

    assert tuple(module.NEW_TABLES) == NEW_TABLES
    assert tuple(module.BOT_SCOPED) == BOT_SCOPED
    # FK ghép và index bot scope dựng bằng helper chung cho từng bảng bot-scoped.
    assert 'name=f"fk_{table}_bot_account"' in source
    assert (
        'op.create_index(f"ix_{table}_bot_scope", table, ["workspace_id", "bot_id", "account_id"])'
        in (source)
    )
    for table in BOT_SCOPED:
        assert f'*_bot_scope("{table}")' in source, table
    assert '_BOT_INSTANCE_SCOPE,\n            name=f"fk_{table}_bot_account"' in source


def test_every_new_table_enables_and_forces_rls_with_per_role_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _statements(_module(), "upgrade", monkeypatch)
    joined = "\n".join(statements)

    for table in NEW_TABLES:
        assert f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY' in joined, table
        assert f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY' in joined, table
        policies = [s for s in statements if s.startswith("CREATE POLICY") and f'"{table}"' in s]
        assert policies, table
        # Mỗi policy có role đích tường minh; không policy áp cho PUBLIC.
        assert all(f" TO {RUNTIME} " in p or f" TO {ADMIN} " in p for p in policies), table
    for table in BOT_SCOPED:
        runtime = [s for s in statements if f'"{table}"' in s and f"TO {RUNTIME}" in s]
        assert any("bot_id = NULLIF(current_setting('app.bot_id'" in s for s in runtime), table


def test_grants_match_data_plan_without_delete_or_group_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    statements = _statements(module, "upgrade", monkeypatch)
    grants = [s for s in statements if s.startswith("GRANT ")]
    actual: dict[str, dict[str, set[str]]] = {}
    for statement in grants:
        match = re.fullmatch(r'GRANT (.+) ON TABLE "([a-z_]+)" TO ([a-z_]+)', statement)
        if match is None:
            continue
        privilege, table, role = match.groups()
        actual.setdefault(table, {}).setdefault(role, set()).add(privilege)
    for table, expected in EXPECTED_GRANTS.items():
        assert actual.get(table) == expected, table
    assert not any("DELETE" in statement for statement in grants)
    assert not any("crypidian_app" in statement for statement in grants)
    assert not any("TRUNCATE" in statement for statement in grants)
    for table in NEW_TABLES:
        assert f'REVOKE ALL ON TABLE "{table}" FROM PUBLIC, crypidian_app' in statements, table
    # Admin-web không được ghi lease/state; runtime không được ghi control/resolution.
    for table in ("bot_runtime_lease", "bot_runtime_state"):
        assert actual[table][ADMIN] == {"SELECT"}
    for table in ("workspace_runtime_control", "bot_runtime_control"):
        assert actual[table][RUNTIME] == {"SELECT"}


def test_job_and_checkpoint_changes_use_explicit_column_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    statements = _statements(module, "upgrade", monkeypatch)

    assert (
        f'GRANT UPDATE (attempts, last_error_code, next_attempt_at) ON TABLE "bot_decision_job" '
        f"TO {RUNTIME}"
    ) in statements
    assert not any(s.startswith("GRANT") and "paper_execution_checkpoint" in s for s in statements)
    joined = "\n".join(statements)
    assert "'DEAD'" in joined
    assert "paper_execution_checkpoint_fence" in joined


def test_trigger_names_are_explicit_constants_and_functions_pin_search_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    statements = _statements(module, "upgrade", monkeypatch)

    expected = {
        "workspace_runtime_control": ("workspace_runtime_control_append_only",),
        "bot_runtime_control": ("bot_runtime_control_append_only",),
        "bot_runtime_lease": ("bot_runtime_lease_guard",),
        "bot_runtime_state": ("bot_runtime_state_fence", "bot_runtime_state_identity_guard"),
        "account_activity_incident": (
            "account_activity_incident_fence",
            "account_activity_incident_transition_guard",
        ),
        "paper_execution_checkpoint": ("paper_execution_checkpoint_fence",),
    }
    assert module.TRIGGERS == expected
    for table, triggers in expected.items():
        for trigger in triggers:
            created = [s for s in statements if s.startswith(f'CREATE TRIGGER "{trigger}"')]
            assert len(created) == 1, trigger
            assert f'ON "{table}"' in created[0], trigger
    functions = [s for s in statements if s.startswith("CREATE FUNCTION")]
    assert functions
    for function in functions:
        assert "SET search_path = pg_catalog, pg_temp" in function, function[:80]
        assert "SECURITY DEFINER" not in function
    assert any("clock_timestamp()" in function for function in functions)
    assert any("FOR SHARE" in function for function in functions)
    for function_name in module.FUNCTIONS:
        assert f"REVOKE ALL ON FUNCTION public.{function_name}() FROM PUBLIC" in statements


def test_constraint_and_index_names_fit_postgres_identifier_limit() -> None:
    source = _source()
    module = _module()
    names = re.findall(r'name="([a-z0-9_]+)"', source)
    for name in names:
        assert len(name) <= 63, name
    for table, names_by_table in module.CHECK_NAMES.items():
        for name in names_by_table:
            assert len(f"ck_{table}_{name}") <= 63, (table, name)


def test_downgrade_guard_refuses_runtime_state_and_restores_job_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _statements(_module(), "downgrade", monkeypatch)
    joined = "\n".join(statements)

    assert statements[0].startswith("DO $$")
    assert "MB05_DOWNGRADE_REQUIRES_EXPORT_OF_RUNTIME_STATE" in statements[0]
    for table in NEW_TABLES:
        assert f"EXISTS (SELECT 1 FROM {table})" in statements[0], table
    assert "status = 'DEAD'" in statements[0]
    assert "attempts > 0" in statements[0]
    assert "fencing_token IS NOT NULL" in statements[0]
    # Trigger job trở lại đúng hợp đồng MB-03: chỉ PENDING -> COMPLETED.
    assert "OLD.status <> 'PENDING' OR NEW.status <> 'COMPLETED'" in joined
