from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from trading_bot.domain.learning.scoring import (
    ModelInvocationStatus,
    ScoreDecision,
    ScoreReason,
    ScoreRequest,
    ScoreResult,
    ScoringPolicy,
    score_candidate,
)
from trading_bot.domain.market.feature_snapshot import (
    DataFreshness,
    FeatureSnapshot,
    MarketRegime,
    RegimeAssessment,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FEATURE_SCHEMA_HASH = "b" * 64


def snapshot(**changes: object) -> FeatureSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "snapshot-1",
        "content_hash": "a" * 64,
        "workspace_id": WORKSPACE_ID,
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "as_of": NOW,
        "computed_at": NOW + timedelta(seconds=1),
        "schema_hash": FEATURE_SCHEMA_HASH,
        "formula_hash": "c" * 64,
        "code_hash": "d" * 64,
        "input_bar_ids": ("bar-1",),
        "input_event_ids": ("event-1",),
        "features": {"ema_20": 100.0, "adx_14": 24.0},
        "warmup": {"ema_20": True, "adx_14": True},
        "quality_flags": (),
        "data_freshness": DataFreshness.FRESH,
        "data_coverage": 1.0,
        "regime": RegimeAssessment(
            MarketRegime.TREND, 0.9, 1.0, "TREND_CONFIRMED", "Xu hướng đủ dữ liệu."
        ),
        "is_usable": True,
    }
    values.update(changes)
    return FeatureSnapshot(**values)  # type: ignore[arg-type]


def policy(**changes: object) -> ScoringPolicy:
    values: dict[str, object] = {
        "policy_version": "score-policy-v1",
        "model_id": "model-1",
        "model_version": "model-v3",
        "response_schema_version": "score-response-v1",
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "required_features": ("ema_20", "adx_14"),
        "max_snapshot_age": timedelta(minutes=5),
        "max_model_latency": timedelta(seconds=2),
        "minimum_coverage": 0.9,
        "minimum_confidence": 0.7,
        "approve_threshold": 0.6,
    }
    values.update(changes)
    return ScoringPolicy(**values)  # type: ignore[arg-type]


def request(**changes: object) -> ScoreRequest:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "strategy_id": "H01",
        "strategy_version": "release-7",
        "strategy_hash": "f" * 64,
        "snapshot": snapshot(),
        "context_refs": ("macro:fixture-1",),
        "context_hash": hashlib.sha256(b'["macro:fixture-1"]').hexdigest(),
        "requested_at": NOW + timedelta(seconds=2),
        "source_dependent": False,
        "source_rights_eligible": None,
        "source_rights_hash": None,
        "is_out_of_distribution": False,
    }
    values.update(changes)
    return ScoreRequest(**values)  # type: ignore[arg-type]


def response(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "model_id": "model-1",
        "model_version": "model-v3",
        "schema_version": "score-response-v1",
        "probability": 0.8,
        "confidence": 0.9,
        "uncertainty": 0.1,
        "calibrated": True,
    }
    values.update(changes)
    return values


def score(
    *,
    request_value: ScoreRequest | None = None,
    response_value: object = None,
    status: ModelInvocationStatus = ModelInvocationStatus.SUCCEEDED,
):
    return score_candidate(
        request_value or request(),
        policy(),
        invocation_status=status,
        model_response=response() if response_value is None else response_value,
        model_latency=timedelta(milliseconds=15),
    )


def test_valid_score_maps_probability_to_approve_or_veto_without_order_capability() -> None:
    approved = score()
    vetoed = score(response_value=response(probability=0.2))

    assert approved.decision is ScoreDecision.APPROVE
    assert vetoed.decision is ScoreDecision.VETO
    assert approved.snapshot_id == "snapshot-1"
    assert approved.snapshot_hash == "a" * 64
    assert approved.model_version == "model-v3"
    assert approved.strategy_version == "release-7"
    assert approved.strategy_hash == "f" * 64
    assert approved.context_refs == ("macro:fixture-1",)
    assert approved.regime is MarketRegime.TREND
    assert approved.evidence_refs == ("bar-1", "event-1")
    assert approved.uncertainty == 0.1
    assert approved.model_latency == timedelta(milliseconds=15)
    assert not hasattr(approved, "order_intent")


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (ModelInvocationStatus.TIMEOUT, ScoreReason.MODEL_TIMEOUT),
        (ModelInvocationStatus.UNAVAILABLE, ScoreReason.MODEL_UNAVAILABLE),
        (ModelInvocationStatus.FAILED, ScoreReason.MODEL_FAILED),
    ],
)
def test_model_execution_failures_abstain(
    status: ModelInvocationStatus, reason: ScoreReason
) -> None:
    result = score(status=status)

    assert result.decision is ScoreDecision.ABSTAIN
    assert result.reason_codes == (reason,)
    assert result.probability is None
    assert result.model_version == "model-v3"


