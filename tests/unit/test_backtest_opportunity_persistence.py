"""Regression cho append opportunity/outcome nguyên tử và contract schema v1/v2."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.unit.test_backtest_decision_opportunities import _pinned_setup
from tests.unit.test_backtest_outcomes_repository import (
    REPORT_ID,
    RUN_ID,
    TRIAL_RECORD_ID,
    WORKSPACE_ID,
    FakeSession,
)

from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.backtests import (
    BacktestAppendResult,
    BacktestRepository,
    BacktestRepositoryConflict,
)
from trading_bot.application.runtime_replay_evaluator import run_runtime_paper_replay


def _runtime_run():
    _, observations, _, manifest, factory = _pinned_setup(cross=True)
    return run_runtime_paper_replay(
        observations, manifest=manifest, runtime_evaluator_factory=factory
    ).runs[0]


def _stub(monkeypatch, duplicate=False):
    async def append(self, **kwargs):
        return BacktestAppendResult(
            RUN_ID, TRIAL_RECORD_ID, REPORT_ID, duplicate, run_duplicate=duplicate
        )

    monkeypatch.setattr(BacktestRepository, "append_result", append)


class AtomicSession(FakeSession):
    def __init__(self, results, fail_outcome=False):
        super().__init__(results)
        self.fail_outcome = fail_outcome
        self.flushed_names = []

    @asynccontextmanager
    async def begin_nested(self):
        count = len(self.added)
        try:
            yield
        except Exception:
            del self.added[count:]
            raise

    async def flush(self):
        names = [row.__tablename__ for row in self.added]
        self.flushed_names.append(tuple(names))
        if self.fail_outcome and "backtest_trade_outcome" in names:
            raise RuntimeError("Lỗi outcome giả lập để kiểm savepoint của toàn lượt append.")


@pytest.mark.asyncio
async def test_opportunities_flush_before_linked_outcomes_and_retry_is_idempotent(monkeypatch):
    run = _runtime_run()
    _stub(monkeypatch)
    session = AtomicSession(
        [
            SimpleNamespace(trial_id=run.sensitivity_run.trial_id),
            None,
            *[None for _ in run.opportunities],
            None,
        ]
    )
    first = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=WORKSPACE_ID,
        sensitivity_run=run.sensitivity_run,
        trade_outcomes=run.trade_outcomes,
        opportunities=run.opportunities,
    )
    scenario, *rows = session.added
    opportunities = [row for row in rows if row.__tablename__ == "backtest_decision_opportunity"]
    outcome = rows[-1]
    assert len(opportunities) == len(run.opportunities)
    assert all(
        row.canonical_opportunity == item.canonical_json()
        for row, item in zip(opportunities, run.opportunities, strict=True)
    )
    assert outcome.entry_opportunity_event_id == run.trade_outcomes[0].entry_opportunity_event_id
    entry_opportunity = next(
        item for item in run.opportunities if item.event_id == outcome.entry_opportunity_event_id
    )
    assert outcome.entry_candidate_id == entry_opportunity.candidate_id
    assert outcome.entry_vector_hash == entry_opportunity.vector_hash
    assert outcome.entry_features_hash == entry_opportunity.feature_values_hash
    assert outcome.outcome["entry_vector_hash"] == run.opportunities[0].vector_hash
    assert session.flushed_names[-2][-1] == "backtest_decision_opportunity"
    _stub(monkeypatch, duplicate=True)
    retry_session = AtomicSession(
        [
            SimpleNamespace(trial_id=run.sensitivity_run.trial_id),
            scenario,
            *[row.canonical_opportunity for row in opportunities],
            outcome,
        ]
    )
    retry = await BacktestOutcomeRepository(retry_session).append_sensitivity_run(
        workspace_id=WORKSPACE_ID,
        sensitivity_run=run.sensitivity_run,
        trade_outcomes=run.trade_outcomes,
        opportunities=run.opportunities,
    )
    assert retry.duplicate
    assert retry.outcomes_hash == first.outcomes_hash
    assert retry_session.added == []


@pytest.mark.asyncio
async def test_outcome_failure_rolls_back_scenario_and_opportunities_in_same_savepoint(monkeypatch):
    run = _runtime_run()
    _stub(monkeypatch)
    session = AtomicSession(
        [
            SimpleNamespace(trial_id=run.sensitivity_run.trial_id),
            None,
            *[None for _ in run.opportunities],
            None,
        ],
        fail_outcome=True,
    )
    with pytest.raises(RuntimeError, match="Lỗi outcome"):
        await BacktestOutcomeRepository(session).append_sensitivity_run(
            workspace_id=WORKSPACE_ID,
            sensitivity_run=run.sensitivity_run,
            trade_outcomes=run.trade_outcomes,
            opportunities=run.opportunities,
        )
    assert session.added == []


@pytest.mark.asyncio
async def test_same_event_with_changed_canonical_payload_conflicts(monkeypatch):
    run = _runtime_run()
    _stub(monkeypatch, duplicate=True)
    conflicting = replace(run.opportunities[0], reason_code="CHANGED_REASON")
    session = AtomicSession(
        [SimpleNamespace(trial_id=run.sensitivity_run.trial_id), None, conflicting.canonical_json()]
    )
    with pytest.raises(BacktestRepositoryConflict, match="opportunity|Opportunity"):
        await BacktestOutcomeRepository(session).append_sensitivity_run(
            workspace_id=WORKSPACE_ID,
            sensitivity_run=run.sensitivity_run,
            trade_outcomes=run.trade_outcomes,
            opportunities=run.opportunities,
        )
    assert session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_candidate_id", "0" * 64),
        ("entry_opportunity_event_id", "wrong-event"),
        ("entry_decision_snapshot_id", "wrong-snapshot"),
        ("entry_symbol", "ETHUSDT"),
    ],
)
async def test_outcome_link_must_match_exact_candidate_entry_before_any_write(
    monkeypatch, field, value
):
    from trading_bot.domain.learning.evaluation import calculate_entry_features_hash

    run = _runtime_run()
    _stub(monkeypatch)
    trade = run.trade_outcomes[0]
    changes = {field: value}
    if field == "entry_decision_snapshot_id":
        changes["entry_features_hash"] = calculate_entry_features_hash(
            value, trade.entry_decision_at, trade.entry_feature_schema_hash, trade.entry_features
        )
    changed = replace(trade, **changes)
    session = AtomicSession([])
    with pytest.raises(
        (ValueError, BacktestRepositoryConflict), match="entry|Entry|opportunity|Opportunity"
    ):
        await BacktestOutcomeRepository(session).append_sensitivity_run(
            workspace_id=WORKSPACE_ID,
            sensitivity_run=run.sensitivity_run,
            trade_outcomes=(changed,),
            opportunities=run.opportunities,
        )
    assert session.added == []


def test_entry_features_must_match_hash_committed_to_exact_opportunity():
    from trading_bot.adapters.database.repositories.backtest_outcomes import (
        _validate_opportunity_links,
    )
    from trading_bot.domain.learning.evaluation import calculate_entry_features_hash

    run = _runtime_run()
    trade = run.trade_outcomes[0]
    entry_opportunity = next(
        item for item in run.opportunities if item.event_id == trade.entry_opportunity_event_id
    )
    assert entry_opportunity.feature_values_hash == trade.entry_features_hash
    changed_features = tuple((key, value + 1.0) for key, value in trade.entry_features)
    forged = replace(
        trade,
        entry_features=changed_features,
        entry_features_hash=calculate_entry_features_hash(
            trade.entry_decision_snapshot_id,
            trade.entry_decision_at,
            trade.entry_feature_schema_hash,
            changed_features,
        ),
    )

    with pytest.raises(ValueError, match="feature|vector|snapshot"):
        _validate_opportunity_links(
            WORKSPACE_ID,
            run.sensitivity_run,
            (forged,),
            run.opportunities,
        )


def test_opportunity_model_has_metadata_only_and_composite_scope_constraints():
    from trading_bot.adapters.database.models import Base
    from trading_bot.adapters.database.models.backtest_opportunity_entities import (
        BacktestDecisionOpportunityRecord,
    )

    table = BacktestDecisionOpportunityRecord.__table__
    assert table.name in Base.metadata.tables
    assert {
        "workspace_id",
        "run_id",
        "trial_record_id",
        "scenario_id",
        "query_version",
        "event_id",
        "canonical_opportunity",
        "content_hash",
    } <= set(table.c.keys())
    assert not {"features", "values", "bars", "label", "entry_features"} & set(table.c.keys())
    assert any(len(fk.column_keys) > 1 for fk in table.foreign_key_constraints)


def test_proof_v2_columns_are_nullable_for_immutable_v1_rows():
    from trading_bot.adapters.database.models.candidate_universe_entities import (
        CandidateUniverseProofRecord,
    )

    table = CandidateUniverseProofRecord.__table__
    for name in (
        "opportunity_count",
        "no_candidate_count",
        "not_run_count",
        "input_excluded_count",
        "unlabeled_candidate_count",
        "outcome_link_count",
        "source_scope_hash",
        "completeness_verified",
    ):
        assert table.c[name].nullable is True
    checks = " ".join(str(item.sqltext) for item in table.constraints if hasattr(item, "sqltext"))
    assert "base-closed-trade-universe-v1" in checks
    assert "base-decision-opportunity-universe-v1" in checks
    assert "opportunity_count = candidate_count + no_candidate_count + not_run_count" in checks
    assert "IS NULL" in checks
    assert "IS NOT NULL" in checks


@pytest.mark.parametrize(
    "pairs",
    [
        (("4h", "a" * 64), ("1h", "b" * 64)),
        (("1h", "a" * 64), ("1h", "b" * 64)),
        (("1h", "not-a-digest"), ("4h", "b" * 64)),
        (("1h", "a" * 64),),
        ({"timeframe": "1h", "values": [1.0]},),
    ],
)
def test_source_digest_map_requires_exact_ordered_unique_metadata_keys(pairs):
    from tests.unit.test_backtest_decision_opportunities import _opportunity

    with pytest.raises(ValueError, match="digest|timeframe|tuple|nguồn|hash"):
        replace(_opportunity(), source_event_digests=pairs)


def test_source_digest_map_hash_is_recomputed_and_schema_is_versioned():
    from tests.unit.test_backtest_decision_opportunities import _opportunity

    from trading_bot.application.backtest_decision_opportunities import metadata_hash

    opportunity = _opportunity()
    assert opportunity.schema_version == "crypidian-replay-decision-opportunity-v3"
    assert opportunity.source_refs_digest == metadata_hash(
        [
            {"timeframe": timeframe, "source_event_digest": digest}
            for timeframe, digest in opportunity.source_event_digests
        ]
    )
    with pytest.raises(ValueError, match="digest|hash|nguồn"):
        replace(opportunity, source_refs_digest="0" * 64)
