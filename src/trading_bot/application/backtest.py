"""Bộ điều phối replay thuần, không truy cập sàn hoặc cơ sở dữ liệu."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_bot.domain.learning.evaluation import EvaluationTrade, build_evaluation_metrics
from trading_bot.domain.strategy.contracts import DecisionSnapshot


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


@dataclass(frozen=True, slots=True)
class ReplayManifest:
    """Định danh bất biến cho dữ liệu, logic, chi phí và lượt thử nghiệm."""

    dataset_hash: str
    range_start: datetime
    range_end: datetime
    settings_version_id: str
    risk_profile_version_id: str
    strategy_release_id: str
    strategy_content_hash: str
    feature_schema_hash: str
    fill_model_version: str
    pipeline_content_hash: str
    cost_assumptions: tuple[tuple[str, str], ...]
    parameter_family_hash: str
    trial_count: int
    trial_index: int
    seed: int

    def __post_init__(self) -> None:
        _aware(self.range_start, "range_start")
        _aware(self.range_end, "range_end")
        if self.range_start >= self.range_end:
            raise ValueError("Replay range phải có range_start < range_end.")
        for name in (
            "settings_version_id",
            "risk_profile_version_id",
            "strategy_release_id",
            "fill_model_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        for name in (
            "dataset_hash",
            "strategy_content_hash",
            "feature_schema_hash",
            "pipeline_content_hash",
            "parameter_family_hash",
        ):
            digest = getattr(self, name)
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")
        keys = tuple(key for key, _ in self.cost_assumptions)
        if len(keys) != len(set(keys)) or any(not key.strip() for key in keys):
            raise ValueError("Cost assumption key phải không rỗng và duy nhất.")
        for _, value in self.cost_assumptions:
            try:
                number = Decimal(value)
            except Exception as error:
                raise ValueError("Cost assumption phải biểu diễn Decimal.") from error
            if not number.is_finite() or number < 0:
                raise ValueError("Cost assumption phải hữu hạn và không âm.")
        if self.trial_count < 1 or not 0 <= self.trial_index < self.trial_count or self.seed < 0:
            raise ValueError("trial_index phải thuộc trial_count; seed không âm.")

    @property
    def content_hash(self) -> str:
        """Hash canonical bao gồm toàn bộ phiên bản, range, costs và trial budget."""
        payload = {
            "dataset_hash": self.dataset_hash,
            "range_start": self.range_start.isoformat(),
            "range_end": self.range_end.isoformat(),
            "settings_version_id": self.settings_version_id,
            "risk_profile_version_id": self.risk_profile_version_id,
            "strategy_release_id": self.strategy_release_id,
            "strategy_content_hash": self.strategy_content_hash,
            "feature_schema_hash": self.feature_schema_hash,
            "fill_model_version": self.fill_model_version,
            "pipeline_content_hash": self.pipeline_content_hash,
            "cost_assumptions": sorted(self.cost_assumptions),
            "parameter_family_hash": self.parameter_family_hash,
            "trial_count": self.trial_count,
            "trial_index": self.trial_index,
            "seed": self.seed,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayObservation:
    """Snapshot quyết định cùng thời điểm sự kiện và thời điểm bot nhận biết."""

    event_id: str
    event_at: datetime
    available_at: datetime
    snapshot: DecisionSnapshot

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id không được để trống.")
        _aware(self.event_at, "event_at")
        _aware(self.available_at, "available_at")


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """Biên nhận append-only ở domain cho cả lượt chạy thành công hoặc thất bại."""

    trial_id: str
    manifest_hash: str
    parameter_family_hash: str
    trial_index: int
    trial_count: int
    status: str
    observation_count: int
    completed_observation_count: int
    trade_count: int
    error_type: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Báo cáo có hash, tổng PnL sau chi phí và cảnh báo về độ bất định."""

    manifest_hash: str
    report_hash: str
    observation_count: int
    usable_observation_count: int
    trade_count: int
    coverage: float
    trade_frequency: float
    gross_pnl: Decimal
    net_pnl: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    expectancy: Decimal
    max_drawdown: Decimal
    average_exposure: Decimal
    confidence_interval: tuple[Decimal, Decimal] | None
    caveat_vi: str
    trial_record: TrialRecord


