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
        "confusion_matrix": [[1, 0, 1, 1], [0, 2, 0, 0], [0, 0, 3, 1]],
        "coverage": 0.75,
        "abstain_count": 2,
        "log_loss": 0.41,
        "brier_score": 0.18,
        "expected_calibration_error": 0.08,
        "false_veto_rate": 0.25,
        "false_veto_count": 1,
        "candidate_retention": 0.67,
        "candidate_count": 6,
        "scored_candidate_count": 4,
        "approved_candidate_count": 3,
    }


def _row(*, not_run_count: int | None = 2, input_excluded_count: int = 2) -> dict[str, object]:
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
        "no_candidate_count": 80,
        "input_excluded_count": input_excluded_count,
        "not_run_count": not_run_count,
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
    assert summary.no_candidate_count == 80
    assert summary.input_excluded_count == 2
    assert summary.not_run_count == 2
    assert summary.coverage == 0.75
    assert summary.approved_candidate_count == 3
    assert summary.class_support == ((-1, 3), (0, 2), (1, 4))
    assert summary.confusion_matrix == ((1, 0, 1, 1), (0, 2, 0, 0), (0, 0, 3, 1))
    assert summary.false_veto_count == 1
    assert summary.positive_class_support == 4
    assert summary.positive_class_false_positive_count == 1
    assert summary.positive_class_false_negative_count == 1
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
    assert "false_veto_count" in compiled.params.values()
    assert "learning_scorer_test_opportunity_proof.not_run_count" in sql
    assert "learning_scorer_test_opportunity_proof.no_candidate_count" in sql
    assert "learning_scorer_test_opportunity_proof.input_excluded_count" in sql
    assert "learning_scorer_test_opportunity_proof.canonical_proof" not in sql
    assert "learning_scorer_test_evaluation_report.proof_id" in sql
    assert "confusion_matrix" in compiled.params.values()
    assert "learning_score_event" not in sql
    assert "LIMIT" not in sql.upper()


@pytest.mark.asyncio
async def test_report_by_id_rechecks_rights_before_exact_report_projection() -> None:
    session = _Session(_row())

    summary = await LearningScorerReportReadRepository(session).get_by_id(
        workspace_id=WORKSPACE_ID,
        report_id=REPORT_ID,
    )

    assert summary is not None
    assert summary.evaluation_report_id == REPORT_ID
    assert summary.report_hash == "a" * 64
    assert len(session.calls) == 3
    identity_statement, identity_parameters = session.calls[0]
    identity_sql = str(identity_statement.compile(dialect=postgresql.dialect()))
    assert "learning_scorer_test_evaluation_report.id =" in identity_sql
    assert REPORT_ID in identity_statement.compile(dialect=postgresql.dialect()).params.values()
    assert WORKSPACE_ID in identity_statement.compile(dialect=postgresql.dialect()).params.values()
    assert identity_parameters is None
    rights_statement, rights_parameters = session.calls[1]
    assert "wp06_recheck_scorer_test_report_source_rights" in str(rights_statement)
    assert rights_parameters == {"workspace_id": str(WORKSPACE_ID), "report_id": str(REPORT_ID)}


@pytest.mark.asyncio
async def test_latest_summary_keeps_legacy_v2_not_run_count_unknown() -> None:
    row = _row(not_run_count=None, input_excluded_count=4)
    metadata = dict(row["report_metadata"])
    metadata["confusion_matrix"] = None
    row["report_metadata"] = metadata
    session = _Session(row)

    summary = await LearningScorerReportReadRepository(session).get_latest(
        workspace_id=WORKSPACE_ID
    )

    assert summary is not None
    assert summary.not_run_count is None
    assert summary.no_candidate_count == 80
    assert summary.input_excluded_count == 4
    assert summary.confusion_matrix is None
    assert summary.positive_class_false_positive_count is None
    assert summary.positive_class_false_negative_count is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_count", [True, -1, 1.5])
