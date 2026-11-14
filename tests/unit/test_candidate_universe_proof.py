"""Kiểm chứng tập trade BASE đầy đủ, không suy rộng thành quyền TRAINING."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from tests.fixtures.runtime_replay import build_h03_runtime_sensitivity

from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.application.backtest_sensitivity import SensitivityScenario

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class MemorySession:
    def __init__(self):
        self.rows = []
        self.info = {"workspace_id": WORKSPACE}
        self.isolation = "read committed"
        self.tenant = str(WORKSPACE)
        self.active = True
        from datetime import UTC, datetime

        self.clock = datetime(2026, 11, 3, tzinfo=UTC)

    def in_transaction(self):
        return self.active

    async def execute(self, statement, parameters=None):
        sql = str(statement)
        value = self.isolation if "transaction_isolation" in sql else self.tenant
        if "wp06_lock_candidate_trial" in sql:
            value = True
        if "clock_timestamp" in sql:
            value = self.clock
        return SimpleNamespace(scalar_one=lambda: value, scalar_one_or_none=lambda: value)

    def selected(self, statement):
        model = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if type(row) is model]
        for parameter, value in statement.compile().params.items():
            field = parameter.rsplit("_", 1)[0]
            rows = [row for row in rows if not hasattr(row, field) or getattr(row, field) == value]
        if model.__name__ == "BacktestTradeOutcomeRecord":
            rows.sort(key=lambda row: row.ordinal)
        return rows

    async def scalar(self, statement):
        rows = self.selected(statement)
        return rows[0] if rows else None

    async def scalars(self, statement):
        rows = self.selected(statement)
        return SimpleNamespace(all=lambda: rows)

    def add(self, row):
        if row.id is None:
            row.id = uuid4()
        if hasattr(row, "created_at") and row.created_at is None:
            row.created_at = self.clock
        self.rows.append(row)

    async def flush(self):
        pass

    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.fixture
async def persisted(monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
        ReplayTrainingOperation,
        ReplayTrainingRightsReceipt,
    )
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission

    async def allowed(self, *, workspace_id, bundle_hash, operation):
        assert operation is ReplayTrainingOperation.TRAINING
        now = datetime(2026, 11, 3, tzinfo=UTC)
        return (
            ReplayTrainingRightsReceipt(
                workspace_id=workspace_id,
                bundle_hash=bundle_hash,
                child_dataset_hash="2" * 64,
                interval="1h",
                symbol="BTCUSDT",
                bars_content_hash="3" * 64,
                product="FUTURES",
                venue="BINANCE",
                contract="USD_M_PERPETUAL",
                data_start=now - timedelta(days=2),
                data_end=now - timedelta(days=1),
                policy_id="fixture:training",
                policy_fingerprint="4" * 64,
                bundle_policy_fingerprint="5" * 64,
                current_policy_fingerprint="4" * 64,
                terms_url="https://example.invalid/fixture-training",
                verified_at=now - timedelta(days=1),
                checked_at=now,
                valid_until=now + timedelta(minutes=5),
                license_evidence_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                evidence_digest="6" * 64,
                license_scope_hash="7" * 64,
                scope_hash="8" * 64,
                operation=operation,
            ),
        )

    monkeypatch.setattr(DatabaseReplayTrainingRightsGate, "authorize", allowed)
    session = MemorySession()
    learning_job_id = uuid4()
    request = BacktestReplayJobSubmission(
        bundle_hash="a" * 64,
        strategy_release_id="h03-v1",
        execution_interval="1h",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0"),
        seed=19,
        learning_intent="TRAINING_BASE",
    )
    replay = build_h03_runtime_sensitivity(
        WORKSPACE,
        (SensitivityScenario(scenario_id="base", kind="BASE"),),
        learning_intent="TRAINING_BASE",
        learning_job_id=str(learning_job_id),
    ).runs[0]
    receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=WORKSPACE,
        sensitivity_run=replay.sensitivity_run,
        trade_outcomes=replay.trade_outcomes,
    )
    payload = request.to_payload()
    session.add(
        BacktestReplayJobRecord(
            id=learning_job_id,
            workspace_id=WORKSPACE,
            requested_by=uuid4(),
            bundle_hash=request.bundle_hash,
            strategy_release_id=request.strategy_release_id,
            execution_interval=request.execution_interval,
            request_payload=payload,
            canonical_request=json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            status="SUCCEEDED",
            stage="completed",
            progress=100,
            cancel_requested=False,
            attempt_count=1,
            lease_generation=1,
            result_run_ids=[str(receipt.run_id)],
            created_at=session.clock - timedelta(seconds=1),
            updated_at=session.clock,
        )
    )
    return session, receipt


async def build(persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofRepository,
    )

    session, receipt = persisted
    return await CandidateUniverseProofRepository(session).build_and_append(
        workspace_id=WORKSPACE, trial_record_id=receipt.trial_record_id
    )


def _add_training_job(session, receipt, manifest, job_id):
    import json
    from datetime import timedelta
    from decimal import Decimal

    from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission

    request = BacktestReplayJobSubmission(
        bundle_hash=manifest.dataset_hash,
        strategy_release_id=manifest.strategy_release_id,
        execution_interval="1h",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0"),
        seed=manifest.seed,
        learning_intent="TRAINING_BASE",
    )
    payload = request.to_payload()
    session.add(
        BacktestReplayJobRecord(
            id=job_id,
            workspace_id=WORKSPACE,
            requested_by=uuid4(),
            bundle_hash=request.bundle_hash,
            strategy_release_id=request.strategy_release_id,
            execution_interval=request.execution_interval,
            request_payload=payload,
            canonical_request=json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            status="SUCCEEDED",
            stage="completed",
            progress=100,
            cancel_requested=False,
            attempt_count=1,
            lease_generation=1,
            result_run_ids=[str(receipt.run_id)],
            created_at=session.clock - timedelta(seconds=1),
            updated_at=session.clock,
        )
    )


@pytest.mark.asyncio
async def test_database_scan_creates_immutable_metadata_only_proof(persisted):
    result = await build(persisted)
    proof = result.proof
    assert proof.candidate_count == proof.eligible_count == 1
    assert proof.exclusion_count == 0
    assert proof.training_authorized is False
    assert proof.eligibility_scope == "entry_provenance_only"
    assert proof.coverage_scope == "persisted_closed_trade_outcomes"
    assert proof.trial_record_id == persisted[1].trial_record_id
    payload = json.loads(proof.canonical_json)
    assert not {"samples", "features", "labels", "entry_features"} & payload.keys()
    assert len(proof.membership_digest) == len(proof.content_hash) == 64
    with pytest.raises((AttributeError, TypeError)):
        proof.candidate_count = 99
    retry = await build(persisted)
    assert retry.duplicate is True
    assert retry.record_id == result.record_id
    assert retry.proof == proof


@pytest.mark.asyncio
async def test_candidate_proof_rejects_replay_without_predeclared_training_intent(persisted):
    import json

    from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session, _ = persisted
    job = next(row for row in session.rows if isinstance(row, BacktestReplayJobRecord))
    job.request_payload["learning_intent"] = "RESEARCH"
    job.canonical_request = json.dumps(
        job.request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(CandidateUniverseProofError, match="learning intent"):
        await build(persisted)


@pytest.mark.asyncio
async def test_legacy_trade_is_explicit_exclusion_without_training_authority(persisted):
    from trading_bot.adapters.database.repositories.backtest_outcomes import _canonical, _sha256

    row = next(
        row for row in persisted[0].rows if type(row).__name__ == "BacktestTradeOutcomeRecord"
    )
    for key in list(row.outcome):
        if key.startswith("entry_"):
            del row.outcome[key]
            setattr(row, key, None)
    row.canonical_json = _canonical(row.outcome)
    row.outcome_hash = _sha256(row.canonical_json)
    result = await build(persisted)
    assert result.proof.candidate_count == result.proof.exclusion_count == 1
    assert result.proof.eligible_count == 0
    assert result.proof.exclusion_reason_counts == (("entry_provenance_missing", 1),)
    assert result.proof.training_authorized is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    ["BacktestRunRecord", "BacktestTrialRecord", "BacktestReportRecord", "BacktestScenarioRecord"],
)
async def test_missing_authoritative_lineage_fails_closed(persisted, model):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    persisted[0].rows = [row for row in persisted[0].rows if type(row).__name__ != model]
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        ("BacktestTrialRecord", "status", "FAILED"),
        ("BacktestTrialRecord", "trade_count", 2),
        ("BacktestTrialRecord", "parameter_family_hash", "0" * 64),
        ("BacktestRunRecord", "manifest_hash", "0" * 64),
        ("BacktestRunRecord", "workspace_id", UUID(int=9)),
        ("BacktestReportRecord", "trade_count", 2),
        ("BacktestReportRecord", "report_hash", "0" * 64),
        ("BacktestReportRecord", "run_id", UUID(int=9)),
        ("BacktestReportRecord", "canonical_json", "[]"),
        ("BacktestReportRecord", "coverage", 0.5),
        ("BacktestScenarioRecord", "kind", "PARAMETER"),
        ("BacktestScenarioRecord", "scenario_id", "forged"),
        ("BacktestScenarioRecord", "scenario_hash", "0" * 64),
        ("BacktestScenarioRecord", "manifest_hash", "0" * 64),
        ("BacktestTradeOutcomeRecord", "run_id", UUID(int=9)),
        ("BacktestTradeOutcomeRecord", "manifest_hash", "0" * 64),
        ("BacktestTradeOutcomeRecord", "ordinal", 1),
        ("BacktestTradeOutcomeRecord", "outcome_hash", "0" * 64),
        ("BacktestTradeOutcomeRecord", "entry_symbol", "ETHUSDT"),
        ("BacktestTradeOutcomeRecord", "entry_features_hash", "0" * 64),
    ],
)
async def test_corrupted_lineage_counts_hash_or_provenance_fails_closed(
    persisted, model, field, value
):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    row = next(row for row in persisted[0].rows if type(row).__name__ == model)
    setattr(row, field, value)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [False, True])
async def test_missing_or_extra_outcomes_fail_counts(persisted, extra):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    row = next(
        row for row in persisted[0].rows if type(row).__name__ == "BacktestTradeOutcomeRecord"
    )
    if extra:
        persisted[0].rows.append(row)
    else:
        persisted[0].rows.remove(row)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("active", False), ("isolation", "repeatable read"), ("tenant", str(UUID(int=8)))],
)
async def test_requires_transaction_snapshot_and_bound_tenant(persisted, field, value):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    setattr(persisted[0], field, value)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
async def test_duplicate_proof_with_changed_canonical_is_rejected(persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    await build(persisted)
    row = next(
        row for row in persisted[0].rows if type(row).__name__ == "CandidateUniverseProofRecord"
    )
    row.membership_digest = "0" * 64
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
async def test_denied_training_rights_stops_before_report_or_outcome_reads(persisted, monkeypatch):
    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
    )
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    async def denied(self, **kwargs):
        raise PermissionError("Policy chỉ cho BACKTEST_RESEARCH.")

    monkeypatch.setattr(DatabaseReplayTrainingRightsGate, "authorize", denied)
    original = persisted[0].scalars

    async def no_payload(statement):
        model = statement.column_descriptions[0]["entity"]
        assert model.__name__ not in ("BacktestReportRecord", "BacktestTradeOutcomeRecord")
        return await original(statement)

    monkeypatch.setattr(persisted[0], "scalars", no_payload)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
async def test_membership_digest_binds_record_uuid_hash_ordinal_and_reason(persisted):
    from trading_bot.application.candidate_universe_proof import canonical, digest

    row = next(
        row for row in persisted[0].rows if type(row).__name__ == "BacktestTradeOutcomeRecord"
    )
    result = await build(persisted)
    expected = [
        {
            "ordinal": 0,
            "outcome_record_id": str(row.id),
            "outcome_hash": row.outcome_hash,
            "eligible": True,
            "exclusion_reason": None,
        }
    ]
    assert result.proof.membership_digest == digest(canonical(expected))
    row.id = uuid4()
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["[]", "{}", "broken"])
async def test_corrupted_persisted_rights_receipt_is_rejected(persisted, value):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    await build(persisted)
    row = next(
        row for row in persisted[0].rows if type(row).__name__ == "CandidateTrainingRightsRecord"
    )
    row.canonical_evidence = value
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["schema", "symbol", "decision_after_entry"])
async def test_valid_canonical_entry_must_match_replay_scope(persisted, failure):
    from dataclasses import replace
    from datetime import timedelta

    from trading_bot.adapters.database.repositories.backtest_outcomes import (
        _canonical,
        _sha256,
        _trade_from_payload,
        _trade_payload,
    )
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )
    from trading_bot.domain.learning.evaluation import calculate_entry_features_hash

    row = next(
        row for row in persisted[0].rows if type(row).__name__ == "BacktestTradeOutcomeRecord"
    )
    trade = _trade_from_payload(row.outcome)
    schema = "0" * 64 if failure == "schema" else trade.entry_feature_schema_hash
    decision = (
        trade.opened_at + timedelta(seconds=1)
        if failure == "decision_after_entry"
        else trade.entry_decision_at
    )
    symbol = "ETHUSDT" if failure == "symbol" else trade.entry_symbol
    trade = replace(
        trade,
        entry_feature_schema_hash=schema,
        entry_decision_at=decision,
        entry_symbol=symbol,
        entry_features_hash=calculate_entry_features_hash(
            trade.entry_decision_snapshot_id, decision, schema, trade.entry_features
        ),
    )
    row.outcome = _trade_payload(trade)
    row.canonical_json = _canonical(row.outcome)
    row.outcome_hash = _sha256(row.canonical_json)
    for field in (
        "entry_feature_schema_hash",
        "entry_decision_at",
        "entry_symbol",
        "entry_features_hash",
    ):
        setattr(row, field, getattr(trade, field))
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("training_authorized", True),
        ("coverage_scope", "all_decisions"),
        ("eligible_count", 10),
        ("membership_digest", "broken"),
    ],
)
async def test_proof_value_object_rejects_expanded_claim_or_invalid_counts(persisted, field, value):
    from dataclasses import replace

    proof = (await build(persisted)).proof
    with pytest.raises(ValueError, match="Proof|Counts|Digest"):
        replace(proof, **{field: value})


@pytest.mark.asyncio
async def test_zero_trade_base_trial_has_empty_membership_not_synthetic_samples(persisted):
    from tests.fixtures.runtime_replay import build_h01_dataset_runtime_replay

    session, _ = persisted
    session.rows.clear()
    learning_job_id = uuid4()
    _, replay = build_h01_dataset_runtime_replay(
        WORKSPACE,
        scenarios=(SensitivityScenario("base", "BASE"),),
        learning_intent="TRAINING_BASE",
        learning_job_id=str(learning_job_id),
    )
    run = replay.runs[0]
    assert not run.trade_outcomes
    receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=WORKSPACE, sensitivity_run=run.sensitivity_run, trade_outcomes=()
    )
    _add_training_job(session, receipt, run.sensitivity_run.manifest, learning_job_id)
    result = await build((session, receipt))
    assert (
        result.proof.candidate_count
        == result.proof.eligible_count
        == result.proof.exclusion_count
        == 0
    )


@pytest.mark.asyncio
async def test_training_lease_expiring_during_scan_prevents_proof_append(persisted, monkeypatch):
    from datetime import timedelta

    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    original = BacktestOutcomeRepository.load_trade_outcome_rows

    async def slow_scan(self, **kwargs):
        rows = await original(self, **kwargs)
        persisted[0].clock += timedelta(minutes=6)
        return rows

    monkeypatch.setattr(BacktestOutcomeRepository, "load_trade_outcome_rows", slow_scan)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)
    assert not any(
        type(row).__name__ == "CandidateUniverseProofRecord" for row in persisted[0].rows
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["BacktestReportRecord", "BacktestScenarioRecord"])
async def test_duplicate_report_or_scenario_is_not_authoritative(persisted, model):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    row = next(row for row in persisted[0].rows if type(row).__name__ == model)
    persisted[0].rows.append(row)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
async def test_canonical_order_and_membership_cover_two_closed_trades(persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session, _ = persisted
    session.rows.clear()
    learning_job_id = uuid4()
    replay = build_h03_runtime_sensitivity(
        WORKSPACE,
        (SensitivityScenario("base", "BASE"),),
        include_second_trade=True,
        learning_intent="TRAINING_BASE",
        learning_job_id=str(learning_job_id),
    ).runs[0]
    assert len(replay.trade_outcomes) == 2
    receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=WORKSPACE,
        sensitivity_run=replay.sensitivity_run,
        trade_outcomes=replay.trade_outcomes,
    )
    _add_training_job(session, receipt, replay.sensitivity_run.manifest, learning_job_id)
    result = await build((session, receipt))
    assert result.proof.candidate_count == result.proof.eligible_count == 2
    outcomes = [row for row in session.rows if type(row).__name__ == "BacktestTradeOutcomeRecord"]
    outcomes[0].ordinal, outcomes[1].ordinal = 1, 0
    with pytest.raises(CandidateUniverseProofError):
        await build((session, receipt))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "field"),
    [
        ("BacktestTrialRecord", "workspace_id"),
        ("BacktestReportRecord", "workspace_id"),
        ("BacktestScenarioRecord", "workspace_id"),
        ("BacktestTradeOutcomeRecord", "workspace_id"),
        ("BacktestScenarioRecord", "run_id"),
        ("BacktestScenarioRecord", "trial_record_id"),
        ("BacktestReportRecord", "trial_record_id"),
    ],
)
async def test_every_denormalized_scope_link_is_rechecked(persisted, model, field):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    row = next(row for row in persisted[0].rows if type(row).__name__ == model)
    setattr(row, field, UUID(int=7))
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
async def test_canonical_trial_boolean_cannot_impersonate_integer_count(persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )
    from trading_bot.application.candidate_universe_proof import canonical, digest

    row = next(row for row in persisted[0].rows if type(row).__name__ == "BacktestTrialRecord")
    payload = json.loads(row.canonical_trial)
    payload["trade_count"] = True
    row.canonical_trial = canonical(payload)
    row.content_hash = digest(row.canonical_trial)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
async def test_canonical_report_boolean_count_is_rejected_even_with_matching_hash(persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )
    from trading_bot.application.candidate_universe_proof import canonical, digest

    row = next(row for row in persisted[0].rows if type(row).__name__ == "BacktestReportRecord")
    payload = dict(row.report)
    payload["trade_count"] = True
    row.report_hash = digest(
        canonical(
            {
                key: value
                for key, value in payload.items()
                if key not in ("report_hash", "caveat_vi")
            }
        )
    )
    payload["report_hash"] = row.report_hash
    row.report = payload
    row.canonical_json = canonical(payload)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_lease_expiring_in_final_flush_or_retry_read_rejects_result(
    persisted, monkeypatch, retry
):
    from datetime import timedelta

    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session, _ = persisted
    if retry:
        await build(persisted)
        original = session.scalars

        async def slow_receipt_read(statement):
            result = await original(statement)
            if (
                statement.column_descriptions[0]["entity"].__name__
                == "CandidateTrainingRightsRecord"
            ):
                session.clock += timedelta(minutes=6)
            return result

        monkeypatch.setattr(session, "scalars", slow_receipt_read)
    else:
        original = session.flush

        async def slow_flush():
            await original()
            if any(type(row).__name__ == "CandidateTrainingRightsRecord" for row in session.rows):
                session.clock += timedelta(minutes=6)

        monkeypatch.setattr(session, "flush", slow_flush)
    with pytest.raises(CandidateUniverseProofError):
        await build(persisted)


@pytest.fixture
async def opportunity_persisted(monkeypatch):
    from datetime import timedelta

    from tests.unit.test_backtest_decision_opportunities import _pinned_setup
    from tests.unit.test_learning_feature_vector import _market_row

    from trading_bot.adapters.database.models.market_entities import MarketBar
    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
        ReplayTrainingOperation,
        ReplayTrainingRightsReceipt,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.application.backtest_dataset_replay_bundle import (
        BacktestDatasetReplayBundleReference,
    )
    from trading_bot.application.runtime_replay_evaluator import run_runtime_paper_replay

    source, observations, _, manifest, factory = _pinned_setup(cross=True)
    replay = run_runtime_paper_replay(
        observations, manifest=manifest, runtime_evaluator_factory=factory
    ).runs[0]
    session = MemorySession()
    session.clock = source.range_end + timedelta(days=1)
    session.events = []
    session.source_rows = {
        item.manifest.interval: [MarketBar(**vars(_market_row(bar))) for bar in item.bars]
        for item in source.components
    }
    original_execute = session.execute

    async def execute(statement, parameters=None):
        sql = str(statement)
        if "market_bar" in sql:
            assert not {"open", "high", "low", "close", "volume", "quote_volume"} & {
                column.key for column in statement.selected_columns
            }
            session.events.append("source_scan")
            values = statement.compile().params
            timeframe = next(value for key, value in values.items() if key.startswith("timeframe"))
            dataset = next(
                item.manifest for item in source.components if item.manifest.interval == timeframe
            )
            assert values["workspace_id_1"] == WORKSPACE
            assert values["symbol_1"] == source.symbol
            assert values["source_1"] == "BINANCE_USDM_REST_BACKFILL"
            assert values["open_time_1"] == dataset.warmup_start
            assert values["open_time_2"] == dataset.range_end
            assert values["available_at_1"] == dataset.prepared_at
            rows = [
                row
                for row in session.source_rows[timeframe]
                if row.available_at <= dataset.prepared_at
                and row.is_closed
                and dataset.warmup_start <= row.open_time < dataset.range_end
            ]
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    all=lambda: [
                        {
                            column.key: getattr(row, column.key)
                            for column in statement.selected_columns
                        }
                        for row in rows
                    ]
                )
            )
        if "pg_advisory_xact_lock" in sql:
            session.events.append("source_lock")
        if "wp06_lock_candidate_trial" in sql:
            session.events.append("trial_lock")
        return await original_execute(statement, parameters)

    session.execute = execute
    receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=WORKSPACE,
        sensitivity_run=replay.sensitivity_run,
        trade_outcomes=replay.trade_outcomes,
        opportunities=replay.opportunities,
    )
    _add_training_job(
        session, receipt, replay.sensitivity_run.manifest, UUID(manifest.learning_job_id)
    )
    state = SimpleNamespace(source=source, denied=False)

    async def bundle_lookup(self, **kwargs):
        return BacktestDatasetReplayBundleReference.from_bundle(state.source)

    async def child_lookup(self, **kwargs):
        return next(
            item.manifest
            for item in state.source.components
            if item.manifest.dataset_hash == kwargs["dataset_hash"]
        )

    async def authorize(self, **kwargs):
        session.events.append("training_rights")
        if state.denied:
            raise PermissionError("Không có TRAINING.")
        return tuple(
            ReplayTrainingRightsReceipt(
                workspace_id=WORKSPACE,
                bundle_hash=source.dataset_hash,
                child_dataset_hash=item.manifest.dataset_hash,
                interval=item.manifest.interval,
                symbol=source.symbol,
                bars_content_hash=item.manifest.bars_content_hash,
                product="FUTURES",
                venue="BINANCE",
                contract="USD_M_PERPETUAL",
                data_start=item.manifest.warmup_start,
                data_end=item.manifest.range_end,
                policy_id="fixture:training",
                policy_fingerprint="4" * 64,
                bundle_policy_fingerprint=source.policy_fingerprint,
                current_policy_fingerprint="4" * 64,
                terms_url="https://example.invalid/training",
                verified_at=session.clock - timedelta(days=1),
                checked_at=session.clock,
                valid_until=session.clock + timedelta(minutes=5),
                license_evidence_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                evidence_digest="6" * 64,
                license_scope_hash="7" * 64,
                scope_hash="8" * 64,
                operation=ReplayTrainingOperation.TRAINING,
            )
            for item in source.components
        )

    monkeypatch.setattr(BacktestDatasetReplayBundleRepository, "get_by_hash", bundle_lookup)
    monkeypatch.setattr(BacktestDatasetManifestRepository, "get_by_hash", child_lookup)
    monkeypatch.setattr(DatabaseReplayTrainingRightsGate, "authorize", authorize)
    return session, receipt, state


async def build_opportunity(persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofRepository,
    )

    return await CandidateUniverseProofRepository(persisted[0]).build_decision_opportunity(
        workspace_id=WORKSPACE, trial_record_id=persisted[1].trial_record_id
    )


@pytest.mark.asyncio
async def test_opportunity_proof_exact_inventory_counts_rights_and_retry(opportunity_persisted):
    result = await build_opportunity(opportunity_persisted)
    proof = result.proof
    assert proof.opportunity_count == 4
    assert proof.no_candidate_count == 3
    assert proof.candidate_count == proof.eligible_count == proof.outcome_link_count == 1
    assert proof.unlabeled_candidate_count == proof.input_excluded_count == proof.not_run_count == 0
    assert proof.completeness_verified is True
    assert proof.training_authorized is False
    assert proof.query_version == "base-decision-opportunity-universe-v1"
    assert "TEST" not in proof.canonical_json
    assert "fold" not in proof.canonical_json
    assert not {"values", "features", "labels", "bars"} & json.loads(proof.canonical_json).keys()
    session = opportunity_persisted[0]
    assert session.events[:4] == [
        "source_lock",
        "source_lock",
        "training_rights",
        "trial_lock",
    ]
    rights = [row for row in session.rows if type(row).__name__ == "CandidateTrainingRightsRecord"]
    assert len(rights) == 2
    assert all(row.purpose == "training" for row in rights)
    retry = await build_opportunity(opportunity_persisted)
    assert retry.record_id == result.record_id
    assert retry.duplicate
    assert retry.proof.canonical_json == proof.canonical_json


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_event", "input_excluded", "source_gap"])
async def test_opportunity_incomplete_inventory_stays_diagnostic(opportunity_persisted, failure):
    session, _, _ = opportunity_persisted
    rows = [
        row for row in session.rows if type(row).__name__ == "BacktestDecisionOpportunityRecord"
    ]
    if failure == "missing_event":
        session.rows.remove(rows[-1])
    elif failure == "source_gap":
        session.source_rows["1h"].pop(0)
    else:
        _replace_opportunity_row(
            rows[-1],
            input_state="INPUT_EXCLUDED",
            strategy_state="NOT_RUN",
            feature_values_hash=None,
            feature_schema_hash=None,
            vector_hash=None,
            runtime_snapshot_id=None,
            reason_code="INPUT_NOT_READY",
        )
    proof = (await build_opportunity(opportunity_persisted)).proof
    assert proof.completeness_verified is False
    assert proof.no_candidate_count == (2 if failure != "source_gap" else 3)
    assert proof.opportunity_count == (3 if failure == "missing_event" else 4)


def _replace_opportunity_row(row, **changes):
    from dataclasses import fields

    from trading_bot.application.backtest_decision_opportunities import ReplayDecisionOpportunity

    values = {
        field.name: getattr(row, "scenario_key" if field.name == "scenario_id" else field.name)
        for field in fields(ReplayDecisionOpportunity)
        if field.init
    }
    values["feature_snapshot_ids"] = tuple(map(tuple, values["feature_snapshot_ids"]))
    values["source_event_digests"] = tuple(map(tuple, values["source_event_digests"]))
    opportunity = ReplayDecisionOpportunity(**(values | changes))
    for field in fields(opportunity):
        if field.name != "scenario_id":
            value = getattr(opportunity, field.name)
            setattr(
                row,
                field.name,
                [list(pair) for pair in value]
                if field.name in ("feature_snapshot_ids", "source_event_digests")
                else value,
            )
    row.canonical_opportunity = opportunity.canonical_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "ordinal",
        "event",
        "strategy",
        "bundle",
        "schema",
        "state",
        "canonical",
        "source_identity",
        "source_digest",
        "outcome_link",
        "snapshot",
        "not_base",
        "intent",
        "job_scope",
        "receipt_purpose",
    ],
)
async def test_opportunity_corrupt_scope_or_links_fail_closed(
    opportunity_persisted, failure, monkeypatch
):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session, _, _ = opportunity_persisted
    rows = [
        row for row in session.rows if type(row).__name__ == "BacktestDecisionOpportunityRecord"
    ]
    if failure == "duplicate":
        session.rows.append(rows[-1])
    elif failure in ("ordinal", "event", "strategy", "bundle", "schema", "source_digest"):
        changes = {
            "ordinal": {"decision_ordinal": 0},
            "event": {"event_id": "wrong"},
            "strategy": {"strategy_content_hash": "0" * 64},
            "bundle": {"bundle_hash": "0" * 64},
            "schema": {"feature_schema_hash": "0" * 64},
            "source_digest": {"source_event_digests": (("1h", "0" * 64), ("4h", "0" * 64))},
        }[failure]
        if failure == "source_digest":
            from trading_bot.application.backtest_decision_opportunities import metadata_hash

            changes["source_refs_digest"] = metadata_hash(
                [
                    {"timeframe": tf, "source_event_digest": value}
                    for tf, value in changes["source_event_digests"]
                ]
            )
        _replace_opportunity_row(rows[-1], **changes)
    elif failure == "state":
        rows[-1].candidate_id = "0" * 64
    elif failure == "canonical":
        rows[-1].canonical_opportunity += " "
    elif failure == "source_identity":
        session.source_rows["1h"][0].id = uuid4()
    elif failure in ("outcome_link", "snapshot"):
        outcome = next(
            row for row in session.rows if type(row).__name__ == "BacktestTradeOutcomeRecord"
        )
        setattr(
            outcome,
            "entry_opportunity_event_id"
            if failure == "outcome_link"
            else "entry_decision_snapshot_id",
            "0" * 64,
        )
    elif failure == "not_base":
        next(
            row for row in session.rows if type(row).__name__ == "BacktestScenarioRecord"
        ).kind = "COST"
    elif failure in ("intent", "job_scope"):
        job = next(row for row in session.rows if type(row).__name__ == "BacktestReplayJobRecord")
        job.request_payload["learning_intent" if failure == "intent" else "execution_interval"] = (
            "RESEARCH" if failure == "intent" else "4h"
        )
        job.canonical_request = json.dumps(
            job.request_payload, sort_keys=True, separators=(",", ":")
        )
    else:
        from dataclasses import replace

        from trading_bot.adapters.database.replay_training_rights import (
            DatabaseReplayTrainingRightsGate,
        )
        from trading_bot.domain.research.source_policy import SourceUsePurpose

        original = DatabaseReplayTrainingRightsGate.authorize

        async def wrong_purpose(self, **kwargs):
            return tuple(
                replace(receipt, purpose=SourceUsePurpose.BACKTEST_RESEARCH)
                for receipt in await original(self, **kwargs)
            )

        monkeypatch.setattr(DatabaseReplayTrainingRightsGate, "authorize", wrong_purpose)
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)


@pytest.mark.asyncio
async def test_opportunity_rights_denial_precedes_trial_lock_and_source_scan(opportunity_persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    opportunity_persisted[2].denied = True
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)
    assert opportunity_persisted[0].events == [
        "source_lock",
        "source_lock",
        "training_rights",
    ]


def test_proof_cli_has_closed_trade_default_and_enum_selector():
    from trading_bot.cli.candidate_proof import parse_request

    arguments = ["--workspace-id", str(WORKSPACE), "--trial-record-id", str(uuid4())]
    assert parse_request(arguments).universe == "closed-trades-v1"
    assert (
        parse_request(arguments + ["--universe", "decision-opportunities-v1"]).universe
        == "decision-opportunities-v1"
    )
    with pytest.raises(SystemExit):
        parse_request(arguments + ["--universe", "arbitrary-query"])


def test_identity_only_helpers_preserve_existing_manifest_and_source_event_bytes():
    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.backtest_dataset_manifest import (
        MarketBarIdentityMetadata,
        backtest_dataset_identity_metadata_hash,
    )
    from trading_bot.application.decision_snapshot_builder import (
        canonical_market_bar_event_id,
        canonical_market_bar_metadata_event_id,
    )

    source, _, _, _, _ = _pinned_setup()
    for item in source.components:
        metadata = tuple(MarketBarIdentityMetadata.from_bar(bar) for bar in item.bars)
        assert (
            backtest_dataset_identity_metadata_hash(metadata, workspace_id=WORKSPACE)
            == item.manifest.bar_identity_hash
        )
        assert canonical_market_bar_metadata_event_id(
            metadata[0], workspace_id=WORKSPACE
        ) == canonical_market_bar_event_id(item.bars[0], workspace_id=WORKSPACE)
        assert not hasattr(metadata[0], "open")
        assert not hasattr(metadata[0], "volume")


@pytest.mark.asyncio
async def test_opportunity_candidate_without_closed_outcome_is_unlabeled(opportunity_persisted):
    session = opportunity_persisted[0]
    row = [
        row for row in session.rows if type(row).__name__ == "BacktestDecisionOpportunityRecord"
    ][-1]
    _replace_opportunity_row(
        row,
        strategy_state="CANDIDATE",
        decision_outcome="RISK_REJECTED",
        reason_code="RISK_LIMIT",
        candidate_id="9" * 64,
    )
    proof = (await build_opportunity(opportunity_persisted)).proof
    assert proof.completeness_verified is True
    assert proof.candidate_count == 2
    assert proof.eligible_count == proof.outcome_link_count == 1
    assert proof.unlabeled_candidate_count == proof.exclusion_count == 1
    assert proof.exclusion_reason_counts == (("closed_outcome_missing", 1),)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["proof_column", "rights_receipt", "membership", "revoked"])
async def test_opportunity_retry_rechecks_metadata_receipts_and_rights(
    opportunity_persisted, failure
):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session, _, state = opportunity_persisted
    await build_opportunity(opportunity_persisted)
    if failure == "proof_column":
        next(
            row for row in session.rows if type(row).__name__ == "CandidateUniverseProofRecord"
        ).no_candidate_count = 0
    elif failure == "rights_receipt":
        next(
            row for row in session.rows if type(row).__name__ == "CandidateTrainingRightsRecord"
        ).purpose = "backtest_research"
    elif failure == "membership":
        row = [
            row for row in session.rows if type(row).__name__ == "BacktestDecisionOpportunityRecord"
        ][-1]
        _replace_opportunity_row(row, reason_code="OTHER_VALID_REASON")
    else:
        state.denied = True
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["trial_lock", "source_scan", "receipt_flush", "retry_receipt"])
async def test_opportunity_expired_lease_prevents_return(opportunity_persisted, monkeypatch, stage):
    from datetime import timedelta

    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session = opportunity_persisted[0]
    if stage == "retry_receipt":
        await build_opportunity(opportunity_persisted)
        original = session.scalars

        async def delayed(statement):
            result = await original(statement)
            if (
                statement.column_descriptions[0]["entity"].__name__
                == "CandidateTrainingRightsRecord"
            ):
                session.clock += timedelta(minutes=6)
            return result

        monkeypatch.setattr(session, "scalars", delayed)
    elif stage == "receipt_flush":
        original = session.flush

        async def delayed():
            await original()
            if any(type(row).__name__ == "CandidateTrainingRightsRecord" for row in session.rows):
                session.clock += timedelta(minutes=6)

        monkeypatch.setattr(session, "flush", delayed)
    else:
        original = session.execute

        async def delayed(statement, parameters=None):
            result = await original(statement, parameters)
            if (stage == "trial_lock" and "wp06_lock_candidate_trial" in str(statement)) or (
                stage == "source_scan" and "market_bar" in str(statement)
            ):
                session.clock += timedelta(minutes=6)
            return result

        monkeypatch.setattr(session, "execute", delayed)
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)


@pytest.mark.asyncio
async def test_opportunity_requires_identity_pinned_child(opportunity_persisted, monkeypatch):
    from dataclasses import fields

    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    source = opportunity_persisted[2].source

    async def legacy_child(self, **kwargs):
        child = next(
            item.manifest
            for item in source.components
            if item.manifest.dataset_hash == kwargs["dataset_hash"]
        )
        return SimpleNamespace(
            **{field.name: getattr(child, field.name) for field in fields(child)},
            has_pinned_source_identities=False,
        )

    monkeypatch.setattr(BacktestDatasetManifestRepository, "get_by_hash", legacy_child)
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)


@pytest.mark.asyncio
async def test_source_raw_reference_tamper_cannot_keep_opportunity_digest(opportunity_persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    opportunity_persisted[0].source_rows["1h"][0].raw_reference_id = uuid4()
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)


def test_v1_canonical_bytes_equal_pre_task_value_object():
    from trading_bot.application.candidate_universe_proof import CandidateUniverseProof

    values = {
        key: UUID(int=index)
        for index, key in enumerate(
            ("workspace_id", "run_id", "trial_record_id", "report_id", "scenario_id"), 1
        )
    }
    values.update(
        dict.fromkeys(
            (
                "manifest_hash",
                "trial_hash",
                "report_hash",
                "scenario_hash",
                "outcomes_hash",
                "membership_digest",
                "training_rights_digest",
            ),
            "a" * 64,
        )
    )
    proof = CandidateUniverseProof(
        **values, candidate_count=1, eligible_count=1, exclusion_count=0, exclusion_reason_counts=()
    )
    assert proof.content_hash == "1ef8e058019a576e96e09b30a5193abb9802f677f20e66f3d65dbb6ad5a445a5"


@pytest.mark.asyncio
async def test_opportunity_scenario_key_must_match_authoritative_base(opportunity_persisted):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session = opportunity_persisted[0]
    row = [
        row for row in session.rows if type(row).__name__ == "BacktestDecisionOpportunityRecord"
    ][-1]
    row.scenario_key = "forged-base"
    _replace_opportunity_row(row)
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)


@pytest.mark.asyncio
async def test_source_inventory_ignores_revision_available_after_pinned_cutoff(
    opportunity_persisted,
):
    from datetime import timedelta

    from trading_bot.adapters.database.models.market_entities import MarketBar

    session, _, state = opportunity_persisted
    original = session.source_rows["1h"][0]
    future = MarketBar(
        **{column.name: getattr(original, column.name) for column in MarketBar.__table__.columns}
    )
    future.id = uuid4()
    future.revision = 2
    future.supersedes_id = original.id
    future.available_at = state.source.range_end + timedelta(seconds=1)
    session.source_rows["1h"].append(future)
    proof = (await build_opportunity(opportunity_persisted)).proof
    assert proof.completeness_verified is True
    assert proof.opportunity_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["closed-trades-v1", "decision-opportunities-v1"])
async def test_cli_routes_enum_through_transaction_and_prints_only_proof_metadata(
    opportunity_persisted, monkeypatch, capsys, selected
):
    from trading_bot.cli import candidate_proof

    session = opportunity_persisted[0]
    state = []

    @asynccontextmanager
    async def sessions():
        yield session

    @asynccontextmanager
    async def begin():
        state.append("transaction_begin")
        yield
        state.append("transaction_end")

    async def dispose():
        state.append("engine_disposed")

    session.begin = begin
    monkeypatch.setattr(
        candidate_proof,
        "create_builder_pool",
        lambda: (
            SimpleNamespace(dispose=dispose),
            sessions,
        ),
    )
    await candidate_proof.run(
        WORKSPACE, opportunity_persisted[1].trial_record_id, candidate_proof.ProofUniverse(selected)
    )
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"record_id", "duplicate", "proof"}
    assert payload["proof"]["query_version"] == (
        "base-closed-trade-universe-v1"
        if selected == "closed-trades-v1"
        else "base-decision-opportunity-universe-v1"
    )
    assert not {"entry_features", "features", "labels", "bars"} & payload["proof"].keys()
    assert state == ["transaction_begin", "transaction_end", "engine_disposed"]


def test_replay_observation_and_proof_share_metadata_only_decision_event_id():
    from datetime import UTC, datetime

    from tests.unit.test_backtest_decision_opportunities import _pinned_setup

    from trading_bot.application.backtest_dataset_replay_observations import (
        replay_decision_event_id,
    )

    source, observations, _, _, _ = _pinned_setup()
    decision = next(item for item in source.components if item.manifest.interval == "1h")
    bars = [
        bar for bar in decision.bars if source.evaluation_start <= bar.open_time < source.range_end
    ]
    assert tuple(
        replay_decision_event_id(
            bundle_hash=source.dataset_hash,
            strategy_release_id="h01-v1",
            decision_timeframe="1h",
            decision_open_time=bar.open_time,
        )
        for bar in bars
    ) == tuple(item.event_id for item in observations)

    assert (
        replay_decision_event_id(
            bundle_hash="a" * 64,
            strategy_release_id="h01-v1",
            decision_timeframe="1h",
            decision_open_time=datetime(2026, 11, 1, tzinfo=UTC),
        )
        == "95682cad1e1276323f7fa910f8f296e0329e100918d564453e6b90a0d6e7a30b"
    )


@pytest.mark.asyncio
async def test_source_gap_never_allows_opportunity_event_outside_manifest_grid(
    opportunity_persisted,
):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session = opportunity_persisted[0]
    session.source_rows["1h"].pop()
    row = [
        row for row in session.rows if type(row).__name__ == "BacktestDecisionOpportunityRecord"
    ][-1]
    _replace_opportunity_row(row, event_id="9" * 64)
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)


@pytest.mark.asyncio
async def test_non_decision_child_gap_never_disables_exact_decision_ordinal(
    opportunity_persisted,
):
    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    session = opportunity_persisted[0]
    session.source_rows["4h"].pop(0)
    row = [
        row for row in session.rows if type(row).__name__ == "BacktestDecisionOpportunityRecord"
    ][-1]
    _replace_opportunity_row(row, decision_ordinal=99)
    with pytest.raises(CandidateUniverseProofError):
        await build_opportunity(opportunity_persisted)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_index", [-3, -1])
async def test_missing_decision_source_keeps_only_grid_bound_diagnostic_opportunity(
    opportunity_persisted,
    missing_index,
):
    from datetime import timedelta

    session = opportunity_persisted[0]
    session.source_rows["1h"].pop(missing_index)
    row = [
        row for row in session.rows if type(row).__name__ == "BacktestDecisionOpportunityRecord"
    ][missing_index]
    _replace_opportunity_row(row, event_at=row.event_at - timedelta(seconds=30))
    proof = (await build_opportunity(opportunity_persisted)).proof
    assert proof.completeness_verified is False
    assert proof.opportunity_count == 4
    assert proof.no_candidate_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["evaluation_count", "start_alignment"])
async def test_decision_grid_rejects_manifest_count_or_start_misalignment(
    opportunity_persisted,
    mismatch,
):
    from datetime import timedelta

    from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
        CandidateUniverseProofError,
    )

    source = opportunity_persisted[2].source
    child = next(item.manifest for item in source.components if item.manifest.interval == "1h")
    # Double repository trả metadata hỏng để kiểm phòng thủ trước đối chiếu rows.
    if mismatch == "evaluation_count":
        object.__setattr__(child, "evaluation_bar_count", child.evaluation_bar_count + 1)
    else:
        object.__setattr__(child, "warmup_start", child.warmup_start + timedelta(minutes=1))
    with pytest.raises(CandidateUniverseProofError, match="Decision grid"):
        await build_opportunity(opportunity_persisted)
