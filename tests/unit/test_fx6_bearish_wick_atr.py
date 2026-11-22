from decimal import Decimal
from types import SimpleNamespace

from trading_bot.application.strategy_pipeline.pipeline_engine import StrategyPipelineEngine
from trading_bot.domain.strategy_card.anatomy import (
    bearish_wick_rejection_strength,
    body_ratio,
    bullish_wick_rejection_strength,
    close_location,
    normalized_candle_range,
    upper_wick_ratio,
    wick_rejection_strength,
)
from trading_bot.domain.strategy_card.operations import is_registered_operation


def test_bearish_wick_rejection_strength_shooting_star() -> None:
    """Nến Shooting Star kinh điển: râu trên dài 80%, thân nhỏ 10%, đóng sát đáy (10%)."""
    # Open=102, High=110, Low=100, Close=101
    open_p, high_p, low_p, close_p = (
        Decimal("102"),
        Decimal("110"),
        Decimal("100"),
        Decimal("101"),
    )

    uw = upper_wick_ratio(open_p, high_p, low_p, close_p)
    cl = close_location(open_p, high_p, low_p, close_p)
    br = body_ratio(open_p, high_p, low_p, close_p)

    assert uw == Decimal("0.8")
    assert cl == Decimal("0.1")
    assert br == Decimal("0.1")

    # strength = uw * (1 - cl) * (1 - br) = 0.8 * 0.9 * 0.9 = 0.648
    strength = bearish_wick_rejection_strength(open_p, high_p, low_p, close_p)
    assert strength == Decimal("0.648")


def test_bearish_and_bullish_wick_rejection_symmetry() -> None:
    """Kiểm tra tính đối xứng hoàn hảo giữa Bullish Hammer và Bearish Shooting Star."""
    # Bullish Hammer: râu dưới dài ở đáy (Low=100, Open=108, Close=109, High=110)
    # range = 10, lower_wick = 108 - 100 = 8 -> lw = 0.8
    # close_location = (109 - 100)/10 = 0.9
    # body = |109 - 108| = 1 -> br = 0.1
    # bull_strength = 0.8 * 0.9 * 0.9 = 0.648
    bull_strength = bullish_wick_rejection_strength(
        Decimal("108"), Decimal("110"), Decimal("100"), Decimal("109")
    )
    assert bull_strength == Decimal("0.648")
    assert bull_strength == wick_rejection_strength(
        Decimal("108"), Decimal("110"), Decimal("100"), Decimal("109")
    )

    # Bearish Shooting Star lật ngược:
    # High=110, Open=102, Close=101, Low=100
    bear_strength = bearish_wick_rejection_strength(
        Decimal("102"), Decimal("110"), Decimal("100"), Decimal("101")
    )
    assert bear_strength == Decimal("0.648")


def test_bearish_wick_rejection_zero_range_safe() -> None:
    """Nến phẳng High == Low không crash chia cho 0, trả về Decimal('0')."""
    flat = Decimal("100")
    assert bearish_wick_rejection_strength(flat, flat, flat, flat) == Decimal("0")


def test_normalized_candle_range_calculation() -> None:
    """Kiểm tra chuẩn hóa biên độ nến theo ATR để phân biệt nến có lực và doji 1-2 tick."""
    # Nến biến động mạnh: High 110, Low 100, ATR 5.0 -> range = 10 / 5 = 2.0 ATR
    assert normalized_candle_range(Decimal("110"), Decimal("100"), Decimal("5.0")) == Decimal("2")
    assert normalized_candle_range(Decimal("110"), Decimal("100"), 5.0) == Decimal("2")

    # Nến doji 1 tick trong thị trường biến động 100 USD:
    # High 50000.1, Low 50000.0, ATR 100.0 -> range = 0.1 / 100 = 0.001 ATR
    tiny = normalized_candle_range(Decimal("50000.1"), Decimal("50000.0"), Decimal("100.0"))
    assert tiny == Decimal("0.001")


def test_normalized_candle_range_edge_cases_safe() -> None:
    """Xử lý an toàn các trường hợp biên của ATR và nến."""
    # ATR is None hoặc <= 0
    assert normalized_candle_range(Decimal("110"), Decimal("100"), None) == Decimal("0")
    assert normalized_candle_range(Decimal("110"), Decimal("100"), Decimal("0")) == Decimal("0")
    assert normalized_candle_range(Decimal("110"), Decimal("100"), -1.5) == Decimal("0")

    # High <= Low
    assert normalized_candle_range(Decimal("100"), Decimal("100"), Decimal("5")) == Decimal("0")
    assert normalized_candle_range(Decimal("90"), Decimal("100"), Decimal("5")) == Decimal("0")


def test_pipeline_engine_and_operations_registration() -> None:
    """Kiểm tra operation mới được đăng ký đúng và tính toán chuẩn trong PipelineEngine."""
    assert is_registered_operation("bearish_wick_rejection_strength")
    assert is_registered_operation("normalized_candle_range")

    engine = StrategyPipelineEngine()
    bar = SimpleNamespace(
        open=Decimal("102"),
        high=Decimal("110"),
        low=Decimal("100"),
        close=Decimal("101"),
        atr=Decimal("5"),
    )

    bear_val = engine._compute_l1_node("bearish_wick_rejection_strength", bar)
    assert bear_val == Decimal("0.648")

    range_val = engine._compute_l1_node("normalized_candle_range", bar)
    assert range_val == Decimal("2")
