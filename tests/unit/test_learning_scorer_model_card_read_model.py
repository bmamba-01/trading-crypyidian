from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.domain.learning.scorer_model_card import scorer_action_latency_summary_from_rows

WORKSPACE_ID = uuid4()
BOT_ID = uuid4()
REPORT_ID = uuid4()
AS_OF = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
HASHES = {
    "artifact_hash": "a" * 64,
    "report_hash": "b" * 64,
    "base_artifact_hash": "c" * 64,
    "policy_hash": "d" * 64,
}
STAGES = (
    "signal_available_to_intent",
    "evidence_persisted_to_intent",
    "decision_persisted_to_intent",
)


def _repository_api():
    module_name = "trading_bot.adapters.database.repositories.learning_scorer_model_card"
    assert importlib.util.find_spec(module_name) is not None, "chưa có repository model card"
    module = importlib.import_module(module_name)
    return module.LearningScorerActionLatencyUnavailable, module.LearningScorerModelCardRepository


def _rows() -> list[dict[str, object]]:
    return [
        {
            "stage_name": stage,
            "window_start": AS_OF - timedelta(days=7),
            "as_of": AS_OF,
            "candidate_decision_count": 100,
            "candidate_no_intent_count": 0,
            "future_intent_count": 0,
            "valid_sample_count": 100,
            "invalid_timestamp_count": 0,
            "p50_ms": 25,
            "p95_ms": 55,
        }
        for stage in STAGES
    ]


class _Mappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def all(self) -> list[dict[str, object]]:
        return self.rows


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> _Mappings:
        return _Mappings(self.rows)


class _Session:
    def __init__(self, rows: list[dict[str, object]], error: Exception | None = None) -> None:
        self.rows = rows
        self.error = error
        self.statement = None
        self.parameters = None

    async def execute(self, statement, parameters=None):
        self.statement = statement
        self.parameters = parameters
        if self.error is not None:
            raise self.error
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_repository_calls_only_workspace_scoped_aggregate_function() -> None:
    _, repository_type = _repository_api()
    session = _Session(_rows())

    result = await repository_type(session).get_action_latency(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        report_id=REPORT_ID,
        **HASHES,
        window_code="7d",
    )

    assert result == scorer_action_latency_summary_from_rows(
        bot_id=BOT_ID,
        window_code="7d",
        rows=_rows(),
    )
    assert "wp06_aggregate_scorer_paper_action_latency" in str(session.statement)
    assert "order_intent" not in str(session.statement)
    assert session.parameters == {
        "workspace_id": str(WORKSPACE_ID),
        "bot_id": str(BOT_ID),
        "report_id": str(REPORT_ID),
        **HASHES,
        "window_code": "7d",
    }


@pytest.mark.asyncio
async def test_repository_maps_database_rights_denial_without_leaking_sql_detail() -> None:
    unavailable_type, repository_type = _repository_api()
    session = _Session([], SQLAlchemyError("sensitive database detail"))

    with pytest.raises(unavailable_type) as error:
        await repository_type(session).get_action_latency(
            workspace_id=WORKSPACE_ID,
            bot_id=BOT_ID,
            report_id=REPORT_ID,
            **HASHES,
            window_code="7d",
        )

    assert "sensitive database detail" not in str(error.value)


@pytest.mark.asyncio
async def test_repository_maps_unsupported_projection_values_without_leaking_type() -> None:
    unavailable_type, repository_type = _repository_api()
    rows = _rows()
    rows[0]["candidate_decision_count"] = object()

    with pytest.raises(unavailable_type) as error:
        await repository_type(_Session(rows)).get_action_latency(
            workspace_id=WORKSPACE_ID,
            bot_id=BOT_ID,
            report_id=REPORT_ID,
            **HASHES,
            window_code="7d",
        )

    assert "Projection latency" in str(error.value)
    assert "object" not in str(error.value)


@pytest.mark.asyncio
async def test_repository_rejects_invalid_hash_before_database_read() -> None:
    _, repository_type = _repository_api()
    session = _Session(_rows())

    with pytest.raises(ValueError, match="SHA-256"):
        await repository_type(session).get_action_latency(
            workspace_id=WORKSPACE_ID,
            bot_id=BOT_ID,
            report_id=REPORT_ID,
            artifact_hash="invalid",
            report_hash=HASHES["report_hash"],
            base_artifact_hash=HASHES["base_artifact_hash"],
            policy_hash=HASHES["policy_hash"],
            window_code="7d",
        )

    assert session.statement is None
