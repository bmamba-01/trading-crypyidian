"""SIG-3 lát 2: thang chốt lời nhiều mục tiêu — giá theo R, R:R ròng từng bậc, hash tương thích."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.domain.risk.planner import CostModel
from trading_bot.domain.risk.take_profit import (
    LADDER_VERSION,
    LadderStep,
    TakeProfitLadder,
    build_targets,
    net_reward_to_risk,
    weighted_net_reward_to_risk,
)

COST = CostModel(
    version_id="vip0",
    entry_fee_rate=Decimal("0.0005"),
    exit_fee_rate=Decimal("0.0005"),
    adverse_funding_per_unit=Decimal("0"),
    entry_slippage_rate=Decimal("0"),
)
LADDER = TakeProfitLadder(
    steps=(
        LadderStep(Decimal("1"), Decimal("0.5")),
        LadderStep(Decimal("2"), Decimal("0.3")),
        LadderStep(Decimal("3"), Decimal("0.2")),
    ),
    breakeven_after_first=True,
)


def test_ladder_validation() -> None:
    assert LADDER_VERSION == "sig3-ladder-v1"
    with pytest.raises(ValueError, match="Tổng tỷ lệ"):
        TakeProfitLadder(steps=(LadderStep(Decimal("1"), Decimal("0.6")),))
    with pytest.raises(ValueError, match="tăng dần"):
        TakeProfitLadder(
            steps=(
                LadderStep(Decimal("2"), Decimal("0.5")),
                LadderStep(Decimal("1"), Decimal("0.5")),
            )
        )
    with pytest.raises(ValueError, match="1–3 bậc"):
        TakeProfitLadder(steps=tuple(LadderStep(Decimal(i), Decimal("0.25")) for i in range(1, 5)))


def test_long_targets_by_r_with_net_rr_after_fees() -> None:
    targets = build_targets(
        side="LONG",
        entry=Decimal("100"),
        stop=Decimal("98"),
        ladder=LADDER,
        tick_size=Decimal("0.1"),
        multiplier=Decimal("1"),
        cost_model=COST,
    )
    assert [t.price for t in targets] == [Decimal("102"), Decimal("104"), Decimal("106")]
    assert [t.index for t in targets] == [1, 2, 3]
    # TP1 (1R): lãi 2 − phí vào 0,05 − phí ra 0,051 = 1,899; lỗ 2 + 0,05 + 0,049 = 2,099.
    assert targets[0].net_reward_to_risk == pytest.approx(Decimal("1.899") / Decimal("2.099"))
    assert targets[0].net_reward_to_risk < 1 < targets[1].net_reward_to_risk
    weighted = weighted_net_reward_to_risk(targets)
    expected = sum(t.fraction * t.net_reward_to_risk for t in targets)
    assert weighted == expected


def test_short_targets_round_conservatively_and_reject_collisions() -> None:
    targets = build_targets(
        side="SHORT",
        entry=Decimal("100.05"),
        stop=Decimal("100.10"),
        ladder=TakeProfitLadder(steps=(LadderStep(Decimal("1"), Decimal("1")),)),
        tick_size=Decimal("0.1"),
        multiplier=Decimal("1"),
        cost_model=COST,
    )
    assert targets[0].price == Decimal("100.0")  # 100,00 làm tròn xuống (xa entry hơn)
    with pytest.raises(ValueError, match="trùng nhau"):
        build_targets(
            side="LONG",
            entry=Decimal("100"),
            stop=Decimal("99.99"),
            ladder=TakeProfitLadder(
                steps=(
                    LadderStep(Decimal("1"), Decimal("0.5")),
                    LadderStep(Decimal("2"), Decimal("0.5")),
                )
            ),
            tick_size=Decimal("1"),
            multiplier=Decimal("1"),
            cost_model=COST,
        )


def test_shared_formula_matches_planner_single_target() -> None:
    ratio = net_reward_to_risk(
        entry=Decimal("100"),
        stop=Decimal("98"),
        target=Decimal("104"),
        multiplier=Decimal("1"),
        cost_model=COST,
    )
    assert ratio == (Decimal("4") - Decimal("0.05") - Decimal("0.052")) / (
        Decimal("2") + Decimal("0.05") + Decimal("0.049")
    )
