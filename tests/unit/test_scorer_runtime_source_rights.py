from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from tests.unit.test_bot_decision_source_bars import _session_for, _snapshot
from tests.unit.test_signal_decision_repository import _job

from trading_bot.adapters.database.repositories.scorer_runtime_source_rights import (
    ScorerDecisionSourceLineageUnavailable,
    authorize_job_source_scope,
    persist_decision_feature_snapshots,
    stage_job_source_bars,
)

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _ScalarResult:
    def __init__(self, value: object, rows: list[dict[str, object]] | None = None) -> None:
        self.value = value
        self.rows = rows or []

    def mappings(self) -> _ScalarResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows

    def scalar_one(self) -> object:
        return self.value

    def scalar_one_or_none(self) -> object:
        return self.value


class _StagingSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.executions: list[tuple[object, object]] = []
        self.flush_count = 0

    async def execute(self, statement: object, params: object = None) -> _ScalarResult:
        self.executions.append((statement, params))
        return _ScalarResult(None, self.rows if len(self.executions) == 1 else [])

    async def flush(self) -> None:
        self.flush_count += 1


@pytest.mark.asyncio
async def test_stage_job_source_bars_stages_only_metadata_and_exact_release_timeframes() -> None:
    snapshot = _snapshot()
    metadata_session = _session_for(snapshot)
    session = _StagingSession(metadata_session.rows)
    job = _job()
    assert snapshot.workspace_id == job.workspace_id

    rows = await stage_job_source_bars(
        session,  # type: ignore[arg-type]
        job=job,
        snapshot=snapshot,
    )

    assert len(rows) == 10
    assert session.flush_count == 1
    query = session.executions[0][0]
    assert "market_bar" in str(query)
    selected_columns = tuple(query.selected_columns.keys())
    assert "open" not in selected_columns
    assert "volume" not in selected_columns
    insert_sql, parameters = session.executions[1]
    assert "bot_decision_job_source_bar" in str(insert_sql)
    assert isinstance(parameters, list)
    assert len(parameters) == 10
    assert [row["timeframe"] for row in parameters] == ["1h"] * 8 + ["4h"] * 2
    assert [row["ordinal"] for row in parameters] == list(range(8)) + [0, 1]
    assert {row["job_id"] for row in parameters} == {job.job_id}
    assert {row["workspace_id"] for row in parameters} == {WORKSPACE_ID}
    assert {row["bot_id"] for row in parameters} == {job.bot_id}
    assert {row["evidence_id"] for row in parameters} == {job.evidence_id}
    assert {row["evidence_hash"] for row in parameters} == {job.evidence_hash}
    assert all(
        set(row)
        == {
            "id",
            "workspace_id",
            "bot_id",
            "job_id",
            "evidence_id",
            "evidence_hash",
            "decision_snapshot_id",
            "decision_time",
            "timeframe",
            "feature_snapshot_id",
            "market_bar_id",
            "source_event_id",
            "source_event_digest",
            "ordinal",
            "timeframe_bar_count",
            "snapshot_timeframe_count",
        }
        for row in parameters
    )


@pytest.mark.asyncio
async def test_stage_job_source_bars_rejects_forged_incomplete_snapshot_timeframe_set() -> None:
    from dataclasses import replace

    snapshot = _snapshot()
    incomplete = replace(
        snapshot,
        series=snapshot.series[:1],
        timeframe_lineage=snapshot.timeframe_lineage[:1],
        feature_snapshots=snapshot.feature_snapshots[:1],
    )
    session = _StagingSession([])

    with pytest.raises(ScorerDecisionSourceLineageUnavailable):
        await stage_job_source_bars(
            session,  # type: ignore[arg-type]
            job=_job(),
            snapshot=incomplete,
        )

    assert session.executions == []


@pytest.mark.asyncio
async def test_stage_job_source_bars_rejects_future_event_after_provider_revision_selection() -> (
    None
):
    from dataclasses import replace
    from datetime import timedelta

    snapshot = _snapshot()
    first_series = snapshot.series[0]
    first_bar = first_series.bars[0]
    future = snapshot.decision_time + timedelta(microseconds=1)
    forged = replace(
        snapshot,
        series=(
            replace(first_series, bars=(first_bar, *first_series.bars[1:])),
            *snapshot.series[1:],
        ),
    )
    # Bar DB phải đạt event_at <= available_at; bản giả kiểm trực tiếp gate causal.
    object.__setattr__(first_bar, "event_at", future)
    session = _StagingSession([])

    with pytest.raises(ScorerDecisionSourceLineageUnavailable):
        await stage_job_source_bars(
            session,  # type: ignore[arg-type]
            job=_job(),
            snapshot=forged,
        )

    assert session.executions == []


