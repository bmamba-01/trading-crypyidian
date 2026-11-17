"""AI-3: backtest đề xuất của AI Analyst: bản gốc (baseline) và bản đề xuất (variant).

Owner quyết định 16/11/2026: đề xuất AI chỉ là **gợi ý**, phải backtest trước; owner duyệt bằng
bản nháp config rồi tự publish (MFA). Module này không đổi cấu hình bot nào.

Luồng cho mỗi đề xuất (``analyst_output.v2`` đã qua validator):

1. Từ cấu hình bot trong Run Report dựng hai "arm": baseline (cấu hình hiện hành) và variant
   (``apply_proposal``). Trường không kiểm được trong replay một-symbol (``risk.max_positions``,
   ``risk.max_leverage``) được liệt kê là ``unsupported_fields``, không bị bỏ qua lặng lẽ.
2. Mỗi coin trong universe của bot: dựng bundle từ nến **đã lưu** nguồn
   ``BINANCE_USDM_REST_BACKFILL`` với bằng chứng quyền BACKTEST_RESEARCH hiện hành (không gọi
   API giao dịch, không nội suy nến thiếu: coin thiếu nến bị loại kèm lý do).
3. Chạy hai arm trên **cùng** bundle bằng replay PAPER (đòn bẩy 1x, tối đa 1 vị thế, khớp ở giá
   mở nến kế tiếp của khung vào lệnh), gộp chỉ số: số lệnh, tỷ lệ thắng, profit factor, PnL ròng,
   drawdown lớn nhất theo coin.
4. Kết luận ``BETTER``/``WORSE``/``INCONCLUSIVE``; ít hơn ``MIN_TRADES_FOR_VERDICT`` lệnh ở cả hai
   arm thì luôn INCONCLUSIVE (mẫu nhỏ, không kết luận).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid5

from trading_bot.application.ai_analyst import H03_PARAMETERIZED, apply_proposal
from trading_bot.application.backtest import EvaluationReport
from trading_bot.application.backtest_dataset_manifest import (
    BacktestDatasetNotReady,
    build_backtest_dataset_manifest,
)
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_replay_executor import build_backtest_replay_plan
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.binance_rest_backfill import (
    BINANCE_USDM_REST_PROVIDER,
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
    market_source_for_rights_provider,
)
from trading_bot.application.strategy_market_requirements import (
    entry_timeframe_for_release,
    feature_requirements_for_release,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.portfolio.portfolio_backtest import (
    PortfolioLimits,
    PortfolioResult,
    PortfolioTrade,
    simulate_portfolio,
    trades_with_symbol_equity,
)

# v2 (CRY-386): thêm mô phỏng danh mục nhiều coin; max_positions/max_leverage kiểm được.
PROPOSAL_BACKTEST_VERSION = "ai3-proposal-backtest-v3"  # v3 (CRY-422): tiêu chí compare-v2
MIN_TRADES_FOR_VERDICT = 10
DEFAULT_RISK_FRACTION = Decimal("0.005")
STARTING_EQUITY = Decimal("10000")
FEE_RATE = Decimal("0.0005")
SLIPPAGE_RATE = Decimal("0.0005")
WARMUP_BARS = 220
# CRY-386: hai trường này giờ kiểm được bằng mô phỏng danh mục; tập rỗng giữ tương thích API.
PORTFOLIO_FIELDS = frozenset({"risk.max_positions", "risk.max_leverage"})
UNSUPPORTED_REPLAY_FIELDS: frozenset[str] = frozenset()
_NAMESPACE = UUID("6f1d0c52-5b3e-4c1e-9a8e-3c0f5a7d2b91")

_INTERVALS = {
    "1m": timedelta(minutes=1),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


@dataclass(frozen=True, slots=True)
class BacktestArm:
    """Một cấu hình chạy replay: release, tham số chiến lược, rủi ro mỗi lệnh."""

    release_id: str
    parameters: tuple[tuple[str, int], ...]
    per_trade_risk_fraction: Decimal

    def payload(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "parameters": dict(self.parameters),
            "per_trade_risk_fraction": str(self.per_trade_risk_fraction),
        }


def arm_from_overlay(overlay: Mapping[str, Any]) -> BacktestArm:
    """Overlay bot (``strategy``/``risk``) → arm; release không tham số thì bỏ tham số."""
    strategy = overlay.get("strategy") or {}
    release = str(strategy.get("release_id") or "")
    parameters = (
        tuple(sorted((str(k), int(v)) for k, v in (strategy.get("parameters") or {}).items()))
        if release in H03_PARAMETERIZED
        else ()
    )
    risk = overlay.get("risk") or {}
    fraction = risk.get("per_trade_risk_fraction")
    return BacktestArm(
        release_id=release,
        parameters=parameters,
        per_trade_risk_fraction=Decimal(str(fraction)) if fraction else DEFAULT_RISK_FRACTION,
    )


def arms_for_proposal(
    bot_config: Mapping[str, Any], changes: Sequence[Mapping[str, Any]]
) -> tuple[BacktestArm, BacktestArm, tuple[str, ...]]:
    """Baseline/variant từ ``config`` của bot trong Run Report và các thay đổi đề xuất."""
    overlay = {
        "strategy": {
            "release_id": bot_config.get("release_id"),
            "parameters": dict(bot_config.get("parameters") or {}),
        },
        "risk": dict(bot_config.get("risk") or {}),
    }
    unsupported = tuple(
        sorted({str(c["field"]) for c in changes if str(c["field"]) in UNSUPPORTED_REPLAY_FIELDS})
    )
    variant = apply_proposal(overlay, changes)
    return arm_from_overlay(overlay), arm_from_overlay(variant), unsupported


def required_timeframes(release_id: str) -> tuple[str, ...]:
    """Khung bundle cần cho release (feature + khung vào lệnh); lỗi nếu replay chưa hỗ trợ."""
    timeframes = set(feature_requirements_for_release(release_id))
    entry = entry_timeframe_for_release(release_id)
    if entry is None:
        raise ValueError(f"Release {release_id} chưa có khung vào lệnh cho replay.")
    timeframes.add(entry)
    return tuple(sorted(timeframes, key=lambda tf: _INTERVALS[tf]))


# ---------------------------------------------------------------------------
# Chỉ số
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolResult:
    """Kết quả một arm trên một coin (từ ``EvaluationReport`` + trade đóng)."""

    symbol: str
    trades: int
    wins: int
    gross_profit: Decimal
    gross_loss: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    # Lệnh đã đóng (cho mô phỏng danh mục); không đưa vào payload.
    portfolio_trades: tuple[PortfolioTrade, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trades": self.trades,
            "wins": self.wins,
            "net_pnl": str(self.net_pnl),
            "max_drawdown": str(self.max_drawdown),
        }


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    """Chỉ số gộp của một arm trên các coin đã chạy."""

    symbols: int
    trades: int
    wins: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    net_pnl: Decimal
    max_drawdown: Decimal

    def payload(self) -> dict[str, Any]:
        return {
            "symbols": self.symbols,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": _text(self.win_rate),
            "profit_factor": _text(self.profit_factor),
            "net_pnl": str(self.net_pnl),
            "max_drawdown": str(self.max_drawdown),
        }


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def aggregate(results: Sequence[SymbolResult]) -> ArmMetrics:
    """Gộp theo coin; drawdown = drawdown lớn nhất trong các coin (mỗi coin vốn riêng)."""
    trades = sum(item.trades for item in results)
    wins = sum(item.wins for item in results)
    profit = sum((item.gross_profit for item in results), Decimal(0))
    loss = sum((item.gross_loss for item in results), Decimal(0))
    return ArmMetrics(
        symbols=len(results),
        trades=trades,
        wins=wins,
        win_rate=(Decimal(wins) / Decimal(trades)).quantize(Decimal("0.0001")) if trades else None,
        profit_factor=(profit / loss).quantize(Decimal("0.01")) if loss > 0 else None,
        net_pnl=sum((item.net_pnl for item in results), Decimal(0)).quantize(Decimal("0.01")),
        max_drawdown=max((item.max_drawdown for item in results), default=Decimal(0)).quantize(
            Decimal("0.01")
        ),
    )


# CRY-422: tiêu chí v2 — không thưởng việc chỉ đổi quy mô lệnh (vd giảm rủi ro trên chiến lược lỗ).
COMPARE_RULE = "compare-v2"
SCALE_ONLY_PF_TOLERANCE = Decimal("0.02")


def _quality(metrics: ArmMetrics) -> Decimal | None:
    """Profit factor không phụ thuộc quy mô lệnh; không có lệnh lỗ mà có lãi → vô cùng."""
    if metrics.profit_factor is not None:
        return metrics.profit_factor
    if metrics.trades and metrics.wins:
        return Decimal("Infinity")
    return None


def _scale_only(baseline: ArmMetrics, variant: ArmMetrics) -> bool:
    """Cùng tập lệnh (cùng số lệnh, cùng số thắng), profit factor gần như không đổi: chỉ đổi
    kích thước lệnh nên lãi/lỗ đổi theo tỷ lệ còn chất lượng chiến lược giữ nguyên."""
    if (
        baseline.trades != variant.trades
        or baseline.wins != variant.wins
        or baseline.profit_factor is None
        or variant.profit_factor is None
    ):
        return False
    return abs(baseline.profit_factor - variant.profit_factor) <= SCALE_ONLY_PF_TOLERANCE


def compare(baseline: ArmMetrics, variant: ArmMetrics) -> tuple[str, list[str]]:
    """Kết luận thận trọng (``compare-v2``).

    - Dưới ``MIN_TRADES_FOR_VERDICT`` lệnh ở cả hai bản → INCONCLUSIVE.
    - Chỉ đổi quy mô lệnh (cùng tập lệnh, profit factor không đổi) → INCONCLUSIVE: PnL lớn/nhỏ hơn
      chỉ do lệnh to/nhỏ hơn, không phải chiến lược tốt/xấu hơn.
    - BETTER cần cả ba: PnL ròng tốt hơn, drawdown không xấu hơn quá 20%, profit factor không giảm.
    - WORSE khi PnL ròng kém hơn. Còn lại INCONCLUSIVE kèm lý do.
    """
    notes: list[str] = []
    if max(baseline.trades, variant.trades) < MIN_TRADES_FOR_VERDICT:
        notes.append(
            f"Mẫu nhỏ: dưới {MIN_TRADES_FOR_VERDICT} lệnh ở cả hai bản; chưa đủ để kết luận."
        )
        return "INCONCLUSIVE", notes
    if _scale_only(baseline, variant):
        notes.append(
            f"Chỉ đổi quy mô lệnh: cùng {variant.trades} lệnh, profit factor "
            f"{baseline.profit_factor}→{variant.profit_factor}; PnL đổi theo kích thước lệnh, "
            "chất lượng chiến lược không đổi."
        )
        if baseline.profit_factor is not None and baseline.profit_factor < 1:
            notes.append("Chiến lược đang lỗ (profit factor < 1): giảm quy mô chỉ làm lỗ ít hơn.")
        return "INCONCLUSIVE", notes
    pnl_better = variant.net_pnl > baseline.net_pnl
    drawdown_ok = variant.max_drawdown <= baseline.max_drawdown * Decimal("1.2") + Decimal("1")
    base_quality, var_quality = _quality(baseline), _quality(variant)
    quality_ok = base_quality is None or (var_quality is not None and var_quality >= base_quality)
    if pnl_better and drawdown_ok and quality_ok:
        if variant.net_pnl < 0:
            notes.append(
                f"Bản đề xuất vẫn lỗ ({variant.net_pnl} USDT); chỉ ít lỗ hơn bản gốc "
                f"({baseline.net_pnl} USDT)."
            )
        return "BETTER", notes
    if variant.net_pnl < baseline.net_pnl:
        return "WORSE", notes
    if pnl_better and not drawdown_ok:
        notes.append("PnL ròng tốt hơn nhưng drawdown xấu hơn quá 20%.")
    if pnl_better and not quality_ok:
        notes.append(
            f"PnL ròng tốt hơn nhưng profit factor giảm ({baseline.profit_factor}→"
            f"{variant.profit_factor}): có thể chỉ do lệnh lớn hơn."
        )
    return "INCONCLUSIVE", notes


def symbol_result(symbol: str, report: EvaluationReport, trades: Sequence[Any]) -> SymbolResult:
    nets = [trade.net_pnl for trade in trades]
    portfolio_trades = trades_with_symbol_equity(
        symbol,
        ((t.opened_at, t.closed_at, t.net_pnl, t.exposure) for t in trades),
        STARTING_EQUITY,
    )
    return SymbolResult(
        symbol=symbol,
        trades=report.trade_count,
        wins=sum(1 for value in nets if value > 0),
        gross_profit=sum((value for value in nets if value > 0), Decimal(0)),
        gross_loss=-sum((value for value in nets if value < 0), Decimal(0)),
        net_pnl=report.net_pnl,
        max_drawdown=report.max_drawdown,
        portfolio_trades=tuple(portfolio_trades),
    )


def portfolio_limits(risk: Mapping[str, Any]) -> PortfolioLimits:
    """Trần danh mục từ khối ``risk`` của cấu hình bot (thiếu → 1 vị thế, 1x)."""
    positions = risk.get("max_positions")
    leverage = risk.get("max_leverage")
    return PortfolioLimits(
        max_positions=int(str(positions)) if positions not in (None, "") else 1,
        max_leverage=Decimal(str(leverage)) if leverage not in (None, "") else Decimal(1),
    )


def limits_for_proposal(
    bot_config: Mapping[str, Any], changes: Sequence[Mapping[str, Any]]
) -> tuple[PortfolioLimits, PortfolioLimits]:
    risk = dict(bot_config.get("risk") or {})
    variant = apply_proposal({"risk": risk}, changes)
    return portfolio_limits(risk), portfolio_limits(variant.get("risk") or {})


def portfolio_metrics(result: PortfolioResult) -> ArmMetrics:
    trades = result.taken
    return ArmMetrics(
        symbols=result.symbols,
        trades=trades,
        wins=result.wins,
        win_rate=(Decimal(result.wins) / Decimal(trades)).quantize(Decimal("0.0001"))
        if trades
        else None,
        profit_factor=(result.gross_profit / result.gross_loss).quantize(Decimal("0.01"))
        if result.gross_loss > 0
        else None,
        net_pnl=result.net_pnl.quantize(Decimal("0.01")),
        max_drawdown=result.max_drawdown.quantize(Decimal("0.01")),
    )


def run_portfolio(results: Sequence[SymbolResult], limits: PortfolioLimits) -> PortfolioResult:
    trades = [trade for item in results for trade in item.portfolio_trades]
    return simulate_portfolio(trades, starting_equity=STARTING_EQUITY, limits=limits)


def _portfolio_payload(result: PortfolioResult, limits: PortfolioLimits) -> dict[str, Any]:
    return {
        **portfolio_metrics(result).payload(),
        **limits.payload(),
        "skipped_max_positions": result.skipped_positions,
        "skipped_leverage": result.skipped_leverage,
        "peak_open_positions": result.peak_open_positions,
    }


# ---------------------------------------------------------------------------
# Chạy replay
# ---------------------------------------------------------------------------


def run_arm(
    bundle: BacktestDatasetReplayBundle,
    arm: BacktestArm,
    *,
    seed: int = 20261116,
) -> SymbolResult:
    """Một arm trên một bundle; khớp lệnh ở khung vào lệnh (giá mở nến kế tiếp)."""
    execution = entry_timeframe_for_release(arm.release_id)
    if execution is None:
        raise ValueError(f"Release {arm.release_id} chưa có khung vào lệnh.")
    submission = BacktestReplayJobSubmission(
        bundle_hash=bundle.dataset_hash,
        strategy_release_id=arm.release_id,
        execution_interval=execution,
        starting_equity=STARTING_EQUITY,
        per_trade_risk_fraction=arm.per_trade_risk_fraction,
        fee_rate=FEE_RATE,
        slippage_rate=SLIPPAGE_RATE,
        seed=seed,
        strategy_parameters=arm.parameters,
    )
    job_id = uuid5(_NAMESPACE, f"{bundle.dataset_hash}:{arm.payload()}")
    plan = build_backtest_replay_plan(bundle.workspace_id, job_id, bundle, submission)
    comparison = plan.executor(bundle, plan.manifest)
    run = next(item for item in comparison.runs if item.sensitivity_run.scenario.kind == "BASE")
    report = run.sensitivity_run.result
    if not isinstance(report, EvaluationReport):
        raise ValueError(f"Replay bị từ chối: {getattr(report, 'reason_code', 'UNKNOWN')}.")
    return symbol_result(bundle.symbol, report, run.trade_outcomes)


@dataclass(frozen=True, slots=True)
class ProposalBacktestResult:
    """Kết quả so sánh một đề xuất; ``payload`` để lưu và vẽ thẻ."""

    bot_name: str
    baseline_arm: BacktestArm
    variant_arm: BacktestArm
    baseline: ArmMetrics
    variant: ArmMetrics
    verdict: str
    notes: tuple[str, ...]
    unsupported_fields: tuple[str, ...]
    skipped_symbols: tuple[tuple[str, str], ...]
    window: tuple[str, str] | None
    per_symbol: tuple[tuple[SymbolResult, SymbolResult], ...] = field(default=())
    # CRY-386: danh mục chung vốn cho từng bản; ``comparison_basis`` = cơ sở của kết luận.
    portfolio: dict[str, Any] | None = None
    comparison_basis: str = "per_symbol"

    def payload(self) -> dict[str, Any]:
        return {
            "version": PROPOSAL_BACKTEST_VERSION,
            "bot_name": self.bot_name,
            "baseline_arm": self.baseline_arm.payload(),
            "variant_arm": self.variant_arm.payload(),
            "baseline": self.baseline.payload(),
            "variant": self.variant.payload(),
            "verdict": self.verdict,
            "notes": list(self.notes),
            "unsupported_fields": list(self.unsupported_fields),
            "skipped_symbols": [{"symbol": s, "reason": r} for s, r in self.skipped_symbols],
            "window": list(self.window) if self.window else None,
            "per_symbol": [
                {"baseline": b.payload(), "variant": v.payload()} for b, v in self.per_symbol
            ],
            "comparison_basis": self.comparison_basis,
            "verdict_rule": COMPARE_RULE,
            "portfolio": self.portfolio,
            "assumptions": {
                "starting_equity_per_symbol": str(STARTING_EQUITY),
                "portfolio_starting_equity": str(STARTING_EQUITY),
                "fee_rate": str(FEE_RATE),
                "slippage_rate": str(SLIPPAGE_RATE),
                "per_trade_leverage": "1",
                "fill": "giá mở nến kế tiếp của khung vào lệnh",
                "portfolio_model": (
                    "gộp lệnh replay từng coin trên vốn chung; trần vị thế/đòn bẩy bỏ lệnh mới; "
                    "lệnh bị bỏ không sinh lại tín hiệu thay thế"
                ),
            },
        }


def _replay_symbol(
    bundle: BacktestDatasetReplayBundle,
    baseline_arm: BacktestArm,
    variant_arm: BacktestArm,
    same_arm: bool,
) -> tuple[SymbolResult, SymbolResult] | str:
    """Một coin, hai bản; lỗi replay → chuỗi lý do (để process con trả về được)."""
    try:
        base = run_arm(bundle, baseline_arm)
        # Chỉ đổi trần danh mục: lệnh từng coin giống hệt, không replay lần hai.
        return base, base if same_arm else run_arm(bundle, variant_arm)
    except ValueError as error:
        return str(error)[:160]


def _replay_symbols(
    ready: Sequence[tuple[str, BacktestDatasetReplayBundle]],
    baseline_arm: BacktestArm,
    variant_arm: BacktestArm,
    same_arm: bool,
    workers: int,
) -> list[tuple[SymbolResult, SymbolResult] | str]:
    if workers <= 1 or len(ready) <= 1:
        return [_replay_symbol(b, baseline_arm, variant_arm, same_arm) for _, b in ready]
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=min(workers, len(ready))) as pool:
        futures = [
            pool.submit(_replay_symbol, bundle, baseline_arm, variant_arm, same_arm)
            for _, bundle in ready
        ]
        return [future.result() for future in futures]


def backtest_proposal(
    *,
    bot_name: str,
    bot_config: Mapping[str, Any],
    changes: Sequence[Mapping[str, Any]],
    bundles: Mapping[str, BacktestDatasetReplayBundle | str],
    workers: int = 1,
) -> ProposalBacktestResult:
    """So sánh baseline/variant trên cùng bundle mỗi coin; ``str`` = lý do coin bị loại.

    ``workers`` > 1: replay các coin song song bằng process pool (CRY-423); kết quả gom theo thứ
    tự coin đã sắp nên trùng khớp chạy tuần tự (mỗi coin tất định, seed cố định).

    Đổi trần danh mục (``max_positions``/``max_leverage``) → kết luận theo mô phỏng danh mục chung
    vốn; ngược lại kết luận theo tổng từng coin như trước, danh mục kèm để tham khảo.
    """
    baseline_arm, variant_arm, unsupported = arms_for_proposal(bot_config, changes)
    base_limits, var_limits = limits_for_proposal(bot_config, changes)
    portfolio_change = base_limits != var_limits
    if baseline_arm == variant_arm and not portfolio_change:
        empty = aggregate([])
        return ProposalBacktestResult(
            bot_name=bot_name,
            baseline_arm=baseline_arm,
            variant_arm=variant_arm,
            baseline=empty,
            variant=empty,
            verdict="UNSUPPORTED",
            notes=("Đề xuất không thay đổi gì so với cấu hình hiện tại.",),
            unsupported_fields=unsupported,
            skipped_symbols=(),
            window=None,
        )
    same_arm = baseline_arm == variant_arm
    skipped: list[tuple[str, str]] = []
    pairs: list[tuple[SymbolResult, SymbolResult]] = []
    windows: list[tuple[datetime, datetime]] = []
    ready: list[tuple[str, BacktestDatasetReplayBundle]] = []
    for symbol in sorted(bundles):
        bundle = bundles[symbol]
        if isinstance(bundle, str):
            skipped.append((symbol, bundle))
        else:
            ready.append((symbol, bundle))
    outcomes = _replay_symbols(ready, baseline_arm, variant_arm, same_arm, workers)
    for (symbol, bundle), outcome in zip(ready, outcomes, strict=True):
        if isinstance(outcome, str):
            skipped.append((symbol, outcome))
            continue
        pairs.append(outcome)
        windows.append((bundle.evaluation_start, bundle.range_end))
    skipped.sort()
    base_portfolio = run_portfolio([b for b, _ in pairs], base_limits)
    var_portfolio = run_portfolio([v for _, v in pairs], var_limits)
    portfolio = {
        "baseline": _portfolio_payload(base_portfolio, base_limits),
        "variant": _portfolio_payload(var_portfolio, var_limits),
    }
    if portfolio_change:
        baseline = portfolio_metrics(base_portfolio)
        variant = portfolio_metrics(var_portfolio)
    else:
        baseline = aggregate([b for b, _ in pairs])
        variant = aggregate([v for _, v in pairs])
    verdict, notes = compare(baseline, variant)
    if not pairs:
        verdict = "INCONCLUSIVE"
        notes = ["Không coin nào chạy được backtest."]
    if portfolio_change and pairs:
        notes.append(
            "Đo trên danh mục chung vốn "
            f"({base_limits.max_positions}→{var_limits.max_positions} vị thế, "
            f"{base_limits.max_leverage}x→{var_limits.max_leverage}x): bỏ "
            f"{base_portfolio.skipped_positions + base_portfolio.skipped_leverage}→"
            f"{var_portfolio.skipped_positions + var_portfolio.skipped_leverage} lệnh vì trần. "
            "Xấp xỉ: lệnh bị bỏ không sinh lại tín hiệu thay thế; mỗi lệnh vẫn ≤ 1x vốn."
        )
    window = (
        (min(w[0] for w in windows).isoformat(), max(w[1] for w in windows).isoformat())
        if windows
        else None
    )
    return ProposalBacktestResult(
        bot_name=bot_name,
        baseline_arm=baseline_arm,
        variant_arm=variant_arm,
        baseline=baseline,
        variant=variant,
        verdict=verdict,
        notes=tuple(notes),
        unsupported_fields=unsupported,
        skipped_symbols=tuple(skipped),
        window=window,
        per_symbol=tuple(pairs),
        portfolio=portfolio,
        comparison_basis="portfolio" if portfolio_change else "per_symbol",
    )


DRAFTABLE_VERDICTS = frozenset({"BETTER", "INCONCLUSIVE"})


def draft_overlay_from_backtest(
    head_overlay: Mapping[str, Any],
    proposal: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay bản nháp = overlay đang publish + thay đổi đề xuất, chỉ khi còn khớp backtest.

    Chặn khi kết luận WORSE/UNSUPPORTED, hoặc cấu hình bot đã đổi so với bản gốc đã backtest
    (kết quả không còn áp dụng; cần backtest lại). Owner vẫn phải validate + publish (MFA).
    """
    verdict = str(result.get("verdict", ""))
    if verdict not in DRAFTABLE_VERDICTS:
        raise ValueError(f"Kết luận {verdict or 'không rõ'}: không tạo bản nháp từ đề xuất này.")
    if arm_from_overlay(head_overlay).payload() != result.get("baseline_arm"):
        raise ValueError(
            "Cấu hình bot đã đổi so với bản gốc đã backtest; cần backtest lại trước khi áp."
        )
    stored = ((result.get("portfolio") or {}).get("baseline")) or {}
    head_risk = head_overlay.get("risk") or {}
    for key, cast in (("max_positions", int), ("max_leverage", Decimal)):
        if (
            key in head_risk
            and key in stored
            and cast(str(head_risk[key])) != cast(str(stored[key]))
        ):
            raise ValueError(
                "Cấu hình bot đã đổi so với bản gốc đã backtest; cần backtest lại trước khi áp."
            )
    changes = proposal.get("changes") or []
    if not changes:
        raise ValueError("Đề xuất không có thay đổi.")
    return apply_proposal(head_overlay, changes)


