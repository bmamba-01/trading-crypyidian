"""Cổng đánh giá bối cảnh thị trường (Context Gate) tại tầng L4 theo QUANTSMART.

Tầng L4 đóng vai trò bộ lọc môi trường (Environment Filter):
- Đánh giá các điều kiện tiên quyết: Biến động (ATR), Xu hướng (EMA),
  Phiên giao dịch, Macro Blackout.
- Cơ chế ngắt mạch tức thì (Short-Circuit): Nếu bất kỳ điều kiện nào không đạt,
  trả về BLOCK kèm danh sách lý do và chỉ số đo đạc, ngăn chặn thực thi L5 Trigger.
"""

from __future__ import annotations

from collections.abc import Mapping
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

        if expected_trend.upper() in {"NEUTRAL", "BIDIRECTIONAL"}:
            return ContextGateResult(
                status=ContextGateStatus.PASS,
                reasons=(),
                metrics=metrics,
            )

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

    def evaluate_rv_regime_gate(
        self,
        *,
        current_rv: Decimal,
        min_rv: Decimal | None = None,
        max_rv: Decimal | None = None,
    ) -> ContextGateResult:
        """Kiểm tra độ biến động thực tế (RV) nằm trong hàng rào an toàn hai đầu."""
        reasons: list[str] = []
        metrics: dict[str, Any] = {"current_rv": float(current_rv)}
        if min_rv is not None and current_rv < min_rv:
            reasons.append(f"RV {current_rv} < min_rv {min_rv}: Chuỗi giá phẳng giả tạo.")
        if max_rv is not None and current_rv > max_rv:
            reasons.append(f"RV {current_rv} > max_rv {max_rv}: Biến động sốc/crash.")
        if reasons:
            return ContextGateResult(
                status=ContextGateStatus.BLOCK, reasons=tuple(reasons), metrics=metrics
            )
        return ContextGateResult(status=ContextGateStatus.PASS, reasons=(), metrics=metrics)

    def evaluate_adx_regime_gate(
        self,
        *,
        current_adx: Decimal,
        min_adx: Decimal | None = None,
        max_adx: Decimal | None = None,
    ) -> ContextGateResult:
        """Kiểm tra chế độ xu hướng / đi ngang theo chỉ số ADX14."""
        reasons: list[str] = []
        metrics: dict[str, Any] = {"current_adx": float(current_adx)}
        if min_adx is not None and current_adx < min_adx:
            reasons.append(f"ADX {current_adx} < min_adx {min_adx}: Xu hướng quá yếu.")
        if max_adx is not None and current_adx >= max_adx:
            reasons.append(f"ADX {current_adx} >= max_adx {max_adx}: Đang có xu hướng mạnh.")
        if reasons:
            return ContextGateResult(
                status=ContextGateStatus.BLOCK, reasons=tuple(reasons), metrics=metrics
            )
        return ContextGateResult(status=ContextGateStatus.PASS, reasons=(), metrics=metrics)

    def _resolve_atr(
        self,
        node_outputs: Mapping[str, Any],
        latest_bar: OHLCVBar | None,
        params: Mapping[str, Any],
    ) -> Decimal:
        atr_val: Decimal | None = None
        for out_key, val in node_outputs.items():
            if "atr" in out_key.lower() and isinstance(val, (int, float, Decimal)):
                atr_val = Decimal(str(val))
                break
        if atr_val is None and latest_bar is not None:
            atr_val = latest_bar.high - latest_bar.low
        if atr_val is None:
            atr_val = Decimal(str(params.get("default_atr", "10.0")))
        return atr_val

    def evaluate_gates(
        self,
        gate_nodes: list[GraphNode],
        node_outputs: dict[str, Any],
        snapshot: DecisionSnapshot,
        latest_bar: OHLCVBar | None = None,
        indicator_sources: Mapping[str, str] | None = None,
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
        sources = indicator_sources or {}

        def _is_preview_warmup(key_fragment: str) -> bool:
            return any(
                key_fragment in node_id.lower() and src == "insufficient_history"
                for node_id, src in sources.items()
            )

        # 2. Duyệt qua từng gate node được cấu hình trong đồ thị
        for node in gate_nodes:
            op = node.operation.lower()
            params = node.parameters
            checked_vol = False

            if "volatility" in op or "atr" in op:
                checked_vol = True
                min_atr = Decimal(str(params["min_atr"])) if "min_atr" in params else None
                max_atr = Decimal(str(params["max_atr"])) if "max_atr" in params else None
                atr_val = self._resolve_atr(node_outputs, latest_bar, params)

                res = self.evaluate_volatility_gate(
                    current_atr=atr_val, min_atr=min_atr, max_atr=max_atr
                )
                all_metrics.update(res.metrics)
                if res.status == ContextGateStatus.BLOCK:
                    all_reasons.extend(res.reasons)

            elif "rv_regime" in op or "realized_volatility" in op:
                min_rv = Decimal(str(params["min_rv"])) if "min_rv" in params else None
                max_rv = Decimal(str(params["max_rv"])) if "max_rv" in params else None
                rv_val: Decimal | None = None
                for out_key, val in node_outputs.items():
                    if "rv" in out_key.lower() and isinstance(val, (int, float, Decimal)):
                        rv_val = Decimal(str(val))
                        break
                if rv_val is not None and not _is_preview_warmup("rv"):
                    res = self.evaluate_rv_regime_gate(
                        current_rv=rv_val, min_rv=min_rv, max_rv=max_rv
                    )
                    all_metrics.update(res.metrics)
                    if res.status == ContextGateStatus.BLOCK:
                        all_reasons.extend(res.reasons)

            elif "adx" in op:
                min_adx = Decimal(str(params["min_adx"])) if "min_adx" in params else None
                max_adx = Decimal(str(params["max_adx"])) if "max_adx" in params else None
                adx_val: Decimal | None = None
                for out_key, val in node_outputs.items():
                    if "adx" in out_key.lower() and isinstance(val, (int, float, Decimal)):
                        adx_val = Decimal(str(val))
                        break
                if adx_val is not None and not _is_preview_warmup("adx"):
                    res = self.evaluate_adx_regime_gate(
                        current_adx=adx_val, min_adx=min_adx, max_adx=max_adx
                    )
                    all_metrics.update(res.metrics)
                    if res.status == ContextGateStatus.BLOCK:
                        all_reasons.extend(res.reasons)

            elif "squeeze" in op:
                squeeze_val: Decimal | None = None
                for out_key, val in node_outputs.items():
                    if "squeeze" in out_key.lower() and isinstance(val, (int, float, Decimal)):
                        squeeze_val = Decimal(str(val))
                        break
                if squeeze_val is not None:
                    all_metrics["squeeze_score"] = float(squeeze_val)

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

                if not _is_preview_warmup("ema"):
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

            # Hỗ trợ kiểm tra min_atr/max_atr đồng nhất trên mọi cổng L4 khi được cấu hình
            if not checked_vol and ("min_atr" in params or "max_atr" in params):
                min_atr = Decimal(str(params["min_atr"])) if "min_atr" in params else None
                max_atr = Decimal(str(params["max_atr"])) if "max_atr" in params else None
                atr_val = self._resolve_atr(node_outputs, latest_bar, params)
                vol_res = self.evaluate_volatility_gate(
                    current_atr=atr_val, min_atr=min_atr, max_atr=max_atr
                )
                all_metrics.update(vol_res.metrics)
                if vol_res.status == ContextGateStatus.BLOCK:
                    all_reasons.extend(vol_res.reasons)

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
