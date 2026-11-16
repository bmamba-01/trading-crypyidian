"""Phát hiện niêm yết/gỡ hợp đồng Binance USDⓈ-M từ ``exchangeInfo`` (AI-4 lát 3, 16/11/2026).

Nguồn (kiểm 16/11/2026):

- ``GET /fapi/v1/exchangeInfo`` (weight 1): mỗi hợp đồng có ``status``, ``contractType``,
  ``deliveryDate``, ``onboardDate``. Danh sách trạng thái: PENDING_TRADING, TRADING,
  PRE_DELIVERING, DELIVERING, DELIVERED, PRE_SETTLE, SETTLING, CLOSE, TRADING_HALT,
  TRADING_CANCEL_ONLY (https://developers.binance.com/docs/derivatives/usds-margined-futures/
  common-definition).
- Binance cập nhật ``deliveryDate`` thành thời điểm gỡ ngay khi công bố gỡ hợp đồng vĩnh cửu
  (Delist Schedule, https://developers.binance.com/docs/derivatives/coin-margined-futures/
  market-data/rest-api/Delist-Schedule). Perpetual bình thường mang mốc giả năm 2100
  (4133404800000 = 2100-12-25); dữ liệu thật 16/11/2026: hợp đồng đã gỡ ở ``SETTLING`` kèm ngày gỡ.
- Binance có thể **dời** ngày gỡ sau khi công bố, nên mọi thay đổi ``deliveryDate`` là sự kiện mới.

Module thuần: không I/O. Mỗi lần tải là danh sách ``ListingEntry`` đã chuẩn hóa; thay đổi suy ra
bằng so sánh hai lần tải liên tiếp (chỉ thấy được sau khi hệ thống tải, không nhìn trước).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

LISTINGS_SOURCE_ID = "BINANCE_USDM_EXCHANGE_INFO"
LISTINGS_DIGEST_VERSION = "listings_digest.v1"
# Mốc từ năm 2100 trở đi = "không có ngày giao" (sentinel của perpetual).
_NO_DELIVERY_FROM = datetime(2100, 1, 1, tzinfo=UTC)
RECENT_WINDOW = timedelta(hours=24)
MAX_FETCH_AGE = timedelta(hours=6)
DIGEST_MAX_CHANGES = 15
_VN = ZoneInfo("Asia/Ho_Chi_Minh")


class ListingsContractError(ValueError):
    """Phản hồi exchangeInfo sai hợp đồng."""


@dataclass(frozen=True, slots=True)
class ListingEntry:
    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    delivery_at: datetime | None  # None = perpetual không có ngày gỡ
    onboard_at: datetime | None

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "contract_type": self.contract_type,
            "quote_asset": self.quote_asset,
            "delivery_at": self.delivery_at.isoformat() if self.delivery_at else None,
            "onboard_at": self.onboard_at.isoformat() if self.onboard_at else None,
        }

    @classmethod
    def from_payload(cls, item: Mapping[str, Any]) -> ListingEntry:
        def moment(value: Any) -> datetime | None:
            return datetime.fromisoformat(str(value)).astimezone(UTC) if value else None

        return cls(
            symbol=str(item["symbol"]),
            status=str(item["status"]),
            contract_type=str(item["contract_type"]),
            quote_asset=str(item["quote_asset"]),
            delivery_at=moment(item.get("delivery_at")),
            onboard_at=moment(item.get("onboard_at")),
        )

    @property
    def delist_scheduled(self) -> bool:
        """Hợp đồng vĩnh cửu còn giao dịch nhưng đã có ngày gỡ thật."""
        return (
            self.contract_type.endswith("PERPETUAL")
            and self.status == "TRADING"
            and self.delivery_at is not None
        )


def _moment(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ListingsContractError(f"{name} phải là mili-giây nguyên không âm.")
    moment = datetime.fromtimestamp(value / 1000, tz=UTC)
    return None if moment >= _NO_DELIVERY_FROM else moment


def parse_exchange_info(payload: Any) -> list[ListingEntry]:
    """``exchangeInfo`` → danh sách hợp đồng chuẩn hóa, sắp theo symbol (bỏ mọi trường khác)."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("symbols"), list):
        raise ListingsContractError("exchangeInfo thiếu mảng symbols.")
    entries: dict[str, ListingEntry] = {}
    for item in payload["symbols"]:
        if not isinstance(item, Mapping):
            raise ListingsContractError("exchangeInfo có phần tử không phải object.")
        symbol = item.get("symbol")
        status = item.get("status")
        if not isinstance(symbol, str) or not symbol or not isinstance(status, str) or not status:
            raise ListingsContractError("Hợp đồng thiếu symbol/status.")
        if symbol in entries:
            raise ListingsContractError(f"Symbol trùng: {symbol}.")
        entries[symbol] = ListingEntry(
            symbol=symbol,
            status=status,
            contract_type=str(item.get("contractType") or ""),
            quote_asset=str(item.get("quoteAsset") or ""),
            delivery_at=_moment(item.get("deliveryDate"), f"{symbol}.deliveryDate"),
            onboard_at=_moment(item.get("onboardDate"), f"{symbol}.onboardDate"),
        )
    if len(entries) < 10:
        raise ListingsContractError("exchangeInfo quá ít hợp đồng, nghi phản hồi lỗi.")
    return [entries[symbol] for symbol in sorted(entries)]


