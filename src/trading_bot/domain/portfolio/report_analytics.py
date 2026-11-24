"""Bộ lọc server-side, phân rã chiều phân tích, so sánh khoảng và report hash WP-07D (CRY-371)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from trading_bot.domain.portfolio.reporting import canonical_decimal, exact_decimal, report_hash

_RATIO_QUANT = Decimal("0.00000001")
_WEEKDAYS_VI: tuple[str, ...] = (
    "Thứ 2",
    "Thứ 3",
    "Thứ 4",
    "Thứ 5",
    "Thứ 6",
    "Thứ 7",
    "Chủ nhật",
)


@dataclass(frozen=True, slots=True)
class ReportFilterCriteria:
    """Bộ lọc server-side áp trên tập trade đã phân quyền (không vượt phạm vi bot/workspace)."""

    symbol: str | None = None
    side: str | None = None
    timeframe: str | None = None
    strategy: str | None = None
    session_name: str | None = None
    weekday: int | None = None

    def normalized(self) -> ReportFilterCriteria:
        sym = self.symbol.strip().upper() if self.symbol and self.symbol.strip() else None
        sd = self.side.strip().upper() if self.side and self.side.strip() else None
        tf = self.timeframe.strip().lower() if self.timeframe and self.timeframe.strip() else None
        strat = self.strategy.strip() if self.strategy and self.strategy.strip() else None
        sess = (
            self.session_name.strip().upper()
            if self.session_name and self.session_name.strip()
            else None
        )
        wd = self.weekday if self.weekday is not None and 0 <= self.weekday <= 6 else None
        return ReportFilterCriteria(
            symbol=sym,
            side=sd,
            timeframe=tf,
            strategy=strat,
            session_name=sess,
            weekday=wd,
        )

    def is_empty(self) -> bool:
        n = self.normalized()
        return (
            n.symbol is None
            and n.side is None
            and n.timeframe is None
            and n.strategy is None
            and n.session_name is None
            and n.weekday is None
        )

    def as_dict(self) -> dict[str, Any]:
        n = self.normalized()
        return {
            "symbol": n.symbol,
            "side": n.side,
            "timeframe": n.timeframe,
            "strategy": n.strategy,
            "session_name": n.session_name,
            "weekday": n.weekday,
        }


def _parse_utc_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _utc_session(dt: datetime | None) -> str:
    """Phân phiên thị trường theo giờ UTC (không phụ thuộc múi giờ hiển thị)."""
    if dt is None:
        return "UNKNOWN"
    hour = dt.astimezone(UTC).hour
    if 0 <= hour < 8:
        return "ASIA"
    if 8 <= hour < 13:
        return "LONDON"
    if 13 <= hour < 16:
        return "OVERLAP_LON_NY"
    return "NEW_YORK"


def _trade_timeframe(trade: Mapping[str, Any]) -> str:
    tf = trade.get("timeframe")
    if isinstance(tf, str) and tf.strip():
        return tf.strip().lower()
    exp = trade.get("explanation")
    if isinstance(exp, Mapping):
        etf = exp.get("timeframe")
        if isinstance(etf, str) and etf.strip():
            return etf.strip().lower()
    return "UNKNOWN"


def _trade_strategy(trade: Mapping[str, Any]) -> str:
    rel = trade.get("strategy_release_id")
    if isinstance(rel, str) and rel.strip():
        return rel.strip()
    exp = trade.get("explanation")
    if isinstance(exp, Mapping):
        sid = exp.get("strategy_release_id") or exp.get("strategy_id")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    return "UNKNOWN"


def _dec(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def filter_report_trades(
    trades: Sequence[Mapping[str, Any]], criteria: ReportFilterCriteria
) -> list[dict[str, Any]]:
    """Lọc trade đã phân quyền theo symbol, side, timeframe, strategy, session, weekday."""
    n = criteria.normalized()
    matched: list[dict[str, Any]] = []
    for raw in trades:
        if n.symbol is not None and str(raw.get("symbol") or "").upper() != n.symbol:
            continue
        if n.side is not None and str(raw.get("side") or "").upper() != n.side:
            continue
        if n.timeframe is not None and _trade_timeframe(raw) != n.timeframe:
            continue
        if n.strategy is not None and _trade_strategy(raw).lower() != n.strategy.lower():
            continue
        closed_dt = _parse_utc_dt(raw.get("closed_at"))
        if n.session_name is not None and _utc_session(closed_dt) != n.session_name:
            continue
        if n.weekday is not None and (closed_dt is None or closed_dt.weekday() != n.weekday):
            continue
        matched.append(dict(raw))
    return matched


def reconcile_filtered_totals(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Tính lại tổng hợp PnL, chi phí, win rate và profit factor đối khớp 100% ledger."""
    with exact_decimal():
        gross = Decimal("0")
        net = Decimal("0")
        fees = Decimal("0")
        funding = Decimal("0")
        slippage = Decimal("0")
        gross_wins = Decimal("0")
        gross_losses = Decimal("0")
        wins = 0
        losses = 0
        breakeven = 0

        for trade in trades:
            t_net = _dec(trade.get("net_pnl"))
            t_gross = _dec(trade.get("gross_pnl"))
            gross += t_gross
            net += t_net
            fees += _dec(trade.get("fees"))
            funding += _dec(trade.get("funding"))
            slippage += _dec(trade.get("slippage"))
            if t_net > 0:
                wins += 1
                gross_wins += t_net
            elif t_net < 0:
                losses += 1
                gross_losses += abs(t_net)
            else:
                breakeven += 1

        total_count = len(trades)
        win_rate: str | None = None
        if total_count > 0:
            win_rate = canonical_decimal(
                (Decimal(wins) / Decimal(total_count)).quantize(
                    _RATIO_QUANT, rounding=ROUND_HALF_UP
                )
            )
        profit_factor: str | None = None
        if gross_losses > 0:
            profit_factor = canonical_decimal(
                (gross_wins / gross_losses).quantize(_RATIO_QUANT, rounding=ROUND_HALF_UP)
            )

        return {
            "trade_count": total_count,
            "winning_trades": wins,
            "losing_trades": losses,
            "breakeven_trades": breakeven,
            "gross_pnl": canonical_decimal(gross),
            "net_pnl": canonical_decimal(net),
            "fees": canonical_decimal(fees),
            "funding": canonical_decimal(funding),
            "slippage": canonical_decimal(slippage),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
        }


