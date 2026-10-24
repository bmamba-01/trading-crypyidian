"""Cổng dữ liệu thị trường công khai, tách hợp đồng khỏi HTTP của Binance."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trading_bot.domain.market.ingestion import InstrumentObservation
from trading_bot.domain.market.models import OHLCVBar


@dataclass(frozen=True, slots=True)
class MarketDataPage[RecordT]:
    """Một phản hồi bất biến, giữ bytes nguồn và checksum để truy vết."""

    records: tuple[RecordT, ...]
    raw_payload: bytes
    checksum: str
    received_at: datetime
    available_at: datetime
    is_complete: bool = True
    missing_times: tuple[datetime, ...] = ()
    incomplete_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExchangeInstrument:
    """Quy cách instrument và metadata USDⓈ-M bổ sung từ exchangeInfo."""

    observation: InstrumentObservation
    contract_type: str
    margin_asset: str


@dataclass(frozen=True, slots=True)
class FundingRateObservation:
    """Một mức funding được sàn công bố cùng thời điểm nhận phản hồi."""

    symbol: str
    funding_time: datetime
    funding_rate: Decimal
    mark_price: Decimal | None
    rate_type: str
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class OpenInterestObservation:
    """Một quan sát open interest theo đơn vị hợp đồng và giá trị nếu có."""

    symbol: str
    observed_at: datetime
    quantity: Decimal
    value: Decimal | None
    received_at: datetime
    available_at: datetime


class MarketDataPort(Protocol):
    """Hợp đồng bất đồng bộ để đọc market data công khai."""

    async def get_exchange_info(self) -> MarketDataPage[ExchangeInstrument]: ...

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> MarketDataPage[OHLCVBar]: ...

    async def get_funding_rates(
        self,
        *,
        symbol: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> MarketDataPage[FundingRateObservation]: ...

    async def get_open_interest(self, symbol: str) -> MarketDataPage[OpenInterestObservation]: ...

    async def get_open_interest_history(
        self,
        *,
        symbol: str,
        period: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 30,
    ) -> MarketDataPage[OpenInterestObservation]: ...
