"""Kiểm thử đơn vị RISK-1 (CRY-31): trần rủi ro và notional theo nhóm tương quan symbol."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from tests.unit.test_risk_engine import _plan, _settings, _state

from trading_bot.domain.risk.correlation_groups import (
    CORRELATION_GROUP_NOTIONAL_EXCEEDED,
    CORRELATION_GROUP_POSITIONS_EXCEEDED,
    CORRELATION_GROUP_RISK_EXCEEDED,
    CRYPTO_L1_MAJORS,
    CRYPTO_L2_DEFI,
    CRYPTO_MEME,
    DEFAULT_CORRELATION_GROUP_POLICY,
    DEFAULT_SYMBOL_CORRELATION_MAP,
    CorrelationGroupPolicy,
    CorrelationGroupSpec,
    calculate_group_exposure,
    evaluate_correlation_group_cap,
    resolve_correlation_group,
)
from trading_bot.domain.risk.engine import RiskEngine
from trading_bot.domain.risk.planner import OpenExposure, TradePlan, calculate_plan_hash


def _plan_for(
    symbol: str,
    *,
    notional: Decimal | None = None,
    estimated_loss: Decimal | None = None,
) -> TradePlan:
    base = _plan()
    changes: dict[str, object] = {"symbol": symbol}
    if notional is not None:
        quantity = notional / base.entry_price
        changes["quantity"] = quantity
        changes["notional"] = notional
        changes["initial_margin"] = notional / base.leverage
    if estimated_loss is not None:
        changes["estimated_loss"] = estimated_loss
    updated = replace(base, **changes, plan_hash="")
    return replace(updated, plan_hash=calculate_plan_hash(updated))


def test_default_policy_maps_high_beta_crypto_groups_and_leaves_unmapped_none() -> None:
    """Bảng mặc định phân nhóm chuẩn xác các mã L1 majors, L2/DeFi, Meme và mã độc lập."""
    for symbol in ("BTCUSDT", "ethusdt", " SOLUSDT ", "BNBUSDT", "AVAXUSDT", "XRPUSDT"):
        assert resolve_correlation_group(symbol) == CRYPTO_L1_MAJORS
    for symbol in ("ARBUSDT", "OPUSDT", "UNIUSDT", "AAVEUSDT"):
        assert resolve_correlation_group(symbol) == CRYPTO_L2_DEFI
    for symbol in ("DOGEUSDT", "PEPEUSDT", "WIFUSDT", "1000PEPEUSDT"):
        assert resolve_correlation_group(symbol) == CRYPTO_MEME
    assert DEFAULT_SYMBOL_CORRELATION_MAP["BTCUSDT"] == CRYPTO_L1_MAJORS
    assert resolve_correlation_group("PAXGUSDT") is None
    assert resolve_correlation_group("   ") is None


def test_risk_engine_rejects_correlated_l1_group_risk_breach_with_default_policy() -> None:
    """Mở thêm BTCUSDT khi đã có ETHUSDT cùng nhóm CRYPTO_L1_MAJORS vượt trần rủi ro nhóm."""
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=3),
    )
    # Trần aggregate = 250, trần nhóm mặc định (60%) = 150.
    # ETHUSDT đang mở risk = 80, BTCUSDT mới có estimated_loss ~ 84.92 -> tổng nhóm 164.92 > 150.
    state = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "ETHUSDT", Decimal("1000"), Decimal("80")),
        ),
        open_position_count=1,
    )

    decision = RiskEngine().evaluate(_plan(), state, settings)

    assert not decision.approved
    assert decision.reason_code == CORRELATION_GROUP_RISK_EXCEEDED
    assert CRYPTO_L1_MAJORS in decision.detail_vi


def test_risk_engine_rejects_correlated_l1_group_notional_breach_with_default_policy() -> None:
    """Mở thêm BTCUSDT khi tổng notional nhóm CRYPTO_L1_MAJORS vượt trần notional nhóm."""
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=3),
    )
    # Trần notional tài khoản = 10000, trần mỗi symbol = 2000, trần nhóm mặc định (35%) = 3500.
    # ETHUSDT đang mở notional = 1800 (risk thấp = 20), BTCUSDT mới notional = 2000 -> 3800 > 3500.
    state = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "ETHUSDT", Decimal("1800"), Decimal("20")),
        ),
        open_position_count=1,
    )

    decision = RiskEngine().evaluate(_plan(), state, settings)

    assert not decision.approved
    assert decision.reason_code == CORRELATION_GROUP_NOTIONAL_EXCEEDED
    assert CRYPTO_L1_MAJORS in decision.detail_vi


def test_risk_engine_allows_positions_across_different_correlation_groups() -> None:
    """Vị thế mở ở nhóm khác (CRYPTO_MEME) không làm đầy trần nhóm CRYPTO_L1_MAJORS."""
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=3),
    )
    state = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "DOGEUSDT", Decimal("1800"), Decimal("80")),
        ),
        open_position_count=1,
    )

    decision = RiskEngine().evaluate(_plan(), state, settings)

    assert decision.approved
    assert decision.reason_code == "APPROVED"


def test_risk_engine_allows_same_group_when_within_group_risk_and_notional_caps() -> None:
    """Nhiều vị thế cùng nhóm nhưng tổng risk <= 150 và tổng notional <= 3500 vẫn được duyệt."""
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=3),
    )
    state = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "SOLUSDT", Decimal("1200"), Decimal("50")),
        ),
        open_position_count=1,
    )

    exposure = calculate_group_exposure(plan=_plan(), state=state, limits=settings.limits)
    assert exposure is not None
    assert exposure.group_id == CRYPTO_L1_MAJORS
    assert exposure.symbols == ("BTCUSDT", "SOLUSDT")
    assert exposure.open_risk == Decimal("50")
    assert exposure.open_notional == Decimal("1200")
    assert exposure.total_notional == Decimal("3200")
    assert exposure.max_group_risk == Decimal("150.00")
    assert exposure.max_group_notional == Decimal("3500.00")

    decision = RiskEngine().evaluate(_plan(), state, settings)
    assert decision.approved
    assert decision.reason_code == "APPROVED"


def test_custom_workspace_mapping_and_explicit_group_caps_are_enforced() -> None:
    """Cho phép cấu hình bảng ánh xạ tùy chỉnh theo workspace và đặt trần tuyệt đối/tỷ lệ."""
    custom_policy = CorrelationGroupPolicy.from_mapping(
        {
            "BTCUSDT": "WORKSPACE_CORE_BETA",
            "PAXGUSDT": "WORKSPACE_CORE_BETA",
        },
        group_specs=(
            CorrelationGroupSpec(
                group_id="WORKSPACE_CORE_BETA",
                symbols=("BTCUSDT", "PAXGUSDT"),
                label_vi="Nhóm tùy chỉnh workspace",
                max_risk_amount=Decimal("120"),
                max_notional=Decimal("2500"),
                max_positions=2,
            ),
        ),
        include_defaults=False,
    )
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=4),
    )
    # PAXGUSDT bình thường không thuộc L1, nhưng trong custom_policy được gom cùng BTCUSDT.
    state_risk = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "PAXGUSDT", Decimal("400"), Decimal("40")),
        ),
        open_position_count=1,
        correlation_policy=custom_policy,
    )
    decision_risk = RiskEngine().evaluate(_plan(), state_risk, settings)
    assert not decision_risk.approved
    assert decision_risk.reason_code == CORRELATION_GROUP_RISK_EXCEEDED
    assert "WORKSPACE_CORE_BETA" in decision_risk.detail_vi

    state_notional = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "PAXGUSDT", Decimal("600"), Decimal("10")),
        ),
        open_position_count=1,
        correlation_policy=custom_policy,
    )
    decision_notional = RiskEngine().evaluate(_plan(), state_notional, settings)
    assert not decision_notional.approved
    assert decision_notional.reason_code == CORRELATION_GROUP_NOTIONAL_EXCEEDED

    mapping_only_policy = CorrelationGroupPolicy.from_mapping(
        {"BTCUSDT": "CUSTOM_PAIR", "PAXGUSDT": "CUSTOM_PAIR"},
        include_defaults=False,
    )
    mapping_exposure = calculate_group_exposure(
        plan=_plan(),
        state=state_risk,
        limits=settings.limits,
        policy=mapping_only_policy,
    )
    assert mapping_exposure is not None
    assert mapping_exposure.group_id == "CUSTOM_PAIR"
    assert mapping_exposure.max_group_risk == Decimal("150.00")
    assert mapping_exposure.max_group_notional == Decimal("3500.00")


def test_explicit_group_cap_cannot_loosen_account_hard_caps() -> None:
    """Trần nhóm cấu hình cao hơn hard cap tài khoản vẫn bị kẹp bởi limits tài khoản."""
    loose_policy = CorrelationGroupPolicy(
        groups=(
            CorrelationGroupSpec(
                group_id=CRYPTO_L1_MAJORS,
                symbols=("BTCUSDT", "ETHUSDT"),
                max_risk_amount=Decimal("999999"),
                max_notional=Decimal("999999"),
            ),
        )
    )
    settings = _settings()
    exposure = calculate_group_exposure(
        plan=_plan(),
        state=_state(),
        limits=settings.limits,
        policy=loose_policy,
    )
    assert exposure is not None
    assert exposure.max_group_risk == settings.limits.max_aggregate_open_risk
    assert exposure.max_group_notional == settings.limits.max_notional


def test_equity_and_aggregate_fraction_group_caps_and_call_override() -> None:
    """Trần theo tỷ lệ vốn (max_equity_*_fraction) và override correlation_policy ở evaluate."""
    policy_risk = CorrelationGroupPolicy(
        groups=(
            CorrelationGroupSpec(
                group_id=CRYPTO_L1_MAJORS,
                symbols=("BTCUSDT", "ETHUSDT"),
                max_equity_risk_fraction=Decimal("0.012"),  # 1.2% của 10000 = 120
                max_risk_fraction=Decimal("0.80"),  # 80% của 250 = 200 -> min = 120
            ),
        )
    )
    policy_notional = CorrelationGroupPolicy(
        groups=(
            CorrelationGroupSpec(
                group_id=CRYPTO_L1_MAJORS,
                symbols=("BTCUSDT", "ETHUSDT"),
                max_equity_notional_fraction=Decimal("0.25"),  # 25% của 10000 = 2500
                max_notional_fraction=Decimal("0.50"),  # 50% của 10000 = 5000 -> min = 2500
            ),
        )
    )
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=3),
    )
    state = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "ETHUSDT", Decimal("600"), Decimal("40")),
        ),
        open_position_count=1,
    )

    rejected_risk = RiskEngine().evaluate(_plan(), state, settings, correlation_policy=policy_risk)
    assert not rejected_risk.approved
    assert rejected_risk.reason_code == CORRELATION_GROUP_RISK_EXCEEDED

    rejected_notional = RiskEngine().evaluate(
        _plan(), state, settings, correlation_policy=policy_notional
    )
    assert not rejected_notional.approved
    assert rejected_notional.reason_code == CORRELATION_GROUP_NOTIONAL_EXCEEDED


def test_group_max_positions_and_fallback_group_enforcement() -> None:
    """Kiểm tra giới hạn số lượng vị thế tối đa trong một nhóm và fallback_group_id."""
    policy = CorrelationGroupPolicy(
        groups=(
            CorrelationGroupSpec(
                group_id="ALT_BASKET",
                symbols=("FETUSDT", "RENDERUSDT", "TAOUSDT"),
                max_positions=2,
            ),
        ),
        fallback_group_id="ALT_BASKET",
    )
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=5, max_notional=Decimal("50000")),
    )
    state = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "FETUSDT", Decimal("500"), Decimal("20")),
            OpenExposure("workspace-a", "paper-a", "UNMAPPEDUSDT", Decimal("500"), Decimal("20")),
        ),
        open_position_count=2,
    )
    engine = RiskEngine(correlation_policy=policy)
    plan = _plan_for("RENDERUSDT", notional=Decimal("500"), estimated_loss=Decimal("20"))

    decision = engine.evaluate(plan, state, settings)

    assert not decision.approved
    assert decision.reason_code == CORRELATION_GROUP_POSITIONS_EXCEEDED
    assert "ALT_BASKET" in decision.detail_vi


def test_pending_group_reservations_and_invalid_state_handling() -> None:
    """Reservation đang giữ chỗ của nhóm được cộng vào trần và từ chối khi giá trị âm/vô hạn."""
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=3),
    )
    state_pending_risk = _state(
        pending_reservation_risk=Decimal("70"),
        pending_group_reservation_risk=Decimal("70"),
        pending_reservation_count=1,
    )
    decision_risk = RiskEngine().evaluate(_plan(), state_pending_risk, settings)
    assert not decision_risk.approved
    assert decision_risk.reason_code == CORRELATION_GROUP_RISK_EXCEEDED

    state_pending_notional = _state(
        pending_reservation_notional=Decimal("1600"),
        pending_group_reservation_notional=Decimal("1600"),
        pending_reservation_count=1,
    )
    decision_notional = RiskEngine().evaluate(_plan(), state_pending_notional, settings)
    assert not decision_notional.approved
    assert decision_notional.reason_code == CORRELATION_GROUP_NOTIONAL_EXCEEDED

    invalid_state = _state(pending_group_reservation_risk=Decimal("-1"))
    invalid_decision = RiskEngine().evaluate(_plan(), invalid_state, settings)
    assert not invalid_decision.approved
    assert invalid_decision.reason_code == "RISK_STATE_INVALID"


def test_disabled_policy_or_unmapped_symbol_skips_group_cap() -> None:
    """Khi tắt policy hoặc symbol không thuộc nhóm nào, cổng tương quan trả APPROVED."""
    settings = replace(
        _settings(),
        limits=replace(_settings().limits, max_positions=3),
    )
    state = _state(
        open_exposures=(
            OpenExposure("workspace-a", "paper-a", "ETHUSDT", Decimal("1900"), Decimal("90")),
        ),
        open_position_count=1,
    )
    disabled = replace(DEFAULT_CORRELATION_GROUP_POLICY, enabled=False)
    decision = RiskEngine(correlation_policy=disabled).evaluate(_plan(), state, settings)
    assert decision.approved

    unmapped_plan = _plan_for("PAXGUSDT")
    unmapped_check = evaluate_correlation_group_cap(
        plan=unmapped_plan,
        state=state,
        limits=settings.limits,
    )
    assert unmapped_check.approved
    assert unmapped_check.group_id is None
    assert (
        calculate_group_exposure(
            plan=unmapped_plan,
            state=state,
            limits=settings.limits,
        )
        is None
    )


def test_correlation_group_validation_rejects_invalid_specs_and_duplicates() -> None:
    """Kiểm tra fail-closed khi cấu hình nhóm tương quan trùng symbol hoặc sai miền giá trị."""
    with pytest.raises(ValueError, match="group_id"):
        CorrelationGroupSpec(group_id="  ", symbols=("BTCUSDT",))
    with pytest.raises(ValueError, match="symbols"):
        CorrelationGroupSpec(group_id="G1", symbols=())
    with pytest.raises(ValueError, match="Tỷ lệ"):
        CorrelationGroupSpec(group_id="G1", symbols=("BTCUSDT",), max_risk_fraction=Decimal("0"))
    with pytest.raises(ValueError, match="Tỷ lệ"):
        CorrelationGroupSpec(
            group_id="G1", symbols=("BTCUSDT",), max_notional_fraction=Decimal("1.1")
        )
    with pytest.raises(ValueError, match="Giới hạn"):
        CorrelationGroupSpec(group_id="G1", symbols=("BTCUSDT",), max_risk_amount=Decimal("-1"))
    with pytest.raises(ValueError, match="max_positions"):
        CorrelationGroupSpec(group_id="G1", symbols=("BTCUSDT",), max_positions=0)
    with pytest.raises(ValueError, match="trùng"):
        CorrelationGroupPolicy(
            groups=(
                CorrelationGroupSpec(group_id="G1", symbols=("BTCUSDT",)),
                CorrelationGroupSpec(group_id="G2", symbols=("BTCUSDT", "ETHUSDT")),
            )
        )
    with pytest.raises(ValueError, match="group_id"):
        CorrelationGroupPolicy(
            groups=(
                CorrelationGroupSpec(group_id="G1", symbols=("BTCUSDT",)),
                CorrelationGroupSpec(group_id="G1", symbols=("ETHUSDT",)),
            )
        )
