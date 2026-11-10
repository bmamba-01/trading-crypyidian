"""Worker bàn giao monitor sang khóa persist trước khi ghi bất kỳ outcome nào."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from tests.fixtures.runtime_replay import build_h01_dataset_runtime_replay

from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeAppendResult
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.workers import backtest_replay_worker as module


@pytest.fixture(scope="module")
def worker_case():
    return build_h01_dataset_runtime_replay(uuid4())


def setup_worker(
    monkeypatch, worker_case, *, begin_error=None, child_error=False, monitor_error=False
):
    bundle, comparison = worker_case
    events = []
    writes = []
    sessions = []
    monitor_started = asyncio.Event()
    monitor_stopped = asyncio.Event()
    persist_active = False
    request = BacktestReplayJobSubmission(
        bundle_hash=bundle.dataset_hash,
        strategy_release_id="h01-v1",
        execution_interval="1h",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.002"),
        seed=17,
    )
    payload = request.to_payload()
    claim = module._Claim(
        job_id=uuid4(),
        status="RUNNING",
        generation=3,
        progress=0,
        request_payload=payload,
        canonical_request=json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        bundle_hash=request.bundle_hash,
        strategy_release_id=request.strategy_release_id,
        execution_interval=request.execution_interval,
    )

    @asynccontextmanager
    async def read_uow(workspace_id):
        assert workspace_id == bundle.workspace_id
        assert not persist_active
        events.append("read")
        yield SimpleNamespace(session=object())

    @asynccontextmanager
    async def persist_uow(workspace_id):
        nonlocal persist_active
        assert workspace_id == bundle.workspace_id
        assert monitor_stopped.is_set(), "Monitor chưa được await dừng trước persist UoW."
        persist_active = True
        session = object()
        sessions.append(session)
        events.append("persist_enter")
        try:
            yield SimpleNamespace(session=session)
        except BaseException:
            writes.clear()
            events.append("rollback")
            raise
        else:
            events.append("commit")
        finally:
            persist_active = False

    class Loader:
        async def load_for_replay(self, **kwargs):
            return bundle

        async def revalidate_current_rights(self, source, **kwargs):
            assert persist_active
            events.append("rights")
            return source

    class Outcomes:
        def __init__(self, session):
            assert session is sessions[-1]

        async def append_sensitivity_run(self, **kwargs):
            assert persist_active
            assert monitor_stopped.is_set()
            events.append("append")
            if child_error:
                raise RuntimeError("Lỗi child fixture.")
            result = BacktestOutcomeAppendResult(
                run_id=uuid4(),
                trial_record_id=uuid4(),
                report_id=uuid4(),
                scenario_id=kwargs["sensitivity_run"].scenario.scenario_id,
                scenario_hash="a" * 64,
                outcomes_hash="b" * 64,
                trade_count=0,
                duplicate=False,
            )
            writes.append(result)
            return result

    class Persistence:
        def __init__(self, session):
            assert session is sessions[-1]

        async def begin(self, **identity):
            assert monitor_stopped.is_set()
            assert persist_active
            assert identity == {
                "workspace_id": bundle.workspace_id,
                "job_id": claim.job_id,
                "worker_id": "worker-A",
                "generation": 3,
            }
            events.append("job_begin")
            if begin_error:
                raise DBAPIError("SELECT begin", {}, Exception(begin_error))

        async def complete(self, **identity):
            assert persist_active
            assert identity["result_run_ids"] == tuple(row.run_id for row in writes)
            events.append("job_complete")

    def executor(*args, **kwargs):
        assert not persist_active
        return comparison

    monkeypatch.setattr(module, "_bundle_loader", lambda *args: Loader())
    monkeypatch.setattr(
        module,
        "build_backtest_replay_plan",
        lambda *args: SimpleNamespace(
            manifest=comparison.runs[0].sensitivity_run.manifest,
            executor=executor,
        ),
    )
    monkeypatch.setattr(module, "BacktestOutcomeRepository", Outcomes)
    monkeypatch.setattr(module, "BacktestJobPersistenceRepository", Persistence, raising=False)
    worker = module.BacktestReplayJobWorker(
        read_sessions=None,
        persist_sessions=None,
        workspace_id=bundle.workspace_id,
        worker_id="worker-A",
        read_unit_of_work_factory=read_uow,
        persist_unit_of_work_factory=persist_uow,
    )

    async def monitor(*args):
        monitor_started.set()
        try:
            if monitor_error:
                raise RuntimeError("Monitor fixture thất bại.")
            await args[-1].wait()
        finally:
            await asyncio.sleep(0)
            monitor_stopped.set()
            events.append("monitor_stopped")

    async def progress(*args, stage, **kwargs):
        await monitor_started.wait()
        assert not persist_active
        events.append(f"progress:{stage}")

    async def finish(*args, status, **kwargs):
        assert not persist_active
        events.append(f"finish:{status}")
        return True

    monkeypatch.setattr(worker, "_monitor", monitor)
    monkeypatch.setattr(worker, "_update_progress", progress)
    monkeypatch.setattr(worker, "_finish", finish)
    return worker, claim, events, writes


async def test_monitor_is_awaited_before_persist_begin_and_success_has_no_second_transaction(
    monkeypatch,
    worker_case,
):
    worker, claim, events, writes = setup_worker(monkeypatch, worker_case)
    await worker._process(claim)
    assert writes
    assert events.index("monitor_stopped") < events.index("persist_enter")
    assert events[events.index("persist_enter") :] == [
        "persist_enter",
        "job_begin",
        "rights",
        "append",
        "job_complete",
        "commit",
    ]
    assert "progress:persist" not in events
    assert not any(event.startswith("finish:") for event in events)


@pytest.mark.parametrize("reason", ["WP07C_PERSIST_OWNER_INVALID", "WP07C_PERSIST_LEASE_EXPIRED"])
async def test_lease_lost_after_monitor_stop_is_rejected_without_children_or_new_owner_update(
    monkeypatch,
    worker_case,
    reason,
):
    worker, claim, events, writes = setup_worker(monkeypatch, worker_case, begin_error=reason)
    await worker._process(claim)
    assert writes == []
    assert events[-3:] == ["persist_enter", "job_begin", "rollback"]
    assert not any(event.startswith("finish:") for event in events)


async def test_cancel_arriving_after_monitor_stop_before_begin_finishes_cancelled(
    monkeypatch,
    worker_case,
):
    worker, claim, events, writes = setup_worker(
        monkeypatch,
        worker_case,
        begin_error="WP07C_PERSIST_CANCELLED",
    )
    await worker._process(claim)
    assert writes == []
    assert events[-2:] == ["rollback", "finish:CANCELLED"]


async def test_child_failure_rolls_back_before_conditional_failure_cleanup(
    monkeypatch, worker_case
):
    worker, claim, events, writes = setup_worker(monkeypatch, worker_case, child_error=True)
    await worker._process(claim)
    assert writes == []
    assert events[-3:] == ["append", "rollback", "finish:FAILED"]
    assert "job_complete" not in events


async def test_monitor_failure_is_logged_and_failure_cleanup_runs_once(
    monkeypatch,
    worker_case,
    caplog,
):
    worker, claim, events, writes = setup_worker(monkeypatch, worker_case, monitor_error=True)
    await worker._process(claim)
    assert writes == []
    assert "persist_enter" not in events
    assert events.count("finish:FAILED") == 1
    assert any(
        record.exc_info and "Monitor fixture thất bại" in str(record.exc_info[1])
        for record in caplog.records
    )
