"""Dựng hồ sơ PAPER xác định cho job replay từ bundle đã kiểm quyền."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid5

from trading_bot.application.backtest import ReplayManifest
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundle,
    BacktestDatasetReplayBundleReference,
)
from trading_bot.application.backtest_dataset_replay_context import (
    run_backtest_dataset_runtime_replay,
)
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.runtime_replay_evaluator import (
    ReplayEvaluationContext,
    RuntimePaperReplayComparison,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_market_requirements import (
    FEATURE_CODE_HASH,
    TIMEFRAME_INTERVALS,
    entry_timeframe_for_release,
    feature_requirements_for_release,
)
from trading_bot.config.workspace_settings import (
    PublishedWorkspaceSettings,
    WorkspaceSettings,
    decimal_text,
)
from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.market.feature_snapshot import FEATURE_SCHEMA_VERSION
from trading_bot.domain.risk.engine import RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    PlannerSettings,
    RiskLimits,
)


class ReplayExecutor(Protocol):
    """Hàm thực thi đồng bộ có thể báo tiến độ và kiểm tra yêu cầu hủy."""

    def __call__(
        self,
        replay_bundle: BacktestDatasetReplayBundle,
        replay_manifest: ReplayManifest,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> RuntimePaperReplayComparison: ...


def learning_feature_schema_hash_for_bundle(
    bundle: BacktestDatasetReplayBundle, release_id: str, release_content_hash: str
) -> str:
    """Schema hash vector học của release, dựng từ snapshot ở nến quyết định đầu tiên."""
    from trading_bot.application.decision_snapshot_builder import (
        build_decision_snapshot_from_bars,
    )
    from trading_bot.application.learning_feature_vector import build_learning_feature_vector

    requirements = feature_requirements_for_release(release_id)
    decision_timeframe = entry_timeframe_for_release(release_id)
    components = {item.manifest.interval: item for item in bundle.components}
    if decision_timeframe is None or decision_timeframe not in components:
        raise ValueError("Bundle thiếu timeframe quyết định của strategy.")
    first = min(
        (
            bar
            for bar in components[decision_timeframe].bars
            if bundle.evaluation_start <= bar.open_time < bundle.range_end and bar.is_closed
        ),
        key=lambda bar: bar.open_time,
    )
    decision_time = first.available_at
    bars_by_timeframe = {}
    for timeframe in requirements:
        visible = sorted(
            (
                bar
                for bar in components[timeframe].bars
                if bar.is_closed and max(bar.close_time, bar.available_at) <= decision_time
            ),
            key=lambda bar: (max(bar.close_time, bar.available_at), bar.open_time),
        )
        bars_by_timeframe[timeframe] = tuple(visible[-REPLAY_MAX_BARS_PER_TIMEFRAME:])
    snapshot = build_decision_snapshot_from_bars(
        workspace_id=bundle.workspace_id,
        symbol=bundle.symbol,
        strategy_release_id=release_id,
        decision_time=decision_time,
        series_quality_as_of=decision_time,
        bars_by_timeframe=bars_by_timeframe,
        source_identity_required=True,
    )
    return build_learning_feature_vector(
        snapshot,
        strategy_release_id=release_id,
        strategy_release_content_hash=release_content_hash,
    ).feature_schema_hash


# CRY-236 (17/11/2026): snapshot replay chỉ thấy N nến gần nhất mỗi khung, đúng cửa sổ producer
# live (``PRODUCER_MAX_BARS_PER_STREAM``). Trước đó mỗi nến quyết định tính feature trên toàn bộ
# lịch sử → O(n²): 180 ngày 15m một coin > 25 phút và > 4 GB RAM. Đo thật BTCUSDT 10 ngày 15m:
# cùng 7 lệnh, PnL lệch < 1e-9 USDT. Đổi cửa sổ = đổi ngữ nghĩa → context provider v2 (đổi
# pipeline_content_hash nên kết quả replay cũ/mới không bị coi là cùng pipeline).
REPLAY_MAX_BARS_PER_TIMEFRAME = 256
REPLAY_CONTEXT_PROVIDER = "backtest-dataset-runtime-context-v2-window256"

_SUPPORTED_EXECUTION_INTERVALS = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}


@dataclass(frozen=True, slots=True)
class BacktestReplayPlan:
    """Manifest và hàm chạy PAPER đã ghim vào đúng request của một job."""

    manifest: ReplayManifest
    executor: ReplayExecutor


def build_backtest_replay_plan(
    workspace_id: UUID,
    job_id: UUID,
    bundle: BacktestDatasetReplayBundle,
    submission: BacktestReplayJobSubmission,
) -> BacktestReplayPlan:
    """Tạo manifest/runtime profile xác định; mọi giả định fill và risk đều tường minh.

    Hồ sơ replay dùng tài khoản PAPER mới với equity do người vận hành khai báo, risk mỗi
    lệnh do request ghim, đòn bẩy 1x, tối đa một vị thế, bước giá/khối lượng tổng hợp,
    spread và funding bằng 0, phí hai chiều bằng nhau, cùng slippage đối xứng. Đây là
    mô hình thử nghiệm nội bộ; không thay quy cách hợp đồng, funding lịch sử hay trạng
    thái tài khoản thật. H06 không nhận macro evidence nên vẫn giữ hành vi abstain khi
    điều kiện đó chưa biết. H03 không ước lượng quote volume bị thiếu; rule sẽ abstain.
    """
    _validate_request_scope(workspace_id, job_id, bundle, submission)
    registry = strategy_registry()
    release = registry.get_release(submission.strategy_release_id)
    try:
        requirements = feature_requirements_for_release(release.release_id)
    except ValueError as error:
        raise ValueError("Strategy release chưa có evaluator replay được hỗ trợ.") from error
    entry_timeframe = entry_timeframe_for_release(release.release_id)
    if entry_timeframe is None:
        raise ValueError("Strategy release chưa có timeframe quyết định được hỗ trợ.")

    available_timeframes = {item.manifest.interval for item in bundle.components}
    required_timeframes = set(requirements)
    missing = sorted(required_timeframes - available_timeframes)
    if missing:
        raise ValueError(
            "Bundle thiếu timeframe bắt buộc cho strategy: " + ", ".join(missing) + "."
        )
    if submission.execution_interval not in available_timeframes:
        raise ValueError("execution interval không có trong bundle đã lưu.")
    if submission.execution_interval not in _SUPPORTED_EXECUTION_INTERVALS:
        raise ValueError("execution interval chưa được hỗ trợ bởi cấu hình PAPER.")
    if bundle.rights_purpose != "BACKTEST_RESEARCH":
        raise PermissionError("Bundle không được cấp cho mục đích backtest nghiên cứu.")
    if any(
        item.current_rights.checked_at is None
        or item.current_rights.valid_until <= item.current_rights.checked_at
        for item in bundle.components
    ):
        raise PermissionError("Bundle thiếu lease quyền hiện hành hợp lệ.")

    equity = submission.starting_equity
    risk_fraction = submission.per_trade_risk_fraction
    fee_rate = decimal_text(submission.fee_rate)
    slippage_rate = decimal_text(submission.slippage_rate)
    profile_payload: dict[str, object] = {
        "workspace_id": str(workspace_id),
        "job_id": str(job_id),
        "bundle_hash": bundle.dataset_hash,
        "strategy_release_id": release.release_id,
        "strategy_content_hash": release.content_hash,
        "execution_interval": submission.execution_interval,
        "starting_equity": decimal_text(equity),
        "per_trade_risk_fraction": decimal_text(risk_fraction),
        "fee_rate": fee_rate,
        "slippage_rate": slippage_rate,
        "seed": submission.seed,
        "leverage": "1",
        "max_positions": 1,
        "spread_rate": "0",
        "adverse_funding_per_unit": "0",
        "exchange_rules_version": "paper-generic-increments-v1",
    }
    strategy_parameters = dict(sorted(submission.strategy_parameters))
    if strategy_parameters:
        # Chỉ thêm khi có tham số: hash profile của job cũ (không tham số) giữ nguyên.
        profile_payload["strategy_parameters"] = strategy_parameters
    profile_hash = _hash(profile_payload)
    settings_version_id = uuid5(job_id, f"settings:{profile_hash}")
    risk_profile_version_id = uuid5(job_id, f"risk:{profile_hash}")
    settings_hash = _hash(
        {
            "profile_hash": profile_hash,
            "settings_version_id": str(settings_version_id),
            "risk_profile_version_id": str(risk_profile_version_id),
        }
    )
    feature_schema_hash = _hash(
        {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "code_hash": FEATURE_CODE_HASH,
            "requirements": {
                timeframe: list(features) for timeframe, features in sorted(requirements.items())
            },
        }
    )
    if submission.learning_intent == "TRAINING_BASE":
        # AI-5 (17/11/2026): runtime so vector học của từng nến với hash này; hash kiểu cũ
        # không bao giờ khớp → mọi cơ hội bị loại FEATURE_SCHEMA_MISMATCH, 0 lệnh. Dùng đúng
        # schema hash của vector học (không phụ thuộc dữ liệu), như fixture WP-06.
        feature_schema_hash = learning_feature_schema_hash_for_bundle(
            bundle, release.release_id, release.content_hash
        )
    pipeline_content_hash = _hash(
        {
            "pipeline": "crypidian-paper-replay-v1",
            "context_provider": REPLAY_CONTEXT_PROVIDER,
            "runtime_evaluator": "runtime-paper-evaluator-v1",
            "runner": "backtest-runner-v1",
            "strategy_content_hash": release.content_hash,
        }
    )
    parameter_family_hash = _hash(
        {
            "profile_hash": profile_hash,
            "strategy_parameters": strategy_parameters,
        }
    )
    manifest = ReplayManifest(
        dataset_hash=bundle.dataset_hash,
        range_start=bundle.evaluation_start,
        range_end=bundle.range_end,
        settings_version_id=str(settings_version_id),
        risk_profile_version_id=str(risk_profile_version_id),
        strategy_release_id=release.release_id,
        strategy_content_hash=release.content_hash,
        feature_schema_hash=feature_schema_hash,
        fill_model_version="paper-fill-v1",
        pipeline_content_hash=pipeline_content_hash,
        cost_assumptions=(
            ("adverse_funding_per_unit", "0"),
            ("entry_fee_rate", fee_rate),
            ("entry_slippage_rate", slippage_rate),
            ("exit_fee_rate", fee_rate),
            ("exit_slippage_rate", slippage_rate),
            ("spread_rate", "0"),
        ),
        parameter_family_hash=parameter_family_hash,
        trial_count=1,
        trial_index=0,
        seed=submission.seed,
        learning_intent=submission.learning_intent,
        learning_job_id=(str(job_id) if submission.learning_intent == "TRAINING_BASE" else None),
    )
    pinned_bundle_reference = BacktestDatasetReplayBundleReference.from_bundle(bundle)

    seed = _build_context_seed(
        workspace_id=workspace_id,
        job_id=job_id,
        bundle=bundle,
        submission=submission,
        release_id=release.release_id,
        release_content_hash=release.content_hash,
        required_timeframes=required_timeframes,
        entry_timeframe=entry_timeframe,
        settings_version_id=settings_version_id,
        risk_profile_version_id=risk_profile_version_id,
        settings_hash=settings_hash,
        feature_schema_hash=feature_schema_hash,
        pipeline_content_hash=pipeline_content_hash,
        fill_model_version=manifest.fill_model_version,
    )
    base_scenario = SensitivityScenario("base", "BASE")

    def executor(
        replay_bundle: BacktestDatasetReplayBundle,
        replay_manifest: ReplayManifest,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> RuntimePaperReplayComparison:
        """Chạy đúng manifest và bundle đã ghim, không chấp nhận request thay thế."""
        if (
            type(replay_bundle) is not BacktestDatasetReplayBundle
            or replay_bundle.workspace_id != workspace_id
            or BacktestDatasetReplayBundleReference.from_bundle(replay_bundle)
            != pinned_bundle_reference
        ):
            raise ValueError("Executor nhận bundle khác workspace hoặc hash đã ghim.")
        if type(replay_manifest) is not ReplayManifest or replay_manifest != manifest:
            raise ValueError("Executor nhận ReplayManifest khác profile đã ghim.")
        return run_backtest_dataset_runtime_replay(
            replay_bundle,
            replay_manifest,
            strategies=registry,
            execution_interval=submission.execution_interval,
            max_bars_per_timeframe=REPLAY_MAX_BARS_PER_TIMEFRAME,
            context_seed_factory=lambda scenario, scenario_manifest: _context_for_scenario(
                scenario,
                scenario_manifest,
                expected_scenario=base_scenario,
                expected_manifest=manifest,
                seed=seed,
            ),
            gateway_factory=lambda scenario, scenario_manifest: _gateway_for_scenario(
                scenario,
                scenario_manifest,
                expected_scenario=base_scenario,
                expected_manifest=manifest,
                fee_rate=submission.fee_rate,
                slippage_rate=submission.slippage_rate,
                fill_model_version=manifest.fill_model_version,
            ),
            scenarios=(base_scenario,),
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
        )

    return BacktestReplayPlan(manifest=manifest, executor=executor)


def _validate_request_scope(
    workspace_id: UUID,
    job_id: UUID,
    bundle: BacktestDatasetReplayBundle,
    submission: BacktestReplayJobSubmission,
) -> None:
    if type(workspace_id) is not UUID or type(job_id) is not UUID:
        raise ValueError("workspace_id và job_id phải là UUID.")
    if type(bundle) is not BacktestDatasetReplayBundle:
        raise ValueError("Cần BacktestDatasetReplayBundle đã xác thực.")
    if type(submission) is not BacktestReplayJobSubmission:
        raise ValueError("Cần BacktestReplayJobSubmission đã xác thực.")
    if bundle.workspace_id != workspace_id:
        raise ValueError("Bundle khác workspace của job.")
    if bundle.dataset_hash != submission.bundle_hash:
        raise ValueError("Bundle hash không khớp yêu cầu job.")


def _build_context_seed(
    *,
    workspace_id: UUID,
    job_id: UUID,
    bundle: BacktestDatasetReplayBundle,
    submission: BacktestReplayJobSubmission,
    release_id: str,
    release_content_hash: str,
    required_timeframes: set[str],
    entry_timeframe: str,
    settings_version_id: UUID,
    risk_profile_version_id: UUID,
    settings_hash: str,
    feature_schema_hash: str,
    pipeline_content_hash: str,
    fill_model_version: str,
) -> ReplayEvaluationContext:
    equity = submission.starting_equity
    risk_fraction = submission.per_trade_risk_fraction
    quote_asset = _quote_asset(bundle.symbol)
    execution_timeframes = required_timeframes | {submission.execution_interval}
    execution_durations = {
        timeframe: _SUPPORTED_EXECUTION_INTERVALS.get(timeframe, TIMEFRAME_INTERVALS.get(timeframe))
        for timeframe in execution_timeframes
    }
    if any(duration is None for duration in execution_durations.values()):
        raise ValueError("Bundle có timeframe chưa được hỗ trợ bởi cấu hình replay.")
    durations = tuple(duration for duration in execution_durations.values() if duration is not None)
    confirmation_timeframe = next(
        (timeframe for timeframe in sorted(required_timeframes) if timeframe != entry_timeframe),
        entry_timeframe,
    )
    settings_payload: dict[str, object] = {
        "schema_version": 1,
        "runtime": {"mode": "PAPER", "timezone": "UTC"},
        "market_data": {
            "venue": "BINANCE_USDM",
            "symbols": [bundle.symbol],
            "timeframes": sorted(execution_timeframes),
            "max_age_seconds": min(86_400, int(max(durations).total_seconds() * 2)),
        },
        "features": {"formula_version": FEATURE_CODE_HASH, "sessions_timezone": "UTC"},
        "strategy": {
            "default_release": release_id,
            "parameters": dict(sorted(submission.strategy_parameters)),
            "entry_timeframe": entry_timeframe,
            "confirmation_timeframe": confirmation_timeframe,
        },
        "risk": {
            "profile_version_id": str(risk_profile_version_id),
            "workspace_max_open_risk_fraction": decimal_text(risk_fraction),
        },
        "paper_execution": {
            "quote_asset": quote_asset,
            "starting_equity": decimal_text(equity),
            "fee_schedule_version": fill_model_version,
            "fill_model": "next_bar_with_adverse_costs",
        },
    }
    published = PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=settings_version_id,
        settings_hash=settings_hash,
        settings=WorkspaceSettings.model_validate(settings_payload),
        effective_at=bundle.evaluation_start,
    )

    unit = Decimal("1")
    quantity_step = Decimal("0.00000001")
    exchange = ExchangeRules(
        tick_size=quantity_step,
        quantity_step=quantity_step,
        contract_multiplier=unit,
        min_notional=Decimal("0.01"),
        max_notional=equity,
        max_quantity=equity / quantity_step,
    )
    max_trade_loss = equity * risk_fraction
    risk_limits = RiskLimits(
        per_trade_risk_fraction=risk_fraction,
        max_aggregate_open_risk=max_trade_loss,
        max_notional=equity,
        max_leverage=unit,
        max_positions=1,
        max_symbol_notional=equity,
        max_concentration_fraction=unit,
        max_daily_loss=max_trade_loss,
        max_rolling_loss=max_trade_loss * 2,
        max_account_age=timedelta(days=1),
        max_data_age=max(durations) * 2,
    )
    planner = PlannerSettings(
        workspace_id=str(workspace_id),
        settings_version_id=str(settings_version_id),
        risk_profile_version_id=str(risk_profile_version_id),
        published=True,
        active_release_ids=(release_id,),
        leverage=unit,
        exchange=exchange,
        limits=risk_limits,
        release_content_hashes=((release_id, release_content_hash),),
        settings_hash=settings_hash,
    )
    account_time = bundle.evaluation_start - timedelta(microseconds=1)
    account_id = f"backtest-paper:{job_id}"
    account = AccountSnapshot(
        workspace_id=str(workspace_id),
        account_id=account_id,
        settings_version_id=str(settings_version_id),
        captured_at=account_time,
        equity=equity,
        available_margin=equity,
        is_known=True,
    )
    risk_state = RiskState(
        workspace_id=str(workspace_id),
        account_id=account_id,
        settings_version_id=str(settings_version_id),
        risk_profile_version_id=str(risk_profile_version_id),
        evaluated_at=bundle.evaluation_start,
        account_captured_at=account_time,
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
        version_id=fill_model_version,
        entry_fee_rate=submission.fee_rate,
        exit_fee_rate=submission.fee_rate,
        adverse_funding_per_unit=Decimal("0"),
        entry_slippage_rate=submission.slippage_rate,
        exit_slippage_rate=submission.slippage_rate,
    )
    return ReplayEvaluationContext(
        published_settings=published,
        evaluated_at=bundle.evaluation_start,
        planner_settings=planner,
        account=account,
        risk_state=risk_state,
        open_exposures=(),
        cost_model=costs,
        feature_schema_hash=feature_schema_hash,
        pipeline_content_hash=pipeline_content_hash,
        execution_events=(),
        # Khớp hành vi paper_market_context.py ở runtime khi chưa đánh giá ngược theo execution bar.
        opposing_signal=False,
        reserve_risk=lambda decision, key: decision.approved and bool(key),
    )


def _context_for_scenario(
    scenario: SensitivityScenario,
    manifest: ReplayManifest,
    *,
    expected_scenario: SensitivityScenario,
    expected_manifest: ReplayManifest,
    seed: ReplayEvaluationContext,
) -> ReplayEvaluationContext:
    if scenario != expected_scenario or manifest != expected_manifest:
        raise ValueError("Profile chỉ cho phép scenario BASE đã ghim.")
    return seed


def _gateway_for_scenario(
    scenario: SensitivityScenario,
    manifest: ReplayManifest,
    *,
    expected_scenario: SensitivityScenario,
    expected_manifest: ReplayManifest,
    fee_rate: Decimal,
    slippage_rate: Decimal,
    fill_model_version: str,
) -> PaperExecutionGateway:
    if scenario != expected_scenario or manifest != expected_manifest:
        raise ValueError("Fill policy chỉ cho phép scenario BASE đã ghim.")
    return PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id=fill_model_version,
            latency=timedelta(0),
            spread_rate=Decimal("0"),
            slippage_rate=slippage_rate,
            fee_rate=fee_rate,
        )
    )


def _quote_asset(symbol: str) -> str:
    for suffix in ("FDUSD", "USDT", "USDC", "TUSD", "BUSD"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return suffix
    raise ValueError("Bundle cần mã hợp đồng có quote asset được hỗ trợ rõ ràng.")


def _hash(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["BacktestReplayPlan", "ReplayExecutor", "build_backtest_replay_plan"]
