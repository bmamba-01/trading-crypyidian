from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from trading_bot.adapters.database.models import SourcePolicyRevisionRecord, SourceQuotaEventRecord

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"
REVISION = "c6e8a0b2d4f6"


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


def test_registry_schema_is_workspace_scoped_and_policy_revisions_are_append_only() -> None:
    policy = SourcePolicyRevisionRecord.__table__
    quota = SourceQuotaEventRecord.__table__
    assert policy.c.workspace_id.foreign_keys
    assert isinstance(policy.c.allowed_purposes.type, JSONB)
    assert {"source_id", "sequence", "policy_id", "evidence_ref", "valid_until"} <= set(
        policy.c.keys()
    )
    assert not {"api_key", "secret", "credential", "token"} & set(policy.c.keys())
    assert any(
        isinstance(constraint, UniqueConstraint)
        and {"workspace_id", "source_id", "sequence"} == set(constraint.columns.keys())
        for constraint in policy.constraints
    )
    assert any(
        isinstance(constraint, CheckConstraint)
        and "verified" in str(constraint.sqltext)
        and "approved_by <> actor_id" in str(constraint.sqltext)
        for constraint in policy.constraints
    )
    assert any(
        isinstance(constraint, ForeignKeyConstraint)
        and [element.parent.name for element in constraint.elements]
        == ["workspace_id", "source_id", "policy_sequence"]
        for constraint in quota.foreign_key_constraints
    )
    assert quota.c.reserved_requests.nullable is False
    assert quota.c.reserved_bytes.nullable is False
    assert quota.c.reserved_cost.nullable is False
    assert policy.c.quota_max_cost.type.precision == 384
    assert policy.c.quota_max_cost.type.scale == 128
    assert quota.c.reserved_cost.type.precision == 384
    assert quota.c.reserved_cost.type.scale == 128
    policy_workspace_fk = next(
        constraint
        for constraint in policy.foreign_key_constraints
        if [element.parent.name for element in constraint.elements] == ["workspace_id"]
    )
    assert policy_workspace_fk.ondelete == "RESTRICT"
    quota_workspace_fk = next(
        constraint
        for constraint in quota.foreign_key_constraints
        if [element.parent.name for element in constraint.elements] == ["workspace_id"]
    )
    assert quota_workspace_fk.ondelete == "RESTRICT"


def test_upgrade_applies_workspace_rls_append_only_and_role_separation() -> None:
    statements = _run("upgrade")
    joined = "\n".join(statements)

    for table in ("source_policy_revision", "source_quota_event"):
        assert f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY' in joined
        assert f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY' in joined
        assert f'CREATE POLICY workspace_isolation ON "{table}"' in joined
        assert (
            f'CREATE TRIGGER "{table}_append_only" BEFORE UPDATE OR DELETE ON "{table}"' in joined
        )
    assert "current_setting('app.workspace_id', true)" in joined
    assert "CREATE POLICY source_policy_insert ON source_policy_revision" in joined
    assert "FOR INSERT TO crypidian_admin_web" in joined
    assert "CREATE POLICY source_quota_insert ON source_quota_event" in joined
    assert "FOR INSERT TO crypidian_ingestion_worker" in joined
    assert "GRANT INSERT ON TABLE source_policy_revision TO crypidian_admin_web" in joined
    assert (
        "GRANT SELECT ON TABLE source_policy_revision TO crypidian_admin_web, "
        "crypidian_ingestion_worker"
    ) in joined
    assert (
        "GRANT INSERT ON TABLE source_policy_revision TO crypidian_ingestion_worker" not in joined
    )
    assert "GRANT INSERT ON TABLE source_quota_event TO crypidian_ingestion_worker" in joined
    assert "UPDATE ON TABLE source_policy_revision" not in joined
    assert "DELETE ON TABLE source_policy_revision" not in joined
    assert "CREATE FUNCTION public.enforce_source_policy_transition()" in joined
    assert "pg_advisory_xact_lock(" in joined
    assert "hashtextextended(" in joined
    assert "WP05_POLICY_FIRST_REVISION_MUST_BE_DISCOVERED" in joined
    assert "WP05_POLICY_TRANSITION_INVALID" in joined


def test_downgrade_drops_policies_triggers_tables_and_indexes() -> None:
    statements = _run("downgrade")
    joined = "\n".join(statements)

    assert 'DROP POLICY source_policy_insert ON "source_policy_revision"' in joined
    assert 'DROP POLICY source_quota_insert ON "source_quota_event"' in joined
    assert 'DROP POLICY workspace_isolation ON "source_policy_revision"' in joined
    assert 'DROP POLICY workspace_isolation ON "source_quota_event"' in joined
    assert 'DROP TRIGGER "source_policy_revision_append_only"' in joined
    assert 'DROP TRIGGER "source_quota_event_append_only"' in joined
    assert joined.index("DROP TABLE source_quota_event") < joined.index(
        "DROP TABLE source_policy_revision"
    )
    assert "WP05_SOURCE_HISTORY_DOWNGRADE_BLOCKED" in joined
    assert joined.index("WP05_SOURCE_HISTORY_DOWNGRADE_BLOCKED") < joined.index(
        "DROP TABLE source_quota_event"
    )
