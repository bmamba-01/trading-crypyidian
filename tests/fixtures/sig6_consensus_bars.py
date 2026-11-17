"""Nến xác định cho kiểm thử đồng thuận giả SIG-6 (có biến động thật để Pearson có nghĩa)."""

from __future__ import annotations

import math
from decimal import Decimal

from trading_bot.application.strategy_pipeline.consensus import PriceBar


def consensus_bars(n: int, *, broken_at: int | None = None) -> list[PriceBar]:
    bars: list[PriceBar] = []
    close = 100.0
    for index in range(n):
        drift = math.sin(index / 7) * 1.5 + math.cos(index / 3) * 0.8
        open_ = close
        close = max(1.0, open_ + drift)
        high = max(open_, close) + abs(math.sin(index)) * 1.2 + 0.1
        low = min(open_, close) - abs(math.cos(index * 1.3)) * 1.1 - 0.1
        if broken_at == index:
            high = low - 1  # vi phạm L0: High < Low
        bars.append(
            PriceBar(
                open=Decimal(f"{open_:.4f}"),
                high=Decimal(f"{high:.4f}"),
                low=Decimal(f"{low:.4f}"),
                close=Decimal(f"{close:.4f}"),
            )
        )
    return bars


async def consensus_bars_provider(
    _principal: object, _symbol: str, _timeframe: str
) -> list[PriceBar]:
    """Nguồn nến giả cho ``StrategyCardAdminService(bars_provider=...)`` trong kiểm thử."""
    return consensus_bars(120)
