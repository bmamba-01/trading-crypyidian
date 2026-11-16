"""Fixture SIG-4: bundle M5/M15 tổng hợp cho H18 chạy qua replay PAPER (unit và Compose E2E).

Thời gian lấy từ dữ liệu (Thứ Hai 02/11/2026, phiên London), nên kết quả không phụ thuộc đồng hồ
máy: đây là "đồng hồ có kiểm soát" cho E2E của bộ lọc phiên/funding. Kịch bản: phiên Á
(00:00–09:00 UTC) có đỉnh 100,5; nến M5 10:00 quét lên 101 rồi đóng 100,1 với delta âm → H18
SHORT; giá rơi chạm TP1 (1R, chốt 50%, dời stop về hòa vốn) rồi TP2 (2R) → đóng ``TAKE_PROFIT``.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

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
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.runtime_replay_evaluator import (
    ReplayEvaluationContext,
    RuntimePaperReplayComparison,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.risk.engine import RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    PlannerSettings,
    RiskLimits,
)

RELEASE_ID = "h18-v1"
DAY = datetime(2026, 11, 2, tzinfo=UTC)
# Bộ tính đặc trưng chung đòi đủ warmup mọi đặc trưng (EMA200) → cần ≥ 200 nến M5 trước quyết định.
START = DAY - timedelta(hours=12)
EVALUATION_START = DAY + timedelta(hours=9, minutes=30)
RANGE_END = DAY + timedelta(hours=11)
# Quyết định owner #4: VIP0 taker 0,05% hai chiều.
TAKER_FEE = "0.0005"

# (open, high, low, close, taker_buy) cho nến M5 từ 10:00 UTC; sau đó đi ngang ở 98,3.
_PATH: tuple[tuple[float, float, float, float, float], ...] = (
    (100.0, 101.0, 99.9, 100.1, 4.0),  # 10:00 quét đỉnh phiên Á 100,5, đóng quay vào, delta âm
    (100.1, 100.2, 99.9, 100.0, 5.0),  # 10:05 khớp lệnh vào ở giá mở
    (100.0, 100.0, 99.0, 99.2, 3.0),  # 10:10 chạm TP1
    (99.2, 99.2, 98.0, 98.3, 3.0),  # 10:15 chạm TP2
)


def _bar(
    timeframe: str, interval: timedelta, opened: datetime, ohlc: tuple[float, ...]
) -> OHLCVBar:
    open_price, high, low, close, taker = ohlc
    closed = opened + interval - timedelta(milliseconds=1)
    cursor = f"sig4:{timeframe}:{opened.isoformat()}"
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        event_at=closed,
        received_at=closed,
        available_at=closed,
        open=Decimal(str(open_price)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("10"),
        quote_volume=Decimal(str(close * 10)),
        taker_buy_base_volume=Decimal(str(taker)),
        taker_buy_quote_volume=Decimal(str(taker * close)),
        trade_count=100,
        is_closed=True,
        source="BINANCE_USDM_REST_BACKFILL",
        source_cursor=cursor,
        checksum=hashlib.sha256(cursor.encode()).hexdigest(),
    )


def same_slot_after(moment: datetime) -> datetime:
    """Ngày quyết định cùng thứ/giờ với ``DAY`` nhưng sau ``moment`` ít nhất một ngày.

    Dời nguyên tuần nên phiên London (10:05 UTC vẫn trong phiên cả khi London dùng giờ mùa hè) và
    mốc funding không đổi; dùng khi ràng buộc DB so với ``now()`` thật (vd. hạn reservation).
    """
    weeks = max(0, (moment - DAY).days // 7 + 1)
    return DAY + timedelta(weeks=weeks)


def sig4_bars(timeframe: str = "5m", *, day: datetime = DAY) -> tuple[OHLCVBar, ...]:
    """Nến tổng hợp 12:00 UTC hôm trước → 11:00 UTC; ``index`` tính từ 00:00 ngày quyết định."""
    interval = timedelta(minutes=5) if timeframe == "5m" else timedelta(minutes=15)
    first = int((START - DAY) / interval)
    count = int((RANGE_END - DAY) / interval)
    bars: list[OHLCVBar] = []
    path_start = int(timedelta(hours=10) / interval)
    for index in range(first, count):
        opened = day + interval * index
        if timeframe == "5m" and path_start <= index < path_start + len(_PATH):
            ohlc: tuple[float, ...] = _PATH[index - path_start]
        elif timeframe == "5m" and index >= path_start + len(_PATH):
            ohlc = (98.3, 98.5, 98.1, 98.3, 5.0)
        else:
            high = 100.5 if timeframe == "5m" and index == 48 else 100.2
            ohlc = (100.0, high, 99.8, 99.9 + (index % 2) * 0.2, 5.0 + (index % 3) * 0.2)
        bars.append(_bar(timeframe, interval, opened, ohlc))
    return tuple(bars)


def build_sig4_bundle(workspace_id: UUID) -> BacktestDatasetReplayBundle:
    rights = BackfillRightsEvidence(
        workspace_id=workspace_id,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/synthetic-replay-terms",
        policy_fingerprint=hashlib.sha256(b"synthetic-replay-policy").hexdigest(),
        verified_at=START - timedelta(days=1),
        checked_at=START - timedelta(hours=1),
        valid_until=RANGE_END + timedelta(days=1),
    )
    bars = sig4_bars("5m")
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="5m",
        start=START,
        end=RANGE_END,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    manifest = build_backtest_dataset_manifest(
        request,
        evaluation_start=EVALUATION_START,
        warmup_bars=int((EVALUATION_START - START) / timedelta(minutes=5)),
        bars=bars,
        rights_evidence=rights,
        prepared_at=RANGE_END,
        schema_version=3,
    )
    manifest.require_ready()
    return build_backtest_dataset_replay_bundle(
        (BacktestDatasetReplayInput(manifest, bars, rights),)
    )


def build_h18_dataset_runtime_replay(
    workspace_id: UUID,
    *,
    bundle: BacktestDatasetReplayBundle | None = None,
    scenarios: tuple[SensitivityScenario, ...] | None = None,
    gateways: list[PaperExecutionGateway] | None = None,
) -> tuple[BacktestDatasetReplayBundle, RuntimePaperReplayComparison]:
    """Chạy bundle M5 qua replay PAPER với registry sản phẩm (có thang chốt lời H18).

    ``gateways`` (tùy chọn) nhận gateway PAPER của từng scenario để test soi vị thế/fill.
    """
    bundle = bundle or build_sig4_bundle(workspace_id)
    registry = strategy_registry()
    release = registry.get_release(RELEASE_ID)
    settings_id = uuid5(workspace_id, "sig4-replay-settings-v1")
    risk_id = uuid5(workspace_id, "sig4-replay-risk-v1")
    settings_hash = hashlib.sha256(b"sig4-replay-settings").hexdigest()
    feature_hash = "c" * 64
    pipeline_hash = "d" * 64
    published = PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=settings_id,
        settings_hash=settings_hash,
        settings=WorkspaceSettings.model_validate(
            {
                "schema_version": 1,
                "runtime": {"mode": "PAPER", "timezone": "UTC"},
                "market_data": {
                    "venue": "BINANCE_USDM",
                    "symbols": ["BTCUSDT"],
                    "timeframes": ["5m", "15m"],
                    "max_age_seconds": 900,
                },
                "features": {"formula_version": "synthetic-v1", "sessions_timezone": "UTC"},
                "strategy": {
                    "default_release": RELEASE_ID,
                    "parameters": {},
                    "entry_timeframe": "5m",
                    "confirmation_timeframe": "15m",
                },
                "risk": {"profile_version_id": str(risk_id)},
                "paper_execution": {
                    "quote_asset": "USDT",
                    "starting_equity": "10000",
                    "fee_schedule_version": "vip0-taker-v1",
                    "fill_model": "next_bar_with_adverse_costs",
                },
            }
        ),
    )
    planner_settings = PlannerSettings(
        workspace_id=str(workspace_id),
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        published=True,
        active_release_ids=(RELEASE_ID,),
        leverage=Decimal("2"),
        exchange=ExchangeRules(
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            contract_multiplier=Decimal("1"),
            min_notional=Decimal("5"),
            max_notional=Decimal("10000"),
            max_quantity=Decimal("1000"),
        ),
        limits=RiskLimits(
            per_trade_risk_fraction=Decimal("0.01"),
            max_aggregate_open_risk=Decimal("250"),
            max_notional=Decimal("10000"),
            max_leverage=Decimal("5"),
            max_positions=2,
            max_symbol_notional=Decimal("5000"),
            max_concentration_fraction=Decimal("0.5"),
            max_daily_loss=Decimal("200"),
            max_rolling_loss=Decimal("500"),
            max_account_age=timedelta(days=1),
            max_data_age=timedelta(hours=8),
        ),
        release_content_hashes=((RELEASE_ID, release.content_hash),),
        settings_hash=settings_hash,
    )
    account_time = EVALUATION_START - timedelta(seconds=1)
    account = AccountSnapshot(
        workspace_id=str(workspace_id),
        account_id="paper-sig4-fixture",
        settings_version_id=str(settings_id),
        captured_at=account_time,
        equity=Decimal("10000"),
        available_margin=Decimal("10000"),
        is_known=True,
    )
    risk_state = RiskState(
        workspace_id=str(workspace_id),
        account_id=account.account_id,
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        evaluated_at=EVALUATION_START,
        account_captured_at=account_time,
        market_data_as_of=EVALUATION_START,
        equity=account.equity,
        available_margin=account.available_margin,
        open_exposures=(),
        realized_loss_today=Decimal("0"),
        realized_loss_rolling=Decimal("0"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=Decimal("0"),
    )
    costs = CostModel(
        version_id="vip0-taker-v1",
        entry_fee_rate=Decimal(TAKER_FEE),
        exit_fee_rate=Decimal(TAKER_FEE),
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=Decimal("0"),
        exit_slippage_rate=Decimal("0"),
    )
    seed = ReplayEvaluationContext(
        published_settings=published,
        evaluated_at=EVALUATION_START + timedelta(microseconds=1),
        planner_settings=planner_settings,
        account=account,
        risk_state=risk_state,
        open_exposures=(),
        cost_model=costs,
        feature_schema_hash=feature_hash,
        pipeline_content_hash=pipeline_hash,
        execution_events=(),
        reserve_risk=lambda decision, key: decision.approved and bool(key),
    )

    def context_seed_factory(
        scenario: SensitivityScenario, _manifest: ReplayManifest
    ) -> ReplayEvaluationContext:
        if scenario.kind != "COST":
            return seed
        overrides = dict(scenario.cost_overrides)
        return replace(
            seed,
            cost_model=replace(
                seed.cost_model,
                entry_fee_rate=Decimal(overrides.get("entry_fee_rate", TAKER_FEE)),
                exit_fee_rate=Decimal(overrides.get("exit_fee_rate", TAKER_FEE)),
            ),
        )

    fill_version = "paper-fill-v1"

    def gateway_factory(
        scenario: SensitivityScenario, _manifest: ReplayManifest
    ) -> PaperExecutionGateway:
        overrides = dict(scenario.cost_overrides)
        gateway = PaperExecutionGateway(
            PaperExecutionPolicy(
                version_id=fill_version,
                latency=timedelta(0),
                spread_rate=Decimal("0"),
                slippage_rate=Decimal(overrides.get("slippage_rate", "0")),
                fee_rate=Decimal(overrides.get("entry_fee_rate", TAKER_FEE)),
            )
        )
        if gateways is not None:
            gateways.append(gateway)
        return gateway

    manifest = ReplayManifest(
        dataset_hash=bundle.dataset_hash,
        range_start=bundle.evaluation_start,
        range_end=bundle.range_end,
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        strategy_release_id=RELEASE_ID,
        strategy_content_hash=release.content_hash,
        feature_schema_hash=feature_hash,
        fill_model_version=fill_version,
        pipeline_content_hash=pipeline_hash,
        cost_assumptions=(
            ("entry_fee_rate", TAKER_FEE),
            ("exit_fee_rate", TAKER_FEE),
            ("adverse_funding_per_unit", "0"),
            ("entry_slippage_rate", "0"),
            ("exit_slippage_rate", "0"),
            ("spread_rate", "0"),
        ),
        parameter_family_hash=hashlib.sha256(b"sig4-h18-parameters").hexdigest(),
        trial_count=1,
        trial_index=0,
        seed=7,
        learning_intent="RESEARCH",
        learning_job_id=None,
    )
    comparison = run_backtest_dataset_runtime_replay(
        bundle,
        manifest,
        strategies=registry,
        execution_interval="5m",
        context_seed_factory=context_seed_factory,
        gateway_factory=gateway_factory,
        scenarios=scenarios,
    )
    return bundle, comparison
