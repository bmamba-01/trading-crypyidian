"""Unit test SIG-3: checklist có cấu trúc (parity với rule) và thẻ tín hiệu."""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.unit.test_strategy_hypotheses import (
    _h01_snapshot,
    _h03_snapshot,
    _h06_snapshot,
    _settings,
)

from trading_bot.domain.strategy.checklists import strategy_checklist
from trading_bot.domain.strategy.contracts import NoTrade, StrategyCandidate
from trading_bot.domain.strategy.rules import (
    H01EmaTrendRule,
    H03DonchianBreakoutRule,
    H06RangeMeanReversionRule,
)
from trading_bot.domain.strategy.signal_card import (
    SignalCardError,
    build_signal_card,
    validate_price_ladder,
)

H01_CASES = [
    {},
    {"ema20": (99.0, 101.0), "ema80": (100.0, 100.5), "h4_close": 90.0, "h4_ema200": 100.0},
    {"ema20": (101.0, 100.1), "ema80": (100.5, 100.0), "adx": 25.0},
    {"adx": 19.99},
    {"h4_close": 99.0},
]
H03_CASES = [{}, {"quote_volume": 5.0}, {"current_close": 99.0}]
H06_CASES = [
    {},
    {"adx": 18.0, "efficiency": 0.1, "z_score": -2.5, "rsi_value": 20.0},
    {"adx": 17.0, "efficiency": 0.25, "z_score": -2.5, "rsi_value": 20.0},
    {"adx": 17.0, "efficiency": 0.2, "z_score": -2.0, "rsi_value": 20.0},
    {"current_close": 80.0, "previous_floor": 85.0},
    {"macro_blackout": True},
]


def _assert_parity(result: object, checklist: tuple) -> None:
    assert checklist, "checklist không được rỗng cho H01/H03/H06"
    all_passed = all(item.passed is True for item in checklist)
    if isinstance(result, StrategyCandidate):
        assert all_passed, [item.as_dict() for item in checklist]
    else:
        assert isinstance(result, NoTrade)
        assert not all_passed, (result.code, [item.as_dict() for item in checklist])


@pytest.mark.parametrize("overrides", H01_CASES)
def test_h01_checklist_matches_rule(overrides: dict) -> None:
    snapshot, settings = _h01_snapshot(**overrides), _settings("h01-v1")
    result = H01EmaTrendRule().evaluate(snapshot, settings)
    _assert_parity(result, strategy_checklist("h01-v1", snapshot, settings))


@pytest.mark.parametrize("overrides", H03_CASES)
def test_h03_checklist_matches_rule(overrides: dict) -> None:
    snapshot, settings = _h03_snapshot(**overrides), _settings("h03-v1")
    result = H03DonchianBreakoutRule().evaluate(snapshot, settings)
    _assert_parity(result, strategy_checklist("h03-v1", snapshot, settings))


@pytest.mark.parametrize("overrides", H06_CASES)
def test_h06_checklist_matches_rule(overrides: dict) -> None:
    snapshot, settings = _h06_snapshot(**overrides), _settings("h06-v1")
    result = H06RangeMeanReversionRule().evaluate(snapshot, settings)
    _assert_parity(result, strategy_checklist("h06-v1", snapshot, settings))


def test_checklist_reports_actual_threshold_and_unknown_release_is_empty() -> None:
    items = strategy_checklist("h01-v1", _h01_snapshot(adx=19.99), _settings("h01-v1"))
    adx = next(item for item in items if item.key == "adx")
    assert (adx.actual, adx.operator, adx.threshold, adx.passed) == ("19.99", ">=", "20", False)
    assert (
        strategy_checklist("sc-binance-usdm-vol-breakout-001", _h01_snapshot(), _settings("x"))
        == ()
    )


def _candidate(direction: str = "long", **recipe: object) -> dict:
    return {
        "symbol": "BTCUSDT",
        "direction": direction,
        "strategy_id": "H06",
        "strategy_release_id": "h06-v1",
        "reason_code": f"H06_{direction.upper()}_ENTRY",
        "signal_at": "2026-11-15T10:00:00+00:00",
        "expires_at": "2026-11-15T10:15:00+00:00",
        "reference_price": "100",
        "atr_value": 2.0,
        "recipe": {"stop_atr_multiple": 1.5, "take_profit_atr_multiple": 2.0, **recipe},
    }


def test_long_card_from_recipe_has_ordered_ladder_and_recipe_target() -> None:
    card = build_signal_card(_candidate(max_hold_bars=16))
    assert card["stop"] == pytest.approx(97.0)
    assert card["entry_zone"] == {"low": 100.0, "high": 100.0}
    prices = [t["price"] for t in card["targets"]]
    assert prices == sorted(prices)
    recipe = next(t for t in card["targets"] if t["source"] == "recipe")
    assert recipe["price"] == pytest.approx(104.0)
    assert recipe["r_multiple"] == pytest.approx(1.33)
    assert [t["r_multiple"] for t in card["targets"] if t["source"] == "r_ladder"] == [1, 2, 3]
    assert card["confidence"]["label"] == "Chưa hiệu chuẩn"
    assert any("16 nến" in line for line in card["invalidation"])


