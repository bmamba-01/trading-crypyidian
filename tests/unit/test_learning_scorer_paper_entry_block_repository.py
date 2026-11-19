"""Kiểm cách repository thực thi câu lệnh và xử lý retry của ledger."""

from dataclasses import asdict, replace
from datetime import timedelta
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.test_learning_scorer_paper_entry_block import _event


@pytest.fixture
def repository():
    from trading_bot.adapters.database.repositories.learning_scorer_paper_entry_blocks import (
        LearningPaperEntryBlockRepository,
    )

    return LearningPaperEntryBlockRepository(_Session())


class _Session:
    """Mô phỏng kết quả DB sau INSERT/SELECT, lưu SQL thực để kiểm tra phạm vi."""

    def __init__(self) -> None:
        self.results = []
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement.compile(dialect=postgresql.dialect()))
        result = Mock()
        value = self.results.pop(0)
        result.scalar_one_or_none.return_value = value
        result.scalar_one.return_value = value
        return result


def _stored_event(event):
    from trading_bot.adapters.database.models.learning_scorer_paper_entry_block_entities import (
        LearningPaperEntryBlockRecord,
    )

    return LearningPaperEntryBlockRecord(**asdict(event))


async def test_append_only_inserts_event_in_caller_transaction(repository) -> None:
    event = _event()
    session = repository._session
    session.results = [event.event_id]

    result = await repository.append_block(workspace_id=event.workspace_id, event=event)

    assert result.event == event
    assert result.is_duplicate is False
    (sql,) = session.statements
    assert sql.string.startswith("INSERT INTO learning_scorer_paper_entry_block ")
    assert "ON CONFLICT DO NOTHING RETURNING" in sql.string
    assert sql.params["bot_decision_id"] == UUID(int=4)
    assert sql.params["workspace_id"] == UUID(int=2)


async def test_retry_returns_original_audit_data(repository) -> None:
    event = _event()
    retry = replace(
        event,
        event_id=UUID(int=101),
        created_at=event.created_at + timedelta(1),
        mfa_verified_at=event.created_at + timedelta(1),
    )
    session = repository._session
    session.results = [None, _stored_event(event)]

    result = await repository.append_block(workspace_id=event.workspace_id, event=retry)

    assert result.event == event
    assert result.is_duplicate is True


@pytest.mark.parametrize(
    "change",
    [
        {"reason": "Lý do khác"},
        {"actor_user_id": UUID(int=99)},
        {"bot_decision_id": UUID(int=99)},
        {"active_pointer_version": 99},
    ],
)
async def test_conflicting_intent_under_same_key_is_denied(repository, change) -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_paper_entry_blocks import (
        LearningPaperEntryBlockConflict,
    )

    event = _event()
    session = repository._session
    session.results = [None, _stored_event(event)]

    with pytest.raises(LearningPaperEntryBlockConflict):
        await repository.append_block(
            workspace_id=event.workspace_id, event=replace(event, **change)
        )


async def test_second_key_for_same_decision_does_not_replace_existing_block(repository) -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_paper_entry_blocks import (
        LearningPaperEntryBlockConflict,
    )

    event = _event()
    session = repository._session
    session.results = [None, None]

    with pytest.raises(LearningPaperEntryBlockConflict):
        await repository.append_block(workspace_id=event.workspace_id, event=event)


async def test_append_rejects_event_from_another_workspace_before_query(repository) -> None:
    with pytest.raises(PermissionError):
        await repository.append_block(workspace_id=UUID(int=99), event=_event())

    assert repository._session.statements == []


@pytest.mark.parametrize(
    ("method", "column", "lookup_id"),
    [
        ("find_by_decision", "bot_decision_id", UUID(int=4)),
        ("find_by_idempotency_key", "idempotency_key", UUID(int=13)),
    ],
)
async def test_audit_lookup_filters_by_workspace_and_returns_immutable_event(
    repository, method, column, lookup_id
) -> None:
    event = _event()
    session = repository._session
    session.results = [_stored_event(event)]

    found = await getattr(repository, method)(workspace_id=UUID(int=2), **{column: lookup_id})

    assert found == event
    (sql,) = session.statements
    assert "WHERE learning_scorer_paper_entry_block.workspace_id = " in sql.string
    assert f"AND learning_scorer_paper_entry_block.{column} = " in sql.string
    assert set(sql.params.values()) == {UUID(int=2), lookup_id}


async def test_runtime_projection_reads_only_workspace_and_decision_columns(repository) -> None:
    session = repository._session
    session.results = [True, False]

    assert await repository.is_blocked(workspace_id=UUID(int=2), bot_decision_id=UUID(int=4))
    assert not await repository.is_blocked(workspace_id=UUID(int=9), bot_decision_id=UUID(int=4))
    first, second = session.statements
    assert first.string.startswith(
        "SELECT EXISTS (SELECT learning_scorer_paper_entry_block.bot_decision_id"
    )
    assert set(first.params.values()) == {UUID(int=2), UUID(int=4)}
    assert set(second.params.values()) == {UUID(int=9), UUID(int=4)}
