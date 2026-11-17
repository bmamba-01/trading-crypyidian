"""Đánh giá tín hiệu ngược chiều point-in-time cho exit recipe khi replay PAPER."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from trading_bot.application.backtest import ReplayManifest, ReplayObservation
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.runtime_replay_evaluator import ReplayEvaluationContext
from trading_bot.domain.execution.paper import OrderStatus, PaperBar, PaperExecutionGateway
from trading_bot.domain.strategy.contracts import (
    SeriesQuality,
    Strategy,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.registry import StrategyRegistry

ZERO = Decimal("0")
_VALID_DIRECTIONS = frozenset({"long", "short"})

OpposingSignalProvider = Callable[[PaperBar, ReplayObservation | None], bool | None]
PositionDirectionProvider = Callable[[PaperBar], str | None]


def _classify_observation(observation: ReplayObservation) -> str:
    """Phân loại độ sạch point-in-time nội tại của một ReplayObservation."""
    snapshot = observation.snapshot
    decision_time = snapshot.decision_time
    if (
        observation.event_at > observation.available_at
        or observation.available_at > decision_time
        or observation.event_at > decision_time
    ):
        return "LOOKAHEAD"
    for series in snapshot.series:
        if any(not bar.is_closed for bar in series.bars):
            return "LOOKAHEAD"
        if any(
            max(bar.close_time, bar.event_at, bar.available_at) > decision_time
            for bar in series.bars
        ):
            return "LOOKAHEAD"
    if any(feature.available_at > decision_time for feature in snapshot.features):
        return "LOOKAHEAD"
    if (
        snapshot.macro_evidence_available_at is not None
        and snapshot.macro_evidence_available_at > decision_time
    ):
        return "LOOKAHEAD"
    for series in snapshot.series:
        if (
            series.quality is not SeriesQuality.READY
            or series.backfill_complete is not True
            or not series.bars
        ):
            return "INPUT_NOT_READY"
        if series.max_event_age is not None:
            latest_event_at = max(bar.event_at for bar in series.bars)
            if decision_time - latest_event_at > series.max_event_age:
                return "INPUT_NOT_READY"
        if series.expected_interval is not None and any(
            right.open_time - left.open_time != series.expected_interval
            for left, right in zip(series.bars, series.bars[1:], strict=False)
        ):
            return "INPUT_NOT_READY"
    return "READY"


def _sort_observations(
    observations: Sequence[ReplayObservation],
) -> tuple[tuple[ReplayObservation, ...], tuple[datetime, ...]]:
    ordered = tuple(
        sorted(
            observations,
            key=lambda item: (
                item.snapshot.decision_time,
                item.available_at,
                item.event_at,
                item.event_id,
            ),
        )
    )
    decision_times = tuple(item.snapshot.decision_time for item in ordered)
    return ordered, decision_times


def _latest_from_ordered(
    ordered: tuple[ReplayObservation, ...],
    decision_times: tuple[datetime, ...],
    bar: PaperBar,
) -> ReplayObservation | None:
    index = bisect_right(decision_times, bar.opened_at) - 1
    while index >= 0:
        candidate = ordered[index]
        index -= 1
        if candidate.snapshot.symbol != bar.symbol:
            continue
        if (
            candidate.available_at > bar.opened_at
            or candidate.snapshot.decision_time > bar.opened_at
        ):
            continue
        status = _classify_observation(candidate)
        if status == "LOOKAHEAD":
            raise ValueError("LOOKAHEAD: observation chứa dữ liệu vượt decision_time.")
        if status == "INPUT_NOT_READY":
            continue
        return candidate
    return None


def latest_point_in_time_observation(
    observations: Sequence[ReplayObservation],
    bar: PaperBar,
) -> ReplayObservation | None:
    """Tìm observation hợp lệ mới nhất có ``available_at <= bar.opened_at`` (không lookahead)."""
    ordered, decision_times = _sort_observations(observations)
    return _latest_from_ordered(ordered, decision_times, bar)


def _resolve_active_directions(
    *,
    bar: PaperBar,
    gateway: PaperExecutionGateway | None,
    open_direction: str | PositionDirectionProvider | None,
    static_opposite_disabled: bool,
) -> tuple[set[str], bool]:
    if open_direction is not None:
        resolved = open_direction(bar) if callable(open_direction) else open_direction
        if resolved is None:
            return set(), True
        if resolved not in _VALID_DIRECTIONS:
            raise ValueError("open_direction chỉ nhận 'long', 'short' hoặc None.")
        return {resolved}, True

    if gateway is None:
        return set(), False

    directions: set[str] = set()
    for position in gateway.open_positions():
        if (
            position.symbol == bar.symbol
            and not position.is_closed
            and position.quantity > ZERO
            and position.recipe.opposite_signal_exit
        ):
            directions.add(position.direction)

    if not static_opposite_disabled:
        for order in gateway.open_orders():
            if (
                order.symbol == bar.symbol
                and order.status in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}
                and order.eligible_at <= bar.opened_at < order.expires_at
                and gateway.position(order.intent_id) is None
            ):
                directions.add(order.direction)
    return directions, True


def _select_observation(
    *,
    bar: PaperBar,
    observation: ReplayObservation | None,
    ordered_observations: tuple[ReplayObservation, ...],
    decision_times: tuple[datetime, ...],
) -> ReplayObservation | None:
    if ordered_observations:
        return _latest_from_ordered(ordered_observations, decision_times, bar)
    if observation is None:
        return None
    status = _classify_observation(observation)
    if status == "LOOKAHEAD":
        raise ValueError("LOOKAHEAD: observation chứa dữ liệu vượt decision_time.")
    if status == "INPUT_NOT_READY":
        return None
    if observation.snapshot.symbol != bar.symbol:
        return None
    if (
        observation.available_at > bar.opened_at
        or observation.snapshot.decision_time > bar.opened_at
    ):
        return None
    return observation


def _signal_direction(
    *,
    strategy: Strategy,
    selection: StrategySelection,
    observation: ReplayObservation,
    cache: dict[tuple[str, str], str | None],
) -> str | None:
    cache_key = (observation.event_id, observation.snapshot.snapshot_id)
    if cache_key not in cache:
        snapshot = (
            observation.snapshot
            if observation.snapshot.position_is_flat is True
            else replace(observation.snapshot, position_is_flat=True)
        )
        result = strategy.evaluate(snapshot, selection)
        cache[cache_key] = result.direction if isinstance(result, StrategyCandidate) else None
    return cache[cache_key]


def build_point_in_time_opposing_provider(
    strategies: StrategyRegistry,
    selection: StrategySelection,
    *,
    release_id: str | None = None,
    observations: Sequence[ReplayObservation] = (),
    gateway: PaperExecutionGateway | None = None,
    open_direction: str | PositionDirectionProvider | None = None,
) -> OpposingSignalProvider:
    """Tạo provider kiểm tra tín hiệu ngược chiều tại execution bar mà không nhìn trước.

    Chỉ dùng `ReplayObservation` mới nhất có `available_at <= bar.opened_at` (và
    `decision_time <= bar.opened_at`). Trả `True` chỉ khi strategy sinh tín hiệu hợp lệ
    ngược hướng với vị thế đang mở (hoặc lệnh đang chờ khớp trên execution bar).
    """
    if release_id is not None:
        target_release_id = release_id.strip()
        if not target_release_id:
            raise ValueError("release_id không được để trống.")
    elif len(selection.active_release_ids) == 1:
        target_release_id = selection.active_release_ids[0]
    else:
        raise ValueError("Cần chỉ rõ release_id khi StrategySelection không chọn đúng một release.")

    if isinstance(open_direction, str) and open_direction not in _VALID_DIRECTIONS:
        raise ValueError("open_direction chỉ nhận 'long' hoặc 'short'.")

    strategy = strategies.resolve(target_release_id, selection)
    static_recipe = getattr(strategy, "recipe", None)
    static_opposite_disabled = (
        isinstance(static_recipe, ExitRecipe) and not static_recipe.opposite_signal_exit
    )
    ordered_observations, decision_times = _sort_observations(observations)
    signal_cache: dict[tuple[str, str], str | None] = {}

    def provider(bar: PaperBar, observation: ReplayObservation | None) -> bool | None:
        active_directions, direction_known = _resolve_active_directions(
            bar=bar,
            gateway=gateway,
            open_direction=open_direction,
            static_opposite_disabled=static_opposite_disabled,
        )
        if direction_known and not active_directions:
            return False

        selected_obs = _select_observation(
            bar=bar,
            observation=observation,
            ordered_observations=ordered_observations,
            decision_times=decision_times,
        )
        if selected_obs is None:
            return False

        direction = _signal_direction(
            strategy=strategy,
            selection=selection,
            observation=selected_obs,
            cache=signal_cache,
        )
        if direction is None:
            return False
        if not direction_known:
            return False if static_opposite_disabled else None
        return any(direction != open_dir for open_dir in active_directions)

    return provider


def _selection_for_replay(
    manifest: ReplayManifest,
    scenario: SensitivityScenario,
    seed: ReplayEvaluationContext,
) -> StrategySelection | None:
    parameters: tuple[tuple[str, int], ...]
    if scenario.kind == "PARAMETER":
        parsed: list[tuple[str, int]] = []
        for key, value in scenario.parameter_overrides:
            try:
                number = Decimal(value)
            except Exception:
                return None
            if not number.is_finite() or number != number.to_integral_value():
                return None
            parsed.append((key, int(number)))
        parameters = tuple(sorted(parsed))
    else:
        parameters = tuple(sorted(seed.published_settings.settings.strategy.parameters.items()))
    try:
        return StrategySelection(
            settings_version_id=manifest.settings_version_id,
            active_release_ids=(manifest.strategy_release_id,),
            published=True,
            parameters=parameters,
        )
    except ValueError:
        return None


def build_default_opposing_signal_provider(
    *,
    strategies: StrategyRegistry,
    manifest: ReplayManifest,
    scenario: SensitivityScenario,
    seed: ReplayEvaluationContext,
    observations: Sequence[ReplayObservation],
    gateway: PaperExecutionGateway,
) -> OpposingSignalProvider | None:
    """Dựng opposing provider mặc định an toàn cho `BacktestDatasetRuntimeEvaluatorFactory`.

    Nếu release chưa có trong registry hoặc tham số scenario không hợp lệ, trả `None` để
    `RuntimeReplayEvaluator._validate_context` tự từ chối theo đúng hợp đồng fail-closed.
    """
    try:
        strategies.get_release(manifest.strategy_release_id)
    except LookupError:
        return None

    selection = _selection_for_replay(manifest, scenario, seed)
    if selection is None:
        return None

    return build_point_in_time_opposing_provider(
        strategies,
        selection,
        release_id=manifest.strategy_release_id,
        observations=observations,
        gateway=gateway,
    )


__all__ = [
    "OpposingSignalProvider",
    "PositionDirectionProvider",
    "build_default_opposing_signal_provider",
    "build_point_in_time_opposing_provider",
    "latest_point_in_time_observation",
]
