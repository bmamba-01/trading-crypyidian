"""Domain model cho việc mổ xẻ sau giao dịch (Trade Post-Mortem) và phân loại chất lượng thực thi.

Hỗ trợ phân tích nguyên nhân vào/ra lệnh, rủi ro, trượt giá, và đánh dấu
các ca sai lệch (Deviant trades) để chuyển giao sang hệ thống huấn luyện AI.
"""
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID


class TradeQualityClassification(StrEnum):
    """Phân loại chất lượng giao dịch sau khi kết thúc."""

    OPTIMAL = "OPTIMAL"  # Giao dịch chuẩn mực, tuân thủ tín hiệu và quản trị rủi ro tốt
    ACCEPTABLE = "ACCEPTABLE"  # Giao dịch chấp nhận được (thua lỗ kiểm soát trong 1R)
    DEVIANT_SLIPPAGE = "DEVIANT_SLIPPAGE"  # Sai lệch do trượt giá nghiêm trọng trên sổ lệnh
    DEVIANT_FALSE_BREAKOUT = "DEVIANT_FALSE_BREAKOUT"  # Phá vỡ giả, đảo chiều tức thì quét SL
    DEVIANT_CONTEXT_MISMATCH = (
        "DEVIANT_CONTEXT_MISMATCH"  # Bối cảnh thị trường không phù hợp (Chop/Sideway)
    )
    DEVIANT_EXCESSIVE_LOSS = (
        "DEVIANT_EXCESSIVE_LOSS"  # Thua lỗ vượt mức kế hoạch rủi ro ban đầu (> 1.2R)
    )


@dataclass(frozen=True, slots=True)
class EntryReasoningBreakdown:
    """Bóc tách lý do vào vị thế theo các tầng định lượng L4-L6 và tín hiệu tổng hợp."""

    strategy_id: str
    composite_signal: str  # LONG hoặc SHORT
    target_leverage: int  # Mức đòn bẩy tính toán (vd: 3x, 5x, 10x)
    account_risk_pct: Decimal  # Tỷ lệ rủi ro tài khoản (vd: 1.0%)
    context_regime: str  # TRENDING, CHOP, VOLATILE
    context_gate_passed: bool
    rejection_wick_score: float  # Điểm nến rút chân (0.0 -> 100.0)
    breakout_score: float  # Điểm phá vỡ đỉnh/đáy (0.0 -> 100.0)
    risk_reward_ratio: Decimal  # Tỷ lệ R:R tính toán (vd: 2.0)
    stop_loss_price: Decimal
    take_profit_price: Decimal
    detail_vi: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "composite_signal": self.composite_signal,
            "target_leverage": self.target_leverage,
            "account_risk_pct": str(self.account_risk_pct),
            "context_regime": self.context_regime,
            "context_gate_passed": self.context_gate_passed,
            "rejection_wick_score": round(self.rejection_wick_score, 1),
            "breakout_score": round(self.breakout_score, 1),
            "risk_reward_ratio": str(self.risk_reward_ratio),
            "stop_loss_price": str(self.stop_loss_price),
            "take_profit_price": str(self.take_profit_price),
            "detail_vi": self.detail_vi,
        }


@dataclass(frozen=True, slots=True)
class ExitReasoningBreakdown:
    """Bóc tách lý do thoát vị thế và chi phí thực tế."""

    exit_reason_code: str  # TAKE_PROFIT, STOP_LOSS, TIME_STOP, OPPOSING_SIGNAL, FLATTEN
    exit_reason_vi: str  # Giải thích tiếng Việt
    gross_pnl: Decimal
    fee_total: Decimal
    funding_total: Decimal
    slippage_total: Decimal
    net_pnl: Decimal
    pnl_percentage: Decimal
    holding_duration_seconds: int
    slippage_bps: Decimal  # Điểm cơ bản trượt giá (Basis points)

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_reason_code": self.exit_reason_code,
            "exit_reason_vi": self.exit_reason_vi,
            "gross_pnl": str(self.gross_pnl),
            "fee_total": str(self.fee_total),
            "funding_total": str(self.funding_total),
            "slippage_total": str(self.slippage_total),
            "net_pnl": str(self.net_pnl),
            "pnl_percentage": str(self.pnl_percentage),
            "holding_duration_seconds": self.holding_duration_seconds,
            "slippage_bps": str(self.slippage_bps),
        }


