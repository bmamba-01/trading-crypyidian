from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.backtest import (
    EvaluationReport,
    ReplayManifest,
    ReplayObservation,
)
from trading_bot.application.backtest_sensitivity import (
    SensitivityScenario,
    run_sensitivity,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade
from trading_bot.domain.strategy.contracts import DecisionSnapshot

START = datetime(2026, 1, 1, tzinfo=UTC)


def _manifest(**changes: object) -> ReplayManifest:
    values: dict[str, object] = {
        "dataset_hash": "d" * 64,
        "range_start": START,
        "range_end": START + timedelta(days=1),
        "settings_version_id": "settings-v1",
        "risk_profile_version_id": "risk-v1",
        "strategy_release_id": "h01-v1",
        "strategy_content_hash": "b" * 64,
        "feature_schema_hash": "c" * 64,
        "fill_model_version": "paper-v1",
        "pipeline_content_hash": "e" * 64,
        "cost_assumptions": (("fee_rate", "0.001"), ("slippage_rate", "0.001")),
        "parameter_family_hash": "a" * 64,
        "trial_count": 1,
        "trial_index": 0,
        "seed": 7,
    }
    values.update(changes)
    return ReplayManifest(**values)  # type: ignore[arg-type]


def _observations() -> tuple[ReplayObservation, ...]:
    snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=START + timedelta(hours=1),
        snapshot_id="snapshot-1",
        series=(),
        features=(),
    )
    return (
        ReplayObservation(
            event_id="event-1",
            event_at=snapshot.decision_time,
            available_at=snapshot.decision_time,
            snapshot=snapshot,
        ),
    )


def _scenarios() -> tuple[SensitivityScenario, ...]:
    return (
        SensitivityScenario(
            scenario_id="parameter-nearby",
            kind="PARAMETER",
            parameter_overrides=(("atr_multiplier", "2.1"),),
        ),
        SensitivityScenario(
            scenario_id="parameter-nearby-wide",
            kind="PARAMETER",
            parameter_overrides=(("atr_multiplier", "2.2"),),
        ),
        SensitivityScenario(
            scenario_id="fee-slippage-stress",
            kind="COST",
            cost_overrides=(("fee_rate", "0.002"), ("slippage_rate", "0.002")),
        ),
        SensitivityScenario(scenario_id="base", kind="BASE"),
    )


def test_sensitivity_runs_base_cost_and_parameter_scenarios_deterministically() -> None:
    observations = _observations()
    seen: list[str] = []

    def evaluator_factory(scenario: SensitivityScenario, _manifest: ReplayManifest):
        def evaluate(snapshot: DecisionSnapshot) -> EvaluationTrade:
            seen.append(scenario.scenario_id)
            fees = Decimal("2") if scenario.kind == "COST" else Decimal("1")
            return EvaluationTrade(
                trade_id=f"trade:{snapshot.snapshot_id}:{scenario.scenario_id}",
                opened_at=snapshot.decision_time,
                closed_at=snapshot.decision_time + timedelta(minutes=1),
                gross_pnl=Decimal("10"),
                fees=fees,
                funding=Decimal("0"),
                slippage=fees,
                exposure=Decimal("100"),
            )

        return evaluate

    first = run_sensitivity(
        observations,
        manifest=_manifest(),
        scenarios=_scenarios(),
        evaluator_factory=evaluator_factory,
    )
    first_order = tuple(seen)
    seen.clear()
    second = run_sensitivity(
        observations,
        manifest=_manifest(),
        scenarios=tuple(reversed(_scenarios())),
        evaluator_factory=evaluator_factory,
    )

    assert first_order == (
        "base",
        "fee-slippage-stress",
        "parameter-nearby",
        "parameter-nearby-wide",
    )
    assert seen == list(first_order)
    assert tuple(run.scenario.scenario_id for run in first.runs) == first_order
    assert tuple(run.manifest_hash for run in first.runs) == tuple(
        run.manifest_hash for run in second.runs
    )
    assert tuple(run.trial_id for run in first.runs) == tuple(run.trial_id for run in second.runs)
    assert len({run.manifest_hash for run in first.runs}) == 4
    assert len({run.trial_id for run in first.runs}) == 4
    assert isinstance(first.runs[0].result, EvaluationReport)
    assert first.runs[1].manifest.cost_assumptions == (
        ("fee_rate", "0.002"),
        ("slippage_rate", "0.002"),
    )
    assert first.runs[2].manifest.strategy_content_hash != _manifest().strategy_content_hash
    assert not hasattr(first, "ranking")
    assert "không phải xếp hạng" in first.caveat_vi


@pytest.mark.parametrize(
    ("scenarios", "message"),
    [
        (
            (
                SensitivityScenario(scenario_id="base", kind="BASE"),
                SensitivityScenario(scenario_id="base", kind="BASE"),
            ),
            "scenario_id",
        ),
        (
            (
                SensitivityScenario(scenario_id="base", kind="BASE"),
                SensitivityScenario(
                    scenario_id="same-a", kind="PARAMETER", parameter_overrides=(("x", "1"),)
                ),
                SensitivityScenario(
                    scenario_id="same-b", kind="PARAMETER", parameter_overrides=(("x", "1.0"),)
                ),
            ),
            "trùng",
        ),
        (
            (
                SensitivityScenario(scenario_id="base", kind="BASE"),
                SensitivityScenario(
                    scenario_id="unknown-cost", kind="COST", cost_overrides=(("other", "0.2"),)
                ),
            ),
            "không tồn tại",
        ),
        (
            (
                SensitivityScenario(scenario_id="base", kind="BASE"),
                SensitivityScenario(
                    scenario_id="negative-cost", kind="COST", cost_overrides=(("fee_rate", "-0.1"),)
                ),
            ),
            "không âm",
        ),
        (
            (
                SensitivityScenario(scenario_id="base", kind="BASE"),
                SensitivityScenario(
                    scenario_id="bad-parameter",
                    kind="PARAMETER",
                    parameter_overrides=(("atr", "NaN"),),
                ),
            ),
            "hữu hạn",
        ),
    ],
)
def test_sensitivity_fails_closed_for_duplicate_or_invalid_scenarios(
    scenarios: tuple[SensitivityScenario, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        run_sensitivity(
            _observations(),
            manifest=_manifest(),
            scenarios=scenarios,
            evaluator_factory=lambda *_: lambda _snapshot: None,
        )
