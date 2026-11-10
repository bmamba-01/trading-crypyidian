from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.test_learning_score_event import _event

from trading_bot.adapters.database.models import Base, LearningScoreEventRecord
from trading_bot.adapters.database.repositories.learning_score_events import (
    LearningScoreEventConflict,
    LearningScoreEventRepository,
)


class FakeSession:
    def __init__(self, results: list[object | None] | None = None) -> None:
        self.results = iter(results or [])
        self.added: list[object] = []
        self.statements: list[str] = []
        self.flushes = 0

    def _record(self, statement: Any) -> None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))

    async def execute(self, statement: Any):
        self._record(statement)
        value = next(self.results)
        if value is None:
            return MappingResult(None)
        return MappingResult(
            {
                "workspace_id": value.workspace_id,
                "bot_id": value.bot_id,
                "bot_decision_id": value.bot_decision_id,
                "idempotency_key": value.idempotency_key,
                "event_hash": value.event_hash,
                "canonical_event": value.canonical_event,
            }
        )

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        self.flushes += 1

    @asynccontextmanager
    async def begin_nested(self):
        yield


class MappingResult:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value


def test_score_event_table_is_scoped_to_workspace_decision_and_deployment() -> None:
    table = Base.metadata.tables["learning_score_event"]

    assert not {
        "feature_values",
        "features",
        "labels",
        "sample_ids",
        "test_payload",
        "raw_source",
    } & set(table.columns.keys())
    assert any(
        tuple(column.name for column in constraint.columns)
        == ("workspace_id", "bot_id", "bot_decision_id")
        and constraint.referred_table.name == "bot_decision"
        for constraint in table.foreign_key_constraints
    )
    assert any(
        tuple(column.name for column in constraint.columns)
        == ("workspace_id", "deployment_event_id")
        and constraint.referred_table.name == "learning_scoring_deployment_event"
        for constraint in table.foreign_key_constraints
    )


async def test_score_event_append_is_idempotent_and_conflicting_payload_is_rejected() -> None:
    event = _event()
    insert_session = FakeSession([None])

    inserted = await LearningScoreEventRepository(insert_session).append(event)  # type: ignore[arg-type]

    assert inserted.is_duplicate is False
    (row,) = insert_session.added
    assert isinstance(row, LearningScoreEventRecord)
    assert row.id == event.score_event_id
    assert row.idempotency_key == event.idempotency_key
    assert row.event_hash == event.event_hash
    assert row.canonical_event == event.canonical_json
    assert insert_session.flushes == 1

    duplicate_session = FakeSession([row])
    duplicate = await LearningScoreEventRepository(duplicate_session).append(event)  # type: ignore[arg-type]
    assert duplicate.is_duplicate is True
    assert duplicate.event is event
    assert duplicate_session.added == []

    changed = _event(active_pointer_version=5)
    conflict_session = FakeSession([row])
    with pytest.raises(LearningScoreEventConflict, match="idempotency"):
        await LearningScoreEventRepository(conflict_session).append(changed)  # type: ignore[arg-type]

    select_sql = duplicate_session.statements[0].lower()
    assert "canonical_event" in select_sql
    assert "event_payload" not in select_sql
    assert "learning_score_event.bot_id" in select_sql
    assert "learning_score_event.bot_decision_id" in select_sql