@dataclass(frozen=True, slots=True)
class ReplayRejected:
    """Kết quả từ chối đầu vào có trial record để không mất dấu lượt thử."""

    manifest_hash: str
    reason_code: str
    detail_vi: str
    trial_record: TrialRecord


ReplayResult = EvaluationReport | ReplayRejected
ReplayEvaluator = Callable[[DecisionSnapshot], EvaluationTrade | None]


def validate_replay_observation(observation: ReplayObservation) -> None:
    """Chặn mọi nến, feature hoặc macro evidence chưa biết tại decision time."""
    snapshot = observation.snapshot
    decision_time = snapshot.decision_time
    if (
        observation.event_at > observation.available_at
        or observation.available_at > decision_time
        or observation.event_at > decision_time
    ):
        raise ValueError("LOOKAHEAD: event/availability vượt decision_time.")
    for series in snapshot.series:
        if series.quality.value != "ready" or series.backfill_complete is not True:
            raise ValueError("INPUT_NOT_READY: series chưa được xác nhận usable.")
        if series.expected_interval is None or series.max_event_age is None:
            raise ValueError("INPUT_NOT_READY: thiếu interval hoặc max_event_age.")
        if not series.bars:
            raise ValueError("INPUT_NOT_READY: series không có nến.")
        if any(not bar.is_closed for bar in series.bars):
            raise ValueError("LOOKAHEAD: chuỗi có nến chưa đóng.")
        if any(
            max(bar.close_time, bar.event_at, bar.available_at) > decision_time
            for bar in series.bars
        ):
            raise ValueError("LOOKAHEAD: mốc close/event/availability của nến ở tương lai.")
        latest_event_at = max(bar.event_at for bar in series.bars)
        if decision_time - latest_event_at > series.max_event_age:
            raise ValueError("INPUT_NOT_READY: nến mới nhất đã quá max_event_age.")
        if any(
            right.open_time - left.open_time != series.expected_interval
            for left, right in zip(series.bars, series.bars[1:], strict=False)
        ):
            raise ValueError("INPUT_NOT_READY: chuỗi nến có khoảng trống hoặc lệch interval.")
    if any(feature.available_at > decision_time for feature in snapshot.features):
        raise ValueError("LOOKAHEAD: feature chưa available tại decision_time.")
    if (
        snapshot.macro_evidence_available_at is not None
        and snapshot.macro_evidence_available_at > decision_time
    ):
        raise ValueError("LOOKAHEAD: macro evidence chưa available tại decision_time.")


