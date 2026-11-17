"""FX-3 lát 3b (CRY-198): chuỗi funding đã chốt đi kèm bundle replay.

Owner chọn (17/11/2026) đưa funding lịch sử **vào bundle** để replay tái lập được: tham chiếu bundle
schema 2 ghim ``content_hash`` của chuỗi; khi nạp lại, các kỳ được đọc lại từ ``market_funding``
(append-only) theo cùng ``as_of`` rồi so hash. Bundle không có funding giữ schema 1 và hash cũ.

Chỉ giữ kỳ có ``mark_price`` (đúng quy tắc của runtime PAPER: không thay mark price bằng giá khác);
số kỳ bị bỏ vì thiếu mark price nằm trong hash để không âm thầm khác dữ liệu.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from trading_bot.domain.execution.funding import FundingSettlement

FUNDING_SERIES_SOURCE = "BINANCE_USDM_REST"
# Quyền BACKTEST_RESEARCH của provider này phủ cả nến REST lẫn lịch sử funding cùng nguồn.
FUNDING_RIGHTS_PROVIDER = "BINANCE_USDM_REST"


class FundingRow(Protocol):
    @property
    def funding_time(self) -> datetime: ...
    @property
    def funding_rate(self) -> Decimal: ...
    @property
    def mark_price(self) -> Decimal | None: ...


@dataclass(frozen=True, slots=True)
class ReplayFundingSeries:
    """Các kỳ funding đã chốt của một symbol trong cửa sổ bundle, nhìn tại ``as_of``."""

    source: str
    symbol: str
    window_start: datetime
    window_end: datetime
    as_of: datetime
    settlements: tuple[FundingSettlement, ...]
    missing_mark_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "settlements", tuple(self.settlements))
        if self.source != FUNDING_SERIES_SOURCE or not self.symbol.strip():
            raise ValueError("Chuỗi funding cần nguồn BINANCE_USDM_REST và symbol.")
        for value in (self.window_start, self.window_end, self.as_of):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError("Mốc thời gian chuỗi funding cần timezone.")
        if self.window_start >= self.window_end:
            raise ValueError("Cửa sổ chuỗi funding không hợp lệ.")
        if type(self.missing_mark_count) is not int or self.missing_mark_count < 0:
            raise ValueError("missing_mark_count phải là số nguyên không âm.")
        previous: datetime | None = None
        for item in self.settlements:
            if type(item) is not FundingSettlement or item.symbol != self.symbol:
                raise ValueError("Kỳ funding khác symbol của chuỗi.")
            if not self.window_start <= item.funding_time < self.window_end:
                raise ValueError("Kỳ funding nằm ngoài cửa sổ chuỗi.")
            if previous is not None and item.funding_time <= previous:
                raise ValueError("Kỳ funding phải tăng dần và không trùng mốc.")
            previous = item.funding_time

    @property
    def content_hash(self) -> str:
        """Hash nội dung các kỳ (mốc UTC, rate, mark) và số kỳ bị bỏ vì thiếu mark."""
        payload = {
            "rows": [
                [
                    item.funding_time.astimezone(UTC).isoformat(),
                    str(item.funding_rate),
                    str(item.mark_price),
                ]
                for item in self.settlements
            ],
            "missing_mark_count": self.missing_mark_count,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def reference(self) -> ReplayFundingReference:
        return ReplayFundingReference(
            source=self.source,
            window_start=self.window_start,
            window_end=self.window_end,
            as_of=self.as_of,
            count=len(self.settlements),
            missing_mark_count=self.missing_mark_count,
            content_hash=self.content_hash,
        )


_REFERENCE_FIELDS = frozenset(
    {
        "source",
        "window_start",
        "window_end",
        "as_of",
        "count",
        "missing_mark_count",
        "content_hash",
    }
)


@dataclass(frozen=True, slots=True)
class ReplayFundingReference:
    """Phần funding trong tham chiếu bundle schema 2 (đủ để đọc lại và kiểm hash)."""

    source: str
    window_start: datetime
    window_end: datetime
    as_of: datetime
    count: int
    missing_mark_count: int
    content_hash: str

    def __post_init__(self) -> None:
        if self.source != FUNDING_SERIES_SOURCE:
            raise ValueError("Nguồn funding của bundle không được hỗ trợ.")
        for value in (self.window_start, self.window_end, self.as_of):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError("Mốc thời gian funding của bundle cần timezone.")
        if self.window_start >= self.window_end:
            raise ValueError("Cửa sổ funding của bundle không hợp lệ.")
        for number in (self.count, self.missing_mark_count):
            if type(number) is not int or number < 0:
                raise ValueError("Số kỳ funding phải là số nguyên không âm.")
        if (
            not isinstance(self.content_hash, str)
            or len(self.content_hash) != 64
            or any(char not in "0123456789abcdef" for char in self.content_hash)
        ):
            raise ValueError("content_hash funding phải là SHA-256 hex thường.")

    def payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "as_of": self.as_of.isoformat(),
            "count": self.count,
            "missing_mark_count": self.missing_mark_count,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ReplayFundingReference:
        if type(payload) is not dict or set(payload) != _REFERENCE_FIELDS:
            raise ValueError("Funding reference có field thiếu hoặc không nhận diện.")
        return cls(
            source=payload["source"],
            window_start=datetime.fromisoformat(payload["window_start"]),
            window_end=datetime.fromisoformat(payload["window_end"]),
            as_of=datetime.fromisoformat(payload["as_of"]),
            count=payload["count"],
            missing_mark_count=payload["missing_mark_count"],
            content_hash=payload["content_hash"],
        )


def funding_series_from_rows(
    rows: Sequence[FundingRow],
    *,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    as_of: datetime,
) -> ReplayFundingSeries:
    """Dựng chuỗi từ dòng đã chốt (một dòng mỗi mốc); dòng thiếu mark price bị đếm và bỏ."""
    settlements: list[FundingSettlement] = []
    missing = 0
    for row in sorted(rows, key=lambda item: item.funding_time):
        if not window_start <= row.funding_time < window_end:
            continue
        if row.mark_price is None:
            missing += 1
            continue
        settlements.append(
            FundingSettlement(
                symbol=symbol,
                funding_time=row.funding_time.astimezone(UTC),
                funding_rate=row.funding_rate,
                mark_price=row.mark_price,
            )
        )
    return ReplayFundingSeries(
        source=FUNDING_SERIES_SOURCE,
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
        settlements=tuple(settlements),
        missing_mark_count=missing,
    )


class FundingRowSource(Protocol):
    async def settled_in_window(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        window_start: datetime,
        window_end: datetime,
        as_of: datetime,
    ) -> Sequence[FundingRow]: ...


async def load_funding_series(
    source: FundingRowSource,
    *,
    workspace_id: UUID,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    as_of: datetime,
) -> ReplayFundingSeries:
    """Đọc point-in-time ``available_at <= as_of`` rồi dựng chuỗi xác định."""
    rows = await source.settled_in_window(
        workspace_id=workspace_id,
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
    )
    return funding_series_from_rows(
        rows, symbol=symbol, window_start=window_start, window_end=window_end, as_of=as_of
    )
