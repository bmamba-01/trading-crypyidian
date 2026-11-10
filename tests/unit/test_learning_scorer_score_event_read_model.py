from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.learning_scorer_score_event_read_model import (
    LearningScorerScoreEventReadRepository,
)

WORKSPACE_ID = uuid4()
EVENT_ID = uuid4()
NOW = datetime(2026, 11, 10, 8, 30, tzinfo=UTC)


def _row() -> dict[str, object]:
    return {
        "id": EVENT_ID,
        "created_at": NOW,
        "mode": "SHADOW",
        "decision": "VETO",
        "reason_code": "LOW_CONFIDENCE",
        "probability": 0.21,
        "confidence": 0.72,
        "uncertainty": 0.28,
    }


class _Mappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def all(self) -> list[dict[str, object]]:
        return self.rows


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> _Mappings:
        return _Mappings(self.rows)


class _Session:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.statement = None
        self.parameters = None

    async def execute(self, statement, parameters=None):
        self.statement = statement
        self.parameters = parameters
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_score_event_page_projects_workspace_scoped_display_metadata_only() -> None:
    session = _Session([_row()])

    page = await LearningScorerScoreEventReadRepository(session).get_page(
        workspace_id=WORKSPACE_ID,
        page=2,
        page_size=1,
    )

    assert page.page == 2
    assert page.has_previous is True
    assert page.has_next is False
    assert len(page.items) == 1
    item = page.items[0]
    assert item.event_id == EVENT_ID
    assert item.mode == "SHADOW"
    assert item.decision == "VETO"
    assert item.reason_code == "LOW_CONFIDENCE"
    assert item.probability == 0.21
    compiled = session.statement.compile(dialect=postgresql.dialect())
    assert WORKSPACE_ID in compiled.params.values()
    assert "learning_score_event.workspace_id" in str(compiled)
    assert "canonical_event" not in str(compiled)
    assert "event_payload" not in str(compiled)
    assert "source_event_digests" not in str(compiled)
    selected_columns = {column.key for column in session.statement.selected_columns}
    assert selected_columns == {
        "id",
        "created_at",
        "mode",
        "decision",
        "reason_code",
        "probability",
        "confidence",
        "uncertainty",
    }
    assert "OFFSET" in str(compiled).upper()
    assert "LIMIT" in str(compiled).upper()


@pytest.mark.asyncio
async def test_score_event_page_uses_extra_row_to_detect_next_page() -> None:
    session = _Session([_row(), {**_row(), "id": uuid4()}])

    page = await LearningScorerScoreEventReadRepository(session).get_page(
        workspace_id=WORKSPACE_ID,
        page=1,
        page_size=1,
    )

    assert page.has_previous is False
    assert page.has_next is True
    assert len(page.items) == 1


@pytest.mark.asyncio
async def test_score_event_page_rejects_malformed_persisted_score() -> None:
    row = _row()
    row["probability"] = 1.5

    with pytest.raises(ValueError, match="probability"):
        await LearningScorerScoreEventReadRepository(_Session([row])).get_page(
            workspace_id=WORKSPACE_ID,
            page=1,
            page_size=20,
        )


@pytest.mark.asyncio
async def test_score_event_page_rejects_invalid_page_bounds() -> None:
    repository = LearningScorerScoreEventReadRepository(_Session([]))

    with pytest.raises(ValueError, match="page"):
        await repository.get_page(workspace_id=WORKSPACE_ID, page=0, page_size=20)
    with pytest.raises(ValueError, match="page_size"):
        await repository.get_page(workspace_id=WORKSPACE_ID, page=1, page_size=101)
