from decimal import Decimal

import pytest

from trading_bot.domain.strategy.geometry import (
    PatternCandidate,
    PatternKind,
    _select_latest_candidate,
    find_confirmed_pivots,
    find_double_pattern,
)


def test_pivot_is_returned_only_when_right_bars_confirm_it() -> None:
    highs = [Decimal(value) for value in (8, 10, 8, 9, 10, 8)]
    lows = [Decimal(value) for value in (7, 8, 6, 7, 8, 7)]

    pivots = find_confirmed_pivots(highs, lows, left_bars=1, right_bars=1)

    assert [(pivot.kind, pivot.index, pivot.confirmed_at_index) for pivot in pivots] == [
        ("high", 1, 2),
        ("low", 2, 3),
        ("high", 4, 5),
    ]


def test_equal_neighbor_does_not_create_duplicate_pivot() -> None:
    highs = [Decimal(value) for value in (8, 10, 10, 8)]
    lows = [Decimal(value) for value in (7, 8, 7, 6)]

    pivots = find_confirmed_pivots(highs, lows, left_bars=1, right_bars=1)

    assert all(pivot.kind != "high" for pivot in pivots)


def test_double_top_waits_for_close_below_neckline_after_pivot_confirmation() -> None:
    highs = [Decimal(value) for value in (8, 10, 8, 9, 10.2, 8, 7, 6)]
    lows = [Decimal(value) for value in (7, 8, 7, 7, 8, 7.5, 6, 5)]
    closes = [Decimal(value) for value in (8, 9, 8, 9, 9.8, 7.8, 6.5, 5.5)]

    before_breakout = find_double_pattern(
        highs[:6], lows[:6], closes[:6], tolerance=Decimal("0.03")
    )
    confirmed = find_double_pattern(highs, lows, closes, tolerance=Decimal("0.03"))

    assert before_breakout is None
    assert confirmed is not None
    assert confirmed.kind is PatternKind.DOUBLE_TOP
    assert confirmed.direction == "short"
    assert confirmed.first_swing_index == 1
    assert confirmed.second_swing_index == 4
    assert confirmed.neckline == Decimal("7")
    assert confirmed.confirmation_index == 6
    assert confirmed.invalidation_price > Decimal("10.2")


def test_double_bottom_requires_confirmed_close_above_neckline() -> None:
    highs = [Decimal(value) for value in (3, 4, 3.5, 3, 4, 5, 6)]
    lows = [Decimal(value) for value in (2, 1, 2, 1.1, 2, 3, 4)]
    closes = [Decimal(value) for value in (2.5, 1.5, 2.5, 1.6, 2.5, 4.2, 5.5)]

    candidate = find_double_pattern(highs, lows, closes, tolerance=Decimal("0.15"))

    assert candidate is not None
    assert candidate.kind is PatternKind.DOUBLE_BOTTOM
    assert candidate.direction == "long"
    assert candidate.first_swing_index == 1
    assert candidate.second_swing_index == 3
    assert candidate.confirmation_index == 5
    assert candidate.neckline == Decimal("3.5")
    assert candidate.invalidation_price < Decimal("1")


def test_insufficient_or_misaligned_data_returns_no_pattern() -> None:
    assert find_confirmed_pivots([], [], left_bars=1, right_bars=1) == ()
    assert find_double_pattern([Decimal("1")], [Decimal("1"), Decimal("2")], [Decimal("1")]) is None


def test_non_positive_or_non_finite_prices_return_no_pattern() -> None:
    highs = [Decimal("2"), Decimal("NaN"), Decimal("2")]
    lows = [Decimal("1"), Decimal("0"), Decimal("1")]
    closes = [Decimal("1.5"), Decimal("1"), Decimal("1.5")]

    assert find_double_pattern(highs, lows, closes) is None


def test_close_outside_bar_range_returns_no_pattern() -> None:
    highs = [Decimal("2"), Decimal("3"), Decimal("2")]
    lows = [Decimal("1"), Decimal("2"), Decimal("1")]
    closes = [Decimal("1.5"), Decimal("3.1"), Decimal("1.5")]

    assert find_double_pattern(highs, lows, closes) is None


def test_later_swing_supersedes_an_older_confirmed_double_top() -> None:
    highs = [Decimal(value) for value in (8, 10, 8, 9, 10.2, 8, 7, 8, 12, 8, 7)]
    lows = [Decimal(value) for value in (7, 8, 7, 7, 8, 7.5, 6, 7, 8, 7, 6)]
    closes = [Decimal(value) for value in (8, 9, 8, 9, 9.8, 7.8, 6.5, 7.5, 11, 7, 6)]

    candidate = find_double_pattern(highs, lows, closes, tolerance=Decimal("0.03"))

    assert candidate is None


def test_later_confirmation_wins_when_both_pattern_directions_are_eligible() -> None:
    highs = [Decimal(value) for value in (4, 3, 5, 4, 5.1, 5.05, 6.2)]
    lows = [Decimal(value) for value in (2, 1, 2, 1.05, 2, 1, 0.9)]
    closes = [Decimal(value) for value in (3, 1.5, 4.5, 1.5, 4.9, 1.02, 5.2)]

    candidate = find_double_pattern(highs, lows, closes, tolerance=Decimal("0.1"))

    assert candidate is not None
    assert candidate.kind is PatternKind.DOUBLE_BOTTOM
    assert candidate.confirmation_index == 6


def test_candidate_selection_uses_stable_pattern_kind_tie_break() -> None:
    bottom = PatternCandidate(
        kind=PatternKind.DOUBLE_BOTTOM,
        direction="long",
        first_swing_index=1,
        second_swing_index=3,
        neckline=Decimal("5"),
        confirmation_index=6,
        invalidation_price=Decimal("1"),
    )
    top = PatternCandidate(
        kind=PatternKind.DOUBLE_TOP,
        direction="short",
        first_swing_index=2,
        second_swing_index=4,
        neckline=Decimal("1"),
        confirmation_index=6,
        invalidation_price=Decimal("6"),
    )

    assert _select_latest_candidate((bottom, top)) == top


@pytest.mark.parametrize(
    "tolerance",
    [Decimal("-0.01"), Decimal("1"), Decimal("1.01"), Decimal("NaN")],
)
def test_invalid_tolerance_returns_no_candidate(tolerance: Decimal) -> None:
    highs = [Decimal(value) for value in (2, 3, 2)]
    lows = [Decimal(value) for value in (1, 2, 1)]
    closes = [Decimal(value) for value in (1.5, 2.5, 1.5)]

    assert find_double_pattern(highs, lows, closes, tolerance=tolerance) is None
