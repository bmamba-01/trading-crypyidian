"""Repository/store PostgreSQL cho evidence chung và job/decision theo bot (fake session)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from tests.unit.test_bot_dispatcher import (
    ACCOUNT_A,
    BOT_A,
    DECISION_TIME,
    WORKSPACE,
    FixtureSnapshots,
    _context,
    _evidence,
)

from trading_bot.adapters.database.models import (
    Base,
    BotDecisionJobRecord,
    BotDecisionRecord,
    OutboxEvent,
    SharedSignalEvidenceRecord,
)
from trading_bot.adapters.database.repositories.bot_decisions import (
    BotDecisionRepository,
    PostgresSignalDecisionStore,
)
from trading_bot.adapters.database.repositories.signal_evidence import (
    SharedSignalEvidenceRepository,
    SignalEvidenceRepositoryConflict,
)
from trading_bot.application.bot_dispatcher import (
    EVIDENCE_RECORDED_EVENT,
    BotDecision,
    BotDecisionConflict,
    BotDecisionJob,
    EvidenceScopeError,
    evaluate_bot_decision,
)
from trading_bot.application.outbox import OutboxRepository
from trading_bot.domain.strategy.registry import StrategyRegistry


class FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class FakeSession:
    """Session giả: trả kết quả scalar theo thứ tự và ghi lại SQL đã biên dịch."""

    def __init__(
        self,
        scalar_results: list[object | None] | None = None,
        *,
        scalars_results: list[list[object]] | None = None,
        flush_error: Exception | None = None,
    ) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.scalars_results = iter(scalars_results or [])
        self.added: list[object] = []
        self.statements: list[str] = []
        self.flush_error = flush_error
        self.info: dict[str, Any] = {}
        self.committed = False

    def _record(self, statement: Any) -> None:
        if hasattr(statement, "compile"):
            self.statements.append(str(statement.compile(dialect=postgresql.dialect())))

    async def scalar(self, statement: Any) -> object | None:
        self._record(statement)
        return next(self.scalar_results)

    async def scalars(self, statement: Any) -> FakeResult:
        self._record(statement)
        return FakeResult(next(self.scalars_results))

    async def execute(self, statement: Any, params: object = None) -> None:
        self._record(statement)

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        if self.flush_error is not None:
            error, self.flush_error = self.flush_error, None
            raise error

    @asynccontextmanager
    async def begin_nested(self):  # type: ignore[no-untyped-def]
        yield

    async def begin(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _evidence_row(evidence: Any, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": evidence.evidence_id,
        "workspace_id": evidence.workspace_id,
        "symbol": evidence.symbol,
        "setup_type": evidence.setup_type,
        "observed_at": evidence.observed_at,
        "available_at": evidence.available_at,
        "validity_end": evidence.validity_end,
        "feature_snapshot_id": evidence.feature_snapshot_id,
        "source_refs": list(evidence.source_refs),
        "model_release_id": evidence.model_release_id,
        "content_hash": evidence.content_hash,
        "evidence_hash": evidence.evidence_hash,
        "canonical_json": evidence.canonical_json(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _retry_of(evidence: Any) -> Any:
    """Cùng observation (feature/source/model) nhưng available_at/validity_end muộn hơn."""
    return _evidence(
        available_at=evidence.available_at + timedelta(minutes=2),
        validity_end=evidence.validity_end + timedelta(minutes=2),
    )


def _job() -> BotDecisionJob:
    return BotDecisionJob.create(
        evidence=_evidence(), context=_context(), evaluated_at=DECISION_TIME
    )


def _job_row(job: BotDecisionJob, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": job.job_id,
        "workspace_id": job.workspace_id,
        "bot_id": job.bot_id,
        "account_id": job.account_id,
        "evidence_id": job.evidence_id,
        "evidence_hash": job.evidence_hash,
        "bot_config_version_id": job.bot_config_version_id,
        "evaluated_at": job.evaluated_at,
        "context_hash": job.context.context_hash,
        "context_payload": job.canonical_payload(),
        "status": "PENDING",
        "completed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _decision(job: BotDecisionJob) -> BotDecision:
    return await evaluate_bot_decision(
        evidence=_evidence(), job=job, strategies=StrategyRegistry(), snapshots=FixtureSnapshots()
    )


def _decision_row(decision: BotDecision, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": decision.decision_id,
        "workspace_id": decision.workspace_id,
        "bot_id": decision.bot_id,
        "account_id": decision.account_id,
        "job_id": decision.job_id,
        "decision_hash": decision.decision_hash,
        "payload": decision.canonical_payload(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_schema_registers_workspace_evidence_and_bot_scoped_job_decision_tables() -> None:
    evidence = Base.metadata.tables["shared_signal_evidence"]
    forbidden = {"bot_id", "account_id", "trading_account_id", "quantity", "notional", "side"}

    assert not forbidden & set(evidence.columns.keys())
    # Dedupe observation ở DB không phụ thuộc timestamp do caller cấp.
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(column.name for column in constraint.columns) == ("workspace_id", "content_hash")
        for constraint in evidence.constraints
    )
    assert evidence.c.content_hash.nullable is False
    assert any(
        tuple(column.name for column in constraint.columns)
        == ("workspace_id", "feature_snapshot_id")
        and constraint.referred_table.name == "feature_snapshot"
        for constraint in evidence.foreign_key_constraints
    )
    for name in ("bot_decision_job", "bot_decision"):
        table = Base.metadata.tables[name]
        assert table.c.bot_id.nullable is False
        assert table.c.bot_config_version_id.nullable is False
        assert any(
            tuple(column.name for column in constraint.columns)
            == ("workspace_id", "bot_id", "account_id")
            and constraint.referred_table.name == "bot_instance"
            for constraint in table.foreign_key_constraints
        ), name
        assert any(
            tuple(column.name for column in constraint.columns) == ("workspace_id", "evidence_id")
            and constraint.referred_table.name == "shared_signal_evidence"
            for constraint in table.foreign_key_constraints
        ), name
        # Chưa có bảng bot_config_version (MB-02): cột config version không có FK.
        assert not table.c.bot_config_version_id.foreign_keys
        assert any(
            isinstance(index, Index)
            and index.name == f"ix_{name}_bot_scope"
            and tuple(column.name for column in index.columns)
            == ("workspace_id", "bot_id", "account_id")
            for index in table.indexes
        ), name
    decision = Base.metadata.tables["bot_decision"]
    assert any(
        tuple(column.name for column in constraint.columns)
        == ("workspace_id", "bot_id", "account_id", "job_id")
        and constraint.referred_table.name == "bot_decision_job"
        for constraint in decision.foreign_key_constraints
    )


async def test_append_evidence_persists_canonical_row_once() -> None:
    evidence = _evidence()
    session = FakeSession([None])

    result = await SharedSignalEvidenceRepository(session).append(evidence)  # type: ignore[arg-type]

    assert result.duplicate is False
    (row,) = session.added
    assert isinstance(row, SharedSignalEvidenceRecord)
    assert row.id == evidence.evidence_id
    assert row.workspace_id == WORKSPACE
    assert row.canonical_json == evidence.canonical_json()
    assert row.source_refs == list(evidence.source_refs)
    assert row.content_hash == evidence.content_hash
    assert "shared_signal_evidence.workspace_id =" in session.statements[0]
    assert "shared_signal_evidence.content_hash =" in session.statements[0]


async def test_append_retry_with_later_timestamps_returns_stored_original() -> None:
    original = _evidence()
    retry = _retry_of(original)
    session = FakeSession([_evidence_row(original)])

    result = await SharedSignalEvidenceRepository(session).append(retry)  # type: ignore[arg-type]

    assert result.duplicate is True
    assert result.evidence == original
    assert result.evidence_hash == original.evidence_hash
    assert session.added == []


async def test_concurrent_retry_insert_resolves_to_stored_original() -> None:
    original = _evidence()
    session = FakeSession(
        [None, _evidence_row(original)],
        flush_error=IntegrityError("insert", {}, RuntimeError("uq content_hash")),
    )

    result = await SharedSignalEvidenceRepository(session).append(_retry_of(original))  # type: ignore[arg-type]

    assert result.duplicate is True
    assert result.evidence == original


async def test_append_same_evidence_is_duplicate_and_different_payload_conflicts() -> None:
    evidence = _evidence()
    duplicate_session = FakeSession([_evidence_row(evidence)])

    result = await SharedSignalEvidenceRepository(duplicate_session).append(evidence)  # type: ignore[arg-type]

    assert result.duplicate is True
    assert duplicate_session.added == []
    tampered = FakeSession([_evidence_row(evidence, canonical_json='{"other":true}')])
    with pytest.raises(SignalEvidenceRepositoryConflict):
        await SharedSignalEvidenceRepository(tampered).append(evidence)  # type: ignore[arg-type]


async def test_concurrent_evidence_insert_rechecks_after_unique_violation() -> None:
    evidence = _evidence()
    session = FakeSession(
        [None, _evidence_row(evidence)],
        flush_error=IntegrityError("insert", {}, RuntimeError("unique")),
    )

    result = await SharedSignalEvidenceRepository(session).append(evidence)  # type: ignore[arg-type]

    assert result.duplicate is True


async def test_get_evidence_verifies_stored_hash_and_columns() -> None:
    evidence = _evidence()

    restored = await SharedSignalEvidenceRepository(
        FakeSession([_evidence_row(evidence)])  # type: ignore[arg-type]
    ).get(workspace_id=WORKSPACE, evidence_id=evidence.evidence_id)

    assert restored == evidence
    with pytest.raises(SignalEvidenceRepositoryConflict):
        await SharedSignalEvidenceRepository(
            FakeSession([_evidence_row(evidence, evidence_hash="0" * 64)])  # type: ignore[arg-type]
        ).get(workspace_id=WORKSPACE, evidence_id=evidence.evidence_id)
    with pytest.raises(SignalEvidenceRepositoryConflict):
        await SharedSignalEvidenceRepository(
            FakeSession([_evidence_row(evidence, symbol="ETHUSDT")])  # type: ignore[arg-type]
        ).get(workspace_id=WORKSPACE, evidence_id=evidence.evidence_id)


async def test_as_of_lookup_excludes_revisions_available_later() -> None:
    session = FakeSession([None])

    result = await SharedSignalEvidenceRepository(session).get_as_of(  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        symbol="BTCUSDT",
        setup_type="trend_breakout",
        observed_at=DECISION_TIME - timedelta(minutes=1),
        as_of=DECISION_TIME,
    )

    assert result is None
    sql = session.statements[0].lower()
    assert "shared_signal_evidence.workspace_id =" in sql
    assert "shared_signal_evidence.available_at <=" in sql
    assert "shared_signal_evidence.observed_at =" in sql
    assert "order by shared_signal_evidence.available_at desc" in sql


async def test_insert_job_dedupes_same_payload_and_rejects_different_payload() -> None:
    job = _job()
    new_session = FakeSession([None])

    stored = await BotDecisionRepository(new_session).insert_job(job)  # type: ignore[arg-type]

    assert stored == job
    (row,) = new_session.added
    assert isinstance(row, BotDecisionJobRecord)
    assert (row.workspace_id, row.bot_id, row.account_id) == (WORKSPACE, BOT_A, ACCOUNT_A)
    assert row.status == "PENDING"
    assert row.context_hash == job.context.context_hash
    sql = new_session.statements[0]
    assert "bot_decision_job.workspace_id =" in sql
    assert "bot_decision_job.bot_id =" in sql
    same = FakeSession([_job_row(job)])
    assert await BotDecisionRepository(same).insert_job(job) == job  # type: ignore[arg-type]
    assert same.added == []
    later = BotDecisionJob.create(
        evidence=_evidence(), context=_context(), evaluated_at=DECISION_TIME + timedelta(minutes=1)
    )
    with pytest.raises(BotDecisionConflict):
        await BotDecisionRepository(FakeSession([_job_row(later)])).insert_job(job)  # type: ignore[arg-type]


async def test_stored_job_with_tampered_context_is_rejected() -> None:
    job = _job()
    tampered = _job_row(job, context_hash="0" * 64)

    with pytest.raises(BotDecisionConflict):
        await BotDecisionRepository(FakeSession([tampered])).insert_job(job)  # type: ignore[arg-type]


async def test_decision_restore_verifies_hash_and_scope() -> None:
    job = _job()
    decision = await _decision(job)

    restored = await BotDecisionRepository(
        FakeSession([_decision_row(decision)])  # type: ignore[arg-type]
    ).decision_for_job(job)

    assert restored == decision
    with pytest.raises(BotDecisionConflict):
        await BotDecisionRepository(
            FakeSession([_decision_row(decision, decision_hash="0" * 64)])  # type: ignore[arg-type]
        ).decision_for_job(job)
    with pytest.raises(BotDecisionConflict):
        await BotDecisionRepository(
            FakeSession([_decision_row(decision, bot_id=UUID(int=7))])  # type: ignore[arg-type]
        ).decision_for_job(job)


async def test_insert_decision_writes_lineage_columns() -> None:
    job = _job()
    decision = await _decision(job)
    session = FakeSession([None])

    await BotDecisionRepository(session).insert_decision(decision)  # type: ignore[arg-type]

    (row,) = session.added
    assert isinstance(row, BotDecisionRecord)
    assert row.id == decision.decision_id
    assert row.job_id == job.job_id
    assert row.status == "CANDIDATE"
    assert row.candidate_id == decision.candidate_id
    assert row.bot_config_version_id == decision.bot_config_version_id
    assert row.strategy_release_hash == decision.strategy_release_hash
    assert row.payload == decision.canonical_payload()


async def test_claim_ready_can_filter_event_types() -> None:
    session = FakeSession(scalars_results=[[]])

    await OutboxRepository(session).claim_ready(  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        worker_id="worker-1",
        now=datetime.now(UTC),
        lease_duration=timedelta(seconds=30),
        max_attempts=3,
        event_types=(EVIDENCE_RECORDED_EVENT,),
    )

    assert "outbox_event.event_type IN" in session.statements[0]
    with pytest.raises(ValueError, match="event_types"):
        await OutboxRepository(FakeSession()).claim_ready(  # type: ignore[arg-type]
            workspace_id=WORKSPACE,
            worker_id="worker-1",
            now=datetime.now(UTC),
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
            event_types=(),
        )


class FakeSessions:
    def __init__(self, *sessions: FakeSession) -> None:
        self._sessions = iter(sessions)
        self.opened: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = next(self._sessions)
        self.opened.append(session)
        return session


async def test_store_records_evidence_and_outbox_event_in_one_transaction() -> None:
    evidence = _evidence()
    session = FakeSession([None])
    store = PostgresSignalDecisionStore(FakeSessions(session))  # type: ignore[arg-type]

    recorded = await store.record_evidence(evidence, evaluated_at=DECISION_TIME)

    assert recorded.created is True
    assert recorded.evidence == evidence
    assert session.committed is True
    evidence_rows = [row for row in session.added if isinstance(row, SharedSignalEvidenceRecord)]
    events = [row for row in session.added if isinstance(row, OutboxEvent)]
    assert len(evidence_rows) == 1
    assert len(events) == 1
    assert events[0].event_type == EVIDENCE_RECORDED_EVENT
    assert events[0].idempotency_key == str(evidence.evidence_id)
    assert events[0].payload["evidence_hash"] == evidence.evidence_hash
    assert "bot_id" not in events[0].payload


async def test_store_skips_outbox_event_for_duplicate_evidence() -> None:
    evidence = _evidence()
    session = FakeSession([_evidence_row(evidence)])
    store = PostgresSignalDecisionStore(FakeSessions(session))  # type: ignore[arg-type]

    recorded = await store.record_evidence(evidence, evaluated_at=DECISION_TIME)

    assert recorded.created is False
    assert session.added == []


async def test_store_retry_with_later_timestamps_returns_original_without_new_event() -> None:
    original = _evidence()
    session = FakeSession([_evidence_row(original)])
    store = PostgresSignalDecisionStore(FakeSessions(session))  # type: ignore[arg-type]

    recorded = await store.record_evidence(
        _retry_of(original), evaluated_at=DECISION_TIME + timedelta(minutes=3)
    )

    assert recorded.created is False
    assert recorded.evidence == original
    assert session.added == []


async def test_store_job_transaction_binds_account_and_rejects_bot_mismatch() -> None:
    job = _job()
    mismatch = FakeSession([SimpleNamespace(id=UUID(int=9))])
    store = PostgresSignalDecisionStore(FakeSessions(mismatch))  # type: ignore[arg-type]

    with pytest.raises(EvidenceScopeError):
        await store.enqueue_job(job)

    assert mismatch.added == []


async def test_store_completed_job_returns_stored_decision_without_reevaluation() -> None:
    job = _job()
    decision = await _decision(job)
    session = FakeSession(
        [
            SimpleNamespace(id=BOT_A),
            _job_row(job, status="COMPLETED", completed_at=DECISION_TIME),
            _decision_row(decision),
        ]
    )
    store = PostgresSignalDecisionStore(FakeSessions(session))  # type: ignore[arg-type]
    calls: list[BotDecisionJob] = []

    async def evaluate(_evidence: object, pending: BotDecisionJob) -> BotDecision:
        calls.append(pending)
        raise AssertionError("không được đánh giá lại job đã hoàn tất")

    assert await store.complete_job(job, evaluate) == decision
    assert calls == []
    assert any("FOR UPDATE" in statement for statement in session.statements)


async def test_store_pending_job_is_evaluated_saved_and_completed_once() -> None:
    job = _job()
    decision = await _decision(job)
    record = _job_row(job)
    session = FakeSession(
        [SimpleNamespace(id=BOT_A), record, _evidence_row(_evidence()), None],
    )
    store = PostgresSignalDecisionStore(  # type: ignore[arg-type]
        FakeSessions(session), clock=lambda: DECISION_TIME + timedelta(seconds=5)
    )

    async def evaluate(_evidence: object, pending: BotDecisionJob) -> BotDecision:
        assert pending == job
        return decision

    assert await store.complete_job(job, evaluate) == decision
    assert record.status == "COMPLETED"
    assert record.completed_at == DECISION_TIME + timedelta(seconds=5)
    assert [type(row) for row in session.added] == [BotDecisionRecord]
    assert session.committed is True
