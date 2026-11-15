"""SIG-0: thu nến đã đóng Binance USDⓈ-M liên tục cho PAPER nội bộ (chỉ dữ liệu công khai).

Owner duyệt ngày 15/11/2026 cho phép lưu dữ liệu thị trường công khai của Binance để dùng
trong PAPER nội bộ. Quyền này vẫn phải được ghi thành policy ``BINANCE_USDM_REST`` có mục đích
``PAPER_DEMO`` trong Source Registry: mọi lần tải đi qua ``SourceRegistryBackfillRightsGate``
**trước** khi gọi mạng, thiếu/hết hạn/thu hồi quyền thì không tải (fail-closed).

Mỗi luồng (symbol, khung) có cursor riêng ``BINANCE_USDM_REST_COLLECTOR``. Mỗi tick chỉ tải các
nến đã đóng sau cursor, tái dùng ``BinanceRestBackfillRunner`` để kiểm hợp lệ từng nến, rồi ghi
nến + cursor trong cùng một transaction. Không gọi API giao dịch, không cần API key.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsGate,
    BackfillRightsPurpose,
    BinanceRestBackfillRunner,
    RestBackfillRequest,
    RestKlineProvider,
)
from trading_bot.application.binance_rest_backfill_ingestion import make_backfill_bar_version
from trading_bot.domain.market.ingestion import (
    IngestionCursor,
    InstrumentObservation,
    MarketStream,
)

logger = logging.getLogger(__name__)

COLLECTOR_CURSOR_SOURCE = "BINANCE_USDM_REST_COLLECTOR"
INSTRUMENT_SOURCE = "BINANCE_USDM"
COLLECTOR_INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}
# Chờ thêm sau mốc đóng nến để REST đã phát hành nến cuối (tránh coi nến chưa có là thiếu).
CLOSE_SETTLE = timedelta(seconds=3)
# Adapter đóng dấu received_at của trang REST khi nhận phản hồi, tức là *sau* lúc tick bắt đầu.
# Runner từ chối trang "nhận trong tương lai" so với mốc ``now`` truyền vào, nên mốc kiểm hợp lệ
# là đầu tick + khoảng cho phép tải. Khoảng request vẫn tính từ đầu tick nên chỉ gồm nến đã đóng.
PAGE_RECEIPT_ALLOWANCE = timedelta(seconds=60)
_SYMBOL = re.compile(r"[A-Z0-9]{3,32}")


@dataclass(frozen=True, slots=True)
class CollectorStream:
    """Một cặp mã/khung cần thu."""

    symbol: str
    interval: str

    def __post_init__(self) -> None:
        if _SYMBOL.fullmatch(self.symbol) is None:
            raise ValueError("symbol collector phải là mã USD-M viết hoa.")
        if self.interval not in COLLECTOR_INTERVALS:
            raise ValueError("interval collector không được hỗ trợ.")


def parse_collector_streams(value: str) -> tuple[CollectorStream, ...]:
    """Đọc cấu hình dạng ``BTCUSDT:1m,5m;ETHUSDT:15m``; trùng lặp bị loại, thứ tự giữ nguyên."""
    streams: list[CollectorStream] = []
    for group in (part.strip() for part in value.split(";")):
        if not group:
            continue
        symbol, separator, intervals = group.partition(":")
        if not separator or not intervals.strip():
            raise ValueError("Mỗi nhóm collector phải có dạng SYMBOL:khung1,khung2.")
        for interval in (item.strip() for item in intervals.split(",")):
            stream = CollectorStream(symbol.strip(), interval)
            if stream not in streams:
                streams.append(stream)
    if not streams:
        raise ValueError("Cấu hình collector không có luồng nào.")
    if len(streams) > 60:
        raise ValueError("Collector tối đa 60 luồng để giữ trong hạn mức request.")
    return tuple(streams)


def last_closed_boundary(now: datetime, interval: str) -> datetime:
    """Mốc kết thúc (không bao gồm) của nến cuối đã đóng và đã qua thời gian chờ phát hành."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now phải có múi giờ.")
    duration = COLLECTOR_INTERVALS[interval]
    settled = now.astimezone(UTC) - CLOSE_SETTLE
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    steps = (settled - epoch) // duration
    return epoch + duration * steps


class CollectorRepository(Protocol):
    async def get_cursor(
        self, *, workspace_id: UUID, stream: MarketStream, as_of: datetime
    ) -> IngestionCursor | None: ...

    async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None: ...

    async def get_instrument(
        self, *, workspace_id: UUID, source: str, symbol: str, as_of: datetime
    ) -> Any: ...

    async def append_instrument(
        self,
        *,
        workspace_id: UUID,
        observation: InstrumentObservation,
        idempotency_key: str,
        received_at: datetime | None = None,
        available_at: datetime | None = None,
    ) -> Any: ...

    async def append_bar(self, **kwargs: Any) -> Any: ...


class CollectorMarketData(RestKlineProvider, Protocol):
    async def get_exchange_info(self) -> Any: ...


class CollectorSession(Protocol):
    """Một transaction workspace-scoped cung cấp repository thị trường và rights gate."""

    market: CollectorRepository
    rights_gate: BackfillRightsGate


