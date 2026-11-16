"""Backtest danh mục nhiều coin từ lệnh replay theo từng coin (CRY-386, AI-3/portfolio-backtest).

Replay hiện chạy mỗi coin độc lập (vốn riêng, 1 vị thế, notional ≤ 1x vốn coin). Mô phỏng danh mục
gộp các lệnh đó theo thời gian trên **một vốn chung** và áp hai trần của cấu hình bot:

- ``max_positions``: số vị thế mở đồng thời; lệnh mới khi đã đủ thì bị bỏ (như runtime
  ``PLAN:MAX_POSITIONS_REACHED``).
- ``max_leverage``: tổng notional đang mở ≤ vốn × đòn bẩy; lệnh mới làm vượt thì bị bỏ.

Kích thước lệnh tỷ lệ với vốn: lệnh replay mở khi coin có vốn ``E_coin`` với notional ``N`` được
phóng theo ``E_danh_mục / E_coin`` (cùng tỷ lệ rủi ro mỗi lệnh), lãi/lỗ phóng cùng tỷ lệ.

Giới hạn (xấp xỉ, ghi rõ trong kết quả): (1) lệnh bị bỏ vì trần không sinh lại tín hiệu thay thế
trong cùng coin như runtime thật có thể; (2) mỗi lệnh vẫn ≤ 1x vốn vì replay đã chặn ở 1x, nên đòn
bẩy chỉ nới tổng notional đồng thời, không làm lệnh đơn lớn hơn; (3) lãi/lỗ ghi khi đóng lệnh,
drawdown tính trên vốn đã chốt (không theo giá đánh dấu).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class PortfolioTrade:
    """Một lệnh đã đóng trong replay của một coin."""

    symbol: str
    opened_at: datetime
    closed_at: datetime
    net_pnl: Decimal
    exposure: Decimal
    symbol_equity_at_entry: Decimal

    def __post_init__(self) -> None:
        if self.closed_at < self.opened_at:
            raise ValueError("Lệnh không thể đóng trước khi mở.")
        if self.exposure < 0 or self.symbol_equity_at_entry <= 0:
            raise ValueError("Notional phải không âm và vốn coin phải dương.")


@dataclass(frozen=True, slots=True)
class PortfolioLimits:
    max_positions: int
    max_leverage: Decimal

    def __post_init__(self) -> None:
        if self.max_positions < 1 or self.max_leverage <= 0:
            raise ValueError("Trần danh mục không hợp lệ.")

    def payload(self) -> dict[str, str | int]:
        return {"max_positions": self.max_positions, "max_leverage": str(self.max_leverage)}


@dataclass(frozen=True, slots=True)
class PortfolioResult:
    taken: int
    skipped_positions: int
    skipped_leverage: int
    wins: int
    gross_profit: Decimal
    gross_loss: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    ending_equity: Decimal
    peak_open_positions: int
    symbols: int


def trades_with_symbol_equity(
    symbol: str,
    trades: Iterable[tuple[datetime, datetime, Decimal, Decimal]],
    starting_equity: Decimal,
) -> list[PortfolioTrade]:
    """Lệnh ``(opened_at, closed_at, net_pnl, exposure)`` của một coin → kèm vốn coin lúc vào.

    Vốn coin lúc vào = vốn đầu + lãi/lỗ các lệnh đã đóng **trước** thời điểm vào (replay một coin
    giữ tối đa 1 vị thế nên các lệnh không chồng nhau).
    """
    ordered = sorted(trades, key=lambda item: (item[0], item[1]))
    result: list[PortfolioTrade] = []
    for opened_at, closed_at, net_pnl, exposure in ordered:
        realized = sum((t.net_pnl for t in result if t.closed_at <= opened_at), ZERO)
        result.append(
            PortfolioTrade(
                symbol, opened_at, closed_at, net_pnl, exposure, starting_equity + realized
            )
        )
    return result


def simulate_portfolio(
    trades: Sequence[PortfolioTrade], *, starting_equity: Decimal, limits: PortfolioLimits
) -> PortfolioResult:
    """Chạy theo thời gian: đóng lệnh trước, rồi xét mở lệnh tại cùng mốc (giải phóng chỗ trước)."""
    if starting_equity <= 0:
        raise ValueError("Vốn danh mục phải dương.")
    events: list[tuple[datetime, int, int]] = []  # (mốc, 0=đóng/1=mở, chỉ số lệnh)
    for index, trade in enumerate(trades):
        events.append((trade.opened_at, 1, index))
        events.append((trade.closed_at, 0, index))
    events.sort(key=lambda item: (item[0], item[1], trades[item[2]].symbol, item[2]))
    equity = starting_equity
    peak = starting_equity
    drawdown = ZERO
    open_trades: dict[int, tuple[Decimal, Decimal]] = {}  # chỉ số → (hệ số phóng, notional)
    open_symbols: set[str] = set()
    taken = skipped_positions = skipped_leverage = wins = peak_open = 0
    profit = loss = ZERO
    used_symbols: set[str] = set()
    for _moment, kind, index in events:
        trade = trades[index]
        if kind == 0:
            position = open_trades.pop(index, None)
            if position is None:
                continue  # lệnh đã bị bỏ lúc mở
            open_symbols.discard(trade.symbol)
            pnl = trade.net_pnl * position[0]
            equity += pnl
            if pnl > 0:
                wins += 1
                profit += pnl
            elif pnl < 0:
                loss -= pnl
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
            continue
        if trade.symbol in open_symbols:
            continue  # phòng thủ: replay một coin không chồng lệnh
        if len(open_trades) >= limits.max_positions:
            skipped_positions += 1
            continue
        scale = equity / trade.symbol_equity_at_entry if equity > 0 else ZERO
        notional = trade.exposure * scale
        open_notional = sum((item[1] for item in open_trades.values()), ZERO)
        if scale <= 0 or open_notional + notional > equity * limits.max_leverage:
            skipped_leverage += 1
            continue
        open_trades[index] = (scale, notional)
        open_symbols.add(trade.symbol)
        used_symbols.add(trade.symbol)
        taken += 1
        peak_open = max(peak_open, len(open_trades))
    return PortfolioResult(
        taken=taken,
        skipped_positions=skipped_positions,
        skipped_leverage=skipped_leverage,
        wins=wins,
        gross_profit=profit,
        gross_loss=loss,
        net_pnl=equity - starting_equity,
        max_drawdown=drawdown,
        ending_equity=equity,
        peak_open_positions=peak_open,
        symbols=len(used_symbols),
    )


__all__ = [
    "PortfolioLimits",
    "PortfolioResult",
    "PortfolioTrade",
    "simulate_portfolio",
    "trades_with_symbol_equity",
]
