"""Đánh giá cooldown sau chuỗi lệnh thua liên tiếp (hàm thuần, không I/O)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

ZERO = Decimal("0")


def _require_utc_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} phải có múi giờ (UTC aware).")


@dataclass(frozen=True, slots=True)
class LossStreakCooldownPolicy:
    """Chính sách tạm nghỉ vào lệnh mới sau chuỗi thua liên tiếp."""

    max_consecutive_losses: int
    cooldown_duration: timedelta

    def __post_init__(self) -> None:
        if self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses phải >= 1.")
        if self.cooldown_duration <= timedelta(0):
            raise ValueError("cooldown_duration phải dương.")


@dataclass(frozen=True, slots=True)
class ClosedTradeOutcome:
    """Kết quả một lệnh đã đóng dùng để tính chuỗi thua theo thời gian."""

    closed_at: datetime
    net_realized_pnl: Decimal

    def __post_init__(self) -> None:
        _require_utc_aware(self.closed_at, field_name="closed_at")
        if not self.net_realized_pnl.is_finite():
            raise ValueError("net_realized_pnl phải là Decimal hữu hạn.")


@dataclass(frozen=True, slots=True)
class LossStreakCooldownState:
    """Trạng thái cooldown tại thời điểm đánh giá."""

    in_cooldown: bool
    consecutive_losses: int
    last_loss_at: datetime | None = None
    cooldown_until: datetime | None = None


def evaluate_loss_streak_cooldown(
    outcomes: Sequence[ClosedTradeOutcome],
    *,
    policy: LossStreakCooldownPolicy,
    as_of: datetime,
) -> LossStreakCooldownState:
    """Tính chuỗi thua liên tiếp đến ``as_of``; lệnh thắng (>= 0) hoặc hết hạn sẽ mở lại."""
    _require_utc_aware(as_of, field_name="as_of")
    ordered = sorted(
        (item for item in outcomes if item.closed_at <= as_of),
        key=lambda item: item.closed_at,
    )
    consecutive_losses = 0
    last_loss_at: datetime | None = None
    for item in reversed(ordered):
        if item.net_realized_pnl < ZERO:
            consecutive_losses += 1
            if last_loss_at is None:
                last_loss_at = item.closed_at
        else:
            break

    if consecutive_losses < policy.max_consecutive_losses or last_loss_at is None:
        return LossStreakCooldownState(
            in_cooldown=False,
            consecutive_losses=consecutive_losses,
            last_loss_at=last_loss_at,
            cooldown_until=None,
        )

    cooldown_until = last_loss_at + policy.cooldown_duration
    return LossStreakCooldownState(
        in_cooldown=as_of < cooldown_until,
        consecutive_losses=consecutive_losses,
        last_loss_at=last_loss_at,
        cooldown_until=cooldown_until,
    )
