from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from trading_bot.application.learning_cpu_trainer import (
    LearningCpuArtifact,
    LearningCpuClassModel,
    LearningCpuValidationReport,
)
from trading_bot.application.learning_scorer_artifact import LearningScorerArtifact
from trading_bot.application.learning_scorer_evaluation import ScorerTestEvaluationPolicy
from trading_bot.domain.learning.feature_vector import (
    LEARNING_FEATURE_VECTOR_SCHEMA_VERSION,
    LearningFeatureVector,
    LearningFeatureVectorFeature,
    learning_feature_schema_hash,
)
from trading_bot.domain.learning.resources import (
    LearningResourceRequest,
    assess_learning_resource_request,
)
from trading_bot.domain.market.feature_snapshot import DataFreshness, MarketRegime
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshotTimeframeLineage,
    FeatureQuality,
    SeriesQuality,
)

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
RELEASE_ID = "h03-v1"
RELEASE_HASH = hashlib.sha256(b"h03-v1-content").hexdigest()


def _source_digest(timeframe: str, event_ids: tuple[str, ...], bar_ids: tuple[str, ...]) -> str:
    payload = {
        "timeframe": timeframe,
        "source_event_ids": list(event_ids),
        "source_bar_ids": list(bar_ids),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _vector(
    *, value: float = 2.0, freshness: DataFreshness = DataFreshness.FRESH
) -> LearningFeatureVector:
    event_ids = ("event:one",)
    bar_ids = ("bar:one",)
    lineage = DecisionSnapshotTimeframeLineage(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        timeframe="1h",
        feature_snapshot_id=hashlib.sha256(b"feature-snapshot").hexdigest(),
        schema_version="feature-snapshot-v1",
        schema_hash=hashlib.sha256(b"feature-schema").hexdigest(),
        formula_version="formula-v1",
        formula_hash=hashlib.sha256(b"formula").hexdigest(),
        code_hash=hashlib.sha256(b"feature-code").hexdigest(),
        feature_order=("momentum",),
        source_event_ids=event_ids,
        source_bar_ids=bar_ids,
        source_event_digest=_source_digest("1h", event_ids, bar_ids),
        as_of=NOW,
        freshness_as_of=NOW,
        available_at=NOW,
        freshness=freshness,
        series_quality=SeriesQuality.READY,
        data_coverage=1.0,
        warmup_by_feature=(("momentum", True),),
        quality_by_feature=(("momentum", FeatureQuality.READY),),
        quality_flags=(),
        regime_version="regime-v1",
        regime=MarketRegime.TREND,
        regime_confidence=1.0,
        regime_coverage=1.0,
        regime_reason_code="fixture",
        source_identity_pinned=True,
    )
    feature = LearningFeatureVectorFeature(
        key="1h:momentum",
        timeframe="1h",
        name="momentum",
        value=value,
        quality=FeatureQuality.READY,
        available_at=NOW,
        unit="ratio",
    )
    features = (feature,)
    lineages = (lineage,)
    return LearningFeatureVector(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        decision_time=NOW,
        snapshot_id=hashlib.sha256(b"decision-snapshot").hexdigest(),
        snapshot_hash=hashlib.sha256(b"decision-snapshot").hexdigest(),
        schema_version=LEARNING_FEATURE_VECTOR_SCHEMA_VERSION,
        feature_schema_hash=learning_feature_schema_hash(features, lineages),
        strategy_release_id=RELEASE_ID,
        strategy_release_content_hash=RELEASE_HASH,
        feature_keys=(feature.key,),
        features=features,
        timeframe_lineage=lineages,
    )


def _base_artifact(vector: LearningFeatureVector) -> LearningCpuArtifact:
    request = LearningResourceRequest(
        training_samples=3,
        validation_samples=9,
        feature_count=1,
        max_steps=30,
        max_runtime_seconds=30,
        memory_limit_bytes=512 * 1024 * 1024,
    )
    return LearningCpuArtifact(
        schema_version="crypidian.learning.cpu-logistic.v1",
        trainer_version="cpu-logistic-v1",
        dependency_fingerprint="f" * 64,
        feature_schema_hash=vector.feature_schema_hash,
        attempt_content_hash="1" * 64,
        split_policy_version="split-v3",
        workspace_id=str(WORKSPACE_ID),
        attempt_id=str(uuid4()),
        normalized_dataset_id=str(uuid4()),
        manifest_hash="a" * 64,
        fold_plan_hash="b" * 64,
        fold_id="c" * 64,
        approval_hash="d" * 64,
        batch_hash="e" * 64,
        seed=7,
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
        feature_names=vector.feature_keys,
        feature_means=(0.0,),
        feature_scales=(1.0,),
        models=(
            LearningCpuClassModel(-1, 0.0, (-4.0,), 1),
            LearningCpuClassModel(0, 0.0, (0.0,), 1),
            LearningCpuClassModel(1, 0.0, (4.0,), 1),
        ),
        training_class_counts=((-1, 1), (0, 1), (1, 1)),
        optimization_steps=3,
        actual_operation_units=1,
        validation_report=LearningCpuValidationReport(
            sample_count=9,
            accuracy=1.0,
            majority_baseline_accuracy=1 / 3,
            macro_recall=1.0,
            log_loss=0.1,
            majority_class=-1,
            confusion_matrix=((3, 0, 0), (0, 3, 0), (0, 0, 3)),
            class_support=((-1, 3), (0, 3), (1, 3)),
        ),
    )


def _scorer_artifact(
    base: LearningCpuArtifact, vector: LearningFeatureVector, *, temperature: float = 1.0
) -> LearningScorerArtifact:
    return LearningScorerArtifact(
        schema_version="crypidian.learning.scorer-temperature.v2",
        state="ready_for_evaluation",
        reason_code=None,
        workspace_id=base.workspace_id,
        attempt_id=base.attempt_id,
        normalized_dataset_id=base.normalized_dataset_id,
        manifest_hash=base.manifest_hash,
        fold_plan_hash=base.fold_plan_hash,
        fold_id=base.fold_id,
        approval_hash=base.approval_hash,
        base_artifact_hash=base.artifact_hash,
        strategy_release_id=vector.strategy_release_id,
        strategy_release_content_hash=vector.strategy_release_content_hash,
        vector_schema_version=vector.schema_version,
        vector_schema_hash=vector.feature_schema_hash,
        training_membership_digest="7" * 64,
        validation_membership_digest="8" * 64,
        training_sample_count=3,
        validation_sample_count=9,
        calibrator_method="temperature_scaling",
        calibrator_version="temperature-scaling-bisection-v1",
        calibration_policy_version="temperature-v1",
        calibration_policy_hash="9" * 64,
        temperature=temperature,
        validation_class_support=((-1, 3), (0, 3), (1, 3)),
        validation_nll_before=0.5,
        validation_nll_after=0.3,
        calibration_iterations=16,
        trainer_version="cpu-logistic-v1",
        trainer_code_version="wp06-scorer-artifact-v2",
        dependency_fingerprint="f" * 64,
    )


def _policy(**overrides: float) -> ScorerTestEvaluationPolicy:
    values: dict[str, object] = {
        "policy_version": "scorer-runtime-v1",
        "minimum_sample_count": 3,
        "minimum_class_support": 1,
        "maximum_calibration_error": 0.25,
        "maximum_log_loss_regression": 0.1,
        "maximum_brier_regression": 0.1,
        "minimum_coverage": 0.5,
        "minimum_candidate_retention": 0.5,
        "maximum_false_veto_rate": 0.5,
        "approve_probability_threshold": 0.7,
        "minimum_confidence": 0.6,
        "reliability_bin_count": 3,
    }
    values.update(overrides)
    return ScorerTestEvaluationPolicy(**values)  # type: ignore[arg-type]


def _score(**kwargs: object):
    from trading_bot.application.learning_scorer_runtime import (
        LearningScorerRuntimeRequest,
        score_runtime_candidate,
    )

    vector = kwargs.pop("vector", _vector())
    base = kwargs.pop("base", _base_artifact(vector))
    scorer = kwargs.pop("scorer", _scorer_artifact(base, vector))
    return score_runtime_candidate(
        request=LearningScorerRuntimeRequest(
            candidate_id="a" * 64,
            vector=vector,
            source_rights_eligible=kwargs.pop("source_rights_eligible", True),
            source_rights_hash=kwargs.pop("source_rights_hash", "b" * 64),
            is_out_of_distribution=kwargs.pop("is_out_of_distribution", False),
        ),
        base_artifact=base,
        scorer_artifact=scorer,
        policy=kwargs.pop("policy", _policy()),
    )


def test_runtime_scorer_returns_metadata_only_distribution_and_approves_positive_candidate() -> (
    None
):
    from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision

    result = _score()

    assert result.decision is RuntimeScorerDecision.APPROVE
    assert result.probability == result.class_probabilities[2][1]
    assert result.confidence == max(probability for _, probability in result.class_probabilities)
    assert result.uncertainty == pytest.approx(1.0 - result.confidence)
    assert sum(probability for _, probability in result.class_probabilities) == pytest.approx(1.0)
    assert result.vector_hash == _vector().vector_hash
    assert "features" not in {field.name for field in result.__dataclass_fields__.values()}


def test_runtime_scorer_vetoes_when_positive_probability_is_below_pinned_threshold() -> None:
    from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision

    result = _score(vector=_vector(value=-2.0))

    assert result.decision is RuntimeScorerDecision.VETO
    assert result.probability is not None
    assert result.probability < 0.7
    assert result.reason_code == "POSITIVE_PROBABILITY_BELOW_THRESHOLD"


def test_runtime_scorer_abstains_when_artifact_cannot_be_canonicalized() -> None:
    from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision

    vector = _vector()
    base = _base_artifact(vector)
    scorer = replace(_scorer_artifact(base, vector), temperature=float("nan"))
    result = _score(vector=vector, base=base, scorer=scorer)

    assert result.decision is RuntimeScorerDecision.ABSTAIN
    assert result.reason_code == "SCORER_ARTIFACT_INVALID"
    assert result.scorer_artifact_hash is None


@pytest.mark.parametrize(
    "invalid_lineage",
    [
        "source_available_at_missing",
        "source_available_at_after_cutoff",
        "feature_source_available_at_mismatch",
        "warmup_incomplete",
        "coverage_below_minimum",
    ],
)
def test_runtime_scorer_abstains_when_lineage_is_incomplete(invalid_lineage: str) -> None:
    from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision

    vector = _vector()
    lineage = vector.timeframe_lineage[0]
    if invalid_lineage == "source_available_at_missing":
        lineage = replace(lineage, available_at=None)
    elif invalid_lineage == "source_available_at_after_cutoff":
        lineage = replace(lineage, available_at=NOW + timedelta(minutes=1))
    elif invalid_lineage == "feature_source_available_at_mismatch":
        feature = replace(vector.features[0], available_at=NOW - timedelta(seconds=1))
        vector = replace(vector, features=(feature,))
    elif invalid_lineage == "warmup_incomplete":
        lineage = replace(lineage, warmup_by_feature=(("momentum", False),))
    else:
        lineage = replace(lineage, data_coverage=0.0)
    vector = replace(vector, timeframe_lineage=(lineage,))

    result = _score(vector=vector)

    assert result.decision is RuntimeScorerDecision.ABSTAIN
    assert result.reason_code == "STALE_OR_UNKNOWN_FEATURES"
    assert result.probability is None
    assert result.class_probabilities == ()


@pytest.mark.parametrize("malformed", ["means", "scales", "coefficients", "intercept"])
def test_runtime_scorer_abstains_for_malformed_numeric_artifact_fields(malformed: str) -> None:
    from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision

    vector = _vector()
    base = _base_artifact(vector)
    if malformed == "means":
        base = replace(base, feature_means=("not-a-number",))
    elif malformed == "scales":
        base = replace(base, feature_scales=("not-a-number",))
    elif malformed == "coefficients":
        model = replace(base.models[0], coefficients=("not-a-number",))
        base = replace(base, models=(model, *base.models[1:]))
    else:
        model = replace(base.models[0], intercept="not-a-number")
        base = replace(base, models=(model, *base.models[1:]))
    scorer = _scorer_artifact(base, vector)

    result = _score(vector=vector, base=base, scorer=scorer)

    assert result.decision is RuntimeScorerDecision.ABSTAIN
    assert result.reason_code == "SCORER_FEATURE_STATISTICS_INVALID"
    assert result.probability is None
    assert result.class_probabilities == ()


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"scorer": "not-ready"}, "SCORER_ARTIFACT_NOT_READY"),
        ({"is_out_of_distribution": True}, "OUT_OF_DISTRIBUTION"),
        ({"source_rights_eligible": None, "source_rights_hash": None}, "CURRENT_RIGHTS_UNVERIFIED"),
        (
            {"source_rights_eligible": False, "source_rights_hash": "b" * 64},
            "SOURCE_RIGHTS_REVOKED",
        ),
        ({"source_rights_eligible": True, "source_rights_hash": None}, "CURRENT_RIGHTS_UNVERIFIED"),
        ({"vector": _vector(freshness=DataFreshness.STALE)}, "STALE_OR_UNKNOWN_FEATURES"),
        ({"vector": _vector(freshness=DataFreshness.UNKNOWN)}, "STALE_OR_UNKNOWN_FEATURES"),
        (
            {"vector": _vector(value=0.0), "policy": _policy(minimum_confidence=0.5)},
            "LOW_CONFIDENCE",
        ),
    ],
)
def test_runtime_scorer_abstains_closed_for_untrusted_input(
    kwargs: dict[str, object], reason: str
) -> None:
    from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision

    if kwargs.get("scorer") == "not-ready":
        vector = _vector()
        base = _base_artifact(vector)
        kwargs = {**kwargs, "scorer": replace(_scorer_artifact(base, vector), state="abstain")}
    result = _score(**kwargs)

    assert result.decision is RuntimeScorerDecision.ABSTAIN
    assert result.reason_code == reason
    assert result.probability is None
    assert result.class_probabilities == ()


