import math

import pytest

from trading_bot.domain.market.features import (
    adx_dmi,
    atr,
    bollinger_bands,
    efficiency_ratio,
    ema,
    realized_volatility,
    rsi,
    sma,
    true_range,
    vwap,
    zscore,
)


def test_sma_returns_no_value_until_the_window_is_full() -> None:
    assert sma([1, 2, 3, 4], 3) == (None, None, 2.0, 3.0)


def test_ema_seeds_with_the_first_full_simple_average() -> None:
    assert ema([1, 2, 3, 4], 3) == (None, None, 2.0, 3.0)


def test_true_range_uses_previous_close_after_the_first_bar() -> None:
    assert true_range([10, 12, 13], [8, 10, 11], [9, 11, 12]) == (
        2.0,
        3.0,
        2.0,
    )


def test_atr_uses_wilder_smoothing_seeded_with_mean_true_range() -> None:
    assert atr([10, 12, 13, 12], [8, 10, 11, 10], [9, 11, 12, 11], 2) == (
        None,
        2.5,
        2.25,
        2.125,
    )


def test_rsi_uses_wilder_average_and_pins_flat_series_to_fifty() -> None:
    assert rsi([1, 2, 1, 3], 2) == (None, None, 50.0, 83.33333333333333)
    assert rsi([5, 5, 5], 2) == (None, None, 50.0)
    assert rsi([1, 2, 3], 2)[-1] == 100.0
    assert rsi([3, 2, 1], 2)[-1] == 0.0


def test_bollinger_bands_use_population_standard_deviation() -> None:
    middle, upper, lower = bollinger_bands([1, 3, 5], period=2)
    assert middle == (None, 2.0, 4.0)
    assert upper == (None, 4.0, 6.0)
    assert lower == (None, 0.0, 2.0)


def test_zscore_compares_current_value_only_with_prior_window() -> None:
    result = zscore([1, 3, 5, 7], window=2)
    assert result[:2] == (None, None)
    assert result[2:] == (3.0, 3.0)


def test_zscore_is_missing_when_prior_window_has_zero_variance() -> None:
    assert zscore([2, 2, 2], window=2) == (None, None, None)


def test_vwap_is_cumulative_for_the_supplied_session() -> None:
    assert vwap([10, 20, 30], [1, 3, 0]) == (10.0, 17.5, 17.5)


def test_vwap_is_missing_until_positive_volume_is_seen() -> None:
    assert vwap([10, 20, 30], [0, 0, 2]) == (None, None, 30.0)


@pytest.mark.parametrize("period", [0, -1])
def test_rolling_features_reject_non_positive_period(period: int) -> None:
    with pytest.raises(ValueError, match="số nguyên dương"):
        sma([1, 2, 3], period)


def test_true_range_rejects_mismatched_bars_and_invalid_ohlc() -> None:
    with pytest.raises(ValueError, match="cùng độ dài"):
        true_range([2, 3], [1], [1, 2])
    with pytest.raises(ValueError, match="thấp.*cao"):
        true_range([2], [3], [2])


def test_indicators_reject_non_finite_inputs() -> None:
    with pytest.raises(ValueError, match="hữu hạn"):
        sma([1, math.nan, 3], 2)


def test_adx_dmi_uses_wilder_seed_and_returns_aligned_series() -> None:
    positive_di, negative_di, dx, adx = adx_dmi(
        [10, 12, 13, 13, 14], [8, 10, 11, 10, 12], [9, 11, 12, 11, 13], 2
    )
    assert positive_di[:2] == (None, None)
    assert negative_di[:2] == (None, None)
    assert positive_di[2] == pytest.approx(100.0 * 1.5 / 2.25)
    assert negative_di[2] == 0.0
    assert dx[2] == 100.0
    assert dx[3] == pytest.approx(20.0)
    assert adx[:3] == (None, None, None)
    assert adx[3] == pytest.approx(60.0)
    assert adx[4] == pytest.approx((60.0 + 55.55555555555556) / 2.0)


def test_adx_dmi_zero_ranges_and_directional_denominators_return_zero() -> None:
    positive_di, negative_di, dx, adx = adx_dmi([5, 5, 5, 5], [5, 5, 5, 5], [5, 5, 5, 5], 2)
    assert positive_di[2:] == (0.0, 0.0)
    assert negative_di[2:] == (0.0, 0.0)
    assert dx[2:] == (0.0, 0.0)
    assert adx[3] == 0.0


def test_realized_volatility_is_per_bar_and_uses_log_returns() -> None:
    result = realized_volatility([1.0, math.e, math.e**2, math.e**3], 2)
    assert result[:2] == (None, None)
    assert result[2:] == pytest.approx((1.0, 1.0))


def test_efficiency_ratio_uses_n_price_changes_and_zero_is_neutral() -> None:
    assert efficiency_ratio([100, 105, 103, 110], 2) == (
        None,
        None,
        pytest.approx(3.0 / 7.0),
        pytest.approx(5.0 / 9.0),
    )
    assert efficiency_ratio([10, 10, 10], 2) == (None, None, 0.0)


def test_new_features_validate_period_and_positive_closes() -> None:
    with pytest.raises(ValueError, match="số nguyên dương"):
        realized_volatility([1, 2], 0)
    with pytest.raises(ValueError, match="lớn hơn 0"):
        realized_volatility([1, 0, 2], 1)


def test_existing_validation_errors_are_written_in_vietnamese() -> None:
    with pytest.raises(ValueError, match="cùng độ dài"):
        true_range([2, 3], [1], [1, 2])
    with pytest.raises(ValueError, match="không được lớn hơn"):
        true_range([2], [3], [2])
