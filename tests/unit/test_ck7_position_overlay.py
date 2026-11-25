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


def test_position_overlay_exposes_tp_ladder_and_hit_state_from_exit_fills() -> None:
    tp1_at = OPENED.replace(minute=25)
    pos = _position(
        entry_fills=[
            {
                "price": Decimal("100"),
                "quantity": Decimal("4"),
                "fee": Decimal("0.16"),
                "occurred_at": OPENED,
            }
        ],
        exit_fills=[
            {
                "price": Decimal("104"),
                "quantity": Decimal("2"),
                "fee": Decimal("0.08"),
                "occurred_at": tp1_at,
                "fill_key": "exit:p1:bar1:TP1",
                "exit_reason": "TAKE_PROFIT_1",
            }
        ],
        initial_stop=Decimal("96"),
        current_stop=Decimal("100"),
        target=Decimal("112"),
        targets=[
            {
                "index": 1,
                "price": "104",
                "fraction": "0.5",
                "r_multiple": "1",
                "stop_to_entry_after": True,
            },
            {
                "index": 2,
                "price": "108",
                "fraction": "0.3",
                "r_multiple": "2",
                "stop_to_entry_after": False,
            },
            {
                "index": 3,
                "price": "112",
                "fraction": "0.2",
                "r_multiple": "3",
                "stop_to_entry_after": False,
            },
        ],
    )
    view = position_overlay(pos, mark_price=Decimal("106"), mark_source="mark")
    # Khối lượng còn lại sau khi chốt 50% TP1 = 4 - 2 = 2; UPnL = (106 - 100) × 2 = 12.
    assert view["quantity"] == pytest.approx(2.0)
    assert view["upnl"] == pytest.approx(12.0)
    assert view["r_multiple"] == pytest.approx(1.5)
    assert len(view["targets"]) == 3
    tp1, tp2, tp3 = view["targets"]
    assert tp1 == {
        "index": 1,
        "label": "TP1",
        "price": 104.0,
        "close_fraction": 0.5,
        "r_multiple": 1.0,
        "stop_to_entry_after": True,
        "hit": True,
        "hit_at": tp1_at.isoformat(),
    }
    assert tp2["hit"] is False
    assert tp2["close_fraction"] == pytest.approx(0.3)
    assert tp3["hit"] is False
    assert tp3["close_fraction"] == pytest.approx(0.2)


def test_short_position_overlay_detects_tp_hit_by_untagged_exit_price() -> None:
    tp1_at = OPENED.replace(minute=30)
    short = _position(
        side="SHORT",
        entry_fills=[
            {
                "price": Decimal("100"),
                "quantity": Decimal("2"),
                "fee": Decimal("0.08"),
                "occurred_at": OPENED,
            }
        ],
        exit_fills=[
            {
                "price": Decimal("95.5"),
                "quantity": Decimal("1"),
                "fee": Decimal("0.04"),
                "occurred_at": tp1_at,
            }
        ],
        initial_stop=Decimal("104"),
        current_stop=Decimal("100"),
        targets=[
            {"index": 1, "price": "96", "fraction": "0.5", "stop_to_entry_after": True},
            {"index": 2, "price": "92", "fraction": "0.5", "stop_to_entry_after": False},
        ],
    )
    view = position_overlay(short, mark_price=Decimal("95"), mark_source="mark")
    assert [t["hit"] for t in view["targets"]] == [True, False]
    assert view["targets"][0]["hit_at"] == tp1_at.isoformat()
