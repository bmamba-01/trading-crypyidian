"""Script backtest đối chứng FX-8 (CRY-30) trên dữ liệu thật `crypidian-review-postgres-1`.

So sánh trực tiếp bản gốc vs FX-8 bằng chính `strategy_registry()`, `TradePlanner` và
`PaperExecutionGateway` (hồ sơ chi phí PAPER: fee 0.05%, slippage 0.02%, spread 0.01%):
- ``h01-v1`` vs ``h01-v2`` (1h + 4h)
- ``h03-v3-1h`` vs ``h03-v4-1h`` (1h)
- ``h03-v3-15m`` vs ``h03-v4-15m`` (15m)
- ``h06-v3-1h`` (1h)

Chạy:
    uv run python scripts/fx8_backtest_compare.py
"""

from __future__ import annotations

import csv
import io
import math
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import mean

from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.domain.execution.paper import (
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
    PaperPosition,
)
from trading_bot.domain.market.features import adx_dmi, atr, efficiency_ratio, ema, rsi, zscore
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    PlannerSettings,
    PlanRejected,
    RiskLimits,
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

SYMBOLS = ("BTCUSDT", "ETHUSDT", "AAVEUSDT", "ZECUSDT", "XMRUSDT")
SOURCE = "BINANCE_USDM_REST_BACKFILL"
EVAL_START = datetime(2026, 5, 22, 0, 0, tzinfo=UTC)
EVAL_END = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)
STARTING_EQUITY = Decimal("10000")
FEE_RATE = Decimal("0.0005")
SLIPPAGE_RATE = Decimal("0.0002")
SPREAD_RATE = Decimal("0.0001")

_INTERVALS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


@dataclass(frozen=True, slots=True)
class TradeRecord:
    symbol: str
    release_id: str
    direction: str
    net_pnl: Decimal
    total_cost: Decimal
    r_multiple: float
    exit_reason: str


@dataclass(frozen=True, slots=True)
class ReleaseSummary:
    release_id: str
    timeframe: str
    trades: int
    win_rate_pct: float
    expectancy_r: float
    net_pnl: Decimal
    total_cost: Decimal
    verdict: str


