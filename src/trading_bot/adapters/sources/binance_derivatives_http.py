"""Tải dữ liệu phái sinh công khai Binance USDⓈ-M (AI-4 lát 2). Chỉ GET endpoint công khai cố định.

Không API key, không endpoint giao dịch/tài khoản. Request ``/futures/data`` (weight 0, 1000/5
phút/IP) được giãn cách ``pause_seconds``; HTTP 418/429 ném ``BinanceThrottled`` để collector dừng
cả lượt thay vì dồn thêm request.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2

from trading_bot.domain.research.derivatives import (
    OI_LOOKBACK_PERIODS,
    STATS_PERIOD,
    SourceThrottled,
)

BASE_URL = "https://fapi.binance.com"
_MAX_BYTES = 1_000_000
_STATS = {
    "oi_hist": ("/futures/data/openInterestHist", OI_LOOKBACK_PERIODS),
    "global_ls": ("/futures/data/globalLongShortAccountRatio", 1),
    "top_ls": ("/futures/data/topLongShortPositionRatio", 1),
    "taker": ("/futures/data/takerlongshortRatio", 1),
}


class BinanceThrottled(SourceThrottled):
    """Binance báo vượt giới hạn (429) hoặc chặn IP (418)."""


class BinanceDerivativesHttpFetcher:
    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        pause_seconds: float = 0.25,
    ) -> None:
        self._client = httpx2.AsyncClient(
            base_url=BASE_URL,
            transport=transport,
            timeout=httpx2.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            auth=None,
            headers={"User-Agent": "Crypidian/0.1 (derivatives stats, PAPER)"},
        )
        self._pause = pause_seconds

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = await self._client.get(path, params=params)
        if response.status_code in {418, 429}:
            raise BinanceThrottled(f"{path}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {response.status_code}")
        if len(response.content) > _MAX_BYTES:
            raise RuntimeError(f"{path}: phản hồi quá lớn")
        return response.json()

    async def fetch_symbol(self, symbol: str) -> dict[str, Any]:
        """Phản hồi thô 5 endpoint cho một symbol (khóa như ``build_snapshot`` yêu cầu)."""
        raw: dict[str, Any] = {
            "premium": await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        }
        for key, (path, limit) in _STATS.items():
            await asyncio.sleep(self._pause)
            raw[key] = await self._get(
                path, {"symbol": symbol, "period": STATS_PERIOD, "limit": limit}
            )
        return raw


__all__ = ["BASE_URL", "BinanceDerivativesHttpFetcher", "BinanceThrottled"]
