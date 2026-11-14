"""Dòng tiền funding của hợp đồng perpetual USDⓈ-M cho sổ PAPER.

Công thức theo tài liệu chính thức của Binance (kiểm ngày 2026-11-14):

- Funding = giá trị danh nghĩa × funding rate; giá trị danh nghĩa = mark price × khối lượng.
  Rate dương: bên long trả bên short; rate âm: bên short trả bên long.
  https://www.binance.com/en/support/faq/introduction-to-binance-futures-funding-rates-360033525031
- Chỉ vị thế còn mở tại mốc funding mới trả/nhận. Chu kỳ mặc định 8 giờ (00:00, 08:00,
  16:00 UTC) nhưng có symbol dùng chu kỳ khác, nên mốc và rate phải lấy từ dữ liệu
  `GET /fapi/v1/fundingRate` (trường `fundingTime`, `fundingRate`, `markPrice`), không tự suy
  từ đồng hồ.
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History

Module chỉ tính toán thuần; không gọi sàn và không tự quyết vị thế nào đủ điều kiện.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    """Một kỳ funding đã công bố của symbol, lấy nguyên từ dữ liệu thị trường."""

    symbol: str
    funding_time: datetime
    funding_rate: Decimal
    mark_price: Decimal

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("Funding settlement cần symbol.")
        if self.funding_time.tzinfo is None or self.funding_time.utcoffset() is None:
            raise ValueError("funding_time bắt buộc có timezone.")
        if not isinstance(self.funding_rate, Decimal) or not self.funding_rate.is_finite():
            raise ValueError("funding_rate phải là Decimal hữu hạn.")
        if (
            not isinstance(self.mark_price, Decimal)
            or not self.mark_price.is_finite()
            or self.mark_price <= ZERO
        ):
            raise ValueError("mark_price phải là Decimal hữu hạn và dương.")

    @property
    def event_id(self) -> str:
        """Khóa idempotent: mỗi vị thế chỉ ghi một kỳ funding đúng một lần."""
        return f"funding:{self.symbol}:{self.funding_time.isoformat()}"


def funding_cashflow(
    *,
    direction: str,
    quantity: Decimal,
    contract_multiplier: Decimal,
    settlement: FundingSettlement,
) -> Decimal:
    """Trả chi phí funding có dấu của vị thế: dương là phải trả, âm là được nhận."""
    if direction not in {"long", "short"}:
        raise ValueError("direction phải là long hoặc short.")
    for value, name in ((quantity, "quantity"), (contract_multiplier, "contract_multiplier")):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
            raise ValueError(f"{name} phải là Decimal hữu hạn và dương.")
    nominal_value = settlement.mark_price * quantity * contract_multiplier
    signed = nominal_value * settlement.funding_rate
    return signed if direction == "long" else -signed