# ---------------------------------------------------------------------------
# Dựng bundle từ nến đã lưu (trong Unit of Work của caller)
# ---------------------------------------------------------------------------


class StoredBars(Protocol):
    async def list_bars(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
        closed_only: bool = False,
        source: str | None = None,
    ) -> list[OHLCVBar]: ...


class RightsGate(Protocol):
    async def authorize(
        self, *, request: RestBackfillRequest, now: datetime
    ) -> BackfillRightsEvidence: ...


def _floor(instant: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((instant - epoch) // step) * step


async def build_stored_bundle(
    *,
    workspace_id: UUID,
    symbol: str,
    timeframes: Sequence[str],
    market_data: StoredBars,
    rights_gate: RightsGate,
    now: datetime,
    warmup_bars: int = WARMUP_BARS,
) -> BacktestDatasetReplayBundle | str:
    """Bundle chung cửa sổ đánh giá cho mọi khung; trả lý do (str) nếu không dựng được.

    Cửa sổ = phần chung của các khung sau khi trừ warm-up, căn theo khung lớn nhất. Nến thiếu
    trong cửa sổ → manifest không READY → coin bị loại (không nội suy).
    """
    source = market_source_for_rights_provider(BINANCE_USDM_REST_PROVIDER)
    largest = max(_INTERVALS[tf] for tf in timeframes)
    lookback_start = now - timedelta(days=400)
    series: dict[str, list[OHLCVBar]] = {}
    for timeframe in timeframes:
        bars = await market_data.list_bars(
            workspace_id=workspace_id,
            symbol=symbol,
            timeframe=timeframe,
            start=lookback_start,
            end=now,
            as_of=now,
            closed_only=True,
            source=source,
        )
        if len(bars) <= warmup_bars:
            return f"Thiếu nến {timeframe}: có {len(bars)}, cần > {warmup_bars} (warm-up)."
        series[timeframe] = sorted(bars, key=lambda bar: bar.open_time)
    range_end = _floor(
        min(bars[-1].open_time + _INTERVALS[tf] for tf, bars in series.items()), largest
    )
    evaluation_start = _floor(
        max(
            bars[0].open_time + _INTERVALS[tf] * warmup_bars + largest
            for tf, bars in series.items()
        ),
        largest,
    )
    if evaluation_start >= range_end:
        return "Không có cửa sổ đánh giá chung giữa các khung sau warm-up."
    inputs: list[BacktestDatasetReplayInput] = []
    for timeframe, bars in series.items():
        step = _INTERVALS[timeframe]
        start = evaluation_start - step * warmup_bars
        request = RestBackfillRequest(
            symbol=symbol,
            interval=timeframe,
            start=start,
            end=range_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        )
        evidence = await rights_gate.authorize(request=request, now=now)
        window = [bar for bar in bars if start <= bar.open_time < range_end]
        manifest = build_backtest_dataset_manifest(
            request,
            evaluation_start=evaluation_start,
            warmup_bars=warmup_bars,
            bars=window,
            rights_evidence=evidence,
            prepared_at=now,
        )
        try:
            manifest.require_ready()
        except BacktestDatasetNotReady as error:
            return f"{timeframe}: {error}"
        inputs.append(
            BacktestDatasetReplayInput(
                manifest=manifest, bars=tuple(window), current_rights=evidence
            )
        )
    return build_backtest_dataset_replay_bundle(inputs)


__all__ = [
    "MIN_TRADES_FOR_VERDICT",
    "DRAFTABLE_VERDICTS",
    "PROPOSAL_BACKTEST_VERSION",
    "ArmMetrics",
    "BacktestArm",
    "ProposalBacktestResult",
    "SymbolResult",
    "aggregate",
    "arm_from_overlay",
    "arms_for_proposal",
    "backtest_proposal",
    "build_stored_bundle",
    "compare",
    "draft_overlay_from_backtest",
    "required_timeframes",
    "run_arm",
]
