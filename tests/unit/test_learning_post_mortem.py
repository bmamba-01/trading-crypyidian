"""Unit tests cho domain post_mortem và application learning_post_mortem."""

from decimal import Decimal
from uuid import uuid4

from trading_bot.application.learning_post_mortem import LearningPostMortemService
from trading_bot.domain.portfolio.post_mortem import (
    TradeQualityClassification,
    evaluate_trade_post_mortem,
)


def test_evaluate_trade_post_mortem_optimal_winning_trade() -> None:
    trade_id = uuid4()
    bot_id = uuid4()
    trade = {
        "trade_id": str(trade_id),
        "bot_id": str(bot_id),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "gross_pnl": "250.0",
        "fee_total": "5.0",
        "funding_total": "2.0",
        "slippage_total": "1.0",
        "net_pnl": "242.0",
        "exit_reason": "TAKE_PROFIT",
        "holding_duration_seconds": 7200,
        "entry_reason": {
            "strategy_id": "TREND_FOLLOWING_V1",
            "target_leverage": 5,
            "account_risk_pct": "1.0",
            "context_regime": "TRENDING",
            "rejection_wick_score": 85.0,
            "breakout_score": 90.0,
            "risk_reward_ratio": "2.5",
            "detail_vi": "Râu nến dưới dài xác nhận phe mua áp đảo.",
        },
        "fills": [{"role": "ENTRY", "price": "90000.0"}],
    }

    result = evaluate_trade_post_mortem(trade)
    assert result.trade_id == trade_id
    assert result.classification == TradeQualityClassification.OPTIMAL
    assert result.is_deviant is False
    assert result.recommended_counterfactual_action == "REINFORCE"
    assert result.ai_penalty_weight == Decimal("0.5")
    assert result.entry_analysis.target_leverage == 5
    assert result.entry_analysis.risk_reward_ratio == Decimal("2.5")
    assert result.exit_analysis.exit_reason_code == "TAKE_PROFIT"
    assert "Chốt lời" in result.exit_analysis.exit_reason_vi


def test_evaluate_trade_post_mortem_deviant_high_slippage() -> None:
    trade_id = uuid4()
    trade = {
        "trade_id": str(trade_id),
        "bot_id": str(uuid4()),
        "symbol": "ETHUSDT",
        "side": "SHORT",
        "gross_pnl": "-50.0",
        "fee_total": "3.0",
        "funding_total": "0.5",
        "slippage_total": "25.0",  # Trượt giá nặng > 15 USDT
        "net_pnl": "-78.5",
        "exit_reason": "STOP_LOSS",
        "holding_duration_seconds": 3600,
        "entry_reason": {"target_leverage": 3},
        "fills": [{"role": "ENTRY", "price": "3100.0"}],
    }

    result = evaluate_trade_post_mortem(trade)
    assert result.classification == TradeQualityClassification.DEVIANT_SLIPPAGE
    assert result.is_deviant is True
    assert "Trượt giá" in (result.deviation_reason or "")
    assert result.recommended_counterfactual_action == "ABSTAIN"
    assert result.ai_penalty_weight == Decimal("1.8")


def test_evaluate_trade_post_mortem_deviant_false_breakout() -> None:
    trade_id = uuid4()
    trade = {
        "trade_id": str(trade_id),
        "bot_id": str(uuid4()),
        "symbol": "SOLUSDT",
        "side": "LONG",
        "gross_pnl": "-95.0",
        "fee_total": "2.0",
        "funding_total": "0.0",
        "slippage_total": "0.1",
        "net_pnl": "-97.1",
        "exit_reason": "STOP_LOSS",
        "holding_duration_seconds": 300,  # Bị quét SL chỉ sau 5 phút (300 giây)
        "entry_reason": {"target_leverage": 10},
        "fills": [{"role": "ENTRY", "price": "180.0"}],
    }

    result = evaluate_trade_post_mortem(trade)
    assert result.classification == TradeQualityClassification.DEVIANT_FALSE_BREAKOUT
    assert result.is_deviant is True
    assert "Phá vỡ giả" in (result.deviation_reason or "")
    assert result.recommended_counterfactual_action == "ABSTAIN"
    assert result.ai_penalty_weight == Decimal("2.0")


def test_learning_post_mortem_service_ingestion() -> None:
    service = LearningPostMortemService()
    trade_id = uuid4()
    bot_id = uuid4()
    trade = {
        "trade_id": str(trade_id),
        "bot_id": str(bot_id),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "gross_pnl": "-150.0",
        "fee_total": "5.0",
        "funding_total": "1.0",
        "slippage_total": "20.0",
        "net_pnl": "-176.0",
        "exit_reason": "STOP_LOSS",
        "holding_duration_seconds": 600,
        "lineage": {"feature_snapshot_id": "feat_snap_123"},
    }

    sample = service.ingest_trade_for_ai_retraining(trade, ingested_by="OPERATOR_ALICE")
    assert sample.trade_id == trade_id
    assert sample.bot_id == bot_id
    assert sample.counterfactual_action == "ABSTAIN"
    assert sample.penalty_weight == "1.8"
    assert sample.ingested_by == "OPERATOR_ALICE"
    assert sample.feature_snapshot_id == "feat_snap_123"

    # Tra cứu
    retrieved = service.get_sample_by_trade_id(trade_id)
    assert retrieved is not None
    assert retrieved.sample_id == sample.sample_id
    assert len(service.list_ingested_samples()) == 1

    # Xuất mẫu huấn luyện DatasetSample
    dataset_samples = service.export_dataset_samples(workspace_id=uuid4())
    assert len(dataset_samples) == 1
    ds = dataset_samples[0]
    assert ds.sample_id == str(sample.sample_id)
    assert ds.outcome_id == str(trade_id)
    assert ds.label_value == 0  # ABSTAIN
    assert ds.label_version == "counterfactual_negative_v1"
    assert ds.source_dependent is False
