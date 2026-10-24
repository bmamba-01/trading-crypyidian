"""Tính các chỉ báo thuần từ chuỗi giá đã được sắp theo thời gian.

Các hàm trả tuple có cùng độ dài đầu vào; phần chưa đủ dữ liệu được đánh dấu
bằng ``None``. Đầu vào phải là giá trị số hữu hạn, không chứa khoảng trống.
Việc kiểm tra nến đóng, dữ liệu cũ và thứ tự thời gian thuộc tầng gọi.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

FeatureSeries = tuple[float | None, ...]


def _period_value(period: int) -> int:
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise ValueError("period phải là số nguyên dương")
    return period


def _values(values: Sequence[float], name: str = "Dữ liệu") -> tuple[float, ...]:
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} phải chứa số hữu hạn") from error
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} phải chứa số hữu hạn")
    return result


def _aligned(*series: Sequence[float]) -> tuple[tuple[float, ...], ...]:
    normalized = tuple(_values(values) for values in series)
    if any(len(values) != len(normalized[0]) for values in normalized[1:]):
        raise ValueError("Các chuỗi OHLC phải có cùng độ dài")
    return normalized


def sma(values: Sequence[float], period: int) -> FeatureSeries:
    """Tính SMA; seed là trung bình cửa sổ đầu tiên gồm đủ ``period`` mẫu."""
    window = _period_value(period)
    samples = _values(values)
    output: list[float | None] = [None] * len(samples)
    if len(samples) < window:
        return tuple(output)
    for index in range(window - 1, len(samples)):
        output[index] = math.fsum(samples[index - window + 1 : index + 1]) / window
    return tuple(output)


def ema(values: Sequence[float], period: int) -> FeatureSeries:
    """Tính EMA với alpha ``2/(period+1)`` và seed bằng SMA đầu tiên."""
    window = _period_value(period)
    samples = _values(values)
    output: list[float | None] = [None] * len(samples)
    if len(samples) < window:
        return tuple(output)
    current = math.fsum(samples[:window]) / window
    output[window - 1] = current
    alpha = 2.0 / (window + 1)
    for index in range(window, len(samples)):
        current = alpha * samples[index] + (1.0 - alpha) * current
        output[index] = current
    return tuple(output)


def true_range(
    high: Sequence[float], low: Sequence[float], close: Sequence[float]
) -> tuple[float, ...]:
    """Tính TR; nến đầu tiên dùng high-low vì chưa có close trước đó."""
    highs, lows, closes = _aligned(high, low, close)
    if any(bar_low > bar_high for bar_high, bar_low in zip(highs, lows, strict=True)):
        raise ValueError("Giá thấp không được lớn hơn giá cao")
    output: list[float] = []
    for index, (bar_high, bar_low) in enumerate(zip(highs, lows, strict=True)):
        if index == 0:
            output.append(bar_high - bar_low)
            continue
        previous_close = closes[index - 1]
        output.append(
            max(
                bar_high - bar_low,
                abs(bar_high - previous_close),
                abs(bar_low - previous_close),
            )
        )
    return tuple(output)


def atr(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int
) -> FeatureSeries:
    """Tính ATR theo Wilder: seed SMA của TR đầu tiên, sau đó dùng RMA."""
    window = _period_value(period)
    ranges = true_range(high, low, close)
    output: list[float | None] = [None] * len(ranges)
    if len(ranges) < window:
        return tuple(output)
    current = math.fsum(ranges[:window]) / window
    output[window - 1] = current
    for index in range(window, len(ranges)):
        current = ((window - 1) * current + ranges[index]) / window
        output[index] = current
    return tuple(output)


def adx_dmi(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int
) -> tuple[FeatureSeries, FeatureSeries, FeatureSeries, FeatureSeries]:
    """Trả lần lượt +DI, -DI, DX và ADX theo công thức Wilder của dự án.

    +DI/-DI bắt đầu sau ``period`` biến động giá. ADX cần thêm ``period`` mẫu
    DX hoàn chỉnh; do đó điểm đầu tiên là chỉ số ``2*period-1``. Mọi RMA được
    seed bằng trung bình số học của ``period`` mẫu đầu tiên.
    """
    window = _period_value(period)
    highs, lows, closes = _aligned(high, low, close)
    average_ranges = atr(highs, lows, closes, window)
    length = len(highs)
    positive_di: list[float | None] = [None] * length
    negative_di: list[float | None] = [None] * length
    dx_values: list[float | None] = [None] * length
    adx_values: list[float | None] = [None] * length
    if length <= window:
        return tuple(positive_di), tuple(negative_di), tuple(dx_values), tuple(adx_values)

    positive_dm = [0.0] * length
    negative_dm = [0.0] * length
    for index in range(1, length):
        upward_move = highs[index] - highs[index - 1]
        downward_move = lows[index - 1] - lows[index]
        if upward_move > downward_move and upward_move > 0.0:
            positive_dm[index] = upward_move
        elif downward_move > upward_move and downward_move > 0.0:
            negative_dm[index] = downward_move

    smoothed_positive = math.fsum(positive_dm[1 : window + 1]) / window
    smoothed_negative = math.fsum(negative_dm[1 : window + 1]) / window
    for index in range(window, length):
        if index > window:
            smoothed_positive = ((window - 1) * smoothed_positive + positive_dm[index]) / window
            smoothed_negative = ((window - 1) * smoothed_negative + negative_dm[index]) / window

        average_range = average_ranges[index]
        if average_range is None or average_range == 0.0:
            positive = 0.0
            negative = 0.0
        else:
            positive = 100.0 * smoothed_positive / average_range
            negative = 100.0 * smoothed_negative / average_range
        positive_di[index] = positive
        negative_di[index] = negative
        directional_sum = positive + negative
        dx_values[index] = (
            100.0 * abs(positive - negative) / directional_sum if directional_sum > 0.0 else 0.0
        )

    first_adx_index = 2 * window - 1
    if length <= first_adx_index:
        return tuple(positive_di), tuple(negative_di), tuple(dx_values), tuple(adx_values)

    initial_dx = [value for value in dx_values[window : first_adx_index + 1] if value is not None]
    current_adx = math.fsum(initial_dx) / window
    adx_values[first_adx_index] = current_adx
    for index in range(first_adx_index + 1, length):
        current_dx = dx_values[index]
        assert current_dx is not None
        current_adx = ((window - 1) * current_adx + current_dx) / window
        adx_values[index] = current_adx
    return tuple(positive_di), tuple(negative_di), tuple(dx_values), tuple(adx_values)


def rsi(close: Sequence[float], period: int) -> FeatureSeries:
    """Tính RSI Wilder; cần ``period`` biến động và flat window trả về 50."""
    window = _period_value(period)
    prices = _values(close, "Giá đóng cửa")
    output: list[float | None] = [None] * len(prices)
    if len(prices) <= window:
        return tuple(output)

    changes = tuple(prices[index] - prices[index - 1] for index in range(1, len(prices)))
    gains = tuple(max(change, 0.0) for change in changes)
    losses = tuple(max(-change, 0.0) for change in changes)
    average_gain = math.fsum(gains[:window]) / window
    average_loss = math.fsum(losses[:window]) / window

    def value(gain: float, loss: float) -> float:
        if gain == 0.0 and loss == 0.0:
            return 50.0
        if loss == 0.0:
            return 100.0
        if gain == 0.0:
            return 0.0
        relative_strength = gain / loss
        return 100.0 - 100.0 / (1.0 + relative_strength)

    output[window] = value(average_gain, average_loss)
    for change_index in range(window, len(changes)):
        average_gain = ((window - 1) * average_gain + gains[change_index]) / window
        average_loss = ((window - 1) * average_loss + losses[change_index]) / window
        output[change_index + 1] = value(average_gain, average_loss)
    return tuple(output)


def realized_volatility(close: Sequence[float], period: int) -> FeatureSeries:
    """Tính độ biến động thực hiện mỗi nến: căn bậc hai trung bình ``r²``.

    Lợi suất là log-return giữa hai giá đóng cửa liên tiếp; cần đủ ``period``
    lợi suất nên warm-up có độ dài ``period`` giá. Hàm không annualize kết quả.
    """
    window = _period_value(period)
    prices = _values(close, "Giá đóng cửa")
    if any(price <= 0.0 for price in prices):
        raise ValueError("Giá đóng cửa phải lớn hơn 0 để tính log-return")
    returns = tuple(
        math.log(prices[index]) - math.log(prices[index - 1]) for index in range(1, len(prices))
    )
    output: list[float | None] = [None] * len(prices)
    for index in range(window, len(prices)):
        sample = returns[index - window : index]
        output[index] = math.sqrt(math.fsum(value * value for value in sample) / window)
    return tuple(output)


def efficiency_ratio(close: Sequence[float], period: int) -> FeatureSeries:
    """Tính ER bằng dịch chuyển ròng chia tổng độ dài đường đi trong cửa sổ."""
    window = _period_value(period)
    prices = _values(close, "Giá đóng cửa")
    output: list[float | None] = [None] * len(prices)
    for index in range(window, len(prices)):
        net_change = abs(prices[index] - prices[index - window])
        path_length = math.fsum(
            abs(prices[position] - prices[position - 1])
            for position in range(index - window + 1, index + 1)
        )
        output[index] = net_change / path_length if path_length > 0.0 else 0.0
    return tuple(output)


def bollinger_bands(
    close: Sequence[float], period: int = 20, deviations: float = 2.0
) -> tuple[FeatureSeries, FeatureSeries, FeatureSeries]:
    """Trả middle, upper, lower; độ lệch chuẩn dùng ddof=0 (population)."""
    window = _period_value(period)
    width = float(deviations)
    if not math.isfinite(width) or width < 0.0:
        raise ValueError("Độ lệch phải là số hữu hạn không âm")
    prices = _values(close, "Giá đóng cửa")
    middle: list[float | None] = [None] * len(prices)
    upper: list[float | None] = [None] * len(prices)
    lower: list[float | None] = [None] * len(prices)
    for index in range(window - 1, len(prices)):
        sample = prices[index - window + 1 : index + 1]
        mean = math.fsum(sample) / window
        standard_deviation = math.sqrt(math.fsum((value - mean) ** 2 for value in sample) / window)
        middle[index] = mean
        upper[index] = mean + width * standard_deviation
        lower[index] = mean - width * standard_deviation
    return tuple(middle), tuple(upper), tuple(lower)


def zscore(values: Sequence[float], window: int) -> FeatureSeries:
    """Chuẩn hóa mẫu hiện tại theo ``window`` mẫu trước đó (không gồm hiện tại).

    Dùng độ lệch chuẩn population. Cửa sổ lịch sử có phương sai bằng 0 được
    đánh dấu ``None`` để caller không diễn giải thành một tín hiệu hợp lệ.
    """
    lookback = _period_value(window)
    samples = _values(values)
    output: list[float | None] = [None] * len(samples)
    for index in range(lookback, len(samples)):
        history = samples[index - lookback : index]
        mean = math.fsum(history) / lookback
        standard_deviation = math.sqrt(
            math.fsum((value - mean) ** 2 for value in history) / lookback
        )
        if standard_deviation == 0.0:
            continue
        output[index] = (samples[index] - mean) / standard_deviation
    return tuple(output)


def vwap(price: Sequence[float], volume: Sequence[float]) -> FeatureSeries:
    """Tính VWAP tích lũy cho đúng một session được truyền vào.

    Hàm không tự xác định/mở phiên: caller phải chia dữ liệu theo session UTC
    và chỉ truyền giá/volume đã sẵn sàng. Mẫu trước khi có volume dương là None.
    """
    prices, volumes = _aligned(price, volume)
    if any(value < 0.0 for value in volumes):
        raise ValueError("Khối lượng không được âm")
    total_volume = 0.0
    weighted_price = 0.0
    output: list[float | None] = []
    for sample_price, sample_volume in zip(prices, volumes, strict=True):
        total_volume += sample_volume
        weighted_price += sample_price * sample_volume
        output.append(weighted_price / total_volume if total_volume > 0.0 else None)
    return tuple(output)
