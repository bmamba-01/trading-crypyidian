"""Unit test cho read-model bối cảnh thị trường có provenance (CRY-374)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.portfolio.market_context import (
    MarketContextAlertCode,
    MarketContextKind,
    MarketContextObservation,
    build_market_context_report,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose


def _observation(
    kind: MarketContextKind,
    *,
    event_at: datetime,
    symbol: str = "BTCUSDT",
    value: Decimal | None = Decimal("0.0001"),
) -> MarketContextObservation:
    return MarketContextObservation(
        kind=kind,
        symbol=symbol,
        source="BINANCE_USDM",
        checksum="a" * 64,
        event_at=event_at,
        available_at=event_at + timedelta(seconds=2),
        value=value,
        unit="ratio",
        revision=1,
        raw_reference="raw/funding/2026-11-25.bin",
    )


def test_report_giu_provenance_va_khong_dung_lam_gia_fill() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    obs = _observation(MarketContextKind.FUNDING, event_at=now - timedelta(minutes=10))
    report = build_market_context_report(
        workspace_id="ws-1",
        symbol="BTCUSDT",
        observations=(obs,),
        purpose=SourceUsePurpose.DISPLAY,
        now=now,
    )
    payload = report.to_dict()
    assert report.display_allowed is True
    assert payload["observations"][0]["checksum"] == "a" * 64
    assert payload["observations"][0]["source"] == "BINANCE_USDM"
    assert payload["observations"][0]["value"] == "0.0001"
    assert payload["used_for_fill_or_signal"] is False
    assert payload["observations"][0]["used_for_fill_or_signal"] is False
    assert MarketContextAlertCode.STALE not in report.alerts


def test_phat_hien_du_lieu_cu_va_thieu_phu() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    obs = _observation(
        MarketContextKind.OPEN_INTEREST,
        event_at=now - timedelta(hours=5),
        value=Decimal("1234.5"),
    )
    report = build_market_context_report(
        workspace_id="ws-1",
        symbol="BTCUSDT",
        observations=(obs,),
        purpose=SourceUsePurpose.DISPLAY,
        now=now,
        expected_per_kind={MarketContextKind.OPEN_INTEREST: 3},
    )
    assert MarketContextAlertCode.STALE in report.alerts
    assert MarketContextAlertCode.PARTIAL_COVERAGE in report.alerts
    coverage = next(
        item for item in report.coverage if item.kind == MarketContextKind.OPEN_INTEREST
    )
    assert coverage.count == 1
    assert coverage.expected_count == 3
    assert coverage.stale is True


def test_khong_co_quyen_hien_thi_thi_khong_lo_du_lieu() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    obs = _observation(MarketContextKind.MARK_PRICE, event_at=now - timedelta(minutes=1))
    report = build_market_context_report(
        workspace_id="ws-1",
        symbol="BTCUSDT",
        observations=(obs,),
        purpose=SourceUsePurpose.TRAINING,
        now=now,
    )
    payload = report.to_dict()
    assert report.display_allowed is False
    assert report.alerts == (MarketContextAlertCode.RIGHTS_NOT_ALLOWED,)
    assert payload["observations"] == []
    assert payload["coverage"] == []
    serialized = str(payload)
    assert "a" * 64 not in serialized
    assert "BINANCE_USDM" not in serialized
    assert "0.0001" not in serialized


def test_khong_co_quan_sat_nao_thi_bao_no_data_mot_lan() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    report = build_market_context_report(
        workspace_id="ws-1",
        symbol="BTCUSDT",
        observations=(),
        purpose=SourceUsePurpose.DISPLAY,
        now=now,
    )
    assert report.alerts.count(MarketContextAlertCode.NO_DATA) == 1
    assert len(report.alerts) == 1
    assert "Không có dữ liệu bối cảnh" in report.to_dict()["alerts"][0]["message_vi"]


def test_loc_theo_symbol_va_cua_so_quan_sat() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    stale = _observation(
        MarketContextKind.LIQUIDATION, event_at=now - timedelta(hours=25), symbol="BTCUSDT"
    )
    other = _observation(
        MarketContextKind.LIQUIDATION, event_at=now - timedelta(minutes=5), symbol="ETHUSDT"
    )
    fresh = _observation(
        MarketContextKind.LIQUIDATION, event_at=now - timedelta(minutes=5), symbol="BTCUSDT"
    )
    report = build_market_context_report(
        workspace_id="ws-1",
        symbol="BTCUSDT",
        observations=(stale, other, fresh),
        purpose=SourceUsePurpose.DISPLAY,
        now=now,
    )
    assert report.observations == (fresh,)
    assert MarketContextAlertCode.STALE not in report.alerts


def test_observation_yeu_cau_provenance_va_moc_thoi_gian() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="checksum"):
        MarketContextObservation(
            kind=MarketContextKind.FUNDING,
            symbol="BTCUSDT",
            source="BINANCE_USDM",
            checksum="",
            event_at=now,
            available_at=now,
        )
    with pytest.raises(ValueError, match="múi giờ"):
        MarketContextObservation(
            kind=MarketContextKind.FUNDING,
            symbol="BTCUSDT",
            source="BINANCE_USDM",
            checksum="a" * 64,
            event_at=now.replace(tzinfo=None),
            available_at=now,
        )
