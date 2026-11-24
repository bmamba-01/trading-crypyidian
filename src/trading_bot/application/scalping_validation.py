"""Chạy kiểm định scalping SIG-5: replay PAPER theo release × kịch bản chi phí → báo cáo + kết luận.

Không gọi mạng/sàn: chỉ nhận bundle replay đã kiểm quyền (cùng đường ``run_backtest_dataset_runtime_
replay`` của BT-1) và registry sản phẩm (có thang chốt lời của H18–H20).

Hồ sơ chi phí ``sig5-costs-v1``:

- **Gốc (``base``)**: phí taker VIP0 0,05% mỗi chiều, không giảm BNB (quyết định owner #4);
  trượt giá cơ sở ``BASE_SLIPPAGE_RATE`` = 0,02% mỗi lần khớp, spread 0 (đã nằm trong trượt giá);
  khớp ở giá mở nến kế tiếp. Trượt giá cơ sở là **giả định của dự án** cho lệnh thị trường cỡ
  nhỏ trên BTC/ETH perp, chưa đo từ sổ lệnh — owner có thể đổi; đổi = lượt thử mới.
- **Xấu (``stress``)**: trượt giá ×2 (0,04%) và chỉ khớp taker. Lát này mọi lệnh vào/ra đều là
  taker, nên "chỉ taker" trùng phí gốc; khi có mô phỏng limit maker (chưa làm) kịch bản xấu vẫn ép
  taker.

Planner nhận cùng phí/trượt giá của kịch bản để cổng R:R ròng phản ánh đúng chi phí. Mỗi
snapshot chỉ thấy ``PRODUCER_MAX_BARS_PER_STREAM`` nến gần nhất mỗi khung như producer live.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from trading_bot.application.backtest import ReplayManifest, ReplayRejected
from trading_bot.application.backtest_dataset_replay_bundle import BacktestDatasetReplayBundle
from trading_bot.application.backtest_dataset_replay_context import (
    run_backtest_dataset_runtime_replay,
)
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.market_signal_composition import PRODUCER_MAX_BARS_PER_STREAM
from trading_bot.application.runtime_replay_evaluator import (
    ReplayEvaluationContext,
    RuntimePaperReplayComparison,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import (
    entry_timeframe_for_release,
    feature_requirements_for_release,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.portfolio.scalping_validation import (
    ScalpTrade,
    paper_enable_verdict,
    setup_report,
    walk_forward_folds,
)
from trading_bot.domain.risk.engine import RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    PlannerSettings,
    RiskLimits,
)

COSTS_VERSION = "sig5-costs-v1"
TAKER_FEE_RATE = Decimal("0.0005")
BASE_SLIPPAGE_RATE = Decimal("0.0002")
STRESS_SLIPPAGE_MULTIPLIER = Decimal("2")
FILL_MODEL_VERSION = "paper-fill-v1"


@dataclass(frozen=True, slots=True)
class CostScenario:
    scenario_id: str
    fee_rate: Decimal
    slippage_rate: Decimal


BASE = CostScenario("base", TAKER_FEE_RATE, BASE_SLIPPAGE_RATE)
STRESS = CostScenario(
    "stress-slippage-x2-taker", TAKER_FEE_RATE, BASE_SLIPPAGE_RATE * STRESS_SLIPPAGE_MULTIPLIER
)


@dataclass(frozen=True, slots=True)
class ScalpingValidationConfig:
    """Thông số tài khoản/sàn cố định của lượt kiểm định; mặc định cho BTC/ETH perp."""

    workspace_id: UUID
    equity: Decimal = Decimal("10000")
    leverage: Decimal = Decimal("2")
    exchange: ExchangeRules = field(
        default_factory=lambda: ExchangeRules(
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            contract_multiplier=Decimal("1"),
            min_notional=Decimal("5"),
            max_notional=Decimal("10000"),
            max_quantity=Decimal("1000"),
        )
    )
    per_trade_risk_fraction: Decimal = Decimal("0.005")
    folds: int = 4
    embargo: timedelta = timedelta(hours=1)
    execution_interval: str = "1m"
    # Cùng cửa sổ producer production → đặc trưng/mức thanh khoản của replay khớp PAPER live.
    max_bars_per_timeframe: int = PRODUCER_MAX_BARS_PER_STREAM


def _cost_model(scenario: CostScenario) -> CostModel:
    return CostModel(
        version_id=f"{COSTS_VERSION}:{scenario.scenario_id}",
        entry_fee_rate=scenario.fee_rate,
        exit_fee_rate=scenario.fee_rate,
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=scenario.slippage_rate,
        exit_slippage_rate=scenario.slippage_rate,
    )


def _sensitivity(scenario: CostScenario) -> SensitivityScenario:
    if scenario == BASE:
        return SensitivityScenario("base", "BASE")
    # BT-1 chỉ nhận override làm đổi giả định gốc: chỉ ghi khóa khác kịch bản gốc.
    overrides = tuple(
        (key, str(value))
        for key, value, base in (
            ("entry_fee_rate", scenario.fee_rate, BASE.fee_rate),
            ("exit_fee_rate", scenario.fee_rate, BASE.fee_rate),
            ("entry_slippage_rate", scenario.slippage_rate, BASE.slippage_rate),
            ("exit_slippage_rate", scenario.slippage_rate, BASE.slippage_rate),
        )
        if value != base
    )
    return SensitivityScenario(scenario.scenario_id, "COST", cost_overrides=overrides)


def run_release_replay(
    bundle: BacktestDatasetReplayBundle,
    *,
    release_id: str,
    config: ScalpingValidationConfig,
    scenarios: Sequence[CostScenario] = (BASE, STRESS),
) -> tuple[RuntimePaperReplayComparison, dict[str, PaperExecutionGateway]]:
    """Replay PAPER một release trên bundle ở các kịch bản chi phí; trả gateway theo kịch bản."""
    by_sensitivity = {_sensitivity(item).scenario_id: item for item in scenarios}
    registry = strategy_registry()
    release = registry.get_release(release_id)
    entry_timeframe = entry_timeframe_for_release(release_id)
    if entry_timeframe is None:
        raise ValueError(f"Release chưa có timeframe quyết định: {release_id}")
    available_intervals = {item.manifest.interval for item in bundle.components}
    effective_exec_tf = (
        config.execution_interval
        if config.execution_interval in available_intervals
        else entry_timeframe
    )
    timeframes = sorted(
        {*feature_requirements_for_release(release_id), effective_exec_tf},
        key=lambda item: int(item[:-1]) * {"m": 1, "h": 60}[item[-1]],
    )
    confirmation = next(
        (tf for tf in timeframes if tf not in {entry_timeframe, effective_exec_tf}),
        entry_timeframe,
    )
    workspace_id = config.workspace_id
    settings_id = uuid5(workspace_id, f"sig5:{release_id}:settings")
    risk_id = uuid5(workspace_id, f"sig5:{release_id}:risk")
    settings_hash = hashlib.sha256(f"sig5:{release_id}".encode()).hexdigest()
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
                    "symbols": [bundle.symbol],
                    "timeframes": timeframes,
                    "max_age_seconds": 3600,
                },
                "features": {
                    "formula_version": "crypidian-features-v1",
                    "sessions_timezone": "UTC",
                },
                "strategy": {
                    "default_release": release_id,
                    "parameters": {},
                    "entry_timeframe": entry_timeframe,
                    "confirmation_timeframe": confirmation,
                },
                "risk": {"profile_version_id": str(risk_id)},
                "paper_execution": {
                    "quote_asset": "USDT",
                    "starting_equity": str(config.equity),
                    "fee_schedule_version": COSTS_VERSION,
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
        active_release_ids=(release_id,),
        leverage=config.leverage,
        exchange=config.exchange,
        limits=RiskLimits(
            per_trade_risk_fraction=config.per_trade_risk_fraction,
            max_aggregate_open_risk=config.equity * Decimal("0.05"),
            max_notional=config.exchange.max_notional,
            max_leverage=Decimal("5"),
            max_positions=1,
            max_symbol_notional=config.exchange.max_notional,
            max_concentration_fraction=Decimal("1"),
            # Kiểm định không cắt lệnh theo lỗ ngày để mẫu không phụ thuộc thứ tự lệnh.
            max_daily_loss=config.equity,
            max_rolling_loss=config.equity,
            max_account_age=timedelta(days=3660),
            max_data_age=timedelta(days=3660),
        ),
        release_content_hashes=((release_id, release.content_hash),),
        settings_hash=settings_hash,
    )
    start = bundle.evaluation_start
    account = AccountSnapshot(
        workspace_id=str(workspace_id),
        account_id=f"sig5:{release_id}",
        settings_version_id=str(settings_id),
        captured_at=start - timedelta(seconds=1),
        equity=config.equity,
        available_margin=config.equity,
        is_known=True,
    )
    risk_state = RiskState(
        workspace_id=str(workspace_id),
        account_id=account.account_id,
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        evaluated_at=start,
        account_captured_at=account.captured_at,
        market_data_as_of=start,
        equity=config.equity,
        available_margin=config.equity,
        open_exposures=(),
        realized_loss_today=Decimal("0"),
        realized_loss_rolling=Decimal("0"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=Decimal("0"),
    )
    seed = ReplayEvaluationContext(
        published_settings=published,
        evaluated_at=start + timedelta(microseconds=1),
        planner_settings=planner_settings,
        account=account,
        risk_state=risk_state,
        open_exposures=(),
        cost_model=_cost_model(BASE),
        feature_schema_hash=hashlib.sha256(b"crypidian-features-v1").hexdigest(),
        pipeline_content_hash=hashlib.sha256(COSTS_VERSION.encode()).hexdigest(),
        execution_events=(),
        reserve_risk=lambda decision, key: decision.approved and bool(key),
    )
    gateways: dict[str, PaperExecutionGateway] = {}

    def seed_factory(scenario: SensitivityScenario, _manifest: ReplayManifest) -> Any:
        return replace(seed, cost_model=_cost_model(by_sensitivity[scenario.scenario_id]))

    def gateway_factory(scenario: SensitivityScenario, _manifest: ReplayManifest) -> Any:
        costs = by_sensitivity[scenario.scenario_id]
        gateway = PaperExecutionGateway(
            PaperExecutionPolicy(
                version_id=FILL_MODEL_VERSION,
                latency=timedelta(0),
                spread_rate=Decimal("0"),
                slippage_rate=costs.slippage_rate,
                fee_rate=costs.fee_rate,
            )
        )
        gateways[scenario.scenario_id] = gateway
        return gateway

    base_costs = _cost_model(BASE)
    manifest = ReplayManifest(
        dataset_hash=bundle.dataset_hash,
        range_start=bundle.evaluation_start,
        range_end=bundle.range_end,
        settings_version_id=str(settings_id),
        risk_profile_version_id=str(risk_id),
        strategy_release_id=release_id,
        strategy_content_hash=release.content_hash,
        feature_schema_hash=seed.feature_schema_hash,
        fill_model_version=FILL_MODEL_VERSION,
        pipeline_content_hash=seed.pipeline_content_hash,
        cost_assumptions=(
            ("entry_fee_rate", str(base_costs.entry_fee_rate)),
            ("exit_fee_rate", str(base_costs.exit_fee_rate)),
            ("adverse_funding_per_unit", "0"),
            ("entry_slippage_rate", str(base_costs.entry_slippage_rate)),
            ("exit_slippage_rate", str(base_costs.exit_slippage_rate)),
            ("spread_rate", "0"),
        ),
        # Tham số rule khóa theo release → họ tham số = hash nội dung release.
        parameter_family_hash=release.content_hash,
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
        execution_interval=effective_exec_tf,
        context_seed_factory=seed_factory,
        gateway_factory=gateway_factory,
        scenarios=tuple(_sensitivity(item) for item in scenarios),
        max_bars_per_timeframe=config.max_bars_per_timeframe,
    )
    return comparison, gateways


def scalp_trades(
    comparison: RuntimePaperReplayComparison,
    gateways: dict[str, PaperExecutionGateway],
    *,
    release_id: str,
    symbol: str,
) -> dict[str, list[ScalpTrade]]:
    """Ghép trade đã đóng của replay với vị thế/lệnh PAPER để có rủi ro ban đầu của từng lệnh."""
    result: dict[str, list[ScalpTrade]] = {}
    for run in comparison.runs:
        scenario_id = run.sensitivity_run.scenario.scenario_id
        if isinstance(run.sensitivity_run.result, ReplayRejected):
            # Replay bị từ chối không có outcome; không được báo cáo như "0 lệnh".
            raise ValueError(
                f"Replay kịch bản {scenario_id} bị từ chối: "
                f"{getattr(run.sensitivity_run.result, 'reason_code', '?')}"
            )
        gateway = gateways[scenario_id]
        positions = {position.position_id: position for position in gateway.positions()}
        trades: list[ScalpTrade] = []
        for outcome in run.trade_outcomes:
            position = positions[outcome.trade_id]
            order = gateway.order(position.intent_id)
            quantity = position.initial_quantity or order.filled_quantity
            risk = (
                abs(position.entry_execution_price - order.stop_price)
                * quantity
                * position.contract_multiplier
            )
            trades.append(
                ScalpTrade(
                    trade_id=outcome.trade_id,
                    setup_id=release_id,
                    symbol=symbol,
                    opened_at=outcome.opened_at,
                    closed_at=outcome.closed_at,
                    net_pnl=outcome.gross_pnl - outcome.fees - outcome.funding - outcome.slippage,
                    fees=outcome.fees,
                    risk_amount=risk,
                )
            )
        result[scenario_id] = trades
    return result


def validate_release(
    bundle: BacktestDatasetReplayBundle,
    *,
    release_id: str,
    config: ScalpingValidationConfig,
) -> dict[str, Any]:
    """Báo cáo kịch bản gốc + xấu và kết luận tiêu chí #5 cho một release trên một bundle."""
    return validate_release_chunks((bundle,), release_id=release_id, config=config)


