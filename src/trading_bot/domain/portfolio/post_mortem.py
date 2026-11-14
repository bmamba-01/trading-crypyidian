"""Domain model cho việc mổ xẻ sau giao dịch (Trade Post-Mortem) và phân loại chất lượng thực thi.

Hỗ trợ phân tích nguyên nhân vào/ra lệnh, rủi ro, trượt giá, và đánh dấu
các ca sai lệch (Deviant trades) để người rà soát gắn nhãn cho huấn luyện AI.

Nguyên tắc: hậu kiểm chỉ dùng dữ liệu có trong giao dịch được truyền vào (ledger PAPER
hoặc nguồn đã ghi rõ). Trường không có nguồn là `None`; quy tắc thiếu dữ liệu đầu vào được
liệt kê ở `unevaluated_checks` thay vì chạy trên giá trị mặc định.
"""
# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

# Ngưỡng vận hành khởi điểm của hậu kiểm. Đây là lựa chọn nội bộ, chưa được nghiên cứu hay
# backtest xác nhận; đổi ngưỡng phải đi kèm cập nhật tài liệu và test.
# Trượt giá thực tế bị coi là sai lệch khi vượt bội số này của trượt giá kế hoạch đã dự trù
# (vào + ra). Mô hình PAPER luôn tính chi phí bất lợi nên so với ngưỡng bps tuyệt đối sẽ gắn
# nhãn sai lệch cho mọi lệnh; so với kế hoạch mới phản ánh thực thi tệ hơn dự kiến.
SLIPPAGE_DEVIANT_PLAN_MULTIPLE = Decimal("1.5")
FALSE_BREAKOUT_MAX_HOLDING_SECONDS = 900
EXCESSIVE_LOSS_R_MULTIPLE = Decimal("1.25")

# Tên các quy tắc phát hiện sai lệch; dùng để báo quy tắc nào chưa kiểm được.
CHECK_SLIPPAGE = "SLIPPAGE"
CHECK_FALSE_BREAKOUT = "FALSE_BREAKOUT"
CHECK_EXCESSIVE_LOSS = "EXCESSIVE_LOSS"

_BPS = Decimal("10000")

EXIT_REASON_LABELS_VI = {
    "TAKE_PROFIT": "Chốt lời theo mục tiêu",
    "STOP_LOSS": "Cắt lỗ bảo vệ vốn",
    "TIME_STOP": "Đóng theo thời gian nắm giữ",
    "OPPOSING_SIGNAL": "Tín hiệu kỹ thuật đảo chiều",
    "CHANNEL_EXIT": "Thoát khi giá chạm biên ngược của kênh Donchian",
    "FEATURE_EXIT": "Thoát khi chỉ báo hồi về ngưỡng đã định",
    "FLATTEN_OPERATION": "Đóng theo thao tác quản trị khẩn cấp",
}


class TradeQualityClassification(StrEnum):
    """Phân loại chất lượng giao dịch sau khi kết thúc."""

    OPTIMAL = "OPTIMAL"  # Không lỗ và không vi phạm quy tắc nào kiểm được
    ACCEPTABLE = "ACCEPTABLE"  # Lỗ nhưng không vi phạm quy tắc nào kiểm được
    DEVIANT_SLIPPAGE = "DEVIANT_SLIPPAGE"  # Sai lệch do trượt giá nghiêm trọng trên sổ lệnh
    DEVIANT_FALSE_BREAKOUT = "DEVIANT_FALSE_BREAKOUT"  # Phá vỡ giả, đảo chiều tức thì quét SL
    DEVIANT_CONTEXT_MISMATCH = (
        "DEVIANT_CONTEXT_MISMATCH"  # Bối cảnh thị trường không phù hợp (Chop/Sideway)
    )
    DEVIANT_EXCESSIVE_LOSS = (
        "DEVIANT_EXCESSIVE_LOSS"  # Thua lỗ vượt mức kế hoạch rủi ro ban đầu (> 1.25R)
    )
    UNVERIFIED = "UNVERIFIED"  # Thiếu dữ liệu: không kết luận được chất lượng thực thi


