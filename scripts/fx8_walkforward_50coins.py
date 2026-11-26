"""Kiểm định Walk-Forward OOS & độ nhạy tham số ±20% trên 50 coin dữ liệu thật cho FX-8 (CRY-30).

Tái sử dụng helper đọc DB, tính chỉ báo và PaperExecutionGateway từ ``fx8_backtest_compare.py``,
mở rộng lên 50 coin Binance USDⓈ-M (20 coin plan + 30 coin thanh khoản cao nhất), chia 4 folds
OOS có embargo 24h, tính khoảng tin cậy 95% bootstrap cho kỳ vọng R và kiểm tra độ nhạy ±20%.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any

from scripts.fx8_backtest_compare import (
    FEE_RATE,
    SLIPPAGE_RATE,
    SPREAD_RATE,
    STARTING_EQUITY,
    TradeRecord,
    _compute_indicators,
    _planner_settings,
    _trim_contiguous_tail,
)

from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.domain.execution.paper import (
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
    PaperPosition,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    PlanRejected,
    TradePlan,
    TradePlanner,
)
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    FeatureQuality,
    MarketSeries,
    SeriesQuality,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.rules_fx8 import exit_evidence_spec

SYMBOLS_50: tuple[str, ...] = (
    # 20 coin trong signal-test-plan.json
    "AAVEUSDT",
    "AEROUSDT",
    "ARIAUSDT",
    "ATOMUSDT",
    "BTCUSDT",
    "BTWUSDT",
    "CRVUSDT",
    "DASHUSDT",
    "DODOXUSDT",
    "ETHUSDT",
    "ICPUSDT",
    "KAVAUSDT",
    "PENGUUSDT",
    "PUMPUSDT",
    "SOPHUSDT",
    "TACUSDT",
    "VIRTUALUSDT",
    "WLDUSDT",
    "XMRUSDT",
    "ZECUSDT",
    # 30 coin thanh khoản cao nhất bổ sung từ Binance USDⓈ-M
    "SOLUSDT",
    "XRPUSDT",
    "HYPEUSDT",
    "BIGTIMEUSDT",
    "NEARUSDT",
    "MANTRAUSDT",
    "1000PEPEUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "EVAAUSDT",
    "MMTUSDT",
    "PAXGUSDT",
    "ENAUSDT",
    "SUIUSDT",
    "ADAUSDT",
    "UNIUSDT",
    "VVVUSDT",
    "LINKUSDT",
    "PIEVERSEUSDT",
    "BLESSUSDT",
    "LISTAUSDT",
    "SSVUSDT",
    "XAUTUSDT",
    "AKEUSDT",
    "TAOUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "GUAUSDT",
    "TRUMPUSDT",
    "FILUSDT",
)

# 4 folds OOS x 45 ngày trên 180 ngày (2026-05-26 -> 2026-11-22), embargo 24h giữa các fold.
WF_FOLDS: tuple[tuple[str, datetime, datetime], ...] = (
    ("F1", datetime(2026, 5, 26, 0, 0, tzinfo=UTC), datetime(2026, 7, 9, 0, 0, tzinfo=UTC)),
    ("F2", datetime(2026, 7, 10, 0, 0, tzinfo=UTC), datetime(2026, 8, 23, 0, 0, tzinfo=UTC)),
    ("F3", datetime(2026, 8, 24, 0, 0, tzinfo=UTC), datetime(2026, 10, 7, 0, 0, tzinfo=UTC)),
    ("F4", datetime(2026, 10, 8, 0, 0, tzinfo=UTC), datetime(2026, 11, 22, 0, 0, tzinfo=UTC)),
)


@dataclass(frozen=True, slots=True)
class FoldStats:
    fold: str
    trades: int
    win_rate_pct: float
    expectancy_r: float
    net_pnl: float


@dataclass(frozen=True, slots=True)
class VariantReport:
    label: str
    release_id: str
    parameters: dict[str, Any]
    active_coins: int
    trades: int
    wins: int
    win_rate_pct: float
    expectancy_r: float
    ci95_low_r: float
    ci95_high_r: float
    net_pnl: float
    total_cost: float
    positive_folds: int
    folds: list[FoldStats]


def _load_real_1h_bars(symbol: str) -> tuple[OHLCVBar, ...]:
    """Đọc nến 1h thật từ `/tmp/fx8_extra30` (30 coin bổ sung) hoặc Postgres (20 coin plan)."""
    interval = timedelta(hours=1)
    jsonl_path = Path(f"/tmp/fx8_extra30/{symbol}-1h.jsonl")
    if jsonl_path.exists():
        rows: list[dict[str, str]] = [
            json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line
        ]
    else:
        query = (
            "SELECT DISTINCT ON (open_time) open_time, close_time, open, high, low, close, "
            "volume, quote_volume "
            "FROM market_bar "
            f"WHERE symbol = '{symbol}' AND timeframe = '1h' "
            "AND source IN ('BINANCE_USDM', 'BINANCE_USDM_REST_BACKFILL') AND is_closed = true "
            "ORDER BY open_time ASC, CASE WHEN source = 'BINANCE_USDM' THEN 0 ELSE 1 END"
        )
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "crypidian-review-postgres-1",
                "psql",
                "-U",
                "crypidian_admin",
                "-d",
                "crypidian",
                "--csv",
                "-c",
                query,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = list(csv.DictReader(io.StringIO(proc.stdout)))
    bars: list[OHLCVBar] = []
    for idx, row in enumerate(rows):
        opened = datetime.fromisoformat(row["open_time"]).astimezone(UTC)
        closed = opened + interval - timedelta(milliseconds=1)
        o_p, h_p, l_p, c_p = (
            Decimal(row["open"]),
            Decimal(row["high"]),
            Decimal(row["low"]),
            Decimal(row["close"]),
        )
        if h_p < max(o_p, c_p) or l_p > min(o_p, c_p) or l_p <= Decimal("0"):
            continue
        bars.append(
            OHLCVBar(
                symbol=symbol,
                timeframe="1h",
                open_time=opened,
                close_time=closed,
                event_at=closed,
                received_at=closed,
                available_at=closed,
                open=o_p,
                high=h_p,
                low=l_p,
                close=c_p,
                volume=Decimal(row["volume"]),
                quote_volume=Decimal(row["quote_volume"]),
                taker_buy_base_volume=Decimal("0"),
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                source_cursor=f"1h:{idx}",
                checksum="0" * 64,
                raw_reference=f"pg:{symbol}:1h:{idx}",
            )
        )
    return _trim_contiguous_tail(tuple(bars), "1h")


def _simulate_fold(
    *,
    symbol: str,
    release_id: str,
    parameters: dict[str, Any],
    bars: tuple[OHLCVBar, ...],
    indicators: dict[str, tuple[float | None, ...]],
    fold_start: datetime,
    fold_end: datetime,
) -> list[TradeRecord]:
    registry = strategy_registry()
    release = registry.get_release(release_id)
    selection = StrategySelection(
        settings_version_id="fx8-backtest-settings-v1",
        active_release_ids=(release_id,),
        published=True,
        parameters=tuple(sorted(parameters.items())),
    )
    rule = registry.resolve(release_id, selection)
    planner = TradePlanner()
    planner_settings = _planner_settings(release_id, release.content_hash)
    cost_model = CostModel(
        version_id="fx8-paper-cost-v1",
        entry_fee_rate=FEE_RATE,
        exit_fee_rate=FEE_RATE,
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=SLIPPAGE_RATE,
        exit_slippage_rate=SLIPPAGE_RATE,
        adverse_funding_rate=Decimal("0.0001"),
        expected_funding_periods=1,
    )
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="fx8-paper-policy-v1",
            latency=timedelta(seconds=0),
            spread_rate=SPREAD_RATE,
            slippage_rate=SLIPPAGE_RATE,
            fee_rate=FEE_RATE,
        )
    )
    interval = timedelta(hours=1)
    quote_volumes = tuple(float(b.quote_volume or Decimal("0")) for b in bars)
    equity = STARTING_EQUITY
    trades: list[TradeRecord] = []
    open_pos_risk: dict[str, Decimal] = {}
    is_h06 = release_id.startswith("h06-")

    for idx in range(140, len(bars)):
        bar = bars[idx]
        if bar.open_time < fold_start:
            continue
        if bar.open_time >= fold_end:
            break

        paper_bar = PaperBar(
            bar_key=f"{symbol}|1h|{bar.open_time.isoformat()}",
            symbol=symbol,
            opened_at=bar.open_time,
            closed_at=bar.close_time,
            available_at=bar.close_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            bar_source="BINANCE_USDM_REST_BACKFILL",
        )
        gateway.apply_market_event(paper_bar)
        for pos in list(gateway.open_positions()):
            if pos.entry_bar_key == paper_bar.bar_key:
                continue
            spec = exit_evidence_spec(pos.recipe.recipe_id)
            channel_level: Decimal | None = None
            feature_val: Decimal | None = None
            if spec is not None and spec.channel_period is not None and idx >= spec.channel_period:
                ch_bars = bars[idx - spec.channel_period : idx]
                channel_level = (
                    min(b.low for b in ch_bars)
                    if pos.direction == "long"
                    else max(b.high for b in ch_bars)
                )
            if spec is not None and spec.feature_name == "zscore_20":
                zv = indicators["zscore_20"][idx]
                if zv is not None:
                    feature_val = Decimal(str(zv))
            atr_v = indicators["atr_14"][idx]
            result = gateway.monitor_position(
                pos,
                paper_bar,
                opposing_signal=False,
                atr=Decimal(str(atr_v)) if atr_v is not None and atr_v > 0 else None,
                exit_channel_level=channel_level,
                exit_feature_value=feature_val,
                strategy_bar_closed=True,
            )
            closed_pos: PaperPosition = result.position
            if closed_pos.is_closed:
                net_pnl = closed_pos.net_realized_pnl
                total_cost = (
                    closed_pos.fee_cost + closed_pos.slippage_cost + closed_pos.funding_cost
                )
                risk_amt = open_pos_risk.pop(closed_pos.intent_id, Decimal("100"))
                r_mult = float(net_pnl / risk_amt) if risk_amt > 0 else 0.0
                equity += net_pnl
                trades.append(
                    TradeRecord(
                        symbol=symbol,
                        release_id=release_id,
                        direction=closed_pos.direction,
                        net_pnl=net_pnl,
                        total_cost=total_cost,
                        r_multiple=r_mult,
                        exit_reason=closed_pos.exit_reason or "UNKNOWN",
                    )
                )

        if gateway.open_positions() or gateway.open_orders():
            continue

        start_i = max(0, idx - 139)
        slice_bars = bars[start_i : idx + 1]
        slice_vols = quote_volumes[start_i : idx + 1]
        series = MarketSeries(
            timeframe="1h",
            bars=slice_bars,
            quote_volumes=slice_vols,
            quality=SeriesQuality.READY,
            expected_interval=interval,
            max_event_age=interval * 2,
            backfill_complete=True,
        )
        feat_list = []
        for fname in (
            "ema_20",
            "ema_80",
            "adx_14",
            "atr_14",
            "efficiency_ratio_40",
            "zscore_20",
            "rsi_14",
        ):
            val = indicators[fname][idx]
            prev = indicators[fname][idx - 1] if idx > 0 else None
            if val is not None and math.isfinite(val):
                feat_list.append(
                    FeatureObservation(
                        feature_id=f"1h:{fname}:{idx}",
                        name=fname,
                        timeframe="1h",
                        value=val,
                        previous_value=prev,
                        available_at=bar.close_time,
                        quality=FeatureQuality.READY,
                    )
                )
        decision_time = bar.close_time + timedelta(microseconds=1)
        snapshot = DecisionSnapshot(
            symbol=symbol,
            decision_time=decision_time,
            snapshot_id=f"{release_id}:{symbol}:{idx}".ljust(64, "0")[:64],
            series=(series,),
            features=tuple(feat_list),
            position_is_flat=True,
            macro_blackout=False if is_h06 else None,
            macro_evidence_ids=("macro:wf50-clear",) if is_h06 else (),
            macro_evidence_available_at=bar.close_time if is_h06 else None,
            macro_is_fresh=True if is_h06 else None,
        )
        decision = rule.evaluate(snapshot, selection)
        if not isinstance(decision, StrategyCandidate):
            continue
        account = AccountSnapshot(
            workspace_id="fx8-backtest-ws",
            account_id="fx8-paper-account",
            settings_version_id="fx8-backtest-settings-v1",
            captured_at=decision_time,
            equity=equity,
            available_margin=equity,
            is_known=True,
        )
        plan = planner.plan(decision, planner_settings, account, (), cost_model, now=decision_time)
        if isinstance(plan, PlanRejected) or not isinstance(plan, TradePlan):
            continue
        order = gateway.submit(plan, f"{release_id}:{symbol}:{idx}", submitted_at=decision_time)
        open_pos_risk[order.intent_id] = plan.estimated_loss

    return trades


def _bootstrap_ci95(values: list[float], resamples: int = 2000) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rng = random.Random(20261126)
    n = len(values)
    means = sorted(mean(rng.choices(values, k=n)) for _ in range(resamples))
    return (means[int(0.025 * resamples)], means[int(0.975 * resamples)])


def _run_variant(
    label: str,
    release_id: str,
    parameters: dict[str, Any],
    coin_cache: dict[str, tuple[tuple[OHLCVBar, ...], dict[str, tuple[float | None, ...]]]],
) -> VariantReport:
    all_trades: list[TradeRecord] = []
    fold_reports: list[FoldStats] = []
    for fname, fstart, fend in WF_FOLDS:
        ftrades: list[TradeRecord] = []
        for sym, (bars, ind) in coin_cache.items():
            ftrades.extend(
                _simulate_fold(
                    symbol=sym,
                    release_id=release_id,
                    parameters=parameters,
                    bars=bars,
                    indicators=ind,
                    fold_start=fstart,
                    fold_end=fend,
                )
            )
        fn = len(ftrades)
        fwins = sum(1 for t in ftrades if t.net_pnl > 0)
        fexp = mean(t.r_multiple for t in ftrades) if fn else 0.0
        fnet = float(sum((t.net_pnl for t in ftrades), Decimal("0")))
        fold_reports.append(
            FoldStats(
                fold=fname,
                trades=fn,
                win_rate_pct=(100.0 * fwins / fn) if fn else 0.0,
                expectancy_r=fexp,
                net_pnl=fnet,
            )
        )
        all_trades.extend(ftrades)

    n = len(all_trades)
    wins = sum(1 for t in all_trades if t.net_pnl > 0)
    exp_r = mean(t.r_multiple for t in all_trades) if n else 0.0
    ci_low, ci_high = _bootstrap_ci95([t.r_multiple for t in all_trades])
    net_pnl = float(sum((t.net_pnl for t in all_trades), Decimal("0")))
    total_cost = float(sum((t.total_cost for t in all_trades), Decimal("0")))
    pos_folds = sum(1 for f in fold_reports if f.expectancy_r > 0 and f.trades > 0)
    return VariantReport(
        label=label,
        release_id=release_id,
        parameters=parameters,
        active_coins=len(coin_cache),
        trades=n,
        wins=wins,
        win_rate_pct=(100.0 * wins / n) if n else 0.0,
        expectancy_r=exp_r,
        ci95_low_r=ci_low,
        ci95_high_r=ci_high,
        net_pnl=net_pnl,
        total_cost=total_cost,
        positive_folds=pos_folds,
        folds=fold_reports,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("/tmp/fx8_wf50_report.json"))
    args = parser.parse_args()

    coin_cache: dict[str, tuple[tuple[OHLCVBar, ...], dict[str, tuple[float | None, ...]]]] = {}
    skipped: list[str] = []
    for sym in SYMBOLS_50:
        bars = _load_real_1h_bars(sym)
        if len(bars) < 500:
            skipped.append(f"{sym}({len(bars)})")
            continue
        coin_cache[sym] = (bars, _compute_indicators(bars))

    print(f"Loaded {len(coin_cache)}/{len(SYMBOLS_50)} coins (skipped={skipped})")

    variants: list[tuple[str, str, dict[str, Any]]] = [
        ("h03-v3-1h (baseline)", "h03-v3-1h", {}),
        ("h03-v4-1h (default)", "h03-v4-1h", {}),
        ("h03-v4-1h donchian -20% (44)", "h03-v4-1h", {"donchian_lookback": 44}),
        ("h03-v4-1h donchian +20% (66)", "h03-v4-1h", {"donchian_lookback": 66}),
        ("h03-v4-1h atr_buf -20% (0.20)", "h03-v4-1h", {"breakout_atr_buffer": 0.20}),
        ("h03-v4-1h atr_buf +20% (0.30)", "h03-v4-1h", {"breakout_atr_buffer": 0.30}),
        ("h03-v4-1h vol_mult -20% (1.2)", "h03-v4-1h", {"volume_median_multiplier": 1.2}),
        ("h03-v4-1h vol_mult +20% (1.8)", "h03-v4-1h", {"volume_median_multiplier": 1.8}),
        ("h03-v4-1h min_adx -20% (16)", "h03-v4-1h", {"min_adx": 16.0}),
        ("h03-v4-1h min_adx +20% (24)", "h03-v4-1h", {"min_adx": 24.0}),
        ("h06-v3-1h (default)", "h06-v3-1h", {}),
        ("h06-v3-1h min_tp -20% (0.96)", "h06-v3-1h", {"min_tp_atr_multiple": 0.96}),
        ("h06-v3-1h min_tp +20% (1.44)", "h06-v3-1h", {"min_tp_atr_multiple": 1.44}),
    ]

    reports: list[VariantReport] = []
    print("\n| Variant | Coins | n | WinRate % | Exp R | CI95 R | Net PnL | Cost | +Folds/4 |")
    print("|---|---:|---:|---:|---:|---|---:|---:|---:|")
    for label, rel, params in variants:
        rep = _run_variant(label, rel, params, coin_cache)
        reports.append(rep)
        print(
            f"| {rep.label} | {rep.active_coins} | {rep.trades} | {rep.win_rate_pct:.1f}% | "
            f"{rep.expectancy_r:+.3f}R | [{rep.ci95_low_r:+.3f}, {rep.ci95_high_r:+.3f}] | "
            f"{rep.net_pnl:+.2f} | {rep.total_cost:.2f} | {rep.positive_folds}/4 |"
        )

    args.out.write_text(
        json.dumps([asdict(r) for r in reports], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
