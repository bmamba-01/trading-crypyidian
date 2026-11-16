"""Tải lịch công bố vĩ mô chính thức qua HTTPS (AI-4). Chỉ GET URL cố định, không xác thực.

OMB PFEI: trang hướng dẫn liệt kê PDF lịch theo năm; chỉ theo link PDF nằm dưới
``https://www.whitehouse.gov/wp-content/uploads/`` (không theo link ngoài), tối đa
``_MAX_PFEI_DOCUMENTS`` file, trích văn bản bằng ``pypdf`` tại adapter để domain parse thuần.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

import httpx2
from pypdf import PdfReader

from trading_bot.domain.research.macro_calendar import (
    BEA_SOURCE_ID,
    FOMC_SOURCE_ID,
    PFEI_SOURCE_ID,
)

MACRO_SOURCE_URLS = {
    BEA_SOURCE_ID: "https://apps.bea.gov/API/signup/release_dates.json",
    FOMC_SOURCE_ID: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    PFEI_SOURCE_ID: (
        "https://www.whitehouse.gov/omb/information-resources/guidance/"
        "us-principal-federal-economic-indicators/"
    ),
}
_MAX_BYTES = 2_000_000
_MAX_PFEI_DOCUMENTS = 4
_PFEI_PDF_LINK = re.compile(
    r"https://www\.whitehouse\.gov/wp-content/uploads/\d{4}/\d{2}/[A-Za-z0-9_.-]*pfei[A-Za-z0-9_.-]*\.pdf",
    re.IGNORECASE,
)


def pfei_pdf_links(html: str) -> list[str]:
    """Link PDF lịch PFEI trên trang OMB (giữ thứ tự, bỏ trùng)."""
    links = list(dict.fromkeys(_PFEI_PDF_LINK.findall(html)))
    if not links:
        raise RuntimeError(f"{PFEI_SOURCE_ID}: trang OMB không có link PDF lịch PFEI")
    return links[:_MAX_PFEI_DOCUMENTS]


def pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)


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

    async def _get(self, source_id: str, url: str) -> httpx2.Response:
        response = await self._client.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"{source_id}: HTTP {response.status_code}")
        if len(response.content) > _MAX_BYTES:
            raise RuntimeError(f"{source_id}: phản hồi quá lớn")
        return response

    async def fetch(self, source_id: str) -> tuple[str, Any]:
        url = MACRO_SOURCE_URLS[source_id]
        response = await self._get(source_id, url)
        if source_id == PFEI_SOURCE_ID:
            documents = []
            for link in pfei_pdf_links(response.text):
                pdf = await self._get(source_id, link)
                documents.append((link, pdf_text(pdf.content)))
            return url, documents
        text = response.text
        return url, (json.loads(text) if source_id == BEA_SOURCE_ID else text)


__all__ = ["MACRO_SOURCE_URLS", "MacroCalendarHttpFetcher", "pdf_text", "pfei_pdf_links"]