@dataclass(frozen=True, slots=True)
class EntryReasoningBreakdown:
    """Bóc tách lý do vào vị thế; mọi trường không có nguồn đều là `None`.

    Ledger PAPER hiện chỉ lưu `strategy_id`, `reason_code`, `detail_vi`, `reference_price` và
    `signal_at`. Các trường tầng L4–L6 (bối cảnh, điểm kích hoạt, R:R, SL/TP, đòn bẩy) chỉ có
    giá trị khi nguồn phát sinh candidate thật sự ghi chúng vào `entry_reason`.
    """

    composite_signal: str  # LONG hoặc SHORT, lấy từ hướng của giao dịch
    strategy_id: str | None = None
    reason_code: str | None = None
    detail_vi: str | None = None
    reference_price: Decimal | None = None
    signal_at: str | None = None
    entry_price: Decimal | None = None  # Giá bình quân gia quyền của các fill vào lệnh
    entry_quantity: Decimal | None = None
    target_leverage: int | None = None
    account_risk_pct: Decimal | None = None
    context_regime: str | None = None  # TRENDING, CHOP, VOLATILE
    context_gate_passed: bool | None = None
    rejection_wick_score: float | None = None  # Điểm nến rút chân (0.0 -> 100.0)
    breakout_score: float | None = None  # Điểm phá vỡ đỉnh/đáy (0.0 -> 100.0)
    risk_reward_ratio: Decimal | None = None
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    planned_risk_amount: Decimal | None = None  # Ngân sách rủi ro của kế hoạch đã duyệt

    def as_dict(self) -> dict[str, Any]:
        return {
            "composite_signal": self.composite_signal,
            "strategy_id": self.strategy_id,
            "reason_code": self.reason_code,
            "detail_vi": self.detail_vi,
            "reference_price": _text(self.reference_price),
            "signal_at": self.signal_at,
            "entry_price": _text(self.entry_price),
            "entry_quantity": _text(self.entry_quantity),
            "target_leverage": self.target_leverage,
            "account_risk_pct": _text(self.account_risk_pct),
            "context_regime": self.context_regime,
            "context_gate_passed": self.context_gate_passed,
            "rejection_wick_score": (
                round(self.rejection_wick_score, 1)
                if self.rejection_wick_score is not None
                else None
            ),
            "breakout_score": (
                round(self.breakout_score, 1) if self.breakout_score is not None else None
            ),
            "risk_reward_ratio": _text(self.risk_reward_ratio),
            "stop_loss_price": _text(self.stop_loss_price),
            "take_profit_price": _text(self.take_profit_price),
            "planned_risk_amount": _text(self.planned_risk_amount),
        }


@dataclass(frozen=True, slots=True)
class ExitReasoningBreakdown:
    """Bóc tách lý do thoát vị thế và chi phí thực tế; thiếu nguồn thì là `None`."""

    exit_reason_code: str | None  # TAKE_PROFIT, STOP_LOSS, TIME_STOP, OPPOSING_SIGNAL, FLATTEN
    exit_reason_vi: str | None  # Giải thích tiếng Việt
    gross_pnl: Decimal | None
    fee_total: Decimal | None
    funding_total: Decimal | None
    slippage_total: Decimal | None
    net_pnl: Decimal | None
    net_pnl_r_multiple: Decimal | None  # PnL ròng / ngân sách rủi ro kế hoạch
    holding_duration_seconds: int | None
    slippage_bps: Decimal | None  # Trượt giá / notional vào lệnh, tính bằng điểm cơ bản
    expected_slippage: Decimal | None = None  # Trượt giá vào + ra mà kế hoạch đã dự trù

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_reason_code": self.exit_reason_code,
            "exit_reason_vi": self.exit_reason_vi,
            "gross_pnl": _text(self.gross_pnl),
            "fee_total": _text(self.fee_total),
            "funding_total": _text(self.funding_total),
            "slippage_total": _text(self.slippage_total),
            "net_pnl": _text(self.net_pnl),
            "net_pnl_r_multiple": _text(self.net_pnl_r_multiple),
            "holding_duration_seconds": self.holding_duration_seconds,
            "slippage_bps": _text(self.slippage_bps),
            "expected_slippage": _text(self.expected_slippage),
        }


@dataclass(frozen=True, slots=True)
class TradePostMortem:
    """Bản tổng hợp mổ xẻ sau giao dịch kèm phân loại và đề xuất cho người rà soát."""

    trade_id: UUID
    bot_id: UUID
    symbol: str
    side: str
    classification: TradeQualityClassification
    is_deviant: bool
    deviation_reason: str | None
    entry_analysis: EntryReasoningBreakdown
    exit_analysis: ExitReasoningBreakdown
    # ABSTAIN, TIGHTEN_SL, MAINTAIN_STRATEGY, REINFORCE hoặc REVIEW_REQUIRED
    recommended_counterfactual_action: str
    # Hệ số phạt đề xuất; `None` khi chưa đủ dữ liệu để đề xuất.
    ai_penalty_weight: Decimal | None
    # Quy tắc phát hiện sai lệch không chạy được vì thiếu dữ liệu đầu vào.
    unevaluated_checks: tuple[str, ...] = ()

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
            "ai_penalty_weight": _text(self.ai_penalty_weight),
            "unevaluated_checks": list(self.unevaluated_checks),
        }


