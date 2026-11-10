"""Contract opportunity metadata và fixture nguồn pinned dùng chung cho Task 1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.backtest_dataset_replay_context import (
    BacktestDatasetRuntimeEvaluatorFactory,
)
from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.domain.execution.paper import PaperExecutionGateway
from trading_bot.domain.strategy.registry import StrategyRegistry


def _pinned_setup(*, cross=False, intent="TRAINING_BASE", close_on_next=True):
    from tests.unit.test_learning_feature_vector import BASE_TIME, _bars, _replay_bundle
    from tests.unit.test_runtime_replay_evaluator import _setup

    one_hour = list(_bars("1h", count=228, last_open_time=BASE_TIME + timedelta(hours=227)))
    if cross:
        for index, original in enumerate(one_hour):
            price = Decimal("120") - Decimal(index) / 10 if index < 224 else Decimal("180")
            checksum = hashlib.sha256(f"opportunity:{index}:{price}".encode()).hexdigest()
            one_hour[index] = replace(
                original,
                open=price,
                high=price + 3,
                low=Decimal("80") if index == 225 and close_on_next else price - 1,
                close=price + 2,
                checksum=checksum,
                source_identity=replace(original.source_identity, checksum=checksum),
            )
    four_hour = _bars("4h", count=207, last_open_time=BASE_TIME + timedelta(hours=224))
    source = _replay_bundle({"1h": tuple(one_hour), "4h": four_hour})
    observations = build_backtest_dataset_replay_observations(source, strategy_release_id="h01-v1")
    _, seed, _, gateway, old_manifest = _setup()
    from trading_bot.application.learning_feature_vector import build_learning_feature_vector

    schema_hash = build_learning_feature_vector(
        observations[0].snapshot,
        strategy_release_id="h01-v1",
        strategy_release_content_hash=old_manifest.strategy_content_hash,
    ).feature_schema_hash
    seed = replace(seed, feature_schema_hash=schema_hash)
    captured = observations[0].snapshot.decision_time - timedelta(seconds=1)
    seed = replace(
        seed,
        account=replace(seed.account, captured_at=captured),
        risk_state=replace(seed.risk_state, account_captured_at=captured),
    )
    manifest = replace(
        old_manifest,
        dataset_hash=source.dataset_hash,
        range_start=source.evaluation_start,
        range_end=source.range_end,
        learning_intent=intent,
        feature_schema_hash=schema_hash,
        learning_job_id="11111111-1111-4111-8111-111111111111"
        if intent == "TRAINING_BASE"
        else None,
    )
    factory = BacktestDatasetRuntimeEvaluatorFactory(
        source,
        observations,
        StrategyRegistry(),
        "1h",
        lambda _scenario, _manifest: seed,
        lambda _scenario, _manifest: PaperExecutionGateway(gateway.policy),
        opposing_signal_provider_factory=lambda _scenario, _manifest: (
            lambda _bar, _observation: False
        ),
    )
    return source, observations, seed, manifest, factory


def _opportunity(**changes):
    from trading_bot.application.backtest_decision_opportunities import (
        ReplayDecisionOpportunity,
        metadata_hash,
    )

    at = datetime(2026, 11, 1, tzinfo=UTC)
    values = {
        "workspace_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "manifest_hash": "a" * 64,
        "bundle_hash": "b" * 64,
        "scenario_id": "base",
        "symbol": "BTCUSDT",
        "strategy_release_id": "h01-v1",
        "strategy_content_hash": "c" * 64,
        "decision_timeframe": "1h",
        "evaluation_start": at,
        "range_end": at + timedelta(hours=4),
        "event_id": "source-event",
        "decision_ordinal": 0,
        "event_at": at,
        "available_at": at,
        "decision_at": at,
        "snapshot_id": "d" * 64,
        "runtime_snapshot_id": "e" * 64,
        "feature_snapshot_ids": (("1h", "f" * 64), ("4h", "1" * 64)),
        "source_event_digests": (("1h", "a" * 64), ("4h", "b" * 64)),
        "source_refs_digest": metadata_hash(
            [
                {"timeframe": timeframe, "source_event_digest": digest}
                for timeframe, digest in (("1h", "a" * 64), ("4h", "b" * 64))
            ]
        ),
        "feature_schema_hash": "3" * 64,
        "vector_hash": "4" * 64,
        "feature_values_hash": "5" * 64,
        "input_state": "EVALUATED",
        "strategy_state": "NO_CANDIDATE",
        "decision_outcome": "NO_TRADE",
        "reason_code": "H01_NO_CROSS",
        "candidate_id": None,
    }
    values.update(changes)
    return ReplayDecisionOpportunity(**values)


def test_opportunity_is_immutable_canonical_metadata_without_payload():
    opportunity = _opportunity()
    payload = json.loads(opportunity.canonical_json())
    assert (
        opportunity.content_hash
        == hashlib.sha256(opportunity.canonical_json().encode()).hexdigest()
    )
    assert opportunity.content_hash == _opportunity().content_hash
    assert (
        opportunity.idempotency_key == _opportunity(reason_code="H01_OTHER_REASON").idempotency_key
    )
    assert opportunity.content_hash != _opportunity(reason_code="H01_OTHER_REASON").content_hash
    assert payload["query_version"] == "base-decision-opportunity-universe-v1"
    assert (
        not {"features", "values", "bars", "entry_features", "source_rights_fingerprint"}
        & payload.keys()
    )
    with pytest.raises(FrozenInstanceError):
        opportunity.reason_code = "mutated"


@pytest.mark.parametrize(
    "changes",
    [
        {"input_state": "INPUT_EXCLUDED"},
        {"candidate_id": "a" * 64},
        {"vector_hash": None},
        {"decision_ordinal": -1},
        {"source_refs_digest": "bad"},
        {"feature_snapshot_ids": [("1h", "a" * 64)]},
    ],
)
def test_opportunity_rejects_inconsistent_states_and_metadata(changes):
    with pytest.raises(ValueError, match="Input|Opportunity|candidate|ordinal|digest|snapshot"):
        _opportunity(**changes)


def test_excluded_input_has_no_candidate_or_synthetic_vector():
    opportunity = _opportunity(
        input_state="INPUT_EXCLUDED",
        strategy_state="NOT_RUN",
        reason_code="INPUT_NOT_READY",
        vector_hash=None,
        feature_values_hash=None,
        feature_schema_hash=None,
        runtime_snapshot_id=None,
    )
    assert opportunity.strategy_state == "NOT_RUN"
    assert opportunity.candidate_id is None
    assert opportunity.vector_hash is None


def test_valid_input_can_stop_before_strategy_without_becoming_input_excluded():
    opportunity = _opportunity(strategy_state="NOT_RUN", reason_code="SETTINGS_NOT_EFFECTIVE")
    assert opportunity.input_state == "EVALUATED"
    assert opportunity.strategy_state == "NOT_RUN"
    assert opportunity.vector_hash is not None
