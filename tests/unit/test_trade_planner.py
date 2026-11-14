from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    OpenExposure,
    PlannerSettings,
    PlanRejected,
    RiskLimits,
    TradePlan,
    TradePlanner,
)
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _settings(**limit_changes: object) -> PlannerSettings:
    limits = RiskLimits(
        per_trade_risk_fraction=Decimal("0.01"),
        max_aggregate_open_risk=Decimal("250"),
        max_notional=Decimal("10000"),
        max_leverage=Decimal("5"),
        max_positions=3,
        max_symbol_notional=Decimal("5000"),
        max_concentration_fraction=Decimal("0.50"),
        max_daily_loss=Decimal("200"),
        max_rolling_loss=Decimal("500"),
        max_account_age=timedelta(seconds=30),
        max_data_age=timedelta(minutes=5),
    )
    if "limits" in limit_changes:
        limits = limit_changes.pop("limits")  # type: ignore[assignment]
    settings = PlannerSettings(
        workspace_id="workspace-a",
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        published=True,
        active_release_ids=("h01-v1",),
        leverage=Decimal("2"),
        exchange=ExchangeRules(
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.01"),
            contract_multiplier=Decimal("1"),
            min_notional=Decimal("5"),
            max_notional=Decimal("10000"),
            max_quantity=Decimal("100"),
        ),
        limits=limits,
        release_content_hashes=(("h01-v1", "a" * 64),),
    )
    if limit_changes:
        return replace(settings, **limit_changes)
    return replace(settings, limits=limits)


def _candidate(**changes: object) -> StrategyCandidate:
    values: dict[str, object] = {
        "strategy_id": "H01",
        "strategy_release_id": "h01-v1",
        "release_content_hash": "a" * 64,
        "settings_version_id": "settings-v1",
        "snapshot_id": "snapshot-v1",
        "symbol": "BTCUSDT",
        "direction": "long",
        "reference_price": Decimal("100"),
        "atr_value": 2.0,
        "signal_at": NOW - timedelta(minutes=1),
        "eligible_after": NOW - timedelta(seconds=30),
        "expires_at": NOW + timedelta(minutes=4),
        "recipe": ExitRecipe("exit-v1", 2, take_profit_atr_multiple=3),
        "evidence_feature_ids": ("feature-1", "feature-2"),
        "evidence_bar_keys": ("BTCUSDT:1h:bar-1",),
        "macro_evidence_ids": (),
        "geometry_version": None,
        "reason_code": "H01_LONG_ENTRY",
        "detail_vi": "Tín hiệu giả định đã xác nhận.",
    }
    values.update(changes)
    return StrategyCandidate(**values)  # type: ignore[arg-type]


def _account(**changes: object) -> AccountSnapshot:
    values: dict[str, object] = {
        "workspace_id": "workspace-a",
        "account_id": "paper-a",
        "settings_version_id": "settings-v1",
        "captured_at": NOW - timedelta(seconds=1),
        "equity": Decimal("10000"),
        "available_margin": Decimal("1000"),
        "is_known": True,
    }
    values.update(changes)
    return AccountSnapshot(**values)  # type: ignore[arg-type]


def _costs(**changes: object) -> CostModel:
    values: dict[str, object] = {
        "version_id": "cost-v1",
        "entry_fee_rate": Decimal("0.001"),
        "exit_fee_rate": Decimal("0.001"),
        "adverse_funding_per_unit": Decimal("0.05"),
        "entry_slippage_rate": Decimal("0.002"),
        "exit_slippage_rate": Decimal("0"),
    }
    values.update(changes)
    return CostModel(**values)  # type: ignore[arg-type]


def test_planner_builds_decimal_plan_with_adverse_costs_once_and_exchange_steps() -> None:
    result = TradePlanner().plan(_candidate(), _settings(), _account(), (), _costs(), now=NOW)

    assert isinstance(result, TradePlan)
    assert result.entry_reference_price == Decimal("100")
    assert result.entry_price == Decimal("100.2")
    assert result.stop_price == Decimal("96.0")
    assert result.target_price == Decimal("106.0")
    assert result.quantity % Decimal("0.01") == 0
    assert result.estimated_loss <= result.risk_budget
    assert result.entry_slippage_cost == (
        (result.entry_price - result.entry_reference_price)
        * result.quantity
        * result.exchange_rules.contract_multiplier
    )
    expected_loss = result.quantity * (
        (result.entry_price - result.stop_price) * result.exchange_rules.contract_multiplier
        + result.entry_price * result.exchange_rules.contract_multiplier * _costs().entry_fee_rate
        + result.stop_price * result.exchange_rules.contract_multiplier * _costs().exit_fee_rate
        + _costs().adverse_funding_per_unit
    )
    assert result.estimated_loss == expected_loss
    assert (
        result.plan_hash
        == TradePlanner()
        .plan(_candidate(), _settings(), _account(), (), _costs(), now=NOW)
        .plan_hash
    )


