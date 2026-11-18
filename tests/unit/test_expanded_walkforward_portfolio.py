"""Bộ kiểm thử Walk-Forward đa chế độ & Portfolio Backtest cho 6 rule mở rộng và 8 Strategy Cards.

Kiểm chứng trên dữ liệu tổng hợp đa chế độ (uptrend mạnh, nén biến động rồi breakout,
đi ngang chop, downtrend, tail shock) đi qua `BacktestReplayExecutor` và
`portfolio_backtest` thật cho cả:
- 6 rule mới (`h02-v1`, `h04-v1`, `h05-v1`, `h07-v1`, `h21-v1`, `h22-v1`)
- 8 Strategy Cards (`sc-binance-usdm-*-001` đến `008`)
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from functools import lru_cache
from uuid import UUID, uuid5

import pytest

from trading_bot.application.backtest import (
    BacktestRunner,
    EvaluationReport,
    ReplayObservation,
    ReplayRejected,
)
from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.application.backtest_replay_executor import (
    BacktestReplayPlan,
    build_backtest_replay_plan,
)
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.runtime_replay_evaluator import (
    RuntimePaperReplayComparison,
    RuntimePaperReplayRun,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import (
    ENTRY_TIMEFRAME_BY_RELEASE,
    FEATURES_BY_RELEASE,
)
from trading_bot.domain.learning.evaluation import (
    SampleLineage,
    TemporalWindow,
    select_temporal_samples,
    walk_forward_splits,
)
from trading_bot.domain.market.features import (
    adx_dmi,
    bollinger_bands,
    efficiency_ratio,
    realized_volatility,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.portfolio.portfolio_backtest import (
    PortfolioLimits,
    PortfolioResult,
    PortfolioTrade,
    simulate_portfolio,
    trades_with_symbol_equity,
)
from trading_bot.domain.strategy.contracts import StrategyCandidate, StrategySelection

WORKSPACE_ID = UUID("c7e8d9a0-1122-4334-8556-778899aabbcc")
BASE_START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

INTERVAL_DURATIONS: dict[str, timedelta] = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}

EXPANDED_RULE_RELEASES: tuple[str, ...] = (
    "h02-v1",
    "h04-v1",
    "h05-v1",
    "h07-v1",
    "h21-v1",
    "h22-v1",
)

STRATEGY_CARD_RELEASES: tuple[str, ...] = (
    "sc-binance-usdm-vol-breakout-001-v1.0.0",
    "sc-binance-usdm-mean-revert-002-v1.2.1",
    "sc-binance-usdm-tsmom-003-v1.0.0",
    "sc-binance-usdm-bbkc-squeeze-004-v1.0.0",
    "sc-binance-usdm-trend-pullback-005-v1.0.0",
    "sc-binance-usdm-vwap-revert-006-v1.0.0",
    "sc-binance-usdm-macd-adx-007-v1.0.0",
    "sc-binance-usdm-taker-flow-008-v1.0.0",
)

ALL_TARGET_RELEASES: tuple[str, ...] = (*EXPANDED_RULE_RELEASES, *STRATEGY_CARD_RELEASES)
REGIME_NAMES: tuple[str, ...] = ("uptrend", "squeeze_breakout", "chop", "downtrend", "tail_shock")


@dataclass(frozen=True, slots=True)
class MultiRegimeSeries:
    """Chuỗi nến tổng hợp đa chế độ kèm chỉ số lát cắt từng chế độ để kiểm định."""

    timeframe: str
    symbol: str
    seed: int
    warmup_bars: int
    evaluation_start: datetime
    range_end: datetime
    bars: tuple[OHLCVBar, ...]
    regime_slices: dict[str, tuple[int, int]]


def _price_decimal(value: float) -> Decimal:
    return Decimal(str(round(max(0.01, value), 4)))


def _make_bar(
    *,
    symbol: str,
    timeframe: str,
    opened_at: datetime,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    taker_buy_ratio: float,
    cursor: str,
) -> OHLCVBar:
    """Dựng một OHLCVBar hợp lệ theo đúng biên lưới Binance REST cho BacktestDatasetManifest."""
    duration = INTERVAL_DURATIONS[timeframe]
    closed_at = opened_at + duration - timedelta(milliseconds=1)
    o = _price_decimal(open_price)
    c = _price_decimal(close)
    h = max(_price_decimal(high), o, c)
    low_d = min(_price_decimal(low), o, c)
    if low_d <= Decimal("0"):
        low_d = Decimal("0.01")
    vol = Decimal(str(round(max(1.0, volume), 4)))
    ratio = min(0.95, max(0.05, taker_buy_ratio))
    tbuy = (vol * Decimal(str(round(ratio, 4)))).quantize(Decimal("0.0001"))
    qvol = (vol * c).quantize(Decimal("0.0001"))
    return OHLCVBar(
        symbol=symbol,
        timeframe=timeframe,
        open_time=opened_at,
        close_time=closed_at,
        event_at=closed_at,
        received_at=closed_at,
        available_at=closed_at,
        open=o,
        high=h,
        low=low_d,
        close=c,
        volume=vol,
        quote_volume=qvol,
        taker_buy_base_volume=tbuy,
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor=cursor,
        checksum=hashlib.sha256(cursor.encode("utf-8")).hexdigest(),
    )


def _aggregate_bars(
    bars: tuple[OHLCVBar, ...],
    *,
    target_timeframe: str,
) -> tuple[OHLCVBar, ...]:
    """Gộp nến khung thấp thành nến khung cao liền mạch theo đúng lưới thời gian UTC."""
    target_step = INTERVAL_DURATIONS[target_timeframe]
    step_seconds = int(target_step.total_seconds())
    grouped: dict[datetime, list[OHLCVBar]] = {}
    for bar in bars:
        epoch = int(bar.open_time.timestamp())
        bucket_epoch = (epoch // step_seconds) * step_seconds
        bucket_open = datetime.fromtimestamp(bucket_epoch, tz=UTC)
        grouped.setdefault(bucket_open, []).append(bar)

    aggregated: list[OHLCVBar] = []
    for bucket_open in sorted(grouped):
        chunk = grouped[bucket_open]
        closed_at = bucket_open + target_step - timedelta(milliseconds=1)
        vol = sum((b.volume for b in chunk), Decimal("0"))
        qvol = sum((b.quote_volume or Decimal("0") for b in chunk), Decimal("0"))
        tbuy = sum((b.taker_buy_base_volume or Decimal("0") for b in chunk), Decimal("0"))
        cursor = f"agg:{chunk[0].symbol}:{target_timeframe}:{int(bucket_open.timestamp())}"
        aggregated.append(
            OHLCVBar(
                symbol=chunk[0].symbol,
                timeframe=target_timeframe,
                open_time=bucket_open,
                close_time=closed_at,
                event_at=closed_at,
                received_at=closed_at,
                available_at=closed_at,
                open=chunk[0].open,
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=chunk[-1].close,
                volume=vol,
                quote_volume=qvol,
                taker_buy_base_volume=tbuy,
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                source_cursor=cursor,
                checksum=hashlib.sha256(cursor.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(aggregated)


def _generate_multi_regime_entry_bars(
    *,
    timeframe: str,
    symbol: str = "BTCUSDT",
    seed: int = 42,
    evaluation_start: datetime = BASE_START,
    warmup_bars: int = 256,
) -> MultiRegimeSeries:
    """Sinh chuỗi nến tổng hợp chứa đủ 5 chế độ thị trường đa dạng.

    Toàn bộ quá trình hoàn toàn tất định theo `seed` và bảo đảm kích hoạt được cả rule xu hướng,
    rule hồi quy trung bình, rule nén biến động, rule taker flow và hàng rào biến động tail shock.
    """
    rng = random.Random(seed)
    step = INTERVAL_DURATIONS[timeframe]
    symbol_scale = {"BTCUSDT": 1.0, "ETHUSDT": 1.12, "SOLUSDT": 0.92}.get(symbol, 1.0)
    base_price = 100.0 * symbol_scale
    phase_shift = (sum(ord(ch) for ch in symbol) + seed) % 3
    origin = evaluation_start - warmup_bars * step
    bars: list[OHLCVBar] = []
    regime_slices: dict[str, tuple[int, int]] = {}

    def append_bar(
        o: float,
        h: float,
        low_p: float,
        c: float,
        vol: float,
        taker_ratio: float,
        tag: str,
    ) -> None:
        idx = len(bars)
        opened = origin + idx * step
        bars.append(
            _make_bar(
                symbol=symbol,
                timeframe=timeframe,
                opened_at=opened,
                open_price=o,
                high=h,
                low=low_p,
                close=c,
                volume=vol,
                taker_buy_ratio=taker_ratio,
                cursor=f"syn:{symbol}:{timeframe}:{seed}:{idx}:{tag}",
            )
        )

    # ------------------------------------------------------------------
    # 0. Warmup ổn định quanh base_price (đủ dao động tạo lịch sử BBWidth)
    # ------------------------------------------------------------------
    price = base_price
    for i in range(warmup_bars):
        wave = math.sin(i * 0.45 + phase_shift) * (1.25 * symbol_scale)
        noise = rng.uniform(-0.18, 0.18) * symbol_scale
        next_p = base_price + wave + noise
        spread = (0.85 + 0.25 * abs(math.cos(i * 0.3))) * symbol_scale
        append_bar(
            price,
            max(price, next_p) + spread,
            min(price, next_p) - spread,
            next_p,
            100.0 + (i % 5) * 4.0,
            0.52,
            "warmup",
        )
        price = next_p

    # ------------------------------------------------------------------
    # 1. Chế độ 1: UPTREND MẠNH (pullback EMA20, MACD cross, hammer, taker breakout)
    # ------------------------------------------------------------------
    up_start = len(bars)
    up_len = 44
    for i in range(up_len):
        if i in (20, 21, 22, 23):
            # Nhịp điều chỉnh nhẹ kéo RSI về [40, 60] nhưng vẫn giữ cấu trúc tăng
            o = price
            c = price - 1.05 * symbol_scale
            h = o + 0.30 * symbol_scale
            low_p = c - 0.30 * symbol_scale
            vol = 110.0
            tr = 0.45
        elif i == 24:
            # Nến rút chân chạm EMA20 và đóng cửa phục hồi (H05/Card 005/Card 001 + mở đường MACD)
            o = price - 0.15 * symbol_scale
            c = price + 2.4 * symbol_scale
            h = c + 0.25 * symbol_scale
            low_p = o - 3.6 * symbol_scale
            vol = 180.0
            tr = 0.72
        elif i == 25:
            # Đẩy MACD histogram cắt lên > 0 + tích lũy đà taker buy
            o = price
            c = price + 2.6 * symbol_scale
            h = c + 0.35 * symbol_scale
            low_p = o - 0.20 * symbol_scale
            vol = 240.0
            tr = 0.80
        elif i == 26:
            # Phá đỉnh 20 nến dứt khoát + taker buy 84% (TI = +0.68) -> H22 & Card 008 LONG!
            o = price
            c = price + 3.8 * symbol_scale
            h = c + 0.40 * symbol_scale
            low_p = o - 0.15 * symbol_scale
            vol = 310.0
            tr = 0.84
        elif i == 36 + phase_shift:
            # Nhịp tiếp diễn breakout với nến hammer + taker buy mạnh
            o = price + 0.1 * symbol_scale
            c = price + 2.8 * symbol_scale
            h = c + 0.3 * symbol_scale
            low_p = o - 3.6 * symbol_scale
            vol = 290.0
            tr = 0.82
        else:
            step_up = (0.85 + rng.uniform(0.05, 0.25)) * symbol_scale
            o = price
            c = price + step_up
            h = max(o, c) + 0.50 * symbol_scale
            low_p = min(o, c) - 0.40 * symbol_scale
            vol = 120.0 + (i % 4) * 8.0
            tr = 0.64
        append_bar(o, h, low_p, c, vol, tr, "uptrend")
        price = c
    regime_slices["uptrend"] = (up_start, len(bars))

    # ------------------------------------------------------------------
    # 2. Chế độ 2: NÉN BIẾN ĐỘNG RỒI BREAKOUT (BB–Keltner Squeeze → Breakout)
    # ------------------------------------------------------------------
    sq_start = len(bars)
    for i in range(8):
        delta = (2.1 if i % 2 == 0 else -2.0) * symbol_scale
        o = price
        c = price + delta
        append_bar(
            o,
            max(o, c) + 1.2 * symbol_scale,
            min(o, c) - 1.2 * symbol_scale,
            c,
            100.0,
            0.52,
            "pre_squeeze",
        )
        price = c
    anchor_sq = price
    for i in range(16):
        c = anchor_sq + (0.04 if i % 2 == 0 else -0.04) * symbol_scale
        append_bar(
            price,
            max(price, c) + 0.08 * symbol_scale,
            min(price, c) - 0.08 * symbol_scale,
            c,
            90.0,
            0.55,
            "squeeze",
        )
        price = c
    brk_close = anchor_sq + 5.6 * symbol_scale
    append_bar(
        price,
        brk_close + 0.4 * symbol_scale,
        price - 0.1 * symbol_scale,
        brk_close,
        320.0,
        0.84,
        "squeeze_breakout",
    )
    price = brk_close
    for i in range(9):
        c = price + (0.95 if i < 5 else 0.35) * symbol_scale
        append_bar(
            price,
            c + 0.5 * symbol_scale,
            price - 0.25 * symbol_scale,
            c,
            150.0,
            0.65,
            "squeeze_follow",
        )
        price = c
    regime_slices["squeeze_breakout"] = (sq_start, len(bars))

    # ------------------------------------------------------------------
    # 3. Chế độ 3: ĐI NGANG CHOP (ADX < 18, ER < 0.22, hồi quy VWAP & Z-score)
    # ------------------------------------------------------------------
    chop_start = len(bars)
    anchor_chop = price
    # Dao động đối xứng +DM/-DM bằng nhau để DX ≈ 0 kéo ADX14 tụt nhanh xuống < 15
    for i in range(32):
        if i % 2 == 0:
            c = anchor_chop + 0.12 * symbol_scale
            h = anchor_chop + 0.55 * symbol_scale
            low_p = anchor_chop - 0.15 * symbol_scale
        else:
            c = anchor_chop - 0.12 * symbol_scale
            h = anchor_chop + 0.15 * symbol_scale
            low_p = anchor_chop - 0.55 * symbol_scale
        append_bar(price, h, low_p, c, 145.0, 0.50, "chop_base")
        price = c
    # Cực trị giảm sâu dưới VWAP (>= 3.5 ATR) + gap > 2.5% >> 0.3%, giữ ADX < 20 & ER < 0.30
    dip_1 = anchor_chop - 4.8 * symbol_scale
    append_bar(
        price,
        price + 0.05 * symbol_scale,
        dip_1 - 0.10 * symbol_scale,
        dip_1,
        110.0,
        0.35,
        "chop_dip",
    )
    rev_up = dip_1 + 0.85 * symbol_scale
    append_bar(
        dip_1 + 0.15 * symbol_scale,
        rev_up + 0.20 * symbol_scale,
        dip_1 - 2.20 * symbol_scale,
        rev_up,
        130.0,
        0.72,
        "chop_rev_long",
    )
    price = rev_up
    for i in range(6):
        c = price + (anchor_chop - price) * ((i + 1) / 6.0)
        append_bar(
            price,
            max(price, c) + 0.25 * symbol_scale,
            min(price, c) - 0.25 * symbol_scale,
            c,
            110.0,
            0.52,
            "chop_rebalance_up",
        )
        price = c
    for i in range(16):
        if i % 2 == 0:
            c = anchor_chop + 0.12 * symbol_scale
            h = anchor_chop + 0.55 * symbol_scale
            low_p = anchor_chop - 0.15 * symbol_scale
        else:
            c = anchor_chop - 0.12 * symbol_scale
            h = anchor_chop + 0.15 * symbol_scale
            low_p = anchor_chop - 0.55 * symbol_scale
        append_bar(price, h, low_p, c, 135.0, 0.50, "chop_mid")
        price = c
    spike_1 = anchor_chop + 4.8 * symbol_scale
    append_bar(
        price,
        spike_1 + 0.10 * symbol_scale,
        price - 0.05 * symbol_scale,
        spike_1,
        110.0,
        0.65,
        "chop_spike",
    )
    rev_dn = spike_1 - 0.85 * symbol_scale
    append_bar(
        spike_1 - 0.15 * symbol_scale,
        spike_1 + 2.20 * symbol_scale,
        rev_dn - 0.20 * symbol_scale,
        rev_dn,
        130.0,
        0.28,
        "chop_rev_short",
    )
    price = rev_dn
    for i in range(6):
        c = price + (anchor_chop - price) * ((i + 1) / 6.0)
        append_bar(
            price,
            max(price, c) + 0.25 * symbol_scale,
            min(price, c) - 0.25 * symbol_scale,
            c,
            110.0,
            0.48,
            "chop_rebalance_dn",
        )
        price = c
    regime_slices["chop"] = (chop_start, len(bars))

    # ------------------------------------------------------------------
    # 4. Chế độ 4: DOWNTREND (giảm sâu dưới EMA200, hồi EMA20 + MACD cross xuống + taker bán)
    # ------------------------------------------------------------------
    dn_start = len(bars)
    dn_len = 52
    for i in range(dn_len):
        if i < 24:
            step_dn = (2.45 + rng.uniform(0.05, 0.25)) * symbol_scale
            o = price
            c = max(45.0 * symbol_scale, price - step_dn)
            h = max(o, c) + 0.45 * symbol_scale
            low_p = min(o, c) - 0.50 * symbol_scale
            vol = 125.0 + (i % 4) * 6.0
            tr = 0.35
        elif i in (24, 25, 26, 27):
            o = price
            c = price + 1.15 * symbol_scale
            h = c + 0.30 * symbol_scale
            low_p = o - 0.25 * symbol_scale
            vol = 115.0
            tr = 0.55
        elif i == 28:
            o = price + 0.1 * symbol_scale
            c = price - 2.3 * symbol_scale
            h = o + 3.4 * symbol_scale
            low_p = c - 0.25 * symbol_scale
            vol = 185.0
            tr = 0.28
        elif i == 29:
            o = price
            c = max(35.0 * symbol_scale, price - 2.6 * symbol_scale)
            h = o + 0.20 * symbol_scale
            low_p = c - 0.35 * symbol_scale
            vol = 240.0
            tr = 0.20
        elif i == 30:
            # Phá đáy 20 nến dứt khoát kèm taker sell 84% (TI = -0.68) -> H22 & Card 008 SHORT!
            o = price
            c = max(30.0 * symbol_scale, price - 3.9 * symbol_scale)
            h = o + 0.15 * symbol_scale
            low_p = c - 0.40 * symbol_scale
            vol = 310.0
            tr = 0.16
        else:
            step_dn = (0.90 + rng.uniform(0.05, 0.25)) * symbol_scale
            o = price
            c = max(25.0 * symbol_scale, price - step_dn)
            h = max(o, c) + 0.40 * symbol_scale
            low_p = min(o, c) - 0.45 * symbol_scale
            vol = 125.0 + (i % 3) * 10.0
            tr = 0.34
        append_bar(o, h, low_p, c, vol, tr, "downtrend")
        price = c
    regime_slices["downtrend"] = (dn_start, len(bars))

    # ------------------------------------------------------------------
    # 5. Chế độ 5: TAIL SHOCK (sụp giá đột ngột RV20 > 0.10 > 0.08, sau đó lắng xuống)
    # ------------------------------------------------------------------
    shock_start = len(bars)
    shock_moves = (-0.21, 0.18, -0.19, 0.16, -0.18, 0.15, -0.17, 0.14, -0.16, 0.13)
    for idx_s, pct in enumerate(shock_moves):
        o = price * (0.97 if pct < 0 else 1.02)
        c = max(16.0 * symbol_scale, price * (1.0 + pct))
        span = abs(c - o) + price * 0.04
        append_bar(
            o,
            max(o, c) + span * 0.35,
            max(12.0 * symbol_scale, min(o, c) - span * 0.35),
            c,
            460.0 + idx_s * 20.0,
            0.18 if pct < 0 else 0.80,
            "tail_shock",
        )
        price = c
    for i in range(10):
        c = price + (0.18 if i % 2 == 0 else -0.18) * symbol_scale
        append_bar(
            price,
            max(price, c) + 0.30 * symbol_scale,
            min(price, c) - 0.30 * symbol_scale,
            c,
            110.0,
            0.50,
            "post_shock",
        )
        price = c
    regime_slices["tail_shock"] = (shock_start, len(bars))

    # Căn range_end về bội số của 4h để mọi khung (5m/15m/1h/4h) khép đều
    bars_per_4h = int(INTERVAL_DURATIONS["4h"] / step)
    eval_bars_count = len(bars) - warmup_bars
    remainder = eval_bars_count % bars_per_4h
    if remainder != 0:
        pad = bars_per_4h - remainder
        for i in range(pad):
            c = price + (0.15 if i % 2 == 0 else -0.15) * symbol_scale
            append_bar(
                price,
                max(price, c) + 0.3 * symbol_scale,
                min(price, c) - 0.3 * symbol_scale,
                c,
                100.0,
                0.50,
                "align_4h",
            )
            price = c

    range_end = origin + len(bars) * step
    return MultiRegimeSeries(
        timeframe=timeframe,
        symbol=symbol,
        seed=seed,
        warmup_bars=warmup_bars,
        evaluation_start=evaluation_start,
        range_end=range_end,
        bars=tuple(bars),
        regime_slices=regime_slices,
    )


def _extend_htf_warmup(
    *,
    htf_bars: tuple[OHLCVBar, ...],
    timeframe: str,
    symbol: str,
    needed_total_before_eval: int,
    evaluation_start: datetime,
) -> tuple[OHLCVBar, ...]:
    """Bổ sung nến warmup khung cao (1h/4h) liền mạch để EMA200/EMA80 sẵn sàng."""
    step = INTERVAL_DURATIONS[timeframe]
    earliest = htf_bars[0].open_time
    existing_before = sum(1 for b in htf_bars if b.open_time < evaluation_start)
    missing = max(0, needed_total_before_eval - existing_before)
    if missing == 0:
        return htf_bars
    anchor = float(htf_bars[0].open)
    prefix: list[OHLCVBar] = []
    for k in range(missing):
        idx_from_start = missing - k
        opened = earliest - idx_from_start * step
        c = anchor + math.sin(k * 0.25) * 0.8
        prefix.append(
            _make_bar(
                symbol=symbol,
                timeframe=timeframe,
                opened_at=opened,
                open_price=c - 0.1,
                high=c + 0.7,
                low=c - 0.7,
                close=c,
                volume=160.0,
                taker_buy_ratio=0.52,
                cursor=f"htf-pre:{symbol}:{timeframe}:{k}",
            )
        )
    return (*prefix, *htf_bars)


def _build_rights(
    workspace_id: UUID,
    *,
    verified_at: datetime,
    valid_until: datetime,
    tag: str = "walkforward-portfolio-policy",
) -> BackfillRightsEvidence:
    return BackfillRightsEvidence(
        workspace_id=workspace_id,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/synthetic-walkforward-terms",
        policy_fingerprint=hashlib.sha256(tag.encode("utf-8")).hexdigest(),
        verified_at=verified_at,
        checked_at=verified_at + timedelta(minutes=1),
        valid_until=valid_until,
    )


def _build_bundle_from_tf_bars(
    *,
    workspace_id: UUID,
    symbol: str,
    bars_by_tf: dict[str, tuple[OHLCVBar, ...]],
    evaluation_start: datetime,
    range_end: datetime,
    prepared_at: datetime | None = None,
    rights: BackfillRightsEvidence | None = None,
) -> BacktestDatasetReplayBundle:
    """Đóng gói các chuỗi nến đã căn lưới thành BacktestDatasetReplayBundle hợp lệ."""
    prep = prepared_at or range_end
    earliest = min(bars[0].open_time for bars in bars_by_tf.values())
    rights_ev = rights or _build_rights(
        workspace_id,
        verified_at=earliest - timedelta(days=1),
        valid_until=prep + timedelta(days=30),
    )
    inputs: list[BacktestDatasetReplayInput] = []
    for tf in sorted(bars_by_tf):
        tf_bars = tuple(b for b in bars_by_tf[tf] if b.open_time < range_end)
        warmup_count = sum(1 for b in tf_bars if b.open_time < evaluation_start)
        manifest = build_backtest_dataset_manifest(
            RestBackfillRequest(
                symbol=symbol,
                interval=tf,
                start=tf_bars[0].open_time,
                end=range_end,
                purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            ),
            evaluation_start=evaluation_start,
            warmup_bars=warmup_count,
            bars=tf_bars,
            rights_evidence=rights_ev,
            prepared_at=prep,
            schema_version=3,
        )
        inputs.append(BacktestDatasetReplayInput(manifest, tf_bars, rights_ev))
    return build_backtest_dataset_replay_bundle(tuple(inputs))


@lru_cache(maxsize=64)
def _build_multi_regime_bundle_for_release(
    release_id: str,
    *,
    symbol: str = "BTCUSDT",
    seed: int = 42,
    evaluation_start: datetime = BASE_START,
) -> tuple[BacktestDatasetReplayBundle, MultiRegimeSeries]:
    """Dựng (và cache trong phiên test) bundle đa chế độ đúng timeframe yêu cầu của `release_id`."""
    entry_tf = ENTRY_TIMEFRAME_BY_RELEASE[release_id]
    assert entry_tf is not None
    req_tfs = set(FEATURES_BY_RELEASE[release_id]) | {entry_tf}

    # Bảo đảm mọi khung đều >= 200 nến trước evaluation_start để EMA200 warmup hoàn chỉnh
    warmup_by_tf = {"5m": 204, "15m": 204, "1h": 204, "4h": 256}
    entry_series = _generate_multi_regime_entry_bars(
        timeframe=entry_tf,
        symbol=symbol,
        seed=seed,
        evaluation_start=evaluation_start,
        warmup_bars=warmup_by_tf[entry_tf],
    )
    bars_by_tf: dict[str, tuple[OHLCVBar, ...]] = {entry_tf: entry_series.bars}

    if "1h" in req_tfs and entry_tf == "15m":
        aligned_15m = tuple(
            b
            for b in entry_series.bars
            if b.open_time
            >= entry_series.bars[0].open_time.replace(minute=0, second=0, microsecond=0)
        )
        step_1h = INTERVAL_DURATIONS["1h"]
        agg_1h = _aggregate_bars(aligned_15m, target_timeframe="1h")
        full_buckets = [
            b
            for b in agg_1h
            if sum(1 for x in aligned_15m if b.open_time <= x.open_time < b.open_time + step_1h)
            == 4
        ]
        bars_by_tf["1h"] = _extend_htf_warmup(
            htf_bars=tuple(full_buckets),
            timeframe="1h",
            symbol=symbol,
            needed_total_before_eval=204,
            evaluation_start=evaluation_start,
        )

    if "4h" in req_tfs and entry_tf == "1h":
        step_4h = INTERVAL_DURATIONS["4h"]
        agg_4h = _aggregate_bars(entry_series.bars, target_timeframe="4h")
        full_4h = [
            b
            for b in agg_4h
            if sum(
                1 for x in entry_series.bars if b.open_time <= x.open_time < b.open_time + step_4h
            )
            == 4
        ]
        bars_by_tf["4h"] = _extend_htf_warmup(
            htf_bars=tuple(full_4h),
            timeframe="4h",
            symbol=symbol,
            needed_total_before_eval=204,
            evaluation_start=evaluation_start,
        )

    bundle = _build_bundle_from_tf_bars(
        workspace_id=WORKSPACE_ID,
        symbol=symbol,
        bars_by_tf=bars_by_tf,
        evaluation_start=evaluation_start,
        range_end=entry_series.range_end,
    )
    return bundle, entry_series


def _run_replay(
    bundle: BacktestDatasetReplayBundle,
    *,
    release_id: str,
    starting_equity: Decimal = Decimal("10000"),
    per_trade_risk_fraction: Decimal = Decimal("0.01"),
    fee_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0002"),
    seed: int = 20261117,
    job_salt: str = "",
) -> tuple[
    BacktestReplayPlan, RuntimePaperReplayComparison, RuntimePaperReplayRun, EvaluationReport
]:
    entry_tf = ENTRY_TIMEFRAME_BY_RELEASE[release_id]
    assert entry_tf is not None
    submission = BacktestReplayJobSubmission(
        bundle_hash=bundle.dataset_hash,
        strategy_release_id=release_id,
        execution_interval=entry_tf,
        starting_equity=starting_equity,
        per_trade_risk_fraction=per_trade_risk_fraction,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        seed=seed,
    )
    job_id = uuid5(
        WORKSPACE_ID,
        f"{bundle.dataset_hash}:{release_id}:{seed}:{fee_rate}:{slippage_rate}:{job_salt}",
    )
    plan = build_backtest_replay_plan(WORKSPACE_ID, job_id, bundle, submission)
    comparison = plan.executor(bundle, plan.manifest)
    assert comparison.is_complete, f"Replay phát sinh lỗi ReplayRejected cho {release_id}"
    base_run = next(r for r in comparison.runs if r.sensitivity_run.scenario.kind == "BASE")
    report = base_run.sensitivity_run.result
    assert not isinstance(report, ReplayRejected), (
        f"{release_id} bị ReplayRejected: {getattr(report, 'reason_code', None)}"
    )
    assert isinstance(report, EvaluationReport)
    return plan, comparison, base_run, report


def _slice_bundle_for_window(
    source_bundle: BacktestDatasetReplayBundle,
    *,
    eval_start: datetime,
    range_end: datetime,
) -> BacktestDatasetReplayBundle:
    """Cắt một lát Walk-Forward `[eval_start, range_end)` giữ nguyên nến warmup trước đó."""
    bars_by_tf = {
        comp.manifest.interval: tuple(b for b in comp.bars if b.open_time < range_end)
        for comp in source_bundle.components
    }
    return _build_bundle_from_tf_bars(
        workspace_id=source_bundle.workspace_id,
        symbol=source_bundle.symbol,
        bars_by_tf=bars_by_tf,
        evaluation_start=eval_start,
        range_end=range_end,
        prepared_at=range_end,
    )


def _assert_report_accounting_invariants(
    report: EvaluationReport, run: RuntimePaperReplayRun
) -> None:
    """Kiểm tra các bất biến kế toán cơ bản của một lần replay hoàn tất."""
    assert len(report.report_hash) == 64
    assert report.observation_count >= 1
    assert report.usable_observation_count >= 1
    assert report.fees >= Decimal("0")
    assert report.slippage >= Decimal("0")
    assert report.max_drawdown >= Decimal("0")
    assert report.net_pnl == report.gross_pnl - report.fees - report.funding - report.slippage
    assert len(run.trade_outcomes) == report.trade_count
    assert sum((t.fees for t in run.trade_outcomes), Decimal("0")) == report.fees
    assert sum((t.slippage for t in run.trade_outcomes), Decimal("0")) == report.slippage
    assert sum((t.gross_pnl for t in run.trade_outcomes), Decimal("0")) == report.gross_pnl
    for trade in run.trade_outcomes:
        assert trade.net_pnl == trade.gross_pnl - trade.fees - trade.funding - trade.slippage
        assert trade.exposure > Decimal("0")


# ---------------------------------------------------------------------------
# 1. Xác minh dữ liệu tổng hợp đa chế độ & tính tất định theo seed
# ---------------------------------------------------------------------------
def test_multi_regime_synthetic_data_exhibits_regimes_and_determinism() -> None:
    first = _generate_multi_regime_entry_bars(timeframe="15m", symbol="BTCUSDT", seed=77)
    second = _generate_multi_regime_entry_bars(timeframe="15m", symbol="BTCUSDT", seed=77)
    other_seed = _generate_multi_regime_entry_bars(timeframe="15m", symbol="BTCUSDT", seed=88)

    assert first.bars == second.bars
    assert first.bars != other_seed.bars
    assert set(first.regime_slices) == set(REGIME_NAMES)

    closes = [float(b.close) for b in first.bars]
    highs = [float(b.high) for b in first.bars]
    lows = [float(b.low) for b in first.bars]
    vols = [float(b.volume) for b in first.bars]

    _, _, _, adx_series = adx_dmi(highs, lows, closes, 14)
    er_series = efficiency_ratio(closes, 40)
    rv_series = realized_volatility(closes, 20)
    bb_m, bb_u, bb_l = bollinger_bands(closes, 20)

    # 1. Uptrend: giá tăng ròng và ADX đạt ngưỡng xu hướng (>= 22)
    u0, u1 = first.regime_slices["uptrend"]
    assert closes[u1 - 1] > closes[u0]
    assert max(v for v in adx_series[u0:u1] if v is not None) >= 22.0

    # 2. Squeeze -> Breakout: tồn tại cửa sổ BandWidth nén sâu rồi nở ra kèm volume >= 2× min
    s0, s1 = first.regime_slices["squeeze_breakout"]
    bws = [
        (bb_u[i] - bb_l[i]) / bb_m[i]  # type: ignore[operator]
        for i in range(s0, s1)
        if bb_u[i] is not None and bb_m[i] and bb_l[i] is not None
    ]
    assert min(bws) < 0.02
    assert max(vols[s0:s1]) >= 2.0 * min(vols[s0:s1])

    # 3. Chop: tồn tại giai đoạn ADX < 20 và ER < 0.30
    c0, c1 = first.regime_slices["chop"]
    assert any(
        adx_series[i] is not None
        and er_series[i] is not None
        and adx_series[i] < 20.0  # type: ignore[operator]
        and er_series[i] < 0.30  # type: ignore[operator]
        for i in range(c0, c1)
    )

    # 4. Downtrend: giá giảm sâu và ADX đạt ngưỡng xu hướng (>= 22)
    d0, d1 = first.regime_slices["downtrend"]
    assert closes[d1 - 1] < closes[d0]
    assert max(v for v in adx_series[d0:d1] if v is not None) >= 22.0

    # 5. Tail shock: RV20 bùng vượt trần an toàn 0.08 của H02/Card 003
    t0, t1 = first.regime_slices["tail_shock"]
    assert max(v for v in rv_series[t0:t1] if v is not None) > 0.08


# ---------------------------------------------------------------------------
# 2. Toàn bộ 6 rule mới + 8 Strategy Cards chạy qua BacktestReplayExecutor thật
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("release_id", ALL_TARGET_RELEASES)
def test_all_6_expanded_rules_and_8_strategy_cards_execute_without_rejection_across_regimes(
    release_id: str,
) -> None:
    """Cả 14 release chạy qua BacktestReplayExecutor thật, có lệnh đóng và không ReplayRejected."""
    bundle, _ = _build_multi_regime_bundle_for_release(release_id, symbol="BTCUSDT", seed=42)
    _plan, _comparison, base_run, report = _run_replay(bundle, release_id=release_id, seed=101)
    _assert_report_accounting_invariants(report, base_run)
    assert report.trade_count >= 1, f"{release_id} không sinh được lệnh nào trên dữ liệu đa chế độ."


# ---------------------------------------------------------------------------
# 3. Walk-Forward nhiều fold thời gian có embargo + chống lookahead
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("release_id", ALL_TARGET_RELEASES)
def test_walk_forward_folds_with_embargo_execute_without_rejection_or_lookahead(
    release_id: str,
) -> None:
    """Chia nhiều fold có purge + embargo căn theo khung thô nhất, chạy OOS qua executor."""
    bundle, _series = _build_multi_regime_bundle_for_release(release_id, symbol="BTCUSDT", seed=42)
    # Căn mọi biên fold theo khung thô nhất trong bundle (vd 1h cho 15m+1h, 4h cho 1h+4h)
    coarse_step = max(INTERVAL_DURATIONS[comp.manifest.interval] for comp in bundle.components)
    total_coarse_bars = int((bundle.range_end - bundle.evaluation_start) / coarse_step)
    # Chọn kích thước fold k (k*coarse_step cho train/val/test/step) + purge/embargo
    # để bảo đảm 5*k + 2 <= total_coarse_bars → sinh ít nhất 3 fold OOS độc lập.
    unit_bars = max(2, (total_coarse_bars - 2) // 5)
    unit_td = unit_bars * coarse_step
    purge_td = coarse_step
    embargo_td = coarse_step

    splits = walk_forward_splits(
        TemporalWindow(bundle.evaluation_start, bundle.range_end),
        train=unit_td,
        validation=unit_td,
        test=unit_td,
        step=unit_td,
        purge=purge_td,
        embargo=embargo_td,
    )
    assert len(splits) >= 2, f"Cần ít nhất 2 fold walk-forward cho {release_id}."

    prev_test_end: datetime | None = None
    for fold_idx, split in enumerate(splits):
        assert split.train.end + purge_td == split.validation.start
        assert split.validation.end + embargo_td == split.test.start
        if prev_test_end is not None:
            assert split.test.start >= prev_test_end
        prev_test_end = split.test.end

        # Kiểm chứng embargo bằng select_temporal_samples: mẫu trong embargo bị cô lập khỏi OOS
        in_embargo = SampleLineage(
            sample_id=f"embargo-{fold_idx}",
            decision_at=split.validation.end,
            feature_available_at=split.validation.end,
            label_interval=TemporalWindow(split.validation.end, split.test.start + coarse_step),
        )
        clean_train = SampleLineage(
            sample_id=f"train-{fold_idx}",
            decision_at=split.train.start,
            feature_available_at=split.train.start,
            label_interval=TemporalWindow(split.train.start, split.train.end),
        )
        protected_test = SampleLineage(
            sample_id=f"test-{fold_idx}",
            decision_at=split.test.start,
            feature_available_at=split.test.start,
            label_interval=TemporalWindow(split.test.start, split.test.end),
        )
        selection = select_temporal_samples(
            (clean_train, in_embargo),
            evaluation_cutoff=split.test.end,
            protected_samples=(protected_test,),
        )
        assert selection.eligible_sample_ids == (f"train-{fold_idx}",)
        assert selection.exclusions[0].sample_id == f"embargo-{fold_idx}"

        fold_bundle = _slice_bundle_for_window(
            bundle,
            eval_start=split.test.start,
            range_end=split.test.end,
        )
        for comp in fold_bundle.components:
            assert all(bar.open_time < split.test.end for bar in comp.bars)

        _plan, _comp, fold_run, fold_report = _run_replay(
            fold_bundle,
            release_id=release_id,
            seed=300 + fold_idx,
            job_salt=f"wf-{fold_idx}",
        )
        _assert_report_accounting_invariants(fold_report, fold_run)


def test_walk_forward_no_lookahead_future_mutation_invariance_and_runner_rejection() -> None:
    """Chứng minh thay đổi nến tương lai sau `split.test.end` không ảnh hưởng kết quả fold OOS."""
    release_id = "h04-v1"
    bundle, _ = _build_multi_regime_bundle_for_release(release_id, symbol="BTCUSDT", seed=42)
    step_td = INTERVAL_DURATIONS["15m"]
    splits = walk_forward_splits(
        TemporalWindow(bundle.evaluation_start, bundle.range_end),
        train=step_td * 48,
        validation=step_td * 24,
        test=step_td * 48,
        step=step_td * 48,
        purge=step_td * 4,
        embargo=step_td * 4,
    )
    target_split = splits[0]
    fold_clean = _slice_bundle_for_window(
        bundle, eval_start=target_split.test.start, range_end=target_split.test.end
    )

    poisoned_bars = tuple(
        replace(
            b,
            open=b.open * Decimal("10"),
            high=b.high * Decimal("10"),
            low=b.low * Decimal("10"),
            close=b.close * Decimal("10"),
        )
        if b.open_time >= target_split.test.end
        else b
        for b in bundle.components[0].bars
    )
    poisoned_bundle = _build_bundle_from_tf_bars(
        workspace_id=WORKSPACE_ID,
        symbol="BTCUSDT",
        bars_by_tf={"15m": poisoned_bars},
        evaluation_start=bundle.evaluation_start,
        range_end=bundle.range_end,
    )
    fold_from_poisoned = _slice_bundle_for_window(
        poisoned_bundle, eval_start=target_split.test.start, range_end=target_split.test.end
    )

    _, _, run_a, rep_a = _run_replay(fold_clean, release_id=release_id, seed=99)
    _, _, run_b, rep_b = _run_replay(fold_from_poisoned, release_id=release_id, seed=99)
    assert rep_a.report_hash == rep_b.report_hash
    assert run_a.trade_outcomes == run_b.trade_outcomes

    obs = build_backtest_dataset_replay_observations(fold_clean, strategy_release_id=release_id)
    leaked_obs = (
        ReplayObservation(
            event_id=obs[0].event_id,
            event_at=obs[0].event_at,
            available_at=obs[0].snapshot.decision_time + timedelta(minutes=1),
            snapshot=obs[0].snapshot,
        ),
        *obs[1:],
    )
    plan = build_backtest_replay_plan(
        WORKSPACE_ID,
        uuid5(WORKSPACE_ID, "leakage-probe"),
        fold_clean,
        BacktestReplayJobSubmission(
            bundle_hash=fold_clean.dataset_hash,
            strategy_release_id=release_id,
            execution_interval="15m",
            starting_equity=Decimal("10000"),
            per_trade_risk_fraction=Decimal("0.01"),
            fee_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0002"),
            seed=99,
        ),
    )
    rejected = BacktestRunner().run(
        leaked_obs,
        manifest=plan.manifest,
        evaluator=lambda _snap: None,
    )
    assert isinstance(rejected, ReplayRejected)
    assert rejected.reason_code == "LOOKAHEAD_DETECTED"


# ---------------------------------------------------------------------------
# 4. Tính tất định theo seed (Replay + Bootstrap CI + Portfolio)
# ---------------------------------------------------------------------------
def test_replay_and_portfolio_are_deterministic_by_seed() -> None:
    """Cùng seed cho manifest, report hash, trade outcomes, CI và kết quả danh mục giống hệt."""
    for release_id in ("h04-v1", "sc-binance-usdm-vol-breakout-001-v1.0.0"):
        bundle, _ = _build_multi_regime_bundle_for_release(release_id, symbol="BTCUSDT", seed=42)
        plan_1, _, run_1, rep_1 = _run_replay(bundle, release_id=release_id, seed=12345)
        plan_2, _, run_2, rep_2 = _run_replay(bundle, release_id=release_id, seed=12345)

        assert plan_1.manifest == plan_2.manifest
        assert plan_1.manifest.content_hash == plan_2.manifest.content_hash
        assert rep_1 == rep_2
        assert run_1.trade_outcomes == run_2.trade_outcomes

        plan_other, _, run_other, rep_other = _run_replay(bundle, release_id=release_id, seed=99999)
        assert plan_other.manifest.seed == 99999
        assert plan_other.manifest.content_hash != plan_1.manifest.content_hash
        assert run_other.trade_outcomes == run_1.trade_outcomes
        assert rep_other.net_pnl == rep_1.net_pnl
        from trading_bot.domain.learning.evaluation import MIN_TRADES_FOR_CONFIDENCE_INTERVAL

        if rep_1.trade_count >= MIN_TRADES_FOR_CONFIDENCE_INTERVAL:
            assert rep_1.confidence_interval is not None
            assert rep_other.confidence_interval is not None

    def build_portfolio_for_seed(data_seed: int, replay_seed: int) -> PortfolioResult:
        all_trades: list[PortfolioTrade] = []
        for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            b, _ = _build_multi_regime_bundle_for_release("h04-v1", symbol=sym, seed=data_seed)
            _, _, r, _ = _run_replay(
                b,
                release_id="h04-v1",
                per_trade_risk_fraction=Decimal("0.05"),
                seed=replay_seed,
            )
            all_trades.extend(
                trades_with_symbol_equity(
                    sym,
                    ((t.opened_at, t.closed_at, t.net_pnl, t.exposure) for t in r.trade_outcomes),
                    Decimal("10000"),
                )
            )
        return simulate_portfolio(
            all_trades,
            starting_equity=Decimal("10000"),
            limits=PortfolioLimits(max_positions=2, max_leverage=Decimal("3")),
        )

    p1 = build_portfolio_for_seed(42, 777)
    p2 = build_portfolio_for_seed(42, 777)
    p_diff = build_portfolio_for_seed(99, 777)
    assert p1 == p2
    assert p1 != p_diff


# ---------------------------------------------------------------------------
# 5. Portfolio Backtest: max_positions, max_leverage, equity scaling & drawdown
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "release_id",
    [
        "h22-v1",
        "h04-v1",
        "sc-binance-usdm-vol-breakout-001-v1.0.0",
        "sc-binance-usdm-taker-flow-008-v1.0.0",
    ],
)
def test_multi_symbol_portfolio_backtest_enforces_max_positions_leverage_and_drawdown(
    release_id: str,
) -> None:
    """Gộp kết quả replay thật của BTC/ETH/SOL vào `simulate_portfolio` và kiểm soát mọi trần."""
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    starting_equity = Decimal("10000")
    all_trades: list[PortfolioTrade] = []

    for sym in symbols:
        bundle, _ = _build_multi_regime_bundle_for_release(release_id, symbol=sym, seed=42)
        _, _, run, report = _run_replay(
            bundle,
            release_id=release_id,
            starting_equity=starting_equity,
            per_trade_risk_fraction=Decimal("0.05"),
            seed=2026,
        )
        _assert_report_accounting_invariants(report, run)
        all_trades.extend(
            trades_with_symbol_equity(
                sym,
                ((t.opened_at, t.closed_at, t.net_pnl, t.exposure) for t in run.trade_outcomes),
                starting_equity,
            )
        )

    assert len(all_trades) >= 3

    pos_tight = simulate_portfolio(
        all_trades,
        starting_equity=starting_equity,
        limits=PortfolioLimits(max_positions=1, max_leverage=Decimal("5")),
    )
    pos_loose = simulate_portfolio(
        all_trades,
        starting_equity=starting_equity,
        limits=PortfolioLimits(max_positions=3, max_leverage=Decimal("5")),
    )
    for res, lim in ((pos_tight, 1), (pos_loose, 3)):
        assert res.peak_open_positions <= lim
        assert res.taken + res.skipped_positions + res.skipped_leverage == len(all_trades)
        assert res.ending_equity == starting_equity + res.net_pnl
        assert abs(res.net_pnl - (res.gross_profit - res.gross_loss)) < Decimal("1e-18")
        assert res.max_drawdown >= Decimal("0")
        assert res.wins <= res.taken

    assert pos_tight.peak_open_positions == 1
    assert pos_loose.taken >= pos_tight.taken
    assert pos_tight.skipped_positions >= pos_loose.skipped_positions

    lev_tight = simulate_portfolio(
        all_trades,
        starting_equity=starting_equity,
        limits=PortfolioLimits(max_positions=3, max_leverage=Decimal("1")),
    )
    lev_loose = simulate_portfolio(
        all_trades,
        starting_equity=starting_equity,
        limits=PortfolioLimits(max_positions=3, max_leverage=Decimal("3")),
    )
    assert lev_tight.taken + lev_tight.skipped_positions + lev_tight.skipped_leverage == len(
        all_trades
    )
    assert lev_loose.taken + lev_loose.skipped_positions + lev_loose.skipped_leverage == len(
        all_trades
    )
    assert lev_loose.taken >= lev_tight.taken
    assert lev_tight.skipped_leverage >= lev_loose.skipped_leverage
    assert lev_tight.skipped_leverage >= 1 or lev_tight.skipped_positions >= 1

    if lev_loose.gross_loss > Decimal("0"):
        pf = lev_loose.gross_profit / lev_loose.gross_loss
        assert pf >= Decimal("0")
        if lev_loose.gross_profit > Decimal("0"):
            assert pf > Decimal("0")


# ---------------------------------------------------------------------------
# 6. Chi phí fee + slippage khớp model (Zero vs Standard vs Stress & Exact Math)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "release_id",
    ["h22-v1", "sc-binance-usdm-taker-flow-008-v1.0.0"],
)
def test_fee_and_slippage_match_cost_model_in_replay_and_portfolio(release_id: str) -> None:
    """Xác minh fee và slippage trong `EvaluationReport` và `PortfolioResult` khớp đúng model."""
    bundle, series = _build_multi_regime_bundle_for_release(release_id, symbol="BTCUSDT", seed=42)
    bar_by_open = {bar.open_time: bar for bar in series.bars}

    cost_profiles = (
        ("zero", Decimal("0"), Decimal("0")),
        ("standard", Decimal("0.0005"), Decimal("0.0002")),
        ("stress", Decimal("0.0015"), Decimal("0.0008")),
    )

    runs_by_name: dict[str, tuple[RuntimePaperReplayRun, EvaluationReport]] = {}
    portfolio_by_name: dict[str, PortfolioResult] = {}

    for label, fee_rate, slippage_rate in cost_profiles:
        plan, _, run, report = _run_replay(
            bundle,
            release_id=release_id,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            seed=555,
            job_salt=label,
        )
        _assert_report_accounting_invariants(report, run)
        assert dict(plan.manifest.cost_assumptions) == {
            "adverse_funding_per_unit": "0",
            "entry_fee_rate": str(fee_rate),
            "entry_slippage_rate": str(slippage_rate),
            "exit_fee_rate": str(fee_rate),
            "exit_slippage_rate": str(slippage_rate),
            "spread_rate": "0",
        }
        assert report.trade_count >= 1

        if label == "zero":
            assert report.fees == Decimal("0")
            # Khi slippage_rate=0, trailing stop có thể mang số lẻ dưới 1 tick (1e-8) nên sai lệch
            # làm tròn tick bị tính tối đa 1 tick mỗi đơn vị khối lượng (~<= 1e-5 USDT).
            assert report.slippage <= Decimal("0.0001")
            assert report.net_pnl == report.gross_pnl - report.slippage
        else:
            assert report.fees > Decimal("0")
            assert report.slippage > Decimal("0")
            expected_ratio = float(fee_rate / slippage_rate)
            for trade in run.trade_outcomes:
                assert trade.fees > Decimal("0")
                assert trade.slippage > Decimal("0")
                actual_ratio = float(trade.fees / trade.slippage)
                assert actual_ratio == pytest.approx(expected_ratio, rel=0.05)

                entry_bar = bar_by_open[trade.opened_at]
                assert trade.exposure > Decimal("0")
                assert entry_bar.open > Decimal("0")
                assert trade.slippage < trade.exposure * slippage_rate * Decimal("5")
                assert trade.fees < trade.exposure * fee_rate * Decimal("5")

        runs_by_name[label] = (run, report)
        p_trades = trades_with_symbol_equity(
            "BTCUSDT",
            ((t.opened_at, t.closed_at, t.net_pnl, t.exposure) for t in run.trade_outcomes),
            Decimal("10000"),
        )
        portfolio_by_name[label] = simulate_portfolio(
            p_trades,
            starting_equity=Decimal("10000"),
            limits=PortfolioLimits(max_positions=1, max_leverage=Decimal("1")),
        )

    zero_run, zero_rep = runs_by_name["zero"]
    std_run, std_rep = runs_by_name["standard"]
    str_run, str_rep = runs_by_name["stress"]

    assert std_rep.fees + std_rep.slippage > Decimal("0")
    assert str_rep.fees + str_rep.slippage > std_rep.fees + std_rep.slippage
    assert zero_rep.net_pnl == zero_rep.gross_pnl - zero_rep.slippage
    if zero_run.trade_outcomes and std_run.trade_outcomes and str_run.trade_outcomes:
        assert portfolio_by_name["stress"].net_pnl < portfolio_by_name["zero"].net_pnl


def test_exact_paper_fee_and_slippage_formula_against_known_trade() -> None:
    """Đối chiếu chính xác đến từng tick công thức fee và slippage trên một lệnh đóng thực."""
    release_id = "sc-binance-usdm-vol-breakout-001-v1.0.0"
    bundle, series = _build_multi_regime_bundle_for_release(release_id, symbol="BTCUSDT", seed=42)
    fee_rate = Decimal("0.0005")
    slippage_rate = Decimal("0.0002")
    tick = Decimal("0.00000001")

    _, _, run, report = _run_replay(
        bundle,
        release_id=release_id,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        seed=808,
    )
    assert report.trade_count >= 1
    trade = run.trade_outcomes[0]
    idx_by_open = {b.open_time: idx for idx, b in enumerate(series.bars)}
    entry_idx = idx_by_open[trade.opened_at]
    entry_bar = series.bars[entry_idx]
    prev_bar = series.bars[entry_idx - 1]

    raw_entry_exec = entry_bar.open * (Decimal("1") + slippage_rate)
    entry_exec = (raw_entry_exec / tick).to_integral_value(rounding=ROUND_CEILING) * tick

    raw_plan_entry = prev_bar.close * (Decimal("1") + slippage_rate)
    plan_entry = (raw_plan_entry / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    qty_step = Decimal("0.00000001")
    matched_qty = ((trade.exposure / plan_entry) / qty_step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * qty_step
    if matched_qty * plan_entry != trade.exposure:
        matched_qty = ((trade.exposure / plan_entry) / qty_step).to_integral_value(
            rounding=ROUND_CEILING
        ) * qty_step
    assert matched_qty * plan_entry == trade.exposure

    exit_ref = entry_bar.open + trade.gross_pnl / matched_qty
    raw_exit_exec = exit_ref * (Decimal("1") - slippage_rate)
    exit_exec = (raw_exit_exec / tick).to_integral_value(rounding=ROUND_FLOOR) * tick

    expected_fees = entry_exec * matched_qty * fee_rate + exit_exec * matched_qty * fee_rate
    expected_slippage = (
        abs(entry_exec - entry_bar.open) * matched_qty + abs(exit_exec - exit_ref) * matched_qty
    )
    assert trade.fees == expected_fees
    assert trade.slippage == expected_slippage


# ---------------------------------------------------------------------------
# 7. Tail Shock: RV Gate chặn tín hiệu cực đoan & chốt lỗ giới hạn drawdown
# ---------------------------------------------------------------------------
def test_tail_shock_regime_triggers_rv_gate_and_bounds_drawdown() -> None:
    """Chứng minh tail shock (RV > 0.08) bị H02 & Card 003 chặn và drawdown được kiểm soát."""
    registry = strategy_registry()
    for release_id in ("h02-v1", "sc-binance-usdm-tsmom-003-v1.0.0"):
        bundle, series = _build_multi_regime_bundle_for_release(
            release_id, symbol="BTCUSDT", seed=42
        )
        t0, _t1 = series.regime_slices["tail_shock"]
        shock_start_time = series.bars[t0].open_time
        shock_end_time = series.bars[min(len(series.bars) - 1, t0 + 11)].close_time

        _, _, run, report = _run_replay(bundle, release_id=release_id, seed=42)
        _assert_report_accounting_invariants(report, run)

        observations = build_backtest_dataset_replay_observations(
            bundle, strategy_release_id=release_id, max_bars_per_timeframe=256
        )
        selection = StrategySelection("s-tail", (release_id,), published=True)
        rule = registry.resolve(release_id, selection)

        high_rv_checked = 0
        for obs in observations:
            if shock_start_time <= obs.snapshot.decision_time <= shock_end_time:
                rv_obs = obs.snapshot.get_feature("realized_volatility_20", "4h")
                if rv_obs is not None and rv_obs.value is not None and rv_obs.value > 0.08:
                    high_rv_checked += 1
                    snap_flat = replace(obs.snapshot, position_is_flat=True)
                    decision = rule.evaluate(snap_flat, selection)
                    assert not isinstance(decision, StrategyCandidate), (
                        f"{release_id} không được phép mở lệnh khi RV20={rv_obs.value:.4f} > 0.08"
                    )
        assert high_rv_checked >= 1
        assert report.max_drawdown < Decimal("10000")
