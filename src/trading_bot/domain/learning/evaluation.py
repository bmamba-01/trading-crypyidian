"""Chia tập thời gian và tính số liệu đánh giá thuần, có thể tái lập."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

ZERO = Decimal("0")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


@dataclass(frozen=True, slots=True)
class TemporalWindow:
    """Khoảng thời gian nửa mở [start, end), dùng để tránh chồng lấn mẫu."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _aware(self.start, "start")
        _aware(self.end, "end")
        if self.start >= self.end:
            raise ValueError("Temporal window phải có start < end.")


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    """Một fold huấn luyện, validation và test theo đúng thứ tự thời gian."""

    train: TemporalWindow
    validation: TemporalWindow
    test: TemporalWindow


def walk_forward_splits(
    data_range: TemporalWindow,
    *,
    train: timedelta,
    validation: timedelta,
    test: timedelta,
    step: timedelta,
    purge: timedelta = timedelta(0),
    embargo: timedelta = timedelta(0),
) -> tuple[WalkForwardSplit, ...]:
    """Tạo fold cuốn chiếu; purge/embargo là khoảng bỏ trống giữa các cửa sổ."""
    durations = (train, validation, test, step)
    if any(duration <= timedelta(0) for duration in durations):
        raise ValueError("Train, validation, test và step phải dương.")
    if purge < timedelta(0) or embargo < timedelta(0):
        raise ValueError("Purge và embargo không được âm.")

    result: list[WalkForwardSplit] = []
    train_start = data_range.start
    while True:
        train_end = train_start + train
        validation_start = train_end + purge
        validation_end = validation_start + validation
        test_start = validation_end + embargo
        test_end = test_start + test
        if test_end > data_range.end:
            break
        result.append(
            WalkForwardSplit(
                TemporalWindow(train_start, train_end),
                TemporalWindow(validation_start, validation_end),
                TemporalWindow(test_start, test_end),
            )
        )
        train_start += step
    return tuple(result)


@dataclass(frozen=True, slots=True)
class EvaluationTrade:
    """Kết quả một giao dịch đã đóng; các chi phí được ghi tách riêng."""

    trade_id: str
    opened_at: datetime
    closed_at: datetime
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    exposure: Decimal

    def __post_init__(self) -> None:
        if not self.trade_id.strip():
            raise ValueError("trade_id không được để trống.")
        _aware(self.opened_at, "opened_at")
        _aware(self.closed_at, "closed_at")
        if self.closed_at < self.opened_at:
            raise ValueError("Giao dịch không thể đóng trước khi mở.")
        if not isinstance(self.gross_pnl, Decimal) or not self.gross_pnl.is_finite():
            raise ValueError("gross_pnl phải là Decimal hữu hạn.")
        for name in ("fees", "slippage", "exposure"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
                raise ValueError(f"{name} phải là Decimal hữu hạn không âm.")
        if not isinstance(self.funding, Decimal) or not self.funding.is_finite():
            raise ValueError("funding phải là dòng tiền Decimal hữu hạn có dấu.")

    @property
    def net_pnl(self) -> Decimal:
        """Lãi/lỗ sau phí, funding và trượt giá, mỗi khoản chỉ trừ một lần."""
        return self.gross_pnl - self.fees - self.funding - self.slippage


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """Số liệu net, chi phí, độ phủ và khoảng bất định của một run."""

    observation_count: int
    usable_observation_count: int
    trade_count: int
    coverage: float
    trade_frequency: float
    gross_pnl: Decimal
    net_pnl: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    expectancy: Decimal
    max_drawdown: Decimal
    average_exposure: Decimal
    confidence_interval: tuple[Decimal, Decimal] | None
    caveat_vi: str


def build_evaluation_metrics(
    trades: tuple[EvaluationTrade, ...],
    *,
    observation_count: int,
    seed: int,
    usable_observation_count: int | None = None,
) -> EvaluationMetrics:
    """Tính báo cáo và khoảng tin cậy bootstrap theo block liên tiếp."""
    usable_count = (
        observation_count if usable_observation_count is None else usable_observation_count
    )
    if (
        observation_count < 0
        or usable_count < 0
        or usable_count > observation_count
        or len(trades) > usable_count
    ):
        raise ValueError("Số quan sát usable phải nằm trong tổng và chứa đủ giao dịch.")
    if seed < 0:
        raise ValueError("Seed phải không âm.")
    ids = tuple(trade.trade_id for trade in trades)
    if len(ids) != len(set(ids)):
        raise ValueError("trade_id bị trùng.")
    if any(
        left.closed_at > right.closed_at for left, right in zip(trades, trades[1:], strict=False)
    ):
        raise ValueError("Giao dịch phải được sắp theo thời điểm đóng.")
    net_values = tuple(trade.net_pnl for trade in trades)
    gross_pnl = sum((trade.gross_pnl for trade in trades), ZERO)
    net_pnl = sum(net_values, ZERO)
    fees = sum((trade.fees for trade in trades), ZERO)
    funding = sum((trade.funding for trade in trades), ZERO)
    slippage = sum((trade.slippage for trade in trades), ZERO)
    expectancy = net_pnl / len(trades) if trades else ZERO
    equity = peak = drawdown = ZERO
    for value in net_values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    average_exposure = (
        sum((trade.exposure for trade in trades), ZERO) / len(trades) if trades else ZERO
    )
    interval = _block_bootstrap_interval(net_values, seed=seed) if len(trades) >= 2 else None
    return EvaluationMetrics(
        observation_count=observation_count,
        usable_observation_count=usable_count,
        trade_count=len(trades),
        coverage=(usable_count / observation_count if observation_count else 0.0),
        trade_frequency=(len(trades) / usable_count if usable_count else 0.0),
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        fees=fees,
        funding=funding,
        slippage=slippage,
        expectancy=expectancy,
        max_drawdown=drawdown,
        average_exposure=average_exposure,
        confidence_interval=interval,
        caveat_vi=(
            "Khoảng bất định bootstrap theo block chỉ mô tả mẫu này, không phải bằng chứng "
            "lợi nhuận tương lai hay cam kết có lợi thế. Exposure là trung bình mỗi giao dịch, "
            "không phải exposure theo thời gian."
        ),
    )


def _block_bootstrap_interval(values: tuple[Decimal, ...], *, seed: int) -> tuple[Decimal, Decimal]:
    """Ước lượng CI 95% bằng lấy mẫu block tròn để giữ phụ thuộc cục bộ."""
    sample_size = len(values)
    block_size = max(1, math.isqrt(sample_size))
    generator = random.Random(seed)
    estimates: list[Decimal] = []
    for _ in range(500):
        sample: list[Decimal] = []
        while len(sample) < sample_size:
            start = generator.randrange(sample_size)
            sample.extend(values[(start + offset) % sample_size] for offset in range(block_size))
        estimates.append(sum(sample[:sample_size], ZERO) / sample_size)
    estimates.sort()
    low = estimates[int(0.025 * (len(estimates) - 1))]
    high = estimates[int(0.975 * (len(estimates) - 1))]
    return low, high
