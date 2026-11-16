"""CK-12: bảo vệ vốn trên bảng giám sát bot — chỗ chạy và lỗ ròng so với giới hạn."""

from __future__ import annotations

from decimal import Decimal

from trading_bot.domain.risk.protection_view import PROTECTION_VERSION, protection_view


def test_slots_and_losses_against_limits() -> None:
    view = protection_view(
        open_positions=2,
        max_positions=3,
        net_today=Decimal("-40"),
        net_24h=Decimal("-85"),
        max_daily_loss=Decimal("50"),
        max_rolling_loss=Decimal("100"),
        quote_asset="USDT",
    )
    assert PROTECTION_VERSION == "ck12-protection-v1"
    assert view["slots"] == {"used": 2, "max": 3, "label": "2/3", "state": "ok"}
    # Lỗ ròng hôm nay 40/50 = 80% → cảnh báo; 24 giờ 85/100 = 85% → cảnh báo.
    assert view["daily"] == {"loss": 40.0, "limit": 50.0, "used_pct": 80.0, "state": "warning"}
    assert view["rolling"]["state"] == "warning"
    assert view["overall"] == "warning"


def test_full_slots_breached_loss_and_profit_days() -> None:
    full = protection_view(
        open_positions=3,
        max_positions=3,
        net_today=Decimal("12"),
        net_24h=Decimal("-120"),
        max_daily_loss=Decimal("50"),
        max_rolling_loss=Decimal("100"),
        quote_asset="USDT",
    )
    assert full["slots"]["state"] == "full"
    assert full["daily"]["loss"] == 0.0  # đang lãi trong ngày → không tính là lỗ
    assert full["rolling"]["state"] == "breached"
    assert full["overall"] == "breached"


def test_unknown_limits_are_reported_not_guessed() -> None:
    view = protection_view(
        open_positions=1,
        max_positions=None,
        net_today=Decimal("-5"),
        net_24h=Decimal("-5"),
        max_daily_loss=None,
        max_rolling_loss=None,
        quote_asset="USDT",
    )
    assert view["slots"] == {"used": 1, "max": None, "label": "1/—", "state": "unknown"}
    assert view["daily"]["state"] == "unknown"
    assert view["overall"] == "unknown"