@dataclass(frozen=True, slots=True)
class StreamResult:
    symbol: str
    interval: str
    status: str
    bars: int = 0
    missing: int = 0
    covered_until: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorTick:
    results: tuple[StreamResult, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        """Tick đạt khi không luồng nào lỗi (thiếu quyền tính là lỗi để readiness thấy)."""
        return all(result.status in {"collected", "up_to_date"} for result in self.results)


class MarketBarCollector:
    """Thu nến đã đóng cho các luồng cấu hình; mỗi luồng một transaction riêng."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        streams: Sequence[CollectorStream],
        market_data: CollectorMarketData,
        session_scope: Callable[[], Any],
        initial_bars: int = 1000,
        page_limit: int = 1000,
        max_pages: int = 2,
    ) -> None:
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if not streams:
            raise ValueError("Collector cần ít nhất một luồng.")
        if not 1 <= initial_bars <= 1500 * 10:
            raise ValueError("initial_bars phải trong 1..15000.")
        if not 1 <= page_limit <= 1500 or not 1 <= max_pages <= 10:
            raise ValueError("page_limit/max_pages không hợp lệ.")
        self._workspace_id = workspace_id
        self._streams = tuple(streams)
        self._market_data = market_data
        self._session_scope = session_scope
        self._initial_bars = initial_bars
        self._page_limit = page_limit
        self._max_pages = max_pages

    @property
    def streams(self) -> tuple[CollectorStream, ...]:
        return self._streams

    async def collect_once(self, now: datetime) -> CollectorTick:
        """Một lượt thu cho mọi luồng; lỗi một luồng không chặn luồng khác."""
        results: list[StreamResult] = []
        for stream in self._streams:
            try:
                results.append(await self._collect_stream(stream, now))
            except PermissionError as error:
                # Thiếu/hết hạn quyền: không tải. Không log chi tiết policy.
                results.append(
                    StreamResult(stream.symbol, stream.interval, "rights_denied", detail=str(error))
                )
            except Exception as error:  # noqa: BLE001 - cô lập lỗi theo luồng, ghi loại lỗi
                logger.warning(
                    "market collector lỗi luồng %s %s: %s",
                    stream.symbol,
                    stream.interval,
                    type(error).__name__,
                )
                results.append(
                    StreamResult(
                        stream.symbol, stream.interval, "error", detail=type(error).__name__
                    )
                )
        return CollectorTick(tuple(results))

    async def _collect_stream(self, stream: CollectorStream, now: datetime) -> StreamResult:
        instant = now.astimezone(UTC)
        duration = COLLECTOR_INTERVALS[stream.interval]
        end = last_closed_boundary(instant, stream.interval)
        cursor_stream = MarketStream(COLLECTOR_CURSOR_SOURCE, stream.symbol, stream.interval)
        async with self._session_scope() as session:
            market = session.market
            cursor = await market.get_cursor(
                workspace_id=self._workspace_id, stream=cursor_stream, as_of=instant
            )
            start = (
                cursor.covered_until if cursor is not None else end - duration * self._initial_bars
            )
            if start >= end:
                return StreamResult(
                    stream.symbol, stream.interval, "up_to_date", covered_until=start
                )
            window_end = min(end, start + duration * self._page_limit * self._max_pages)
            request = RestBackfillRequest(
                symbol=stream.symbol,
                interval=stream.interval,
                start=start,
                end=window_end,
                purpose=BackfillRightsPurpose.PAPER_DEMO,
                page_limit=self._page_limit,
            )
            runner = BinanceRestBackfillRunner(
                market_data=self._market_data, rights_gate=session.rights_gate
            )
            # Rights gate chạy trong runner trước mọi request mạng;
            # instrument chỉ tra sau khi đã có nến.
            batch = await runner.run_batch(
                request, now=instant + PAGE_RECEIPT_ALLOWANCE, max_pages=self._max_pages
            )
            if batch.bars:
                instrument_id = await self._instrument_id(market, stream.symbol, instant)
            for bar in batch.bars:
                versioned_bar, version = make_backfill_bar_version(
                    workspace_id=self._workspace_id, bar=bar
                )
                await market.append_bar(
                    workspace_id=self._workspace_id,
                    instrument_id=instrument_id,
                    bar=versioned_bar,
                    version=version,
                )
            # Chỉ tiến cursor tới sau nến cuối thực nhận; nến chưa phát hành sẽ được lấy lượt sau.
            covered_until = batch.bars[-1].open_time + duration if batch.bars else start
            missing = int((covered_until - start) / duration) - len(batch.bars)
            if covered_until > start:
                await market.save_cursor(
                    workspace_id=self._workspace_id,
                    cursor=IngestionCursor(
                        stream=cursor_stream,
                        token=json.dumps(
                            {"version": 1, "next_open_time": covered_until.isoformat()},
                            sort_keys=True,
                        ),
                        covered_until=covered_until,
                        updated_at=instant,
                    ),
                )
            return StreamResult(
                stream.symbol,
                stream.interval,
                "collected" if batch.bars else "up_to_date",
                bars=len(batch.bars),
                missing=max(missing, 0),
                covered_until=covered_until,
            )

    async def _instrument_id(self, market: CollectorRepository, symbol: str, now: datetime) -> UUID:
        row = await market.get_instrument(
            workspace_id=self._workspace_id, source=INSTRUMENT_SOURCE, symbol=symbol, as_of=now
        )
        if row is not None:
            return UUID(str(row.id))
        page = await self._market_data.get_exchange_info()
        record = next(
            (item for item in page.records if item.observation.instrument.symbol == symbol), None
        )
        if record is None:
            raise LookupError(f"Binance không có hợp đồng {symbol}.")
        observation = record.observation
        result = await market.append_instrument(
            workspace_id=self._workspace_id,
            observation=observation,
            idempotency_key=f"collector:{symbol}:{observation.observed_at.isoformat()}",
            received_at=page.received_at,
            available_at=page.available_at,
        )
        return UUID(str(result.record_id))
