"""Tính báo cáo theo bot và tổng hợp workspace từ ledger PAPER append-only (MB-06).

Module thuần: không I/O, không đồng hồ. Mọi số tiền lấy nguyên văn từ cột ledger rồi chỉ
cộng ``Decimal``; không tính lại PnL. Hàng sai scope/cửa sổ hoặc lệch đối soát làm báo cáo
dừng với ``ReportIntegrityError`` thay vì âm thầm loại bỏ (fail-closed).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from typing import Any
from uuid import UUID

REPORT_VERSION = "bot-report.v1"
REPORT_MODE = "PAPER"
MAX_REPORT_WINDOW = timedelta(days=366)
MAX_TRADES_PER_BOT = 10_000
MAX_OPEN_TRADES_PER_BOT = 1_000
MAX_BOTS_PER_WORKSPACE_REPORT = 200
# Numeric(38,18) có tối đa 38 chữ số có nghĩa; 76 đủ cho mọi tổng/hiệu báo cáo hợp lý.
DECIMAL_PRECISION = 76
WINDOW_BOUNDARY = "[start,end) theo trade.closed_at"
# Khóa evidence của order_intent mang lineage cấu hình MB-02 (effective config).
INTENT_LINEAGE_KEYS = (
    "settings_hash",
    "bot_config_version_id",
    "bot_config_hash",
    "effective_config_hash",
)
_COST_TYPES = ("FEE", "FUNDING", "SLIPPAGE")


class ReportIntegrityError(ValueError):
    """Ledger trả hàng sai scope/cửa sổ hoặc số liệu không đối soát được; không trả báo cáo."""


@dataclass(frozen=True, slots=True)
class ReportBot:
    """Định danh bot cùng account độc quyền của nó trong một workspace."""

    workspace_id: UUID
    bot_id: UUID
    account_id: UUID


@dataclass(frozen=True, slots=True)
class LedgerTrade:
    """Ảnh chỉ đọc của một hàng ``trade`` (số liệu nguyên văn từ ledger)."""

    trade_id: UUID
    intent_id: UUID
    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    status: str
    symbol: str
    side: str
    quote_asset: str
    opened_at: datetime
    closed_at: datetime | None
    gross_pnl: Decimal
    fee_total: Decimal
    funding_total: Decimal
    slippage_total: Decimal
    total_cost: Decimal
    net_pnl: Decimal
    settings_version_id: UUID
    strategy_release_id: UUID
    strategy_release_hash: str
    feature_snapshot_id: str
    candidate_id: str
    risk_decision_id: UUID
    risk_reservation_id: UUID
    lineage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LedgerCost:
    """Ảnh chỉ đọc của một hàng ``trade_cost``."""

    trade_id: UUID
    workspace_id: UUID
    bot_id: UUID
    cost_type: str
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class IntentLineage:
    """Evidence của ``order_intent`` (lineage effective config MB-02)."""

    intent_id: UUID
    bot_id: UUID
    evidence: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DecisionLineage:
    """Quyết định MB-03 cùng bot và candidate: config, root settings và model release."""

    decision_id: UUID
    bot_id: UUID
    candidate_id: str
    decision_hash: str
    bot_config_version_id: UUID
    bot_config_hash: str
    root_settings_version_id: UUID
    model_release_id: UUID | None


@dataclass(frozen=True, slots=True)
class BotLedgerSnapshot:
    """Mọi hàng ledger repository đọc cho một bot trong một cửa sổ."""

    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    closed_trades: tuple[LedgerTrade, ...]
    open_trades: tuple[LedgerTrade, ...]
    costs: tuple[LedgerCost, ...]
    intents: tuple[IntentLineage, ...]
    decisions: tuple[DecisionLineage, ...]


@dataclass(frozen=True, slots=True)
class ReportTotals:
    """Tổng theo một quote asset; drawdown trên PnL ròng đã thực hiện."""

    quote_asset: str
    closed_trade_count: int
    winning_trade_count: int
    losing_trade_count: int
    breakeven_trade_count: int
    gross_pnl: Decimal
    fee_total: Decimal
    funding_total: Decimal
    slippage_total: Decimal
    total_cost: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    drawdown_peak_trade_id: UUID | None
    drawdown_trough_trade_id: UUID | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "quote_asset": self.quote_asset,
            "closed_trade_count": self.closed_trade_count,
            "winning_trade_count": self.winning_trade_count,
            "losing_trade_count": self.losing_trade_count,
            "breakeven_trade_count": self.breakeven_trade_count,
            "gross_pnl": canonical_decimal(self.gross_pnl),
            "fee_total": canonical_decimal(self.fee_total),
            "funding_total": canonical_decimal(self.funding_total),
            "slippage_total": canonical_decimal(self.slippage_total),
            "total_cost": canonical_decimal(self.total_cost),
            "net_pnl": canonical_decimal(self.net_pnl),
            "max_drawdown": canonical_decimal(self.max_drawdown),
            "drawdown_peak_trade_id": _optional_id(self.drawdown_peak_trade_id),
            "drawdown_trough_trade_id": _optional_id(self.drawdown_trough_trade_id),
        }


@dataclass(frozen=True, slots=True)
class BotReport:
    """Báo cáo một bot: payload JSON canonical kèm hash SHA-256 tất định."""

    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    start: datetime
    end: datetime
    closed_trades: tuple[LedgerTrade, ...]
    totals: Mapping[str, ReportTotals]
    report_hash: str
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return json.loads(_canonical_json(self.payload))  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class WorkspaceAggregateReport:
    """Tổng hợp tường minh mọi bot của workspace; đối soát với tổng ledger độc lập."""

    workspace_id: UUID
    start: datetime
    end: datetime
    bot_reports: tuple[BotReport, ...]
    totals: Mapping[str, ReportTotals]
    report_hash: str
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return json.loads(_canonical_json(self.payload))  # type: ignore[no-any-return]


# --- Canonical ---------------------------------------------------------------------------


def canonical_decimal(value: Decimal) -> str:
    """Biểu diễn cố định không số 0 thừa: ``1.500000000000000000`` → ``1.5``; 0 → ``0``."""
    if not value.is_finite():
        raise ReportIntegrityError("Ledger chứa Decimal không hữu hạn.")
    if value == 0:
        return "0"
    with exact_decimal():  # normalize() làm tròn theo context: không được dùng prec 28
        return format(value.normalize(), "f")


def canonical_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReportIntegrityError("Mốc thời gian ledger thiếu timezone.")
    return value.astimezone(UTC).isoformat()


def report_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 trên JSON UTF-8 sắp xếp khóa, không khoảng trắng; payload không chứa hash."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _optional_id(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# --- Báo cáo bot -------------------------------------------------------------------------


@contextmanager
def exact_decimal() -> Iterator[None]:
    """Context Decimal rộng (prec 76 ≥ 2 x Numeric(38,18)) và bẫy ``Inexact``.

    Context mặc định (prec 28) làm tròn âm thầm số 38 chữ số của ledger; mọi phép cộng/so
    sánh của báo cáo phải chính xác tuyệt đối, nếu không thì dừng (fail-closed).
    """
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.traps[Inexact] = True
        try:
            yield
        except Inexact as error:
            raise ReportIntegrityError(
                "Phép tính Decimal của báo cáo không chính xác (vượt độ chính xác)."
            ) from error


def build_bot_report(
    *,
    snapshot: BotLedgerSnapshot,
    start: datetime,
    end: datetime,
    mode: str = REPORT_MODE,
    report_version: str = REPORT_VERSION,
) -> BotReport:
    """Dựng báo cáo một bot; kiểm lại scope/cửa sổ từng hàng và đối soát cost với trade."""
    with exact_decimal():
        return _build_bot_report(
            snapshot=snapshot, start=start, end=end, mode=mode, report_version=report_version
        )


def _build_bot_report(
    *,
    snapshot: BotLedgerSnapshot,
    start: datetime,
    end: datetime,
    mode: str,
    report_version: str,
) -> BotReport:
    window = {"start": canonical_time(start), "end": canonical_time(end)}
    closed = sorted(snapshot.closed_trades, key=_trade_order)
    for trade in closed:
        _require_scope(trade, snapshot)
        _require_closed_in_window(trade, start, end)
        _require_trade_arithmetic(trade)
    open_rows = sorted(snapshot.open_trades, key=lambda t: (t.opened_at, str(t.trade_id)))
    for trade in open_rows:
        _require_scope(trade, snapshot)
        if not trade.opened_at < end or (trade.closed_at is not None and trade.closed_at < end):
            raise ReportIntegrityError("Trade mở tại end nằm ngoài định nghĩa cửa sổ.")
    cost_count = _reconcile_costs(closed, snapshot)
    intents = _intent_evidence(snapshot)
    decisions = _decisions_by_candidate(snapshot)

    trade_rows = [_trade_row(trade, intents, decisions) for trade in closed]
    totals = _totals(closed)
    payload: dict[str, Any] = {
        "report_version": report_version,
        "scope": "bot",
        "mode": mode,
        "workspace_id": str(snapshot.workspace_id),
        "bot_id": str(snapshot.bot_id),
        "account_id": str(snapshot.account_id),
        "window": window | {"boundary": WINDOW_BOUNDARY},
        "totals": {asset: item.as_dict() for asset, item in sorted(totals.items())},
        "trades": trade_rows,
        "open_trades": [
            {
                "trade_id": str(trade.trade_id),
                "symbol": trade.symbol,
                "side": trade.side,
                "quote_asset": trade.quote_asset,
                "opened_at": canonical_time(trade.opened_at),
            }
            for trade in open_rows
        ],
        "lineage": _lineage_summary(trade_rows),
        "reconciliation": {"closed_trade_rows": len(closed), "trade_cost_rows": cost_count},
    }
    digest = report_hash(payload)
    return BotReport(
        workspace_id=snapshot.workspace_id,
        bot_id=snapshot.bot_id,
        account_id=snapshot.account_id,
        start=start,
        end=end,
        closed_trades=tuple(closed),
        totals=totals,
        report_hash=digest,
        payload=payload | {"report_hash": digest},
    )


def _trade_order(trade: LedgerTrade) -> tuple[datetime, str]:
    if trade.closed_at is None:
        raise ReportIntegrityError("Trade đóng thiếu closed_at.")
    return trade.closed_at, str(trade.trade_id)


def _require_scope(trade: LedgerTrade, snapshot: BotLedgerSnapshot) -> None:
    if (trade.workspace_id, trade.bot_id, trade.account_id) != (
        snapshot.workspace_id,
        snapshot.bot_id,
        snapshot.account_id,
    ):
        raise ReportIntegrityError("Ledger trả trade ngoài scope workspace/bot/account.")


def _require_closed_in_window(trade: LedgerTrade, start: datetime, end: datetime) -> None:
    if trade.status != "CLOSED" or trade.closed_at is None:
        raise ReportIntegrityError("Chỉ trade CLOSED được tính PnL thực hiện.")
    if not start <= trade.closed_at < end:
        raise ReportIntegrityError("Trade đóng ngoài cửa sổ [start, end).")


def _require_trade_arithmetic(trade: LedgerTrade) -> None:
    if trade.total_cost != trade.fee_total + trade.funding_total + trade.slippage_total:
        raise ReportIntegrityError("trade.total_cost khác tổng fee + funding + slippage.")
    if trade.net_pnl != trade.gross_pnl - trade.total_cost:
        raise ReportIntegrityError("trade.net_pnl khác gross_pnl - total_cost.")


def _reconcile_costs(closed: Sequence[LedgerTrade], snapshot: BotLedgerSnapshot) -> int:
    """Tổng ``trade_cost`` theo loại phải bằng đúng cột tổng của trade (Decimal chính xác)."""
    by_trade = {trade.trade_id: trade for trade in closed}
    sums = {trade_id: dict.fromkeys(_COST_TYPES, Decimal(0)) for trade_id in by_trade}
    for cost in snapshot.costs:
        if (cost.workspace_id, cost.bot_id) != (snapshot.workspace_id, snapshot.bot_id):
            raise ReportIntegrityError("Ledger trả trade_cost ngoài scope bot.")
        trade = by_trade.get(cost.trade_id)
        if trade is None:
            raise ReportIntegrityError("trade_cost không thuộc trade đóng trong cửa sổ.")
        if cost.cost_type not in _COST_TYPES or cost.currency != trade.quote_asset:
            raise ReportIntegrityError("trade_cost sai loại hoặc khác quote asset của trade.")
        sums[cost.trade_id][cost.cost_type] += cost.amount
    for trade_id, trade in by_trade.items():
        expected = (trade.fee_total, trade.funding_total, trade.slippage_total)
        if tuple(sums[trade_id][kind] for kind in _COST_TYPES) != expected:
            raise ReportIntegrityError(f"Tổng trade_cost lệch cột tổng của trade {trade_id}.")
    return len(snapshot.costs)


def _intent_evidence(snapshot: BotLedgerSnapshot) -> dict[UUID, Mapping[str, Any]]:
    evidence: dict[UUID, Mapping[str, Any]] = {}
    for intent in snapshot.intents:
        if intent.bot_id != snapshot.bot_id:
            raise ReportIntegrityError("Ledger trả order_intent của bot khác.")
        evidence[intent.intent_id] = intent.evidence
    return evidence


def _decisions_by_candidate(snapshot: BotLedgerSnapshot) -> dict[str, list[DecisionLineage]]:
    grouped: dict[str, list[DecisionLineage]] = {}
    for decision in snapshot.decisions:
        if decision.bot_id != snapshot.bot_id:
            raise ReportIntegrityError("Ledger trả bot_decision của bot khác.")
        grouped.setdefault(decision.candidate_id, []).append(decision)
    return grouped


def _trade_row(
    trade: LedgerTrade,
    intents: Mapping[UUID, Mapping[str, Any]],
    decisions: Mapping[str, Sequence[DecisionLineage]],
) -> dict[str, Any]:
    evidence = intents.get(trade.intent_id, {})
    matched = sorted(decisions.get(trade.candidate_id, ()), key=lambda d: str(d.decision_id))
    lineage: dict[str, Any] = {
        "settings_version_id": str(trade.settings_version_id),
        "strategy_release_id": str(trade.strategy_release_id),
        "strategy_release_hash": trade.strategy_release_hash,
        "feature_snapshot_id": trade.feature_snapshot_id,
        "candidate_id": trade.candidate_id,
        "risk_decision_id": str(trade.risk_decision_id),
        "risk_reservation_id": str(trade.risk_reservation_id),
        "plan_hash": _optional_text(trade.lineage.get("plan_hash")),
        "paper_execution_profile_hash": _optional_text(
            trade.lineage.get("paper_execution_profile_hash")
        ),
        "bot_decision_ids": [str(d.decision_id) for d in matched],
        "model_release_ids": sorted(
            {str(d.model_release_id) for d in matched if d.model_release_id is not None}
        ),
    }
    for key in INTENT_LINEAGE_KEYS:
        lineage[key] = _optional_text(evidence.get(key))
    assert trade.closed_at is not None
    return {
        "trade_id": str(trade.trade_id),
        "intent_id": str(trade.intent_id),
        "bot_id": str(trade.bot_id),
        "account_id": str(trade.account_id),
        "symbol": trade.symbol,
        "side": trade.side,
        "quote_asset": trade.quote_asset,
        "opened_at": canonical_time(trade.opened_at),
        "closed_at": canonical_time(trade.closed_at),
        "gross_pnl": canonical_decimal(trade.gross_pnl),
        "fee_total": canonical_decimal(trade.fee_total),
        "funding_total": canonical_decimal(trade.funding_total),
        "slippage_total": canonical_decimal(trade.slippage_total),
        "total_cost": canonical_decimal(trade.total_cost),
        "net_pnl": canonical_decimal(trade.net_pnl),
        "lineage": lineage,
    }


def _lineage_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    lineages = [row["lineage"] for row in rows]

    def distinct(key: str) -> list[str]:
        return sorted({item[key] for item in lineages if item.get(key)})

    def pairs(id_key: str, hash_key: str) -> list[dict[str, str | None]]:
        found = {(item[id_key], item.get(hash_key)) for item in lineages if item.get(id_key)}
        return [{"id": i, "hash": h} for i, h in sorted(found, key=lambda p: (p[0], p[1] or ""))]

    return {
        "settings_version_ids": distinct("settings_version_id"),
        "strategy_releases": pairs("strategy_release_id", "strategy_release_hash"),
        "bot_config_versions": pairs("bot_config_version_id", "bot_config_hash"),
        "effective_config_hashes": distinct("effective_config_hash"),
        "paper_execution_profile_hashes": distinct("paper_execution_profile_hash"),
        "model_release_ids": sorted(
            {model for item in lineages for model in item["model_release_ids"]}
        ),
        "trades_without_bot_config_lineage": sum(
            1 for item in lineages if not item.get("bot_config_version_id")
        ),
    }


def _totals(trades: Iterable[LedgerTrade]) -> dict[str, ReportTotals]:
    """Gom theo quote asset; ``trades`` phải đã sắp theo ``(closed_at, trade_id)``."""
    grouped: dict[str, list[LedgerTrade]] = {}
    for trade in trades:
        grouped.setdefault(trade.quote_asset, []).append(trade)
    totals: dict[str, ReportTotals] = {}
    for asset, rows in grouped.items():
        drawdown, peak_id, trough_id = realized_drawdown(rows)
        totals[asset] = ReportTotals(
            quote_asset=asset,
            closed_trade_count=len(rows),
            winning_trade_count=sum(1 for t in rows if t.net_pnl > 0),
            losing_trade_count=sum(1 for t in rows if t.net_pnl < 0),
            breakeven_trade_count=sum(1 for t in rows if t.net_pnl == 0),
            gross_pnl=sum((t.gross_pnl for t in rows), Decimal(0)),
            fee_total=sum((t.fee_total for t in rows), Decimal(0)),
            funding_total=sum((t.funding_total for t in rows), Decimal(0)),
            slippage_total=sum((t.slippage_total for t in rows), Decimal(0)),
            total_cost=sum((t.total_cost for t in rows), Decimal(0)),
            net_pnl=sum((t.net_pnl for t in rows), Decimal(0)),
            max_drawdown=drawdown,
            drawdown_peak_trade_id=peak_id,
            drawdown_trough_trade_id=trough_id,
        )
    return totals


def realized_drawdown(trades: Sequence[LedgerTrade]) -> tuple[Decimal, UUID | None, UUID | None]:
    """Sụt giảm lớn nhất của PnL ròng tích lũy (bắt đầu 0); trả trade đỉnh và trade đáy.

    Đỉnh ``None`` nghĩa là mốc 0 đầu cửa sổ. Trade cần được sắp theo thời điểm đóng.
    """
    cumulative = peak = worst = Decimal(0)
    peak_id: UUID | None = None
    worst_peak: UUID | None = None
    worst_trough: UUID | None = None
    for trade in trades:
        cumulative += trade.net_pnl
        if cumulative > peak:
            peak, peak_id = cumulative, trade.trade_id
        if peak - cumulative > worst:
            worst, worst_peak, worst_trough = peak - cumulative, peak_id, trade.trade_id
    return worst, worst_peak, worst_trough


# --- Tổng hợp workspace ------------------------------------------------------------------


def build_workspace_report(
    *,
    workspace_id: UUID,
    start: datetime,
    end: datetime,
    bot_reports: Sequence[BotReport],
    ledger: Mapping[str, tuple[int, Decimal]],
    mode: str = REPORT_MODE,
    report_version: str = REPORT_VERSION,
) -> WorkspaceAggregateReport:
    """Cộng các báo cáo bot; số trade/net PnL phải khớp tổng ledger độc lập theo quote asset."""
    with exact_decimal():
        return _build_workspace_report(
            workspace_id=workspace_id,
            start=start,
            end=end,
            bot_reports=bot_reports,
            ledger=ledger,
            mode=mode,
            report_version=report_version,
        )


def _build_workspace_report(
    *,
    workspace_id: UUID,
    start: datetime,
    end: datetime,
    bot_reports: Sequence[BotReport],
    ledger: Mapping[str, tuple[int, Decimal]],
    mode: str,
    report_version: str,
) -> WorkspaceAggregateReport:
    reports = sorted(bot_reports, key=lambda r: str(r.bot_id))
    if len({r.bot_id for r in reports}) != len(reports):
        raise ReportIntegrityError("Một bot xuất hiện nhiều lần trong tổng hợp.")
    for report in reports:
        if report.workspace_id != workspace_id or (report.start, report.end) != (start, end):
            raise ReportIntegrityError("Báo cáo bot khác workspace hoặc khác cửa sổ.")
    merged = sorted((t for r in reports for t in r.closed_trades), key=_trade_order)
    totals = _totals(merged)
    observed = {asset: (item.closed_trade_count, item.net_pnl) for asset, item in totals.items()}
    expected = {asset: (count, net) for asset, (count, net) in ledger.items() if count}
    if observed != expected:
        raise ReportIntegrityError("Tổng hợp bot lệch tổng ledger workspace (count/net PnL).")
    payload: dict[str, Any] = {
        "report_version": report_version,
        "scope": "workspace",
        "mode": mode,
        "workspace_id": str(workspace_id),
        "window": {
            "start": canonical_time(start),
            "end": canonical_time(end),
            "boundary": WINDOW_BOUNDARY,
        },
        "bots": [
            {
                "bot_id": str(r.bot_id),
                "account_id": str(r.account_id),
                "report_hash": r.report_hash,
                "totals": {asset: item.as_dict() for asset, item in sorted(r.totals.items())},
            }
            for r in reports
        ],
        "totals": {asset: item.as_dict() for asset, item in sorted(totals.items())},
        "reconciliation": {
            "bot_count": len(reports),
            "closed_trade_rows": len(merged),
            "ledger": {
                asset: {"closed_trade_rows": count, "net_pnl": canonical_decimal(net)}
                for asset, (count, net) in sorted(expected.items())
            },
        },
    }
    digest = report_hash(payload)
    return WorkspaceAggregateReport(
        workspace_id=workspace_id,
        start=start,
        end=end,
        bot_reports=tuple(reports),
        totals=totals,
        report_hash=digest,
        payload=payload | {"report_hash": digest},
    )
