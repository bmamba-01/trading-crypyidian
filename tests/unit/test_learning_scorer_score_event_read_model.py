from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
        "bot_id": uuid4(),
        "bot_decision_id": uuid4(),
        "created_at": NOW,
        "mode": "SHADOW",
        "decision": "VETO",
        "reason_code": "LOW_CONFIDENCE",
        "probability": 0.21,
        "confidence": 0.72,
        "uncertainty": 0.28,
        "operator_block_actor_user_id": None,
        "operator_block_mfa_verified_at": None,
        "operator_block_reason": None,
        "operator_block_created_at": None,
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

    def scalar_one_or_none(self):
        return None


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
    assert item.bot_id == session.rows[0]["bot_id"]
    assert item.bot_decision_id == session.rows[0]["bot_decision_id"]
    assert item.paper_entry_block is None
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
        "bot_id",
        "bot_decision_id",
        "created_at",
        "mode",
        "decision",
        "reason_code",
        "probability",
        "confidence",
        "uncertainty",
        "operator_block_actor_user_id",
        "operator_block_mfa_verified_at",
        "operator_block_reason",
        "operator_block_created_at",
    }
    assert "OFFSET" in str(compiled).upper()
    assert "LIMIT" in str(compiled).upper()


@pytest.mark.asyncio
async def test_score_event_projection_includes_workspace_scoped_operator_audit() -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_score_event_read_model import (
        LearningScorerScoreEventReadRepository,
    )

    row = _row()
    actor_user_id = uuid4()
    row.update(
        {
            "mode": "PAPER_GATE",
            "decision": "APPROVE",
            "reason_code": None,
            "operator_block_actor_user_id": actor_user_id,
            "operator_block_mfa_verified_at": NOW - timedelta(minutes=1),
            "operator_block_reason": "Kiểm tra thủ công",
            "operator_block_created_at": NOW,
        }
    )
    session = _Session([row])

    page = await LearningScorerScoreEventReadRepository(session).get_page(
        workspace_id=WORKSPACE_ID,
        page=1,
    )

    item = page.items[0]
    assert item.mode == "PAPER_GATE"
    assert item.decision == "APPROVE"
    assert item.paper_entry_block is not None
    assert item.paper_entry_block.actor_user_id == actor_user_id
    assert item.paper_entry_block.reason == "Kiểm tra thủ công"
    assert item.paper_entry_block.created_at == NOW
    compiled = session.statement.compile(dialect=postgresql.dialect())
    assert WORKSPACE_ID in compiled.params.values()
    assert "learning_scorer_paper_entry_block.workspace_id" in str(compiled)
    assert "event_payload" not in str(compiled)


@pytest.mark.asyncio
async def test_score_event_repository_resolves_only_workspace_scoped_paper_approve() -> None:
    from trading_bot.adapters.database.repositories.learning_scorer_score_event_read_model import (
        LearningScorerScoreEventReadRepository,
    )

    repository = LearningScorerScoreEventReadRepository(_Session([]))
    decision_id = await repository.get_blockable_decision_id(
        workspace_id=WORKSPACE_ID,
        score_event_id=EVENT_ID,
    )

    assert decision_id is None
    compiled = repository._session.statement.compile(dialect=postgresql.dialect())
    assert WORKSPACE_ID in compiled.params.values()
    assert EVENT_ID in compiled.params.values()
    assert "PAPER_GATE" in compiled.params.values()
    assert "APPROVE" in compiled.params.values()


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
