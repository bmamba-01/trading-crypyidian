from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.backtest_outcomes import (
    BacktestOutcomeAppendResult,
)
from trading_bot.application.backtest_sensitivity import SensitivityScenario


@pytest.fixture(scope="module")
def replay_case():
    from tests.fixtures.runtime_replay import build_h01_dataset_runtime_replay

    return build_h01_dataset_runtime_replay(
        uuid4(),
        scenarios=(
            SensitivityScenario("base", "BASE"),
            SensitivityScenario(
                "higher-fees",
                "COST",
                cost_overrides=(
                    ("entry_fee_rate", "0.002"),
                    ("exit_fee_rate", "0.002"),
                ),
            ),
        ),
    )


class _UnitOfWork:
    def __init__(self, owner: _UowFactory, index: int) -> None:
        self.owner = owner
        self.index = index
        self.session = object()

    async def __aenter__(self) -> _UnitOfWork:
        assert not self.owner.active
        self.owner.active = True
        self.owner.active_uow = self
        self.owner.events.append(f"begin:{self.index}")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.owner.active = False
        self.owner.active_uow = None
        self.owner.events.append(f"rollback:{self.index}" if exc_type else f"commit:{self.index}")


class _UowFactory:
    def __init__(self) -> None:
        self.active = False
        self.events: list[str] = []
        self.calls = 0
        self.active_uow: _UnitOfWork | None = None
        self.used_sessions: list[tuple[str, object]] = []

    def __call__(self, workspace_id):
        del workspace_id
        self.calls += 1
        return _UnitOfWork(self, self.calls)


class _BundleLoader:
    def __init__(
        self, owner: _UowFactory, bundle, *, session: object, revoke_on_revalidate: bool = False
    ) -> None:
        self.owner = owner
        self.bundle = bundle
        self.revoke_on_revalidate = revoke_on_revalidate
        self.session = session

    async def load_for_replay(self, *, workspace_id, bundle_hash, now):
        assert self.owner.active
        assert workspace_id == self.bundle.workspace_id
        assert bundle_hash == self.bundle.dataset_hash
        assert now.tzinfo is not None
        self.owner.events.append("load")
        return self.bundle

    async def revalidate_current_rights(self, bundle, *, now):
        assert self.owner.active
        assert bundle is self.bundle
        assert now.tzinfo is not None
        self.owner.events.append("revalidate")
        assert self.owner.active_uow is not None
        self.owner.used_sessions.append(("rights", self.session))
        if self.revoke_on_revalidate:
            raise PermissionError("Quyền đã bị thu hồi trong lúc replay.")
        return bundle


class _OutcomeWriter:
    def __init__(self, owner: _UowFactory, *, session: object, fail_on: int | None = None) -> None:
        self.owner = owner
        self.fail_on = fail_on
        self.session = session
        self.calls: list[dict[str, Any]] = []

    async def append_sensitivity_run(self, **kwargs):
        assert self.owner.active
        self.calls.append(kwargs)
        self.owner.events.append(f"append:{len(self.calls)}")
        assert self.owner.active_uow is not None
        self.owner.used_sessions.append(("append", self.session))
        if self.fail_on == len(self.calls):
            raise RuntimeError("Lỗi giả lập khi ghi child outcome.")
        sensitivity_run = kwargs["sensitivity_run"]
        return BacktestOutcomeAppendResult(
            run_id=uuid4(),
            trial_record_id=uuid4(),
            report_id=uuid4(),
            scenario_id=sensitivity_run.scenario.scenario_id,
            scenario_hash="a" * 64,
            outcomes_hash="b" * 64,
            trade_count=len(kwargs["trade_outcomes"]),
            duplicate=False,
        )


def _service_type():
    from trading_bot.application.backtest_dataset_replay_execution import (
        BacktestDatasetReplayExecutionService,
    )

    return BacktestDatasetReplayExecutionService


