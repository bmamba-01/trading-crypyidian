from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.risk.engine import RiskDecision, RiskEngine, RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    OpenExposure,
    PlannerSettings,
    RiskLimits,
    TradePlan,
    TradePlanner,
    calculate_plan_hash,
)
from trading_bot.domain.strategy.contracts import StrategyCandidate
from trading_bot.domain.strategy.recipes import ExitRecipe

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _settings() -> PlannerSettings:
    return PlannerSettings(
        workspace_id="workspace-a",
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        published=True,
        active_release_ids=("h01-v1",),
        leverage=Decimal("2"),
        exchange=ExchangeRules(
            Decimal("0.1"),
            Decimal("0.01"),
            Decimal("1"),
            Decimal("5"),
            Decimal("10000"),
            Decimal("100"),
        ),
        limits=RiskLimits(
            per_trade_risk_fraction=Decimal("0.01"),
            max_aggregate_open_risk=Decimal("250"),
            max_notional=Decimal("10000"),
            max_leverage=Decimal("5"),
            max_positions=2,
            max_symbol_notional=Decimal("2000"),
            max_concentration_fraction=Decimal("0.2"),
            max_daily_loss=Decimal("200"),
            max_rolling_loss=Decimal("500"),
            max_account_age=timedelta(seconds=30),
            max_data_age=timedelta(minutes=5),
        ),
        release_content_hashes=(("h01-v1", "a" * 64),),
    )


def _plan() -> TradePlan:
    candidate = StrategyCandidate(
        strategy_id="H01",
        strategy_release_id="h01-v1",
        release_content_hash="a" * 64,
        settings_version_id="settings-v1",
        snapshot_id="snapshot-v1",
        symbol="BTCUSDT",
        direction="long",
        reference_price=Decimal("100"),
        atr_value=2.0,
        signal_at=NOW - timedelta(minutes=1),
        eligible_after=NOW - timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=4),
        recipe=ExitRecipe("exit-v1", 2),
        evidence_feature_ids=("feature-1",),
        evidence_bar_keys=("BTCUSDT:1h:bar-1",),
        macro_evidence_ids=(),
        geometry_version=None,
        reason_code="H01_LONG_ENTRY",
        detail_vi="Tín hiệu.",
    )
    account = AccountSnapshot(
        "workspace-a",
        "paper-a",
        "settings-v1",
        NOW - timedelta(seconds=1),
        Decimal("10000"),
        Decimal("1000"),
        True,
    )
    costs = CostModel("cost-v1", Decimal("0.001"), Decimal("0.001"), Decimal("0.05"), Decimal("0"))
    plan = TradePlanner().plan(candidate, _settings(), account, (), costs, now=NOW)
    assert isinstance(plan, TradePlan)
    return plan


def _state(**changes: object) -> RiskState:
    values: dict[str, object] = {
        "workspace_id": "workspace-a",
        "account_id": "paper-a",
        "settings_version_id": "settings-v1",
        "risk_profile_version_id": "risk-v1",
        "evaluated_at": NOW,
        "account_captured_at": NOW - timedelta(seconds=1),
        "market_data_as_of": NOW - timedelta(minutes=1),
        "equity": Decimal("10000"),
        "available_margin": Decimal("1000"),
        "open_exposures": (),
        "realized_loss_today": Decimal("0"),
        "realized_loss_rolling": Decimal("0"),
        "open_position_count": 0,
        "is_known": True,
        "pending_reservation_risk": Decimal("0"),
        "pending_reservation_count": 0,
        "pending_reservation_initial_margin": Decimal("0"),
    }
    values.update(changes)
    return RiskState(**values)  # type: ignore[arg-type]


def test_risk_engine_approves_with_plan_and_policy_lineage() -> None:
    settings = _settings()
    result = RiskEngine().evaluate(_plan(), _state(), settings)

    assert isinstance(result, RiskDecision)
    assert result.approved
    assert result.plan_hash == _plan().plan_hash
    assert result.settings_version_id == settings.settings_version_id
    assert result.risk_profile_version_id == settings.risk_profile_version_id
    assert result.symbol == "BTCUSDT"
    assert result.candidate_expires_at == _plan().expires_at
    assert result.requested_notional == _plan().notional
    assert result.max_notional == settings.limits.max_notional
    assert result.max_symbol_notional == min(
        settings.limits.max_symbol_notional,
        Decimal("10000") * settings.limits.max_concentration_fraction,
    )


