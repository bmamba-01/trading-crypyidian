"""Thư viện khối nguyên tử giải phẫu nến (Candle Anatomy Primitives) theo QUANTSMART.

Các hàm đo lường hình học nến chuẩn xác với Decimal, chống lỗi chia cho 0 khi High == Low.
"""

from __future__ import annotations

from decimal import Decimal


def lower_wick_ratio(open_price: Decimal, high: Decimal, low: Decimal, close: Decimal) -> Decimal:
    """Tính tỷ lệ râu nến dưới so với tổng biên độ nến.

    Công thức: (min(open, close) - low) / (high - low).
    Nếu high == low (nến phẳng không có biên độ), trả về 0 để chống chia cho 0.
    """
    candle_range = high - low
    if candle_range <= Decimal("0"):
        return Decimal("0")
    lower_wick = min(open_price, close) - low
    return lower_wick / candle_range


def upper_wick_ratio(open_price: Decimal, high: Decimal, low: Decimal, close: Decimal) -> Decimal:
    """Tính tỷ lệ râu nến trên so với tổng biên độ nến.

    Công thức: (high - max(open, close)) / (high - low).
    Nếu high == low (nến phẳng không có biên độ), trả về 0 để chống chia cho 0.
    """
    candle_range = high - low
    if candle_range <= Decimal("0"):
        return Decimal("0")
    upper_wick = high - max(open_price, close)
    return upper_wick / candle_range


def body_ratio(open_price: Decimal, high: Decimal, low: Decimal, close: Decimal) -> Decimal:
    """Tính tỷ lệ thân nến so với tổng biên độ nến.

    Công thức: abs(close - open) / (high - low).
    Nếu high == low (nến phẳng không có biên độ), trả về 0 để chống chia cho 0.
    """
    candle_range = high - low
    if candle_range <= Decimal("0"):
        return Decimal("0")
    body = abs(close - open_price)
    return body / candle_range


def close_location(open_price: Decimal, high: Decimal, low: Decimal, close: Decimal) -> Decimal:
    """Tính vị trí tương đối của giá đóng cửa trong dải giá [low, high] (khoảng [0, 1]).

    Công thức: (close - low) / (high - low).
    Nếu high == low (nến phẳng không có biên độ), trả về Decimal('0.5') đại diện
    cho vị trí trung lập chính giữa dải giá, chống chia cho 0 an toàn.
    """
    candle_range = high - low
    if candle_range <= Decimal("0"):
        return Decimal("0.5")
    return (close - low) / candle_range


def wick_rejection_strength(
    open_price: Decimal, high: Decimal, low: Decimal, close: Decimal
) -> Decimal:
    """Tính sức mạnh từ chối giá của râu nến dưới (Wick Rejection Strength).

    Công thức kết hợp: lower_wick_ratio * close_location * (1 - body_ratio).
    Đo lường mức độ lực mua đẩy giá lên từ đáy khi tạo râu dưới dài và đóng gần đỉnh.
    """
    lw = lower_wick_ratio(open_price, high, low, close)
    cl = close_location(open_price, high, low, close)
    br = body_ratio(open_price, high, low, close)
    return lw * cl * (Decimal("1") - br)
