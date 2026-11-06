"""Kiểm hợp đồng migration của allowlist Telegram signal và DB role."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace

MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "f8a2c4e6d0b1_wp07_signal_filters.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp07_signal_filters_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_statements() -> list[str]:
    module = _load()
    recorded: list[str] = []
    module.op = SimpleNamespace(
        execute=lambda statement: recorded.append(str(statement)),
        add_column=lambda *_args, **_kwargs: recorded.append("add_column"),
        drop_column=lambda *_args, **_kwargs: recorded.append("drop_column"),
        create_check_constraint=lambda name, _table, condition: recorded.append(
            f"create_check_constraint:{name}:{condition}"
        ),
        drop_constraint=lambda *_args, **_kwargs: recorded.append("drop_constraint"),
        alter_column=lambda *_args, **_kwargs: recorded.append("alter_column"),
        f=lambda name: name,
    )
    module.upgrade()
    return [re.sub(r"\s+", " ", statement).strip() for statement in recorded]


def test_signal_filter_migration_adds_allowlists_and_filtered_receipt_state() -> None:
    module = _load()
    statements = _upgrade_statements()

    assert module.revision == "f8a2c4e6d0b1"
    assert module.down_revision == "d4f6a8c0e2b1"
    assert statements.count("add_column") == 5
    assert sum(item.startswith("create_check_constraint:") for item in statements) == 6
    assert any("FILTERED" in item for item in statements)
    assert any(
        "GRANT SELECT (symbols, timeframes, directions, strategy_ids, setup_codes)" in item
        for item in statements
    )
    assert any(
        "GRANT UPDATE (symbols, timeframes, directions, strategy_ids, setup_codes, updated_at)"
        in item
        for item in statements
    )


def test_signal_filter_migration_has_data_preserving_downgrade_guard() -> None:
    module = _load()
    recorded: list[str] = []
    module.op = SimpleNamespace(
        execute=lambda statement: recorded.append(str(statement)),
        drop_column=lambda *_args, **_kwargs: None,
        drop_constraint=lambda *_args, **_kwargs: None,
        create_check_constraint=lambda *_args, **_kwargs: None,
        alter_column=lambda *_args, **_kwargs: None,
        f=lambda name: name,
    )
    module.downgrade()

    assert any("WP07_SIGNAL_FILTERS_DOWNGRADE_BLOCKED" in item for item in recorded)
