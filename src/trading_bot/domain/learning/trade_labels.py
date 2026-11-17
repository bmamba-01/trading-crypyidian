"""Nhãn kết quả lệnh cho dataset học (WP-06/AI-5).

Hai phiên bản nhãn, đều 3 lớp {-1, 0, 1}:

- ``net-pnl-sign-v1``: dấu của net PnL (lớp 0 chỉ khi PnL đúng bằng 0).
- ``net-r-band-v1`` (AI-5, owner chọn "Small R band" 17/11/2026): R ròng = net PnL / 1R; lớp 0
  khi |R ròng| ≤ 0,1R ("hòa vốn theo R"), ngoài dải lấy dấu. Cần ``risk_amount`` (1R) dương.
"""

from __future__ import annotations

from decimal import Decimal

from trading_bot.domain.learning.evaluation import EvaluationTrade

NET_PNL_SIGN_V1 = "net-pnl-sign-v1"
NET_R_BAND_V1 = "net-r-band-v1"
SUPPORTED_LABEL_VERSIONS = (NET_PNL_SIGN_V1, NET_R_BAND_V1)
R_BAND_HALF_WIDTH = Decimal("0.1")


def trade_label_value(trade: EvaluationTrade, label_version: str) -> int:
    """Giá trị nhãn của lệnh theo phiên bản; lỗi nếu thiếu dữ liệu bắt buộc."""
    net = trade.net_pnl
    if label_version == NET_PNL_SIGN_V1:
        return (net > 0) - (net < 0)
    if label_version == NET_R_BAND_V1:
        if trade.risk_amount is None:
            raise ValueError("Nhãn dải R cần risk_amount (1R) của lệnh.")
        r_multiple = net / trade.risk_amount
        if abs(r_multiple) <= R_BAND_HALF_WIDTH:
            return 0
        return 1 if r_multiple > 0 else -1
    raise ValueError(f"Label version {label_version} không được hỗ trợ.")


__all__ = [
    "NET_PNL_SIGN_V1",
    "NET_R_BAND_V1",
    "R_BAND_HALF_WIDTH",
    "SUPPORTED_LABEL_VERSIONS",
    "trade_label_value",
]