class BacktestRunner:
    """Replay snapshots theo thứ tự deterministic và gọi evaluator dùng chung runtime."""

    def run(
        self,
        observations: tuple[ReplayObservation, ...],
        *,
        manifest: ReplayManifest,
        evaluator: ReplayEvaluator,
    ) -> ReplayResult:
        """Chạy evaluator tuần tự; lỗi được ghi thành trial thất bại, không bị nuốt."""
        manifest_hash = manifest.content_hash
        trial_id = hashlib.sha256(f"trial:{manifest_hash}".encode()).hexdigest()

        def rejected(
            code: str,
            detail: str,
            *,
            completed: int = 0,
            status: str = "REJECTED",
            error_type: str | None = None,
        ) -> ReplayRejected:
            return ReplayRejected(
                manifest_hash,
                code,
                detail,
                TrialRecord(
                    trial_id,
                    manifest_hash,
                    manifest.parameter_family_hash,
                    manifest.trial_index,
                    manifest.trial_count,
                    status,
                    len(observations),
                    completed,
                    0,
                    error_type,
                    "Lỗi evaluator; xem log vận hành đã được redacted."
                    if status == "FAILED"
                    else None,
                ),
            )

        if len({item.event_id for item in observations}) != len(observations):
            return rejected("DUPLICATE_EVENT", "event_id bị trùng trong dataset.")
        if len({item.snapshot.snapshot_id for item in observations}) != len(observations):
            return rejected("DUPLICATE_SNAPSHOT", "snapshot_id bị trùng trong dataset.")
        ordered = tuple(
            sorted(observations, key=lambda item: (item.available_at, item.event_at, item.event_id))
        )
        usable: list[ReplayObservation] = []
        for item in ordered:
            if not manifest.range_start <= item.snapshot.decision_time < manifest.range_end:
                return rejected("EVENT_OUTSIDE_RANGE", "Snapshot nằm ngoài khoảng replay nửa mở.")
            try:
                validate_replay_observation(item)
            except ValueError as error:
                reason = str(error).split(":", maxsplit=1)[0]
                if reason == "INPUT_NOT_READY":
                    continue
                if reason == "LOOKAHEAD":
                    reason = "LOOKAHEAD_DETECTED"
                return rejected(reason, str(error))
            usable.append(item)

        trades: list[EvaluationTrade] = []
        for index, item in enumerate(usable):
            try:
                trade = evaluator(item.snapshot)
            except Exception as error:
                return rejected(
                    "EVALUATOR_FAILED",
                    "Replay evaluator thất bại; chi tiết lỗi không được ghi vào báo cáo công khai.",
                    completed=index,
                    status="FAILED",
                    error_type=type(error).__name__,
                )
            if trade is not None:
                if getattr(evaluator, "emits_trade_on_close", False):
                    if (
                        trade.opened_at < manifest.range_start
                        or trade.closed_at >= manifest.range_end
                    ):
                        return rejected(
                            "TRADE_OUTSIDE_RANGE", "Giao dịch nằm ngoài khoảng replay nửa mở."
                        )
                    if trade.closed_at > item.snapshot.decision_time:
                        return rejected(
                            "TRADE_AFTER_OBSERVATION",
                            "Giao dịch đóng sau observation đã phát ra kết quả.",
                        )
                else:
                    if trade.opened_at < item.snapshot.decision_time:
                        return rejected(
                            "TRADE_BEFORE_DECISION",
                            "Callback evaluator generic trả trade mở trước decision_time.",
                        )
                    if trade.closed_at >= manifest.range_end:
                        return rejected(
                            "TRADE_OUTSIDE_RANGE", "Giao dịch đóng ngoài khoảng replay nửa mở."
                        )
                trades.append(trade)

        try:
            metrics = build_evaluation_metrics(
                tuple(sorted(trades, key=lambda trade: (trade.closed_at, trade.trade_id))),
                observation_count=len(ordered),
                seed=manifest.seed,
                usable_observation_count=len(usable),
            )
        except (ArithmeticError, ValueError) as error:
            return rejected(
                "EVALUATION_OUTPUT_INVALID",
                "Kết quả evaluator không hợp lệ; chi tiết được giữ ngoài báo cáo công khai.",
                completed=len(ordered),
                status="FAILED",
                error_type=type(error).__name__,
            )
        trial = TrialRecord(
            trial_id,
            manifest_hash,
            manifest.parameter_family_hash,
            manifest.trial_index,
            manifest.trial_count,
            "COMPLETED",
            len(ordered),
            len(usable),
            metrics.trade_count,
        )
        report_values = {
            "manifest_hash": manifest_hash,
            "observation_count": metrics.observation_count,
            "usable_observation_count": metrics.usable_observation_count,
            "trade_count": metrics.trade_count,
            "coverage": metrics.coverage,
            "trade_frequency": metrics.trade_frequency,
            "gross_pnl": str(metrics.gross_pnl),
            "net_pnl": str(metrics.net_pnl),
            "fees": str(metrics.fees),
            "funding": str(metrics.funding),
            "slippage": str(metrics.slippage),
            "expectancy": str(metrics.expectancy),
            "max_drawdown": str(metrics.max_drawdown),
            "average_exposure": str(metrics.average_exposure),
            "confidence_interval": (
                [str(value) for value in metrics.confidence_interval]
                if metrics.confidence_interval is not None
                else None
            ),
        }
        canonical = json.dumps(report_values, sort_keys=True, separators=(",", ":"))
        return EvaluationReport(
            manifest_hash=manifest_hash,
            report_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            observation_count=metrics.observation_count,
            usable_observation_count=metrics.usable_observation_count,
            trade_count=metrics.trade_count,
            coverage=metrics.coverage,
            trade_frequency=metrics.trade_frequency,
            gross_pnl=metrics.gross_pnl,
            net_pnl=metrics.net_pnl,
            fees=metrics.fees,
            funding=metrics.funding,
            slippage=metrics.slippage,
            expectancy=metrics.expectancy,
            max_drawdown=metrics.max_drawdown,
            average_exposure=metrics.average_exposure,
            confidence_interval=metrics.confidence_interval,
            caveat_vi=metrics.caveat_vi,
            trial_record=trial,
        )
