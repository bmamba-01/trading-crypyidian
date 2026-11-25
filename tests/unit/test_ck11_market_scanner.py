"""CK-11: bộ quét thị trường trên nến đã lưu — tất định, lọc thanh khoản, loại mã thiếu dữ liệu."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from trading_bot.domain.market.scanner import (
    SCANNER_VERSION,
    ScanCriteria,
    scan_markets,
)

NOW = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)


def _daily(
    closes: list[float], *, volume: float = 1000.0, quote: bool = True, gap_high: float = 0.0
) -> list[dict[str, Any]]:
    """Nến 1d kết thúc ngay trước NOW; nến cuối có thể có râu cao hơn ``gap_high``."""
    count = len(closes)
    rows = []
    for index, close in enumerate(closes):
        opened = NOW - timedelta(days=count - index)
        rows.append(
            {
                "time": int(opened.timestamp()),
                "open": close,
                "high": close * 1.01 + (gap_high if index == count - 1 else 0.0),
                "low": close * 0.99,
                "close": close,
                "volume": volume,
                "quote_volume": volume * close if quote else None,
            }
        )
    return rows


def _series() -> dict[str, list[dict[str, Any]]]:
    return {
        "BTCUSDT": _daily([100.0] * 15 + [110.0] * 7, volume=1_000_000),  # +10% / 7 ngày
        "ETHUSDT": _daily([50.0] * 15 + [45.0] * 7, volume=2_000_000),  # −10% / 7 ngày
        "DOGEUSDT": _daily([1.0] * 15 + [1.5] * 7, volume=100),  # +50% nhưng thanh khoản thấp
        "NEWUSDT": _daily([2.0] * 5, volume=10_000_000),  # chỉ 5 ngày dữ liệu
        "SOLUSDT": _daily([20.0] * 22, volume=1_000_000, quote=False),  # thiếu quote_volume
    }


def test_rankings_are_deterministic_and_liquidity_filtered() -> None:
    criteria = ScanCriteria(
        min_quote_volume_24h=1_000_000.0, range_min_pct=0.0, range_max_pct=100.0
    )
    first = scan_markets(_series(), timeframe="1d", now=NOW, criteria=criteria)
    second = scan_markets(
        dict(reversed(list(_series().items()))), timeframe="1d", now=NOW, criteria=criteria
    )
    assert SCANNER_VERSION == "ck11-scanner-v1"
    assert first == second  # cùng dữ liệu → cùng kết quả, không phụ thuộc thứ tự đầu vào
    movers = [row["symbol"] for row in first["movers_7d"]]
    # BTC +10%, ETH −10% (cùng độ lớn → xếp theo tên); DOGE bị loại do thanh khoản < 1 triệu.
    assert movers == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    btc = next(row for row in first["movers_7d"] if row["symbol"] == "BTCUSDT")
    assert btc["change_7d_pct"] == pytest.approx(10.0)
    assert btc["quote_volume_24h"] == pytest.approx(110_000_000.0)
    sol = next(row for row in first["movers_7d"] if row["symbol"] == "SOLUSDT")
    assert sol["quote_volume_estimated"] is True  # thiếu quote_volume → khối lượng × giá đóng
    assert [row["symbol"] for row in first["top_volume"]][0] == "BTCUSDT"
    excluded = {row["symbol"]: row["reason"] for row in first["excluded"]}
    assert excluded["NEWUSDT"].startswith("Thiếu dữ liệu")
    assert "thanh khoản" in excluded["DOGEUSDT"]
    assert first["universe"] == 5
    assert first["passed"] == 3


def test_range_filter_bounds_21_day_range() -> None:
    criteria = ScanCriteria(min_quote_volume_24h=0.0, range_min_pct=15.0, range_max_pct=40.0)
    result = scan_markets(_series(), timeframe="1d", now=NOW, criteria=criteria)
    ranged = {row["symbol"]: row["range_21d_pct"] for row in result["top_range_21d"]}
    # BTC biên 21 ngày ≈ (111,1 − 99)/110 ≈ 11% → ngoài [15, 40]; ETH ≈ (50,5 − 44,55)/45 ≈ 13%.
    assert "BTCUSDT" not in ranged
    assert ranged["DOGEUSDT"] == pytest.approx((1.515 - 0.99) / 1.5 * 100)


def test_stale_series_is_excluded() -> None:
    stale = {"BTCUSDT": _daily([100.0] * 22)}
    later = NOW + timedelta(days=3)
    result = scan_markets(stale, timeframe="1d", now=later, criteria=ScanCriteria())
    assert result["excluded"][0]["reason"].startswith("Dữ liệu cũ")
    with pytest.raises(ValueError, match="khung"):
        scan_markets(stale, timeframe="5m", now=NOW, criteria=ScanCriteria())


class _Unit:
    def __init__(self, store: dict[str, Any], **kwargs: Any) -> None:
        self.store = store
        self.kwargs = kwargs
        self.session = object()

    async def __aenter__(self) -> _Unit:
        self.store["units"].append(self.kwargs)
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def record_audit(self, **kwargs: Any) -> None:
        self.store["audits"].append(kwargs)


class _Repository:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def list_market_series(self, **kwargs: Any) -> list[str]:
        return [
            "BTCUSDT|1d|BINANCE_USDM_REST_BACKFILL",
            "BTCUSDT|1d|CRYPIDIAN_PIPELINE_FIXTURE",
            "ETHUSDT|1d|BINANCE_USDM",
            "XRPUSDT|1h|BINANCE_USDM",
        ]

    async def load_recent_bars(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.store["loads"].append((kwargs["symbol"], kwargs["source"], kwargs["limit"]))
        return _series()[kwargs["symbol"]]


def _service(store: dict[str, Any]) -> Any:
    from trading_bot.application.market_scanner import MarketScannerService

    return MarketScannerService(
        "admin",
        report_reader_sessions="report-reader",  # type: ignore[arg-type]
        unit_of_work=lambda sessions, **kwargs: _Unit(store, session_factory=sessions, **kwargs),
        repository=lambda session: _Repository(store),
    )


def _principal(role: Any) -> Any:
    from uuid import uuid4

    from trading_bot.auth.policy import Principal

    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=NOW,
    )


@pytest.mark.asyncio
async def test_service_reads_preferred_source_and_records_only_on_run() -> None:
    from trading_bot.application.market_scanner import MarketScannerError
    from trading_bot.auth.policy import Role

    store: dict[str, Any] = {"units": [], "audits": [], "loads": []}
    owner = _principal(Role.OWNER)
    criteria = ScanCriteria(min_quote_volume_24h=0.0)
    viewed = await _service(store).scan(owner, timeframe="1d", criteria=criteria, now=NOW)
    assert store["audits"] == []
    assert store["units"][-1] == {
        "session_factory": "report-reader",
        "workspace_id": owner.workspace_id,
        "actor_membership_id": owner.membership_id,
        "snapshot_read_only": True,
    }
    # Có nguồn REST thật → không đọc nguồn mẫu; XRP chưa có nến 1d → bị loại kèm lý do.
    assert ("BTCUSDT", "BINANCE_USDM_REST_BACKFILL", 33) in store["loads"]
    assert {row["symbol"]: row["reason"] for row in viewed["excluded"]}["XRPUSDT"].startswith(
        "Thiếu dữ liệu"
    )
    assert viewed["summary"].endswith("đạt 2/3 mã")
    await _service(store).scan(owner, timeframe="1d", criteria=criteria, now=NOW, record=True)
    (audit,) = store["audits"]
    assert audit["action"] == "market_scan.run"
    assert audit["reason"] == viewed["summary"]
    assert store["units"][-2]["session_factory"] == "report-reader"
    assert store["units"][-2]["snapshot_read_only"] is True
    assert store["units"][-1]["session_factory"] == "admin"
    assert "snapshot_read_only" not in store["units"][-1]
    with pytest.raises(MarketScannerError) as bad:
        await _service(store).scan(owner, timeframe="5m", criteria=criteria)
    assert bad.value.status_code == 422