@pytest.mark.parametrize("mismatch", ["schema", "release", "feature", "base"])
def test_runtime_scorer_abstains_for_cross_lineage_artifacts(mismatch: str) -> None:
    from trading_bot.application.learning_scorer_runtime import RuntimeScorerDecision

    vector = _vector()
    base = _base_artifact(vector)
    scorer = _scorer_artifact(base, vector)
    if mismatch == "schema":
        scorer = replace(scorer, vector_schema_hash="c" * 64)
        expected_reason = "SCORER_VECTOR_SCHEMA_MISMATCH"
    elif mismatch == "release":
        vector = replace(
            vector,
            strategy_release_content_hash=hashlib.sha256(b"different-release").hexdigest(),
        )
        expected_reason = "SCORER_STRATEGY_RELEASE_MISMATCH"
    elif mismatch == "feature":
        base = replace(base, feature_names=("wrong-feature",))
        scorer = _scorer_artifact(base, vector)
        expected_reason = "SCORER_FEATURE_SCHEMA_MISMATCH"
    else:
        base = replace(base, batch_hash="0" * 64)
        expected_reason = "SCORER_BASE_ARTIFACT_HASH_MISMATCH"

    result = _score(vector=vector, base=base, scorer=scorer)

    assert result.decision is RuntimeScorerDecision.ABSTAIN
    assert result.reason_code == expected_reason
