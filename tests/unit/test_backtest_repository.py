from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.backtests import (
    BacktestRepository,
)
from trading_bot.application.backtest import BacktestRunner, ReplayManifest

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def make_manifest() -> ReplayManifest:
    return ReplayManifest(
        dataset_hash="a" * 64,
        range_start=NOW,
        range_end=NOW + timedelta(days=1),
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        strategy_release_id="strategy-v1",
        strategy_content_hash="b" * 64,
        feature_schema_hash="c" * 64,
        fill_model_version="paper-fill-v1",
        pipeline_content_hash="d" * 64,
        cost_assumptions=(("fee", "0.001"),),
        parameter_family_hash="e" * 64,
        trial_count=1,
        trial_index=0,
        seed=17,
    )


class FakeSession:
    def __init__(self, scalar_results: list[object | None] | None = None) -> None:
        self.scalar_results = iter(scalar_results or [])
        self.added: list[object] = []
        self.statements: list[str] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.scalar_results)

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.mark.asyncio
async def test_append_result_persists_run_trial_and_report_with_workspace_scope() -> None:
    manifest = make_manifest()
    result = BacktestRunner().run((), manifest=manifest, evaluator=lambda _: None)
    session = FakeSession([None, None, None])
    repository = BacktestRepository(session)  # type: ignore[arg-type]

    persisted = await repository.append_result(
        workspace_id=WORKSPACE_ID, manifest=manifest, result=result
    )

    assert persisted.duplicate is False
    assert len(session.added) == 3
    run, trial, report = session.added
    assert run.workspace_id == WORKSPACE_ID
    assert run.manifest_hash == manifest.content_hash
    assert trial.workspace_id == WORKSPACE_ID
    assert trial.trial_id == result.trial_record.trial_id
    assert report.workspace_id == WORKSPACE_ID
    assert report.report_hash == result.report_hash
    assert all("workspace_id =" in sql for sql in session.statements)


@pytest.mark.asyncio
async def test_append_result_is_idempotent_for_same_manifest_trial_and_report() -> None:
    manifest = make_manifest()
    result = BacktestRunner().run((), manifest=manifest, evaluator=lambda _: None)
    first_session = FakeSession([None, None, None])
    first_result = await BacktestRepository(first_session).append_result(
        workspace_id=WORKSPACE_ID, manifest=manifest, result=result
    )  # type: ignore[arg-type]
    session = FakeSession(first_session.added)
    repository = BacktestRepository(session)  # type: ignore[arg-type]

    persisted = await repository.append_result(
        workspace_id=WORKSPACE_ID, manifest=manifest, result=result
    )

    assert persisted.duplicate is True
    assert persisted.run_id == first_result.run_id
    assert persisted.trial_record_id == first_result.trial_record_id
    assert persisted.report_id == first_result.report_id
    assert session.added == []


@pytest.mark.asyncio
async def test_append_result_scopes_every_lookup_to_explicit_workspace() -> None:
    manifest = make_manifest()
    result = BacktestRunner().run((), manifest=manifest, evaluator=lambda _: None)
    other_workspace = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    session = FakeSession([None, None, None])
    repository = BacktestRepository(session)  # type: ignore[arg-type]

    await repository.append_result(
        workspace_id=other_workspace,
        manifest=manifest,
        result=result,
    )

    assert len(session.statements) == 3
    assert all("workspace_id =" in sql for sql in session.statements)
    assert all(row.workspace_id == other_workspace for row in session.added)


def test_backtest_records_are_append_only_workspace_rls_ready() -> None:
    from trading_bot.adapters.database.models.backtest_entities import (
        BacktestReportRecord,
        BacktestRunRecord,
        BacktestTrialRecord,
    )

    for model in (BacktestRunRecord, BacktestTrialRecord, BacktestReportRecord):
        assert "workspace_id" in model.__table__.c
        assert any(
            constraint.name and constraint.name.startswith("uq_")
            for constraint in model.__table__.constraints
        )
