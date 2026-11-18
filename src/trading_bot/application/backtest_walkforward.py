"""BT-1: Walk-forward backtest cho chiến lược H0x và chiết khấu theo số lượt thử."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from trading_bot.application.backtest import EvaluationReport
from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_replay_executor import build_backtest_replay_plan
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.binance_rest_backfill import BackfillRightsPurpose, RestBackfillRequest
from trading_bot.domain.learning.evaluation import (
    MIN_TRADES_FOR_CONFIDENCE_INTERVAL,
    EvaluationMetrics,
    EvaluationTrade,
    build_evaluation_metrics,
)
from trading_bot.domain.portfolio.scalping_validation import Fold, walk_forward_folds

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class WalkForwardFoldOutcome:
    """Kết quả out-of-sample của một fold trong walk-forward H0x."""

    fold_index: int
    test_start: datetime
    test_end: datetime
    observation_count: int
    trade_count: int
    net_pnl: Decimal
    expectancy: Decimal
    max_drawdown: Decimal


@dataclass(frozen=True, slots=True)
class WalkForwardBacktestReport:
    """Báo cáo tổng hợp walk-forward OOS cho H0x kèm ngưỡng mẫu và chiết khấu thử nghiệm."""

    strategy_release_id: str
    trial_count: int
    haircut_factor: Decimal
    folds: tuple[WalkForwardFoldOutcome, ...]
    metrics: EvaluationMetrics
    discounted_expectancy: Decimal
    sample_status: str

    @property
    def has_enough_samples(self) -> bool:
        """Đủ ngưỡng tối thiểu để báo khoảng tin cậy hay chưa."""
        return self.metrics.confidence_interval is not None


def multiple_testing_haircut(trial_count: int) -> Decimal:
    """Hệ số chiết khấu kỳ vọng theo số lượt thử (1 / sqrt(1 + ln(trial_count)))."""
    if type(trial_count) is not int or trial_count < 1:
        raise ValueError("trial_count phải là số nguyên >= 1.")
    if trial_count == 1:
        return Decimal("1")
    factor = 1.0 / math.sqrt(1.0 + math.log(trial_count))
    return Decimal(str(round(factor, 6)))


def _slice_bundle_for_fold(
    bundle: BacktestDatasetReplayBundle,
    fold: Fold,
) -> BacktestDatasetReplayBundle:
    """Tạo bundle cho fold OOS [fold.start, fold.end), giữ warm-up trước fold.start."""
    durations = [
        RestBackfillRequest(
            symbol=series.manifest.symbol,
            interval=series.manifest.interval,
            start=series.manifest.warmup_start,
            end=series.manifest.range_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        ).interval_duration
        for series in bundle.components
    ]
    align_interval = max(durations)
    base_start = bundle.evaluation_start
    start_offset = math.ceil((fold.start - base_start) / align_interval)
    end_offset = int((fold.end - base_start) // align_interval)
    fold_start = base_start + align_interval * start_offset
    fold_end = base_start + align_interval * end_offset
    if fold_end <= fold_start:
        raise ValueError("Khoảng fold không đủ một nến evaluation theo timeframe lớn nhất.")

    inputs: list[BacktestDatasetReplayInput] = []
    for series, interval in zip(bundle.components, durations, strict=True):
        warmup_start = series.manifest.warmup_start
        warmup_bars = int((fold_start - warmup_start) // interval)
        if warmup_bars <= 0:
            raise ValueError("Khoảng fold không đủ warm-up.")
        sliced_bars = tuple(bar for bar in series.bars if warmup_start <= bar.open_time < fold_end)
        req = RestBackfillRequest(
            symbol=series.manifest.symbol,
            interval=series.manifest.interval,
            start=warmup_start,
            end=fold_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        )
        manifest = build_backtest_dataset_manifest(
            req,
            evaluation_start=fold_start,
            warmup_bars=warmup_bars,
            bars=sliced_bars,
            rights_evidence=series.current_rights,
            prepared_at=max(series.manifest.prepared_at, fold_end),
            schema_version=series.manifest.schema_version,
        )
        inputs.append(BacktestDatasetReplayInput(manifest, sliced_bars, series.current_rights))
    return build_backtest_dataset_replay_bundle(tuple(inputs))


def run_h0x_walk_forward(
    workspace_id: UUID,
    job_id: UUID,
    bundle: BacktestDatasetReplayBundle,
    submission: BacktestReplayJobSubmission,
    *,
    folds: int = 3,
    embargo: timedelta = timedelta(0),
    min_trades_for_ci: int = MIN_TRADES_FOR_CONFIDENCE_INTERVAL,
) -> WalkForwardBacktestReport:
    """Chạy walk-forward OOS cho chiến lược H0x với cùng hồ sơ chi phí PAPER và trial_count."""
    fold_windows = walk_forward_folds(
        bundle.evaluation_start,
        bundle.range_end,
        folds=folds,
        embargo=embargo,
    )
    fold_outcomes: list[WalkForwardFoldOutcome] = []
    oos_trades: list[EvaluationTrade] = []
    total_observations = 0

    for fold in fold_windows:
        fold_bundle = _slice_bundle_for_fold(bundle, fold)
        fold_job_id = uuid5(job_id, f"wf-fold-{fold.index}")
        fold_sub = replace(submission, bundle_hash=fold_bundle.dataset_hash)
        plan = build_backtest_replay_plan(workspace_id, fold_job_id, fold_bundle, fold_sub)
        comparison = plan.executor(fold_bundle, plan.manifest)
        if not comparison.is_complete or not comparison.runs:
            detail = f" ({comparison.runs[0].sensitivity_run.result!r})" if comparison.runs else ""
            raise RuntimeError(f"Fold {fold.index} không hoàn tất replay{detail}.")
        run = comparison.runs[0]
        res = run.sensitivity_run.result
        if not isinstance(res, EvaluationReport):
            raise RuntimeError(f"Fold {fold.index} bị từ chối: {res.reason_code}.")
        total_observations += res.observation_count
        oos_trades.extend(run.trade_outcomes)
        fold_outcomes.append(
            WalkForwardFoldOutcome(
                fold_index=fold.index,
                test_start=fold.start,
                test_end=fold.end,
                observation_count=res.observation_count,
                trade_count=res.trade_count,
                net_pnl=res.net_pnl,
                expectancy=res.expectancy,
                max_drawdown=res.max_drawdown,
            )
        )

    ordered_trades = tuple(sorted(oos_trades, key=lambda item: (item.closed_at, item.trade_id)))
    metrics = build_evaluation_metrics(
        ordered_trades,
        observation_count=total_observations,
        seed=submission.seed,
        min_trades_for_ci=min_trades_for_ci,
    )
    haircut = multiple_testing_haircut(submission.trial_count)
    discounted = metrics.expectancy * haircut
    sample_status = "READY" if metrics.confidence_interval is not None else "INSUFFICIENT_SAMPLE"
    return WalkForwardBacktestReport(
        strategy_release_id=submission.strategy_release_id,
        trial_count=submission.trial_count,
        haircut_factor=haircut,
        folds=tuple(fold_outcomes),
        metrics=metrics,
        discounted_expectancy=discounted,
        sample_status=sample_status,
    )


__all__ = [
    "WalkForwardBacktestReport",
    "WalkForwardFoldOutcome",
    "multiple_testing_haircut",
    "run_h0x_walk_forward",
]
