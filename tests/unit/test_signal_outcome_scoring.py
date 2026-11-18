"""CRY-430: phương pháp chấm kết quả tín hiệu ``sig-outcome-v2``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.strategy.signal_outcome import (
    FirstTouch,
    ForwardBar,
    SignalLevels,
    SignalOutcomeError,
    contiguous_window,
    levels_from_candidate,
    r_band_label,
    reference_matches,
    score_signal,
    signal_bar,
)

SIGNAL_AT = datetime(2026, 11, 17, 11, 59, 59, 999000, tzinfo=UTC)
STEP = timedelta(minutes=15)
LONG = SignalLevels(
    direction="long",
    entry=Decimal("100"),
    stop=Decimal("90"),
    tp1=Decimal("110"),
    tp2=Decimal("120"),
)
SHORT = SignalLevels(
    direction="short",
    entry=Decimal("100"),
    stop=Decimal("110"),
    tp1=Decimal("90"),
    tp2=Decimal("80"),
)


def _bars(*ohlc: tuple[str, str, str, str]) -> tuple[ForwardBar, ...]:
    start = SIGNAL_AT + timedelta(milliseconds=1)
    return tuple(
        ForwardBar(
            open_time=start + STEP * index,
            close_time=start + STEP * (index + 1) - timedelta(milliseconds=1),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
            ref=f"bar-{index}",
        )
        for index, (o, h, low, c) in enumerate(ohlc)
    )


def _score(levels: SignalLevels, bars: tuple[ForwardBar, ...]):
    return score_signal(levels, signal_at=SIGNAL_AT, window=bars)


def test_long_tp1_first_then_tp2_without_sl() -> None:
    outcome = _score(
        LONG,
        _bars(
            ("100", "105", "98", "104"), ("104", "112", "103", "111"), ("111", "121", "109", "119")
        ),
    )
    assert outcome.first_touch is FirstTouch.TP1
    assert outcome.tp1_hit and outcome.tp2_hit and not outcome.invalidated  # noqa: PT018
    assert outcome.bars_to_first_touch == 2
    assert outcome.seconds_to_first_touch == 1800
    assert outcome.outcome_r == Decimal("1.0000")
    assert outcome.close_r == Decimal("1.9000")
    assert outcome.mfe_r == Decimal("2.1000")
    assert outcome.mae_r == Decimal("-0.2000")
    assert outcome.label == 1
    assert not outcome.same_bar_conflict


def test_long_sl_first_is_wrong_and_invalidated() -> None:
    outcome = _score(LONG, _bars(("100", "103", "89", "92"), ("92", "115", "91", "114")))
    assert outcome.first_touch is FirstTouch.SL
    assert outcome.invalidated and not outcome.tp1_hit and not outcome.tp2_hit  # noqa: PT018
    assert outcome.outcome_r == Decimal("-1.0000")
    assert outcome.label == -1
    # MFE vẫn tính trên toàn horizon dù đã chạm SL.
    assert outcome.mfe_r == Decimal("1.5000")


def test_same_bar_sl_and_tp1_assumes_sl_first() -> None:
    outcome = _score(LONG, _bars(("100", "111", "89", "105")))
    assert outcome.first_touch is FirstTouch.SL
    assert outcome.same_bar_conflict
    assert not outcome.tp1_hit
    assert outcome.outcome_r == Decimal("-1.0000")
    assert outcome.label == -1


def test_short_same_bar_sl_and_tp1_assumes_sl_first() -> None:
    outcome = _score(SHORT, _bars(("100", "111", "89", "95")))
    assert outcome.first_touch is FirstTouch.SL
    assert outcome.same_bar_conflict
    assert outcome.outcome_r == Decimal("-1.0000")


def test_tp2_same_bar_as_later_sl_is_not_counted() -> None:
    outcome = _score(LONG, _bars(("100", "111", "99", "110"), ("110", "121", "89", "95")))
    assert outcome.first_touch is FirstTouch.TP1
    assert not outcome.tp2_hit
    assert outcome.invalidated
    assert outcome.outcome_r == Decimal("1.0000")


def test_tp1_and_tp2_in_first_bar_without_sl() -> None:
    outcome = _score(LONG, _bars(("100", "125", "99", "122")))
    assert outcome.tp1_hit and outcome.tp2_hit  # noqa: PT018
    assert outcome.bars_to_first_touch == 1


def test_gap_through_stop_exits_at_open() -> None:
    outcome = _score(LONG, _bars(("85", "88", "80", "86")))
    assert outcome.first_touch is FirstTouch.SL
    assert outcome.outcome_r == Decimal("-1.5000")
    assert not outcome.same_bar_conflict


def test_gap_through_tp1_exits_at_open() -> None:
    outcome = _score(SHORT, _bars(("88", "89", "86", "87")))
    assert outcome.first_touch is FirstTouch.TP1
    assert outcome.outcome_r == Decimal("1.2000")


def test_no_touch_uses_close_and_band_label() -> None:
    flat = _score(LONG, _bars(("100", "104", "97", "100.5")))
    assert flat.first_touch is FirstTouch.NONE
    assert flat.outcome_r == Decimal("0.0500")
    assert flat.label == 0
    assert flat.bars_to_first_touch is None and flat.seconds_to_first_touch is None  # noqa: PT018
    loss = _score(LONG, _bars(("100", "101", "95", "96")))
    assert loss.label == -1


def test_r_band_boundaries() -> None:
    assert r_band_label(Decimal("0.1")) == 0
    assert r_band_label(Decimal("-0.1")) == 0
    assert r_band_label(Decimal("0.1001")) == 1
    assert r_band_label(Decimal("-0.1001")) == -1


def test_input_hash_is_deterministic_and_depends_on_bars() -> None:
    bars = _bars(("100", "104", "97", "100.5"))
    assert _score(LONG, bars).input_hash == _score(LONG, bars).input_hash
    other = _bars(("100", "104", "97", "101"))
    assert _score(LONG, bars).input_hash != _score(LONG, other).input_hash


def test_contiguous_window_requires_enough_bars_and_no_gap() -> None:
    bars = _bars(("100", "101", "99", "100"), ("100", "101", "99", "100"), ("1", "1", "1", "1"))
    assert contiguous_window(bars, signal_at=SIGNAL_AT, interval=STEP, horizon_bars=4) is None
    window = contiguous_window(bars, signal_at=SIGNAL_AT, interval=STEP, horizon_bars=2)
    assert window is not None and len(window) == 2  # noqa: PT018
    gapped = (bars[0], bars[2])
    assert contiguous_window(gapped, signal_at=SIGNAL_AT, interval=STEP, horizon_bars=2) is None
    # Nến tín hiệu và nến trước đó không được dùng (không nhìn lùi, không lẫn nến tín hiệu).
    earlier = ForwardBar(
        open_time=SIGNAL_AT - STEP + timedelta(milliseconds=1),
        close_time=SIGNAL_AT,
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal(1),
    )
    window = contiguous_window(
        (earlier, *bars[:1]), signal_at=SIGNAL_AT, interval=STEP, horizon_bars=1
    )
    assert window == bars[:1]
    late = contiguous_window(bars[1:2], signal_at=SIGNAL_AT, interval=STEP, horizon_bars=1)
    assert late is None


def test_levels_from_candidate_match_signal_card() -> None:
    candidate = {
        "symbol": "ZECUSDT",
        "direction": "long",
        "strategy_id": "H03",
        "strategy_release_id": "h03-v3-15m",
        "reference_price": "950.08",
        "atr_value": 9.5,
        "recipe": {"stop_atr_multiple": 2.0, "take_profit_atr_multiple": None},
        "signal_at": SIGNAL_AT.isoformat(),
    }
    levels = levels_from_candidate(candidate)
    assert levels.entry == Decimal("950.08")
    assert levels.stop == Decimal("931.08")
    assert levels.tp1 == Decimal("969.08")
    assert levels.tp2 == Decimal("988.08")


def test_levels_from_candidate_rejects_broken_candidate() -> None:
    with pytest.raises(SignalOutcomeError):
        levels_from_candidate({"direction": "long", "reference_price": "1", "atr_value": 0,
                               "recipe": {"stop_atr_multiple": 2}})  # fmt: skip


def test_levels_validation() -> None:
    with pytest.raises(SignalOutcomeError):
        SignalLevels(direction="long", entry=Decimal(1), stop=Decimal(2), tp1=Decimal(3), tp2=None)
    with pytest.raises(SignalOutcomeError):
        SignalLevels(direction="up", entry=Decimal(1), stop=Decimal(0), tp1=Decimal(3), tp2=None)


def test_reference_price_must_match_signal_bar_close() -> None:
    """v2: giá tham chiếu giả (100) trên nến BTC thật (~65.000) bị loại thay vì cho R hàng vạn."""
    assert reference_matches(Decimal("65000"), Decimal("65200"))
    assert not reference_matches(Decimal("65000"), Decimal("65400"))
    assert not reference_matches(Decimal("100.2"), Decimal("65309.8"))
    assert not reference_matches(Decimal("1"), Decimal("0"))
    origin = ForwardBar(
        open_time=SIGNAL_AT - STEP + timedelta(milliseconds=1),
        close_time=SIGNAL_AT,
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal("100"),
    )
    later = _bars(("100", "101", "99", "100"))
    assert signal_bar((origin, *later), signal_at=SIGNAL_AT, interval=STEP) == origin
    assert signal_bar(later, signal_at=SIGNAL_AT, interval=STEP) is None