def _load_bars_from_postgres(symbol: str, timeframe: str) -> tuple[OHLCVBar, ...]:
    """Đọc chuỗi nến đã đóng từ container `crypidian-review-postgres-1` (nguồn backfill thật)."""
    query = (
        "SELECT open_time, close_time, open, high, low, close, volume, quote_volume "
        "FROM market_bar "
        f"WHERE symbol = '{symbol}' AND timeframe = '{timeframe}' "
        f"AND source = '{SOURCE}' AND is_closed = true "
        "ORDER BY open_time ASC"
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
    interval = _INTERVALS[timeframe]
    reader = csv.DictReader(io.StringIO(proc.stdout))
    bars: list[OHLCVBar] = []
    seen_open: set[datetime] = set()
    for idx, row in enumerate(reader):
        opened = datetime.fromisoformat(row["open_time"]).astimezone(UTC)
        if opened in seen_open:
            continue
        seen_open.add(opened)
        closed = opened + interval - timedelta(milliseconds=1)
        o_p = Decimal(row["open"])
        h_p = Decimal(row["high"])
        l_p = Decimal(row["low"])
        c_p = Decimal(row["close"])
        if h_p < max(o_p, c_p) or l_p > min(o_p, c_p) or l_p <= Decimal("0"):
            continue
        bars.append(
            OHLCVBar(
                symbol=symbol,
                timeframe=timeframe,
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
                source=SOURCE,
                source_cursor=f"{timeframe}:{idx}",
                checksum="0" * 64,
                raw_reference=f"pg:{symbol}:{timeframe}:{idx}",
            )
        )
    return tuple(bars)


def _trim_contiguous_tail(bars: Sequence[OHLCVBar], timeframe: str) -> tuple[OHLCVBar, ...]:
    """Lấy đoạn nến liên tục dài nhất chứa cửa sổ đánh giá (tránh gap hiếm làm fail `_inputs`)."""
    if not bars:
        return ()
    interval = _INTERVALS[timeframe]
    segments: list[list[OHLCVBar]] = [[bars[0]]]
    for bar in bars[1:]:
        if bar.open_time - segments[-1][-1].open_time == interval:
            segments[-1].append(bar)
        else:
            segments.append([bar])
    longest = max(segments, key=len)
    return tuple(longest)


def _compute_indicators(bars: Sequence[OHLCVBar]) -> dict[str, tuple[float | None, ...]]:
    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    _, _, _, adx_s = adx_dmi(highs, lows, closes, 14)
    return {
        "ema_20": ema(closes, 20),
        "ema_80": ema(closes, 80),
        "ema_200": ema(closes, 200),
        "atr_14": atr(highs, lows, closes, 14),
        "adx_14": adx_s,
        "efficiency_ratio_40": efficiency_ratio(closes, 40),
        "zscore_20": zscore(closes, 20),
        "rsi_14": rsi(closes, 14),
    }


def _planner_settings(release_id: str, release_hash: str) -> PlannerSettings:
    return PlannerSettings(
        workspace_id="fx8-backtest-ws",
        settings_version_id="fx8-backtest-settings-v1",
        risk_profile_version_id="fx8-backtest-risk-v1",
        published=True,
        active_release_ids=(release_id,),
        leverage=Decimal("3"),
        exchange=ExchangeRules(
            tick_size=Decimal("0.0001"),
            quantity_step=Decimal("0.0001"),
            contract_multiplier=Decimal("1"),
            min_notional=Decimal("5"),
            max_notional=Decimal("1000000"),
            max_quantity=Decimal("1000000"),
        ),
        limits=RiskLimits(
            per_trade_risk_fraction=Decimal("0.01"),
            max_aggregate_open_risk=Decimal("10000"),
            max_notional=Decimal("50000"),
            max_leverage=Decimal("5"),
            max_positions=1,
            max_symbol_notional=Decimal("50000"),
            max_concentration_fraction=Decimal("1.0"),
            max_daily_loss=Decimal("2000"),
            max_rolling_loss=Decimal("5000"),
            max_account_age=timedelta(hours=1),
            max_data_age=timedelta(hours=12),
        ),
        release_content_hashes=((release_id, release_hash),),
    )


def _simulate_symbol(
    *,
    symbol: str,
    release_id: str,
    timeframe: str,
    bars: tuple[OHLCVBar, ...],
    h4_bars: tuple[OHLCVBar, ...] | None = None,
) -> list[TradeRecord]:
    registry = strategy_registry()
    release = registry.get_release(release_id)
    selection = StrategySelection(
        settings_version_id="fx8-backtest-settings-v1",
        active_release_ids=(release_id,),
        published=True,
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
    policy = PaperExecutionPolicy(
        version_id="fx8-paper-policy-v1",
        latency=timedelta(seconds=0),
        spread_rate=SPREAD_RATE,
        slippage_rate=SLIPPAGE_RATE,
        fee_rate=FEE_RATE,
    )
    gateway = PaperExecutionGateway(policy)
    indicators = _compute_indicators(bars)
    h4_indicators = _compute_indicators(h4_bars) if h4_bars else None
    interval = _INTERVALS[timeframe]
    quote_volumes = tuple(float(b.quote_volume or Decimal("0")) for b in bars)

    equity = STARTING_EQUITY
    trades: list[TradeRecord] = []
    open_pos_risk: dict[str, Decimal] = {}
    last_stop_exit_at: datetime | None = None
    h4_ptr = -1
    window_bars = 140
    min_warmup = 205 if timeframe != "15m" else 205

    for idx in range(min_warmup, len(bars)):
        bar = bars[idx]
        if bar.open_time < EVAL_START or bar.open_time >= EVAL_END:
            continue

        # 1. Đưa nến hiện tại vào PaperExecutionGateway để khớp lệnh chờ & giám sát vị thế mở.
        paper_bar = PaperBar(
            bar_key=f"{symbol}|{timeframe}|{bar.open_time.isoformat()}",
            symbol=symbol,
            opened_at=bar.open_time,
            closed_at=bar.close_time,
            available_at=bar.close_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            bar_source=SOURCE,
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
            opposing = False
            if pos.recipe.opposite_signal_exit:
                f_curr = indicators["ema_20"][idx]
                s_curr = indicators["ema_80"][idx]
                if f_curr is not None and s_curr is not None:
                    opposing = (pos.direction == "long" and f_curr < s_curr) or (
                        pos.direction == "short" and f_curr > s_curr
                    )
            atr_v = indicators["atr_14"][idx]
            result = gateway.monitor_position(
                pos,
                paper_bar,
                opposing_signal=opposing,
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
                exit_reason = closed_pos.exit_reason or "UNKNOWN"
                equity += net_pnl
                if exit_reason in {"STOP_LOSS", "TRAILING_STOP"}:
                    last_stop_exit_at = bar.close_time
                trades.append(
                    TradeRecord(
                        symbol=symbol,
                        release_id=release_id,
                        direction=closed_pos.direction,
                        net_pnl=net_pnl,
                        total_cost=total_cost,
                        r_multiple=r_mult,
                        exit_reason=exit_reason,
                    )
                )

        if gateway.open_positions() or gateway.open_orders():
            continue

        # 2. Dựng DecisionSnapshot point-in-time tại thời điểm đóng nến.
        start_i = max(0, idx - window_bars + 1)
        slice_bars = bars[start_i : idx + 1]
        slice_vols = quote_volumes[start_i : idx + 1]
        series_list = [
            MarketSeries(
                timeframe=timeframe,
                bars=slice_bars,
                quote_volumes=slice_vols,
                quality=SeriesQuality.READY,
                expected_interval=interval,
                max_event_age=interval * 2,
                backfill_complete=True,
            )
        ]
        feat_list: list[FeatureObservation] = []
        feat_names = (
            "ema_20",
            "ema_80",
            "adx_14",
            "atr_14",
            "efficiency_ratio_40",
            "zscore_20",
            "rsi_14",
        )
        for fname in feat_names:
            val = indicators[fname][idx]
            prev = indicators[fname][idx - 1] if idx > 0 else None
            if val is not None and math.isfinite(val):
                feat_list.append(
                    FeatureObservation(
                        feature_id=f"{timeframe}:{fname}:{idx}",
                        name=fname,
                        timeframe=timeframe,
                        value=val,
                        previous_value=prev,
                        available_at=bar.close_time,
                        quality=FeatureQuality.READY,
                    )
                )

        if h4_bars is not None and h4_indicators is not None:
            while h4_ptr + 1 < len(h4_bars) and h4_bars[h4_ptr + 1].close_time <= bar.close_time:
                h4_ptr += 1
            if h4_ptr < 200:
                continue
            h4_b = h4_bars[h4_ptr]
            h4_ema200 = h4_indicators["ema_200"][h4_ptr]
            if h4_ema200 is None:
                continue
            series_list.append(
                MarketSeries(
                    timeframe="4h",
                    bars=(h4_b,),
                    quality=SeriesQuality.READY,
                    expected_interval=_INTERVALS["4h"],
                    max_event_age=_INTERVALS["4h"] * 2,
                    backfill_complete=True,
                )
            )
            feat_list.append(
                FeatureObservation(
                    feature_id=f"4h:ema_200:{h4_ptr}",
                    name="ema_200",
                    timeframe="4h",
                    value=h4_ema200,
                    previous_value=h4_indicators["ema_200"][h4_ptr - 1],
                    available_at=h4_b.close_time,
                    quality=FeatureQuality.READY,
                )
            )

        decision_time = bar.close_time + timedelta(microseconds=1)
        is_h06 = release_id.startswith("h06-")
        snapshot = DecisionSnapshot(
            symbol=symbol,
            decision_time=decision_time,
            snapshot_id=f"{release_id}:{symbol}:{idx}".ljust(64, "0")[:64],
            series=tuple(series_list),
            features=tuple(feat_list),
            position_is_flat=True,
            macro_blackout=False if is_h06 else None,
            macro_evidence_ids=("macro:backtest-clear",) if is_h06 else (),
            macro_evidence_available_at=bar.close_time if is_h06 else None,
            macro_is_fresh=True if is_h06 else None,
            last_stop_exit_at=last_stop_exit_at,
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
        plan = planner.plan(
            decision,
            planner_settings,
            account,
            (),
            cost_model,
            now=decision_time,
        )
        if isinstance(plan, PlanRejected) or not isinstance(plan, TradePlan):
            continue
        order = gateway.submit(
            plan,
            f"{release_id}:{symbol}:{idx}",
            submitted_at=decision_time,
        )
        open_pos_risk[order.intent_id] = plan.estimated_loss

    return trades


def _summarize(release_id: str, timeframe: str, trades: list[TradeRecord]) -> ReleaseSummary:
    n = len(trades)
    if n == 0:
        return ReleaseSummary(
            release_id=release_id,
            timeframe=timeframe,
            trades=0,
            win_rate_pct=0.0,
            expectancy_r=0.0,
            net_pnl=Decimal("0"),
            total_cost=Decimal("0"),
            verdict="EXPERIMENTAL_UNVALIDATED (n=0)",
        )
    wins = sum(1 for t in trades if t.net_pnl > 0)
    win_rate = 100.0 * wins / n
    exp_r = mean(t.r_multiple for t in trades)
    net_pnl = sum((t.net_pnl for t in trades), Decimal("0"))
    total_cost = sum((t.total_cost for t in trades), Decimal("0"))
    if n < 30:
        verdict = "EXPERIMENTAL_UNVALIDATED (chưa đủ mẫu n<30)"
    elif exp_r > 0 and net_pnl > 0:
        verdict = "EXPERIMENTAL_UNVALIDATED (expectancy dương, cần OOS/WF)"
    else:
        verdict = "EXPERIMENTAL_UNVALIDATED (expectancy <= 0)"
    return ReleaseSummary(
        release_id=release_id,
        timeframe=timeframe,
        trades=n,
        win_rate_pct=win_rate,
        expectancy_r=exp_r,
        net_pnl=net_pnl,
        total_cost=total_cost,
        verdict=verdict,
    )


def main() -> None:
    print(
        f"Đang nạp dữ liệu thật ({SOURCE}) cho {len(SYMBOLS)} coin: {', '.join(SYMBOLS)} "
        f"[{EVAL_START.date()} -> {EVAL_END.date()}]..."
    )
    data: dict[tuple[str, str], tuple[OHLCVBar, ...]] = {}
    for sym in SYMBOLS:
        for tf in ("15m", "1h", "4h"):
            raw = _load_bars_from_postgres(sym, tf)
            data[(sym, tf)] = _trim_contiguous_tail(raw, tf)

    scenarios = (
        ("h01-v1", "1h", True),
        ("h01-v2", "1h", True),
        ("h03-v3-1h", "1h", False),
        ("h03-v4-1h", "1h", False),
        ("h03-v3-15m", "15m", False),
        ("h03-v4-15m", "15m", False),
        ("h06-v3-1h", "1h", False),
    )

    header = (
        "\n| Release | TF | n | WinRate % | Expectancy (R) "
        "| Net PnL (USDT) | Total Cost | Verdict |"
    )
    print(header)
    print("|---|---|---:|---:|---:|---:|---:|---|")
    for release_id, tf, needs_h4 in scenarios:
        all_trades: list[TradeRecord] = []
        for sym in SYMBOLS:
            trades = _simulate_symbol(
                symbol=sym,
                release_id=release_id,
                timeframe=tf,
                bars=data[(sym, tf)],
                h4_bars=data[(sym, "4h")] if needs_h4 else None,
            )
            all_trades.extend(trades)
        summary = _summarize(release_id, tf, all_trades)
        print(
            f"| `{summary.release_id}` | {summary.timeframe} | {summary.trades} | "
            f"{summary.win_rate_pct:.1f}% | {summary.expectancy_r:+.3f}R | "
            f"{summary.net_pnl:+.2f} | {summary.total_cost:.2f} | {summary.verdict} |"
        )


if __name__ == "__main__":
    main()