def _make_service(
    replay_case,
    *,
    revoke_on_revalidate: bool = False,
    fail_on: int | None = None,
    job_persistence_factory=None,
):
    bundle, comparison = replay_case
    owner = _UowFactory()
    loaders: list[_BundleLoader] = []
    writers: list[_OutcomeWriter] = []

    def loader_factory(uow):
        assert uow.owner.active
        loader = _BundleLoader(
            owner, bundle, session=uow.session, revoke_on_revalidate=revoke_on_revalidate
        )
        loaders.append(loader)
        return loader

    def outcome_factory(uow):
        assert uow.owner.active
        writer = _OutcomeWriter(owner, session=uow.session, fail_on=fail_on)
        writers.append(writer)
        return writer

    def executor(loaded_bundle, manifest, *, progress_callback=None, cancel_requested=None):
        assert not owner.active
        assert loaded_bundle is bundle
        assert manifest == comparison.runs[0].sensitivity_run.manifest
        assert progress_callback is None or callable(progress_callback)
        assert cancel_requested is None or callable(cancel_requested)
        owner.events.append("execute")
        return comparison

    now = datetime(2026, 11, 3, 12, tzinfo=UTC)
    job_options = (
        {"job_persistence_factory": job_persistence_factory}
        if job_persistence_factory is not None
        else {}
    )
    service = _service_type()(
        load_unit_of_work_factory=owner,
        persist_unit_of_work_factory=owner,
        bundle_loader_factory=loader_factory,
        outcome_repository_factory=outcome_factory,
        executor=executor,
        clock=lambda: now,
        **job_options,
    )
    return service, owner, writers, bundle, comparison


@pytest.mark.asyncio
async def test_execution_loads_runs_without_transaction_then_revalidates_and_persists_all_children(
    replay_case,
):
    service, owner, writers, bundle, comparison = _make_service(replay_case)

    result = await service.execute(
        workspace_id=bundle.workspace_id,
        bundle_hash=bundle.dataset_hash,
        manifest=comparison.runs[0].sensitivity_run.manifest,
    )

    assert owner.events == [
        "begin:1",
        "load",
        "commit:1",
        "execute",
        "begin:2",
        "revalidate",
        "append:1",
        "append:2",
        "commit:2",
    ]
    assert owner.calls == 2
    assert len(writers) == 1
    assert len(writers[0].calls) == len(comparison.runs) == 2
    assert all(
        call["dataset_bundle"].dataset_hash == bundle.dataset_hash for call in writers[0].calls
    )
    assert result.bundle_hash == bundle.dataset_hash
    assert len(result.outcomes) == 2
    assert result.scenario_ids == ("base", "higher-fees")
    assert result.run_ids == tuple(outcome.run_id for outcome in result.outcomes)
    assert result.report_ids == tuple(outcome.report_id for outcome in result.outcomes)


@pytest.mark.asyncio
async def test_execution_rejects_manifest_bundle_mismatch_before_calling_executor(replay_case):
    service, owner, _, bundle, comparison = _make_service(replay_case)
    wrong_manifest = replace(
        comparison.runs[0].sensitivity_run.manifest,
        range_end=comparison.runs[0].sensitivity_run.manifest.range_end + timedelta(hours=1),
    )

    with pytest.raises(ValueError, match="evaluation window|bundle"):
        await service.execute(
            workspace_id=bundle.workspace_id,
            bundle_hash=bundle.dataset_hash,
            manifest=wrong_manifest,
        )

    assert owner.events == ["begin:1", "load", "commit:1"]
    assert owner.calls == 1


@pytest.mark.asyncio
async def test_execution_does_not_persist_when_rights_are_revoked_during_replay(replay_case):
    service, owner, writers, bundle, comparison = _make_service(
        replay_case, revoke_on_revalidate=True
    )

    with pytest.raises(PermissionError, match="thu hồi"):
        await service.execute(
            workspace_id=bundle.workspace_id,
            bundle_hash=bundle.dataset_hash,
            manifest=comparison.runs[0].sensitivity_run.manifest,
        )

    assert owner.events == [
        "begin:1",
        "load",
        "commit:1",
        "execute",
        "begin:2",
        "revalidate",
        "rollback:2",
    ]
    assert writers == []


