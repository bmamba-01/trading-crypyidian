from __future__ import annotations

import math

import pytest

from trading_bot.domain.learning.calibration import (
    TemperatureCalibrationError,
    TemperatureCalibrationFailureCode,
    TemperatureCalibrationPolicy,
    ValidationCalibrationExample,
    fit_validation_temperature,
)


def _policy(**overrides: object) -> TemperatureCalibrationPolicy:
    values: dict[str, object] = {
        "policy_version": "temperature-v1",
        "minimum_temperature": 0.05,
        "maximum_temperature": 20.0,
        "minimum_samples": 9,
        "minimum_class_support": 2,
        "relative_tolerance": 1e-10,
        "maximum_iterations": 256,
    }
    values.update(overrides)
    return TemperatureCalibrationPolicy(**values)  # type: ignore[arg-type]


def _one_hot_logits(
    predicted_index: int, selected_value: float = 2.0
) -> tuple[float, float, float]:
    return (
        selected_value if predicted_index == 0 else 0.0,
        selected_value if predicted_index == 1 else 0.0,
        selected_value if predicted_index == 2 else 0.0,
    )


def _examples(*, correct_count: int = 4) -> tuple[ValidationCalibrationExample, ...]:
    rows: list[ValidationCalibrationExample] = []
    for predicted_index, label in enumerate((-1, 0, 1)):
        logits = _one_hot_logits(predicted_index)
        labels = [label] * correct_count
        labels.extend(candidate for candidate in (-1, 0, 1) if candidate != label)
        while len(labels) < 6:
            labels.append(label)
        rows.extend(
            ValidationCalibrationExample(logits=logits, label=sample_label, fold_role="validation")
            for sample_label in labels
        )
    return tuple(rows)


def test_validation_example_requires_finite_three_class_logits_and_validation_role() -> None:
    with pytest.raises(TypeError):
        ValidationCalibrationExample(logits=(1.0, 0.0, -1.0), label=-1)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="ba logit"):
        ValidationCalibrationExample(
            logits=(1.0, 2.0),  # type: ignore[arg-type]
            label=-1,
            fold_role="validation",
        )
    with pytest.raises(ValueError, match="hữu hạn"):
        ValidationCalibrationExample(logits=(1.0, math.inf, 0.0), label=-1, fold_role="validation")
    with pytest.raises(ValueError, match="VALIDATION"):
        ValidationCalibrationExample(
            logits=(1.0, 0.0, -1.0),
            label=-1,
            fold_role="test",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="nhãn"):
        ValidationCalibrationExample(
            logits=(1.0, 0.0, -1.0),
            label=True,
            fold_role="validation",
        )


def test_calibration_policy_requires_versioned_positive_bounds_and_feasible_support() -> None:
    with pytest.raises(ValueError, match="policy_version"):
        _policy(policy_version="")
    with pytest.raises(ValueError, match="temperature"):
        _policy(minimum_temperature=0.0)
    with pytest.raises(ValueError, match="nhỏ hơn"):
        _policy(minimum_temperature=2.0, maximum_temperature=2.0)
    with pytest.raises(ValueError, match="support"):
        _policy(minimum_samples=5, minimum_class_support=2)


def test_fit_is_deterministic_and_reduces_validation_multiclass_nll() -> None:
    examples = _examples(correct_count=4)

    first = fit_validation_temperature(examples, _policy())
    second = fit_validation_temperature(examples, _policy())

    assert first == second
    assert first.policy_version == "temperature-v1"
    assert first.sample_count == 18
    assert first.class_support == ((-1, 6), (0, 6), (1, 6))
    assert first.converged is True
    assert first.temperature == pytest.approx(2.0 / math.log(4.0), rel=1e-9)
    assert 1.0 < first.temperature < 20.0
    assert first.validation_nll_after < first.validation_nll_before
    with pytest.raises((AttributeError, TypeError)):
        first.temperature = 3.0  # type: ignore[misc]


def test_fit_rejects_insufficient_per_class_validation_support() -> None:
    examples = tuple(
        ValidationCalibrationExample(
            logits=example.logits,
            label=label,
            fold_role="validation",
        )
        for example, label in zip(
            _examples(correct_count=4),
            (-1,) * 10 + (0,) * 4 + (1,) * 4,
            strict=True,
        )
    )
    policy = _policy(minimum_class_support=5, minimum_samples=15)

    with pytest.raises(TemperatureCalibrationError) as error:
        fit_validation_temperature(examples, policy)

    assert error.value.code is TemperatureCalibrationFailureCode.INSUFFICIENT_SUPPORT


def test_fit_fails_closed_when_optimum_is_at_temperature_bound() -> None:
    examples = tuple(
        ValidationCalibrationExample(
            logits=_one_hot_logits(predicted),
            label=label,
            fold_role="validation",
        )
        for predicted, label in enumerate((-1, 0, 1))
        for _ in range(3)
    )

    with pytest.raises(TemperatureCalibrationError) as error:
        fit_validation_temperature(examples, _policy(minimum_samples=9))

    assert error.value.code is TemperatureCalibrationFailureCode.BOUNDARY_OPTIMUM


def test_fit_rejects_non_identifiable_temperature_and_nonconvergence() -> None:
    uniform_logits = tuple(
        ValidationCalibrationExample(logits=(1.0, 1.0, 1.0), label=label, fold_role="validation")
        for label in (-1, 0, 1)
        for _ in range(3)
    )
    with pytest.raises(TemperatureCalibrationError) as error:
        fit_validation_temperature(uniform_logits, _policy())
    assert error.value.code is TemperatureCalibrationFailureCode.NON_IDENTIFIABLE

    with pytest.raises(TemperatureCalibrationError) as error:
        fit_validation_temperature(
            _examples(correct_count=4), _policy(relative_tolerance=1e-16, maximum_iterations=1)
        )
    assert error.value.code is TemperatureCalibrationFailureCode.NON_CONVERGENCE


def test_fit_fails_closed_when_finite_logits_overflow_inside_policy_bounds() -> None:
    examples = tuple(
        ValidationCalibrationExample(
            logits=_one_hot_logits(predicted, 1e308),
            label=label,
            fold_role="validation",
        )
        for predicted, label in enumerate((-1, 0, 1))
        for _ in range(3)
    )

    with pytest.raises(TemperatureCalibrationError) as error:
        fit_validation_temperature(
            examples,
            _policy(minimum_temperature=1e-308, maximum_temperature=1e308),
        )

    assert error.value.code is TemperatureCalibrationFailureCode.NUMERICAL_FAILURE


def test_fit_requires_an_immutable_tuple_of_typed_validation_examples() -> None:
    with pytest.raises(ValueError, match="tuple"):
        fit_validation_temperature(list(_examples()), _policy())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ValidationCalibrationExample"):
        fit_validation_temperature((object(),), _policy())  # type: ignore[arg-type]
