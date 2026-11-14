from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.learning.paper_performance import (
    PaperPerformanceCohort,
    PaperPerformancePolicy,
    PaperPerformanceStatus,
    assess_paper_performance,
    score_multiclass_outcomes,
)

AS_OF = datetime(2026, 11, 13, tzinfo=UTC)
CLASSES = (-1, 0, 1)


def _cohort(
    *,
    index: int,
    brier_delta: str = "0.005",
    log_loss_delta: str = "0.005",
    eligible: int = 1_250,
    covered: int = 1_000,
    support: tuple[int, int, int] = (334, 333, 333),
) -> PaperPerformanceCohort:
    end = AS_OF - timedelta(days=30 + 30 * (1 - index))
    start = end - timedelta(days=30)
    return PaperPerformanceCohort(
        window_start=start,
        window_end=end,
        eligible_decision_count=eligible,
        covered_outcome_count=covered,
        class_support=tuple(zip(CLASSES, support, strict=True)),
        observed_brier=(Decimal("0.2") + Decimal(brier_delta) if covered else None),
        observed_log_loss=(Decimal("0.6") + Decimal(log_loss_delta) if covered else None),
        baseline_brier=Decimal("0.2"),
        baseline_log_loss=Decimal("0.6"),
        membership_digest=f"{index + 1:064x}",
    )


def test_multiclass_scores_use_class_order_minus_one_zero_plus_one() -> None:
    probabilities = (
        (Decimal("0.8"), Decimal("0.1"), Decimal("0.1")),
        (Decimal("0.1"), Decimal("0.8"), Decimal("0.1")),
        (Decimal("0.1"), Decimal("0.1"), Decimal("0.8")),
    )

    brier, log_loss = score_multiclass_outcomes(probabilities, (-1, 0, 1))

    assert brier == Decimal("0.06")
    assert abs(log_loss + Decimal("0.8").ln()) < Decimal("1e-28")


@pytest.mark.parametrize(
    ("probabilities", "labels", "message"),
    [
        (((Decimal("0.5"), Decimal("0.5"), Decimal("0")),), (), "cùng số"),
        (((Decimal("0.5"), Decimal("0.5"), Decimal("0.1")),), (0,), "Tổng xác suất"),
        (((Decimal("1.1"), Decimal("-0.1"), Decimal("0")),), (-1,), "Xác suất"),
        (((Decimal("NaN"), Decimal("0"), Decimal("0")),), (0,), "probability"),
        (((Decimal("0.2"), Decimal("0.2"), Decimal("0.6")),), (2,), "Nhãn"),
    ],
)
def test_multiclass_scores_reject_invalid_probabilities_or_labels(
    probabilities: tuple[tuple[Decimal, Decimal, Decimal], ...],
    labels: tuple[int, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        score_multiclass_outcomes(probabilities, labels)


def test_policy_hash_pins_diagnostic_thresholds_and_score_clipping() -> None:
    policy = PaperPerformancePolicy()

    assert policy.version == "wp06-paper-performance-v1"
    assert policy.cohort_days == 30
    assert policy.maturity_days == 30
    assert policy.minimum_outcomes == 1_000
    assert policy.minimum_class_support == 100
    assert policy.minimum_coverage == Decimal("0.80")
    assert policy.maximum_brier_regression == Decimal("0.01")
    assert policy.maximum_log_loss_regression == Decimal("0.01")
    assert len(policy.content_hash) == 64
    assert policy.content_hash == PaperPerformancePolicy().content_hash


def test_two_mature_adequate_cohorts_without_joint_regression_are_healthy() -> None:
    assessment = assess_paper_performance(
        (_cohort(index=0), _cohort(index=1)), as_of=AS_OF, policy=PaperPerformancePolicy()
    )

    assert assessment.status is PaperPerformanceStatus.HEALTHY
    assert assessment.reasons == ()
    assert len(assessment.cohorts) == 2


def test_joint_regression_in_both_adjacent_cohorts_is_degraded() -> None:
    assessment = assess_paper_performance(
        (
            _cohort(index=0, brier_delta="0.011", log_loss_delta="0.012"),
            _cohort(index=1, brier_delta="0.02", log_loss_delta="0.015"),
        ),
        as_of=AS_OF,
        policy=PaperPerformancePolicy(),
    )

    assert assessment.status is PaperPerformanceStatus.DEGRADED
    assert all(item.degraded for item in assessment.cohorts)


@pytest.mark.parametrize(
    "cohorts",
    [
        (_cohort(index=0, eligible=1_000, covered=799, support=(267, 266, 266)), _cohort(index=1)),
        (_cohort(index=0, support=(802, 99, 99)), _cohort(index=1)),
        (_cohort(index=0), _cohort(index=1, brier_delta="0.02", log_loss_delta="0.02")),
        (
            _cohort(index=0, brier_delta="0.02", log_loss_delta="0.02"),
            _cohort(index=1, brier_delta="0.02", log_loss_delta="0.009"),
        ),
    ],
)
def test_inadequate_or_one_sided_evidence_never_degrades(
    cohorts: tuple[PaperPerformanceCohort, ...],
) -> None:
    assessment = assess_paper_performance(cohorts, as_of=AS_OF, policy=PaperPerformancePolicy())

    if len(cohorts) < 2 or any(
        item.covered_outcome_count < 1_000
        or item.coverage < Decimal("0.80")
        or min(count for _, count in item.class_support) < 100
        for item in cohorts
    ):
        assert assessment.status is PaperPerformanceStatus.INCONCLUSIVE
    else:
        assert assessment.status is PaperPerformanceStatus.HEALTHY


def test_assessment_requires_exact_adjacent_mature_utc_cohorts() -> None:
    first = _cohort(index=0)
    second = _cohort(index=1)
    overlapping = replace(
        first,
        window_start=first.window_start + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="Cohort"):
        assess_paper_performance(
            (overlapping, second), as_of=AS_OF, policy=PaperPerformancePolicy()
        )


def test_assessment_requires_two_cohorts() -> None:
    with pytest.raises(ValueError, match="đúng hai cohort"):
        assess_paper_performance((_cohort(index=0),), as_of=AS_OF, policy=PaperPerformancePolicy())


def test_no_matured_outcomes_has_no_score_and_is_inconclusive() -> None:
    empty_cohort = _cohort(index=0, eligible=0, covered=0, support=(0, 0, 0))
    second_empty_cohort = _cohort(index=1, eligible=0, covered=0, support=(0, 0, 0))
    first = empty_cohort
    second = second_empty_cohort

    assessment = assess_paper_performance(
        (first, second), as_of=AS_OF, policy=PaperPerformancePolicy()
    )

    assert assessment.status is PaperPerformanceStatus.INCONCLUSIVE
    assert first.brier_regression is None
    assert first.log_loss_regression is None
    assert all("minimum_outcomes_not_met" in cohort.reasons for cohort in assessment.cohorts)


def test_cohort_rejects_invalid_counts_support_and_timezone() -> None:
    first = _cohort(index=0)
    with pytest.raises(ValueError, match="window_start"):
        PaperPerformanceCohort(
            window_start=first.window_start.replace(tzinfo=None),
            window_end=first.window_end,
            eligible_decision_count=100,
            covered_outcome_count=101,
            class_support=tuple(zip(CLASSES, (40, 30, 31), strict=True)),
            observed_brier=Decimal("0.2"),
            observed_log_loss=Decimal("0.6"),
            baseline_brier=Decimal("0.2"),
            baseline_log_loss=Decimal("0.6"),
            membership_digest="a" * 64,
        )
