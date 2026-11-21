from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_score_event_facade_resolves_blockable_decision_in_read_only_workspace_scope(
    monkeypatch,
) -> None:
    from trading_bot.application import learning_scorer_score_event_read_model as module

    workspace_id = uuid4()
    score_event_id = uuid4()
    decision_id = uuid4()
    session_factory = object()
    trace: list[str] = []

    class Unit:
        def __init__(self, sessions, *, workspace_id, snapshot_read_only):
            assert sessions is session_factory
            assert workspace_id == workspace_id_expected
            assert snapshot_read_only is True
            self.session = object()

        async def __aenter__(self):
            trace.append("enter")
            return self

        async def __aexit__(self, *_args):
            trace.append("exit")

    workspace_id_expected = workspace_id

    class Repository:
        def __init__(self, session):
            assert session is not None

        async def get_blockable_decision_id(self, *, workspace_id, score_event_id):
            assert workspace_id == workspace_id_expected
            assert score_event_id == score_event_id_expected
            trace.append("resolve")
            return decision_id

    score_event_id_expected = score_event_id
    monkeypatch.setattr(module, "UnitOfWork", Unit)
    monkeypatch.setattr(module, "LearningScorerScoreEventReadRepository", Repository)

    result = await module.ScorerScoreEventReadModel(session_factory).get_blockable_decision_id(
        workspace_id,
        score_event_id=score_event_id,
    )

    assert result == decision_id
    assert trace == ["enter", "resolve", "exit"]