def entries_hash(entries: Sequence[ListingEntry]) -> str:
    canonical = json.dumps(
        [e.payload() for e in sorted(entries, key=lambda e: e.symbol)],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# So sánh hai lần tải
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListingChange:
    """Một thay đổi giữa hai lần tải; ``detected_at`` = mốc lần tải sau (lúc hệ thống biết)."""

    kind: str  # LISTED | REMOVED | STATUS_CHANGED | DELIST_SCHEDULED | DELIST_RESCHEDULED |
    #            DELIST_CANCELLED
    symbol: str
    detected_at: datetime
    before: str | None = None
    after: str | None = None


def diff_listings(
    previous: Iterable[ListingEntry], current: Iterable[ListingEntry], detected_at: datetime
) -> list[ListingChange]:
    old = {e.symbol: e for e in previous}
    new = {e.symbol: e for e in current}
    changes: list[ListingChange] = []
    for symbol in sorted(set(old) | set(new)):
        before, after = old.get(symbol), new.get(symbol)
        if before is None and after is not None:
            changes.append(ListingChange("LISTED", symbol, detected_at, None, after.status))
            continue
        if after is None and before is not None:
            changes.append(ListingChange("REMOVED", symbol, detected_at, before.status, None))
            continue
        if before is None or after is None:  # pragma: no cover - đã xử lý ở trên
            continue
        if before.status != after.status:
            changes.append(
                ListingChange("STATUS_CHANGED", symbol, detected_at, before.status, after.status)
            )
        if before.delivery_at != after.delivery_at and after.contract_type.endswith("PERPETUAL"):
            old_at = before.delivery_at.isoformat() if before.delivery_at else None
            new_at = after.delivery_at.isoformat() if after.delivery_at else None
            kind = (
                "DELIST_SCHEDULED"
                if before.delivery_at is None
                else "DELIST_CANCELLED"
                if after.delivery_at is None
                else "DELIST_RESCHEDULED"
            )
            changes.append(ListingChange(kind, symbol, detected_at, old_at, new_at))
    return changes


# ---------------------------------------------------------------------------
# Digest cho AI Analyst / thẻ Telegram
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListingsFetch:
    """Một lần tải có nội dung (``entries``); lần tải trùng nội dung lần trước không lưu lại."""

    fetch_id: str
    fetched_at: datetime
    entries: tuple[ListingEntry, ...]


_KIND_VI = {
    "LISTED": "niêm yết mới",
    "REMOVED": "biến mất khỏi danh sách",
    "STATUS_CHANGED": "đổi trạng thái",
    "DELIST_SCHEDULED": "lên lịch gỡ",
    "DELIST_RESCHEDULED": "dời ngày gỡ",
    "DELIST_CANCELLED": "hủy lịch gỡ",
}


def _vn(moment: datetime) -> str:
    return moment.astimezone(_VN).strftime("%d/%m %H:%M")


def listings_digest(
    fetches: Iterable[ListingsFetch],
    now: datetime,
    *,
    last_checked_at: datetime | None,
    watch_symbols: Sequence[str] = (),
    window: timedelta = RECENT_WINDOW,
) -> dict[str, Any]:
    """Coin đang theo dõi có rủi ro gỡ + thay đổi trong ``window`` gần nhất (point-in-time).

    ``fetches`` là các lần tải có nội dung, ``last_checked_at`` là lần kiểm gần nhất (kể cả lần
    nội dung không đổi) để đánh giá độ mới.
    """
    if now.tzinfo is None:
        raise ValueError("now phải có múi giờ.")
    instant = now.astimezone(UTC)
    usable = sorted((f for f in fetches if f.fetched_at <= instant), key=lambda f: f.fetched_at)
    fresh = last_checked_at is not None and instant - last_checked_at <= MAX_FETCH_AGE
    base: dict[str, Any] = {
        "digest_version": LISTINGS_DIGEST_VERSION,
        "source_id": LISTINGS_SOURCE_ID,
        "as_of": instant.isoformat(),
        "last_checked_at": last_checked_at.isoformat() if last_checked_at else None,
        "is_fresh": fresh,
    }
    if not usable:
        return {**base, "watched_at_risk": [], "recent_changes": [], "pending_listings": []}
    latest = {e.symbol: e for e in usable[-1].entries}
    changes: list[ListingChange] = []
    for previous, current in zip(usable, usable[1:], strict=False):
        if instant - current.fetched_at <= window:
            changes.extend(diff_listings(previous.entries, current.entries, current.fetched_at))
    watch = list(dict.fromkeys(watch_symbols))
    at_risk = []
    for symbol in watch:
        entry = latest.get(symbol)
        if entry is None:
            at_risk.append({"symbol": symbol, "status": "MISSING", "delist_at_vn": None})
        elif entry.status != "TRADING" or entry.delist_scheduled:
            at_risk.append(
                {
                    "symbol": symbol,
                    "status": entry.status,
                    "delist_at_utc": entry.delivery_at.isoformat() if entry.delivery_at else None,
                    "delist_at_vn": _vn(entry.delivery_at) if entry.delivery_at else None,
                }
            )
    watched = set(watch)
    ordered = sorted(changes, key=lambda c: (c.symbol not in watched, -c.detected_at.timestamp()))
    return {
        **base,
        "trading_perpetual_count": sum(
            1 for e in latest.values() if e.status == "TRADING" and e.contract_type == "PERPETUAL"
        ),
        "watched_at_risk": at_risk,
        "recent_changes": [
            {
                "symbol": c.symbol,
                "kind": c.kind,
                "kind_vi": _KIND_VI[c.kind],
                "before": c.before,
                "after": c.after,
                "detected_at": c.detected_at.isoformat(),
                "watched": c.symbol in watched,
            }
            for c in ordered[:DIGEST_MAX_CHANGES]
        ],
        "pending_listings": sorted(
            e.symbol for e in latest.values() if e.status == "PENDING_TRADING"
        )[:DIGEST_MAX_CHANGES],
    }


__all__ = [
    "LISTINGS_DIGEST_VERSION",
    "LISTINGS_SOURCE_ID",
    "ListingChange",
    "ListingEntry",
    "ListingsContractError",
    "ListingsFetch",
    "diff_listings",
    "entries_hash",
    "listings_digest",
    "parse_exchange_info",
]
