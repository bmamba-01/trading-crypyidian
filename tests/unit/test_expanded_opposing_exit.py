"""Bộ kiểm thử TDD cho cơ chế đánh giá tín hiệu ngược chiều point-in-time (`OPPOSING_SIGNAL`)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

import pytest

from trading_bot.application.backtest import ReplayManifest
from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.backtest_dataset_replay_context import (
    run_backtest_dataset_runtime_replay,
)
from trading_bot.application.backtest_dataset_replay_observations import (
    build_backtest_dataset_replay_observations,
)
from trading_bot.application.backtest_replay_executor import (
    REPLAY_MAX_BARS_PER_TIMEFRAME,
    build_backtest_replay_plan,
)
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.backtest_replay_opposing import (
    build_point_in_time_opposing_provider,
    latest_point_in_time_observation,
)
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.runtime_replay_evaluator import ReplayEvaluationContext
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import (
    FEATURE_CODE_HASH,
    entry_timeframe_for_release,
    feature_requirements_for_release,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperBar, PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.market.feature_snapshot import FEATURE_SCHEMA_VERSION
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.risk.engine import RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    PlannerSettings,
    RiskLimits,
)
from trading_bot.domain.strategy.contracts import FeatureQuality, StrategySelection

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
START_4H = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
START_15M = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _rights(start: datetime, end: datetime) -> BackfillRightsEvidence:
    return BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/opposing-exit-terms",
        policy_fingerprint=hashlib.sha256(b"opposing-exit-policy").hexdigest(),
        verified_at=start - timedelta(days=1),
        checked_at=start - timedelta(hours=1),
        valid_until=end + timedelta(days=1),
    )


def _ohlcv_bar(
    *,
    timeframe: str,
    interval: timedelta,
    opened: datetime,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float = 150.0,
    taker_buy_base: float = 75.0,
    cursor_prefix: str = "opp",
) -> OHLCVBar:
    closed = opened + interval - timedelta(milliseconds=1)
    cursor = f"{cursor_prefix}:{timeframe}:{opened.isoformat()}"
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=Decimal(str(round(open_price, 4))),
        high=Decimal(str(round(high, 4))),
        low=Decimal(str(round(low, 4))),
        close=Decimal(str(round(close, 4))),
        volume=Decimal(str(round(volume, 4))),
        quote_volume=Decimal(str(round(volume * close, 4))),
        taker_buy_base_volume=Decimal(str(round(taker_buy_base, 4))),
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor=cursor,
        checksum=hashlib.sha256(cursor.encode()).hexdigest(),
    )


def _build_h02_bundle(mode: str) -> tuple[BacktestDatasetReplayBundle, datetime]:
    """Tạo bundle 4h cho `h02-v1`: warmup 255 nến rồi 4 nến evaluation."""
    interval = timedelta(hours=4)
    warmup_count = 255
    eval_start = START_4H + warmup_count * interval
    total_bars = warmup_count + 4
    range_end = START_4H + total_bars * interval
    rights = _rights(START_4H, range_end)

    bars: list[OHLCVBar] = []
    for idx in range(warmup_count):
        opened = START_4H + idx * interval
        if mode == "no_signal" and idx == 215:
            base = 105.0
        elif mode == "no_signal" and idx == 131:
            base = 95.0
        elif mode == "no_signal" and idx == 5:
            base = 100.0
        else:
            base = 100.0 + (0.6 if idx % 2 == 0 else -0.6)
        bars.append(
            _ohlcv_bar(
                timeframe="4h",
                interval=interval,
                opened=opened,
                open_price=base,
                high=base + 1.8,
                low=base - 1.8,
                close=base,
                cursor_prefix=f"h02-{mode}",
            )
        )

    # Bar 255 (eval_start) và các nến sau theo từng kịch bản
    if mode == "short_to_long":
        # Bar 255: giảm xuống 95.0 (< 99.4 ở mọi chân trời) -> SHORT
        bars.append(
            _ohlcv_bar(
                timeframe="4h",
                interval=interval,
                opened=eval_start,
                open_price=99.8,
                high=100.2,
                low=94.5,
                close=95.0,
                cursor_prefix=f"h02-{mode}",
            )
        )
        # Bar 256: khớp SHORT tại open=95.0; giữ < 99.4 (cùng chiều SHORT)
        bars.append(
            _ohlcv_bar(
                timeframe="4h",
                interval=interval,
                opened=eval_start + interval,
                open_price=95.0,
                high=97.0,
                low=94.2,
                close=95.5,
                cursor_prefix=f"h02-{mode}",
            )
        )
        # Bar 257: hồi lên 104.0 (dưới stop ~107.0) -> đóng nến sinh LONG
        bars.append(
            _ohlcv_bar(
                timeframe="4h",
                interval=interval,
                opened=eval_start + 2 * interval,
                open_price=95.5,
                high=104.5,
                low=95.0,
                close=104.0,
                cursor_prefix=f"h02-{mode}",
            )
        )
        # Bar 258: nến kế tiếp -> vị thế SHORT đóng bằng OPPOSING_SIGNAL tại 103.5
        bars.append(
            _ohlcv_bar(
                timeframe="4h",
                interval=interval,
                opened=eval_start + 3 * interval,
                open_price=104.0,
                high=104.5,
                low=103.0,
                close=103.5,
                cursor_prefix=f"h02-{mode}",
            )
        )
    else:
        # Bar 255 (eval_start): tăng rõ so với 42/126/252 nến trước -> LONG
        bars.append(
            _ohlcv_bar(
                timeframe="4h",
                interval=interval,
                opened=eval_start,
                open_price=100.2,
                high=106.0,
                low=99.8,
                close=105.0,
                cursor_prefix=f"h02-{mode}",
            )
        )
        # Bar 256: khớp lệnh LONG ở 105.0; vẫn giữ > 100.6 (tín hiệu cùng chiều LONG)
        bars.append(
            _ohlcv_bar(
                timeframe="4h",
                interval=interval,
                opened=eval_start + interval,
                open_price=105.0,
                high=106.0,
                low=102.5,
                close=104.5,
                cursor_prefix=f"h02-{mode}",
            )
        )

        if mode == "reverse_short":
            # Bar 257: giảm về 96.0 (trên stop ~93.0) -> sinh SHORT tại bar[257].available_at
            bars.append(
                _ohlcv_bar(
                    timeframe="4h",
                    interval=interval,
                    opened=eval_start + 2 * interval,
                    open_price=104.5,
                    high=104.8,
                    low=95.5,
                    close=96.0,
                    cursor_prefix=f"h02-{mode}",
                )
            )
            # Bar 258: nến kế tiếp -> vị thế LONG đóng bằng OPPOSING_SIGNAL tại 96.5
            bars.append(
                _ohlcv_bar(
                    timeframe="4h",
                    interval=interval,
                    opened=eval_start + 3 * interval,
                    open_price=96.0,
                    high=97.0,
                    low=95.5,
                    close=96.5,
                    cursor_prefix=f"h02-{mode}",
                )
            )
        elif mode == "same_long":
            for step in (2, 3):
                bars.append(
                    _ohlcv_bar(
                        timeframe="4h",
                        interval=interval,
                        opened=eval_start + step * interval,
                        open_price=104.5,
                        high=105.8,
                        low=103.5,
                        close=104.8,
                        cursor_prefix=f"h02-{mode}",
                    )
                )
        elif mode == "no_signal":
            # Bar 257 & 258 ở 100.0: signs=(-1, +1, 0) => NO_SIGNAL
            for step in (2, 3):
                bars.append(
                    _ohlcv_bar(
                        timeframe="4h",
                        interval=interval,
                        opened=eval_start + step * interval,
                        open_price=103.0 if step == 2 else 100.0,
                        high=103.5 if step == 2 else 101.0,
                        low=99.5,
                        close=100.0,
                        cursor_prefix=f"h02-{mode}",
                    )
                )
        else:
            raise ValueError(f"Mode không hợp lệ: {mode}")

    manifest = build_backtest_dataset_manifest(
        RestBackfillRequest(
            symbol="BTCUSDT",
            interval="4h",
            start=START_4H,
            end=range_end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        ),
        evaluation_start=eval_start,
        warmup_bars=warmup_count,
        bars=tuple(bars),
        rights_evidence=rights,
        prepared_at=range_end,
        schema_version=3,
    )
    bundle = build_backtest_dataset_replay_bundle(
        (BacktestDatasetReplayInput(manifest, tuple(bars), rights),)
    )
    return bundle, eval_start


def _build_h22_bundle(
    *,
    mode: str,
    include_1m: bool = False,
) -> tuple[BacktestDatasetReplayBundle, datetime]:
    """Tạo bundle 15m + 1h (+ tùy chọn 1m) cho `h22-v1`."""
    origin = START_15M
    warmup_hours = 210  # >= 200 nến 1h để FeatureCalculator warmup đủ ema_200 và đánh dấu READY
    eval_start = origin + timedelta(hours=warmup_hours)
    range_end = eval_start + timedelta(hours=2)
    rights = _rights(origin, range_end)

    # 1h bars: 0..209 tăng dần từ 70.0 lên 101.35 (EMA80 ~95.5 < 101.35);
    # nến 1h thứ 210 (18:00..19:00, đóng lúc 18:59:59.999) giảm về 90.0 (< EMA80) nếu reverse_short!
    bars_1h: list[OHLCVBar] = []
    count_1h = int((range_end - origin) / timedelta(hours=1))
    for i in range(count_1h):
        opened = origin + i * timedelta(hours=1)
        if i < warmup_hours:
            c = 70.0 + i * 0.15
        elif i == warmup_hours and mode in {"reverse_short", "short_to_long"}:
            c = 90.0
        elif i == warmup_hours + 1 and mode == "short_to_long":
            c = 104.0
        else:
            c = 102.0
        bars_1h.append(
            _ohlcv_bar(
                timeframe="1h",
                interval=timedelta(hours=1),
                opened=opened,
                open_price=c,
                high=c + 1.0,
                low=c - 1.0,
                close=c,
                volume=300.0,
                taker_buy_base=180.0,
                cursor_prefix=f"h22-{mode}",
            )
        )

    # 15m bars
    bars_15m: list[OHLCVBar] = []
    count_15m = int((range_end - origin) / timedelta(minutes=15))
    eval_idx = int((eval_start - origin) / timedelta(minutes=15))
    for i in range(count_15m):
        opened = origin + i * timedelta(minutes=15)
        if i < eval_idx - 10:
            c = 70.0 + (i / (eval_idx - 10)) * 30.0
            o, h, low_p, vol, tb = c - 0.1, c + 1.4, c - 1.4, 100.0, 52.0
        elif i < eval_idx:
            c = 100.0 + (0.1 if i % 2 == 0 else -0.1)
            o, h, low_p, vol, tb = c, 100.5, 98.8, 100.0, 58.0
        elif i == eval_idx:
            # 18:00..18:15: tích lũy với TI = +0.40
            o, h, low_p, c, vol, tb = 99.9, 100.4, 99.1, 100.0, 100.0, 70.0
        elif i == eval_idx + 1:
            # 18:15..18:30: tích lũy với TI = -0.20 (giữ trung bình 3 nến cho cả LONG và SHORT!)
            o, h, low_p, c, vol, tb = 100.0, 100.4, 99.0, 99.9, 100.0, 40.0
        elif i == eval_idx + 2:
            # 18:30..18:45: phá đỉnh 20 nến (>100.5) với TI = +0.60, TB3 = +0.27 -> LONG!
            o, h, low_p, c, vol, tb = 100.1, 102.0, 99.8, 101.5, 250.0, 200.0
        elif i == eval_idx + 3:
            # 18:45..19:00: khớp lệnh vào LONG tại open=101.5 (trên stop ~97.1); đóng trùng nến 1h!
            if mode in {"reverse_short", "short_to_long"}:
                # Phá đáy ll20_prev (98.8) về 98.2 với TI = -0.80, TB3 = -0.133 <= -0.12 -> SHORT!
                o, h, low_p, c, vol, tb = 101.5, 101.8, 98.0, 98.2, 260.0, 26.0
            else:
                o, h, low_p, c, vol, tb = 101.5, 101.9, 99.2, 99.8, 90.0, 45.0
        elif i == eval_idx + 4:
            # 19:00..19:15: nến sau khi SHORT phát ra lúc 18:59:59.999 -> đóng bằng OPPOSING_SIGNAL
            if mode in {"reverse_short", "short_to_long"}:
                o, h, low_p, c, vol, tb = 98.2, 98.9, 97.9, 98.5, 110.0, 55.0
            else:
                o, h, low_p, c, vol, tb = 99.8, 100.4, 99.4, 100.1, 90.0, 45.0
        else:
            o, h, low_p, c, vol, tb = 99.8, 100.4, 99.4, 100.1, 100.0, 50.0
        bars_15m.append(
            _ohlcv_bar(
                timeframe="15m",
                interval=timedelta(minutes=15),
                opened=opened,
                open_price=o,
                high=h,
                low=low_p,
                close=c,
                volume=vol,
                taker_buy_base=tb,
                cursor_prefix=f"h22-{mode}",
            )
        )

    inputs: list[BacktestDatasetReplayInput] = [
        BacktestDatasetReplayInput(
            build_backtest_dataset_manifest(
                RestBackfillRequest(
                    symbol="BTCUSDT",
                    interval="15m",
                    start=origin,
                    end=range_end,
                    purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
                ),
                evaluation_start=eval_start,
                warmup_bars=eval_idx,
                bars=tuple(bars_15m),
                rights_evidence=rights,
                prepared_at=range_end,
                schema_version=3,
            ),
            tuple(bars_15m),
            rights,
        ),
        BacktestDatasetReplayInput(
            build_backtest_dataset_manifest(
                RestBackfillRequest(
                    symbol="BTCUSDT",
                    interval="1h",
                    start=origin,
                    end=range_end,
                    purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
                ),
                evaluation_start=eval_start,
                warmup_bars=int((eval_start - origin) / timedelta(hours=1)),
                bars=tuple(bars_1h),
                rights_evidence=rights,
                prepared_at=range_end,
                schema_version=3,
            ),
            tuple(bars_1h),
            rights,
        ),
    ]

    if include_1m:
        # Sinh nến 1m cho vùng quanh evaluation window (từ eval_start - 1h tới range_end)
        start_1m = eval_start - timedelta(hours=1)
        count_1m = int((range_end - start_1m) / timedelta(minutes=1))
        bars_1m: list[OHLCVBar] = []
        bar_by_15m = {b.open_time: b for b in bars_15m}
        for idx in range(count_1m):
            opened = start_1m + idx * timedelta(minutes=1)
            parent_open = opened.replace(minute=(opened.minute // 15) * 15, second=0, microsecond=0)
            parent = bar_by_15m[parent_open]
            p_close = float(parent.close)
            p_open = float(parent.open)
            # Giữ OHLC 1m an toàn trong biên không chạm stop (~96.8)
            mid = (p_open + p_close) / 2.0
            c = p_close if (opened.minute % 15 == 14) else mid
            o = p_open if (opened.minute % 15 == 0) else mid
            bars_1m.append(
                _ohlcv_bar(
                    timeframe="1m",
                    interval=timedelta(minutes=1),
                    opened=opened,
                    open_price=o,
                    high=max(o, c) + 0.2,
                    low=min(o, c) - 0.2,
                    close=c,
                    volume=10.0,
                    taker_buy_base=5.0,
                    cursor_prefix=f"h22-{mode}-1m",
                )
            )
        inputs.append(
            BacktestDatasetReplayInput(
                build_backtest_dataset_manifest(
                    RestBackfillRequest(
                        symbol="BTCUSDT",
                        interval="1m",
                        start=start_1m,
                        end=range_end,
                        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
                    ),
                    evaluation_start=eval_start,
                    warmup_bars=int((eval_start - start_1m) / timedelta(minutes=1)),
                    bars=tuple(bars_1m),
                    rights_evidence=rights,
                    prepared_at=range_end,
                    schema_version=3,
                ),
                tuple(bars_1m),
                rights,
            )
        )

    return build_backtest_dataset_replay_bundle(tuple(inputs)), eval_start


def _run_replay_with_gateway(
    bundle: BacktestDatasetReplayBundle,
    *,
    release_id: str,
    execution_interval: str,
) -> tuple[object, PaperExecutionGateway]:
    """Chạy bundle qua `run_backtest_dataset_runtime_replay` mặc định và trả về gateway."""
    registry = strategy_registry()
    release = registry.get_release(release_id)
    requirements = feature_requirements_for_release(release_id)
    entry_timeframe = entry_timeframe_for_release(release_id) or execution_interval
    equity = Decimal("10000")
    risk_fraction = Decimal("0.01")
    job_id = uuid5(WORKSPACE_ID, f"opposing-test:{release_id}:{bundle.dataset_hash}")
    settings_id = uuid5(job_id, "settings")
    risk_id = uuid5(job_id, "risk")
    settings_hash = hashlib.sha256(f"{job_id}".encode()).hexdigest()
    feature_schema_hash = hashlib.sha256(
        f"{FEATURE_SCHEMA_VERSION}:{FEATURE_CODE_HASH}:{release_id}".encode()
    ).hexdigest()
    pipeline_hash = hashlib.sha256(f"pipeline:{release_id}".encode()).hexdigest()
    confirmation = next(
        (tf for tf in sorted(requirements) if tf != entry_timeframe), entry_timeframe
    )
    published = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=settings_id,
        settings_hash=settings_hash,
        settings=WorkspaceSettings.model_validate(
            {
                "schema_version": 1,
                "runtime": {"mode": "PAPER", "timezone": "UTC"},
                "market_data": {
                    "venue": "BINANCE_USDM",
                    "symbols": [bundle.symbol],
                    "timeframes": sorted({*requirements, execution_interval}),
                    "max_age_seconds": 86_400,
                },
                "features": {"formula_version": FEATURE_CODE_HASH, "sessions_timezone": "UTC"},
                "strategy": {
                    "default_release": release_id,
                    "parameters": {},
                    "entry_timeframe": entry_timeframe,
                    "confirmation_timeframe": confirmation,
                },
                "risk": {
                    "profile_version_id": str(risk_id),
                    "workspace_max_open_risk_fraction": "0.01",
                },
                "paper_execution": {
                    "quote_asset": "USDT",
                    "starting_equity": "10000",
                    "fee_schedule_version": "paper-fill-v1",
                    "fill_model": "next_bar_with_adverse_costs",
                },
            }
        ),
    )
    step = Decimal("0.00000001")
    planner = PlannerSettings(
        workspace_id=str(WORKSPACE_ID),
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        published=True,
        active_release_ids=(release_id,),
        leverage=Decimal("1"),
        exchange=ExchangeRules(step, step, Decimal("1"), Decimal("0.01"), equity, equity / step),
        limits=RiskLimits(
            per_trade_risk_fraction=risk_fraction,
            max_aggregate_open_risk=equity * risk_fraction,
            max_notional=equity,
            max_leverage=Decimal("1"),
            max_positions=1,
            max_symbol_notional=equity,
            max_concentration_fraction=Decimal("1"),
            max_daily_loss=equity,
            max_rolling_loss=equity,
            max_account_age=timedelta(days=1),
            max_data_age=timedelta(hours=12),
        ),
        release_content_hashes=((release_id, release.content_hash),),
        settings_hash=settings_hash,
    )
    captured_at = bundle.evaluation_start - timedelta(microseconds=1)
    account = AccountSnapshot(
        workspace_id=str(WORKSPACE_ID),
        account_id=f"paper:{job_id}",
        settings_version_id=str(settings_id),
        captured_at=captured_at,
        equity=equity,
        available_margin=equity,
        is_known=True,
    )
    risk_state = RiskState(
        workspace_id=str(WORKSPACE_ID),
        account_id=account.account_id,
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        evaluated_at=bundle.evaluation_start,
        account_captured_at=captured_at,
        market_data_as_of=bundle.evaluation_start,
        equity=equity,
        available_margin=equity,
        open_exposures=(),
        realized_loss_today=Decimal("0"),
        realized_loss_rolling=Decimal("0"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=Decimal("0"),
    )
    costs = CostModel(
        version_id="paper-fill-v1",
        entry_fee_rate=Decimal("0.0005"),
        exit_fee_rate=Decimal("0.0005"),
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=Decimal("0.0002"),
        exit_slippage_rate=Decimal("0.0002"),
    )
    seed = ReplayEvaluationContext(
        published_settings=published,
        evaluated_at=bundle.evaluation_start,
        planner_settings=planner,
        account=account,
        risk_state=risk_state,
        open_exposures=(),
        cost_model=costs,
        feature_schema_hash=feature_schema_hash,
        pipeline_content_hash=pipeline_hash,
        execution_events=(),
        opposing_signal=False,
        reserve_risk=lambda decision, key: decision.approved and bool(key),
    )
    manifest = ReplayManifest(
        dataset_hash=bundle.dataset_hash,
        range_start=bundle.evaluation_start,
        range_end=bundle.range_end,
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        strategy_release_id=release_id,
        strategy_content_hash=release.content_hash,
        feature_schema_hash=feature_schema_hash,
        fill_model_version="paper-fill-v1",
        pipeline_content_hash=pipeline_hash,
        cost_assumptions=(
            ("adverse_funding_per_unit", "0"),
            ("entry_fee_rate", "0.0005"),
            ("entry_slippage_rate", "0.0002"),
            ("exit_fee_rate", "0.0005"),
            ("exit_slippage_rate", "0.0002"),
            ("spread_rate", "0"),
        ),
        parameter_family_hash=release.content_hash,
        trial_count=1,
        trial_index=0,
        seed=42,
    )
    gateways: list[PaperExecutionGateway] = []

    def make_gateway(
        _scenario: SensitivityScenario, _manifest: ReplayManifest
    ) -> PaperExecutionGateway:
        gw = PaperExecutionGateway(
            PaperExecutionPolicy(
                version_id="paper-fill-v1",
                latency=timedelta(0),
                spread_rate=Decimal("0"),
                slippage_rate=Decimal("0.0002"),
                fee_rate=Decimal("0.0005"),
            )
        )
        gateways.append(gw)
        return gw

    comparison = run_backtest_dataset_runtime_replay(
        bundle,
        manifest,
        strategies=registry,
        execution_interval=execution_interval,
        context_seed_factory=lambda _s, _m: seed,
        gateway_factory=make_gateway,
        max_bars_per_timeframe=REPLAY_MAX_BARS_PER_TIMEFRAME,
    )
    return comparison, gateways[0]


def test_opposing_provider_enforces_no_lookahead_and_direction_matching() -> None:
    bundle, eval_start = _build_h02_bundle("reverse_short")
    observations = build_backtest_dataset_replay_observations(
        bundle,
        strategy_release_id="h02-v1",
        max_bars_per_timeframe=REPLAY_MAX_BARS_PER_TIMEFRAME,
    )
    assert len(observations) == 4
    registry = strategy_registry()
    selection = StrategySelection(
        settings_version_id="settings-opposing-unit",
        active_release_ids=("h02-v1",),
        published=True,
    )

    provider_long = build_point_in_time_opposing_provider(
        registry,
        selection,
        observations=observations,
        open_direction="long",
    )
    provider_short = build_point_in_time_opposing_provider(
        registry,
        selection,
        observations=observations,
        open_direction="short",
    )

    interval = timedelta(hours=4)
    bar_before = PaperBar(
        bar_key="b-before",
        symbol="BTCUSDT",
        opened_at=eval_start,
        closed_at=eval_start + interval - timedelta(milliseconds=1),
        available_at=eval_start + interval - timedelta(milliseconds=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
    )
    # Tại bar_before (opened_at = eval_start), obs[0] đóng sau opened_at -> chưa có observation
    assert latest_point_in_time_observation(observations, bar_before) is None
    assert provider_long(bar_before, observations[0]) is False
    assert provider_short(bar_before, observations[0]) is False

    # Nến bar[257] (opened_at = eval_start + 8h): mới có obs[1] (LONG) đóng ở +8h - 1ms!
    # Cho dù caller truyền nhầm obs[2] (SHORT, đóng ở +12h - 1ms), không được nhìn trước obs[2]!
    bar_reversal = PaperBar(
        bar_key="b-rev",
        symbol="BTCUSDT",
        opened_at=eval_start + 2 * interval,
        closed_at=eval_start + 3 * interval - timedelta(milliseconds=1),
        available_at=eval_start + 3 * interval - timedelta(milliseconds=1),
        open=Decimal("104.5"),
        high=Decimal("104.8"),
        low=Decimal("95.5"),
        close=Decimal("96.0"),
    )
    assert latest_point_in_time_observation(observations, bar_reversal) == observations[1]
    assert provider_long(bar_reversal, observations[2]) is False
    assert provider_short(bar_reversal, observations[2]) is True

    # Nến bar[258] (opened_at = eval_start + 12h): obs[2] (SHORT) đã đóng -> vị thế LONG phải thoát!
    bar_next = PaperBar(
        bar_key="b-next",
        symbol="BTCUSDT",
        opened_at=eval_start + 3 * interval,
        closed_at=eval_start + 4 * interval - timedelta(milliseconds=1),
        available_at=eval_start + 4 * interval - timedelta(milliseconds=1),
        open=Decimal("96.0"),
        high=Decimal("97.0"),
        low=Decimal("95.5"),
        close=Decimal("96.5"),
    )
    assert latest_point_in_time_observation(observations, bar_next) == observations[2]
    assert provider_long(bar_next, observations[3]) is True
    assert provider_short(bar_next, observations[3]) is False

    # Chế độ đơn lẻ (observations=()): cũng phải chặn lookahead & kiểm tra direction
    single_provider = build_point_in_time_opposing_provider(
        registry,
        selection,
        open_direction="long",
    )
    assert single_provider(bar_reversal, observations[2]) is False  # observations[2] ở tương lai!
    assert single_provider(bar_next, observations[2]) is True
    assert single_provider(bar_next, None) is False

    # Nếu không có gateway lẫn open_direction: trả None (fail-closed) khi có candidate có hướng
    unknown_dir_provider = build_point_in_time_opposing_provider(
        registry,
        selection,
        observations=observations,
    )
    assert unknown_dir_provider(bar_next, None) is None

    # Nếu open_direction trả None (tài khoản đang phẳng): luôn trả False
    flat_provider = build_point_in_time_opposing_provider(
        registry,
        selection,
        observations=observations,
        open_direction=lambda _bar: None,
    )
    assert flat_provider(bar_next, None) is False

    # Nếu observation bị nhiễm lookahead nội tại -> từ chối rõ ràng
    corrupt_feature = replace(
        observations[2].snapshot.features[0],
        available_at=observations[2].snapshot.decision_time + timedelta(seconds=1),
    )
    corrupt_obs = replace(
        observations[2],
        snapshot=replace(
            observations[2].snapshot,
            features=(corrupt_feature, *observations[2].snapshot.features[1:]),
        ),
    )
    with pytest.raises(ValueError, match="LOOKAHEAD"):
        single_provider(bar_next, corrupt_obs)


def test_h02_long_position_closes_on_opposite_short_signal_and_ignores_same_or_empty() -> None:
    # 1. Đảo chiều sang SHORT -> đóng vị thế LONG tại nến sau bằng OPPOSING_SIGNAL
    bundle_rev, eval_start = _build_h02_bundle("reverse_short")
    comparison_rev, gateway_rev = _run_replay_with_gateway(
        bundle_rev,
        release_id="h02-v1",
        execution_interval="4h",
    )
    assert comparison_rev.is_complete
    report_rev = comparison_rev.runs[0].sensitivity_run.result
    assert report_rev.trade_count == 1
    positions = gateway_rev.positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.direction == "long"
    assert pos.is_closed is True
    assert pos.exit_reason == "OPPOSING_SIGNAL"
    assert pos.exit_reference_price == Decimal("96.5")
    # Không lookahead: bar 257 đóng ở eval_start + 12h - 1ms mới sinh SHORT -> chỉ đóng ở bar 258
    trade = comparison_rev.runs[0].trade_outcomes[0]
    assert trade.opened_at == eval_start + timedelta(hours=4)
    assert trade.closed_at == eval_start + timedelta(hours=16) - timedelta(milliseconds=1)

    # Cũng kiểm chứng qua `build_backtest_replay_plan` thật (đường executor sản phẩm)
    sub = BacktestReplayJobSubmission(
        bundle_hash=bundle_rev.dataset_hash,
        strategy_release_id="h02-v1",
        execution_interval="4h",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.0005"),
        slippage_rate=Decimal("0.0002"),
        seed=7,
    )
    plan = build_backtest_replay_plan(
        WORKSPACE_ID, uuid5(WORKSPACE_ID, "h02-plan"), bundle_rev, sub
    )
    plan_comp = plan.executor(bundle_rev, plan.manifest)
    assert plan_comp.is_complete
    assert plan_comp.runs[0].sensitivity_run.result.trade_count == 1
    assert plan_comp.runs[0].trade_outcomes[0].closed_at == trade.closed_at

    # 2. Tín hiệu tiếp diễn cùng chiều LONG -> KHÔNG đóng nhầm vị thế LONG
    bundle_same, _ = _build_h02_bundle("same_long")
    comparison_same, gateway_same = _run_replay_with_gateway(
        bundle_same,
        release_id="h02-v1",
        execution_interval="4h",
    )
    assert comparison_same.is_complete
    assert comparison_same.runs[0].sensitivity_run.result.trade_count == 0
    assert len(gateway_same.open_positions()) == 1
    assert gateway_same.open_positions()[0].direction == "long"

    # 3. Nến sau trả NO_SIGNAL -> KHÔNG đóng nhầm vị thế LONG
    bundle_none, _ = _build_h02_bundle("no_signal")
    comparison_none, gateway_none = _run_replay_with_gateway(
        bundle_none,
        release_id="h02-v1",
        execution_interval="4h",
    )
    assert comparison_none.is_complete
    assert comparison_none.runs[0].sensitivity_run.result.trade_count == 0
    assert len(gateway_none.open_positions()) == 1
    assert gateway_none.open_positions()[0].direction == "long"


def test_h22_long_position_closes_on_opposite_short_signal_and_respects_1m_no_lookahead() -> None:
    # 1. H22 (15m + 1h) trên khung execution 15m: LONG -> nến sau phát SHORT -> đóng ở bar kế tiếp
    bundle_rev, eval_start = _build_h22_bundle(mode="reverse_short", include_1m=False)
    comparison_rev, gateway_rev = _run_replay_with_gateway(
        bundle_rev,
        release_id="h22-v1",
        execution_interval="15m",
    )
    assert comparison_rev.is_complete
    assert comparison_rev.runs[0].sensitivity_run.result.trade_count == 1
    positions = gateway_rev.positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.direction == "long"
    assert pos.is_closed is True
    assert pos.exit_reason == "OPPOSING_SIGNAL"
    assert pos.exit_reference_price == Decimal("98.5")
    # bar[eval_idx+2] = +30m..+45m -> LONG; khớp ở +45m; bar[eval_idx+3] = +45m..+60m -> SHORT;
    # đóng ở bar[eval_idx+4] = +60m..+75m (closed_at = eval_start + 75m - 1ms)
    trade = comparison_rev.runs[0].trade_outcomes[0]
    assert trade.opened_at == eval_start + timedelta(minutes=45)
    assert trade.closed_at == eval_start + timedelta(minutes=75) - timedelta(milliseconds=1)

    # 2. H22 không có tín hiệu ngược chiều -> giữ nguyên vị thế LONG
    bundle_hold, _ = _build_h22_bundle(mode="hold", include_1m=False)
    comparison_hold, gateway_hold = _run_replay_with_gateway(
        bundle_hold,
        release_id="h22-v1",
        execution_interval="15m",
    )
    assert comparison_hold.is_complete
    assert comparison_hold.runs[0].sensitivity_run.result.trade_count == 0
    assert len(gateway_hold.open_positions()) == 1
    assert gateway_hold.open_positions()[0].direction == "long"

    # 3. H22 trên execution_interval = 1m: mọi nến 1m trước 100:59:59.999 KHÔNG được nhìn trước,
    # nến 1m đầu tiên mở lúc 101:00:00 mới thấy tín hiệu SHORT và đóng bằng OPPOSING_SIGNAL!
    bundle_1m, _ = _build_h22_bundle(mode="reverse_short", include_1m=True)
    comparison_1m, gateway_1m = _run_replay_with_gateway(
        bundle_1m,
        release_id="h22-v1",
        execution_interval="1m",
    )
    assert comparison_1m.is_complete
    assert comparison_1m.runs[0].sensitivity_run.result.trade_count == 1
    pos_1m = gateway_1m.positions()[0]
    assert pos_1m.exit_reason == "OPPOSING_SIGNAL"
    trade_1m = comparison_1m.runs[0].trade_outcomes[0]
    # Nến 15m đóng lúc 100:59:59.999 (eval_start + 60m - 1ms), nên nến 1m đầu tiên mở sau mốc này
    # là 101:00:00..101:01:00 (eval_start + 61m - 1ms).
    assert trade_1m.closed_at == eval_start + timedelta(minutes=61) - timedelta(milliseconds=1)


def test_h22_with_injected_no_signal_check_does_not_skip_when_ready_features_missing() -> None:
    """Đảm bảo nếu observation có FeatureQuality != READY thì không phát tín hiệu giả."""
    bundle, eval_start = _build_h02_bundle("reverse_short")
    observations = build_backtest_dataset_replay_observations(
        bundle,
        strategy_release_id="h02-v1",
        max_bars_per_timeframe=REPLAY_MAX_BARS_PER_TIMEFRAME,
    )
    degraded_features = tuple(
        replace(feat, quality=FeatureQuality.MISSING, value=None)
        if feat.name == "realized_volatility_20"
        else feat
        for feat in observations[2].snapshot.features
    )
    degraded_obs = replace(
        observations[2],
        snapshot=replace(observations[2].snapshot, features=degraded_features),
    )
    selection = StrategySelection(
        settings_version_id="settings-opposing-degraded",
        active_release_ids=("h02-v1",),
        published=True,
    )
    provider = build_point_in_time_opposing_provider(
        strategy_registry(),
        selection,
        open_direction="long",
    )
    bar_after = PaperBar(
        bar_key="b-degraded",
        symbol="BTCUSDT",
        opened_at=eval_start + timedelta(hours=12),
        closed_at=eval_start + timedelta(hours=16) - timedelta(milliseconds=1),
        available_at=eval_start + timedelta(hours=16) - timedelta(milliseconds=1),
        open=Decimal("96"),
        high=Decimal("97"),
        low=Decimal("95"),
        close=Decimal("96"),
    )
    assert provider(bar_after, degraded_obs) is False


def test_h02_short_position_closes_on_opposite_long_signal() -> None:
    """Đối xứng: vị thế SHORT của `h02-v1` đóng bằng `OPPOSING_SIGNAL` khi nến sau phát LONG."""
    bundle, eval_start = _build_h02_bundle("short_to_long")
    comparison, gateway = _run_replay_with_gateway(
        bundle,
        release_id="h02-v1",
        execution_interval="4h",
    )
    assert comparison.is_complete
    assert comparison.runs[0].sensitivity_run.result.trade_count == 1
    positions = gateway.positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.direction == "short"
    assert pos.is_closed is True
    assert pos.exit_reason == "OPPOSING_SIGNAL"
    assert pos.exit_reference_price == Decimal("103.5")
    trade = comparison.runs[0].trade_outcomes[0]
    assert trade.opened_at == eval_start + timedelta(hours=4)
    assert trade.closed_at == eval_start + timedelta(hours=16) - timedelta(milliseconds=1)


def test_opposing_provider_respects_gateway_recipe_flag() -> None:
    """Khi vị thế mở có `opposite_signal_exit=False` thì provider không kích hoạt thoát ngược."""
    from trading_bot.domain.risk.planner import TradePlan, TradePlanner, calculate_plan_hash
    from trading_bot.domain.strategy.contracts import StrategyCandidate
    from trading_bot.domain.strategy.recipes import ExitRecipe

    bundle, eval_start = _build_h02_bundle("reverse_short")
    observations = build_backtest_dataset_replay_observations(
        bundle,
        strategy_release_id="h02-v1",
        max_bars_per_timeframe=REPLAY_MAX_BARS_PER_TIMEFRAME,
    )
    registry = strategy_registry()
    release = registry.get_release("h02-v1")
    selection = StrategySelection(
        settings_version_id="settings-recipe-check",
        active_release_ids=("h02-v1",),
        published=True,
    )
    rule = registry.resolve("h02-v1", selection)
    candidate = rule.evaluate(replace(observations[0].snapshot, position_is_flat=True), selection)
    assert isinstance(candidate, StrategyCandidate)

    step = Decimal("0.01")
    equity = Decimal("10000")
    planner = PlannerSettings(
        workspace_id=str(WORKSPACE_ID),
        settings_version_id="settings-recipe-check",
        risk_profile_version_id="risk-recipe-check",
        published=True,
        active_release_ids=("h02-v1",),
        leverage=Decimal("1"),
        exchange=ExchangeRules(step, step, Decimal("1"), Decimal("5"), equity, equity / step),
        limits=RiskLimits(
            per_trade_risk_fraction=Decimal("0.01"),
            max_aggregate_open_risk=Decimal("200"),
            max_notional=equity,
            max_leverage=Decimal("1"),
            max_positions=1,
            max_symbol_notional=equity,
            max_concentration_fraction=Decimal("1"),
            max_daily_loss=equity,
            max_rolling_loss=equity,
            max_account_age=timedelta(days=1),
            max_data_age=timedelta(hours=12),
        ),
        release_content_hashes=(("h02-v1", release.content_hash),),
    )
    account = AccountSnapshot(
        workspace_id=str(WORKSPACE_ID),
        account_id="acc-recipe-check",
        settings_version_id="settings-recipe-check",
        captured_at=observations[0].snapshot.decision_time,
        equity=equity,
        available_margin=equity,
        is_known=True,
    )
    costs = CostModel(
        version_id="paper-fill-v1",
        entry_fee_rate=Decimal("0.0005"),
        exit_fee_rate=Decimal("0.0005"),
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=Decimal("0.0002"),
        exit_slippage_rate=Decimal("0.0002"),
    )
    plan = TradePlanner().plan(
        candidate,
        planner,
        account,
        (),
        costs,
        now=observations[0].snapshot.decision_time + timedelta(microseconds=1),
    )
    assert isinstance(plan, TradePlan)
    recipe_disabled = ExitRecipe("no-opp-exit", stop_atr_multiple=3.0, opposite_signal_exit=False)
    plan_disabled = replace(plan, exit_recipe=recipe_disabled, plan_hash="")
    plan_disabled = replace(plan_disabled, plan_hash=calculate_plan_hash(plan_disabled))

    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-fill-v1",
            latency=timedelta(0),
            spread_rate=Decimal("0"),
            slippage_rate=Decimal("0.0002"),
            fee_rate=Decimal("0.0005"),
        )
    )
    gateway.submit(
        plan_disabled,
        "k-recipe-check",
        submitted_at=observations[0].snapshot.decision_time + timedelta(microseconds=1),
        eligible_after=candidate.eligible_after,
    )
    entry_at = eval_start + timedelta(hours=4)
    gateway.apply_market_event(
        PaperBar(
            bar_key="fill-1",
            symbol="BTCUSDT",
            opened_at=entry_at,
            closed_at=entry_at + timedelta(hours=4) - timedelta(milliseconds=1),
            available_at=entry_at + timedelta(hours=4) - timedelta(milliseconds=1),
            open=Decimal("105"),
            high=Decimal("106"),
            low=Decimal("102"),
            close=Decimal("104.5"),
        )
    )
    assert len(gateway.open_positions()) == 1

    provider = build_point_in_time_opposing_provider(
        registry,
        selection,
        observations=observations,
        gateway=gateway,
    )
    bar_after_short = PaperBar(
        bar_key="bar-after",
        symbol="BTCUSDT",
        opened_at=eval_start + timedelta(hours=12),
        closed_at=eval_start + timedelta(hours=16) - timedelta(milliseconds=1),
        available_at=eval_start + timedelta(hours=16) - timedelta(milliseconds=1),
        open=Decimal("96"),
        high=Decimal("97"),
        low=Decimal("95"),
        close=Decimal("96.5"),
    )
    assert provider(bar_after_short, observations[2]) is False

    open_pos = gateway.open_positions()[0]
    gateway._positions[open_pos.intent_id] = replace(
        open_pos,
        recipe=replace(recipe_disabled, opposite_signal_exit=True),
    )
    assert provider(bar_after_short, observations[2]) is True
