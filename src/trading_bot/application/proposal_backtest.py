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

PROPOSAL_BACKTEST_VERSION = "ai3-proposal-backtest-v1"
MIN_TRADES_FOR_VERDICT = 10
DEFAULT_RISK_FRACTION = Decimal("0.005")
STARTING_EQUITY = Decimal("10000")
FEE_RATE = Decimal("0.0005")
SLIPPAGE_RATE = Decimal("0.0005")
WARMUP_BARS = 220
UNSUPPORTED_REPLAY_FIELDS = frozenset({"risk.max_positions", "risk.max_leverage"})
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


def compare(baseline: ArmMetrics, variant: ArmMetrics) -> tuple[str, list[str]]:
    """Kết luận thận trọng: cần đủ lệnh, PnL ròng tốt hơn và drawdown không xấu hơn quá 20%."""
    notes: list[str] = []
    if max(baseline.trades, variant.trades) < MIN_TRADES_FOR_VERDICT:
        notes.append(
            f"Mẫu nhỏ: dưới {MIN_TRADES_FOR_VERDICT} lệnh ở cả hai bản; chưa đủ để kết luận."
        )
        return "INCONCLUSIVE", notes
    pnl_better = variant.net_pnl > baseline.net_pnl
    drawdown_ok = variant.max_drawdown <= baseline.max_drawdown * Decimal("1.2") + Decimal("1")
    if pnl_better and drawdown_ok:
        return "BETTER", notes
    if not pnl_better and variant.net_pnl < baseline.net_pnl:
        return "WORSE", notes
    if pnl_better and not drawdown_ok:
        notes.append("PnL ròng tốt hơn nhưng drawdown xấu hơn quá 20%.")
    return "INCONCLUSIVE", notes


def symbol_result(symbol: str, report: EvaluationReport, trades: Sequence[Any]) -> SymbolResult:
    nets = [trade.net_pnl for trade in trades]
    return SymbolResult(
        symbol=symbol,
        trades=report.trade_count,
        wins=sum(1 for value in nets if value > 0),
        gross_profit=sum((value for value in nets if value > 0), Decimal(0)),
        gross_loss=-sum((value for value in nets if value < 0), Decimal(0)),
        net_pnl=report.net_pnl,
        max_drawdown=report.max_drawdown,
    )


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
            "assumptions": {
                "starting_equity_per_symbol": str(STARTING_EQUITY),
                "fee_rate": str(FEE_RATE),
                "slippage_rate": str(SLIPPAGE_RATE),
                "leverage": "1",
                "max_positions": 1,
                "fill": "giá mở nến kế tiếp của khung vào lệnh",
            },
        }


def backtest_proposal(
    *,
    bot_name: str,
    bot_config: Mapping[str, Any],
    changes: Sequence[Mapping[str, Any]],
    bundles: Mapping[str, BacktestDatasetReplayBundle | str],
) -> ProposalBacktestResult:
    """So sánh baseline/variant trên cùng bundle mỗi coin; ``str`` = lý do coin bị loại."""
    baseline_arm, variant_arm, unsupported = arms_for_proposal(bot_config, changes)
    if baseline_arm == variant_arm:
        # Chỉ đổi trường replay không mô phỏng được → hai bản giống hệt, chạy là vô nghĩa.
        empty = aggregate([])
        return ProposalBacktestResult(
            bot_name=bot_name,
            baseline_arm=baseline_arm,
            variant_arm=variant_arm,
            baseline=empty,
            variant=empty,
            verdict="UNSUPPORTED",
            notes=(
                "Đề xuất chỉ đổi trường replay một coin/1 vị thế không mô phỏng được: "
                + ", ".join(unsupported or ("(không đổi gì)",))
                + ". Cần backtest danh mục nhiều coin (chưa có).",
            ),
            unsupported_fields=unsupported,
            skipped_symbols=(),
            window=None,
        )
    skipped: list[tuple[str, str]] = []
    pairs: list[tuple[SymbolResult, SymbolResult]] = []
    windows: list[tuple[datetime, datetime]] = []
    for symbol in sorted(bundles):
        bundle = bundles[symbol]
        if isinstance(bundle, str):
            skipped.append((symbol, bundle))
            continue
        try:
            pairs.append((run_arm(bundle, baseline_arm), run_arm(bundle, variant_arm)))
        except ValueError as error:
            skipped.append((symbol, str(error)[:160]))
            continue
        windows.append((bundle.evaluation_start, bundle.range_end))
    baseline = aggregate([b for b, _ in pairs])
    variant = aggregate([v for _, v in pairs])
    verdict, notes = compare(baseline, variant)
    if not pairs:
        verdict = "INCONCLUSIVE"
        notes = ["Không coin nào chạy được backtest."]
    if unsupported:
        notes.append(
            "Không kiểm được trong replay một coin/1 vị thế: " + ", ".join(unsupported) + "."
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