@pytest.mark.asyncio
async def test_stage_job_source_bars_rejects_foreign_snapshot_workspace_before_db_access() -> None:
    from dataclasses import replace

    snapshot = _snapshot()
    foreign_workspace = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    foreign_lineage = (
        replace(snapshot.timeframe_lineage[0], workspace_id=foreign_workspace),
        *snapshot.timeframe_lineage[1:],
    )
    foreign_features = (
        replace(snapshot.feature_snapshots[0], workspace_id=foreign_workspace),
        *snapshot.feature_snapshots[1:],
    )
    forged = replace(
        snapshot,
        timeframe_lineage=foreign_lineage,
        feature_snapshots=foreign_features,
    )
    session = _StagingSession([])

    with pytest.raises(ScorerDecisionSourceLineageUnavailable):
        await stage_job_source_bars(
            session,  # type: ignore[arg-type]
            job=_job(),
            snapshot=forged,
        )

    assert session.executions == []


@pytest.mark.asyncio
async def test_stage_job_source_bars_rejects_missing_lineage_before_database_access() -> None:
    session = _StagingSession([])

    with pytest.raises(ScorerDecisionSourceLineageUnavailable):
        await stage_job_source_bars(
            session,  # type: ignore[arg-type]
            job=_job(),
            snapshot=SimpleNamespace(snapshot_id="a" * 64),  # type: ignore[arg-type]
        )

    assert session.executions == []


@pytest.mark.asyncio
async def test_stage_job_source_bars_rejects_unknown_market_bar_before_insert() -> None:
    snapshot = _snapshot()
    source_session = _session_for(snapshot)
    source_session.rows.pop()
    session = _StagingSession(source_session.rows)

    with pytest.raises(ScorerDecisionSourceLineageUnavailable):
        await stage_job_source_bars(
            session,  # type: ignore[arg-type]
            job=_job(),
            snapshot=snapshot,
        )

    assert len(session.executions) == 1
    assert "bot_decision_job_source_bar" not in str(session.executions[0][0])
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_persist_feature_snapshots_validates_all_canonical_lineage_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from trading_bot.adapters.database.repositories import scorer_runtime_source_rights as module

    snapshot = _snapshot()
    corrupted_feature = replace(
        snapshot.feature_snapshots[1],
        input_bar_ids=("0" * 64, *snapshot.feature_snapshots[1].input_bar_ids[1:]),
    )
    corrupted = replace(
        snapshot,
        feature_snapshots=(snapshot.feature_snapshots[0], corrupted_feature),
    )
    appended: list[object] = []

    class FeatureRepository:
        def __init__(self, _session) -> None:
            pass

        async def append(self, **kwargs) -> None:
            appended.append(kwargs["snapshot"])

    monkeypatch.setattr(module, "FeatureSnapshotRepository", FeatureRepository)

    with pytest.raises(ScorerDecisionSourceLineageUnavailable, match="canonical input"):
        await persist_decision_feature_snapshots(
            _StagingSession([]),  # type: ignore[arg-type]
            job=_job(),
            snapshot=corrupted,
        )

    assert appended == []


@pytest.mark.asyncio
async def test_persist_feature_snapshots_appends_only_verified_workspace_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_bot.adapters.database.repositories import scorer_runtime_source_rights as module

    snapshot = _snapshot()
    appended: list[object] = []

    class FeatureRepository:
        def __init__(self, _session) -> None:
            pass

        async def append(self, *, workspace_id, snapshot) -> None:
            assert workspace_id == WORKSPACE_ID
            appended.append(snapshot)

    monkeypatch.setattr(module, "FeatureSnapshotRepository", FeatureRepository)

    await persist_decision_feature_snapshots(
        _StagingSession([]),  # type: ignore[arg-type]
        job=_job(),
        snapshot=snapshot,
    )

    assert tuple(appended) == snapshot.feature_snapshots


@pytest.mark.asyncio
async def test_job_source_scope_gateway_receives_only_workspace_and_locked_job_identity() -> None:
    fingerprint = "f" * 64
    session = _StagingSession([])
    job = _job()
    session.execute = _gateway_execute(  # type: ignore[method-assign]
        session, fingerprint
    )

    result = await authorize_job_source_scope(
        session,  # type: ignore[arg-type]
        workspace_id=WORKSPACE_ID,
        job_id=job.job_id,
    )

    assert result == fingerprint
    assert len(session.executions) == 2
    gate_sql, gate_params = session.executions[1]
    assert "wp06_authorize_scorer_job_source_scope" in str(gate_sql)
    assert gate_params == {"workspace_id": str(WORKSPACE_ID), "job_id": str(job.job_id)}
    assert "scope" not in str(gate_params).lower()
    assert "bar" not in str(gate_params).lower()


def _gateway_execute(session: _StagingSession, fingerprint: str):
    async def execute(statement: object, params: object = None) -> _ScalarResult:
        session.executions.append((statement, params))
        return _ScalarResult(fingerprint)

    return execute
