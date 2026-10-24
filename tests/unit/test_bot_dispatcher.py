"""Fan-out evidence chung tới strategy riêng của từng bot, idempotent và point-in-time."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from tests.unit.test_strategy_hypotheses import _h01_snapshot, _h03_snapshot

from trading_bot.application import bot_dispatcher
from trading_bot.application.bot_dispatcher import (
    EVIDENCE_RECORDED_EVENT,
    BotDecision,
    BotDecisionConflict,
    BotDecisionContext,
    BotDecisionJob,
    BotDecisionStatus,
    BotEvidenceDispatcher,
    DecisionEvaluator,
    EvidenceRecordedEvent,
    EvidenceScopeError,
    SignalEvidenceFanOutConsumer,
)
from trading_bot.domain.market.signal_evidence import SharedSignalEvidence, SignalEvidenceError
from trading_bot.domain.strategy.contracts import DecisionSnapshot
from trading_bot.domain.strategy.registry import StrategyRegistry

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FOREIGN_WORKSPACE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
BOT_A = UUID("a1a1a1a1-0000-4000-8000-000000000001")
BOT_B = UUID("b2b2b2b2-0000-4000-8000-000000000002")
ACCOUNT_A = UUID("a1a1a1a1-1111-4111-8111-111111111111")
ACCOUNT_B = UUID("b2b2b2b2-2222-4222-8222-222222222222")
CONFIG_A = UUID("a1a1a1a1-3333-4333-8333-333333333333")
CONFIG_B = UUID("b2b2b2b2-4444-4444-8444-444444444444")
ROOT_VERSION = UUID("cccccccc-5555-4555-8555-555555555555")
DECISION_TIME = _h01_snapshot().decision_time


def _shift(snapshot: DecisionSnapshot, delta: timedelta) -> DecisionSnapshot:
    series = tuple(
        replace(
            item,
            bars=tuple(
                replace(
                    bar,
                    open_time=bar.open_time + delta,
                    close_time=bar.close_time + delta,
                    event_at=bar.event_at + delta,
                    received_at=bar.received_at + delta,
                    available_at=bar.available_at + delta,
                )
                for bar in item.bars
            ),
        )
        for item in snapshot.series
    )
    features = tuple(
        replace(item, available_at=item.available_at + delta) for item in snapshot.features
    )
    macro = snapshot.macro_evidence_available_at
    return replace(
        snapshot,
        decision_time=snapshot.decision_time + delta,
        series=series,
        features=features,
        macro_evidence_available_at=None if macro is None else macro + delta,
    )


def _h03_aligned() -> DecisionSnapshot:
    snapshot = _h03_snapshot()
    return _shift(snapshot, DECISION_TIME - snapshot.decision_time)


def _evidence(**overrides: object) -> SharedSignalEvidence:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "symbol": "BTCUSDT",
        "setup_type": "trend_breakout",
        "observed_at": DECISION_TIME - timedelta(minutes=1),
        "available_at": DECISION_TIME,
        "validity_end": DECISION_TIME + timedelta(hours=1),
        "feature_snapshot_id": "f" * 64,
        "source_refs": ("market_bar:BTCUSDT:1h",),
        "model_release_id": None,
    }
    values.update(overrides)
    return SharedSignalEvidence.create(**values)  # type: ignore[arg-type]


def _context(**overrides: object) -> BotDecisionContext:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "bot_id": BOT_A,
        "account_id": ACCOUNT_A,
        "bot_config_version_id": CONFIG_A,
        "bot_config_hash": "a" * 64,
        "root_settings_version_id": ROOT_VERSION,
        "root_settings_hash": "1" * 64,
        "strategy_release_id": "h01-v1",
        "symbols": ("BTCUSDT",),
        "effective_at": DECISION_TIME - timedelta(days=1),
    }
    values.update(overrides)
    return BotDecisionContext(**values)  # type: ignore[arg-type]


def _context_b(**overrides: object) -> BotDecisionContext:
    values: dict[str, object] = {
        "bot_id": BOT_B,
        "account_id": ACCOUNT_B,
        "bot_config_version_id": CONFIG_B,
        "bot_config_hash": "b" * 64,
        "strategy_release_id": "h03-v1",
    }
    values.update(overrides)
    return _context(**values)


class FixtureSnapshots:
    """Provider xác định: trả snapshot theo release của bot và đếm số lần gọi."""

    def __init__(self, overrides: dict[UUID, DecisionSnapshot] | None = None) -> None:
        self.calls: list[tuple[UUID, datetime]] = []
        self._overrides = overrides or {}

    async def snapshot_for(
        self,
        *,
        evidence: SharedSignalEvidence,
        context: BotDecisionContext,
        evaluated_at: datetime,
    ) -> DecisionSnapshot:
        self.calls.append((context.bot_id, evaluated_at))
        if context.bot_id in self._overrides:
            return self._overrides[context.bot_id]
        if context.strategy_release_id == "h01-v1":
            return _h01_snapshot()
        return _h03_aligned()


class InMemoryStore:
    """Store in-memory mô phỏng unique/idempotency của PostgreSQL cho unit test."""

    def __init__(self) -> None:
        self.evidence: dict[tuple[UUID, UUID], SharedSignalEvidence] = {}
        self.events: dict[UUID, EvidenceRecordedEvent] = {}
        self.event_status: dict[UUID, str] = {}
        self.receipts: list[tuple[UUID, str]] = []
        self.failures: list[tuple[UUID, str]] = []
        self.jobs: dict[tuple[UUID, UUID, UUID, UUID], BotDecisionJob] = {}
        self.decisions: dict[UUID, BotDecision] = {}

    async def record_evidence(
        self, evidence: SharedSignalEvidence, *, evaluated_at: datetime
    ) -> bool:
        key = (evidence.workspace_id, evidence.evidence_id)
        if key in self.evidence:
            assert self.evidence[key] == evidence
            return False
        self.evidence[key] = evidence
        event = EvidenceRecordedEvent.for_evidence(evidence, evaluated_at=evaluated_at)
        self.events[event.event_id] = event
        self.event_status[event.event_id] = "PENDING"
        return True

    async def load_evidence(
        self, *, workspace_id: UUID, evidence_id: UUID
    ) -> SharedSignalEvidence | None:
        return self.evidence.get((workspace_id, evidence_id))

    async def enqueue_job(self, job: BotDecisionJob) -> BotDecisionJob:
        key = (job.workspace_id, job.bot_id, job.evidence_id, job.bot_config_version_id)
        existing = self.jobs.get(key)
        if existing is None:
            self.jobs[key] = job
            return job
        if existing != job:
            raise BotDecisionConflict("job khác payload")
        return existing

    async def pending_jobs(
        self, *, workspace_id: UUID, account_id: UUID, limit: int
    ) -> tuple[BotDecisionJob, ...]:
        pending = [
            job
            for job in self.jobs.values()
            if job.workspace_id == workspace_id
            and job.account_id == account_id
            and job.job_id not in self.decisions
        ]
        return tuple(sorted(pending, key=lambda job: (job.evaluated_at, str(job.job_id)))[:limit])

    async def complete_job(self, job: BotDecisionJob, evaluate: DecisionEvaluator) -> BotDecision:
        if job.job_id in self.decisions:
            return self.decisions[job.job_id]
        evidence = self.evidence[(job.workspace_id, job.evidence_id)]
        decision = await evaluate(evidence, job)
        self.decisions[job.job_id] = decision
        return decision

    async def claim_evidence_events(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
        limit: int,
    ) -> tuple[EvidenceRecordedEvent, ...]:
        ready = [
            event
            for event_id, event in self.events.items()
            if event.workspace_id == workspace_id and self.event_status[event_id] == "PENDING"
        ]
        for event in ready:
            self.event_status[event.event_id] = "PROCESSING"
        return tuple(ready[:limit])

    async def acknowledge_event(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        worker_id: str,
        consumer_name: str,
        now: datetime,
    ) -> None:
        self.event_status[event_id] = "DELIVERED"
        self.receipts.append((event_id, consumer_name))

    async def record_event_failure(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        error: BaseException,
        max_attempts: int,
    ) -> str:
        self.event_status[event_id] = "PENDING"
        self.failures.append((event_id, type(error).__name__))
        return "PENDING"


def _dispatcher(
    store: InMemoryStore | None = None, snapshots: FixtureSnapshots | None = None
) -> tuple[BotEvidenceDispatcher, InMemoryStore, FixtureSnapshots]:
    store = store or InMemoryStore()
    snapshots = snapshots or FixtureSnapshots()
    dispatcher = BotEvidenceDispatcher(store, strategies=StrategyRegistry(), snapshots=snapshots)
    return dispatcher, store, snapshots


async def test_one_shared_evidence_feeds_two_bots_with_different_strategy_releases() -> None:
    dispatcher, store, _ = _dispatcher()
    evidence = _evidence()

    decisions = await dispatcher.dispatch_evidence(
        evidence, [_context(), _context_b()], DECISION_TIME
    )

    assert len(store.evidence) == 1
    assert len(store.events) == 1
    assert len(store.jobs) == 2
    first, second = decisions
    assert (first.bot_id, first.account_id) == (BOT_A, ACCOUNT_A)
    assert (second.bot_id, second.account_id) == (BOT_B, ACCOUNT_B)
    assert first.status is BotDecisionStatus.CANDIDATE
    assert second.status is BotDecisionStatus.CANDIDATE
    assert first.reason_code == "H01_LONG_ENTRY"
    assert second.reason_code == "H03_LONG_ENTRY"
    assert (first.strategy_release_id, second.strategy_release_id) == ("h01-v1", "h03-v1")
    assert first.strategy_release_hash != second.strategy_release_hash
    assert (first.bot_config_version_id, second.bot_config_version_id) == (CONFIG_A, CONFIG_B)
    assert first.candidate_id != second.candidate_id
    assert first.idempotency_key != second.idempotency_key
    assert first.decision_id != second.decision_id
    for decision in decisions:
        assert decision.evidence_id == evidence.evidence_id
        assert decision.evidence_hash == evidence.evidence_hash
        assert decision.feature_snapshot_id == evidence.feature_snapshot_id
        assert decision.root_settings_version_id == ROOT_VERSION
        assert decision.evaluated_at == DECISION_TIME


async def test_bot_specific_parameters_make_independent_candidate_and_abstain() -> None:
    dispatcher, _, _ = _dispatcher()
    default_h03 = _context(strategy_release_id="h03-v1")
    long_channel = _context_b(strategy_parameters=(("donchian_lookback", 60),))

    accepted, abstained = await dispatcher.dispatch_evidence(
        _evidence(), [default_h03, long_channel], DECISION_TIME
    )

    assert accepted.status is BotDecisionStatus.CANDIDATE
    assert abstained.status is BotDecisionStatus.ABSTAIN
    assert abstained.reason_code == "H03_INSUFFICIENT_BARS"
    assert abstained.candidate_id is None
    assert abstained.strategy_parameters == (("donchian_lookback", 60),)


async def test_retry_creates_no_duplicates_and_does_not_rerun_strategy() -> None:
    dispatcher, store, snapshots = _dispatcher()
    evidence = _evidence()
    contexts = [_context(), _context_b()]

    first = await dispatcher.dispatch_evidence(evidence, contexts, DECISION_TIME)
    calls_after_first = len(snapshots.calls)
    second = await dispatcher.dispatch_evidence(evidence, contexts, DECISION_TIME)

    assert second == first
    assert len(store.evidence) == 1
    assert len(store.events) == 1
    assert len(store.jobs) == 2
    assert len(store.decisions) == 2
    assert len(snapshots.calls) == calls_after_first == 2


async def test_replay_on_fresh_store_is_deterministic() -> None:
    contexts = [_context(), _context_b()]
    first = await _dispatcher()[0].dispatch_evidence(_evidence(), contexts, DECISION_TIME)
    second = await _dispatcher()[0].dispatch_evidence(_evidence(), contexts, DECISION_TIME)

    assert [item.decision_hash for item in first] == [item.decision_hash for item in second]
    assert [item.decision_id for item in first] == [item.decision_id for item in second]


async def test_foreign_workspace_evidence_is_rejected_before_any_write() -> None:
    dispatcher, store, _ = _dispatcher()
    foreign = _evidence(workspace_id=FOREIGN_WORKSPACE)

    with pytest.raises(EvidenceScopeError):
        await dispatcher.dispatch_evidence(foreign, [_context()], DECISION_TIME)

    assert store.evidence == {}
    assert store.jobs == {}


async def test_duplicate_bot_or_account_contexts_are_rejected() -> None:
    dispatcher, store, _ = _dispatcher()

    with pytest.raises(EvidenceScopeError):
        await dispatcher.dispatch_evidence(_evidence(), [_context(), _context()], DECISION_TIME)
    with pytest.raises(EvidenceScopeError):
        await dispatcher.dispatch_evidence(
            _evidence(), [_context(), _context_b(account_id=ACCOUNT_A)], DECISION_TIME
        )

    assert store.jobs == {}


@pytest.mark.parametrize(
    ("as_of", "code"),
    [
        (DECISION_TIME - timedelta(microseconds=1), "EVIDENCE_NOT_YET_AVAILABLE"),
        (DECISION_TIME + timedelta(hours=1), "EVIDENCE_EXPIRED"),
    ],
)
async def test_stale_or_future_evidence_is_rejected_before_any_write(
    as_of: datetime, code: str
) -> None:
    dispatcher, store, _ = _dispatcher()

    with pytest.raises(SignalEvidenceError) as rejected:
        await dispatcher.dispatch_evidence(_evidence(), [_context()], as_of)

    assert rejected.value.code == code
    assert store.evidence == {}
    assert store.jobs == {}


async def test_revised_later_evidence_is_rejected_for_historical_evaluation() -> None:
    dispatcher, store, _ = _dispatcher()
    original = _evidence()
    revision = _evidence(
        available_at=DECISION_TIME + timedelta(minutes=30),
        source_refs=("market_bar:BTCUSDT:1h", "market_bar:correction:1"),
    )

    decisions = await dispatcher.dispatch_evidence(original, [_context()], DECISION_TIME)
    with pytest.raises(SignalEvidenceError) as rejected:
        await dispatcher.dispatch_evidence(
            revision, [_context()], DECISION_TIME + timedelta(minutes=10)
        )

    assert rejected.value.code == "EVIDENCE_NOT_YET_AVAILABLE"
    assert decisions[0].evidence_id == original.evidence_id
    assert list(store.evidence) == [(WORKSPACE, original.evidence_id)]


@pytest.mark.parametrize("target", ["feature", "bar", "decision_time", "macro"])
async def test_future_market_inputs_are_vetoed_without_running_strategy(target: str) -> None:
    snapshot = _h01_snapshot()
    later = DECISION_TIME + timedelta(minutes=5)
    if target == "feature":
        features = (replace(snapshot.features[0], available_at=later), *snapshot.features[1:])
        snapshot = replace(snapshot, features=features)
    elif target == "bar":
        series = snapshot.series[0]
        bar = replace(series.bars[-1], available_at=later)
        snapshot = replace(
            snapshot,
            series=(replace(series, bars=(*series.bars[:-1], bar)), *snapshot.series[1:]),
        )
    elif target == "decision_time":
        snapshot = replace(snapshot, decision_time=later)
    else:
        snapshot = replace(snapshot, macro_evidence_available_at=later)
    dispatcher, _, _ = _dispatcher(snapshots=FixtureSnapshots({BOT_A: snapshot}))

    (decision,) = await dispatcher.dispatch_evidence(_evidence(), [_context()], DECISION_TIME)

    assert decision.status is BotDecisionStatus.VETO
    assert decision.reason_code == "FUTURE_INPUT_REJECTED"
    assert decision.candidate_id is None


async def test_config_not_yet_effective_is_vetoed() -> None:
    dispatcher, _, snapshots = _dispatcher()
    pending_config = _context(effective_at=DECISION_TIME + timedelta(seconds=1))

    (decision,) = await dispatcher.dispatch_evidence(_evidence(), [pending_config], DECISION_TIME)

    assert decision.status is BotDecisionStatus.VETO
    assert decision.reason_code == "BOT_CONFIG_NOT_EFFECTIVE"
    assert snapshots.calls == []


async def test_symbol_outside_bot_universe_abstains() -> None:
    dispatcher, _, snapshots = _dispatcher()
    eth_only = _context(symbols=("ETHUSDT",))

    (decision,) = await dispatcher.dispatch_evidence(_evidence(), [eth_only], DECISION_TIME)

    assert decision.status is BotDecisionStatus.ABSTAIN
    assert decision.reason_code == "SYMBOL_OUT_OF_BOT_UNIVERSE"
    assert snapshots.calls == []


async def test_unknown_strategy_release_is_vetoed() -> None:
    dispatcher, _, _ = _dispatcher()

    (decision,) = await dispatcher.dispatch_evidence(
        _evidence(), [_context(strategy_release_id="h99-v1")], DECISION_TIME
    )

    assert decision.status is BotDecisionStatus.VETO
    assert decision.reason_code == "STRATEGY_RELEASE_UNAVAILABLE"
    assert decision.strategy_release_hash is None


async def test_snapshot_for_another_symbol_is_vetoed() -> None:
    eth = replace(
        _h01_snapshot(),
        symbol="ETHUSDT",
        series=(),
        features=(),
    )
    dispatcher, _, _ = _dispatcher(snapshots=FixtureSnapshots({BOT_A: eth}))

    (decision,) = await dispatcher.dispatch_evidence(_evidence(), [_context()], DECISION_TIME)

    assert decision.status is BotDecisionStatus.VETO
    assert decision.reason_code == "DECISION_SNAPSHOT_SCOPE_MISMATCH"


async def test_same_job_key_with_different_evaluation_time_is_conflict() -> None:
    dispatcher, _, _ = _dispatcher()
    evidence = _evidence()
    await dispatcher.dispatch_evidence(evidence, [_context()], DECISION_TIME)

    with pytest.raises(BotDecisionConflict):
        await dispatcher.dispatch_evidence(
            evidence, [_context()], DECISION_TIME + timedelta(minutes=1)
        )


async def test_new_bot_config_version_gets_its_own_job_and_decision() -> None:
    dispatcher, store, _ = _dispatcher()
    evidence = _evidence()
    await dispatcher.dispatch_evidence(evidence, [_context()], DECISION_TIME)

    (republished,) = await dispatcher.dispatch_evidence(
        evidence,
        [_context(bot_config_version_id=UUID("a1a1a1a1-9999-4999-8999-999999999999"))],
        DECISION_TIME,
    )

    assert len(store.jobs) == 2
    assert republished.bot_config_version_id != CONFIG_A


def test_dispatcher_cannot_create_orders_or_hold_execution_dependencies() -> None:
    forbidden = {"PaperExecutionGateway", "TradePlanner", "RiskEngine", "PaperDecisionCycle"}
    decision_fields = {field.name for field in dataclasses.fields(BotDecision)}

    assert not forbidden & set(vars(bot_dispatcher))
    assert not {"quantity", "notional", "order_id", "intent_id", "reservation_id"} & (
        decision_fields
    )


def test_context_rejects_scope_or_selection_errors() -> None:
    with pytest.raises(ValueError, match="bot_config_hash"):
        _context(bot_config_hash="short")
    with pytest.raises(ValueError, match="symbols"):
        _context(symbols=())
    with pytest.raises(ValueError, match="múi giờ"):
        _context(effective_at=datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="chưa hỗ trợ tham số"):
        _context(strategy_parameters=(("donchian_lookback", 60),))
    with pytest.raises(ValueError, match="donchian_lookback"):
        _context(strategy_release_id="h03-v1", strategy_parameters=(("donchian_lookback", 5),))


def test_job_and_decision_payloads_round_trip_with_hash_verification() -> None:
    context = _context_b(strategy_parameters=(("donchian_lookback", 55),))
    job = BotDecisionJob.create(evidence=_evidence(), context=context, evaluated_at=DECISION_TIME)

    assert BotDecisionJob.from_payload(job.canonical_payload()) == job
    assert BotDecisionContext.from_payload(context.canonical_payload()) == context
    with pytest.raises(ValueError, match="job_id"):
        replace(job, job_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"))
    with pytest.raises(ValueError, match="context"):
        replace(job, bot_id=BOT_A)


async def test_decision_round_trip_detects_tampering() -> None:
    dispatcher, _, _ = _dispatcher()
    (decision,) = await dispatcher.dispatch_evidence(_evidence(), [_context()], DECISION_TIME)

    restored = BotDecision.from_payload(decision.canonical_payload(), decision.decision_hash)

    assert restored == decision
    with pytest.raises(ValueError, match="decision_hash"):
        BotDecision.from_payload(decision.canonical_payload(), "0" * 64)
    with pytest.raises(ValueError, match="decision_hash"):
        replace(decision, reason_code="TAMPERED")


async def test_outbox_consumer_fans_out_recorded_evidence_then_acknowledges() -> None:
    dispatcher, store, _ = _dispatcher()
    evidence = _evidence()
    contexts = (_context(), _context_b())

    class Provider:
        def __init__(self) -> None:
            self.as_of: list[datetime] = []

        async def contexts_for(
            self, *, workspace_id: UUID, as_of: datetime
        ) -> tuple[BotDecisionContext, ...]:
            assert workspace_id == WORKSPACE
            self.as_of.append(as_of)
            return contexts

    provider = Provider()
    consumer = SignalEvidenceFanOutConsumer(store, dispatcher, provider, worker_id="worker-1")
    assert await dispatcher.record_evidence(evidence, evaluated_at=DECISION_TIME) is True
    assert await dispatcher.record_evidence(evidence, evaluated_at=DECISION_TIME) is False

    jobs = await consumer.consume_once(workspace_id=WORKSPACE, now=datetime.now(UTC))
    decisions_a = await dispatcher.process_pending(workspace_id=WORKSPACE, account_id=ACCOUNT_A)
    decisions_b = await dispatcher.process_pending(workspace_id=WORKSPACE, account_id=ACCOUNT_B)

    assert provider.as_of == [DECISION_TIME]
    assert {job.bot_id for job in jobs} == {BOT_A, BOT_B}
    assert [event.event_id for event in store.events.values()] == [
        receipt for receipt, _ in store.receipts
    ]
    assert store.receipts[0][1] == SignalEvidenceFanOutConsumer.consumer_name
    assert decisions_a[0].bot_id == BOT_A
    assert decisions_b[0].bot_id == BOT_B
    assert await consumer.consume_once(workspace_id=WORKSPACE, now=datetime.now(UTC)) == ()
    assert await dispatcher.process_pending(workspace_id=WORKSPACE, account_id=ACCOUNT_A) == ()
    event = next(iter(store.events.values()))
    assert event.evidence_hash == evidence.evidence_hash
    assert EVIDENCE_RECORDED_EVENT == "signal.evidence.recorded.v1"


async def test_outbox_consumer_records_failure_when_evidence_hash_mismatches() -> None:
    dispatcher, store, _ = _dispatcher()
    evidence = _evidence()
    await dispatcher.record_evidence(evidence, evaluated_at=DECISION_TIME)
    event = next(iter(store.events.values()))
    store.events[event.event_id] = replace(event, evidence_hash="0" * 64)

    class Provider:
        async def contexts_for(
            self, *, workspace_id: UUID, as_of: datetime
        ) -> tuple[BotDecisionContext, ...]:
            return (_context(),)

    consumer = SignalEvidenceFanOutConsumer(store, dispatcher, Provider(), worker_id="worker-1")

    assert await consumer.consume_once(workspace_id=WORKSPACE, now=datetime.now(UTC)) == ()
    assert store.failures == [(event.event_id, "EvidenceScopeError")]
    assert store.jobs == {}
    assert store.receipts == []
