"""FX-3 lát 1: thu mức funding **đã chốt** của Binance USDⓈ-M vào ``market_funding`` (CRY-198).

Nguồn: ``GET /fapi/v1/fundingRate`` (kiểm 17/11/2026,
https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)
trả ``fundingTime``, ``fundingRate``, ``markPrice`` (mark price gắn với lần thu funding đó) và
``rateType``. Đây là nguồn để ghi funding vào ledger PAPER; ``lastFundingRate`` của
``premiumIndex`` không được tài liệu nói rõ là mức đã chốt nên không dùng.

Chạy trong ``ingestion-worker`` cùng quyền dữ liệu của collector nến (Source Registry
``BINANCE_USDM_REST``, mục đích ``PAPER_DEMO``): rights gate chạy **trước** mọi request mạng,
thiếu quyền thì không tải (fail-closed). Mỗi symbol một cursor ``BINANCE_USDM_REST_FUNDING``;
mỗi tick chỉ đọc các kỳ sau cursor. Không cần API key.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.market.ingestion import IngestionCursor, MarketRecordVersion, MarketStream
from trading_bot.ports.market_data import FundingRateObservation, MarketDataPage

logger = logging.getLogger(__name__)

FUNDING_CURSOR_SOURCE = "BINANCE_USDM_REST_FUNDING"
# Cột ``source`` của bản ghi funding: cùng nhà cung cấp REST công khai với nến backfill.
FUNDING_RECORD_SOURCE = "BINANCE_USDM_REST"
INSTRUMENT_SOURCE = "BINANCE_USDM"
FUNDING_TIMEFRAME = "funding"
DEFAULT_LOOKBACK = timedelta(days=3)
PAGE_LIMIT = 100
_EPSILON = timedelta(milliseconds=1)


class FundingMarketData(Protocol):
    async def get_funding_rates(
        self,
        *,
        symbol: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> MarketDataPage[FundingRateObservation]: ...


@dataclass(frozen=True, slots=True)
class FundingSymbolResult:
    symbol: str
    status: str  # collected | up_to_date | rights_denied | error
    records: int = 0
    covered_until: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class FundingTick:
    results: tuple[FundingSymbolResult, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        return all(result.status in {"collected", "up_to_date"} for result in self.results)


def funding_record_version(
    workspace_id: UUID, observation: FundingRateObservation
) -> tuple[MarketRecordVersion, Decimal | None]:
    """Khóa/record id xác định theo (symbol, fundingTime); checksum gồm cả mark price và loại."""
    millis = int(observation.funding_time.timestamp() * 1000)
    key = f"funding:{observation.symbol}:{millis}"
    payload = {
        "symbol": observation.symbol,
        "funding_time": observation.funding_time.astimezone(UTC).isoformat(),
        "funding_rate": str(observation.funding_rate),
        "mark_price": None if observation.mark_price is None else str(observation.mark_price),
        "rate_type": observation.rate_type,
    }
    checksum = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    version = MarketRecordVersion(
        record_id=uuid5(NAMESPACE_URL, f"crypidian:{workspace_id}:{FUNDING_RECORD_SOURCE}:{key}"),
        stream=MarketStream(FUNDING_RECORD_SOURCE, observation.symbol, FUNDING_TIMEFRAME),
        logical_key=key,
        idempotency_key=key,
        revision=1,
        content_checksum=checksum,
        event_at=observation.funding_time,
        received_at=observation.received_at,
        available_at=observation.available_at,
    )
    return version, observation.mark_price


def _hour_floor(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class FundingRateCollector:
    """Thu funding đã chốt cho danh sách symbol; mỗi symbol một transaction riêng."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        symbols: Sequence[str],
        market_data: FundingMarketData,
        session_scope: Callable[[], Any],
        lookback: timedelta = DEFAULT_LOOKBACK,
        prune_interval: timedelta = timedelta(hours=1),
    ) -> None:
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        unique = tuple(dict.fromkeys(symbols))
        if not unique:
            raise ValueError("Collector funding cần ít nhất một symbol.")
        if not timedelta(hours=1) <= lookback <= timedelta(days=30):
            raise ValueError("lookback funding phải trong 1 giờ..30 ngày.")
        if prune_interval <= timedelta(0):
            raise ValueError("prune_interval phải dương.")
        self._workspace_id = workspace_id
        self._symbols = unique
        self._market_data = market_data
        self._session_scope = session_scope
        self._lookback = lookback
        self._prune_interval = prune_interval
        self._last_pruned_at: datetime | None = None

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    async def prune_expired(self, *, now: datetime) -> dict[str, int]:
        """Dọn các bản ghi hết hạn dưới RLS workspace khi đến chu kỳ."""
        instant = now.astimezone(UTC)
        async with self._session_scope() as session:
            prune_fn = getattr(session.market, "prune_expired_live_market", None)
            if not callable(prune_fn):
                return {}
            deleted = await prune_fn(workspace_id=self._workspace_id, now=instant)
            self._last_pruned_at = instant
            return dict(deleted)

    async def collect_once(self, now: datetime) -> FundingTick:
        instant = now.astimezone(UTC)
        results: list[FundingSymbolResult] = []
        for symbol in self._symbols:
            try:
                results.append(await self._collect_symbol(symbol, instant))
            except PermissionError as error:
                results.append(FundingSymbolResult(symbol, "rights_denied", detail=str(error)))
            except Exception as error:  # noqa: BLE001 - cô lập lỗi theo symbol
                logger.warning("funding collector lỗi %s: %s", symbol, type(error).__name__)
                results.append(FundingSymbolResult(symbol, "error", detail=type(error).__name__))
        if self._last_pruned_at is None or instant - self._last_pruned_at >= self._prune_interval:
            try:
                await self.prune_expired(now=instant)
            except Exception as error:  # noqa: BLE001 - dọn retention không chặn thu funding
                logger.warning("funding collector prune lỗi: %s", type(error).__name__)
        return FundingTick(tuple(results))

    async def _collect_symbol(self, symbol: str, now: datetime) -> FundingSymbolResult:
        stream = MarketStream(FUNDING_CURSOR_SOURCE, symbol, FUNDING_TIMEFRAME)
        async with self._session_scope() as session:
            market = session.market
            cursor = await market.get_cursor(
                workspace_id=self._workspace_id, stream=stream, as_of=now
            )
            start = cursor.covered_until if cursor is not None else now - self._lookback
            # Kiểm quyền nhà cung cấp trước khi gọi mạng; request khung 1h đã căn biên chỉ dùng
            # để gate nhận diện nguồn REST công khai và mục đích PAPER.
            request = RestBackfillRequest(
                symbol=symbol,
                interval="1h",
                start=_hour_floor(start),
                end=_hour_floor(now) + timedelta(hours=1),
                purpose=BackfillRightsPurpose.PAPER_DEMO,
            )
            await session.rights_gate.authorize(request=request, now=now)
            page = await self._market_data.get_funding_rates(
                symbol=symbol, start_time=start, end_time=now, limit=PAGE_LIMIT
            )
            settled = sorted(
                (item for item in page.records if start <= item.funding_time <= now),
                key=lambda item: item.funding_time,
            )
            if not settled:
                return FundingSymbolResult(symbol, "up_to_date", covered_until=start)
            instrument = await market.get_instrument(
                workspace_id=self._workspace_id,
                source=INSTRUMENT_SOURCE,
                symbol=symbol,
                as_of=now,
            )
            if instrument is None:
                raise LookupError(f"Chưa có quy cách hợp đồng {symbol} (collector nến tạo trước).")
            for item in settled:
                version, mark_price = funding_record_version(self._workspace_id, item)
                await market.append_funding_rate(
                    workspace_id=self._workspace_id,
                    instrument_id=UUID(str(instrument.id)),
                    version=version,
                    funding_time=item.funding_time,
                    funding_rate=item.funding_rate,
                    mark_price=mark_price,
                    rate_type=item.rate_type,
                )
            covered_until = settled[-1].funding_time + _EPSILON
            await market.save_cursor(
                workspace_id=self._workspace_id,
                cursor=IngestionCursor(
                    stream=stream,
                    token=json.dumps(
                        {"version": 1, "after": covered_until.isoformat()}, sort_keys=True
                    ),
                    covered_until=covered_until,
                    updated_at=now,
                ),
            )
            return FundingSymbolResult(
                symbol, "collected", records=len(settled), covered_until=covered_until
            )


async def run_funding_collector(
    collector: FundingRateCollector,
    stopping: Any,
    *,
    tick_seconds: float = 300.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_tick: Callable[[FundingTick], None] | None = None,
) -> None:
    """Lặp tới khi ``stopping`` được set; funding chốt 1–3 lần/ngày nên 5 phút là đủ sớm."""
    import asyncio

    while not stopping.is_set():
        tick = await collector.collect_once(clock())
        if on_tick is not None:
            on_tick(tick)
        stored = sum(result.records for result in tick.results)
        if stored or not tick.succeeded:
            logger.info(
                "funding collector: %d kỳ mới, %d symbol lỗi/thiếu quyền",
                stored,
                sum(result.status in {"rights_denied", "error"} for result in tick.results),
            )
        try:
            await asyncio.wait_for(stopping.wait(), timeout=tick_seconds)
        except TimeoutError:
            continue
