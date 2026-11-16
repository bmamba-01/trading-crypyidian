"""AI-4 lát 3: phát hiện niêm yết/gỡ hợp đồng Binance từ exchangeInfo, collector và cảnh báo."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from trading_bot.domain.research.listings import (
    LISTINGS_SOURCE_ID,
    ListingsContractError,
    ListingsFetch,
    diff_listings,
    entries_hash,
    listings_digest,
    parse_exchange_info,
)

FAR = 4133404800000  # mốc giả 2100-12-25 của perpetual (dữ liệu thật 16/11/2026)
DELIST = 1795161600000  # 2026-11-20 08:00 UTC
T0 = datetime(2026, 11, 16, 9, tzinfo=UTC)


def _symbol(name: str, status: str = "TRADING", delivery: int = FAR, kind: str = "PERPETUAL"):
    return {
        "symbol": name,
        "status": status,
        "contractType": kind,
        "quoteAsset": "USDT",
        "deliveryDate": delivery,
        "onboardDate": 1569398400000,
        "filters": [{"filterType": "PRICE_FILTER"}],  # trường khác bị bỏ
    }


BASE = [_symbol(f"C{i}USDT") for i in range(10)] + [
    _symbol("BTCUSDT"),
    _symbol("ZECUSDT"),
    _symbol("OLDUSDT", "SETTLING", 1748736000000),
    _symbol("BTCUSDT_261225", delivery=1798185600000, kind="CURRENT_QUARTER"),
]


def _entries(symbols: list[dict]) -> list:
    return parse_exchange_info({"symbols": symbols})


def test_parse_normalizes_sentinel_and_flags_scheduled_delists() -> None:
    entries = {e.symbol: e for e in _entries(BASE)}
    assert entries["BTCUSDT"].delivery_at is None  # mốc 2100 = không có ngày gỡ
    assert not entries["BTCUSDT"].delist_scheduled
    assert entries["OLDUSDT"].delivery_at == datetime(2025, 6, 1, tzinfo=UTC)
    assert not entries["OLDUSDT"].delist_scheduled  # đã gỡ, không còn TRADING
    # Hợp đồng quý có ngày giao là bình thường, không phải gỡ.
    assert not entries["BTCUSDT_261225"].delist_scheduled
    scheduled = {e.symbol: e for e in _entries([*BASE[:-3], _symbol("ZECUSDT", delivery=DELIST)])}
    assert scheduled["ZECUSDT"].delist_scheduled
    assert entries_hash(_entries(BASE)) == entries_hash(list(reversed(_entries(BASE))))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "symbols"),
        ({"symbols": [_symbol("A")] * 12}, "trùng"),
        ({"symbols": [_symbol("A")]}, "quá ít"),
        ({"symbols": [{**_symbol("A"), "status": ""}] + BASE}, "symbol/status"),
        ({"symbols": [{**_symbol("A"), "deliveryDate": "x"}] + BASE}, "mili-giây"),
    ],
)
def test_parse_rejects_bad_payloads(payload: Any, message: str) -> None:
    with pytest.raises(ListingsContractError, match=message):
        parse_exchange_info(payload)


def test_diff_detects_listing_delist_reschedule_and_status_changes() -> None:
    before = _entries(BASE)
    after = _entries(
        [s for s in BASE if s["symbol"] not in {"ZECUSDT", "C0USDT"}]
        + [
            _symbol("ZECUSDT", delivery=DELIST),
            _symbol("NEWUSDT", "PENDING_TRADING"),
            _symbol("C0USDT", "SETTLING", DELIST),
        ]
    )
    changes = {(c.kind, c.symbol) for c in diff_listings(before, after, T0)}
    assert changes == {
        ("DELIST_SCHEDULED", "ZECUSDT"),
        ("LISTED", "NEWUSDT"),
        ("STATUS_CHANGED", "C0USDT"),
        ("DELIST_SCHEDULED", "C0USDT"),
    }
    moved = _entries([*BASE[:-3], _symbol("ZECUSDT", delivery=DELIST + 86_400_000)])
    scheduled = _entries([*BASE[:-3], _symbol("ZECUSDT", delivery=DELIST)])
    assert [c.kind for c in diff_listings(scheduled, moved, T0)] == ["DELIST_RESCHEDULED"]
    cancelled = _entries([*BASE[:-3], _symbol("ZECUSDT")])
    assert [c.kind for c in diff_listings(scheduled, cancelled, T0)] == ["DELIST_CANCELLED"]


def test_digest_flags_watched_coins_and_is_point_in_time() -> None:
    first = ListingsFetch("a", T0, tuple(_entries(BASE)))
    second_entries = _entries(
        [s for s in BASE if s["symbol"] != "ZECUSDT"] + [_symbol("ZECUSDT", delivery=DELIST)]
    )
    second = ListingsFetch("b", T0 + timedelta(hours=1), tuple(second_entries))
    now = T0 + timedelta(hours=2)
    digest = listings_digest(
        [first, second],
        now,
        last_checked_at=now - timedelta(minutes=10),
        watch_symbols=["ZECUSDT", "BTCUSDT", "GONEUSDT"],
    )
    assert digest["is_fresh"] is True
    assert digest["watched_at_risk"] == [
        {
            "symbol": "ZECUSDT",
            "status": "TRADING",
            "delist_at_utc": "2026-11-20T08:00:00+00:00",
            "delist_at_vn": "20/11 15:00",
        },
        {"symbol": "GONEUSDT", "status": "MISSING", "delist_at_vn": None},
    ]
    assert digest["recent_changes"][0]["kind"] == "DELIST_SCHEDULED"
    assert digest["recent_changes"][0]["watched"] is True
    # Trước lần tải thứ hai: chưa biết lịch gỡ (không nhìn trước).
    early = listings_digest(
        [first, second], T0 + timedelta(minutes=30), last_checked_at=T0, watch_symbols=["ZECUSDT"]
    )
    assert early["watched_at_risk"] == []
    stale = listings_digest([first], now + timedelta(hours=7), last_checked_at=T0)
    assert stale["is_fresh"] is False
    assert listings_digest([], now, last_checked_at=None)["recent_changes"] == []


# --- Collector ----------------------------------------------------------------------------


class _Evidence:
    policy_fingerprint = "a" * 64


class _Gate:
    def __init__(self, allowed: list[bool]) -> None:
        self.allowed = allowed
        self.calls = 0

    async def authorize(self, purpose: Any) -> _Evidence:
        ok = self.allowed[min(self.calls, len(self.allowed) - 1)]
        self.calls += 1
        if not ok:
            raise PermissionError("Nguồn chưa có policy.")
        return _Evidence()


class _Store:
    def __init__(self, allowed: list[bool], last: tuple[datetime, str] | None = None) -> None:
        self.gate_obj = _Gate(allowed)
        self.last = last
        self.rows: list[dict] = []

    def gate(self, source_id: str) -> _Gate:
        assert source_id == LISTINGS_SOURCE_ID
        return self.gate_obj

    async def last_check(self) -> tuple[datetime, str] | None:
        return self.last

    async def insert(self, record: dict) -> None:
        self.rows.append(record)
        self.last = (record["fetched_at"], record["entries_sha256"])


class _Fetcher:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    async def fetch_exchange_info(self) -> Any:
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _collector(store: _Store, fetcher: _Fetcher, clock: list[datetime]) -> Any:
    from trading_bot.application.listings_collector import ListingsCollector

    @asynccontextmanager
    async def scope():
        yield store

    return ListingsCollector(
        workspace_id=UUID(int=1), fetcher=fetcher, store_scope=scope, clock=lambda: clock[0]
    )


def test_collector_stores_entries_only_when_changed() -> None:
    clock = [T0]
    store = _Store([True])
    fetcher = _Fetcher({"symbols": BASE})
    collector = _collector(store, fetcher, clock)
    assert asyncio.run(collector.collect_once(T0)).status == "changed"
    assert store.rows[0]["entries"] is not None
    assert store.rows[0]["entry_count"] == len(BASE)
    # Chưa tới hạn làm mới: không gọi Binance.
    assert asyncio.run(collector.collect_once(T0 + timedelta(minutes=5))).status == "skipped_recent"
    assert fetcher.calls == 1
    clock[0] = T0 + timedelta(minutes=31)
    assert asyncio.run(collector.collect_once(clock[0])).status == "unchanged"
    assert store.rows[1]["entries"] is None
    assert store.rows[1]["changed"] is False


def test_collector_rights_and_bad_payload_store_nothing() -> None:
    denied = _Store([False])
    fetcher = _Fetcher({"symbols": BASE})
    assert asyncio.run(_collector(denied, fetcher, [T0]).collect_once(T0)).status == (
        "rights_denied"
    )
    assert fetcher.calls == 0
    revoked = _Store([True, False])
    result = asyncio.run(_collector(revoked, _Fetcher({"symbols": BASE}), [T0]).collect_once(T0))
    assert result.status == "rights_denied"
    assert revoked.rows == []
    bad = _Store([True])
    result = asyncio.run(_collector(bad, _Fetcher({"symbols": []}), [T0]).collect_once(T0))
    assert (result.status, bad.rows) == ("error", [])


def test_setting_only_allowed_in_ingestion_worker() -> None:
    from trading_bot.config.settings import AppSettings

    with pytest.raises(ValueError, match="listings collector"):
        AppSettings(process_role="admin-web", listings_collector_enabled=True)


def test_card_shows_listing_alert_from_data_and_validator_accepts_delist_time() -> None:
    from trading_bot.adapters.telegram.card_renderer import render_analysis_card
    from trading_bot.application.ai_analyst import topic_report, validate_analysis
    from trading_bot.application.telegram_cards import (
        analysis_card_caption,
        analysis_card_view_from,
    )

    entries = _entries(
        [s for s in BASE if s["symbol"] != "ZECUSDT"] + [_symbol("ZECUSDT", delivery=DELIST)]
    )
    report = {
        "report_hash": "b" * 64,
        "window": {"start": "2026-11-15T09:00:00+00:00", "end": "2026-11-16T09:00:00+00:00"},
        "bots": [
            {
                "bot_name": "T03",
                "config": {"strategy_family": "H03", "timeframe": "1h", "universe": ["ZECUSDT"]},
                "funnel": {"candidates": 0},
                "performance": {"net_pnl_usdt": "0", "closed_trades": 0},
            }
        ],
        "listings": listings_digest(
            [ListingsFetch("a", T0, tuple(entries))],
            T0 + timedelta(minutes=5),
            last_checked_at=T0,
            watch_symbols=["ZECUSDT"],
        ),
    }
    topic = topic_report(report, "H03")
    assert topic["listings"] == report["listings"]
    output = {
        "summary": "ZECUSDT bị lên lịch gỡ.",
        "findings": [
            {
                "statement": "ZECUSDT sẽ bị Binance gỡ lúc 20/11 15:00 giờ VN.",
                "evidence_refs": ["/listings/watched_at_risk/0"],
            }
        ],
        "hypotheses": [],
        "proposals": [],
        "confidence": "low",
    }
    assert validate_analysis(output, topic).ok
    view = analysis_card_view_from("family:H03", topic, output)
    assert view.listing_alerts == ("ZECUSDT: Binance lên lịch gỡ lúc 20/11 15:00 (giờ VN)",)
    assert "CẢNH BÁO niêm yết: ZECUSDT" in analysis_card_caption(view)
    assert render_analysis_card(view).startswith(b"\x89PNG")
