"""Run Report tất định (WBS AI-1, 16/11/2026): đầu vào duy nhất cho AI Analyst (AI-2).

Báo cáo gộp các bot của một đợt chạy trong cửa sổ ``[start, end)``; mọi con số tính từ ledger
PAPER (qua báo cáo bot ``bot-report.v2`` đã đối soát cost), quyết định đã lưu và nến đã đóng —
**không có LLM** ở bước này. AI-2 chỉ được trích số từ báo cáo này (``evidence_refs`` là đường dẫn
JSON tới trường), nên báo cáo phải đủ để diễn giải mà không cần truy vấn thêm.

Nội dung theo bot:
- ``config``: release, họ chiến lược, khung, tham số, rủi ro, số coin theo dõi;
- ``funnel``: số quyết định theo trạng thái, lý do bỏ qua phổ biến, tín hiệu → lệnh;
- ``performance``: tổng từ báo cáo bot + thống kê R-multiple, MAE/MFE (đơn vị R);
- ``by_symbol``: lệnh/thắng/PnL theo coin;
- ``signal_outcomes`` (CRY-430, v2): kết quả các tín hiệu phát trong cửa sổ, chấm bằng nến đã đóng
  sau 1×/4×/24× khung (đúng/sai/hòa theo dải ±0,1R, TP1/SL chạm trước, MFE/MAE theo R), theo coin,
  đã gửi Telegram hay không, và nhóm sai lặp lại — kể cả tín hiệu không thành lệnh;
- ``data_quality``: cờ mẫu nhỏ để AI không kết luận quá mức.

R-multiple = PnL ròng / rủi ro đã giữ chỗ (``risk_reservation.reserved_risk`` ≈ lỗ dự kiến tại
SL). MAE/MFE (Maximum Adverse/Favorable Excursion): biên bất lợi/thuận lợi lớn nhất của giá trong
lúc giữ lệnh, tính từ đáy/đỉnh nến đã đóng của khung chiến lược, quy về R.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any

from trading_bot.domain.strategy.signal_outcome import (
    HORIZON_MULTIPLES,
    LABEL_VERSION,
    METHOD_VERSION,
)
from trading_bot.domain.strategy.signal_outcome_summary import (
    OutcomeRow,
    group_summaries,
    repeated_wrong,
    summarize,
)

RUN_REPORT_VERSION = "ai1-run-report-v2"
MIN_TRADES_FOR_CONCLUSION = 20
MIN_SIGNALS_FOR_CONCLUSION = 20
SIGNAL_TOP_SYMBOLS = 10
TOP_REASONS = 8
OUT_OF_UNIVERSE = "SYMBOL_OUT_OF_BOT_UNIVERSE"
_Q = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class DecisionCount:
    status: str
    reason_code: str
    count: int


@dataclass(frozen=True, slots=True)
class TradeExtremes:
    """Rủi ro giữ chỗ và đáy/đỉnh giá trong lúc giữ lệnh (None nếu không có dữ liệu)."""

    trade_id: str
    risk_amount: Decimal | None
    lowest_price: Decimal | None
    highest_price: Decimal | None


@dataclass(frozen=True, slots=True)
class SignalOutcomeInput:
    """Một hàng ``signal_outcome`` của bot (CRY-430) kèm trạng thái gửi Telegram."""

    horizon_bars: int
    symbol: str
    direction: str
    delivered: bool
    row: OutcomeRow


@dataclass(frozen=True, slots=True)
class BotRunInput:
    bot_id: str
    bot_name: str
    release_id: str
    timeframe: str | None
    parameters: Mapping[str, Any]
    risk: Mapping[str, Any]
    universe: tuple[str, ...]
    bot_report: Mapping[str, Any]
    decisions: tuple[DecisionCount, ...] = ()
    candidates_with_intent: int = 0
    extremes: tuple[TradeExtremes, ...] = ()
    # (giai đoạn, mã) → số candidate bị từ chối; từ paper_candidate_outcome (16/11/2026).
    rejections: tuple[DecisionCount, ...] = ()
    candidates_with_recorded_rejection: int = 0
    signal_outcomes: tuple[SignalOutcomeInput, ...] = ()


@dataclass(frozen=True, slots=True)
class RunReport:
    payload: Mapping[str, Any]
    report_hash: str
    warnings: tuple[str, ...] = field(default=())


def strategy_family(release_id: str) -> str:
    """Họ chiến lược để so nhóm: H01/H03/H06/CARD."""
    lowered = release_id.lower()
    if lowered.startswith("sc-"):
        return "CARD"
    return lowered.split("-", 1)[0].upper()


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(_Q).normalize(), "f")


def _ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return _text(Decimal(numerator) / Decimal(denominator))


def _fills(trade: Mapping[str, Any], role: str) -> tuple[Decimal, Decimal] | None:
    """Giá trung bình theo khối lượng và tổng khối lượng của fill vai trò ``role``."""
    quantity = Decimal(0)
    notional = Decimal(0)
    for fill in trade.get("fills") or ():
        if str(fill.get("role", "")).upper() != role:
            continue
        qty = _dec(fill["quantity"])
        quantity += qty
        notional += qty * _dec(fill["price"])
    if quantity <= 0:
        return None
    return notional / quantity, quantity


def _excursions(
    trade: Mapping[str, Any], extremes: TradeExtremes | None
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """(R, MAE_R, MFE_R) của một trade; None khi thiếu rủi ro hoặc giá."""
    if extremes is None or extremes.risk_amount is None or extremes.risk_amount <= 0:
        return None, None, None
    risk = extremes.risk_amount
    r_value = _dec(trade["net_pnl"]) / risk
    entry = _fills(trade, "ENTRY")
    if entry is None or extremes.lowest_price is None or extremes.highest_price is None:
        return r_value, None, None
    price, quantity = entry
    long = str(trade.get("side", "")).lower() in {"long", "buy"}
    adverse = (price - extremes.lowest_price) if long else (extremes.highest_price - price)
    favorable = (extremes.highest_price - price) if long else (price - extremes.lowest_price)
    return (
        r_value,
        max(Decimal(0), adverse) * quantity / risk,
        max(Decimal(0), favorable) * quantity / risk,
    )


def _distribution(values: Sequence[Decimal]) -> dict[str, str | None]:
    if not values:
        return {"mean": None, "median": None, "min": None, "max": None}
    return {
        "mean": _text(sum(values, Decimal(0)) / Decimal(len(values))),
        "median": _text(Decimal(median(values))),
        "min": _text(min(values)),
        "max": _text(max(values)),
    }


_COMPACT = (
    "signals", "scored", "correct", "wrong", "neutral", "hit_rate_percent", "outcome_r_mean",
    "tp1_first", "sl_first",
)  # fmt: skip
PRIMARY_HORIZON_BARS = 4


def _compact(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in ("key", *_COMPACT) if key in item}


def signal_outcome_section(items: Sequence[SignalOutcomeInput]) -> dict[str, Any]:
    """Khối ``signal_outcomes`` của một bot: horizon chính 4× khung có phân rã chi tiết."""
    by_horizon = {
        str(horizon): summarize(item.row for item in items if item.horizon_bars == horizon)
        for horizon in HORIZON_MULTIPLES
    }
    for horizon, summary in by_horizon.items():
        if horizon != str(PRIMARY_HORIZON_BARS):
            summary.pop("r_distribution", None)
    primary = [item for item in items if item.horizon_bars == PRIMARY_HORIZON_BARS]
    return {
        "method_version": METHOD_VERSION,
        "label_version": LABEL_VERSION,
        "primary_horizon_bars": PRIMARY_HORIZON_BARS,
        "by_horizon_bars": by_horizon,
        "by_symbol": [
            _compact(item)
            for item in group_summaries(
                ((item.symbol, item.row) for item in primary), limit=SIGNAL_TOP_SYMBOLS
            )
        ],
        "by_delivery": [
            _compact(item)
            for item in group_summaries(
                ("sent" if item.delivered else "not_sent", item.row) for item in primary
            )
        ],
        "repeated_wrong": [
            _compact(item)
            for item in repeated_wrong(
                ((f"{item.symbol} {item.direction}", item.row) for item in primary),
                min_wrong=2,
                limit=5,
            )
        ],
        "source": {"table": "signal_outcome", "window_field": "signal_at"},
    }


def _bot_section(bot: BotRunInput) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    report = bot.bot_report
    trades = list(report.get("trades") or ())
    totals = dict((report.get("totals") or {}).get("USDT") or {})
    by_extremes = {item.trade_id: item for item in bot.extremes}
    r_values: list[Decimal] = []
    mae: list[Decimal] = []
    mfe: list[Decimal] = []
    exit_reasons: Counter[str] = Counter()
    per_symbol: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"closed": 0, "wins": 0, "net_pnl": Decimal(0)}
    )
    for trade in trades:
        r_value, mae_r, mfe_r = _excursions(trade, by_extremes.get(str(trade["trade_id"])))
        if r_value is not None:
            r_values.append(r_value)
        if mae_r is not None and mfe_r is not None:
            mae.append(mae_r)
            mfe.append(mfe_r)
        exit_reasons[str(trade.get("exit_reason") or "UNKNOWN")] += 1
        row = per_symbol[str(trade["symbol"])]
        row["closed"] += 1
        row["wins"] += 1 if _dec(trade["net_pnl"]) > 0 else 0
        row["net_pnl"] += _dec(trade["net_pnl"])
    status_counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    out_of_universe = 0
    for item in bot.decisions:
        if item.reason_code == OUT_OF_UNIVERSE:
            # Producer đánh giá mọi coin của root cho mọi bot; coin ngoài danh sách của bot là
            # nhiễu, tách riêng để không lấn át lý do bỏ qua thật.
            out_of_universe += item.count
            continue
        status_counts[item.status] += item.count
        if item.status != "CANDIDATE":
            reasons[item.reason_code] += item.count
    candidates = status_counts.get("CANDIDATE", 0)
    closed = len(trades)
    if closed < MIN_TRADES_FOR_CONCLUSION:
        warnings.append(
            f"{bot.bot_name}: chỉ {closed} lệnh đóng (< {MIN_TRADES_FOR_CONCLUSION}), "
            "chưa đủ để kết luận thống kê."
        )
    signal_block = signal_outcome_section(bot.signal_outcomes)
    primary_scored = signal_block["by_horizon_bars"][str(PRIMARY_HORIZON_BARS)]["scored"]
    if primary_scored < MIN_SIGNALS_FOR_CONCLUSION:
        warnings.append(
            f"{bot.bot_name}: chỉ {primary_scored} tín hiệu đã chấm ở horizon chính "
            f"(< {MIN_SIGNALS_FOR_CONCLUSION}), chưa đủ để kết luận về chất lượng tín hiệu."
        )
    unexplained = candidates - bot.candidates_with_intent - bot.candidates_with_recorded_rejection
    if unexplained > 0:
        warnings.append(
            f"{bot.bot_name}: {unexplained} tín hiệu chưa thành lệnh mà chưa có lý do được lưu "
            "(chưa tới lượt runtime xử lý hoặc tín hiệu trước khi bắt đầu lưu lý do)."
        )
    section = {
        "bot_id": bot.bot_id,
        "bot_name": bot.bot_name,
        "config": {
            "release_id": bot.release_id,
            "strategy_family": strategy_family(bot.release_id),
            "timeframe": bot.timeframe,
            "parameters": dict(sorted(bot.parameters.items())),
            "risk": dict(sorted(bot.risk.items())),
            "universe_size": len(bot.universe),
            "universe": list(bot.universe),
        },
        "funnel": {
            "decisions_total": sum(status_counts.values()),
            "out_of_universe_evaluations": out_of_universe,
            "by_status": dict(sorted(status_counts.items())),
            "top_skip_reasons": dict(reasons.most_common(TOP_REASONS)),
            "candidates": candidates,
            "candidates_with_intent": bot.candidates_with_intent,
            "candidate_to_order_rate": _ratio(bot.candidates_with_intent, candidates),
            "candidates_with_recorded_rejection": bot.candidates_with_recorded_rejection,
            "rejection_reasons": {
                f"{item.status}:{item.reason_code}": item.count
                for item in sorted(
                    bot.rejections, key=lambda r: (-r.count, r.status, r.reason_code)
                )
            },
        },
        "performance": {
            "closed_trades": closed,
            "open_trades": len(report.get("open_trades") or ()),
            "net_pnl_usdt": totals.get("net_pnl"),
            "win_rate_percent": totals.get("win_rate_percent"),
            "profit_factor": totals.get("profit_factor"),
            "expectancy_per_trade_usdt": totals.get("expectancy_per_trade"),
            "max_drawdown_usdt": totals.get("max_drawdown"),
            "total_cost_usdt": totals.get("total_cost"),
            "average_holding_seconds": totals.get("average_holding_seconds"),
            "r_multiple": _distribution(r_values),
            "mae_r": _distribution(mae),
            "mfe_r": _distribution(mfe),
            "exit_reasons": dict(sorted(exit_reasons.items())),
        },
        "by_symbol": {
            symbol: {
                "closed": row["closed"],
                "wins": row["wins"],
                "net_pnl_usdt": _text(row["net_pnl"]),
            }
            for symbol, row in sorted(per_symbol.items())
        },
        "signal_outcomes": signal_block,
        "source": {"bot_report_hash": report.get("report_hash")},
    }
    return section, warnings


def group_sections(sections: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    """Gộp các mục bot theo ``config[key]`` (họ chiến lược hoặc khung)."""
    return _groups(sections, key)


def _groups(sections: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for section in sections:
        name = str(section["config"].get(key) or "UNKNOWN")
        row = grouped.setdefault(
            name, {"bots": 0, "closed_trades": 0, "candidates": 0, "net_pnl_usdt": Decimal(0)}
        )
        row["bots"] += 1
        row["closed_trades"] += section["performance"]["closed_trades"]
        row["candidates"] += section["funnel"]["candidates"]
        row["net_pnl_usdt"] += _dec(section["performance"]["net_pnl_usdt"] or 0)
    return {
        name: row | {"net_pnl_usdt": _text(row["net_pnl_usdt"])}
        for name, row in sorted(grouped.items())
    }


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_run_report(
    *,
    workspace_id: str,
    run_label: str,
    window_start: datetime,
    window_end: datetime,
    bots: Sequence[BotRunInput],
) -> RunReport:
    """Dựng Run Report tất định; cùng đầu vào → cùng JSON và hash."""
    if window_start >= window_end:
        raise ValueError("Cửa sổ báo cáo không hợp lệ.")
    sections: list[dict[str, Any]] = []
    warnings: list[str] = []
    for bot in sorted(bots, key=lambda item: item.bot_name):
        section, notes = _bot_section(bot)
        sections.append(section)
        warnings.extend(notes)
    ranking = sorted(
        (
            {
                "bot_name": section["bot_name"],
                # Null (chưa có lệnh đóng) giữ null, không đổi thành "0" (AI phát hiện lệch 16/11).
                "net_pnl_usdt": section["performance"]["net_pnl_usdt"],
                "closed_trades": section["performance"]["closed_trades"],
            }
            for section in sections
        ),
        key=lambda row: (-_dec(row["net_pnl_usdt"] or 0), row["bot_name"]),
    )
    payload = {
        "report_version": RUN_REPORT_VERSION,
        "workspace_id": workspace_id,
        "run_label": run_label,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "bots": sections,
        "groups": {
            "by_strategy_family": _groups(sections, "strategy_family"),
            "by_timeframe": _groups(sections, "timeframe"),
        },
        "ranking_by_net_pnl": ranking,
        "data_quality": {
            "min_trades_for_conclusion": MIN_TRADES_FOR_CONCLUSION,
            "min_signals_for_conclusion": MIN_SIGNALS_FOR_CONCLUSION,
            "warnings": warnings,
        },
    }
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return RunReport(
        payload=payload | {"report_hash": digest}, report_hash=digest, warnings=tuple(warnings)
    )