@pytest.mark.parametrize(
    ("state_changes", "settings_changes", "expected"),
    [
        ({"is_known": False}, {}, "RISK_STATE_UNKNOWN"),
        ({"account_captured_at": NOW - timedelta(minutes=1)}, {}, "ACCOUNT_STATE_STALE"),
        ({"market_data_as_of": NOW - timedelta(minutes=10)}, {}, "MARKET_DATA_STALE"),
        ({"settings_version_id": "settings-old"}, {}, "STALE_SETTINGS_VERSION"),
        ({"risk_profile_version_id": "risk-old"}, {}, "STALE_RISK_PROFILE"),
        ({"realized_loss_today": Decimal("200")}, {}, "DAILY_LOSS_CAP_REACHED"),
        ({"realized_loss_rolling": Decimal("500")}, {}, "ROLLING_LOSS_CAP_REACHED"),
        ({"open_position_count": 2}, {}, "MAX_POSITIONS_REACHED"),
        ({}, {"published": False}, "SETTINGS_UNPUBLISHED"),
    ],
)
def test_risk_engine_rejects_stale_unknown_and_cap_breaches(
    state_changes: dict[str, object], settings_changes: dict[str, object], expected: str
) -> None:
    settings = replace(_settings(), **settings_changes)
    result = RiskEngine().evaluate(_plan(), _state(**state_changes), settings)

    assert isinstance(result, RiskDecision)
    assert not result.approved
    assert result.reason_code == expected
    assert result.detail_vi
    assert result.plan_hash == _plan().plan_hash


def test_risk_engine_rejects_expired_trade_plan_at_evaluation_time() -> None:
    from dataclasses import replace

    plan = replace(_plan(), expires_at=NOW, plan_hash="")
    plan = replace(plan, plan_hash=calculate_plan_hash(plan))

    result = RiskEngine().evaluate(plan, _state(), _settings())

    assert not result.approved
    assert result.reason_code == "CANDIDATE_EXPIRED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_price", Decimal("0")),
        ("stop_price", Decimal("0")),
        ("stop_price", Decimal("96.05")),
        ("target_price", Decimal("106.05")),
    ],
)
def test_risk_engine_rejects_nonpositive_or_off_tick_plan_prices(
    field: str, value: Decimal
) -> None:
    from dataclasses import replace

    original = _plan()
    changed = replace(original, **{field: value}, plan_hash="")
    changed = replace(changed, plan_hash=calculate_plan_hash(changed))

    result = RiskEngine().evaluate(changed, _state(), _settings())

    assert not result.approved
    assert result.reason_code == "PRICE_PRECISION_OR_DIRECTION_INVALID"


def test_risk_engine_enforces_open_plus_pending_risk_and_symbol_caps() -> None:
    settings = _settings()
    state = _state(pending_reservation_risk=Decimal("240"))
    result = RiskEngine().evaluate(_plan(), state, settings)
    assert not result.approved
    assert result.reason_code == "AGGREGATE_RISK_CAP_EXCEEDED"

    state = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "BTCUSDT", Decimal("1900"), Decimal("1")),
        )
    )
    result = RiskEngine().evaluate(_plan(), state, settings)
    assert not result.approved
    assert result.reason_code == "SYMBOL_EXPOSURE_CAP_EXCEEDED"


def test_risk_engine_includes_pending_notional_in_account_and_symbol_caps() -> None:
    settings = _settings()
    state = _state(
        pending_reservation_notional=Decimal("9500"),
        pending_symbol_reservation_notional=Decimal("9500"),
    )

    result = RiskEngine().evaluate(_plan(), state, settings)

    assert not result.approved
    assert result.reason_code == "NOTIONAL_CAP_EXCEEDED"


def test_risk_engine_counts_pending_initial_margin_against_available_margin() -> None:
    plan = _plan()
    state = _state(
        available_margin=plan.initial_margin + Decimal("1"),
        pending_reservation_initial_margin=Decimal("2"),
    )

    result = RiskEngine().evaluate(plan, state, _settings())

    assert not result.approved
    assert result.reason_code == "MARGIN_UNAVAILABLE"
    assert result.available_margin == plan.initial_margin + Decimal("1")
    assert result.pending_reservation_initial_margin == Decimal("2")


def test_risk_engine_rejects_plan_bound_to_different_cost_or_recipe_hash() -> None:
    original = _plan()
    changed = replace(original, entry_fee_cost=original.entry_fee_cost + Decimal("1"))
    result = RiskEngine().evaluate(changed, _state(), _settings())

    assert not result.approved
    assert result.reason_code == "PLAN_HASH_INVALID"