@pytest.mark.asyncio
async def test_execution_rolls_back_the_shared_persist_unit_of_work_on_later_child_failure(
    replay_case,
):
    service, owner, writers, bundle, comparison = _make_service(replay_case, fail_on=2)

    with pytest.raises(RuntimeError, match="child outcome"):
        await service.execute(
            workspace_id=bundle.workspace_id,
            bundle_hash=bundle.dataset_hash,
            manifest=comparison.runs[0].sensitivity_run.manifest,
        )

    assert owner.events[-3:] == ["append:1", "append:2", "rollback:2"]
    assert owner.calls == 2
    assert len(writers) == 1
    assert len(writers[0].calls) == 2


@pytest.mark.asyncio
async def test_job_fence_uses_outcome_session_and_precedes_rights_check(replay_case):
    fences = []

    def factory(uow):
        fence = _JobFence(uow)
        fences.append(fence)
        return fence

    service, owner, writers, bundle, comparison = _make_service(
        replay_case, job_persistence_factory=factory
    )
    token = _job_token(bundle.workspace_id)
    result = await service.execute(
        workspace_id=bundle.workspace_id,
        bundle_hash=bundle.dataset_hash,
        manifest=comparison.runs[0].sensitivity_run.manifest,
        job_token=token,
    )
    assert owner.events == [
        "begin:1",
        "load",
        "commit:1",
        "execute",
        "begin:2",
        "job_begin",
        "revalidate",
        "append:1",
        "append:2",
        "job_complete",
        "commit:2",
    ]
    assert len(fences) == 1
    assert fences[0].token is token
    assert fences[0].run_ids == result.run_ids
    assert len(writers[0].calls) == len(comparison.runs)
    assert [label for label, _ in owner.used_sessions] == [
        "job_begin",
        "rights",
        "append",
        "append",
        "job_complete",
    ]
    assert all(session is fences[0].uow.session for _, session in owner.used_sessions)


class _JobFence:
    """Ranh giới job quan sát đúng UoW caller, lỗi mô phỏng xảy ra trước mọi append."""

    def __init__(self, uow, *, fail_begin=False, fail_complete=False):
        self.uow = uow
        self.fail_begin = fail_begin
        self.fail_complete = fail_complete
        self.token = None
        self.run_ids = None

    async def begin(self, token):
        assert self.uow.owner.active_uow is self.uow
        self.uow.owner.events.append("job_begin")
        self.uow.owner.used_sessions.append(("job_begin", self.uow.session))
        self.token = token
        if self.fail_begin:
            raise RuntimeError("WP07C_PERSIST_OWNER_INVALID")

    async def complete(self, token, result_run_ids):
        assert self.uow.owner.active_uow is self.uow
        assert token is self.token
        self.uow.owner.events.append("job_complete")
        self.uow.owner.used_sessions.append(("job_complete", self.uow.session))
        self.run_ids = result_run_ids
        if self.fail_complete:
            raise RuntimeError("Lỗi complete job.")


def _job_token(workspace_id):
    from trading_bot.application.backtest_job_persistence import BacktestJobPersistToken

    return BacktestJobPersistToken(
        workspace_id=workspace_id,
        job_id=uuid4(),
        worker_id="fixture-worker",
        generation=2,
    )


@pytest.mark.asyncio
async def test_stale_job_owner_writes_no_children(replay_case):
    service, owner, writers, bundle, comparison = _make_service(
        replay_case, job_persistence_factory=lambda uow: _JobFence(uow, fail_begin=True)
    )
    with pytest.raises(RuntimeError, match="WP07C_PERSIST_OWNER_INVALID"):
        await service.execute(
            workspace_id=bundle.workspace_id,
            bundle_hash=bundle.dataset_hash,
            manifest=comparison.runs[0].sensitivity_run.manifest,
            job_token=_job_token(bundle.workspace_id),
        )
    assert owner.events == [
        "begin:1",
        "load",
        "commit:1",
        "execute",
        "begin:2",
        "job_begin",
        "rollback:2",
    ]
    assert writers == []


