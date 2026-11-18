from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.learning_scorer_report_read_model import (
    LearningScorerReportReadRepository,
)

WORKSPACE_ID = uuid4()
REPORT_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
NOW = datetime(2026, 11, 10, 8, 30, tzinfo=UTC)


def _metrics() -> dict[str, object]:
    return {
        "evaluated_at": NOW.isoformat(),
        "manifest_hash": "1" * 64,
        "fold_plan_hash": "2" * 64,
        "fold_id": "fold-test-01",
        "vector_schema_version": "scorer-vector-v2",
        "vector_schema_hash": "3" * 64,
        "class_support": [[-1, 3], [0, 2], [1, 4]],
        "coverage": 0.75,
        "abstain_count": 2,
        "log_loss": 0.41,
        "brier_score": 0.18,
        "expected_calibration_error": 0.08,
        "false_veto_rate": 0.25,
        "candidate_retention": 0.67,
        "candidate_count": 6,
        "scored_candidate_count": 4,
        "approved_candidate_count": 3,
    }


def _row() -> dict[str, object]:
    return {
        "id": REPORT_ID,
        "created_at": NOW,
        "report_hash": "a" * 64,
        "proof_hash": "b" * 64,
        "scorer_artifact_hash": "c" * 64,
        "base_artifact_hash": "d" * 64,
        "policy_hash": "e" * 64,
        "policy_version": "test-policy-v1",
        "evaluation_status": "INCONCLUSIVE",
        "candidate_completeness_verified": False,
        "scored_candidate_coverage_verified": False,
        "test_sample_count": 9,
        "opportunity_count": 90,
        "report_metadata": _metrics(),
    }


class _Mappings:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def first(self) -> dict[str, object] | None:
        return self.row


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def mappings(self) -> _Mappings:
        return _Mappings(self.row)

    def scalar_one_or_none(self) -> UUID | None:
        if self.row is None:
            return None
        if isinstance(self.row, UUID):
            return self.row
        return self.row.get("report_id", self.row.get("id"))


class _Session:
    def __init__(
        self,
        row: dict[str, object] | None,
        *,
        report_id: UUID | None = REPORT_ID,
        rights_error: Exception | None = None,
    ) -> None:
        self.row = row
        self.report_id = report_id
        self.rights_error = rights_error
        self.calls: list[tuple[object, object]] = []
        self.statement = None
        self.parameters = None

    async def execute(self, statement, parameters=None):
        self.statement = statement
        self.parameters = parameters
        self.calls.append((statement, parameters))
        sql = str(statement)
        if "wp06_recheck_scorer_test_report_source_rights" in sql:
            if self.rights_error is not None:
                raise self.rights_error
            return _Result(None)
        if "AS report_id" in sql:
            return _Result(None if self.report_id is None else {"report_id": self.report_id})
        return _Result(self.row)


@pytest.mark.asyncio
async def test_latest_summary_projects_only_workspace_scoped_metadata() -> None:
    session = _Session(_row())

    summary = await LearningScorerReportReadRepository(session).get_latest(
        workspace_id=WORKSPACE_ID
    )

    assert summary is not None
    assert summary.evaluation_report_id == REPORT_ID
    assert summary.status == "INCONCLUSIVE"
    assert summary.test_sample_count == 9
    assert summary.opportunity_count == 90
    assert summary.coverage == 0.75
    assert summary.approved_candidate_count == 3
    assert summary.class_support == ((-1, 3), (0, 2), (1, 4))
    assert summary.report_hash == "a" * 64
    assert summary.policy_version == "test-policy-v1"
    assert len(session.calls) == 3
    report_id_statement, report_id_parameters = session.calls[0]
    rights_statement, rights_parameters = session.calls[1]
    report_id_compiled = report_id_statement.compile(dialect=postgresql.dialect())
    assert WORKSPACE_ID in report_id_compiled.params.values()
    assert "LIMIT" in str(report_id_compiled).upper()
    assert "wp06_recheck_scorer_test_report_source_rights" in str(rights_statement)
    assert rights_parameters["report_id"] == str(REPORT_ID)
    compiled = session.statement.compile(dialect=postgresql.dialect())
    assert WORKSPACE_ID in compiled.params.values()
    selected_columns = {column.key for column in session.statement.selected_columns}
    assert "report" not in selected_columns
    assert "canonical_report" not in selected_columns
    sql = str(compiled)
    assert "learning_scorer_test_evaluation_report.workspace_id" in sql
    assert "canonical_report" not in sql
    assert "learning_scorer_test_evaluation_report.report" in sql
    assert "learning_score_event" not in sql
    assert "LIMIT" not in sql.upper()


@pytest.mark.asyncio
async def test_latest_summary_returns_none_when_workspace_has_no_persisted_report() -> None:
    session = _Session(None, report_id=None)
    summary = await LearningScorerReportReadRepository(session).get_latest(
        workspace_id=WORKSPACE_ID
    )

    assert summary is None
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_latest_summary_is_not_loaded_when_current_rights_are_denied() -> None:
    session = _Session(
        _row(),
        rights_error=ValueError("current training source scope is no longer covered"),
    )

    with pytest.raises(ValueError, match="no longer covered"):
        await LearningScorerReportReadRepository(session).get_latest(workspace_id=WORKSPACE_ID)

    assert len(session.calls) == 2
    assert "wp06_recheck_scorer_test_report_source_rights" in str(session.calls[1][0])


@pytest.mark.asyncio
async def test_latest_summary_rejects_malformed_persisted_aggregate_metadata() -> None:
    row = _row()
    metadata = dict(row["report_metadata"])
    metadata["coverage"] = 1.5
    row["report_metadata"] = metadata

    with pytest.raises(ValueError, match="coverage"):
        await LearningScorerReportReadRepository(_Session(row)).get_latest(
            workspace_id=WORKSPACE_ID
        )
