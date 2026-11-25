"""Hợp đồng aggregate an toàn cho model card scorer và độ trễ PAPER."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

ScorerLatencyStage = Literal[
    "signal_available_to_intent",
    "evidence_persisted_to_intent",
    "decision_persisted_to_intent",
]
ScorerLatencyWindow = Literal["24h", "7d", "30d"]

_STAGES: tuple[ScorerLatencyStage, ...] = (
    "signal_available_to_intent",
    "evidence_persisted_to_intent",
    "decision_persisted_to_intent",
)
_WINDOWS: dict[ScorerLatencyWindow, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_ROW_KEYS = frozenset(
    {
        "stage_name",
        "window_start",
        "as_of",
        "candidate_decision_count",
        "candidate_no_intent_count",
        "future_intent_count",
        "valid_sample_count",
        "invalid_timestamp_count",
        "p50_ms",
        "p95_ms",
    }
)
_MINIMUM_PERCENTILE_SUPPORT = 100


@dataclass(frozen=True, slots=True)
class ScorerLatencyStageSummary:
    """Một chặng latency đã được kiểm schema và ngưỡng support."""

    stage: ScorerLatencyStage
    valid_sample_count: int
    invalid_timestamp_count: int
    p50_ms: int | None
    p95_ms: int | None

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise ValueError("Tên chặng latency không hợp lệ.")
        _require_nonnegative_int(self.valid_sample_count, "valid_sample_count")
        _require_nonnegative_int(self.invalid_timestamp_count, "invalid_timestamp_count")
        if (self.p50_ms is None) != (self.p95_ms is None):
            raise ValueError("p50 và p95 phải cùng được cung cấp hoặc cùng vắng mặt.")
        if self.valid_sample_count < _MINIMUM_PERCENTILE_SUPPORT:
            if self.p50_ms is not None:
                raise ValueError("Không được công bố percentile khi chưa đủ 100 mẫu hợp lệ.")
        elif self.p50_ms is None:
            raise ValueError("Cần có percentile khi đạt tối thiểu 100 mẫu hợp lệ.")
        if self.p50_ms is not None and self.p95_ms is not None:
            _require_nonnegative_int(self.p50_ms, "p50_ms")
            _require_nonnegative_int(self.p95_ms, "p95_ms")
            if self.p95_ms < self.p50_ms:
                raise ValueError("p95 không được nhỏ hơn p50.")


@dataclass(frozen=True, slots=True)
class ScorerActionLatencySummary:
    """Snapshot latency PAPER aggregate-only cho một bot và report cụ thể."""

    bot_id: UUID
    window_code: ScorerLatencyWindow
    as_of: datetime
    window_start: datetime
    candidate_decision_count: int
    candidate_no_intent_count: int
    future_intent_count: int
    stages: tuple[ScorerLatencyStageSummary, ...]

    def __post_init__(self) -> None:
        if type(self.bot_id) is not UUID:
            raise ValueError("Bot phải là UUID.")
        if self.window_code not in _WINDOWS:
            raise ValueError("Mã cửa sổ latency không hợp lệ.")
        as_of = _require_aware_datetime(self.as_of, "as_of").astimezone(UTC)
        window_start = _require_aware_datetime(self.window_start, "window_start").astimezone(UTC)
        if window_start >= as_of or as_of - window_start != _WINDOWS[self.window_code]:
            raise ValueError("Khoảng thời gian latency không khớp cửa sổ đã chọn.")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "window_start", window_start)
        _require_nonnegative_int(self.candidate_decision_count, "candidate_decision_count")
        _require_nonnegative_int(self.candidate_no_intent_count, "candidate_no_intent_count")
        _require_nonnegative_int(self.future_intent_count, "future_intent_count")
        if (
            self.candidate_no_intent_count + self.future_intent_count
            > self.candidate_decision_count
        ):
            raise ValueError("Số decision chưa có intent và timestamp tương lai vượt quá cohort.")
        if type(self.stages) is not tuple or tuple(item.stage for item in self.stages) != _STAGES:
            raise ValueError("Projection phải có đúng ba chặng theo thứ tự đã định nghĩa.")
        if any(
            item.valid_sample_count + item.invalid_timestamp_count > self.candidate_decision_count
            for item in self.stages
        ):
            raise ValueError("Số mẫu latency vượt quá số decision trong cohort.")


def scorer_action_latency_summary_from_rows(
    *,
    bot_id: UUID,
    window_code: object,
    rows: Sequence[Mapping[str, object]],
) -> ScorerActionLatencySummary:
    """Kiểm projection ba dòng aggregate trước khi đưa nó vào ứng dụng."""
    if type(bot_id) is not UUID:
        raise ValueError("Bot phải là UUID.")
    if type(window_code) is not str or window_code not in _WINDOWS:
        raise ValueError("Mã cửa sổ latency không hợp lệ.")
    if type(rows) not in {list, tuple} or len(rows) != len(_STAGES):
        raise ValueError("Projection latency phải có đúng ba chặng.")

    parsed_rows: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _ROW_KEYS:
            raise ValueError("Projection latency sai schema.")
        parsed_rows.append(dict(row))
    stages_seen = tuple(row["stage_name"] for row in parsed_rows)
    if stages_seen != _STAGES:
        raise ValueError("Projection latency thiếu, trùng hoặc sai thứ tự ba chặng.")

    first = parsed_rows[0]
    as_of = _require_aware_datetime(first["as_of"], "as_of").astimezone(UTC)
    window_start = _require_aware_datetime(first["window_start"], "window_start").astimezone(UTC)
    if as_of - window_start != _WINDOWS[window_code]:
        raise ValueError("Khoảng thời gian latency không khớp cửa sổ đã chọn.")

    shared_fields = (
        "window_start",
        "as_of",
        "candidate_decision_count",
        "candidate_no_intent_count",
        "future_intent_count",
    )
    for row in parsed_rows[1:]:
        for field in shared_fields:
            if row[field] != first[field]:
                raise ValueError("Các chặng latency phải cùng snapshot và cùng mẫu số.")

    stages: list[ScorerLatencyStageSummary] = []
    for row in parsed_rows:
        stage_name = row["stage_name"]
        valid_count = _strict_nonnegative_int(row["valid_sample_count"], "valid_sample_count")
        invalid_count = _strict_nonnegative_int(
            row["invalid_timestamp_count"], "invalid_timestamp_count"
        )
        p50_ms = _nullable_nonnegative_int(row["p50_ms"], "p50_ms")
        p95_ms = _nullable_nonnegative_int(row["p95_ms"], "p95_ms")
        stages.append(
            ScorerLatencyStageSummary(
                stage=cast(ScorerLatencyStage, stage_name),
                valid_sample_count=valid_count,
                invalid_timestamp_count=invalid_count,
                p50_ms=p50_ms,
                p95_ms=p95_ms,
            )
        )

    candidate_count = _strict_nonnegative_int(
        first["candidate_decision_count"], "candidate_decision_count"
    )
    return ScorerActionLatencySummary(
        bot_id=bot_id,
        window_code=window_code,
        as_of=as_of,
        window_start=window_start,
        candidate_decision_count=candidate_count,
        candidate_no_intent_count=_strict_nonnegative_int(
            first["candidate_no_intent_count"], "candidate_no_intent_count"
        ),
        future_intent_count=_strict_nonnegative_int(
            first["future_intent_count"], "future_intent_count"
        ),
        stages=tuple(stages),
    )


def _require_aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} phải là thời gian có múi giờ.")
    return value


def _require_nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} phải là số nguyên không âm.")
    return value


def _strict_nonnegative_int(value: object, field: str) -> int:
    return _require_nonnegative_int(value, field)


def _nullable_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _require_nonnegative_int(value, field)