@pytest.mark.asyncio
async def test_second_child_failure_rolls_back_job_completion_and_outcomes(replay_case):
    service, owner, writers, bundle, comparison = _make_service(
        replay_case, fail_on=2, job_persistence_factory=lambda uow: _JobFence(uow)
    )
    with pytest.raises(RuntimeError, match="child outcome"):
        await service.execute(
            workspace_id=bundle.workspace_id,
            bundle_hash=bundle.dataset_hash,
            manifest=comparison.runs[0].sensitivity_run.manifest,
            job_token=_job_token(bundle.workspace_id),
        )
    assert owner.events[-4:] == ["revalidate", "append:1", "append:2", "rollback:2"]
    assert "job_complete" not in owner.events
    assert len(writers[0].calls) == 2


@pytest.mark.asyncio
async def test_job_complete_failure_rolls_back_outcome_transaction(replay_case):
    service, owner, writers, bundle, comparison = _make_service(
        replay_case, job_persistence_factory=lambda uow: _JobFence(uow, fail_complete=True)
    )
    with pytest.raises(RuntimeError, match="complete job"):
        await service.execute(
            workspace_id=bundle.workspace_id,
            bundle_hash=bundle.dataset_hash,
            manifest=comparison.runs[0].sensitivity_run.manifest,
            job_token=_job_token(bundle.workspace_id),
        )
    assert owner.events[-2:] == ["job_complete", "rollback:2"]
    assert len(writers[0].calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["missing_factory", "wrong_workspace", "wrong_type"])
async def test_job_token_requires_factory_and_matching_workspace(replay_case, configuration):
    factory = None if configuration == "missing_factory" else lambda uow: _JobFence(uow)
    service, owner, writers, bundle, comparison = _make_service(
        replay_case, job_persistence_factory=factory
    )
    token = _job_token(uuid4() if configuration == "wrong_workspace" else bundle.workspace_id)
    if configuration == "wrong_type":
        token = object()
    with pytest.raises(ValueError, match="job_token|workspace|factory"):
        await service.execute(
            workspace_id=bundle.workspace_id,
            bundle_hash=bundle.dataset_hash,
            manifest=comparison.runs[0].sensitivity_run.manifest,
            job_token=token,
        )
    assert owner.events == []
    assert writers == []


def test_job_token_is_immutable():
    token = _job_token(uuid4())
    with pytest.raises(FrozenInstanceError):
        token.generation = 3


@pytest.mark.asyncio
async def test_queued_job_does_not_report_persist_phase_before_database_lock(replay_case):
    service, _, _, bundle, comparison = _make_service(
        replay_case, job_persistence_factory=lambda uow: _JobFence(uow)
    )
    phases = []

    async def phase_callback(phase):
        phases.append(phase)

    await service.execute(
        workspace_id=bundle.workspace_id,
        bundle_hash=bundle.dataset_hash,
        manifest=comparison.runs[0].sensitivity_run.manifest,
        job_token=_job_token(bundle.workspace_id),
        phase_callback=phase_callback,
    )
    assert phases == ["load", "replay"]


@pytest.mark.asyncio
async def test_execution_forwards_exact_runtime_opportunities_in_persist_transaction():
    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.runtime_replay_evaluator import run_runtime_paper_replay

    source, observations, _, manifest, factory = _pinned_setup(cross=True)
    comparison = run_runtime_paper_replay(
        observations, manifest=manifest, runtime_evaluator_factory=factory
    )
    service, owner, writers, _, _ = _make_service((source, comparison))
    manifest = comparison.runs[0].sensitivity_run.manifest
    await service.execute(
        workspace_id=source.workspace_id, bundle_hash=source.dataset_hash, manifest=manifest
    )
    assert writers[0].calls[0]["opportunities"] is comparison.runs[0].opportunities
    assert owner.events[-1] == "commit:2"
