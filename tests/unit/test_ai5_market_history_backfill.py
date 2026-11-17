"""AI-5 (CRY-236): backfill lùi lịch sử nến — chia cửa sổ, mốc căn khung, trạng thái kết quả."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from trading_bot.application import market_history_backfill as module
from trading_bot.application.market_history_backfill import (
    MarketHistoryBackfill,
    align_down,
    plan_windows,
)

NOW = datetime(2026, 11, 17, 3, 25, tzinfo=UTC)
W = UUID(int=7)


def test_align_and_plan_windows() -> None:
    assert align_down(NOW, "4h") == datetime(2026, 11, 17, 0, tzinfo=UTC)
    assert align_down(NOW, "15m") == datetime(2026, 11, 17, 3, 15, tzinfo=UTC)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    windows = plan_windows(start, start + timedelta(hours=10), "1h", bars_per_window=4)
    assert windows == [
        (start, start + timedelta(hours=4)),
        (start + timedelta(hours=4), start + timedelta(hours=8)),
        (start + timedelta(hours=8), start + timedelta(hours=10)),
    ]
    # Không cửa sổ nào vượt 366 ngày dù bars_per_window rất lớn (giới hạn của request REST).
    long = plan_windows(start, start + timedelta(days=500), "4h", bars_per_window=100_000)
    assert all(end - begin <= timedelta(days=366) for begin, end in long)
    assert long[0][0] == start and long[-1][1] == start + timedelta(days=500)  # noqa: PT018
    with pytest.raises(ValueError, match="dương"):
        plan_windows(start, start + timedelta(hours=1), "1h", bars_per_window=0)


@dataclass
class _Market:
    appended: list[Any]
    instrument: Any = field(default_factory=lambda: SimpleNamespace(id=UUID(int=9)))

    async def get_instrument(self, **_: Any) -> Any:
        return self.instrument

    async def append_bar(self, **kwargs: Any) -> None:
        self.appended.append(kwargs["bar"])


def _runner_factory(bars_per_request: list[int], requests: list[Any]):
    class _Runner:
        def __init__(self, **_: Any) -> None:
            pass

        async def run_batch(self, request: Any, **_: Any) -> Any:
            requests.append(request)
            count = bars_per_request[min(len(requests) - 1, len(bars_per_request) - 1)]
            bars = [
                SimpleNamespace(
                    source="BINANCE_USDM_REST_BACKFILL",
                    symbol=request.symbol,
                    timeframe=request.interval,
                    open_time=request.start + i * request.interval_duration,
                )
                for i in range(count)
            ]
            return SimpleNamespace(bars=bars)

    return _Runner


def _backfill(market: _Market, earliest: datetime | None) -> MarketHistoryBackfill:
    @asynccontextmanager
    async def scope():
        yield SimpleNamespace(market=market, rights_gate=object())

    async def first(_session: Any, _symbol: str, _interval: str) -> datetime | None:
        return earliest

    return MarketHistoryBackfill(
        workspace_id=W,
        market_data=object(),
        session_scope=scope,
        earliest_open_time=first,
        page_limit=10,
        max_pages=1,
        pause_seconds=0,
    )


def test_backfill_fills_before_earliest_bar_with_backtest_purpose(monkeypatch) -> None:
    requests: list[Any] = []
    monkeypatch.setattr(module, "BinanceRestBackfillRunner", _runner_factory([10], requests))
    monkeypatch.setattr(module, "make_backfill_bar_version", lambda **kw: (kw["bar"], None))
    market = _Market([])
    earliest = datetime(2026, 11, 15, tzinfo=UTC)
    result = asyncio.run(_backfill(market, earliest).backfill("ATOMUSDT", "4h", days=10, now=NOW))
    assert result.status == "filled"
    assert result.requested_end == earliest  # không chồng lên nến đã có
    assert result.requested_start == datetime(2026, 11, 7, tzinfo=UTC)
    # 8 ngày = 48 nến 4h, mỗi cửa sổ tối đa 10 nến → 5 cửa sổ.
    assert result.windows == 5
    assert all(r.purpose.value == "BACKTEST_RESEARCH" for r in requests)
    assert result.bars_written == len(market.appended)


def test_backfill_statuses(monkeypatch) -> None:
    requests: list[Any] = []
    monkeypatch.setattr(module, "BinanceRestBackfillRunner", _runner_factory([0], requests))
    monkeypatch.setattr(module, "make_backfill_bar_version", lambda **kw: (kw["bar"], None))
    # Coin niêm yết muộn: Binance không trả nến cũ hơn → no_older_history, không lỗi.
    none = asyncio.run(
        _backfill(_Market([]), datetime(2026, 11, 15, tzinfo=UTC)).backfill(
            "BTWUSDT", "4h", days=10, now=NOW
        )
    )
    assert none.status == "no_older_history"
    # Đã đủ lịch sử → up_to_date, không gọi mạng.
    calls = len(requests)
    done = asyncio.run(
        _backfill(_Market([]), datetime(2025, 1, 1, tzinfo=UTC)).backfill(
            "BTCUSDT", "4h", days=10, now=NOW
        )
    )
    assert done.status == "up_to_date"
    assert len(requests) == calls
    # Thiếu instrument → lỗi theo luồng, có lý do.
    monkeypatch.setattr(module, "BinanceRestBackfillRunner", _runner_factory([3], requests))
    missing = asyncio.run(
        _backfill(_Market([], instrument=None), datetime(2026, 11, 15, tzinfo=UTC)).backfill(
            "NEWUSDT", "4h", days=10, now=NOW
        )
    )
    assert missing.status == "error"
    assert "instrument" in missing.detail
