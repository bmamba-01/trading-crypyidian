"""Port chỉ đọc tra cứu bản phát hành chiến lược cho tầng adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from trading_bot.domain.strategy.registry import StrategyRelease


class StrategyReleaseLookup(Protocol):
    """Hợp đồng tra cứu metadata release chiến lược theo `release_id`."""

    def get_release(self, release_id: str) -> StrategyRelease: ...


ReleaseLookup = Callable[[str], StrategyRelease]
