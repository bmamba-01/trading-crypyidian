"""Catalog release chiến lược của sản phẩm: rule dựng sẵn H0x cộng các Strategy Card được phép.

Mọi nơi trong runtime và trang quản trị phải dùng `strategy_registry()` thay vì tự dựng
`StrategyRegistry()`, để dispatcher, PaperDecisionCycle, kiểm tra release ghim của vị thế mở và
kiểm định cấu hình cùng nhìn thấy đúng một tập release và đúng một hash cho mỗi release.

Một Strategy Card chỉ vào đường quyết định khi được liệt kê tường minh ở `PRODUCT_CARD_IDS`. Như
mọi release khác, nó chỉ được dùng khi workspace có hàng `strategy_release` PUBLISHED khớp hash và
cấu hình đã công bố của bot chọn release đó; trạng thái là `EXPERIMENTAL_UNVALIDATED`, chưa có
bằng chứng về hiệu quả giao dịch.
"""

from __future__ import annotations

from functools import lru_cache

from trading_bot.application.strategy_pipeline.card_strategy import StrategyCardStrategy
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.domain.strategy.contracts import Strategy
from trading_bot.domain.strategy.expanded_rules import EXPANDED_RULES
from trading_bot.domain.strategy.registry import StrategyRegistry
from trading_bot.domain.strategy.rules_fx8 import FX8_RULES
from trading_bot.domain.strategy.scalping_rules import (
    H18SweepReclaimRule,
    H19VwapRangeReversionRule,
    H20BreakRetestRule,
)

# Thẻ được phép làm release của sản phẩm (toàn bộ 8 Strategy Card đã hoàn thiện L0–L6).
PRODUCT_CARD_IDS: tuple[str, ...] = (
    "SC-BINANCE-USDM-VOL-BREAKOUT-001",
    "SC-BINANCE-USDM-MEAN-REVERT-002",
    "SC-BINANCE-USDM-TSMOM-003",
    "SC-BINANCE-USDM-BBKC-SQUEEZE-004",
    "SC-BINANCE-USDM-TREND-PULLBACK-005",
    "SC-BINANCE-USDM-VWAP-REVERT-006",
    "SC-BINANCE-USDM-MACD-ADX-007",
    "SC-BINANCE-USDM-TAKER-FLOW-008",
)
STRATEGY_CARD_RECIPE_VERSION = "strategy-card-bridge-v1"

_CARD_FORMULAS_VI: dict[str, str] = {
    "SC-BINANCE-USDM-VOL-BREAKOUT-001": (
        "Đồ thị L0–L6: râu dưới của nến tín hiệu vượt ngưỡng ngòi nổ khi ATR đã quản "
        "trị qua cổng biến động; stop và mục tiêu theo bội số ATR của vùng giá L6."
    ),
    "SC-BINANCE-USDM-MEAN-REVERT-002": (
        "Đồ thị L0–L6: Z-Score 20 vượt cực trị ±1,5 kèm nến rút chân & thân hẹp, lọc bởi ATR/EMA."
    ),
    "SC-BINANCE-USDM-TSMOM-003": (
        "Đồ thị L0–L6: Động lượng chuỗi thời gian đa chân trời 4h kết hợp hàng rào biến động RV20."
    ),
    "SC-BINANCE-USDM-BBKC-SQUEEZE-004": (
        "Đồ thị L0–L6: Nén Bollinger–Keltner tại t-1 rồi bùng nổ BandWidth kèm khối lượng."
    ),
    "SC-BINANCE-USDM-TREND-PULLBACK-005": (
        "Đồ thị L0–L6: Nhịp hồi về EMA20 trong xu hướng 1h/4h đồng thuận kèm RSI14 [40, 60]."
    ),
    "SC-BINANCE-USDM-VWAP-REVERT-006": (
        "Đồ thị L0–L6: Độ lệch giá chuẩn ATR khỏi Session VWAP UTC trong vùng ADX14 < 20."
    ),
    "SC-BINANCE-USDM-MACD-ADX-007": (
        "Đồ thị L0–L6: Chuyển pha MACD Histogram có xác nhận ADX14 ≥ 22, ±DI và 4h EMA200."
    ),
    "SC-BINANCE-USDM-TAKER-FLOW-008": (
        "Đồ thị L0–L6: Phá đỉnh/đáy 20 nến xác nhận bằng mất cân bằng dòng lệnh chủ động taker."
    ),
}


@lru_cache(maxsize=1)
def strategy_card_rules() -> tuple[tuple[Strategy, str, str, str], ...]:
    """Các rule Strategy Card của sản phẩm theo định dạng `StrategyRegistry(extra_rules=...)`."""
    cards = {card.card_id: card for card in build_default_demo_cards()}
    rules: list[tuple[Strategy, str, str, str]] = []
    for card_id in PRODUCT_CARD_IDS:
        card = cards[card_id]
        rules.append(
            (
                StrategyCardStrategy(card),
                f"Strategy Card: {card.name}",
                _CARD_FORMULAS_VI.get(card_id, f"Đồ thị L0–L6 của thẻ {card.name}."),
                STRATEGY_CARD_RECIPE_VERSION,
            )
        )
    return tuple(rules)


SCALPING_RECIPE_VERSION = "sig4-scalping-exit-v1"


def scalping_rules() -> tuple[tuple[Strategy, str, str, str], ...]:
    """Họ scalping H18–H20 (SIG-4, M5); thang chốt lời lấy từ ``ladder_for_release``."""
    common = (
        " Chỉ phiên London/New York, tránh ±10 phút quanh funding, stop ≥ 3× phí khứ hồi; "
        "TP1 1R chốt 50% rồi dời stop về hòa vốn, TP2 2R; tối đa 12 nến M5."
    )
    return (
        (
            H18SweepReclaimRule(),
            "Quét thanh khoản và lấy lại mức",
            "Râu M5 vượt đỉnh/đáy phiên hoặc PDH/PDL rồi đóng quay vào; delta nến ngược hướng "
            "quét; stop sau râu + 0,1 ATR." + common,
            SCALPING_RECIPE_VERSION,
        ),
        (
            H19VwapRangeReversionRule(),
            "Hồi về VWAP trong vùng đi ngang",
            "ADX14 < 18 và ER40 < 0,25; giá đóng M5 lệch ≥ 2σ khỏi VWAP ngày UTC; vào ngược "
            "hướng lệch; stop 1 ATR." + common,
            SCALPING_RECIPE_VERSION,
        ),
        (
            H20BreakRetestRule(),
            "Phá cấu trúc và kiểm tra lại",
            "BOS M5 bằng giá đóng có |delta| ≥ phân vị 80; xu hướng M15 cùng chiều; nến retest "
            "giữ ngoài mức phá; stop tại swing đối diện." + common,
            SCALPING_RECIPE_VERSION,
        ),
    )


def expanded_rules() -> tuple[tuple[Strategy, str, str, str], ...]:
    """6 rule chiến lược định lượng mở rộng (truyền thống & nâng cao Crypto Futures)."""
    return EXPANDED_RULES  # type: ignore[return-value]


def fx8_rules() -> tuple[tuple[Strategy, str, str, str], ...]:
    """5 release cải tiến FX-8 (CRY-30) cho H01 v2, H03 v4 (15m/1h/4h) và H06 v3 (1h)."""
    return FX8_RULES


def strategy_registry() -> StrategyRegistry:
    """Registry đầy đủ của sản phẩm; rẻ để dựng và không giữ trạng thái."""
    return StrategyRegistry(
        extra_rules=(*strategy_card_rules(), *scalping_rules(), *expanded_rules(), *fx8_rules())
    )