def test_short_card_uses_approved_plan_entry_zone_and_descending_targets() -> None:
    card = build_signal_card(
        _candidate("short"),
        plan={"entry_price": "99.9", "stop_price": "103.1", "target_price": "96"},
        checklist=[{"key": "x", "passed": True}, {"key": "y", "passed": None}],
    )
    assert card["entry_zone"] == {"low": 99.9, "high": 100.0}
    prices = [t["price"] for t in card["targets"]]
    assert prices == sorted(prices, reverse=True)
    assert card["plan_source"] == "approved_plan"
    assert (card["checklist_passed"], card["checklist_total"]) == (1, 2)


def test_ladder_validation_rejects_wrong_side_targets() -> None:
    # Ví dụ ảnh tham khảo: lệnh BÁN nhưng TP3 nằm trên vùng vào.
    with pytest.raises(SignalCardError, match="SHORT"):
        validate_price_ladder(
            "short",
            entry_low=Decimal("3939.98"),
            entry_high=Decimal("3949.51"),
            stop=Decimal("3969.51"),
            targets=[Decimal("3924.56"), Decimal("3899.61"), Decimal("3964.93")],
        )
    with pytest.raises(SignalCardError, match="LONG: SL"):
        validate_price_ladder(
            "long", entry_low=Decimal(100), entry_high=Decimal(100), stop=Decimal(101), targets=[]
        )


# --- Quyết định có checklist, xác minh hash và dựng thẻ từ payload đã lưu ---------------------


def _decision_for(release_id: str, snapshot: object | None = None):
    import asyncio

    from tests.unit.test_bot_dispatcher import BOT_A, FixtureSnapshots, _context, _evidence

    from trading_bot.application.bot_dispatcher import BotDecisionJob, evaluate_bot_decision
    from trading_bot.domain.strategy.registry import StrategyRegistry

    context = _context(strategy_release_id=release_id)
    job = BotDecisionJob.create(
        evidence=_evidence(), context=context, evaluated_at=_evidence().available_at
    )
    snapshots = FixtureSnapshots({BOT_A: snapshot} if snapshot is not None else None)
    return asyncio.run(
        evaluate_bot_decision(
            evidence=_evidence(), job=job, strategies=StrategyRegistry(), snapshots=snapshots
        )
    )


def test_candidate_decision_persists_checklist_and_roundtrips_hash() -> None:
    from trading_bot.application.bot_dispatcher import BotDecision, BotDecisionStatus

    decision = _decision_for("h01-v1")
    assert decision.status is BotDecisionStatus.CANDIDATE
    payload = decision.canonical_payload()
    assert payload["checklist"]["version"] == "sig3-checklist-v1"
    assert all(item["passed"] is True for item in payload["checklist"]["items"])
    restored = BotDecision.from_payload(payload, decision.decision_hash)
    assert restored == decision


def test_abstain_decision_keeps_failed_checklist_items() -> None:
    from trading_bot.application.bot_dispatcher import BotDecisionStatus

    decision = _decision_for("h01-v1", _h01_snapshot(adx=10.0))
    assert decision.status is BotDecisionStatus.ABSTAIN
    items = decision.canonical_payload()["checklist"]["items"]
    adx = next(item for item in items if item["key"] == "adx")
    assert adx["passed"] is False
    assert adx["actual"] == "10"


def test_latest_signal_view_builds_card_and_skips_tampered_rows() -> None:
    from trading_bot.application.bot_reporting import build_latest_signal_view

    candidate = _decision_for("h01-v1")
    abstain = _decision_for("h01-v1", _h01_snapshot(adx=10.0))
    tampered = dict(abstain.canonical_payload(), reason_code="SỬA_TAY")
    rows = [
        {"payload": tampered, "decision_hash": abstain.decision_hash, "plan": None},
        {"payload": abstain.canonical_payload(), "decision_hash": abstain.decision_hash},
        {"payload": candidate.canonical_payload(), "decision_hash": candidate.decision_hash},
    ]
    view = build_latest_signal_view(rows)
    assert view["unverified_decisions"] == 1
    assert view["latest_decision"]["status"] == "ABSTAIN"
    assert view["latest_decision"]["checklist"]
    card = view["card"]
    assert card["direction"] == "long"
    assert card["decision_id"] == str(candidate.decision_id)
    assert card["checklist_passed"] == card["checklist_total"] > 0
    assert card["plan_source"] == "recipe"


def test_expired_card_is_flagged_and_not_overlaid() -> None:
    from datetime import UTC, datetime, timedelta

    from trading_bot.admin.web_router import _card_for_symbol
    from trading_bot.application.bot_reporting import build_latest_signal_view

    decision = _decision_for("h01-v1")
    rows = [{"payload": decision.canonical_payload(), "decision_hash": decision.decision_hash}]
    expires = datetime.fromisoformat(decision.canonical_payload()["candidate"]["expires_at"])
    live = build_latest_signal_view(rows, now=expires - timedelta(seconds=1))
    stale = build_latest_signal_view(rows, now=expires + timedelta(seconds=1))
    assert live["card"]["is_expired"] is False
    assert stale["card"]["is_expired"] is True
    assert _card_for_symbol(live, "BTCUSDT") is not None
    assert _card_for_symbol(stale, "BTCUSDT") is None
    assert _card_for_symbol(live, "ETHUSDT") is None
    assert expires.tzinfo is UTC or expires.utcoffset() == timedelta(0)
