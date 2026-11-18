from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from tests.unit.test_learning_scorer_runtime import _policy

import trading_bot.application.bot_decision_contexts as bot_decision_contexts
from trading_bot.adapters.database.repositories import bot_decisions
from trading_bot.adapters.database.repositories.learning_scorer_runtime_committer import (
    PostgresScorerRuntimeCommitter,
)
from trading_bot.app import _runtime_job_processor
from trading_bot.application.scorer_runtime_policy import ConfiguredScorerRuntimePolicyProvider

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_admin_runtime_job_processor_wires_configured_scorer_committer(monkeypatch) -> None:
    captured: list[object] = []
    context_provider_options: list[dict[str, object]] = []

    class Store:
        def __init__(self, _sessions, *, score_candidate):
            captured.append(score_candidate)

    class SnapshotProvider:
        max_bars_per_stream = 1200

        async def snapshot_for(self, *_args, **_kwargs):
            raise AssertionError("Factory test must not request runtime snapshots.")

    class ContextProvider:
        def __init__(self, *_args, **kwargs) -> None:
            context_provider_options.append(kwargs)

    monkeypatch.setattr(
        bot_decision_contexts,
        "EffectiveBotDecisionContextProvider",
        ContextProvider,
    )

    monkeypatch.setattr(bot_decisions, "PostgresSignalDecisionStore", Store)
    provider = ConfiguredScorerRuntimePolicyProvider(_policy())

    processor = _runtime_job_processor(
        context_provider=SnapshotProvider(),
        sessions=object(),
        workspace_id=WORKSPACE_ID,
        application=SimpleNamespace(runtime_account=object()),
        scorer_policy_provider=provider,
        active_modes_enabled=True,
    )

    assert callable(processor)
    assert len(captured) == 1
    assert isinstance(captured[0], PostgresScorerRuntimeCommitter)
    assert captured[0]._policy_provider is provider
    assert context_provider_options[0]["max_bars_per_stream"] == 1200


def test_runtime_job_processor_keeps_active_scorer_modes_locked_by_default(monkeypatch) -> None:
    captured: list[object] = []

    class Store:
        def __init__(self, _sessions, *, score_candidate):
            captured.append(score_candidate)

    class SnapshotProvider:
        max_bars_per_stream = 1200

        async def snapshot_for(self, *_args, **_kwargs):
            raise AssertionError("Factory test must not request runtime snapshots.")

    monkeypatch.setattr(
        bot_decision_contexts,
        "EffectiveBotDecisionContextProvider",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(bot_decisions, "PostgresSignalDecisionStore", Store)

    processor = _runtime_job_processor(
        context_provider=SnapshotProvider(),
        sessions=object(),
        workspace_id=WORKSPACE_ID,
        application=SimpleNamespace(runtime_account=object()),
        scorer_policy_provider=ConfiguredScorerRuntimePolicyProvider(_policy()),
    )

    assert callable(processor)
    assert len(captured) == 1
    assert isinstance(captured[0], PostgresScorerRuntimeCommitter)
    assert captured[0]._policy_provider is None