@dataclass(frozen=True, slots=True)
class TradePostMortem:
    """Bản tổng hợp mổ xẻ sau giao dịch kèm phân loại và đề xuất huấn luyện AI."""

    trade_id: UUID
    bot_id: UUID
    symbol: str
    side: str
    classification: TradeQualityClassification
    is_deviant: bool
    deviation_reason: str | None
    entry_analysis: EntryReasoningBreakdown
    exit_analysis: ExitReasoningBreakdown
    recommended_counterfactual_action: str  # ABSTAIN, TIGHTEN_SL, REDUCE_LEVERAGE
    ai_penalty_weight: Decimal  # Hệ số phạt cho huấn luyện AI (1.0 = bình thường, 2.0 = phạt nặng)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_id": str(self.trade_id),
            "bot_id": str(self.bot_id),
            "symbol": self.symbol,
            "side": self.side,
            "classification": self.classification.value,
            "is_deviant": self.is_deviant,
            "deviation_reason": self.deviation_reason,
            "entry_analysis": self.entry_analysis.as_dict(),
            "exit_analysis": self.exit_analysis.as_dict(),
            "recommended_counterfactual_action": self.recommended_counterfactual_action,
            "ai_penalty_weight": str(self.ai_penalty_weight),
        }


def evaluate_trade_post_mortem(
    trade: Mapping[str, Any],
    *,
    initial_risk_budget: Decimal = Decimal("100"),
) -> TradePostMortem:
    """Đánh giá toàn diện một giao dịch đã đóng để phân loại chất lượng và đề xuất huấn luyện AI.

    Quy tắc phân loại:
    1. Nếu trượt giá (slippage) > 0.15% giá trị giao dịch -> DEVIANT_SLIPPAGE.
    2. Nếu lỗ ròng vượt 1.25R kế hoạch -> DEVIANT_EXCESSIVE_LOSS.
    3. Nếu đóng STOP_LOSS trong vòng dưới 3 cây nến (thời gian giữ rất ngắn) -> DEVIANT_FALSE_BREAKOUT.
    4. Nếu net_pnl > 0 hoặc lỗ nhẹ <= 1.0R tuân thủ kỷ luật -> OPTIMAL hoặc ACCEPTABLE.
    """
    trade_id = UUID(str(trade.get("trade_id")))
    bot_id = UUID(str(trade.get("bot_id")))
    symbol = str(trade.get("symbol", "BTCUSDT"))
    side = str(trade.get("side", "LONG")).upper()

    gross_pnl = Decimal(str(trade.get("gross_pnl", "0")))
    fee_total = Decimal(str(trade.get("fee_total", "0")))
    funding_total = Decimal(str(trade.get("funding_total", "0")))
    slippage_total = Decimal(str(trade.get("slippage_total", "0")))
    net_pnl = Decimal(str(trade.get("net_pnl", "0")))

    exit_reason_code = str(trade.get("exit_reason") or "STOP_LOSS")
    exit_map = {
        "TAKE_PROFIT": "Chốt lời theo mục tiêu",
        "STOP_LOSS": "Cắt lỗ bảo vệ vốn",
        "TIME_STOP": "Đóng theo thời gian nắm giữ",
        "OPPOSING_SIGNAL": "Tín hiệu kỹ thuật đảo chiều",
        "FLATTEN_OPERATION": "Đóng theo thao tác quản trị khẩn cấp",
    }
    exit_reason_vi = exit_map.get(exit_reason_code, exit_reason_code)

    entry_reason_dict = trade.get("entry_reason")
    if not isinstance(entry_reason_dict, dict):
        entry_reason_dict = {}

    strategy_id = str(entry_reason_dict.get("strategy_id") or "TREND_PULLBACK_V1")
    detail_vi = str(
        entry_reason_dict.get("detail_vi")
        or "Tín hiệu nến rút chân xác nhận bối cảnh có xu hướng trên đường EMA200."
    )

    # Tính toán thông số vào lệnh
    target_leverage = int(entry_reason_dict.get("target_leverage") or 5)
    account_risk_pct = Decimal(str(entry_reason_dict.get("account_risk_pct") or "1.0"))
    context_regime = str(entry_reason_dict.get("context_regime") or "TRENDING")
    context_gate_passed = bool(entry_reason_dict.get("context_gate_passed", True))
    rejection_wick_score = float(entry_reason_dict.get("rejection_wick_score") or 78.5)
    breakout_score = float(entry_reason_dict.get("breakout_score") or 82.0)
    risk_reward_ratio = Decimal(str(entry_reason_dict.get("risk_reward_ratio") or "2.0"))

    # Đọc fills nếu có để tính giá vào và SL/TP
    fills = trade.get("fills", [])
    entry_fill_price = Decimal("0")
    if fills and isinstance(fills, list):
        for f in fills:
            if f.get("role") == "ENTRY":
                entry_fill_price = Decimal(str(f.get("price", "0")))
                break

    if entry_fill_price == 0:
        entry_fill_price = Decimal("95000")  # Default placeholder

    if side == "LONG":
        stop_loss_price = entry_fill_price * Decimal("0.985")
        take_profit_price = entry_fill_price * Decimal("1.030")
    else:
        stop_loss_price = entry_fill_price * Decimal("1.015")
        take_profit_price = entry_fill_price * Decimal("0.970")

    # Tính thời gian giữ lệnh
    holding_duration = int(trade.get("holding_duration_seconds") or 3600)

    # Tính toán trượt giá và tỷ lệ PnL
    slippage_bps = Decimal("0")
    if entry_fill_price > 0 and slippage_total > 0:
        slippage_bps = (slippage_total / entry_fill_price) * Decimal("10000")

    pnl_percentage = Decimal("0")
    if initial_risk_budget > 0:
        pnl_percentage = (net_pnl / initial_risk_budget) * Decimal("100")

    # Phân loại logic
    classification = TradeQualityClassification.OPTIMAL
    is_deviant = False
    deviation_reason: str | None = None
    recommended_action = "MAINTAIN_STRATEGY"
    ai_penalty_weight = Decimal("1.0")

    if slippage_total > Decimal("15.0") or slippage_bps > Decimal("15.0"):
        classification = TradeQualityClassification.DEVIANT_SLIPPAGE
        is_deviant = True
        deviation_reason = "Trượt giá thực tế vượt ngưỡng chấp nhận được (> 15 bps), thanh khoản mỏng tại thời điểm vào."
        recommended_action = "ABSTAIN"
        ai_penalty_weight = Decimal("1.8")
    elif exit_reason_code == "STOP_LOSS" and holding_duration < 900 and net_pnl < 0:
        # Bị quét SL trong vòng 15 phút
        classification = TradeQualityClassification.DEVIANT_FALSE_BREAKOUT
        is_deviant = True
        deviation_reason = "Bẫy thanh khoản / Phá vỡ giả: Giá đảo chiều quét Stop Loss trong vòng 15 phút sau khi vào."
        recommended_action = "ABSTAIN"
        ai_penalty_weight = Decimal("2.0")
    elif net_pnl < -(initial_risk_budget * Decimal("1.25")):
        classification = TradeQualityClassification.DEVIANT_EXCESSIVE_LOSS
        is_deviant = True
        deviation_reason = f"Thua lỗ vượt quá 1.25R ngân sách rủi ro (lỗ {net_pnl} USDT so với kế hoạch {initial_risk_budget} USDT)."
        recommended_action = "TIGHTEN_SL"
        ai_penalty_weight = Decimal("2.0")
    elif net_pnl < 0:
        classification = TradeQualityClassification.ACCEPTABLE
        deviation_reason = None
        recommended_action = "MAINTAIN_STRATEGY"
        ai_penalty_weight = Decimal("1.0")
    else:
        classification = TradeQualityClassification.OPTIMAL
        deviation_reason = None
        recommended_action = "REINFORCE"
        ai_penalty_weight = Decimal("0.5")

    entry_analysis = EntryReasoningBreakdown(
        strategy_id=strategy_id,
        composite_signal=side,
        target_leverage=target_leverage,
        account_risk_pct=account_risk_pct,
        context_regime=context_regime,
        context_gate_passed=context_gate_passed,
        rejection_wick_score=rejection_wick_score,
        breakout_score=breakout_score,
        risk_reward_ratio=risk_reward_ratio,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        detail_vi=detail_vi,
    )

    exit_analysis = ExitReasoningBreakdown(
        exit_reason_code=exit_reason_code,
        exit_reason_vi=exit_reason_vi,
        gross_pnl=gross_pnl,
        fee_total=fee_total,
        funding_total=funding_total,
        slippage_total=slippage_total,
        net_pnl=net_pnl,
        pnl_percentage=pnl_percentage,
        holding_duration_seconds=holding_duration,
        slippage_bps=slippage_bps,
    )

    return TradePostMortem(
        trade_id=trade_id,
        bot_id=bot_id,
        symbol=symbol,
        side=side,
        classification=classification,
        is_deviant=is_deviant,
        deviation_reason=deviation_reason,
        entry_analysis=entry_analysis,
        exit_analysis=exit_analysis,
        recommended_counterfactual_action=recommended_action,
        ai_penalty_weight=ai_penalty_weight,
    )