def test_planner_reserves_exit_slippage_without_double_counting_entry() -> None:
    # Recipe trailing không có target nên cổng R:R ròng không can thiệp vào phép kiểm chi phí.
    candidate = _candidate(recipe=ExitRecipe("exit-trailing-v1", 2, trailing_atr_multiple=3))
    baseline = TradePlanner().plan(candidate, _settings(), _account(), (), _costs(), now=NOW)
    stressed_costs = _costs(
        entry_slippage_rate=Decimal("0.01"),
        exit_slippage_rate=Decimal("0.01"),
    )
    stressed = TradePlanner().plan(candidate, _settings(), _account(), (), stressed_costs, now=NOW)

    assert isinstance(baseline, TradePlan)
    assert isinstance(stressed, TradePlan)
    multiplier = stressed.exchange_rules.contract_multiplier
    expected_exit_slippage_per_unit = stressed.stop_price * Decimal("0.01") * multiplier
    expected_loss = stressed.quantity * (
        (stressed.entry_price - stressed.stop_price) * multiplier
        + stressed.entry_price * multiplier * stressed_costs.entry_fee_rate
        + stressed.stop_price * multiplier * stressed_costs.exit_fee_rate
        + stressed_costs.adverse_funding_per_unit
        + expected_exit_slippage_per_unit
    )
    assert stressed.entry_price == Decimal("101.0")
    assert stressed.estimated_loss == expected_loss
    assert stressed.estimated_loss <= stressed.risk_budget
    assert stressed.quantity <= baseline.quantity


def test_planner_rejects_target_plan_whose_net_reward_is_below_net_risk() -> None:
    # Trượt giá 1% đẩy entry lên 101 trong khi stop/target neo theo giá tham chiếu 100:
    # thưởng gộp 5, rủi ro gộp 5, sau phí và trượt giá thoát R:R ròng chỉ còn khoảng 0,59.
    costs = _costs(entry_slippage_rate=Decimal("0.01"), exit_slippage_rate=Decimal("0.01"))

    result = TradePlanner().plan(_candidate(), _settings(), _account(), (), costs, now=NOW)

    assert isinstance(result, PlanRejected)
    assert result.reason_code == "NET_REWARD_RISK_BELOW_MINIMUM"
    assert "0.59" in result.detail_vi


def test_planner_net_reward_risk_gate_uses_costs_on_both_sides() -> None:
    # Stop 2 ATR, target 2 ATR: R:R gộp đúng 1,0 nhưng chi phí làm R:R ròng < 1 → từ chối.
    symmetric = _candidate(recipe=ExitRecipe("exit-symmetric-v1", 2, take_profit_atr_multiple=2))
    free = _costs(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=Decimal("0"),
    )

    with_costs = TradePlanner().plan(symmetric, _settings(), _account(), (), _costs(), now=NOW)
    without_costs = TradePlanner().plan(symmetric, _settings(), _account(), (), free, now=NOW)

    assert isinstance(with_costs, PlanRejected)
    assert with_costs.reason_code == "NET_REWARD_RISK_BELOW_MINIMUM"
    # Không chi phí thì R:R ròng đúng bằng ngưỡng 1,0 và được chấp nhận (so sánh >=).
    assert isinstance(without_costs, TradePlan)


def test_planner_rejects_nonpositive_rounded_stop() -> None:
    candidate = _candidate(reference_price=Decimal("1"), atr_value=1.0)

    result = TradePlanner().plan(candidate, _settings(), _account(), (), _costs(), now=NOW)

    assert isinstance(result, PlanRejected)
    assert result.reason_code == "PRICE_PRECISION_OR_DIRECTION_INVALID"


