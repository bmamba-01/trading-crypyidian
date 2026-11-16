"""Ngắt mạch drawdown PAPER (FX-2): chặn mở vị thế mới khi vốn sụt sâu so với đỉnh.

Quyết định owner 15/11/2026: ngưỡng **10%** drawdown so với đỉnh; mở lại **bằng tay** — người có
quyền "Tạm dừng" rồi "Tiếp tục" bot (resume bắt buộc MFA gần đây + lý do, ghi append-only ở
``bot_runtime_control``). Mốc tiếp tục gần nhất trở thành điểm bắt đầu đo đỉnh mới.

Cách đo (``fx2-drawdown-v1``):
- Đường vốn đã chốt: ``seed + Σ net_pnl`` của trade CLOSED theo thứ tự đóng lệnh.
- Đỉnh = max(vốn đã chốt tại mốc bắt đầu, vốn đã chốt sau mỗi lệnh đóng sau mốc).
- Vốn hiện tại = vốn mark-to-market của ledger (gồm UPNL phần đang mở), nên lỗ chưa chốt cũng
  được tính — thận trọng hơn chỉ dùng lỗ đã chốt.
- drawdown = max(0, (đỉnh − vốn hiện tại) / đỉnh); ngắt khi drawdown ≥ ngưỡng.

Breaker chỉ chặn NEW_ENTRY; thoát lệnh bảo vệ không đi qua RiskEngine nên không bị ảnh hưởng.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class DrawdownBreakerPolicy:
    """Ngưỡng drawdown đã được owner chốt; đổi ngưỡng phải đổi ``version``."""

    max_drawdown_fraction: Decimal = Decimal("0.10")
    version: str = "fx2-drawdown-v1"

    def __post_init__(self) -> None:
        if not ZERO < self.max_drawdown_fraction < Decimal("1"):
            raise ValueError("Ngưỡng drawdown phải nằm trong (0, 1).")


PAPER_DRAWDOWN_BREAKER_POLICY = DrawdownBreakerPolicy()


@dataclass(frozen=True, slots=True)
class ClosedTradePnl:
    closed_at: datetime
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class DrawdownBreakerState:
    """Kết quả đo drawdown tại một mốc đánh giá, kèm bằng chứng để ghi vào context."""

    peak_equity: Decimal
    current_equity: Decimal
    drawdown_fraction: Decimal
    tripped: bool
    baseline_at: datetime | None
    policy_version: str

    def evidence(self) -> dict[str, str | None]:
        return {
            "drawdown_policy": self.policy_version,
            "drawdown_peak_equity": str(self.peak_equity),
            "drawdown_current_equity": str(self.current_equity),
            "drawdown_fraction": str(self.drawdown_fraction),
            "drawdown_baseline_at": self.baseline_at.isoformat() if self.baseline_at else None,
            "drawdown_breaker_tripped": str(self.tripped).lower(),
        }


def evaluate_drawdown_breaker(
    *,
    seed_equity: Decimal,
    closed_trades: Iterable[ClosedTradePnl],
    current_equity: Decimal,
    baseline_at: datetime | None,
    policy: DrawdownBreakerPolicy = PAPER_DRAWDOWN_BREAKER_POLICY,
) -> DrawdownBreakerState:
    """Đo drawdown từ đỉnh kể từ ``baseline_at`` (None = từ khi account bắt đầu)."""
    if not seed_equity.is_finite() or seed_equity <= ZERO:
        raise ValueError("Vốn seed phải dương và hữu hạn.")
    if not current_equity.is_finite():
        raise ValueError("Vốn hiện tại phải hữu hạn.")
    equity = seed_equity
    peak: Decimal | None = None
    for trade in sorted(closed_trades, key=lambda item: item.closed_at):
        if peak is None and (baseline_at is None or trade.closed_at > baseline_at):
            # Vốn đã chốt ngay trước lệnh đầu tiên sau mốc là điểm khởi đầu của đỉnh.
            peak = equity
        equity += trade.net_pnl
        if peak is not None:
            peak = max(peak, equity)
    if peak is None:
        # Chưa có lệnh nào đóng sau mốc: đỉnh là vốn đã chốt hiện tại.
        peak = equity
    if peak <= ZERO:
        fraction = Decimal("1")
    else:
        fraction = max(ZERO, (peak - current_equity) / peak)
    return DrawdownBreakerState(
        peak_equity=peak,
        current_equity=current_equity,
        drawdown_fraction=fraction,
        tripped=fraction >= policy.max_drawdown_fraction,
        baseline_at=baseline_at,
        policy_version=policy.version,
    )
