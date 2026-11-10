"""Cổng đánh giá bối cảnh thị trường (Context Gate) tại tầng L4 theo QUANTSMART.

Tầng L4 đóng vai trò bộ lọc môi trường (Environment Filter):
- Đánh giá các điều kiện tiên quyết: Biến động (ATR), Xu hướng (EMA),
  Phiên giao dịch, Macro Blackout.
- Cơ chế ngắt mạch tức thì (Short-Circuit): Nếu bất kỳ điều kiện nào không đạt,
  trả về BLOCK kèm danh sách lý do và chỉ số đo đạc, ngăn chặn thực thi L5 Trigger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_bot.domain.market.models import OHLCVBar
    from trading_bot.domain.strategy.contracts import DecisionSnapshot
    from trading_bot.domain.strategy_card.models import GraphNode


class ContextGateStatus(StrEnum):
    """Trạng thái đánh giá của Cổng Bối cảnh L4."""

    PASS = "PASS"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class ContextGateResult:
    """Kết quả thẩm định của Cổng Bối cảnh L4."""

    status: ContextGateStatus
    reasons: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


class ContextGateEvaluator:
    """Bộ đánh giá cổng bối cảnh thị trường L4."""

    def evaluate_volatility_gate(
        self,
        current_atr: Decimal,
        min_atr: Decimal | None = None,
        max_atr: Decimal | None = None,
    ) -> ContextGateResult:
        """Kiểm tra điều kiện biên độ biến động (ATR) nằm trong vùng an toàn."""
        reasons: list[str] = []
        metrics: dict[str, Any] = {"current_atr": float(current_atr)}

        if min_atr is not None and current_atr < min_atr:
            reasons.append(
                f"ATR {current_atr} < min_atr {min_atr}: Biến động thị trường "
                "quá thấp, nguy cơ đi ngang (chop) rủi ro."
            )
        if max_atr is not None and current_atr > max_atr:
            reasons.append(
                f"ATR {current_atr} > max_atr {max_atr}: Biến động thị trường "
                "quá cao, vượt ngưỡng kiểm soát rủi ro an toàn."
            )

        if reasons:
            return ContextGateResult(
                status=ContextGateStatus.BLOCK,
                reasons=tuple(reasons),
                metrics=metrics,
            )

        return ContextGateResult(
            status=ContextGateStatus.PASS,
            reasons=(),
            metrics=metrics,
        )

    def evaluate_trend_gate(
        self,
        current_price: Decimal,
        trend_ema: Decimal,
        expected_trend: str = "BULLISH",
    ) -> ContextGateResult:
        """Kiểm tra xu hướng giá có phù hợp với chiều kỳ vọng của chiến lược."""
        metrics: dict[str, Any] = {
            "current_price": float(current_price),
            "trend_ema": float(trend_ema),
            "expected_trend": expected_trend,
        }

        if expected_trend.upper() in {"BULLISH", "LONG"} and current_price < trend_ema:
            return ContextGateResult(
                status=ContextGateStatus.BLOCK,
                reasons=(
                    f"Giá hiện tại {current_price} < EMA {trend_ema}: "
                    "Xu hướng giảm ngược chiều vị thế mua (Bullish) kỳ vọng.",
                ),
                metrics=metrics,
            )

        if expected_trend.upper() in {"BEARISH", "SHORT"} and current_price > trend_ema:
            return ContextGateResult(
                status=ContextGateStatus.BLOCK,
                reasons=(
                    f"Giá hiện tại {current_price} > EMA {trend_ema}: "
                    "Xu hướng tăng ngược chiều vị thế bán (Bearish) kỳ vọng.",
                ),
                metrics=metrics,
            )

        return ContextGateResult(
            status=ContextGateStatus.PASS,
            reasons=(),
            metrics=metrics,
        )

    def evaluate_gates(
        self,
        gate_nodes: list[GraphNode],
        node_outputs: dict[str, Any],
        snapshot: DecisionSnapshot,
        latest_bar: OHLCVBar | None = None,
    ) -> ContextGateResult:
        """Đánh giá toàn diện tất cả các node thuộc tầng L4 Context Gate trong đồ thị."""
        # 1. Kiểm tra Macro Blackout từ snapshot trước tiên
        if getattr(snapshot, "macro_blackout", False):
            return ContextGateResult(
                status=ContextGateStatus.BLOCK,
                reasons=(
                    "MACRO_BLACKOUT: Đang trong thời gian cấm giao dịch do tin tức "
                    "vĩ mô lớn hoặc sự kiện thị trường cực đoan.",
                ),
                metrics={"macro_blackout": True},
            )

        all_reasons: list[str] = []
        all_metrics: dict[str, Any] = {}

        # 2. Duyệt qua từng gate node được cấu hình trong đồ thị
        for node in gate_nodes:
            op = node.operation.lower()
            params = node.parameters

            if "volatility" in op or "atr" in op:
                min_atr = Decimal(str(params["min_atr"])) if "min_atr" in params else None
                max_atr = Decimal(str(params["max_atr"])) if "max_atr" in params else None
                # Tìm giá trị ATR: ưu tiên từ node_outputs (L2), sau đó là biên độ nến gần nhất
                atr_val = None
                for out_key, val in node_outputs.items():
                    if "atr" in out_key.lower() and isinstance(val, (int, float, Decimal)):
                        atr_val = Decimal(str(val))
                        break

                if atr_val is None and latest_bar is not None:
                    atr_val = latest_bar.high - latest_bar.low

                if atr_val is None:
                    atr_val = Decimal(str(params.get("default_atr", "10.0")))

                res = self.evaluate_volatility_gate(
                    current_atr=atr_val, min_atr=min_atr, max_atr=max_atr
                )
                all_metrics.update(res.metrics)
                if res.status == ContextGateStatus.BLOCK:
                    all_reasons.extend(res.reasons)

            elif "trend" in op or "ema" in op:
                expected_trend = str(params.get("trend", "BULLISH"))
                price = latest_bar.close if latest_bar is not None else Decimal("100.0")
                ema_val = None
                for out_key, val in node_outputs.items():
                    if "ema" in out_key.lower() and isinstance(val, (int, float, Decimal)):
                        ema_val = Decimal(str(val))
                        break
                if ema_val is None:
                    ema_val = Decimal(str(params.get("trend_ema", str(price))))

                res = self.evaluate_trend_gate(
                    current_price=price,
                    trend_ema=ema_val,
                    expected_trend=expected_trend,
                )
                all_metrics.update(res.metrics)
                if res.status == ContextGateStatus.BLOCK:
                    all_reasons.extend(res.reasons)

            else:
                # Kiểm tra cờ boolean mặc định nếu có node đầu vào
                for inp in node.inputs:
                    inp_val = node_outputs.get(inp.name)
                    if inp_val is False:
                        all_reasons.append(
                            f"Gate '{node.node_id}' nhận cờ False từ cổng vào '{inp.name}'."
                        )

        if all_reasons:
            return ContextGateResult(
                status=ContextGateStatus.BLOCK,
                reasons=tuple(all_reasons),
                metrics=all_metrics,
            )

        return ContextGateResult(
            status=ContextGateStatus.PASS,
            reasons=(),
            metrics=all_metrics,
        )
