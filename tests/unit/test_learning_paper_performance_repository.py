from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from trading_bot.adapters.database import models
from trading_bot.adapters.database.models.learning_paper_performance_entities import (
    LearningPaperPerformanceReportRecord,
)
from trading_bot.adapters.database.repositories.learning_paper_performance import (
    LearningPaperPerformanceRepository,
    PaperPerformanceAggregateUnavailable,
)


def test_performance_report_table_is_workspace_scoped_aggregate_only_and_immutable() -> None:
    assert hasattr(models, "LearningPaperPerformanceReportRecord")
    table = models.LearningPaperPerformanceReportRecord.__table__
    assert table.name == "learning_paper_performance_report"
    assert not {
        "probabilities",
        "labels",
        "trade_id",
        "decision_id",
        "trade_payload",
        "feature_payload",
    } & set(table.c.keys())

    constraints = tuple(table.constraints)
    assert any(
        isinstance(item, UniqueConstraint)
        and {column.name for column in item.columns} == {"workspace_id", "idempotency_key"}
        for item in constraints
    )
    references = [
        {element.target_fullname for element in item.elements}
        for item in constraints
        if isinstance(item, ForeignKeyConstraint)
    ]
    assert {
        "learning_scorer_test_evaluation_report.workspace_id",
        "learning_scorer_test_evaluation_report.id",
    } in references
    checks = " ".join(
        str(item.sqltext) for item in constraints if isinstance(item, CheckConstraint)
    )
    assert "target_fingerprint" in checks
    assert "report_hash" in checks
    assert "jsonb_typeof(report) = 'object'" in checks


