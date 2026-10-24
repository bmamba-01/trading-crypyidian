from uuid import UUID

import pytest
from tests.fixtures.runtime_replay import build_h03_runtime_sensitivity

from trading_bot.application.backtest import EvaluationReport
from trading_bot.application.backtest_sensitivity import SensitivityScenario


def test_h03_sensitivity_fixture_uses_runtime_for_base_cost_and_parameter_runs() -> None:
    comparison = build_h03_runtime_sensitivity(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        (
            SensitivityScenario("base", "BASE"),
            SensitivityScenario(
                "fee-stress",
                "COST",
                cost_overrides=(
                    ("entry_fee_rate", "0.002"),
                    ("exit_fee_rate", "0.002"),
                ),
            ),
            SensitivityScenario(
                "shorter-channel",
                "PARAMETER",
                parameter_overrides=(("donchian_lookback", "20"),),
            ),
        ),
    )

    assert comparison.is_complete
    assert [run.sensitivity_run.scenario.kind for run in comparison.runs] == [
        "BASE",
        "COST",
        "PARAMETER",
    ]
    assert all(isinstance(run.sensitivity_run.result, EvaluationReport) for run in comparison.runs)
    assert all(run.sensitivity_run.result.trade_count == 1 for run in comparison.runs)
    assert all(len(run.trade_outcomes) == 1 for run in comparison.runs)
    base, cost, parameter = comparison.runs
    assert cost.trade_outcomes[0].fees > base.trade_outcomes[0].fees
    assert cost.sensitivity_run.result.net_pnl < base.sensitivity_run.result.net_pnl
    assert parameter.sensitivity_run.manifest.strategy_content_hash != (
        base.sensitivity_run.manifest.strategy_content_hash
    )


def test_h03_slippage_sensitivity_changes_planner_and_entry_exit_fill_costs() -> None:
    comparison = build_h03_runtime_sensitivity(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        (
            SensitivityScenario("base", "BASE"),
            SensitivityScenario(
                "slippage-stress",
                "COST",
                cost_overrides=(
                    ("entry_slippage_rate", "0.002"),
                    ("exit_slippage_rate", "0.002"),
                ),
            ),
        ),
    )

    assert comparison.is_complete
    base, stressed = comparison.runs
    base_report = base.sensitivity_run.result
    stress_report = stressed.sensitivity_run.result
    assert isinstance(base_report, EvaluationReport)
    assert isinstance(stress_report, EvaluationReport)
    assert stressed.sensitivity_run.manifest.cost_assumptions != (
        base.sensitivity_run.manifest.cost_assumptions
    )
    assert stressed.sensitivity_run.manifest_hash != base.sensitivity_run.manifest_hash
    assert stressed.sensitivity_run.trial_id != base.sensitivity_run.trial_id
    assert stressed.trade_outcomes[0].slippage > base.trade_outcomes[0].slippage
    assert stress_report.slippage == stressed.trade_outcomes[0].slippage
    assert stress_report.net_pnl < base_report.net_pnl


def test_h03_sensitivity_fixture_hashes_are_stable_across_replays() -> None:
    workspace_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    scenarios = (
        SensitivityScenario("base", "BASE"),
        SensitivityScenario(
            "fee-stress",
            "COST",
            cost_overrides=(
                ("entry_fee_rate", "0.002"),
                ("exit_fee_rate", "0.002"),
            ),
        ),
        SensitivityScenario(
            "shorter-channel",
            "PARAMETER",
            parameter_overrides=(("donchian_lookback", "20"),),
        ),
        SensitivityScenario(
            "slippage-stress",
            "COST",
            cost_overrides=(
                ("entry_slippage_rate", "0.002"),
                ("exit_slippage_rate", "0.002"),
            ),
        ),
    )

    first = build_h03_runtime_sensitivity(workspace_id, scenarios)
    replayed = build_h03_runtime_sensitivity(workspace_id, tuple(reversed(scenarios)))

    def run_evidence(comparison):
        return {
            run.sensitivity_run.scenario.scenario_id: (
                run.sensitivity_run.manifest_hash,
                run.sensitivity_run.result.report_hash,
                run.trade_outcomes,
            )
            for run in comparison.runs
        }

    assert first.is_complete
    assert replayed.is_complete
    assert run_evidence(first) == run_evidence(replayed)


@pytest.mark.parametrize(
    "overrides",
    [
        (("entry_slippage_rate", "0.002"),),
        (("entry_slippage_rate", "0.002"), ("exit_slippage_rate", "0.003")),
        (("spread_rate", "0.001"),),
        (("adverse_funding_per_unit", "0.01"),),
        (("unknown_cost", "0.001"),),
        (("entry_slippage_rate", "1"), ("exit_slippage_rate", "1")),
    ],
)
def test_h03_runtime_rejects_unaligned_or_unmodeled_cost_stress(overrides) -> None:
    with pytest.raises((ValueError, RuntimeError)):
        build_h03_runtime_sensitivity(
            UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            (
                SensitivityScenario("base", "BASE"),
                SensitivityScenario("invalid-stress", "COST", cost_overrides=overrides),
            ),
        )
