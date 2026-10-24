"""Chạy các kịch bản độ nhạy có thể tái lập cho bộ điều phối replay hiện có."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Literal

from trading_bot.application.backtest import (
    BacktestRunner,
    EvaluationReport,
    ReplayEvaluator,
    ReplayManifest,
    ReplayObservation,
    ReplayRejected,
)

ScenarioKind = Literal["BASE", "COST", "PARAMETER"]


@dataclass(frozen=True, slots=True)
class SensitivityScenario:
    """Một kịch bản gốc, stress chi phí hoặc lân cận tham số số học."""

    scenario_id: str
    kind: ScenarioKind
    cost_overrides: tuple[tuple[str, str], ...] = ()
    parameter_overrides: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("scenario_id không được để trống.")
        if self.kind not in ("BASE", "COST", "PARAMETER"):
            raise ValueError("kind phải là BASE, COST hoặc PARAMETER.")
        _validate_pairs(self.cost_overrides, "cost_overrides")
        _validate_pairs(self.parameter_overrides, "parameter_overrides")
        if self.kind == "BASE" and (self.cost_overrides or self.parameter_overrides):
            raise ValueError("Kịch bản BASE không được ghi đè cost hoặc parameter.")
        if self.kind == "COST" and (not self.cost_overrides or self.parameter_overrides):
            raise ValueError("Kịch bản COST cần cost override và không nhận parameter.")
        if self.kind == "PARAMETER" and (not self.parameter_overrides or self.cost_overrides):
            raise ValueError("Kịch bản PARAMETER cần parameter override và không nhận cost.")


@dataclass(frozen=True, slots=True)
class SensitivityRun:
    """Kết quả một kịch bản cùng manifest và Trial ID đã tạo."""

    scenario: SensitivityScenario
    manifest: ReplayManifest
    result: EvaluationReport | ReplayRejected

    @property
    def manifest_hash(self) -> str:
        return self.result.manifest_hash

    @property
    def trial_id(self) -> str:
        return self.result.trial_record.trial_id


@dataclass(frozen=True, slots=True)
class SensitivityComparison:
    """Các kết quả đối chiếu; không xếp hạng, khuyến nghị hoặc quảng bá chiến lược."""

    runs: tuple[SensitivityRun, ...]
    is_complete: bool
    caveat_vi: str = (
        "Đây là phép đối chiếu độ nhạy theo kịch bản, không phải xếp hạng, khuyến nghị "
        "hay bằng chứng về lợi nhuận tương lai."
    )


EvaluatorFactory = Callable[[SensitivityScenario, ReplayManifest], ReplayEvaluator]


def run_sensitivity(
    observations: tuple[ReplayObservation, ...],
    *,
    manifest: ReplayManifest,
    scenarios: tuple[SensitivityScenario, ...],
    evaluator_factory: EvaluatorFactory,
    runner: BacktestRunner | None = None,
) -> SensitivityComparison:
    """Chạy BASE và các biến thể với manifest/trial riêng, theo thứ tự xác định.

    Factory phải tạo evaluator phù hợp với cấu hình từng scenario. Hash strategy
    của biến thể tham số là hash tổng hợp giữa hash code gốc và override; giá trị
    override vẫn được trả trong từng record để caller lưu cùng kết quả. Hàm không
    tự xếp hạng hoặc chọn biến thể.
    """
    ordered = _validate_and_order_scenarios(scenarios, manifest)
    active_runner = runner or BacktestRunner()
    runs: list[SensitivityRun] = []
    for trial_index, scenario in enumerate(ordered):
        scenario_manifest = _manifest_for_scenario(
            manifest,
            scenario,
            trial_index=trial_index,
            trial_count=len(ordered),
        )
        evaluator = evaluator_factory(scenario, scenario_manifest)
        result = active_runner.run(
            observations,
            manifest=scenario_manifest,
            evaluator=evaluator,
        )
        runs.append(SensitivityRun(scenario, scenario_manifest, result))
    return SensitivityComparison(
        runs=tuple(runs),
        is_complete=all(isinstance(run.result, EvaluationReport) for run in runs),
    )


def _validate_and_order_scenarios(
    scenarios: tuple[SensitivityScenario, ...], manifest: ReplayManifest
) -> tuple[SensitivityScenario, ...]:
    if not isinstance(scenarios, tuple) or not scenarios:
        raise ValueError("scenarios phải là tuple không rỗng.")
    ids = tuple(scenario.scenario_id for scenario in scenarios)
    if len(set(ids)) != len(ids):
        raise ValueError("scenario_id bị trùng.")
    if sum(scenario.kind == "BASE" for scenario in scenarios) != 1:
        raise ValueError("Cần đúng một kịch bản BASE.")

    base_costs = dict(manifest.cost_assumptions)
    if len(base_costs) != len(manifest.cost_assumptions):
        raise ValueError("Cost assumption gốc bị trùng.")
    effective_keys: set[str] = set()
    for scenario in scenarios:
        if scenario.kind == "COST":
            for key, value in scenario.cost_overrides:
                if key not in base_costs:
                    raise ValueError(f"Cost key '{key}' không tồn tại trong manifest gốc.")
                replacement = _decimal_text(value, f"cost {key}")
                original = _decimal_text(base_costs[key], f"base cost {key}")
                if replacement < original:
                    raise ValueError("Cost stress không được giảm giả định chi phí gốc.")
                if replacement == original:
                    raise ValueError("Cost override phải làm thay đổi ít nhất một giả định.")
        for _, value in scenario.parameter_overrides:
            _decimal_text(value, "parameter")
        effective_key = _effective_scenario_key(scenario, base_costs)
        if effective_key in effective_keys:
            raise ValueError("Có kịch bản độ nhạy hiệu dụng bị trùng.")
        effective_keys.add(effective_key)

    priority = {"BASE": 0, "COST": 1, "PARAMETER": 2}
    return tuple(sorted(scenarios, key=lambda item: (priority[item.kind], item.scenario_id)))


def _manifest_for_scenario(
    base: ReplayManifest,
    scenario: SensitivityScenario,
    *,
    trial_index: int,
    trial_count: int,
) -> ReplayManifest:
    costs = dict(base.cost_assumptions)
    costs.update(scenario.cost_overrides)
    normalized_costs = tuple(
        sorted((key, _decimal_text(value, f"cost {key}")) for key, value in costs.items())
    )
    strategy_hash = base.strategy_content_hash
    if scenario.kind == "PARAMETER":
        strategy_hash = _hash(
            {
                "base_strategy_content_hash": base.strategy_content_hash,
                "parameter_overrides": [
                    [key, _decimal_text(value, f"parameter {key}")]
                    for key, value in sorted(scenario.parameter_overrides)
                ],
            }
        )
    return replace(
        base,
        strategy_content_hash=strategy_hash,
        cost_assumptions=normalized_costs,
        trial_index=trial_index,
        trial_count=trial_count,
    )


def _effective_scenario_key(scenario: SensitivityScenario, base_costs: dict[str, str]) -> str:
    payload = {
        "kind": scenario.kind,
        "costs": sorted(
            (key, _decimal_text(value, f"cost {key}"))
            for key, value in (
                base_costs.items() if scenario.kind == "BASE" else scenario.cost_overrides
            )
        ),
        "parameters": sorted(
            (key, _decimal_text(value, f"parameter {key}"))
            for key, value in scenario.parameter_overrides
        ),
    }
    return _hash(payload)


def _validate_pairs(values: tuple[tuple[str, str], ...], name: str) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} phải là tuple bất biến.")
    keys = tuple(key for key, _ in values)
    if len(keys) != len(set(keys)) or any(not key.strip() for key in keys):
        raise ValueError(f"{name} cần key không rỗng và duy nhất.")


def _decimal_text(value: str, name: str) -> str:
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError) as error:
        raise ValueError(f"{name} phải biểu diễn Decimal.") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} phải hữu hạn và không âm.")
    return format(number.normalize(), "f")


def _hash(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
