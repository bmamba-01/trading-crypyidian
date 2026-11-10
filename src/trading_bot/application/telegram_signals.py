"""Hợp đồng và nội dung thông báo tín hiệu-only qua Telegram."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

_SYMBOL = re.compile(r"^[A-Z0-9_]{2,40}$")
_DIRECTIONS = frozenset({"LONG", "SHORT"})
_TIMEFRAME = re.compile(r"^[1-9][0-9]{0,2}[smhdwM]$")
_FILTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_FILTER_VALUES = 100


@dataclass(frozen=True, slots=True)
class TelegramSignalFilters:
    """Allowlist tùy chọn theo dữ liệu của candidate; tuple rỗng nghĩa là mọi giá trị."""

    symbols: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()
    strategy_ids: tuple[str, ...] = ()
    setup_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", _normalize_filter_values(self.symbols, "symbols"))
        object.__setattr__(
            self, "directions", _normalize_filter_values(self.directions, "directions")
        )
        object.__setattr__(
            self,
            "strategy_ids",
            _normalize_filter_values(self.strategy_ids, "strategy_ids"),
        )
        object.__setattr__(
            self,
            "setup_codes",
            _normalize_filter_values(self.setup_codes, "setup_codes"),
        )
        timeframes = _normalize_filter_values(self.timeframes, "timeframes", uppercase=False)
        if any(_TIMEFRAME.fullmatch(value) is None for value in timeframes):
            raise ValueError("timeframes chứa interval không hợp lệ.")
        if any(value not in _DIRECTIONS for value in self.directions):
            raise ValueError("directions chỉ nhận LONG hoặc SHORT.")
        if any(_SYMBOL.fullmatch(value) is None for value in self.symbols):
            raise ValueError("symbols chứa mã không hợp lệ.")
        if any(
            _FILTER_ID.fullmatch(value) is None for value in (*self.strategy_ids, *self.setup_codes)
        ):
            raise ValueError("strategy_ids/setup_codes chứa giá trị không hợp lệ.")
        object.__setattr__(self, "timeframes", timeframes)

    def matches(self, candidate: TelegramSignalCandidate) -> bool:
        """Trả đúng khi candidate nằm trong mọi allowlist đang được bật."""
        return (
            (not self.symbols or candidate.symbol in self.symbols)
            and (not self.timeframes or candidate.timeframe in self.timeframes)
            and (not self.directions or candidate.direction in self.directions)
            and (not self.strategy_ids or candidate.strategy_id in self.strategy_ids)
            and (not self.setup_codes or candidate.reason_code in self.setup_codes)
        )

    def as_dict(self) -> dict[str, list[str]]:
        """Trả payload JSON ổn định cho DB, API và audit."""
        return {
            "symbols": list(self.symbols),
            "timeframes": list(self.timeframes),
            "directions": list(self.directions),
            "strategy_ids": list(self.strategy_ids),
            "setup_codes": list(self.setup_codes),
        }


def _normalize_filter_values(
    values: tuple[str, ...], name: str, *, uppercase: bool = True
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > _MAX_FILTER_VALUES:
        raise ValueError(f"{name} phải là tuple có tối đa {_MAX_FILTER_VALUES} giá trị.")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError(f"{name} chứa giá trị rỗng hoặc quá dài.")
        clean = value.strip()
        normalized.add(clean.upper() if uppercase else clean)
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class TelegramSignalCandidate:
    """Candidate đã được decision pipeline xác nhận, chưa phải order hay vị thế."""

    decision_id: UUID
    symbol: str
    direction: str
    timeframe: str
    strategy_id: str
    strategy_release_id: str
    reference_price: Decimal
    signal_at: datetime
    expires_at: datetime
    reason_code: str
    detail_vi: str
    invalidation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, UUID):
            raise ValueError("decision_id phải là UUID.")
        if not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("symbol không hợp lệ.")
        if self.direction not in _DIRECTIONS:
            raise ValueError("direction chỉ nhận LONG hoặc SHORT.")
        for name in (
            "timeframe",
            "strategy_id",
            "strategy_release_id",
            "reason_code",
            "detail_vi",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"{name} không hợp lệ.")
        if not isinstance(self.reference_price, Decimal) or self.reference_price <= 0:
            raise ValueError("reference_price phải là Decimal dương.")
        for name in ("signal_at", "expires_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} phải có múi giờ.")
        if self.expires_at <= self.signal_at:
            raise ValueError("expires_at phải sau signal_at.")
        if self.invalidation is not None and len(self.invalidation) > 500:
            raise ValueError("invalidation vượt giới hạn.")


def is_candidate_deliverable(
    candidate: TelegramSignalCandidate,
    *,
    now: datetime,
    max_age: timedelta,
    allowed_clock_skew: timedelta = timedelta(seconds=2),
) -> bool:
    """Chặn candidate hết hạn, quá cũ hoặc có thời gian phát sinh bất hợp lý."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now phải có múi giờ.")
    if max_age <= timedelta(0) or allowed_clock_skew < timedelta(0):
        raise ValueError("Ngưỡng freshness không hợp lệ.")
    instant = now.astimezone(candidate.signal_at.tzinfo)
    return (
        candidate.expires_at > instant
        and candidate.signal_at <= instant + allowed_clock_skew
        and instant - candidate.signal_at <= max_age
    )


def build_signal_message(candidate: TelegramSignalCandidate) -> str:
    """Tạo nội dung thuần văn bản, không có lệnh giao dịch hoặc cú pháp parse-mode."""
    lines = [
        "TÍN HIỆU THAM KHẢO — KHÔNG PHẢI LỆNH",
        f"Mã tín hiệu: {candidate.decision_id}",
        f"Cặp: {candidate.symbol} · Hướng: {candidate.direction} · Khung: {candidate.timeframe}",
        f"Giá tham chiếu: {candidate.reference_price}",
        f"Chiến thuật: {candidate.strategy_id}",
        f"Bản chiến thuật: {candidate.strategy_release_id}",
        f"Mã bắt tín hiệu: {candidate.reason_code}",
        f"Vì sao bắt tín hiệu: {candidate.detail_vi}",
        f"Thời điểm: {candidate.signal_at.isoformat()}",
        f"Hết hiệu lực: {candidate.expires_at.isoformat()}",
    ]
    if candidate.invalidation:
        lines.append(f"Điều kiện mất hiệu lực: {candidate.invalidation}")
    message = "\n".join(lines)
    if len(message) > 4096:
        raise ValueError("Nội dung tín hiệu vượt giới hạn Telegram.")
    return message
