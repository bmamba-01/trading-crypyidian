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
from trading_bot.domain.strategy.registry import StrategyRegistry

# Thẻ được phép làm release của sản phẩm. Thẻ `SC-BINANCE-USDM-MEAN-REVERT-002` chưa được đưa vào
# vì engine chưa hiện thực ngòi nổ và z-score mà thẻ khai báo.
PRODUCT_CARD_IDS: tuple[str, ...] = ("SC-BINANCE-USDM-VOL-BREAKOUT-001",)
STRATEGY_CARD_RECIPE_VERSION = "strategy-card-bridge-v1"


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
                (
                    "Đồ thị L0–L6: râu dưới của nến tín hiệu vượt ngưỡng ngòi nổ khi ATR đã quản "
                    "trị qua cổng biến động; stop và mục tiêu theo bội số ATR của vùng giá L6."
                ),
                STRATEGY_CARD_RECIPE_VERSION,
            )
        )
    return tuple(rules)


def strategy_registry() -> StrategyRegistry:
    """Registry đầy đủ của sản phẩm; rẻ để dựng và không giữ trạng thái."""
    return StrategyRegistry(extra_rules=strategy_card_rules())
