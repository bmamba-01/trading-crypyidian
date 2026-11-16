"""Tải lịch công bố vĩ mô chính thức qua HTTPS (AI-4). Chỉ GET URL cố định, không xác thực."""

from __future__ import annotations

import json
from typing import Any

import httpx2

from trading_bot.domain.research.macro_calendar import BEA_SOURCE_ID, FOMC_SOURCE_ID

MACRO_SOURCE_URLS = {
    BEA_SOURCE_ID: "https://apps.bea.gov/API/signup/release_dates.json",
    FOMC_SOURCE_ID: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
}
_MAX_BYTES = 2_000_000


class MacroCalendarHttpFetcher:
    """Trả nội dung thô (JSON cho BEA, HTML cho FOMC); lỗi HTTP/kích thước ném ``RuntimeError``."""

    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._client = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(20.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            auth=None,
            headers={"User-Agent": "Crypidian/0.1 (macro calendar, PAPER)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, source_id: str) -> tuple[str, Any]:
        url = MACRO_SOURCE_URLS[source_id]
        response = await self._client.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"{source_id}: HTTP {response.status_code}")
        if len(response.content) > _MAX_BYTES:
            raise RuntimeError(f"{source_id}: phản hồi quá lớn")
        text = response.text
        return url, (json.loads(text) if source_id == BEA_SOURCE_ID else text)


__all__ = ["MACRO_SOURCE_URLS", "MacroCalendarHttpFetcher"]
