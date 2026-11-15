"""CK-7: lớp vị thế PAPER trên chart — UPnL theo mark price khớp số liệu ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_bot.domain.portfolio.position_overlay import (
    POSITION_OVERLAY_VERSION,
    position_overlay,
)

OPENED = datetime(2026, 11, 15, 14, 5, tzinfo=UTC)


def _position(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "trade_id": "t1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quote_asset": "USDT",
        "entry_fills": [
            {
                "price": Decimal("100"),
                "quantity": Decimal("2"),
                "fee": Decimal("0.08"),
                "occurred_at": OPENED,
            },
            {
                "price": Decimal("103"),
                "quantity": Decimal("1"),
                "fee": Decimal("0.04"),
                "occurred_at": OPENED.replace(minute=10),
            },
        ],
        "initial_stop": Decimal("97"),
        "current_stop": Decimal("99"),
        "target": Decimal("110"),
    }
    return base | overrides


def test_long_upnl_uses_weighted_entry_and_mark_price() -> None:
    view = position_overlay(_position(), mark_price=Decimal("105"), mark_source="mark")
    assert POSITION_OVERLAY_VERSION == "ck7-position-v1"
    assert view["entry_price"] == 101.0  # (100×2 + 103×1) / 3
    assert view["quantity"] == 3.0
    assert view["upnl"] == 12.0  # (105 − 101) × 3
    assert view["entry_fees"] == pytest.approx(0.12)
    assert view["upnl_after_entry_fees"] == pytest.approx(11.88)
    assert view["upnl_pct"] == pytest.approx(4 / 101 * 100)
    # R theo rủi ro ban đầu (101 − 97) × 3 = 12 → UPnL = 1R.
    assert view["r_multiple"] == pytest.approx(1.0)
    assert view["stop"] == 99.0
    assert view["initial_stop"] == 97.0
    assert view["target"] == 110.0
    assert view["filled_at"] == OPENED.isoformat()
    assert view["mark_source"] == "mark"


def test_short_upnl_sign_and_missing_mark() -> None:
    short = _position(side="SHORT", initial_stop=Decimal("106"), current_stop=None, target=None)
    view = position_overlay(short, mark_price=Decimal("99.5"), mark_source="last_close")
    assert view["upnl"] == pytest.approx(4.5)  # (101 − 99.5) × 3
    assert view["stop"] == 106.0  # chưa dời stop → dùng stop ban đầu
    assert view["target"] is None
    none = position_overlay(short, mark_price=None, mark_source=None)
    assert none["upnl"] is None
    assert none["r_multiple"] is None


def test_position_without_entry_fill_is_rejected() -> None:
    with pytest.raises(ValueError, match="fill"):
        position_overlay(_position(entry_fills=[]), mark_price=Decimal("1"), mark_source="mark")
