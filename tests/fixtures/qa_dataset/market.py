"""Lớp dữ liệu thị trường tổng hợp (synthetic) cho BTCUSDT/ETHUSDT của workspace QA.

Nến được sinh tất định từ số nguyên rồi đổi sang Decimal, không lấy từ sàn. Chuỗi 1h nền
dài 880 giờ: lưu 240 nến 1h cuối và 220 nến 4h tổng hợp từ cùng chuỗi, nên nến 4h trùng giai
đoạn khớp đúng OHLCV của nến 1h. BTCUSDT kết thúc bằng một nến phá kênh Donchian 55 nến
(ứng viên H03); ETHUSDT dao động trong biên độ (không có tín hiệu phá kênh).

Mốc thời gian cố định trong quá khứ nên runtime live sẽ coi dữ liệu là cũ (fail-closed);
fixture phục vụ replay, kiểm thử RLS/scope và kiểm thử thủ công các màn hình đọc dữ liệu.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tests.fixtures.qa_dataset.identity import (
    DATASET_VERSION,
    MARKET_END,
    QA_WORKSPACE_KEY,
    qa_id,
)

from trading_bot.domain.market.ingestion import (
    InstrumentObservation,
    MarketRecordVersion,
    MarketStream,
)
from trading_bot.domain.market.models import Instrument, OHLCVBar

# Nguồn của nến khác nguồn sàn thật để không nhầm fixture với dữ liệu Binance đã thu thập.
FIXTURE_SOURCE = "CRYPIDIAN_QA_FIXTURE"
# Runtime tra instrument theo nguồn `BINANCE_USDM`; quy cách ở đây là giá trị mô phỏng.
INSTRUMENT_SOURCE = "BINANCE_USDM"
HOURLY_BAR_COUNT = 240
FOUR_HOUR_BAR_COUNT = 220
_TOTAL_HOURS = FOUR_HOUR_BAR_COUNT * 4
MARKET_START = MARKET_END - timedelta(hours=_TOTAL_HOURS)
_RECEIVE_DELAY = timedelta(milliseconds=1)
_AVAILABLE_DELAY = timedelta(milliseconds=2)


@dataclass(frozen=True, slots=True)
class QaInstrument:
    """Quan sát quy cách hợp đồng kèm khóa idempotency của dataset."""

    workspace_key: str
    idempotency_key: str
    observation: InstrumentObservation
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class QaBar:
    """Một nến đã đóng và phiên bản bản ghi (revision 1) dùng cho repository thị trường."""

    workspace_key: str
    bar: OHLCVBar
    version: MarketRecordVersion


@dataclass(frozen=True, slots=True)
class _SeriesProfile:
    base: int
    drift: int
    period: int
    amplitude: int
    wick: int
    volume_base: int
    volume_step: int
    volume_cycle: int
    breakout: bool


@dataclass(frozen=True, slots=True)
class _Candle:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


_INSTRUMENTS = {
    "BTCUSDT": Instrument(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_notional=Decimal("5"),
    ),
    "ETHUSDT": Instrument(
        symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        quantity_step=Decimal("0.001"),
        min_notional=Decimal("5"),
    ),
}
# Biên độ chia hết cho nửa chu kỳ để giá luôn là số nguyên, không phát sinh làm tròn.
_PROFILES = {
    "BTCUSDT": _SeriesProfile(60000, 2, 48, 600, 40, 100, 5, 7, breakout=True),
    "ETHUSDT": _SeriesProfile(2500, 0, 24, 36, 3, 1000, 20, 5, breakout=False),
}


def _triangle(index: int, period: int, amplitude: int) -> int:
    """Sóng tam giác nguyên trong đoạn [-amplitude, amplitude]."""
    half = period // 2
    phase = index % period
    distance = phase if phase <= half else period - phase
    return amplitude * (2 * distance - half) // half


def _hourly_candles(profile: _SeriesProfile) -> list[_Candle]:
    candles: list[_Candle] = []
    previous_close = Decimal(profile.base + _triangle(0, profile.period, profile.amplitude))
    wick = Decimal(profile.wick)
    for index in range(_TOTAL_HOURS):
        close = Decimal(
            profile.base
            + profile.drift * index
            + _triangle(index, profile.period, profile.amplitude)
        )
        open_ = previous_close
        candles.append(
            _Candle(
                open=open_,
                high=max(open_, close) + wick,
                low=min(open_, close) - wick,
                close=close,
                volume=Decimal(
                    profile.volume_base + (index % profile.volume_cycle) * profile.volume_step
                ),
            )
        )
        previous_close = close
    if profile.breakout:
        open_ = candles[-2].close
        close = max(candle.high for candle in candles[-56:-1]) + Decimal(200)
        candles[-1] = _Candle(
            open=open_,
            high=close + Decimal(50),
            low=min(open_, close) - wick,
            close=close,
            volume=Decimal(profile.volume_base * 4),
        )
    return candles


def _aggregate(parts: list[_Candle]) -> _Candle:
    return _Candle(
        open=parts[0].open,
        high=max(part.high for part in parts),
        low=min(part.low for part in parts),
        close=parts[-1].close,
        volume=sum((part.volume for part in parts), Decimal(0)),
    )


def _bar(
    symbol: str, timeframe: str, open_time: datetime, step: timedelta, candle: _Candle
) -> QaBar:
    close_time = open_time + step
    received_at = close_time + _RECEIVE_DELAY
    available_at = close_time + _AVAILABLE_DELAY
    logical_key = f"{symbol}:{timeframe}:{open_time.isoformat()}"
    content = "|".join(
        (
            DATASET_VERSION,
            logical_key,
            str(candle.open),
            str(candle.high),
            str(candle.low),
            str(candle.close),
            str(candle.volume),
        )
    )
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    bar = OHLCVBar(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        close_time=close_time,
        event_at=close_time,
        received_at=received_at,
        available_at=available_at,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        is_closed=True,
        source=FIXTURE_SOURCE,
        source_cursor=f"qa-fixture:{logical_key}",
        checksum=checksum,
    )
    version = MarketRecordVersion(
        record_id=qa_id("market_bar", logical_key),
        stream=MarketStream(FIXTURE_SOURCE, symbol, timeframe),
        logical_key=logical_key,
        idempotency_key=f"qa-fixture:{logical_key}",
        revision=1,
        content_checksum=checksum,
        event_at=close_time,
        received_at=received_at,
        available_at=available_at,
    )
    return QaBar(workspace_key=QA_WORKSPACE_KEY, bar=bar, version=version)


def build_instruments() -> tuple[QaInstrument, ...]:
    observed_at = MARKET_START - timedelta(hours=1)
    return tuple(
        QaInstrument(
            workspace_key=QA_WORKSPACE_KEY,
            idempotency_key=f"qa-fixture:exchange-info:{symbol}",
            observation=InstrumentObservation(
                instrument=instrument, source=INSTRUMENT_SOURCE, observed_at=observed_at
            ),
            received_at=observed_at + _RECEIVE_DELAY,
            available_at=observed_at + _AVAILABLE_DELAY,
        )
        for symbol, instrument in _INSTRUMENTS.items()
    )


def build_bars() -> tuple[QaBar, ...]:
    """Nến 1h rồi 4h theo từng symbol, sắp xếp tăng dần theo open_time."""
    bars: list[QaBar] = []
    hour = timedelta(hours=1)
    four_hours = timedelta(hours=4)
    for symbol, profile in _PROFILES.items():
        candles = _hourly_candles(profile)
        first_hourly = _TOTAL_HOURS - HOURLY_BAR_COUNT
        for index in range(first_hourly, _TOTAL_HOURS):
            bars.append(_bar(symbol, "1h", MARKET_START + hour * index, hour, candles[index]))
        for index in range(FOUR_HOUR_BAR_COUNT):
            parts = candles[index * 4 : index * 4 + 4]
            bars.append(
                _bar(symbol, "4h", MARKET_START + four_hours * index, four_hours, _aggregate(parts))
            )
    return tuple(bars)
