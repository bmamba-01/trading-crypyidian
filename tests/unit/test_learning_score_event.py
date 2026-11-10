from __future__ import annotations

import hashlib
import json
from uuid import UUID

import pytest

from trading_bot.application.bot_dispatcher import BotDecisionStatus
from trading_bot.domain.learning.score_event import (
    LearningScoreDecision,
    LearningScoreEvent,
)
from trading_bot.domain.learning.scoring_deployment import ScorerDeploymentMode


def _event(**overrides: object) -> LearningScoreEvent:
    values: dict[str, object] = {
        "workspace_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "bot_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "bot_decision_id": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        "bot_decision_hash": "2" * 64,
        "incumbent_decision_hash": "2" * 64,
        "incumbent_status": BotDecisionStatus.CANDIDATE,
        "final_status": BotDecisionStatus.CANDIDATE,
        "candidate_id": "3" * 64,
        "candidate_hash": "4" * 64,
        "decision_snapshot_id": "5" * 64,
        "vector_hash": "6" * 64,
        "feature_schema_version": "crypidian-learning-feature-vector-v1",
        "feature_schema_hash": "7" * 64,
        "timeframe_snapshot_ids": (("1h", "8" * 64),),
        "source_event_digests": (("1h", "9" * 64),),
        "source_rights_fingerprint": "a" * 64,
        "mode": ScorerDeploymentMode.SHADOW,
        "deployment_event_id": UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        "deployment_revision": 2,
        "registry_id": UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        "registry_revision": 3,
        "active_pointer_version": 4,
        "scorer_artifact_id": UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
        "scorer_artifact_hash": "b" * 64,
        "base_artifact_hash": "0" * 64,
        "evaluation_report_id": UUID("11111111-1111-4111-8111-111111111111"),
        "evaluation_report_hash": "c" * 64,
        "evaluation_status": "ACCEPTED_SHADOW_ONLY",
        "report_scope_hash": "f" * 64,
        "proof_id": UUID("22222222-2222-4222-8222-222222222222"),
        "proof_hash": "d" * 64,
        "proof_scope_hash": "f" * 64,
        "candidate_completeness_verified": False,
        "policy_id": "wp06-scorer-policy-v1",
        "policy_hash": "e" * 64,
        "decision": LearningScoreDecision.APPROVE,
        "reason_code": None,
        "class_probabilities": ((-1, 0.2), (0, 0.3), (1, 0.5)),
        "probability": 0.5,
        "confidence": 0.5,
        "uncertainty": 0.5,
        "model_latency_ms": 2,
    }
    values.update(overrides)
    return LearningScoreEvent.create(**values)  # type: ignore[arg-type]


def test_score_event_is_canonical_metadata_and_stable_for_same_decision() -> None:
    first = _event()
    retried = _event()
    payload = json.loads(first.canonical_json)

    assert first.idempotency_key == retried.idempotency_key
    assert first.event_hash == retried.event_hash
    assert hashlib.sha256(first.canonical_json.encode()).hexdigest() == first.event_hash
    assert payload["decision_snapshot_id"] == "5" * 64
    assert payload["vector_hash"] == "6" * 64
    assert payload["timeframe_snapshot_ids"] == [["1h", "8" * 64]]
    assert payload["source_event_digests"] == [["1h", "9" * 64]]
    assert (
        not {
            "features",
            "feature_values",
            "labels",
            "sample_ids",
            "test_payload",
            "raw_source",
        }
        & payload.keys()
    )
    assert "candidate_json" not in payload


def test_same_score_idempotency_key_with_changed_pointer_changes_event_hash() -> None:
    original = _event()
    changed = _event(active_pointer_version=5)

    assert changed.idempotency_key == original.idempotency_key
    assert changed.event_hash != original.event_hash


def test_score_event_rejects_incomplete_distribution_and_ineligible_gate() -> None:
    with pytest.raises(ValueError, match="distribution"):
        _event(class_probabilities=(), probability=None, confidence=None, uncertainty=None)

    with pytest.raises(ValueError, match="PAPER_GATE"):
        _event(
            mode=ScorerDeploymentMode.PAPER_GATE,
            evaluation_status="ACCEPTED_SHADOW_ONLY",
            candidate_completeness_verified=False,
        )

    with pytest.raises(ValueError, match="candidate_hash"):
        _event(candidate_hash="not-a-hash")


def test_shadow_event_keeps_the_strategy_decision_hash_unchanged() -> None:
    with pytest.raises(ValueError, match="SHADOW.*hash"):
        _event(bot_decision_hash="a" * 64)