@pytest.mark.parametrize(
    ("request_changes", "policy_changes", "reason"),
    [
        ({"requested_at": NOW - timedelta(seconds=1)}, {}, ScoreReason.FUTURE_SNAPSHOT),
        (
            {
                "snapshot": snapshot(computed_at=NOW + timedelta(seconds=3)),
                "requested_at": NOW + timedelta(seconds=2),
            },
            {},
            ScoreReason.FUTURE_SNAPSHOT,
        ),
        ({"requested_at": NOW + timedelta(minutes=10)}, {}, ScoreReason.STALE_SNAPSHOT),
        ({"snapshot": snapshot(is_usable=False)}, {}, ScoreReason.UNUSABLE_SNAPSHOT),
        (
            {"snapshot": snapshot(data_freshness=DataFreshness.STALE)},
            {},
            ScoreReason.STALE_SNAPSHOT,
        ),
        (
            {"snapshot": snapshot(data_coverage=0.5)},
            {},
            ScoreReason.LOW_COVERAGE,
        ),
        (
            {"snapshot": snapshot(schema_hash="f" * 64)},
            {},
            ScoreReason.FEATURE_SCHEMA_MISMATCH,
        ),
        (
            {"snapshot": snapshot(features={"ema_20": None, "adx_14": 24.0})},
            {},
            ScoreReason.MISSING_FEATURE,
        ),
        (
            {"snapshot": snapshot(warmup={"ema_20": False, "adx_14": True})},
            {},
            ScoreReason.FEATURE_WARMUP_INCOMPLETE,
        ),
        ({"is_out_of_distribution": True}, {}, ScoreReason.OUT_OF_DISTRIBUTION),
        (
            {"source_dependent": True, "source_rights_eligible": False},
            {},
            ScoreReason.SOURCE_RIGHTS_INELIGIBLE,
        ),
        (
            {
                "source_dependent": True,
                "source_rights_eligible": True,
                "source_rights_hash": "e" * 64,
            },
            {},
            None,
        ),
    ],
)
def test_input_gates_fail_closed(
    request_changes: dict[str, object],
    policy_changes: dict[str, object],
    reason: ScoreReason | None,
) -> None:
    del policy_changes
    result = score(request_value=request(**request_changes))

    if reason is None:
        assert result.decision is ScoreDecision.APPROVE
    else:
        assert result.decision is ScoreDecision.ABSTAIN
        assert reason in result.reason_codes


@pytest.mark.parametrize(
    ("response_changes", "reason"),
    [
        ({"extra": "field"}, ScoreReason.INVALID_RESPONSE_SCHEMA),
        ({"probability": True}, ScoreReason.INVALID_RESPONSE_SCHEMA),
        ({"probability": float("nan")}, ScoreReason.INVALID_RESPONSE_SCHEMA),
        ({"probability": 1.1}, ScoreReason.INVALID_RESPONSE_SCHEMA),
        ({"probability": 10**1000}, ScoreReason.INVALID_RESPONSE_SCHEMA),
        ({"confidence": 0.69}, ScoreReason.LOW_CONFIDENCE),
        ({"calibrated": False}, ScoreReason.UNCALIBRATED_SCORE),
        ({"model_version": "old-model"}, ScoreReason.MODEL_VERSION_MISMATCH),
        ({"schema_version": "old-schema"}, ScoreReason.INVALID_RESPONSE_SCHEMA),
    ],
)
def test_invalid_or_untrusted_model_response_abstains(
    response_changes: dict[str, object], reason: ScoreReason
) -> None:
    result = score(response_value=response(**response_changes))

    assert result.decision is ScoreDecision.ABSTAIN
    assert reason in result.reason_codes


def test_policy_and_request_reject_invalid_bounds_naive_time_and_rights_ambiguity() -> None:
    with pytest.raises(ValueError, match="minimum_coverage"):
        policy(minimum_coverage=1.1)
    with pytest.raises(ValueError, match="max_model_latency"):
        policy(max_model_latency=timedelta(0))
    with pytest.raises(ValueError, match="requested_at"):
        request(requested_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="source_rights_eligible"):
        request(source_dependent=True, source_rights_eligible=None)
    with pytest.raises(ValueError, match="source_rights_hash"):
        request(source_dependent=True, source_rights_eligible=True)
    with pytest.raises(ValueError, match="không áp dụng"):
        request(source_dependent=False, source_rights_eligible=True)
    with pytest.raises(ValueError, match="không áp dụng"):
        request(source_dependent=False, source_rights_hash="e" * 64)


def test_out_of_distribution_input_and_missing_rights_are_strict_booleans() -> None:
    with pytest.raises(ValueError, match="is_out_of_distribution"):
        request(is_out_of_distribution="false")
    with pytest.raises(ValueError, match="source_rights_eligible"):
        request(source_dependent=True, source_rights_eligible=1)


def test_request_pins_strategy_version_and_canonical_context_hash() -> None:
    with pytest.raises(ValueError, match="strategy_hash"):
        request(strategy_hash="bad")
    with pytest.raises(ValueError, match="context_hash"):
        request(context_hash="0" * 64)


def test_model_latency_above_policy_abstains_and_direct_results_are_strict() -> None:
    delayed = score_candidate(
        request(),
        policy(),
        invocation_status=ModelInvocationStatus.SUCCEEDED,
        model_response=response(),
        model_latency=timedelta(seconds=3),
    )
    assert delayed.decision is ScoreDecision.ABSTAIN
    assert ScoreReason.MODEL_LATENCY_EXCEEDED in delayed.reason_codes
    assert delayed.model_latency == timedelta(seconds=3)

    valid = score()
    assert isinstance(valid, ScoreResult)
    with pytest.raises(ValueError, match="model identity"):
        replace(valid, model_id=None)
    with pytest.raises(ValueError, match="ScoreReason"):
        replace(
            valid,
            decision=ScoreDecision.ABSTAIN,
            probability=None,
            confidence=None,
            reason_codes=("unknown",),  # type: ignore[arg-type]
        )


def test_policy_required_feature_list_is_canonical_and_unique() -> None:
    configured = policy(required_features=("adx_14", "ema_20", "adx_14"))

    assert configured.required_features == ("adx_14", "ema_20")
