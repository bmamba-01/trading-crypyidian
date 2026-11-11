"""Yêu cầu replay dữ liệu tổng hợp QA có danh tính tách biệt Binance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from trading_bot.application.binance_rest_backfill import (
    CRYPIDIAN_FIRST_PARTY_QA_PROVIDER,
    BackfillRightsPurpose,
    RestBackfillRequest,
)

CRYPIDIAN_FIRST_PARTY_QA = CRYPIDIAN_FIRST_PARTY_QA_PROVIDER
CRYPIDIAN_FIRST_PARTY_QA_SOURCE = "CRYPIDIAN_FIRST_PARTY_QA_FIXTURE"
CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF = (
    "https://github.com/bmamba-01/trading-crypyidian/blob/dev/"
    "docs/research/wp06-first-party-qa-fixture.md"
)
_FIXTURE_ID = "wp06-h03-source-pinned-v1"
_CURSOR_PREFIX = "crypidian-first-party-qa:v1:"


@dataclass(frozen=True, slots=True)
class FirstPartyQADatasetRequest:
    """Khoảng synthetic replay được ràng buộc với fixture do repository sở hữu."""

    symbol: str
    interval: str
    start: datetime
    end: datetime
    purpose: BackfillRightsPurpose = BackfillRightsPurpose.BACKTEST_RESEARCH
    _validated_range: RestBackfillRequest = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.purpose is not BackfillRightsPurpose.BACKTEST_RESEARCH:
            raise ValueError("Fixture QA chỉ được dùng cho backtest research.")
        validated = RestBackfillRequest(
            symbol=self.symbol,
            interval=self.interval,
            start=self.start,
            end=self.end,
            purpose=self.purpose,
        )
        object.__setattr__(self, "start", validated.start)
        object.__setattr__(self, "end", validated.end)
        object.__setattr__(self, "_validated_range", validated)

    @property
    def provider(self) -> str:
        """Provider synthetic được allowlist riêng, không đại diện Binance."""
        return CRYPIDIAN_FIRST_PARTY_QA

    @property
    def market_source(self) -> str:
        """Nhãn MarketBar tách biệt nguồn Binance REST backfill."""
        return CRYPIDIAN_FIRST_PARTY_QA_SOURCE

    @property
    def cursor_prefix(self) -> str:
        """Prefix cursor xác định fixture/version được repository sở hữu."""
        return _CURSOR_PREFIX

    @property
    def interval_duration(self) -> timedelta:
        """Độ dài interval đã xác thực theo cùng lưới UTC với Binance REST."""
        return self._validated_range.interval_duration

    @property
    def expected_bars(self) -> int:
        """Số mốc nến trong khoảng nửa mở đã xác thực."""
        return self._validated_range.expected_bars

    @property
    def fingerprint(self) -> str:
        """Hash request gắn provider QA và phiên bản fixture cụ thể."""
        payload = {
            "provider": self.provider,
            "fixture_id": _FIXTURE_ID,
            "symbol": self.symbol,
            "interval": self.interval,
            "start": self.start.astimezone(UTC).isoformat(),
            "end": self.end.astimezone(UTC).isoformat(),
            "purpose": self.purpose.value,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def validate_bar_identity(self, *, source: str, source_cursor: str) -> None:
        """Từ chối bar không mang đúng nhãn và cursor của fixture QA."""
        if source != self.market_source or not source_cursor.startswith(self.cursor_prefix):
            raise ValueError("Bar không thuộc fixture first-party QA đã định danh.")