async def test_latest_summary_rejects_invalid_not_run_count(invalid_count: object) -> None:
    row = _row()
    row["not_run_count"] = invalid_count
    session = _Session(row)

    with pytest.raises(ValueError, match="not_run_count"):
        await LearningScorerReportReadRepository(session).get_latest(workspace_id=WORKSPACE_ID)


@pytest.mark.parametrize("field", ["no_candidate_count", "input_excluded_count"])
@pytest.mark.parametrize("invalid_count", [True, -1, 1.5])
@pytest.mark.asyncio
async def test_latest_summary_rejects_invalid_inventory_count(
    field: str, invalid_count: object
) -> None:
    row = _row()
    row[field] = invalid_count

    with pytest.raises(ValueError, match=field):
        await LearningScorerReportReadRepository(_Session(row)).get_latest(
            workspace_id=WORKSPACE_ID
        )


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


@pytest.mark.parametrize(
    ("matrix", "sample_count", "abstain_count", "error_match"),
    [
        ([[1, 2, 3]], 9, 2, "3 hàng và 4 cột"),
        ([[True, 0, 0, 2], [0, 2, 0, 0], [0, 0, 3, 1]], 9, 3, "số nguyên không âm"),
        ([["2", 0, 0, 1], [0, 2, 0, 0], [0, 0, 3, 1]], 9, 2, "số nguyên không âm"),
        ([[-1, 0, 0, 4], [0, 2, 0, 0], [0, 0, 3, 1]], 9, 5, "số nguyên không âm"),
        ([[1, 0, 0, 1], [0, 2, 0, 0], [0, 0, 3, 1]], 9, 2, "class_support"),
        ([[2, 0, 0, 1], [0, 2, 0, 0], [0, 0, 3, 1]], 10, 2, "test_sample_count"),
        ([[2, 0, 0, 1], [0, 2, 0, 0], [0, 0, 3, 1]], 9, 3, "abstain_count"),
    ],
)
@pytest.mark.asyncio
async def test_latest_summary_rejects_invalid_confusion_matrix_contract(
    matrix: list[list[object]],
    sample_count: int,
    abstain_count: int,
    error_match: str,
) -> None:
    row = _row()
    metadata = dict(row["report_metadata"])
    metadata["confusion_matrix"] = matrix
    metadata["abstain_count"] = abstain_count
    row["test_sample_count"] = sample_count
    row["report_metadata"] = metadata

    with pytest.raises(ValueError, match=error_match):
        await LearningScorerReportReadRepository(_Session(row)).get_latest(
            workspace_id=WORKSPACE_ID
        )


@pytest.mark.asyncio
async def test_latest_summary_rejects_class_support_outside_canonical_class_order() -> None:
    row = _row()
    metadata = dict(row["report_metadata"])
    metadata["class_support"] = [[0, 2], [-1, 3], [1, 4]]
    row["report_metadata"] = metadata

    with pytest.raises(ValueError, match="class_support phải theo thứ tự lớp -1, 0, 1"):
        await LearningScorerReportReadRepository(_Session(row)).get_latest(
            workspace_id=WORKSPACE_ID
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("false_veto_count", [True, -1, 5])
async def test_latest_summary_rejects_false_veto_count_outside_positive_class_support(
    false_veto_count: object,
) -> None:
    row = _row()
    metadata = dict(row["report_metadata"])
    metadata["false_veto_count"] = false_veto_count
    row["report_metadata"] = metadata

    with pytest.raises(ValueError, match="false_veto_count"):
        await LearningScorerReportReadRepository(_Session(row)).get_latest(
            workspace_id=WORKSPACE_ID
        )


@pytest.mark.asyncio
async def test_latest_summary_rejects_false_veto_rate_mismatched_with_persisted_count() -> None:
    row = _row()
    metadata = dict(row["report_metadata"])
    metadata["false_veto_rate"] = 0.5
    row["report_metadata"] = metadata

    with pytest.raises(ValueError, match="false_veto_rate không khớp"):
        await LearningScorerReportReadRepository(_Session(row)).get_latest(
            workspace_id=WORKSPACE_ID
        )