def _text(value: Decimal | None) -> str | None:
    """Chuỗi thập phân không ký hiệu khoa học; giữ `None` để giao diện ghi "chưa có dữ liệu"."""
    return format(value, "f") if value is not None else None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _positive_decimal(value: object) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_text(trade: Mapping[str, Any], key: str) -> str:
    text = _optional_text(trade.get(key))
    if text is None:
        raise ValueError(f"Giao dịch thiếu trường bắt buộc `{key}` để hậu kiểm.")
    return text


def _required_uuid(trade: Mapping[str, Any], key: str) -> UUID:
    try:
        return UUID(_required_text(trade, key))
    except ValueError as error:
        raise ValueError(f"Giao dịch thiếu `{key}` hợp lệ để hậu kiểm.") from error


def _score(value: object) -> float | None:
    number = _decimal(value)
    return float(number) if number is not None else None


def _leverage(value: object) -> int | None:
    number = _positive_decimal(value)
    return int(number) if number is not None and number == number.to_integral_value() else None


def _entry_fill_summary(fills: object) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Trả (khối lượng, giá bình quân gia quyền, notional) của các fill vào lệnh có đủ số liệu."""
    quantity = Decimal("0")
    notional = Decimal("0")
    if isinstance(fills, list):
        for fill in fills:
            if not isinstance(fill, Mapping) or fill.get("role") != "ENTRY":
                continue
            fill_quantity = _positive_decimal(fill.get("quantity"))
            fill_price = _positive_decimal(fill.get("price"))
            if fill_quantity is None or fill_price is None:
                continue
            quantity += fill_quantity
            notional += fill_quantity * fill_price
    if quantity <= 0:
        return None, None, None
    return quantity, notional / quantity, notional


def _first_entry_price(fills: object) -> Decimal | None:
    """Giá của fill vào lệnh đầu tiên khi ledger không kèm khối lượng."""
    if isinstance(fills, list):
        for fill in fills:
            if isinstance(fill, Mapping) and fill.get("role") == "ENTRY":
                return _positive_decimal(fill.get("price"))
    return None


def _holding_seconds(trade: Mapping[str, Any]) -> int | None:
    """Thời gian giữ lệnh: ưu tiên trường đã ghi, sau đó tính từ mốc mở/đóng của ledger."""
    explicit = _decimal(trade.get("holding_duration_seconds"))
    if explicit is not None and explicit >= 0:
        return int(explicit)
    try:
        opened_at = datetime.fromisoformat(str(trade["opened_at"]))
        closed_at = datetime.fromisoformat(str(trade["closed_at"]))
        seconds = int((closed_at - opened_at).total_seconds())
    except KeyError, TypeError, ValueError:
        return None
    return seconds if seconds >= 0 else None


def evaluate_trade_post_mortem(
    trade: Mapping[str, Any],
    *,
    planned_risk_amount: Decimal | None = None,
    plan: Mapping[str, Any] | None = None,
) -> TradePostMortem:
    """Đánh giá một giao dịch đã đóng để phân loại chất lượng và đề xuất cho người rà soát.

    Quy tắc phát hiện sai lệch, theo thứ tự; quy tắc thiếu dữ liệu đầu vào bị bỏ qua và được
    ghi trong `unevaluated_checks`:

    1. `SLIPPAGE`: trượt giá thực tế > 1,5 lần trượt giá kế hoạch đã dự trù cho cả vào và ra
       (2 × `entry_slippage_cost`). Cần kế hoạch đã duyệt của lệnh; không có ngưỡng tuyệt đối.
    2. `FALSE_BREAKOUT`: đóng `STOP_LOSS`, lỗ ròng, giữ lệnh dưới 15 phút. Cần lý do đóng và
       thời gian giữ lệnh.
    3. `EXCESSIVE_LOSS`: lỗ ròng vượt 1,25 lần `planned_risk_amount`. Cần ngân sách rủi ro kế
       hoạch do nơi gọi cung cấp từ nguồn thật; không có giá trị mặc định.

    `plan` là kế hoạch đã được risk gate duyệt của chính trade này (`trade_plan.payload`): giá
    stop/mục tiêu, đòn bẩy và ngân sách rủi ro thật. Khi có, nó là nguồn cho SL/TP, đòn bẩy, R:R
    và cho `planned_risk_amount` nếu nơi gọi không truyền riêng: ưu tiên `estimated_loss` (1R
    thật của khối lượng đã duyệt), thiếu mới dùng trần `risk_budget`.

    Không vi phạm quy tắc nào kiểm được: lỗ ròng → `ACCEPTABLE`, còn lại → `OPTIMAL`. Thiếu PnL
    ròng hoặc không quy tắc nào kiểm được → `UNVERIFIED`.
    """
    trade_id = _required_uuid(trade, "trade_id")
    bot_id = _required_uuid(trade, "bot_id")
    symbol = _required_text(trade, "symbol")
    side = _required_text(trade, "side").upper()
    quote_asset = _optional_text(trade.get("quote_asset"))

    gross_pnl = _decimal(trade.get("gross_pnl"))
    fee_total = _decimal(trade.get("fee_total"))
    funding_total = _decimal(trade.get("funding_total"))
    slippage_total = _decimal(trade.get("slippage_total"))
    net_pnl = _decimal(trade.get("net_pnl"))

    exit_reason_code = _optional_text(trade.get("exit_reason"))
    exit_reason_vi = (
        EXIT_REASON_LABELS_VI.get(exit_reason_code, exit_reason_code)
        if exit_reason_code is not None
        else None
    )

    raw_entry_reason = trade.get("entry_reason")
    entry_reason: Mapping[str, Any] = (
        raw_entry_reason if isinstance(raw_entry_reason, Mapping) else {}
    )
    gate_passed = entry_reason.get("context_gate_passed")

    fills = trade.get("fills")
    entry_quantity, entry_price, entry_notional = _entry_fill_summary(fills)
    if entry_price is None:
        entry_price = _first_entry_price(fills)

    holding_duration = _holding_seconds(trade)
    plan_values: Mapping[str, Any] = plan if isinstance(plan, Mapping) else {}
    plan_entry = _positive_decimal(plan_values.get("entry_price"))
    plan_stop = _positive_decimal(plan_values.get("stop_price"))
    plan_target = _positive_decimal(plan_values.get("target_price"))
    plan_reward_risk = (
        abs(plan_target - plan_entry) / abs(plan_entry - plan_stop)
        if plan_entry is not None
        and plan_stop is not None
        and plan_target is not None
        and plan_entry != plan_stop
        else None
    )
    risk_amount = (
        planned_risk_amount
        if planned_risk_amount and planned_risk_amount > 0
        # 1R thật là mức lỗ ước tính của đúng khối lượng đã duyệt (gồm phí, funding, trượt giá).
        # `risk_budget` chỉ là trần ngân sách: làm tròn khối lượng xuống khiến 1R thật nhỏ hơn,
        # nên chia PnL cho trần sẽ báo R đẹp hơn thực tế.
        else _positive_decimal(plan_values.get("estimated_loss"))
        or _positive_decimal(plan_values.get("risk_budget"))
    )

    slippage_bps = (
        slippage_total / entry_notional * _BPS
        if slippage_total is not None and entry_notional is not None
        else None
    )
    net_pnl_r_multiple = (
        net_pnl / risk_amount if net_pnl is not None and risk_amount is not None else None
    )

    planned_entry_slippage = _decimal(plan_values.get("entry_slippage_cost"))
    expected_slippage = (
        planned_entry_slippage * 2
        if planned_entry_slippage is not None and planned_entry_slippage >= 0
        else None
    )
    slippage_checkable = slippage_total is not None and expected_slippage is not None

    unevaluated: list[str] = []
    if not slippage_checkable:
        unevaluated.append(CHECK_SLIPPAGE)
    if exit_reason_code is None or holding_duration is None or net_pnl is None:
        unevaluated.append(CHECK_FALSE_BREAKOUT)
    if net_pnl_r_multiple is None:
        unevaluated.append(CHECK_EXCESSIVE_LOSS)
    all_checks_unevaluated = len(unevaluated) == 3

    classification: TradeQualityClassification
    is_deviant = False
    deviation_reason: str | None = None
    recommended_action: str
    ai_penalty_weight: Decimal | None

    if (
        slippage_total is not None
        and expected_slippage is not None
        and slippage_total > expected_slippage * SLIPPAGE_DEVIANT_PLAN_MULTIPLE
    ):
        classification = TradeQualityClassification.DEVIANT_SLIPPAGE
        is_deviant = True
        unit = f" {quote_asset}" if quote_asset else ""
        deviation_reason = (
            f"Trượt giá thực tế {_text(slippage_total)}{unit} vượt "
            f"{SLIPPAGE_DEVIANT_PLAN_MULTIPLE} lần mức kế hoạch dự trù "
            f"({_text(expected_slippage)}{unit} cho cả vào và ra)."
        )
        recommended_action = "ABSTAIN"
        ai_penalty_weight = Decimal("1.8")
    elif (
        exit_reason_code == "STOP_LOSS"
        and holding_duration is not None
        and holding_duration < FALSE_BREAKOUT_MAX_HOLDING_SECONDS
        and net_pnl is not None
        and net_pnl < 0
    ):
        classification = TradeQualityClassification.DEVIANT_FALSE_BREAKOUT
        is_deviant = True
        deviation_reason = (
            "Bẫy thanh khoản / Phá vỡ giả: giá đảo chiều quét Stop Loss sau "
            f"{holding_duration} giây, dưới ngưỡng {FALSE_BREAKOUT_MAX_HOLDING_SECONDS} giây."
        )
        recommended_action = "ABSTAIN"
        ai_penalty_weight = Decimal("2.0")
    elif net_pnl_r_multiple is not None and net_pnl_r_multiple < -EXCESSIVE_LOSS_R_MULTIPLE:
        classification = TradeQualityClassification.DEVIANT_EXCESSIVE_LOSS
        is_deviant = True
        unit = f" {quote_asset}" if quote_asset else ""
        deviation_reason = (
            f"Thua lỗ vượt quá {EXCESSIVE_LOSS_R_MULTIPLE}R ngân sách rủi ro "
            f"(lỗ {_text(net_pnl)}{unit} so với kế hoạch {_text(risk_amount)}{unit})."
        )
        recommended_action = "TIGHTEN_SL"
        ai_penalty_weight = Decimal("2.0")
    elif net_pnl is None or all_checks_unevaluated:
        classification = TradeQualityClassification.UNVERIFIED
        recommended_action = "REVIEW_REQUIRED"
        ai_penalty_weight = None
    elif net_pnl < 0:
        classification = TradeQualityClassification.ACCEPTABLE
        recommended_action = "MAINTAIN_STRATEGY"
        ai_penalty_weight = Decimal("1.0")
    else:
        classification = TradeQualityClassification.OPTIMAL
        recommended_action = "REINFORCE"
        ai_penalty_weight = Decimal("0.5")

    entry_analysis = EntryReasoningBreakdown(
        composite_signal=side,
        strategy_id=_optional_text(entry_reason.get("strategy_id")),
        reason_code=_optional_text(entry_reason.get("reason_code")),
        detail_vi=_optional_text(entry_reason.get("detail_vi")),
        reference_price=_positive_decimal(entry_reason.get("reference_price")),
        signal_at=_optional_text(entry_reason.get("signal_at")),
        entry_price=entry_price,
        entry_quantity=entry_quantity,
        target_leverage=_leverage(plan_values.get("leverage"))
        or _leverage(entry_reason.get("target_leverage")),
        account_risk_pct=_positive_decimal(entry_reason.get("account_risk_pct")),
        context_regime=_optional_text(entry_reason.get("context_regime")),
        context_gate_passed=gate_passed if isinstance(gate_passed, bool) else None,
        rejection_wick_score=_score(entry_reason.get("rejection_wick_score")),
        breakout_score=_score(entry_reason.get("breakout_score")),
        risk_reward_ratio=plan_reward_risk
        or _positive_decimal(entry_reason.get("risk_reward_ratio")),
        stop_loss_price=plan_stop or _positive_decimal(entry_reason.get("stop_loss_price")),
        take_profit_price=plan_target or _positive_decimal(entry_reason.get("take_profit_price")),
        planned_risk_amount=risk_amount,
    )

    exit_analysis = ExitReasoningBreakdown(
        exit_reason_code=exit_reason_code,
        exit_reason_vi=exit_reason_vi,
        gross_pnl=gross_pnl,
        fee_total=fee_total,
        funding_total=funding_total,
        slippage_total=slippage_total,
        net_pnl=net_pnl,
        net_pnl_r_multiple=net_pnl_r_multiple,
        holding_duration_seconds=holding_duration,
        slippage_bps=slippage_bps,
        expected_slippage=expected_slippage,
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
        unevaluated_checks=tuple(unevaluated),
    )
