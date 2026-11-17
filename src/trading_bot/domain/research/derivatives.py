"""Dữ liệu phái sinh công khai Binance USDⓈ-M (AI-4 lát 2, 16/11/2026): ảnh chụp theo giờ.

Nguồn (tài liệu kiểm 16/11/2026, https://developers.binance.com/docs/derivatives/
usds-margined-futures/market-data/rest-api):

- ``GET /fapi/v1/premiumIndex`` — mark price, ``lastFundingRate``, ``nextFundingTime`` (weight 1).
- ``GET /futures/data/openInterestHist`` — tổng OI và giá trị OI theo kỳ; giữ 30 ngày gần nhất.
- ``GET /futures/data/globalLongShortAccountRatio`` — tỷ lệ tài khoản long/short (mọi tài khoản).
- ``GET /futures/data/topLongShortPositionRatio`` — tỷ lệ vị thế long/short của top trader.
- ``GET /futures/data/takerlongshortRatio`` — tỷ lệ khối lượng mua/bán chủ động (taker).

Các endpoint ``/futures/data`` có weight 0, giới hạn 1000 request/5 phút/IP; kỳ không có giao dịch
bị bỏ (không điền). Mốc ``timestamp`` của từng loại có thể khác nhau (vd taker trễ một kỳ), nên mỗi
chỉ số giữ mốc riêng và đều phải ≤ ``collected_at`` (thời điểm dữ liệu khả dụng cho hệ thống).

Module thuần: không I/O. Chỉ là **bối cảnh** cho AI Analyst; chưa chiến lược nào dùng để vào lệnh.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

DERIVATIVES_SOURCE_ID = "BINANCE_USDM_DERIVATIVES_STATS"
DERIVATIVES_DIGEST_VERSION = "derivatives_digest.v1"
STATS_PERIOD = "1h"
OI_LOOKBACK_PERIODS = 25  # kỳ hiện tại + 24 kỳ 1h trước → thay đổi OI 24 giờ
DIGEST_MAX_AGE = timedelta(hours=3)
DIGEST_MAX_ROWS = 20
# Đồng hồ máy chủ Binance có thể nhanh hơn đồng hồ local vài giây: chấp nhận lệch nhỏ và đẩy
# ``collected_at`` lên mốc nguồn muộn nhất để giữ bất biến "mọi mốc ≤ collected_at".
CLOCK_SKEW = timedelta(seconds=5)


class DerivativesContractError(ValueError):
    """Phản hồi Binance sai hợp đồng (thiếu trường, sai symbol, mốc ở tương lai...)."""


class SourceThrottled(RuntimeError):
    """Nguồn báo vượt giới hạn request; collector phải dừng cả lượt."""


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DerivativesContractError(f"{name} không phải số.") from error
    if not number.is_finite():
        raise DerivativesContractError(f"{name} không hữu hạn.")
    if positive and number <= 0:
        raise DerivativesContractError(f"{name} phải dương.")
    return number


def _instant(value: Any, name: str, *, not_after: datetime) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativesContractError(f"{name} phải là mili-giây nguyên.")
    moment = datetime.fromtimestamp(value / 1000, tz=UTC)
    if moment > not_after:
        raise DerivativesContractError(f"{name} nằm sau thời điểm nhận dữ liệu.")
    return moment


@dataclass(frozen=True, slots=True)
class DerivativesSnapshot:
    """Ảnh chụp phái sinh một symbol tại ``collected_at``; ``None`` = Binance không trả kỳ đó."""

    symbol: str
    collected_at: datetime
    mark_price: Decimal
    funding_rate: Decimal
    next_funding_time: datetime
    premium_time: datetime
    oi_contracts: Decimal | None = None
    oi_value_usdt: Decimal | None = None
    oi_at: datetime | None = None
    oi_value_24h_ago: Decimal | None = None
    long_short_account_ratio: Decimal | None = None
    ls_at: datetime | None = None
    top_position_ratio: Decimal | None = None
    top_at: datetime | None = None
    taker_buy_sell_ratio: Decimal | None = None
    taker_at: datetime | None = None

    @property
    def oi_change_24h(self) -> Decimal | None:
        """Thay đổi tương đối giá trị OI so với 24 giờ trước (0,05 = +5%)."""
        if self.oi_value_usdt is None or not self.oi_value_24h_ago:
            return None
        return self.oi_value_usdt / self.oi_value_24h_ago - 1

    def record(self) -> dict[str, Any]:
        """Các cột lưu DB (không gồm id/workspace/policy)."""
        return {
            "symbol": self.symbol,
            "collected_at": self.collected_at,
            "mark_price": self.mark_price,
            "funding_rate": self.funding_rate,
            "next_funding_time": self.next_funding_time,
            "premium_time": self.premium_time,
            "oi_contracts": self.oi_contracts,
            "oi_value_usdt": self.oi_value_usdt,
            "oi_at": self.oi_at,
            "oi_value_24h_ago": self.oi_value_24h_ago,
            "long_short_account_ratio": self.long_short_account_ratio,
            "ls_at": self.ls_at,
            "top_position_ratio": self.top_position_ratio,
            "top_at": self.top_at,
            "taker_buy_sell_ratio": self.taker_buy_sell_ratio,
            "taker_at": self.taker_at,
        }


def payload_hash(raw: Mapping[str, Any]) -> str:
    """SHA-256 của phản hồi thô đã chuẩn hóa (bằng chứng tái lập ảnh chụp)."""
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rows(value: Any, name: str, symbol: str | None) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise DerivativesContractError(f"{name} phải là mảng.")
    rows = []
    for item in value:
        if not isinstance(item, Mapping):
            raise DerivativesContractError(f"{name} có phần tử không phải object.")
        if symbol is not None and "symbol" in item and item["symbol"] != symbol:
            raise DerivativesContractError(f"{name}: symbol không khớp request.")
        rows.append(item)
    return sorted(rows, key=lambda row: row.get("timestamp", 0))


def _latest_ratio(
    value: Any, name: str, field: str, symbol: str, collected_at: datetime
) -> tuple[Decimal | None, datetime | None]:
    rows = _rows(value, name, symbol)
    if not rows:
        return None, None
    last = rows[-1]
    return (
        _decimal(last.get(field), f"{name}.{field}", positive=True),
        _instant(last.get("timestamp"), f"{name}.timestamp", not_after=collected_at),
    )


def build_snapshot(
    symbol: str, collected_at: datetime, raw: Mapping[str, Any]
) -> DerivativesSnapshot:
    """Phản hồi thô của 5 endpoint (khóa ``premium``, ``oi_hist``, ``global_ls``, ``top_ls``,
    ``taker``) → ảnh chụp đã kiểm hợp đồng. Thiếu ``premium`` là lỗi; các chỉ số còn lại có thể
    rỗng (kỳ không giao dịch) và thành ``None``."""
    if collected_at.tzinfo is None:
        raise ValueError("collected_at phải có múi giờ.")
    received_at = collected_at.astimezone(UTC)
    collected_at = received_at + CLOCK_SKEW  # giới hạn trên cho mốc nguồn
    premium = raw.get("premium")
    if not isinstance(premium, Mapping) or premium.get("symbol") != symbol:
        raise DerivativesContractError("premiumIndex thiếu hoặc sai symbol.")
    premium_time = _instant(premium.get("time"), "premiumIndex.time", not_after=collected_at)
    next_funding = premium.get("nextFundingTime")
    if isinstance(next_funding, bool) or not isinstance(next_funding, int):
        raise DerivativesContractError("nextFundingTime phải là mili-giây nguyên.")
    oi_rows = _rows(raw.get("oi_hist"), "openInterestHist", symbol)
    oi_contracts = oi_value = oi_ago = None
    oi_at = None
    if oi_rows:
        last = oi_rows[-1]
        oi_contracts = _decimal(last.get("sumOpenInterest"), "sumOpenInterest")
        oi_value = _decimal(last.get("sumOpenInterestValue"), "sumOpenInterestValue")
        oi_at = _instant(
            last.get("timestamp"), "openInterestHist.timestamp", not_after=collected_at
        )
        target = oi_at - timedelta(hours=24)
        exact = [r for r in oi_rows if r.get("timestamp") == int(target.timestamp() * 1000)]
        if exact:
            oi_ago = _decimal(exact[0].get("sumOpenInterestValue"), "sumOpenInterestValue")
        if oi_contracts < 0 or oi_value < 0:
            raise DerivativesContractError("Open interest không được âm.")
    ls, ls_at = _latest_ratio(
        raw.get("global_ls"), "globalLongShortAccountRatio", "longShortRatio", symbol, collected_at
    )
    top, top_at = _latest_ratio(
        raw.get("top_ls"), "topLongShortPositionRatio", "longShortRatio", symbol, collected_at
    )
    taker, taker_at = _latest_ratio(
        raw.get("taker"), "takerlongshortRatio", "buySellRatio", symbol, collected_at
    )
    moments = [m for m in (premium_time, oi_at, ls_at, top_at, taker_at) if m is not None]
    return DerivativesSnapshot(
        symbol=symbol,
        collected_at=max([received_at, *moments]),
        mark_price=_decimal(premium.get("markPrice"), "markPrice", positive=True),
        funding_rate=_decimal(premium.get("lastFundingRate"), "lastFundingRate"),
        next_funding_time=datetime.fromtimestamp(next_funding / 1000, tz=UTC),
        premium_time=premium_time,
        oi_contracts=oi_contracts,
        oi_value_usdt=oi_value,
        oi_at=oi_at,
        oi_value_24h_ago=oi_ago,
        long_short_account_ratio=ls,
        ls_at=ls_at,
        top_position_ratio=top,
        top_at=top_at,
        taker_buy_sell_ratio=taker,
        taker_at=taker_at,
    )


# ---------------------------------------------------------------------------
# Digest cho AI Analyst
# ---------------------------------------------------------------------------


def _text(value: Decimal | None, places: int) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal(1).scaleb(-places)))


def derivatives_digest(
    snapshots: Iterable[DerivativesSnapshot],
    now: datetime,
    *,
    priority_symbols: Sequence[str] = (),
    max_age: timedelta = DIGEST_MAX_AGE,
    max_rows: int = DIGEST_MAX_ROWS,
) -> dict[str, Any]:
    """Ảnh chụp mới nhất mỗi symbol (``collected_at ≤ now``, không cũ hơn ``max_age``).

    Số đã làm tròn sẵn thành chuỗi để AI trích nguyên văn: funding theo % mỗi kỳ (4 chữ số), thay
    đổi OI 24 giờ theo % (2 chữ số), giá trị OI theo USDT (số nguyên), tỷ lệ (4 chữ số). Symbol ưu
    tiên (coin bot đã giao dịch) đứng trước, sau đó theo |funding| giảm dần.
    """
    if now.tzinfo is None:
        raise ValueError("now phải có múi giờ.")
    instant = now.astimezone(UTC)
    latest: dict[str, DerivativesSnapshot] = {}
    for snap in snapshots:
        if snap.collected_at > instant or instant - snap.collected_at > max_age:
            continue
        current = latest.get(snap.symbol)
        if current is None or snap.collected_at > current.collected_at:
            latest[snap.symbol] = snap
    priority = {symbol: index for index, symbol in enumerate(priority_symbols)}
    ordered = sorted(
        latest.values(),
        key=lambda s: (priority.get(s.symbol, len(priority)), -abs(s.funding_rate), s.symbol),
    )
    rows = [
        {
            "symbol": snap.symbol,
            "funding_rate_pct": _text(snap.funding_rate * 100, 4),
            "oi_value_usdt": _text(snap.oi_value_usdt, 0),
            "oi_change_24h_pct": _text(
                snap.oi_change_24h * 100 if snap.oi_change_24h is not None else None, 2
            ),
            "long_short_account_ratio": _text(snap.long_short_account_ratio, 4),
            "top_trader_position_ratio": _text(snap.top_position_ratio, 4),
            "taker_buy_sell_ratio": _text(snap.taker_buy_sell_ratio, 4),
            "collected_at": snap.collected_at.isoformat(),
        }
        for snap in ordered[:max_rows]
    ]
    fundings = [snap.funding_rate * 100 for snap in latest.values()]
    return {
        "digest_version": DERIVATIVES_DIGEST_VERSION,
        "source_id": DERIVATIVES_SOURCE_ID,
        "as_of": instant.isoformat(),
        "period": STATS_PERIOD,
        "symbol_count": len(latest),
        "positive_funding_count": sum(1 for value in fundings if value > 0),
        "median_funding_rate_pct": _text(Decimal(median(fundings)), 4) if fundings else None,
        "symbols": rows,
    }


__all__ = [
    "DERIVATIVES_DIGEST_VERSION",
    "DERIVATIVES_SOURCE_ID",
    "DIGEST_MAX_AGE",
    "OI_LOOKBACK_PERIODS",
    "STATS_PERIOD",
    "DerivativesContractError",
    "DerivativesSnapshot",
    "SourceThrottled",
    "build_snapshot",
    "derivatives_digest",
    "payload_hash",
]