def _group_breakdown(
    trades: Sequence[Mapping[str, Any]], key_fn: Any
) -> list[dict[str, Any]]:
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for trade in trades:
        label = str(key_fn(trade))
        buckets.setdefault(label, []).append(trade)

    rows: list[dict[str, Any]] = []
    for label in sorted(buckets.keys()):
        totals = reconcile_filtered_totals(buckets[label])
        rows.append({"key": label, **totals})
    return rows


def breakdown_trades(trades: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Phân rã giao dịch theo strategy, symbol, side, timeframe, session (UTC) và thứ (UTC)."""
    return {
        "by_strategy": _group_breakdown(trades, _trade_strategy),
        "by_symbol": _group_breakdown(trades, lambda t: str(t.get("symbol") or "UNKNOWN")),
        "by_side": _group_breakdown(trades, lambda t: str(t.get("side") or "UNKNOWN")),
        "by_timeframe": _group_breakdown(trades, _trade_timeframe),
        "by_session": _group_breakdown(
            trades, lambda t: _utc_session(_parse_utc_dt(t.get("closed_at")))
        ),
        "by_weekday": _group_breakdown(
            trades,
            lambda t: (
                _WEEKDAYS_VI[dt.weekday()]
                if (dt := _parse_utc_dt(t.get("closed_at"))) is not None
                else "UNKNOWN"
            ),
        ),
    }


def compare_report_periods(
    primary_trades: Sequence[Mapping[str, Any]],
    baseline_trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """So sánh hiệu năng hai khoảng thời gian (kỳ chính vs kỳ đối chiếu)."""
    primary = reconcile_filtered_totals(primary_trades)
    baseline = reconcile_filtered_totals(baseline_trades)
    with exact_decimal():
        net_diff = _dec(primary["net_pnl"]) - _dec(baseline["net_pnl"])
        win_rate_diff: str | None = None
        if primary["win_rate"] is not None and baseline["win_rate"] is not None:
            win_rate_diff = canonical_decimal(
                (_dec(primary["win_rate"]) - _dec(baseline["win_rate"])).quantize(
                    _RATIO_QUANT, rounding=ROUND_HALF_UP
                )
            )
    return {
        "primary": primary,
        "baseline": baseline,
        "delta": {
            "trade_count_diff": int(primary["trade_count"]) - int(baseline["trade_count"]),
            "net_pnl_diff": canonical_decimal(net_diff),
            "win_rate_diff": win_rate_diff,
        },
    }


def filtered_report_hash(
    *,
    source_report_hash: str | None,
    criteria: ReportFilterCriteria,
    filtered_totals: Mapping[str, Any],
    trade_ids: Sequence[str],
) -> str:
    """Tính SHA-256 bất biến cho bản báo cáo đã lọc gắn với lineage báo cáo gốc."""
    return report_hash(
        {
            "source_report_hash": source_report_hash,
            "filters": criteria.as_dict(),
            "totals": dict(filtered_totals),
            "trade_ids": list(trade_ids),
        }
    )


__all__ = [
    "ReportFilterCriteria",
    "breakdown_trades",
    "compare_report_periods",
    "filter_report_trades",
    "filtered_report_hash",
    "reconcile_filtered_totals",
]
