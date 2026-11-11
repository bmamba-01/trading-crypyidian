from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from tests.unit.test_signal_decision_repository import _decision, _job

from trading_bot.adapters.database.repositories import learning_scorer_runtime_committer as module
from trading_bot.adapters.database.repositories.learning_scorer_runtime_committer import (
    PostgresScorerRuntimeCommitter,
    _final_decision,
)
from trading_bot.adapters.database.repositories.learning_scoring_deployments import (
    ScorerDeploymentRightsUnavailable,
)
from trading_bot.application.bot_dispatcher import BotDecisionStatus
from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentMode,
)

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


async def test_disabled_deployment_is_inert_when_authoritative_resolver_returns_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active(self, *, workspace_id):
            assert workspace_id == WORKSPACE
            return None

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)

    result = await PostgresScorerRuntimeCommitter()(
        SimpleNamespace(),
        SimpleNamespace(workspace_id=WORKSPACE),
        SimpleNamespace(snapshot=None, vector=None),
        SimpleNamespace(),
    )

    assert result is None


async def test_active_target_without_configured_policy_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RuntimeTargetRepository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active(self, *, workspace_id):
            assert workspace_id == WORKSPACE
            return object()

    monkeypatch.setattr(module, "PostgresScorerRuntimeTargetRepository", RuntimeTargetRepository)

    with pytest.raises(ScorerDeploymentRightsUnavailable, match="policy runtime"):
        await PostgresScorerRuntimeCommitter()(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(workspace_id=WORKSPACE),
            SimpleNamespace(snapshot=object(), vector=object()),
            SimpleNamespace(),
        )


@pytest.mark.parametrize(
    ("mode", "scorer_decision", "expected_status"),
    [
        (ScorerDeploymentMode.PAPER_GATE, RuntimeScorerDecision.VETO, BotDecisionStatus.VETO),
        (
            ScorerDeploymentMode.PAPER_GATE,
            RuntimeScorerDecision.ABSTAIN,
            BotDecisionStatus.ABSTAIN,
        ),
    ],
)
async def test_paper_gate_maps_veto_and_abstain_without_changing_decision_identity(
    mode: ScorerDeploymentMode,
    scorer_decision: RuntimeScorerDecision,
    expected_status: BotDecisionStatus,
) -> None:
    incumbent = await _decision(_job())
    result = SimpleNamespace(decision=scorer_decision, reason_code="OUT_OF_DISTRIBUTION")

    final = _final_decision(incumbent, mode, result)  # type: ignore[arg-type]

    assert final.decision_id == incumbent.decision_id
    assert final.idempotency_key == incumbent.idempotency_key
    assert final.status is expected_status
    assert final.candidate_json is None
    assert final.decision_snapshot_id == incumbent.decision_snapshot_id
    assert final.decision_hash != incumbent.decision_hash
    assert f"SCORER_{scorer_decision.value}:OUT_OF_DISTRIBUTION" in final.gate_details


async def test_shadow_veto_observation_never_changes_strategy_decision() -> None:
    incumbent = await _decision(_job())
    result = SimpleNamespace(decision=RuntimeScorerDecision.VETO, reason_code="LOW_CONFIDENCE")

    final = _final_decision(
        incumbent,
        ScorerDeploymentMode.SHADOW,
        result,  # type: ignore[arg-type]
    )

    assert final is incumbent
    assert final.status is BotDecisionStatus.CANDIDATE
    assert final.decision_hash == incumbent.decision_hash


async def test_paper_gate_approve_keeps_strategy_candidate() -> None:
    incumbent = await _decision(_job())
    result = SimpleNamespace(decision=RuntimeScorerDecision.APPROVE, reason_code=None)

    final = _final_decision(
        incumbent,
        ScorerDeploymentMode.PAPER_GATE,
        result,  # type: ignore[arg-type]
    )

    assert final is incumbent
