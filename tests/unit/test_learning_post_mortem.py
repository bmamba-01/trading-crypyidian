"""Unit tests cho domain post_mortem và application learning_post_mortem.

Nguyên tắc được kiểm: hậu kiểm chỉ dùng dữ liệu có trong giao dịch được truyền vào. Trường
không có nguồn phải là `None`, quy tắc không đủ dữ liệu phải được ghi là chưa kiểm, không
được thay bằng giá trị mặc định.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.learning_post_mortem import LearningPostMortemService
from trading_bot.domain.portfolio.post_mortem import (
    CHECK_EXCESSIVE_LOSS,
    CHECK_FALSE_BREAKOUT,
    CHECK_SLIPPAGE,
    TradeQualityClassification,
    evaluate_trade_post_mortem,
)


def _ledger_trade(**overrides: object) -> dict[str, object]:
    """Dòng giao dịch có hình dạng giống báo cáo ledger PAPER (không có trường L4–L6)."""
    trade: dict[str, object] = {
        "trade_id": str(uuid4()),
        "bot_id": str(uuid4()),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quote_asset": "USDT",
        "opened_at": "2026-11-10T01:00:00+00:00",
        "closed_at": "2026-11-10T03:00:00+00:00",
        "gross_pnl": "250",
        "fee_total": "5",
        "funding_total": "2",
        "slippage_total": "1",
        "net_pnl": "242",
        "exit_reason": "TAKE_PROFIT",
        "entry_reason": {
            "strategy_id": "H03_BREAKOUT",
            "reason_code": "BREAKOUT_CONFIRMED",
            "detail_vi": "Phá vỡ vùng tích lũy có xác nhận.",
            "reference_price": "90000",
            "signal_at": "2026-11-10T00:55:00+00:00",
        },
        "fills": [
            {"role": "ENTRY", "quantity": "0.5", "price": "90000"},
            {"role": "EXIT", "quantity": "0.5", "price": "90500"},
        ],
    }
    trade.update(overrides)
    return trade


def test_ledger_trade_keeps_unsourced_entry_fields_empty() -> None:
    result = evaluate_trade_post_mortem(_ledger_trade())
    entry = result.entry_analysis

    assert entry.strategy_id == "H03_BREAKOUT"
    assert entry.reason_code == "BREAKOUT_CONFIRMED"
    assert entry.detail_vi == "Phá vỡ vùng tích lũy có xác nhận."
    assert entry.reference_price == Decimal("90000")
    assert entry.signal_at == "2026-11-10T00:55:00+00:00"
    assert entry.entry_price == Decimal("90000")
    assert entry.entry_quantity == Decimal("0.5")
    # Ledger PAPER không lưu các trường này nên không được tự điền.
    assert entry.target_leverage is None
    assert entry.account_risk_pct is None
    assert entry.context_regime is None
    assert entry.context_gate_passed is None
    assert entry.rejection_wick_score is None
    assert entry.breakout_score is None
    assert entry.risk_reward_ratio is None
    assert entry.stop_loss_price is None
    assert entry.take_profit_price is None

    payload = result.as_dict()["entry_analysis"]
    assert payload["target_leverage"] is None
    assert payload["stop_loss_price"] is None
    assert payload["take_profit_price"] is None


def test_exit_metrics_come_from_fills_and_ledger_timestamps() -> None:
    result = evaluate_trade_post_mortem(_ledger_trade(slippage_total="9"))
    exit_analysis = result.exit_analysis

    # Notional vào lệnh = 0,5 × 90000 = 45000; 9 / 45000 × 10000 = 2 bps.
    assert exit_analysis.slippage_bps == Decimal("2")
    assert exit_analysis.holding_duration_seconds == 7200
    assert exit_analysis.exit_reason_code == "TAKE_PROFIT"
    assert "Chốt lời" in (exit_analysis.exit_reason_vi or "")
    # Không có ngân sách rủi ro kế hoạch nên không tính bội số R.
    assert exit_analysis.net_pnl_r_multiple is None


def test_winning_ledger_trade_is_optimal_and_names_the_unevaluated_check() -> None:
    result = evaluate_trade_post_mortem(_ledger_trade())

    assert result.classification == TradeQualityClassification.OPTIMAL
    assert result.is_deviant is False
    assert result.deviation_reason is None
    assert result.recommended_counterfactual_action == "REINFORCE"
    assert result.ai_penalty_weight == Decimal("0.5")
    assert result.unevaluated_checks == (CHECK_SLIPPAGE, CHECK_EXCESSIVE_LOSS)


PLAN = {
    "entry_price": "100.4",
    "stop_price": "97.6",
    "target_price": "105.4",
    "leverage": "5",
    "risk_budget": "100.0000000000",
    "entry_slippage_cost": "6.25",
}


def test_slippage_within_planned_budget_is_not_deviant() -> None:
    """Mô hình PAPER tính trượt giá bất lợi cả vào lẫn ra: đúng mức dự trù thì không sai lệch."""
    result = evaluate_trade_post_mortem(_ledger_trade(slippage_total="12.5"), plan=PLAN)

    assert result.exit_analysis.expected_slippage == Decimal("12.50")
    assert result.classification == TradeQualityClassification.OPTIMAL
    assert result.unevaluated_checks == ()


def test_slippage_beyond_one_and_a_half_times_plan_is_deviant() -> None:
    result = evaluate_trade_post_mortem(
        _ledger_trade(slippage_total="19", net_pnl="-90", gross_pnl="-60", exit_reason="STOP_LOSS"),
        plan=PLAN,
    )

    assert result.classification == TradeQualityClassification.DEVIANT_SLIPPAGE
    assert result.is_deviant is True
    assert "Trượt giá" in (result.deviation_reason or "")
    assert "12.50" in (result.deviation_reason or "")
    assert result.recommended_counterfactual_action == "ABSTAIN"
    assert result.ai_penalty_weight == Decimal("1.8")


def test_slippage_rule_is_unevaluated_without_an_approved_plan() -> None:
    """Không có kế hoạch thì không có mức dự trù để so; không dùng ngưỡng tuyệt đối."""
    result = evaluate_trade_post_mortem(_ledger_trade(slippage_total="500"))

    assert result.classification == TradeQualityClassification.OPTIMAL
    assert CHECK_SLIPPAGE in result.unevaluated_checks
    # Chỉ số bps vẫn được tính để hiển thị: 500 / 45000 × 10000.
    assert result.exit_analysis.slippage_bps is not None


def test_false_breakout_rule_uses_real_holding_time() -> None:
    result = evaluate_trade_post_mortem(
        _ledger_trade(
            closed_at="2026-11-10T01:05:00+00:00",
            exit_reason="STOP_LOSS",
            gross_pnl="-95",
            net_pnl="-97.1",
            slippage_total="0.1",
        )
    )

    assert result.exit_analysis.holding_duration_seconds == 300
    assert result.classification == TradeQualityClassification.DEVIANT_FALSE_BREAKOUT
    assert "Phá vỡ giả" in (result.deviation_reason or "")
    assert result.ai_penalty_weight == Decimal("2.0")


def test_excessive_loss_rule_runs_only_with_planned_risk() -> None:
    losing = _ledger_trade(gross_pnl="-140", net_pnl="-150", exit_reason="STOP_LOSS")

    without_risk = evaluate_trade_post_mortem(losing)
    assert without_risk.classification == TradeQualityClassification.ACCEPTABLE
    assert CHECK_EXCESSIVE_LOSS in without_risk.unevaluated_checks

    with_risk = evaluate_trade_post_mortem(losing, planned_risk_amount=Decimal("100"))
    assert with_risk.classification == TradeQualityClassification.DEVIANT_EXCESSIVE_LOSS
    assert with_risk.exit_analysis.net_pnl_r_multiple == Decimal("-1.5")
    assert with_risk.unevaluated_checks == (CHECK_SLIPPAGE,)
    assert "USDT" in (with_risk.deviation_reason or "")


def test_sparse_trade_is_unverified_instead_of_optimal() -> None:
    """Thiếu fills, mốc thời gian và ngân sách rủi ro thì không quy tắc nào kiểm được."""
    sparse = {
        "trade_id": str(uuid4()),
        "bot_id": str(uuid4()),
        "symbol": "ETHUSDT",
        "side": "SHORT",
        "net_pnl": "12",
    }
    result = evaluate_trade_post_mortem(sparse)

    assert result.classification == TradeQualityClassification.UNVERIFIED
    assert result.is_deviant is False
    assert result.recommended_counterfactual_action == "REVIEW_REQUIRED"
    assert result.ai_penalty_weight is None
    assert set(result.unevaluated_checks) == {
        CHECK_SLIPPAGE,
        CHECK_FALSE_BREAKOUT,
        CHECK_EXCESSIVE_LOSS,
    }
    assert result.exit_analysis.exit_reason_code is None
    assert result.exit_analysis.holding_duration_seconds is None
    assert result.exit_analysis.slippage_bps is None
    assert result.entry_analysis.strategy_id is None
    assert result.entry_analysis.entry_price is None


def test_missing_net_pnl_is_unverified() -> None:
    trade = _ledger_trade()
    del trade["net_pnl"]

    result = evaluate_trade_post_mortem(trade)
    assert result.classification == TradeQualityClassification.UNVERIFIED
    assert result.exit_analysis.net_pnl is None


@pytest.mark.parametrize("missing", ["trade_id", "bot_id", "symbol", "side"])
def test_identity_fields_are_required(missing: str) -> None:
    trade = _ledger_trade()
    del trade[missing]

    with pytest.raises(ValueError, match=missing):
        evaluate_trade_post_mortem(trade)


def test_explicit_sourced_entry_fields_are_passed_through() -> None:
    """Khi nguồn (ví dụ strategy pipeline) thật sự cung cấp L4–L6 thì engine giữ nguyên."""
    result = evaluate_trade_post_mortem(
        _ledger_trade(
            entry_reason={
                "strategy_id": "TREND_FOLLOWING_V1",
                "target_leverage": 5,
                "account_risk_pct": "1.0",
                "context_regime": "TRENDING",
                "context_gate_passed": True,
                "rejection_wick_score": 85.0,
                "breakout_score": 90.0,
                "risk_reward_ratio": "2.5",
                "stop_loss_price": "88650",
                "take_profit_price": "93375",
            }
        )
    )
    entry = result.entry_analysis

    assert entry.target_leverage == 5
    assert entry.account_risk_pct == Decimal("1.0")
    assert entry.context_regime == "TRENDING"
    assert entry.context_gate_passed is True
    assert entry.rejection_wick_score == 85.0
    assert entry.breakout_score == 90.0
    assert entry.risk_reward_ratio == Decimal("2.5")
    assert entry.stop_loss_price == Decimal("88650")
    assert entry.take_profit_price == Decimal("93375")


def test_learning_post_mortem_service_ingestion() -> None:
    service = LearningPostMortemService()
    trade = _ledger_trade(
        closed_at="2026-11-10T01:05:00+00:00",
        gross_pnl="-90",
        net_pnl="-176",
        exit_reason="STOP_LOSS",
        lineage={"feature_snapshot_id": "feat_snap_123"},
    )

    sample = service.ingest_trade_for_ai_retraining(trade, ingested_by="OPERATOR_ALICE")
    assert str(sample.trade_id) == trade["trade_id"]
    assert str(sample.bot_id) == trade["bot_id"]
    assert sample.counterfactual_action == "ABSTAIN"
    assert sample.penalty_weight == "2.0"
    assert sample.ingested_by == "OPERATOR_ALICE"
    assert sample.feature_snapshot_id == "feat_snap_123"

    retrieved = service.get_sample_by_trade_id(sample.trade_id)
    assert retrieved is not None
    assert retrieved.sample_id == sample.sample_id
    assert len(service.list_ingested_samples()) == 1


def test_ingestion_of_unverified_trade_records_no_penalty_weight() -> None:
    service = LearningPostMortemService()
    sample = service.ingest_trade_for_ai_retraining(
        {
            "trade_id": str(uuid4()),
            "bot_id": str(uuid4()),
            "symbol": "ETHUSDT",
            "side": "SHORT",
        },
        override_reason="Người rà soát gắn nhãn thủ công.",
    )

    assert sample.counterfactual_action == "REVIEW_REQUIRED"
    assert sample.penalty_weight is None
    assert sample.as_dict()["penalty_weight"] is None
    assert sample.deviation_reason == "Người rà soát gắn nhãn thủ công."


def test_service_does_not_fabricate_training_samples() -> None:
    """Nhãn thủ công không có lineage đặc trưng/nhãn nên không được biến thành DatasetSample."""
    assert not hasattr(LearningPostMortemService, "export_dataset_samples")


def test_approved_plan_is_the_source_for_levels_leverage_and_planned_risk() -> None:
    """Kế hoạch đã duyệt cung cấp SL/TP, đòn bẩy, R:R và ngân sách rủi ro thật của lệnh."""
    plan = {
        "entry_price": "100.4",
        "stop_price": "97.6",
        "target_price": "105.4",
        "leverage": "5",
        "risk_budget": "100.0000000000",
    }
    losing = _ledger_trade(gross_pnl="-140", net_pnl="-150", exit_reason="STOP_LOSS")

    result = evaluate_trade_post_mortem(losing, plan=plan)
    entry = result.entry_analysis

    assert entry.stop_loss_price == Decimal("97.6")
    assert entry.take_profit_price == Decimal("105.4")
    assert entry.target_leverage == 5
    assert entry.planned_risk_amount == Decimal("100.0000000000")
    # |105,4 − 100,4| / |100,4 − 97,6| = 5 / 2,8
    assert entry.risk_reward_ratio == Decimal("5.0") / Decimal("2.8")
    assert result.exit_analysis.net_pnl_r_multiple == Decimal("-1.5")
    assert result.classification == TradeQualityClassification.DEVIANT_EXCESSIVE_LOSS
    assert result.unevaluated_checks == (CHECK_SLIPPAGE,)
    assert result.as_dict()["entry_analysis"]["planned_risk_amount"] == "100.0000000000"


def test_missing_plan_keeps_levels_empty() -> None:
    result = evaluate_trade_post_mortem(_ledger_trade(), plan=None)

    assert result.entry_analysis.stop_loss_price is None
    assert result.entry_analysis.planned_risk_amount is None
    assert CHECK_EXCESSIVE_LOSS in result.unevaluated_checks
