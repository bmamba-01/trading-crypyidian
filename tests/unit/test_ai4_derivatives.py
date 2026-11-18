"""AI-4 lát 2: ảnh chụp phái sinh Binance (funding, OI, long/short, taker) và digest AI."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from trading_bot.domain.research.derivatives import (
    DERIVATIVES_SOURCE_ID,
    DerivativesContractError,
    SourceThrottled,
    build_snapshot,
    derivatives_digest,
)

COLLECTED = datetime(2026, 11, 16, 8, 6, 40, tzinfo=UTC)
T08 = 1794816000000  # 2026-11-16 08:00 UTC
HOUR = 3_600_000


def _raw(symbol: str = "BTCUSDT", *, oi_now: str = "6061044037.16", **override: Any) -> dict:
    # Định dạng lấy từ phản hồi thật của Binance ngày 16/11/2026.
    raw = {
        "premium": {
            "symbol": symbol,
            "markPrice": "64772.00000000",
            "indexPrice": "64793.31673913",
            "lastFundingRate": "0.00000489",
            "interestRate": "0.00010000",
            "nextFundingTime": 1794844800000,
            "time": 1794816400000,
        },
        "oi_hist": [
            {
                "symbol": symbol,
                "sumOpenInterest": "93000",
                "sumOpenInterestValue": "5772422892.53",
                "timestamp": T08 - 24 * HOUR,
            },
            {
                "symbol": symbol,
                "sumOpenInterest": "93300.563",
                "sumOpenInterestValue": oi_now,
                "timestamp": T08,
            },
        ],
        "global_ls": [
            {
                "symbol": symbol,
                "longAccount": "0.7313",
                "longShortRatio": "2.7216",
                "shortAccount": "0.2687",
                "timestamp": T08,
            }
        ],
        "top_ls": [{"symbol": symbol, "longShortRatio": "1.7403", "timestamp": T08}],
        "taker": [
            {"buySellRatio": "1.0619", "sellVol": "3551", "buyVol": "3770", "timestamp": T08 - HOUR}
        ],
    }
    raw.update(override)
    return raw


def test_snapshot_parses_real_binance_shapes() -> None:
    snap = build_snapshot("BTCUSDT", COLLECTED, _raw())
    assert snap.funding_rate == Decimal("0.00000489")
    assert snap.mark_price == Decimal("64772.00000000")
    assert snap.oi_at == datetime(2026, 11, 16, 8, tzinfo=UTC)
    assert snap.long_short_account_ratio == Decimal("2.7216")
    assert snap.top_position_ratio == Decimal("1.7403")
    # Taker trễ một kỳ: giữ mốc riêng.
    assert snap.taker_at == datetime(2026, 11, 16, 7, tzinfo=UTC)
    assert snap.oi_change_24h is not None
    assert round(snap.oi_change_24h, 4) == Decimal("0.0500")
    # Kỳ không giao dịch (mảng rỗng) → None, không lỗi.
    empty = build_snapshot("BTCUSDT", COLLECTED, _raw(global_ls=[], taker=[], oi_hist=[]))
    assert empty.long_short_account_ratio is None
    assert empty.oi_change_24h is None


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"premium": {"symbol": "ETHUSDT"}}, "premiumIndex"),
        ({"global_ls": [{"symbol": "ETHUSDT", "longShortRatio": "1", "timestamp": T08}]}, "symbol"),
        ({"top_ls": [{"longShortRatio": "1", "timestamp": T08 + 2 * HOUR}]}, "sau thời điểm"),
        ({"taker": [{"buySellRatio": "-1", "timestamp": T08}]}, "dương"),
        ({"taker": [{"buySellRatio": "NaN", "timestamp": T08}]}, "hữu hạn"),
        ({"oi_hist": {"bad": 1}}, "mảng"),
    ],
)
def test_snapshot_rejects_contract_violations(override: dict, message: str) -> None:
    with pytest.raises(DerivativesContractError, match=message):
        build_snapshot("BTCUSDT", COLLECTED, _raw(**override))


def test_small_clock_skew_moves_collected_at_to_source_time() -> None:
    early = COLLECTED - timedelta(seconds=3)
    snap = build_snapshot("BTCUSDT", early, _raw())
    assert snap.collected_at == snap.premium_time  # premium 08:06:40 > đồng hồ local 08:06:37
    with pytest.raises(DerivativesContractError, match="sau thời điểm"):
        build_snapshot("BTCUSDT", COLLECTED - timedelta(seconds=10), _raw())


def test_missing_24h_point_leaves_change_unknown() -> None:
    raw = _raw()
    raw["oi_hist"] = raw["oi_hist"][1:]
    assert build_snapshot("BTCUSDT", COLLECTED, raw).oi_change_24h is None


def test_digest_rounds_orders_and_drops_stale_snapshots() -> None:
    btc = build_snapshot("BTCUSDT", COLLECTED, _raw())
    hot_raw = _raw("PUMPUSDT")
    hot_raw["premium"] = {**hot_raw["premium"], "lastFundingRate": "-0.00250000"}
    hot = build_snapshot("PUMPUSDT", COLLECTED, hot_raw)
    old = replace(btc, symbol="ZECUSDT", collected_at=COLLECTED - timedelta(hours=4))
    digest = derivatives_digest([btc, hot, old], COLLECTED, priority_symbols=["BTCUSDT"])
    assert [row["symbol"] for row in digest["symbols"]] == ["BTCUSDT", "PUMPUSDT"]
    first = digest["symbols"][0]
    assert first["funding_rate_pct"] == "0.0005"
    assert first["oi_change_24h_pct"] == "5.00"
    assert first["oi_value_usdt"] == "6061044037"
    assert first["long_short_account_ratio"] == "2.7216"
    assert digest["symbols"][1]["funding_rate_pct"] == "-0.2500"
    assert digest["symbol_count"] == 2
    assert digest["positive_funding_count"] == 1
    # Không nhìn trước: ảnh chụp sau thời điểm báo cáo bị bỏ.
    assert derivatives_digest([btc], COLLECTED - timedelta(seconds=1))["symbols"] == []
    # Không có symbol ưu tiên: sắp theo |funding| giảm dần.
    assert derivatives_digest([btc, hot], COLLECTED)["symbols"][0]["symbol"] == "PUMPUSDT"


# --- Collector ------------------------------------------------------------------------------


class _Evidence:
    policy_fingerprint = "e" * 64


class _Gate:
    def __init__(self, allowed: list[bool]) -> None:
        self.allowed = allowed
        self.calls = 0

    async def authorize(self, purpose: Any) -> _Evidence:
        from trading_bot.domain.research.source_policy import SourceUsePurpose

        assert purpose is SourceUsePurpose.PAPER_DEMO
        ok = self.allowed[min(self.calls, len(self.allowed) - 1)]
        self.calls += 1
        if not ok:
            raise PermissionError("Nguồn chưa có policy.")
        return _Evidence()


class _Store:
    def __init__(self, allowed: list[bool], last: datetime | None = None) -> None:
        self.gate_obj = _Gate(allowed)
        self.last = last
        self.rows: list[dict] = []

    def gate(self, source_id: str) -> _Gate:
        assert source_id == DERIVATIVES_SOURCE_ID
        return self.gate_obj

    async def last_collected_at(self) -> datetime | None:
        return self.last

    async def insert(self, record: dict) -> None:
        self.rows.append(record)


class _Fetcher:
    def __init__(self, fail: dict[str, Exception] | None = None) -> None:
        self.fail = fail or {}
        self.calls: list[str] = []

    async def fetch_symbol(self, symbol: str) -> dict:
        self.calls.append(symbol)
        if symbol in self.fail:
            raise self.fail[symbol]
        return _raw(symbol)


def _collector(store: _Store, fetcher: _Fetcher, symbols: tuple[str, ...]) -> Any:
    from trading_bot.application.derivatives_collector import DerivativesCollector

    @asynccontextmanager
    async def scope():
        yield store

    return DerivativesCollector(
        workspace_id=UUID(int=1),
        symbols=symbols,
        fetcher=fetcher,
        store_scope=scope,
        clock=lambda: COLLECTED,
    )


def test_collector_needs_rights_and_isolates_symbol_failures() -> None:
    denied = _Store([False])
    fetcher = _Fetcher()
    result = asyncio.run(_collector(denied, fetcher, ("BTCUSDT",)).collect_once(COLLECTED))
    assert result.status == "rights_denied"
    assert fetcher.calls == []  # thiếu quyền thì không gọi Binance

    store = _Store([True])
    fetcher = _Fetcher({"ETHUSDT": RuntimeError("HTTP 500")})
    result = asyncio.run(
        _collector(store, fetcher, ("BTCUSDT", "ETHUSDT", "BTCUSDT")).collect_once(COLLECTED)
    )
    assert (result.status, result.stored, result.failed) == ("stored", 1, ("ETHUSDT",))
    assert fetcher.calls == ["BTCUSDT", "ETHUSDT"]  # bỏ trùng
    row = store.rows[0]
    assert row["symbol"] == "BTCUSDT"
    assert row["policy_fingerprint"] == "e" * 64
    assert len(row["payload_sha256"]) == 64
    assert store.gate_obj.calls == 2  # kiểm quyền trước khi tải và trước khi lưu


def test_collector_skips_recent_stops_on_throttle_and_rechecks_rights() -> None:
    recent = _Store([True], last=COLLECTED - timedelta(minutes=10))
    assert asyncio.run(
        _collector(recent, _Fetcher(), ("BTCUSDT",)).collect_once(COLLECTED)
    ).status == ("skipped_recent")
    throttled = _Store([True])
    fetcher = _Fetcher({"BTCUSDT": SourceThrottled("HTTP 429")})
    result = asyncio.run(
        _collector(throttled, fetcher, ("BTCUSDT", "ETHUSDT")).collect_once(COLLECTED)
    )
    assert result.status == "throttled"
    assert fetcher.calls == ["BTCUSDT"]
    assert throttled.rows == []
    revoked = _Store([True, False])
    result = asyncio.run(_collector(revoked, _Fetcher(), ("BTCUSDT",)).collect_once(COLLECTED))
    assert result.status == "rights_denied"
    assert revoked.rows == []


def test_fetcher_calls_only_public_endpoints_with_fixed_params() -> None:
    import httpx2

    from trading_bot.adapters.sources.binance_derivatives_http import (
        BinanceDerivativesHttpFetcher,
        BinanceThrottled,
    )

    seen: list[tuple[str, dict]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.path, dict(request.url.params)))
        assert request.url.host == "fapi.binance.com"
        assert "x-mbx-apikey" not in {key.lower() for key in request.headers}
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx2.Response(200, json=_raw()["premium"])
        if request.url.path == "/fapi/v1/ticker/24hr":
            return httpx2.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "lastPrice": "64770.00",
                    "quoteVolume": "123456789.12",
                    "priceChangePercent": "1.25",
                    "closeTime": T08,
                },
            )
        return httpx2.Response(200, json=[])

    async def run() -> dict:
        fetcher = BinanceDerivativesHttpFetcher(
            transport=httpx2.MockTransport(handler), pause_seconds=0
        )
        try:
            return await fetcher.fetch_symbol("BTCUSDT")
        finally:
            await fetcher.aclose()

    raw = asyncio.run(run())
    assert set(raw) == {"premium", "ticker_24h", "oi_hist", "global_ls", "top_ls", "taker", "basis"}
    assert seen[0] == ("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
    assert seen[1] == ("/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"})
    assert seen[2] == (
        "/futures/data/openInterestHist",
        {"symbol": "BTCUSDT", "period": "1h", "limit": "25"},
    )
    assert [path for path, _ in seen[3:]] == [
        "/futures/data/globalLongShortAccountRatio",
        "/futures/data/topLongShortPositionRatio",
        "/futures/data/takerlongshortRatio",
        "/futures/data/basis",
    ]

    async def throttled() -> None:
        fetcher = BinanceDerivativesHttpFetcher(
            transport=httpx2.MockTransport(lambda _: httpx2.Response(429)), pause_seconds=0
        )
        try:
            await fetcher.fetch_symbol("BTCUSDT")
        finally:
            await fetcher.aclose()

    with pytest.raises(BinanceThrottled):
        asyncio.run(throttled())


def test_setting_only_allowed_in_ingestion_worker() -> None:
    from trading_bot.config.settings import AppSettings

    with pytest.raises(ValueError, match="derivatives collector"):
        AppSettings(process_role="admin-web", derivatives_collector_enabled=True)


def test_analyst_topic_and_validator_use_derivatives_numbers() -> None:
    from trading_bot.application.ai_analyst import build_prompt, topic_report, validate_analysis

    snap = build_snapshot("BTCUSDT", COLLECTED, _raw())
    report = {
        "report_hash": "a" * 64,
        "window": {"start": "2026-11-15T08:00:00+00:00", "end": "2026-11-16T08:06:40+00:00"},
        "bots": [
            {
                "bot_name": "T01",
                "config": {"strategy_family": "H03", "timeframe": "1h"},
                "funnel": {"candidates": 0},
                "performance": {"net_pnl_usdt": "0", "closed_trades": 0},
            }
        ],
        "derivatives": derivatives_digest([snap], COLLECTED),
    }
    topic = topic_report(report, "H03")
    assert topic["derivatives"] == report["derivatives"]
    assert "taker_buy_sell_ratio" in build_prompt(topic)
    ref = ["/derivatives/symbols/0/oi_change_24h_pct", "/derivatives/symbols/0/funding_rate_pct"]
    good = {
        "summary": "Tóm tắt.",
        "findings": [{"statement": "OI BTC tăng 5.00%, funding 0.0005%.", "evidence_refs": ref}],
        "hypotheses": [],
        "proposals": [],
        "confidence": "low",
    }
    assert validate_analysis(good, topic).ok
    good["findings"][0]["statement"] = "OI BTC tăng 7.00%."
    assert not validate_analysis(good, topic).ok


def test_snapshot_parses_basis_and_24h_ticker_and_rejects_bad_contracts() -> None:
    """DATA-1: parse basis + 24h ticker theo shape Binance và kiểm contract."""
    payload = _raw(
        basis=[
            {
                "pair": "BTCUSDT",
                "contractType": "PERPETUAL",
                "futuresPrice": "64772.0",
                "indexPrice": "64793.31673913",
                "basis": "-21.31673913",
                "basisRate": "-0.00032900",
                "timestamp": T08,
            }
        ],
        ticker_24h={
            "symbol": "BTCUSDT",
            "lastPrice": "64770.50",
            "quoteVolume": "987654321.50",
            "priceChangePercent": "2.45",
            "closeTime": T08,
        },
    )
    snap = build_snapshot("BTCUSDT", COLLECTED, payload)
    assert snap.index_price == Decimal("64793.31673913")
    assert snap.basis == Decimal("-21.31673913")
    assert snap.basis_rate == Decimal("-0.00032900")
    assert snap.basis_at == datetime(2026, 11, 16, 8, tzinfo=UTC)
    assert snap.ticker_last_price == Decimal("64770.50")
    assert snap.ticker_quote_volume_24h == Decimal("987654321.50")
    assert snap.ticker_price_change_pct_24h == Decimal("2.45")
    assert snap.ticker_at == datetime(2026, 11, 16, 8, tzinfo=UTC)

    digest = derivatives_digest([snap], COLLECTED)
    row = digest["symbols"][0]
    assert row["basis_rate_pct"] == "-0.0329"
    assert row["quote_volume_24h_usdt"] == "987654322"

    with pytest.raises(DerivativesContractError, match="basis"):
        build_snapshot(
            "BTCUSDT",
            COLLECTED,
            _raw(
                basis=[
                    {
                        "pair": "ETHUSDT",
                        "contractType": "PERPETUAL",
                        "basis": "1",
                        "basisRate": "0.0001",
                        "timestamp": T08,
                    }
                ]
            ),
        )
    with pytest.raises(DerivativesContractError, match="quoteVolume"):
        build_snapshot(
            "BTCUSDT",
            COLLECTED,
            _raw(
                ticker_24h={
                    "symbol": "BTCUSDT",
                    "lastPrice": "100",
                    "quoteVolume": "-1",
                    "priceChangePercent": "0",
                    "closeTime": T08,
                }
            ),
        )


def test_http_fetcher_enforces_1000_per_5m_futures_data_budget() -> None:
    """DATA-1: giữ trần 1.000 request /futures/data trong cửa sổ 5 phút."""
    import httpx2

    from trading_bot.adapters.sources.binance_derivatives_http import (
        BinanceDerivativesHttpFetcher,
        BinanceThrottled,
    )

    now = [0.0]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx2.Response(200, json=_raw()["premium"])
        if request.url.path == "/fapi/v1/ticker/24hr":
            return httpx2.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "lastPrice": "64770",
                    "quoteVolume": "1000",
                    "priceChangePercent": "0.5",
                    "closeTime": T08,
                },
            )
        return httpx2.Response(200, json=[])

    async def run() -> None:
        fetcher = BinanceDerivativesHttpFetcher(
            transport=httpx2.MockTransport(handler),
            pause_seconds=0,
            max_futures_data_per_5m=5,
            monotonic_clock=lambda: now[0],
        )
        try:
            # 1 symbol dùng đúng 5 request /futures/data -> vừa đủ trần 5.
            await fetcher.fetch_symbol("BTCUSDT")
            with pytest.raises(BinanceThrottled, match="1.000"):
                await fetcher.fetch_symbol("BTCUSDT")
            # Sau 301 giây, cửa sổ trượt được giải phóng.
            now[0] = 301.0
            await fetcher.fetch_symbol("BTCUSDT")
        finally:
            await fetcher.aclose()

    asyncio.run(run())
