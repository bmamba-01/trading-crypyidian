from __future__ import annotations

import ast
from pathlib import Path

from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/versions/b5d7f9a1c3e6_wp06_paper_decision_attribution.py"


def test_paper_decision_attribution_is_nullable_typed_and_unique_per_bot() -> None:
    table = OrderIntentRecord.__table__
    assert table.c.bot_decision_id.nullable is True

    foreign_key = next(
        constraint
        for constraint in table.foreign_key_constraints
        if constraint.name == "fk_order_intent_bot_decision"
    )
    assert [element.parent.name for element in foreign_key.elements] == [
        "workspace_id",
        "bot_id",
        "bot_decision_id",
    ]
    assert [element.target_fullname for element in foreign_key.elements] == [
        "bot_decision.workspace_id",
        "bot_decision.bot_id",
        "bot_decision.id",
    ]

    decision_index = next(
        index for index in table.indexes if index.name == "uq_order_intent_bot_decision"
    )
    assert decision_index.unique is True
    assert "bot_decision_id IS NOT NULL" in str(
        decision_index.dialect_options["postgresql"]["where"]
    )


def test_paper_decision_migration_is_additive_and_blocks_lossy_downgrade() -> None:
    module = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "b7d9f1a3c5e8"' in source
    assert 'op.add_column("order_intent"' in source
    assert 'sa.Column("bot_decision_id", sa.Uuid(), nullable=True)' in source
    assert "bot_decision_id IS NOT NULL" in source
    assert "WP06_PAPER_DECISION_ATTRIBUTION_DOWNGRADE_BLOCKED" in source
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "bulk_update_mappings"}
        and "UPDATE order_intent" in ast.unparse(node)
        for node in ast.walk(module)
    )