def validate_release_chunks(
    bundles: Iterable[BacktestDatasetReplayBundle],
    *,
    release_id: str,
    config: ScalpingValidationConfig,
) -> dict[str, Any]:
    """Như ``validate_release`` nhưng replay từng đoạn liền nhau rồi gộp lệnh (giới hạn bộ nhớ).

    Mỗi bundle tự mang lịch sử khởi động trước ``evaluation_start`` của nó; khoảng đánh giá các
    đoạn phải nối liền (``range_end`` đoạn trước = ``evaluation_start`` đoạn sau, cùng mã). Mỗi
    đoạn khởi động với tài khoản phẳng; lệnh còn mở lúc hết đoạn theo quy tắc kết thúc replay.
    Bundle được duyệt lần lượt, nên có thể truyền generator để chỉ giữ một đoạn trong bộ nhớ.
    """
    trades: dict[str, list[ScalpTrade]] = {}
    dataset_hashes: list[str] = []
    symbol: str | None = None
    evaluation_start = range_end = None
    for bundle in bundles:
        if symbol is None:
            symbol, evaluation_start = bundle.symbol, bundle.evaluation_start
        elif bundle.symbol != symbol or bundle.evaluation_start != range_end:
            raise ValueError("Các đoạn kiểm định phải cùng mã và liền nhau theo thời gian.")
        comparison, gateways = run_release_replay(bundle, release_id=release_id, config=config)
        chunk = scalp_trades(comparison, gateways, release_id=release_id, symbol=bundle.symbol)
        for scenario_id, items in chunk.items():
            trades.setdefault(scenario_id, []).extend(items)
        dataset_hashes.append(bundle.dataset_hash)
        range_end = bundle.range_end
    if symbol is None or evaluation_start is None or range_end is None:
        raise ValueError("Cần ít nhất một đoạn dữ liệu để kiểm định.")
    for scenario_id, items in trades.items():
        if len({item.trade_id for item in items}) != len(items):
            raise ValueError(f"Trùng mã lệnh giữa các đoạn ở kịch bản {scenario_id}.")
    folds = walk_forward_folds(
        evaluation_start, range_end, folds=config.folds, embargo=config.embargo
    )
    reports = {
        scenario_id: setup_report(release_id, items, folds=folds)
        for scenario_id, items in trades.items()
    }
    verdict = paper_enable_verdict(
        base=reports[BASE.scenario_id], stress=reports[STRESS.scenario_id]
    )
    body = {
        "costs_version": COSTS_VERSION,
        "release_id": release_id,
        "release_content_hash": strategy_registry().get_release(release_id).content_hash,
        "dataset_hashes": dataset_hashes,
        "symbol": symbol,
        "evaluation_start": evaluation_start.isoformat(),
        "range_end": range_end.isoformat(),
        "scenarios": {
            item.scenario_id: {
                "fee_rate": str(item.fee_rate),
                "slippage_rate": str(item.slippage_rate),
            }
            for item in (BASE, STRESS)
        },
        "reports": reports,
        "verdict": verdict,
    }
    body["validation_hash"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


# Điều khoản nguồn dữ liệu REST công khai của Binance; owner xác nhận quyền dùng cho nghiên cứu.
BINANCE_TERMS_URL = "https://www.binance.com/en/terms"
RESEARCH_POLICY_NOTE = b"sig5-binance-usdm-rest-public-klines-research-only"


def build_research_bundle(
    bars_by_timeframe: dict[str, Sequence[Any]],
    *,
    workspace_id: UUID,
    symbol: str,
    start: Any,
    end: Any,
    evaluation_start: Any,
    checked_at: Any,
) -> BacktestDatasetReplayBundle:
    """Bundle replay đa khung từ nến REST đã lưu cục bộ (không gọi mạng)."""
    from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
    from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
    from trading_bot.application.backtest_dataset_replay_bundle import (
        build_backtest_dataset_replay_bundle,
    )
    from trading_bot.application.binance_rest_backfill import (
        BackfillRightsEvidence,
        BackfillRightsPurpose,
        RestBackfillRequest,
    )

    rights = BackfillRightsEvidence(
        workspace_id=workspace_id,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url=BINANCE_TERMS_URL,
        policy_fingerprint=hashlib.sha256(RESEARCH_POLICY_NOTE).hexdigest(),
        verified_at=checked_at,
        checked_at=checked_at,
        valid_until=checked_at + timedelta(days=30),
    )
    inputs = []
    for timeframe in sorted(bars_by_timeframe):
        bars = tuple(sorted(bars_by_timeframe[timeframe], key=lambda bar: bar.open_time))
        request = RestBackfillRequest(
            symbol=symbol,
            interval=timeframe,
            start=start,
            end=end,
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        )
        interval = bars[1].open_time - bars[0].open_time if len(bars) > 1 else timedelta(0)
        manifest = build_backtest_dataset_manifest(
            request,
            evaluation_start=evaluation_start,
            warmup_bars=int((evaluation_start - start) / interval) if interval else 0,
            bars=bars,
            rights_evidence=rights,
            prepared_at=end,
            schema_version=3,
        )
        manifest.require_ready()
        inputs.append(BacktestDatasetReplayInput(manifest, bars, rights))
    return build_backtest_dataset_replay_bundle(tuple(inputs))