@pytest.mark.parametrize(
    ("candidate_changes", "account_changes", "settings_changes", "expected"),
    [
        ({"expires_at": NOW}, {}, {}, "CANDIDATE_EXPIRED"),
        ({"eligible_after": NOW + timedelta(seconds=1)}, {}, {}, "ENTRY_NOT_ELIGIBLE"),
        ({}, {"is_known": False}, {}, "ACCOUNT_STATE_UNKNOWN"),
        ({}, {"captured_at": NOW - timedelta(minutes=1)}, {}, "ACCOUNT_STATE_STALE"),
        ({}, {"settings_version_id": "settings-old"}, {}, "STALE_SETTINGS_VERSION"),
        ({"strategy_release_id": "h03-v1"}, {}, {}, "STRATEGY_RELEASE_NOT_SELECTED"),
    ],
)
def test_planner_fails_closed_for_expired_stale_or_unselected_inputs(
    candidate_changes: dict[str, object],
    account_changes: dict[str, object],
    settings_changes: dict[str, object],
    expected: str,
) -> None:
    result = TradePlanner().plan(
        _candidate(**candidate_changes),
        _settings(**settings_changes),
        _account(**account_changes),
        (),
        _costs(),
        now=NOW,
    )

    assert isinstance(result, PlanRejected)
    assert result.reason_code == expected
    assert result.detail_vi
    assert result.settings_version_id == "settings-v1"


def test_planner_rejects_short_stop_on_wrong_side_and_exchange_minimum() -> None:
    short_plan = TradePlanner().plan(
        _candidate(direction="short"), _settings(), _account(), (), _costs(), now=NOW
    )
    assert isinstance(short_plan, TradePlan)
    assert short_plan.stop_price > short_plan.entry_price
    assert short_plan.target_price is not None
    assert short_plan.target_price < short_plan.entry_price

    tiny_limits = replace(_settings().limits, per_trade_risk_fraction=Decimal("0.000001"))
    tiny_equity = TradePlanner().plan(
        _candidate(),
        _settings(limits=tiny_limits),
        _account(),
        (),
        _costs(),
        now=NOW,
    )
    assert isinstance(tiny_equity, PlanRejected)
    assert tiny_equity.reason_code == "MIN_NOTIONAL_NOT_MET"


def test_planner_reserves_remaining_open_risk_budget_and_rejects_stale_market_data() -> None:
    exposure = OpenExposure("workspace-a", "paper-a", "ETHUSDT", Decimal("1000"), Decimal("250"))
    result = TradePlanner().plan(
        _candidate(), _settings(), _account(), (exposure,), _costs(), now=NOW
    )
    assert isinstance(result, PlanRejected)
    assert result.reason_code == "AGGREGATE_RISK_CAP_EXCEEDED"

    stale = TradePlanner().plan(
        _candidate(signal_at=NOW - timedelta(minutes=10)),
        _settings(),
        _account(),
        (),
        _costs(),
        now=NOW,
    )
    assert isinstance(stale, PlanRejected)
    assert stale.reason_code == "MARKET_DATA_STALE"


def test_funding_reserve_scales_with_notional_rate_and_expected_periods() -> None:
    # Recipe trailing để chỉ đo phần dự trù funding, không dính cổng R:R.
    candidate = _candidate(recipe=ExitRecipe("exit-trailing-v1", 2, trailing_atr_multiple=3))
    flat = _costs(adverse_funding_per_unit=Decimal("0"))
    proportional = _costs(
        adverse_funding_per_unit=Decimal("0"),
        adverse_funding_rate=Decimal("0.0001"),
        expected_funding_periods=3,
    )

    baseline = TradePlanner().plan(candidate, _settings(), _account(), (), flat, now=NOW)
    reserved = TradePlanner().plan(candidate, _settings(), _account(), (), proportional, now=NOW)

    assert isinstance(baseline, TradePlan)
    assert isinstance(reserved, TradePlan)
    multiplier = reserved.exchange_rules.contract_multiplier
    # Binance: funding = giá trị danh nghĩa × rate, tính cho từng kỳ trong thời gian giữ.
    per_unit = reserved.entry_price * multiplier * Decimal("0.0001") * 3
    assert reserved.adverse_funding_cost == reserved.quantity * per_unit
    assert baseline.adverse_funding_cost == Decimal("0")
    assert reserved.quantity <= baseline.quantity
    assert proportional.funding_per_unit(Decimal("0.1"), Decimal("1")) == Decimal("0.00003")


def test_cost_model_rejects_invalid_funding_reserve() -> None:
    with pytest.raises(ValueError, match="Số kỳ funding"):
        _costs(expected_funding_periods=-1)
    with pytest.raises(ValueError, match="không âm"):
        _costs(adverse_funding_rate=Decimal("-0.0001"))
