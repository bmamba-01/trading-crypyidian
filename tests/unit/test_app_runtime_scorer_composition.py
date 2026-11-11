from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from tests.unit.test_learning_scorer_runtime import _policy

from trading_bot.adapters.database.repositories import bot_decisions
from trading_bot.adapters.database.repositories.learning_scorer_runtime_committer import (
    PostgresScorerRuntimeCommitter,
)
from trading_bot.app import _runtime_job_processor
from trading_bot.application.scorer_runtime_policy import ConfiguredScorerRuntimePolicyProvider

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_admin_runtime_job_processor_wires_configured_scorer_committer(monkeypatch) -> None:
    captured: list[object] = []

    class Store:
        def __init__(self, _sessions, *, score_candidate):
            captured.append(score_candidate)

    class SnapshotProvider:
        async def snapshot_for(self, *_args, **_kwargs):
            raise AssertionError("Factory test must not request runtime snapshots.")

    monkeypatch.setattr(bot_decisions, "PostgresSignalDecisionStore", Store)
    provider = ConfiguredScorerRuntimePolicyProvider(_policy())

    processor = _runtime_job_processor(
        context_provider=SnapshotProvider(),
        sessions=object(),
        workspace_id=WORKSPACE_ID,
        application=SimpleNamespace(runtime_account=object()),
        scorer_policy_provider=provider,
    )

    assert callable(processor)
    assert len(captured) == 1
    assert isinstance(captured[0], PostgresScorerRuntimeCommitter)
    assert captured[0]._policy_provider is provider
