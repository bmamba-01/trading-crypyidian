"""Kiểm schema purpose và downgrade guard cho rights gate backfill."""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "a2c4e6f8b0d2_wp07_backfill_rights_purpose.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp07_backfill_rights_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_expands_policy_and_quota_purpose_checks() -> None:
    module = _load()
    constraints: list[tuple[str, str, str]] = []
    module.op = SimpleNamespace(
        drop_constraint=lambda *_args, **_kwargs: None,
        create_check_constraint=lambda name, table, condition: constraints.append(
            (name, table, condition)
        ),
        execute=lambda _statement: None,
        f=lambda name: name,
    )

    module.upgrade()

    assert module.down_revision == "f8a2c4e6d0b1"
    assert len(constraints) == 2
    assert all(
        "backtest_research" in condition
        for _, table, condition in constraints
        if table == "source_policy_revision"
    )
    assert all("paper_demo" in condition for _, _, condition in constraints)


def test_downgrade_refuses_when_new_purpose_has_been_persisted() -> None:
    module = _load()
    statements: list[str] = []
    module.op = SimpleNamespace(
        execute=lambda statement: statements.append(str(statement)),
        drop_constraint=lambda *_args, **_kwargs: None,
        create_check_constraint=lambda *_args, **_kwargs: None,
        f=lambda name: name,
    )

    module.downgrade()

    assert any("WP07_BACKFILL_PURPOSE_DOWNGRADE_BLOCKED" in item for item in statements)
    assert any("backtest_research" in item and "paper_demo" in item for item in statements)
