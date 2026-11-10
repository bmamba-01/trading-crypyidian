from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.telegram_signals import (
    TelegramSignalCandidate,
    TelegramSignalFilters,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _candidate(**overrides: object) -> TelegramSignalCandidate:
    values: dict[str, object] = {
        "decision_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "timeframe": "1h",
        "strategy_id": "H01",
        "strategy_release_id": "h01-v1",
        "reference_price": Decimal("68000"),
        "signal_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "reason_code": "H01_BREAKOUT",
        "detail_vi": "Đóng cửa vượt ngưỡng đã xác nhận.",
    }
    values.update(overrides)
    return TelegramSignalCandidate(**values)  # type: ignore[arg-type]


def test_empty_subscription_filters_match_every_candidate() -> None:
    assert TelegramSignalFilters().matches(_candidate())
    assert TelegramSignalFilters().matches(_candidate(symbol="ETHUSDT", direction="SHORT"))


def test_filter_allowlists_match_all_selected_candidate_fields() -> None:
    filters = TelegramSignalFilters(
        symbols=("btcusdt",),
        timeframes=("1h",),
        directions=("long",),
        strategy_ids=("H01",),
        setup_codes=("h01_breakout",),
    )

    assert filters.as_dict() == {
        "symbols": ["BTCUSDT"],
        "timeframes": ["1h"],
        "directions": ["LONG"],
        "strategy_ids": ["H01"],
        "setup_codes": ["H01_BREAKOUT"],
    }
    assert filters.matches(_candidate())


@pytest.mark.parametrize(
    "override",
    [
        {"symbol": "ETHUSDT"},
        {"timeframe": "15m"},
        {"direction": "SHORT"},
        {"strategy_id": "H03"},
        {"reason_code": "H01_PULLBACK"},
    ],
)
def test_filter_allowlist_rejects_candidate_outside_each_selected_value(
    override: dict[str, str],
) -> None:
    filters = TelegramSignalFilters(
        symbols=("BTCUSDT",),
        timeframes=("1h",),
        directions=("LONG",),
        strategy_ids=("H01",),
        setup_codes=("H01_BREAKOUT",),
    )

    assert not filters.matches(_candidate(**override))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"symbols": ("btc-usdt",)}, "symbols chứa"),
        ({"timeframes": ("0m",)}, "timeframes chứa"),
        ({"directions": ("BOTH",)}, "directions chỉ"),
        ({"strategy_ids": ("",)}, "strategy_ids chứa"),
        ({"setup_codes": ("a" * 129,)}, "setup_codes chứa"),
        ({"symbols": tuple(f"S{i:03d}" for i in range(101))}, "symbols phải là tuple"),
    ],
)
def test_filter_rejects_invalid_or_unbounded_allowlists(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        TelegramSignalFilters(**kwargs)  # type: ignore[arg-type]