class _AggregateResult:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def mappings(self) -> _AggregateResult:
        return self

    def all(self) -> tuple[dict[str, object], ...]:
        return self._rows

    def first(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> dict[str, object] | None:
        if len(self._rows) > 1:
            raise AssertionError("Query metadata trả nhiều hơn một hàng.")
        return self.first()


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def one_or_none(self) -> object:
        return self.value


class _RepositorySession:
    def __init__(
        self,
        *,
        rows: tuple[dict[str, object], ...] = (),
        scalar: object = None,
        report: object = None,
    ) -> None:
        self.rows = rows
        self.scalar_value = scalar
        self.report = report
        self.calls: list[tuple[object, object]] = []

    async def execute(self, statement: object, params: object = None) -> _AggregateResult:
        self.calls.append((statement, params))
        return _AggregateResult(self.rows)

    async def scalar(self, statement: object, params: object = None) -> object:
        self.calls.append((statement, params))
        return self.scalar_value

    async def scalars(self, statement: object, params: object = None) -> _ScalarResult:
        self.calls.append((statement, params))
        return _ScalarResult(self.report)


def _aggregate_rows(rights_digests: tuple[object, object]) -> tuple[dict[str, object], ...]:
    return (
        {"cohort_name": "older", "rights_digest": rights_digests[0]},
        {"cohort_name": "newer", "rights_digest": rights_digests[1]},
    )


@pytest.mark.asyncio
async def test_aggregate_requires_one_identical_current_rights_digest() -> None:
    session = _RepositorySession(rows=_aggregate_rows(("a" * 64, "a" * 64)))
    repository = LearningPaperPerformanceRepository(session)  # type: ignore[arg-type]

    rows = await repository.aggregate_active_target(
        workspace_id=uuid4(),
        bot_id=uuid4(),
        target_fingerprint="b" * 64,
        baseline_report_id=uuid4(),
        baseline_report_hash="c" * 64,
        as_of=datetime(2026, 11, 14, tzinfo=UTC),
    )

    assert len(rows) == 2
    assert rows[0]["rights_digest"] == rows[1]["rights_digest"] == "a" * 64


@pytest.mark.parametrize(
    "rights_digests",
    [
        ("a" * 64, "b" * 64),
        ("a" * 64, None),
        ("A" * 64, "A" * 64),
        ("not-a-digest", "not-a-digest"),
        (True, True),
    ],
)
@pytest.mark.asyncio
async def test_aggregate_denies_missing_malformed_or_mixed_rights_digest(
    rights_digests: tuple[object, object],
) -> None:
    session = _RepositorySession(rows=_aggregate_rows(rights_digests))
    repository = LearningPaperPerformanceRepository(session)  # type: ignore[arg-type]

    with pytest.raises(PaperPerformanceAggregateUnavailable):
        await repository.aggregate_active_target(
            workspace_id=uuid4(),
            bot_id=uuid4(),
            target_fingerprint="b" * 64,
            baseline_report_id=uuid4(),
            baseline_report_hash="c" * 64,
            as_of=datetime(2026, 11, 14, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_report_current_rights_calls_narrow_database_recheck_and_returns_bool_only() -> None:
    workspace_id = uuid4()
    report_id = uuid4()
    session = _RepositorySession(scalar=True)
    repository = LearningPaperPerformanceRepository(session)  # type: ignore[arg-type]

    authorized = await repository.report_current_rights(
        workspace_id=workspace_id,
        report_id=report_id,
    )

    assert authorized is True
    statement, params = session.calls[0]
    assert "wp06_recheck_paper_performance_report_rights" in str(statement)
    assert "CAST(:workspace_id AS uuid)" in str(statement)
    assert "CAST(:report_id AS uuid)" in str(statement)
    assert params == {"workspace_id": str(workspace_id), "report_id": str(report_id)}


@pytest.mark.asyncio
async def test_report_current_rights_fails_closed_for_non_boolean_database_value() -> None:
    session = _RepositorySession(scalar=SimpleNamespace(authorized=True))
    repository = LearningPaperPerformanceRepository(session)  # type: ignore[arg-type]

    authorized = await repository.report_current_rights(
        workspace_id=uuid4(),
        report_id=uuid4(),
    )

    assert authorized is False


@pytest.mark.asyncio
async def test_idempotency_lookup_selects_only_metadata_reference() -> None:
    report_id = uuid4()
    bot_id = uuid4()
    session = _RepositorySession(rows=({"id": report_id, "bot_id": bot_id},))
    repository = LearningPaperPerformanceRepository(session)  # type: ignore[arg-type]

    reference = await repository.get_idempotency_reference(
        workspace_id=uuid4(), idempotency_key=uuid4()
    )

    assert reference is not None
    assert reference.report_id == report_id
    assert reference.bot_id == bot_id
    statement = str(session.calls[0][0])
    assert "learning_paper_performance_report.id" in statement
    assert "learning_paper_performance_report.bot_id" in statement
    assert "learning_paper_performance_report.report" not in statement
    assert "canonical_report" not in statement
    assert "report_hash" not in statement


@pytest.mark.asyncio
async def test_snapshot_and_recent_queries_return_metadata_references_only() -> None:
    report_id = uuid4()
    bot_id = uuid4()
    workspace_id = uuid4()
    rows = (
        {"id": report_id, "bot_id": bot_id},
        {"id": uuid4(), "bot_id": uuid4()},
    )
    session = _RepositorySession(rows=(rows[0],))
    repository = LearningPaperPerformanceRepository(session)  # type: ignore[arg-type]

    snapshot = await repository.get_snapshot_reference(
        workspace_id=workspace_id,
        target_fingerprint="a" * 64,
        policy_hash="b" * 64,
        as_of=datetime(2026, 11, 14, tzinfo=UTC),
        membership_digest="c" * 64,
    )
    session.rows = rows
    recent = await repository.list_recent_references(workspace_id=workspace_id)

    assert snapshot is not None
    assert snapshot.report_id == report_id
    assert snapshot.bot_id == bot_id
    assert tuple((item.report_id, item.bot_id) for item in recent) == tuple(
        (row["id"], row["bot_id"]) for row in rows
    )
    for statement, _params in session.calls:
        sql = str(statement)
        assert "learning_paper_performance_report.report" not in sql
        assert "canonical_report" not in sql
        assert "report_hash" not in sql


@pytest.mark.asyncio
async def test_payload_read_uses_rights_checked_security_definer_gateway() -> None:
    report = SimpleNamespace(id=uuid4())
    session = _RepositorySession(report=report)
    repository = LearningPaperPerformanceRepository(session)  # type: ignore[arg-type]

    result = await repository.get_report_by_id(workspace_id=uuid4(), report_id=report.id)

    assert result is report
    statement = str(session.calls[0][0])
    assert "wp06_read_paper_performance_report" in statement
    assert "FROM learning_paper_performance_report" not in statement


@pytest.mark.asyncio
async def test_report_insert_uses_narrow_database_writer_without_orm_table_insert() -> None:
    report = LearningPaperPerformanceReportRecord(
        id=uuid4(),
        workspace_id=uuid4(),
        bot_id=uuid4(),
        baseline_report_id=uuid4(),
        baseline_report_hash="a" * 64,
        policy_version="wp06-paper-performance-v1",
        policy_hash="b" * 64,
        target_fingerprint="c" * 64,
        membership_digest="d" * 64,
        as_of=datetime(2026, 11, 14, tzinfo=UTC),
        status="inconclusive",
        idempotency_key=uuid4(),
        report_hash="e" * 64,
        report={"schema_version": "crypidian.learning.paper-performance.v1"},
        canonical_report='{"schema_version":"crypidian.learning.paper-performance.v1"}',
    )
    session = _RepositorySession()
    repository = LearningPaperPerformanceRepository(session)  # type: ignore[arg-type]

    persisted = await repository.insert(report)

    assert persisted is report
    assert len(session.calls) == 1
    statement, params = session.calls[0]
    sql = str(statement)
    assert "wp06_insert_paper_performance_report" in sql
    assert "INSERT INTO learning_paper_performance_report" not in sql
    assert params["report_id"] == str(report.id)
    assert params["workspace_id"] == str(report.workspace_id)
    assert params["canonical_report"] == report.canonical_report
