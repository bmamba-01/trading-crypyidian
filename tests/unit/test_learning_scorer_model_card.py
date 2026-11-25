from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

BOT_ID = uuid4()
AS_OF = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
WINDOW_START = AS_OF - timedelta(days=7)
STAGES = (
    "signal_available_to_intent",
    "evidence_persisted_to_intent",
    "decision_persisted_to_intent",
)


def _api():
    module_name = "trading_bot.domain.learning.scorer_model_card"
    assert importlib.util.find_spec(module_name) is not None, "chưa có hợp đồng model card"
    module = importlib.import_module(module_name)
    return (
        module.ScorerActionLatencySummary,
        module.ScorerLatencyStageSummary,
        module.scorer_action_latency_summary_from_rows,
    )


def _rows(
    *,
    sample_count: int = 100,
    p50_ms: int | None = 120,
    p95_ms: int | None = 480,
) -> list[dict[str, object]]:
    return [
        {
            "stage_name": stage,
            "window_start": WINDOW_START,
            "as_of": AS_OF,
            "candidate_decision_count": 130,
            "candidate_no_intent_count": 20,
            "future_intent_count": 2,
            "valid_sample_count": sample_count,
            "invalid_timestamp_count": 8,
            "p50_ms": p50_ms,
            "p95_ms": p95_ms,
        }
        for stage in STAGES
    ]


def test_latency_projection_parses_all_three_stages_and_separates_no_intent() -> None:
    summary_type, _, parse_rows = _api()
    summary = parse_rows(
        bot_id=BOT_ID,
        window_code="7d",
        rows=_rows(),
    )

    assert isinstance(summary, summary_type)
    assert summary.bot_id == BOT_ID
    assert summary.window_code == "7d"
    assert summary.as_of == AS_OF
    assert summary.window_start == WINDOW_START
    assert summary.candidate_decision_count == 130
    assert summary.candidate_no_intent_count == 20
    assert summary.future_intent_count == 2
    assert tuple(stage.stage for stage in summary.stages) == STAGES
    assert all(stage.p50_ms == 120 and stage.p95_ms == 480 for stage in summary.stages)


def test_percentiles_are_hidden_below_one_hundred_valid_samples() -> None:
    _, _, parse_rows = _api()
    rows = _rows(sample_count=99, p50_ms=None, p95_ms=None)

    summary = parse_rows(
        bot_id=BOT_ID,
        window_code="7d",
        rows=rows,
    )

    assert all(stage.valid_sample_count == 99 for stage in summary.stages)
    assert all(stage.p50_ms is None and stage.p95_ms is None for stage in summary.stages)


def test_percentile_values_are_rejected_below_the_support_floor() -> None:
    _, _, parse_rows = _api()
    rows = _rows(sample_count=99)

    with pytest.raises(ValueError, match="100 mẫu"):
        parse_rows(bot_id=BOT_ID, window_code="7d", rows=rows)


@pytest.mark.parametrize("window_code", ["1d", "90d", "", None])
def test_unknown_window_code_is_rejected(window_code: object) -> None:
    _, _, parse_rows = _api()
    with pytest.raises(ValueError, match="cửa sổ"):
        parse_rows(
            bot_id=BOT_ID,
            window_code=window_code,
            rows=_rows(),
        )


def test_duplicate_or_missing_stage_is_rejected() -> None:
    _, _, parse_rows = _api()
    rows = _rows()
    rows[-1] = dict(rows[0])

    with pytest.raises(ValueError, match="ba chặng"):
        parse_rows(bot_id=BOT_ID, window_code="7d", rows=rows)


def test_no_intent_and_future_intent_counts_cannot_overlap_or_exceed_cohort() -> None:
    _, _, parse_rows = _api()
    rows = _rows()
    for row in rows:
        row["candidate_no_intent_count"] = 129
        row["future_intent_count"] = 2

    with pytest.raises(ValueError, match="vượt quá"):
        parse_rows(bot_id=BOT_ID, window_code="7d", rows=rows)


def test_stage_counts_and_percentiles_must_be_valid() -> None:
    _, _, parse_rows = _api()
    rows = _rows()
    rows[0]["invalid_timestamp_count"] = -1

    with pytest.raises(ValueError, match="không âm"):
        parse_rows(bot_id=BOT_ID, window_code="7d", rows=rows)


def test_stage_summary_rejects_one_missing_percentile() -> None:
    _, stage_summary_type, _ = _api()
    with pytest.raises(ValueError, match="cùng được cung cấp"):
        stage_summary_type(
            stage="signal_available_to_intent",
            valid_sample_count=100,
            invalid_timestamp_count=0,
            p50_ms=20,
            p95_ms=None,
        )
