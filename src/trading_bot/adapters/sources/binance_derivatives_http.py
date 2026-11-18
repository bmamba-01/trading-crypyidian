"""Tải dữ liệu công khai Binance USDⓈ-M (AI-4 lát 2–3). Chỉ GET endpoint công khai cố định.

Không API key, không endpoint giao dịch/tài khoản. Request ``/futures/data`` (weight 0, 1000/5
phút/IP) được giãn cách ``pause_seconds``; HTTP 418/429 ném ``BinanceThrottled`` để collector dừng
cả lượt thay vì dồn thêm request.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx2

from trading_bot.domain.research.derivatives import (
    MAX_FUTURES_DATA_REQUESTS_PER_5M,
    OI_LOOKBACK_PERIODS,
    STATS_PERIOD,
    SourceThrottled,
)

BASE_URL = "https://fapi.binance.com"
_MAX_BYTES = 1_000_000
_RATE_WINDOW_SECONDS = 300.0
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
        max_futures_data_per_5m: int = MAX_FUTURES_DATA_REQUESTS_PER_5M,
        monotonic_clock: Callable[[], float] = time.monotonic,
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
        self._max_futures_data_per_5m = max_futures_data_per_5m
        self._monotonic = monotonic_clock
        self._futures_data_timestamps: deque[float] = deque()

    def _consume_futures_data_budget(self) -> None:
        """Giữ số request `/futures/data` dưới trần 1.000/5 phút để tránh bị 418/429."""
        now = self._monotonic()
        cutoff = now - _RATE_WINDOW_SECONDS
        while self._futures_data_timestamps and self._futures_data_timestamps[0] <= cutoff:
            self._futures_data_timestamps.popleft()
        if len(self._futures_data_timestamps) >= self._max_futures_data_per_5m:
            raise BinanceThrottled("Vượt ngưỡng an toàn 1.000 request /futures/data trong 5 phút.")
        self._futures_data_timestamps.append(now)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any], *, max_bytes: int = _MAX_BYTES) -> Any:
        if path.startswith("/futures/data/"):
            self._consume_futures_data_budget()
        response = await self._client.get(path, params=params)
        if response.status_code in {418, 429}:
            raise BinanceThrottled(f"{path}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {response.status_code}")
        if len(response.content) > max_bytes:
            raise RuntimeError(f"{path}: phản hồi quá lớn")
        return response.json()

    async def fetch_symbol(self, symbol: str) -> dict[str, Any]:
        """Phản hồi thô 7 endpoint cho một symbol (khóa như ``build_snapshot`` yêu cầu)."""
        raw: dict[str, Any] = {
            "premium": await self._get("/fapi/v1/premiumIndex", {"symbol": symbol}),
            "ticker_24h": await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol}),
        }
        for key, (path, limit) in _STATS.items():
            await asyncio.sleep(self._pause)
            raw[key] = await self._get(
                path, {"symbol": symbol, "period": STATS_PERIOD, "limit": limit}
            )
        await asyncio.sleep(self._pause)
        raw["basis"] = await self._get(
            "/futures/data/basis",
            {
                "pair": symbol,
                "contractType": "PERPETUAL",
                "period": STATS_PERIOD,
                "limit": 1,
            },
        )
        return raw

    async def fetch_exchange_info(self) -> Any:
        """AI-4 lát 3: ``exchangeInfo`` đầy đủ (weight 1, ~1,2 MB ngày 16/11/2026)."""
        return await self._get("/fapi/v1/exchangeInfo", {}, max_bytes=8_000_000)


__all__ = ["BASE_URL", "BinanceDerivativesHttpFetcher", "BinanceThrottled"]
