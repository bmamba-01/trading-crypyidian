"""Gói ứng dụng Strategy Pipeline Runtime Engine cho Strategy Card (QUANTSMART).

Bao gồm:
- ContextGateStatus, ContextGateResult, ContextGateEvaluator (Tầng L4)
- TriggerSignal, TriggerEvaluationResult, TriggerEvaluator (Tầng L5)
- ExecutionZones, ZoneBuilder (Tầng L6)
- StrategyCandidate, PipelineExecutionResult, StrategyPipelineEngine (Điều phối L0->L6)
"""

from __future__ import annotations

from trading_bot.application.strategy_pipeline.context_gate import (
    ContextGateEvaluator,
    ContextGateResult,
    ContextGateStatus,
)
from trading_bot.application.strategy_pipeline.microscope import (
    LayerTrace,
    MicroscopeReplayService,
    MicroscopeTrace,
    PresetCandle,
)
from trading_bot.application.strategy_pipeline.pipeline_engine import (
    PipelineExecutionResult,
    StrategyCandidate,
    StrategyPipelineEngine,
    compute_candidate_id,
)
from trading_bot.application.strategy_pipeline.trigger_evaluator import (
    TriggerEvaluationResult,
    TriggerEvaluator,
    TriggerSignal,
)
from trading_bot.application.strategy_pipeline.zone_builder import (
    ExecutionZones,
    ZoneBuilder,
)

__all__ = [
    "ContextGateEvaluator",
    "ContextGateResult",
    "ContextGateStatus",
    "ExecutionZones",
    "LayerTrace",
    "MicroscopeReplayService",
    "MicroscopeTrace",
    "PipelineExecutionResult",
    "PresetCandle",
    "StrategyCandidate",
    "StrategyPipelineEngine",
    "TriggerEvaluationResult",
    "TriggerEvaluator",
    "TriggerSignal",
    "ZoneBuilder",
    "compute_candidate_id",
]
