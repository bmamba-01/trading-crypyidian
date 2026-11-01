from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from trading_bot.domain.learning.scoring import (
    ModelInvocationStatus,
    ScoreDecision,
    ScoreReason,
    ScoreRequest,
    ScoringPolicy,
)
from trading_bot.domain.market.feature_snapshot import (
    DataFreshness,
    FeatureSnapshot,
    MarketRegime,
    RegimeAssessment,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SCHEMA_HASH = "b" * 64


def scoring_policy(**changes: Any) -> ScoringPolicy:
    values: dict[str, Any] = {
        "policy_version": "score-policy-v1",
        "model_id": "model-1",
        "model_version": "model-v3",
        "response_schema_version": "score-response-v1",
        "feature_schema_hash": SCHEMA_HASH,
        "required_features": ("adx_14", "ema_20"),
        "max_snapshot_age": timedelta(minutes=5),
        "max_model_latency": timedelta(seconds=2),
        "minimum_coverage": 0.9,
        "minimum_confidence": 0.7,
        "approve_threshold": 0.6,
    }
    values.update(changes)
    return ScoringPolicy(**values)


def feature_snapshot(**changes: Any) -> FeatureSnapshot:
    values: dict[str, Any] = {
        "snapshot_id": "snapshot-1",
        "content_hash": "a" * 64,
        "workspace_id": WORKSPACE_ID,
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "as_of": NOW,
        "computed_at": NOW + timedelta(seconds=1),
        "schema_hash": SCHEMA_HASH,
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
    return FeatureSnapshot(**values)


def score_request(**changes: Any) -> ScoreRequest:
    context_refs = ("macro:fixture-1",)
    values: dict[str, Any] = {
        "candidate_id": "candidate-1",
        "strategy_id": "H01",
        "strategy_version": "release-7",
        "strategy_hash": "f" * 64,
        "snapshot": feature_snapshot(),
        "context_refs": context_refs,
        "context_hash": hashlib.sha256(b'["macro:fixture-1"]').hexdigest(),
        "requested_at": NOW + timedelta(seconds=2),
        "source_dependent": False,
        "source_rights_eligible": None,
        "source_rights_hash": None,
        "is_out_of_distribution": False,
    }
    values.update(changes)
    return ScoreRequest(**values)


def model_response(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
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


class StubModelPort:
    def __init__(self, result: object) -> None:
        self.result = result
        self.received: tuple[ScoreRequest, ScoringPolicy] | None = None

    async def invoke(self, request: ScoreRequest, policy: ScoringPolicy) -> object:
        self.received = (request, policy)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_application_scorer_passes_explicit_snapshot_context_and_policy_to_model_port() -> (
    None
):
    from trading_bot.application.learning_scoring import (
        LearningScoringService,
        ModelInvocation,
    )

    request = score_request()
    policy = scoring_policy()
    port = StubModelPort(ModelInvocation(ModelInvocationStatus.SUCCEEDED, model_response()))
    service = LearningScoringService(port)

    result = await service.score(request=request, policy=policy)

    assert port.received == (request, policy)
    assert result.decision is ScoreDecision.APPROVE
    assert result.snapshot_id == request.snapshot.snapshot_id
    assert result.context_refs == request.context_refs
    assert result.policy_version == policy.policy_version
    assert result.model_latency >= timedelta(0)


@pytest.mark.asyncio
async def test_application_scorer_maps_model_port_exception_to_fail_closed_abstain() -> None:
    from trading_bot.application.learning_scoring import LearningScoringService

    service = LearningScoringService(StubModelPort(RuntimeError("private detail")))

    result = await service.score(request=score_request(), policy=scoring_policy())

    assert result.decision is ScoreDecision.ABSTAIN
    assert result.reason_codes == (ScoreReason.MODEL_FAILED,)
    assert result.probability is None


@pytest.mark.asyncio
async def test_application_scorer_maps_adapter_status_to_existing_abstain_reason() -> None:
    from trading_bot.application.learning_scoring import (
        LearningScoringService,
        ModelInvocation,
    )

    service = LearningScoringService(
        StubModelPort(ModelInvocation(ModelInvocationStatus.UNAVAILABLE, None))
    )

    result = await service.score(request=score_request(), policy=scoring_policy())

    assert result.decision is ScoreDecision.ABSTAIN
    assert result.reason_codes == (ScoreReason.MODEL_UNAVAILABLE,)


@pytest.mark.asyncio
async def test_application_scorer_preflight_source_rights_before_invoking_model() -> None:
    from trading_bot.application.learning_scoring import LearningScoringService

    port = StubModelPort(RuntimeError("Không được gọi model khi quyền nguồn bị từ chối."))
    service = LearningScoringService(port)
    request = score_request(
        source_dependent=True,
        source_rights_eligible=False,
    )

    result = await service.score(request=request, policy=scoring_policy())

    assert port.received is None
    assert result.decision is ScoreDecision.ABSTAIN
    assert result.reason_codes == (ScoreReason.SOURCE_RIGHTS_INELIGIBLE,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot_changes", "reason"),
    [
        ({"data_freshness": DataFreshness.STALE}, ScoreReason.STALE_SNAPSHOT),
        ({"schema_hash": "e" * 64}, ScoreReason.FEATURE_SCHEMA_MISMATCH),
    ],
)
async def test_application_scorer_preflight_invalid_snapshot_without_model_call(
    snapshot_changes: dict[str, object],
    reason: ScoreReason,
) -> None:
    from trading_bot.application.learning_scoring import LearningScoringService

    port = StubModelPort(RuntimeError("Không được gọi model khi snapshot không hợp lệ."))
    request = score_request(snapshot=feature_snapshot(**snapshot_changes))
    service = LearningScoringService(port)

    result = await service.score(request=request, policy=scoring_policy())

    assert port.received is None
    assert result.decision is ScoreDecision.ABSTAIN
    assert result.reason_codes == (reason,)


@pytest.mark.asyncio
async def test_application_scorer_enforces_policy_timeout_and_cancels_model_call() -> None:
    from trading_bot.application.learning_scoring import LearningScoringService

    class SlowPort:
        def __init__(self) -> None:
            self.cancelled = False

        async def invoke(self, request: ScoreRequest, policy: ScoringPolicy) -> object:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("Lời gọi model phải bị hủy khi hết hạn.")

    port = SlowPort()
    policy = scoring_policy(max_model_latency=timedelta(milliseconds=20))
    service = LearningScoringService(port)

    result = await service.score(request=score_request(), policy=policy)

    await asyncio.sleep(0)
    assert port.cancelled is True
    assert result.decision is ScoreDecision.ABSTAIN
    assert result.reason_codes == (ScoreReason.MODEL_TIMEOUT,)
    assert result.probability is None


@pytest.mark.asyncio
async def test_application_scorer_returns_at_deadline_when_port_suppresses_cancellation() -> None:
    from trading_bot.application.learning_scoring import LearningScoringService

    class CancellationResistantPort:
        def __init__(self) -> None:
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()
            self.cleaned = asyncio.Event()

        async def invoke(self, request: ScoreRequest, policy: ScoringPolicy) -> object:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release.wait()
                self.cleaned.set()
                return model_response()

    port = CancellationResistantPort()
    policy = scoring_policy(max_model_latency=timedelta(milliseconds=20))
    service = LearningScoringService(port)

    scoring_task = asyncio.create_task(service.score(request=score_request(), policy=policy))
    await asyncio.wait_for(port.cancelled.wait(), timeout=0.2)
    completed_before_release, _ = await asyncio.wait({scoring_task}, timeout=0.1)
    port.release.set()
    result = await asyncio.wait_for(scoring_task, timeout=0.2)

    assert scoring_task in completed_before_release
    assert result.decision is ScoreDecision.ABSTAIN
    assert result.reason_codes == (ScoreReason.MODEL_TIMEOUT,)
    await asyncio.wait_for(port.cleaned.wait(), timeout=0.2)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_application_scorer_rejects_malformed_port_result_as_model_failure() -> None:
    from trading_bot.application.learning_scoring import LearningScoringService

    service = LearningScoringService(StubModelPort({"status": "succeeded"}))

    result = await service.score(request=score_request(), policy=scoring_policy())

    assert result.decision is ScoreDecision.ABSTAIN
    assert result.reason_codes == (ScoreReason.MODEL_FAILED,)
