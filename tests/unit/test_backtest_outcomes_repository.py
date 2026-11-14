from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.backtest_outcomes import (
    BacktestOutcomeRepository,
    PersistedBacktestTradeOutcome,
)
from trading_bot.adapters.database.repositories.backtests import BacktestAppendResult
from trading_bot.application.backtest import EvaluationReport, ReplayManifest, TrialRecord
from trading_bot.application.backtest_sensitivity import SensitivityRun, SensitivityScenario
from trading_bot.domain.learning.evaluation import EvaluationTrade

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TRIAL_RECORD_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
RUN_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
REPORT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _manifest() -> ReplayManifest:
    return ReplayManifest(
        dataset_hash="a" * 64,
        range_start=NOW,
        range_end=NOW + timedelta(days=1),
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        strategy_release_id="h01-v1",
        strategy_content_hash="b" * 64,
        feature_schema_hash="c" * 64,
        fill_model_version="paper-v1",
        pipeline_content_hash="d" * 64,
        cost_assumptions=(("fee_rate", "0.001"),),
        parameter_family_hash="e" * 64,
        trial_count=1,
        trial_index=0,
        seed=7,
    )


def _trade() -> EvaluationTrade:
    return EvaluationTrade(
        trade_id="trade-1",
        opened_at=NOW + timedelta(minutes=1),
        closed_at=NOW + timedelta(minutes=2),
        gross_pnl=Decimal("10"),
        fees=Decimal("1"),
        funding=Decimal("-0.25"),
        slippage=Decimal("0.5"),
        exposure=Decimal("100"),
    )


def _run() -> SensitivityRun:
    manifest = _manifest()
    report = EvaluationReport(
        manifest_hash=manifest.content_hash,
        report_hash="f" * 64,
        observation_count=1,
        usable_observation_count=1,
        trade_count=1,
        coverage=1.0,
        trade_frequency=1.0,
        gross_pnl=Decimal("10"),
        net_pnl=Decimal("8.75"),
        fees=Decimal("1"),
        funding=Decimal("-0.25"),
        slippage=Decimal("0.5"),
        expectancy=Decimal("8.75"),
        max_drawdown=Decimal("0"),
        average_exposure=Decimal("100"),
        confidence_interval=None,
        caveat_vi="Chỉ là kiểm thử.",
        trial_record=TrialRecord(
            trial_id=hashlib.sha256(f"trial:{manifest.content_hash}".encode()).hexdigest(),
            manifest_hash=manifest.content_hash,
            parameter_family_hash=manifest.parameter_family_hash,
            trial_index=0,
            trial_count=1,
            status="COMPLETED",
            observation_count=1,
            completed_observation_count=1,
            trade_count=1,
        ),
    )
    return SensitivityRun(
        scenario=SensitivityScenario(
            scenario_id="atr-neighbor",
            kind="PARAMETER",
            parameter_overrides=(("atr_multiplier", "2.1"),),
        ),
        manifest=manifest,
        result=report,
    )


class FakeSession:
    def __init__(self, results: list[object | None] | None = None) -> None:
        self.results = iter(results or [])
        self.added: list[object] = []
        self.statements: list[str] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.results)

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.mark.asyncio
async def test_persists_sensitivity_values_and_canonical_trade_outcome(monkeypatch) -> None:
    from trading_bot.adapters.database.repositories.backtests import BacktestRepository

    async def append_result(self, *, workspace_id, manifest, result):
        assert workspace_id == WORKSPACE_ID
        assert manifest.content_hash == result.manifest_hash
        return BacktestAppendResult(RUN_ID, TRIAL_RECORD_ID, REPORT_ID, False, run_duplicate=False)

    monkeypatch.setattr(BacktestRepository, "append_result", append_result)
    session = FakeSession([SimpleNamespace(trial_id=_run().trial_id), None, None])
    repository = BacktestOutcomeRepository(session)  # type: ignore[arg-type]

    appended = await repository.append_sensitivity_run(
        workspace_id=WORKSPACE_ID,
        sensitivity_run=_run(),
        trade_outcomes=(_trade(),),
    )

    assert appended.scenario_id == "atr-neighbor"
    assert len(appended.scenario_hash) == 64
    assert len(appended.outcomes_hash) == 64
    assert appended.trade_count == 1
    scenario, outcome = session.added
    assert scenario.workspace_id == WORKSPACE_ID
    assert scenario.trial_record_id == TRIAL_RECORD_ID
    assert scenario.scenario["parameter_overrides"] == [["atr_multiplier", "2.1"]]
    assert outcome.canonical_json
    assert outcome.outcome_hash == hashlib.sha256(outcome.canonical_json.encode()).hexdigest()
    assert all("workspace_id =" in statement for statement in session.statements)


@pytest.mark.asyncio
async def test_rejects_outcomes_that_do_not_match_report_before_persisting() -> None:
    repository = BacktestOutcomeRepository(FakeSession())  # type: ignore[arg-type]
    different_trade = _trade().__class__(
        trade_id="different",
        opened_at=_trade().opened_at,
        closed_at=_trade().closed_at,
        gross_pnl=Decimal("11"),
        fees=Decimal("1"),
        funding=Decimal("-0.25"),
        slippage=Decimal("0.5"),
        exposure=Decimal("100"),
    )

    with pytest.raises(ValueError, match="không khớp report"):
        await repository.append_sensitivity_run(
            workspace_id=WORKSPACE_ID,
            sensitivity_run=_run(),
            trade_outcomes=(different_trade,),
        )


def test_outcome_models_are_append_only_workspace_scoped() -> None:
    from trading_bot.adapters.database.models.backtest_outcome_entities import (
        BacktestScenarioRecord,
        BacktestTradeOutcomeRecord,
    )

    for model in (BacktestScenarioRecord, BacktestTradeOutcomeRecord):
        assert "workspace_id" in model.__table__.c
        assert any(
            constraint.name and constraint.name.startswith("uq_")
            for constraint in model.__table__.constraints
        )


def _bundle_run(bundle):
    from dataclasses import replace

    run = _run()
    manifest = replace(run.manifest, dataset_hash=bundle.dataset_hash)
    trial = replace(
        run.result.trial_record,
        manifest_hash=manifest.content_hash,
        trial_id=hashlib.sha256(f"trial:{manifest.content_hash}".encode()).hexdigest(),
    )
    return replace(
        run,
        manifest=manifest,
        result=replace(run.result, manifest_hash=manifest.content_hash, trial_record=trial),
    )


class RightsSession(FakeSession):
    def __init__(self, results, receipts=()):
        super().__init__(results)
        self.receipts = receipts
        self.flushed = []

    async def scalars(self, statement):
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(all=lambda: list(self.receipts))

    async def flush(self):
        self.flushed = list(self.added)


@pytest.mark.asyncio
async def test_existing_receiptless_run_with_new_report_never_acquires_current_rights():
    from dataclasses import replace

    from test_backtest_dataset_replay_bundle_persistence import _bundle

    from trading_bot.adapters.database.models.backtest_entities import (
        BacktestReportRecord,
        BacktestRunRecord,
    )
    from trading_bot.adapters.database.models.backtest_outcome_entities import (
        BacktestRunDatasetRightsRecord,
    )

    bundle = _bundle()
    run = _bundle_run(bundle)
    # Giữ repository thật để cờ duplicate phụ thuộc từng run/trial/report đã lưu.
    first_session = RightsSession(
        [None, None, None, SimpleNamespace(trial_id=run.trial_id), None, None]
    )
    first = await BacktestOutcomeRepository(first_session).append_sensitivity_run(
        workspace_id=bundle.workspace_id,
        sensitivity_run=run,
        trade_outcomes=(_trade(),),
    )
    stored_run, stored_trial, first_report, scenario, outcome = first_session.added
    changed_run = replace(
        run,
        result=replace(run.result, report_hash="0" * 64, caveat_vi="Báo cáo bổ sung."),
    )
    session = RightsSession([stored_run, stored_trial, None, stored_trial, scenario, outcome])
    second = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=bundle.workspace_id,
        sensitivity_run=changed_run,
        trade_outcomes=(_trade(),),
        dataset_bundle=bundle,
    )

    assert first.run_id == second.run_id
    assert first.trial_record_id == second.trial_record_id
    assert first.report_id != second.report_id
    assert not second.duplicate
    all_rows = first_session.added + session.added
    assert sum(isinstance(row, BacktestRunRecord) for row in all_rows) == 1
    assert sum(isinstance(row, BacktestReportRecord) for row in all_rows) == 2
    assert not any(isinstance(row, BacktestRunDatasetRightsRecord) for row in all_rows)
    assert session.added[0].report_hash != first_report.report_hash


def _outcome_append_stub(monkeypatch, *, duplicate=False):
    from trading_bot.adapters.database.repositories.backtests import BacktestRepository

    async def append_result(self, **kwargs):
        return BacktestAppendResult(
            RUN_ID, TRIAL_RECORD_ID, REPORT_ID, duplicate, run_duplicate=duplicate
        )

    monkeypatch.setattr(BacktestRepository, "append_result", append_result)


@pytest.mark.asyncio
async def test_bundle_outcome_appends_each_child_receipt_in_callers_session(monkeypatch):
    from test_backtest_dataset_replay_bundle_persistence import _bundle

    from trading_bot.adapters.database.models.backtest_outcome_entities import (
        BacktestRunDatasetRightsRecord,
    )

    bundle = _bundle()
    run = _bundle_run(bundle)
    _outcome_append_stub(monkeypatch)
    session = RightsSession([SimpleNamespace(trial_id=run.trial_id), None, None])
    result = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=bundle.workspace_id,
        sensitivity_run=run,
        trade_outcomes=(_trade(),),
        dataset_bundle=bundle,
    )
    receipts = [row for row in session.added if isinstance(row, BacktestRunDatasetRightsRecord)]
    assert not result.duplicate
    assert len(receipts) == 2
    assert {row.child_dataset_hash for row in receipts} == {
        item.manifest.dataset_hash for item in bundle.components
    }
    for row in receipts:
        assert row in session.flushed
        assert row.workspace_id == bundle.workspace_id
        assert row.run_id == RUN_ID
        assert row.bundle_hash == bundle.dataset_hash
        assert row.checked_at == bundle.components[0].current_rights.checked_at
        assert row.evidence_hash == hashlib.sha256(row.canonical_evidence.encode()).hexdigest()
    assert all("workspace_id =" in statement for statement in session.statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["type", "workspace", "hash", "unchecked", "policy"])
async def test_bundle_outcome_rejects_invalid_evidence_before_any_write(monkeypatch, invalid):
    from dataclasses import replace
    from uuid import uuid4

    from test_backtest_dataset_replay_bundle_persistence import _bundle

    from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict

    bundle = _bundle()
    run = _bundle_run(bundle)
    workspace_id = bundle.workspace_id
    if invalid == "workspace":
        workspace_id = uuid4()
    elif invalid == "hash":
        run = _run()
    elif invalid == "type":
        bundle = SimpleNamespace(**{field: getattr(bundle, field) for field in bundle.__slots__})
    else:
        rights = bundle.components[-1].current_rights
        changed = replace(
            rights,
            **(
                {"checked_at": None} if invalid == "unchecked" else {"policy_fingerprint": "0" * 64}
            ),
        )
        # Mô phỏng snapshot bị sửa sau khi bundle đã được xác thực.
        object.__setattr__(bundle.components[-1], "current_rights", changed)
    session = RightsSession([])
    with pytest.raises(
        (ValueError, BacktestRepositoryConflict), match="bundle|workspace|khớp|checked_at|quyền"
    ):
        await BacktestOutcomeRepository(session).append_sensitivity_run(
            workspace_id=workspace_id,
            sensitivity_run=run,
            trade_outcomes=(_trade(),),
            dataset_bundle=bundle,
        )
    assert not session.added
    assert not session.statements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        "same",
        "lease",
        "legacy",
        "partial",
        "extra",
        "verified",
        "canonical",
        "json",
        "hash",
        "scope",
    ],
)
async def test_duplicate_bundle_outcome_preserves_first_complete_receipts(monkeypatch, state):
    from dataclasses import replace

    from test_backtest_dataset_replay_bundle_persistence import _bundle

    from trading_bot.adapters.database.models.backtest_outcome_entities import (
        BacktestRunDatasetRightsRecord,
    )
    from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict

    bundle = _bundle()
    run = _bundle_run(bundle)
    _outcome_append_stub(monkeypatch)
    first_session = RightsSession([SimpleNamespace(trial_id=run.trial_id), None, None])
    await BacktestOutcomeRepository(first_session).append_sensitivity_run(
        workspace_id=bundle.workspace_id,
        sensitivity_run=run,
        trade_outcomes=(_trade(),),
        dataset_bundle=bundle,
    )
    scenario, outcome = first_session.added[:2]
    receipts = [
        row for row in first_session.added if isinstance(row, BacktestRunDatasetRightsRecord)
    ]
    original = [(row.canonical_evidence, row.evidence_hash) for row in receipts]
    if state == "legacy":
        receipts = []
    elif state == "partial":
        receipts = receipts[:1]
    elif state == "extra":
        receipts = [*receipts, receipts[0]]
    elif state == "lease":
        bundle = replace(
            bundle,
            components=tuple(
                replace(
                    item,
                    current_rights=replace(
                        item.current_rights,
                        checked_at=item.current_rights.checked_at + timedelta(seconds=1),
                    ),
                )
                for item in bundle.components
            ),
        )
    elif state == "verified":
        bundle = replace(
            bundle,
            components=tuple(
                replace(
                    item,
                    current_rights=replace(
                        item.current_rights,
                        verified_at=item.current_rights.verified_at + timedelta(seconds=1),
                    ),
                )
                for item in bundle.components
            ),
        )
    elif state == "canonical":
        receipts[0].canonical_evidence = "{}"
    elif state == "json":
        receipts[0].evidence_json = {**receipts[0].evidence_json, "schema_version": True}
    elif state == "hash":
        receipts[0].evidence_hash = "0" * 64
    elif state == "scope":
        receipts[0].terms_url = "https://example.invalid/changed"
    _outcome_append_stub(monkeypatch, duplicate=True)
    session = RightsSession([SimpleNamespace(trial_id=run.trial_id), scenario, outcome], receipts)
    if state in ("same", "lease", "legacy"):
        result = await BacktestOutcomeRepository(session).append_sensitivity_run(
            workspace_id=bundle.workspace_id,
            sensitivity_run=run,
            trade_outcomes=(_trade(),),
            dataset_bundle=bundle,
        )
        assert result.duplicate
        if state in ("same", "lease"):
            assert [(row.canonical_evidence, row.evidence_hash) for row in receipts] == original
    else:
        with pytest.raises(BacktestRepositoryConflict, match="quyền|biên nhận"):
            await BacktestOutcomeRepository(session).append_sensitivity_run(
                workspace_id=bundle.workspace_id,
                sensitivity_run=run,
                trade_outcomes=(_trade(),),
                dataset_bundle=bundle,
            )
    assert not session.added
    assert "backtest_run_dataset_rights.workspace_id" in session.statements[-1]
    assert "backtest_run_dataset_rights.run_id" in session.statements[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("provenance", [False, True])
async def test_outcome_round_trip_preserves_legacy_or_complete_entry(monkeypatch, provenance):
    import json
    from dataclasses import replace

    from test_evaluation_trade_entry_lineage import trade_with_entry

    from trading_bot.adapters.database.repositories.backtest_outcomes import _trade_payload

    trade = _trade()
    if provenance:
        entry = trade_with_entry()
        trade = replace(
            trade,
            entry_decision_snapshot_id=entry.entry_decision_snapshot_id,
            entry_decision_at=entry.entry_decision_at,
            entry_feature_schema_hash=entry.entry_feature_schema_hash,
            entry_features=entry.entry_features,
            entry_features_hash=entry.entry_features_hash,
            entry_symbol=entry.entry_symbol,
        )
    _outcome_append_stub(monkeypatch)
    session = RightsSession([SimpleNamespace(trial_id=_run().trial_id), None, None])
    appended = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=WORKSPACE_ID,
        sensitivity_run=_run(),
        trade_outcomes=(trade,),
    )
    scenario, outcome = session.added
    keys = {key for key in outcome.outcome if key.startswith("entry_")}
    assert len(keys) == (6 if provenance else 0)
    assert outcome.entry_decision_snapshot_id == trade.entry_decision_snapshot_id
    assert outcome.entry_features == (
        json.loads(json.dumps(trade.entry_features)) if provenance else None
    )
    assert outcome.entry_features_hash == trade.entry_features_hash
    assert outcome.entry_symbol == trade.entry_symbol
    loaded = await BacktestOutcomeRepository(RightsSession([], [outcome])).load_trade_outcomes(
        workspace_id=WORKSPACE_ID,
        trial_record_id=TRIAL_RECORD_ID,
    )
    assert loaded.trades == (trade,)
    assert loaded.trades[0].has_entry_provenance is provenance
    assert loaded.outcomes_hash == appended.outcomes_hash
    by_id = await BacktestOutcomeRepository(
        RightsSession([], [outcome])
    ).load_trade_outcome_rows_by_id(
        workspace_id=WORKSPACE_ID,
        trial_record_id=TRIAL_RECORD_ID,
        outcome_record_ids={outcome.id},
    )
    assert by_id.rows[0].trade == trade
    _outcome_append_stub(monkeypatch, duplicate=True)
    retry = await BacktestOutcomeRepository(
        RightsSession(
            [
                SimpleNamespace(trial_id=_run().trial_id),
                scenario,
                outcome,
            ]
        )
    ).append_sensitivity_run(
        workspace_id=WORKSPACE_ID,
        sensitivity_run=_run(),
        trade_outcomes=(trade,),
    )
    assert retry.duplicate
    assert retry.outcomes_hash == loaded.outcomes_hash
    assert _trade_payload(trade) == outcome.outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        "columns_only",
        "partial_payload",
        "vector_shape",
        "vector_bool",
        "vector_hash",
        "column_hash",
        "column_vector",
        "column_symbol",
        "unknown_key",
        "payload_array",
    ],
)
@pytest.mark.parametrize("loader", ["all", "ids", "retry"])
async def test_outcome_rejects_malformed_provenance_on_read_and_retry(
    monkeypatch, malformed, loader
):
    import json
    from dataclasses import replace

    from test_evaluation_trade_entry_lineage import trade_with_entry

    from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict

    entry = trade_with_entry()
    trade = replace(
        _trade(),
        entry_decision_snapshot_id=entry.entry_decision_snapshot_id,
        entry_decision_at=entry.entry_decision_at,
        entry_feature_schema_hash=entry.entry_feature_schema_hash,
        entry_features=entry.entry_features,
        entry_features_hash=entry.entry_features_hash,
        entry_symbol=entry.entry_symbol,
    )
    _outcome_append_stub(monkeypatch)
    first = RightsSession([SimpleNamespace(trial_id=_run().trial_id), None, None])
    await BacktestOutcomeRepository(first).append_sensitivity_run(
        workspace_id=WORKSPACE_ID,
        sensitivity_run=_run(),
        trade_outcomes=(trade,),
    )
    scenario, row = first.added
    if malformed == "columns_only":
        row.outcome = {
            key: value for key, value in row.outcome.items() if not key.startswith("entry_")
        }
    elif malformed == "partial_payload":
        row.outcome.pop("entry_decision_at")
    elif malformed == "vector_shape":
        row.outcome["entry_features"] = {"1h:atr": 1.0}
    elif malformed == "vector_bool":
        row.outcome["entry_features"][0][1] = True
    elif malformed == "vector_hash":
        row.outcome["entry_features_hash"] = "0" * 64
    elif malformed == "column_hash":
        row.entry_features_hash = "0" * 64
    elif malformed == "column_symbol":
        row.entry_symbol = "ETHUSDT"
    elif malformed == "column_vector":
        row.entry_features = [["1h:atr", 7.0]]
    elif malformed == "unknown_key":
        row.outcome["unknown"] = True
    elif malformed == "payload_array":
        row.outcome = []
    row.canonical_json = json.dumps(
        row.outcome, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    row.outcome_hash = hashlib.sha256(row.canonical_json.encode()).hexdigest()
    _outcome_append_stub(monkeypatch, duplicate=True)
    session = RightsSession([SimpleNamespace(trial_id=_run().trial_id), scenario, row], [row])
    repository = BacktestOutcomeRepository(session)
    if loader == "all":
        action = repository.load_trade_outcomes(
            workspace_id=WORKSPACE_ID,
            trial_record_id=TRIAL_RECORD_ID,
        )
    elif loader == "ids":
        action = repository.load_trade_outcome_rows_by_id(
            workspace_id=WORKSPACE_ID,
            trial_record_id=TRIAL_RECORD_ID,
            outcome_record_ids={row.id},
        )
    else:
        action = repository.append_sensitivity_run(
            workspace_id=WORKSPACE_ID,
            sensitivity_run=_run(),
            trade_outcomes=(trade,),
        )
    with pytest.raises(BacktestRepositoryConflict):
        await action


def _training_bundle_repository_fixture(*, corrupt_bar: bool = False):
    import json
    from types import SimpleNamespace
    from uuid import uuid4

    from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
    from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
    from trading_bot.application.backtest_dataset_replay_bundle import (
        BacktestDatasetReplayBundleReference,
        build_backtest_dataset_replay_bundle,
    )
    from trading_bot.application.binance_rest_backfill import (
        BackfillRightsEvidence,
        BackfillRightsPurpose,
        RestBackfillRequest,
    )
    from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar

    start = NOW - timedelta(days=1)
    evaluation_start = start + timedelta(hours=1)
    range_end = start + timedelta(hours=2)
    rights = BackfillRightsEvidence(
        workspace_id=WORKSPACE_ID,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/terms",
        policy_fingerprint="e" * 64,
        verified_at=NOW - timedelta(days=3),
        checked_at=NOW - timedelta(days=2),
        valid_until=NOW + timedelta(days=1),
    )
    bars = []
    for index in range(2):
        opened_at = start + timedelta(hours=index)
        closed_at = opened_at + timedelta(hours=1) - timedelta(milliseconds=1)
        checksum = hashlib.sha256(f"bar-{index}".encode()).hexdigest()
        logical_key = f"BTCUSDT:1h:{opened_at.isoformat()}"
        identity = MarketBarSourceIdentity(
            workspace_id=WORKSPACE_ID,
            record_id=uuid4(),
            logical_key=logical_key,
            revision=1,
            supersedes_record_id=None,
            checksum=checksum,
        )
        bars.append(
            OHLCVBar(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=opened_at,
                close_time=closed_at,
                event_at=closed_at,
                received_at=closed_at,
                available_at=closed_at,
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("99"),
                close=Decimal("101"),
                volume=Decimal("1"),
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                checksum=checksum,
                source_identity=identity,
            )
        )
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=start,
        end=range_end,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    dataset = build_backtest_dataset_manifest(
        request,
        evaluation_start=evaluation_start,
        warmup_bars=1,
        bars=tuple(bars),
        rights_evidence=rights,
        prepared_at=range_end,
        schema_version=4,
    )
    bundle = build_backtest_dataset_replay_bundle(
        (BacktestDatasetReplayInput(dataset, tuple(bars), rights),)
    )
    reference = BacktestDatasetReplayBundleReference.from_bundle(bundle)
    run_manifest = {"dataset_hash": reference.bundle_hash}
    canonical_manifest = json.dumps(
        run_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    manifest_hash = hashlib.sha256(canonical_manifest.encode()).hexdigest()
    run_id = uuid4()
    trial = SimpleNamespace(
        workspace_id=WORKSPACE_ID,
        id=TRIAL_RECORD_ID,
        run_id=run_id,
        manifest_hash=manifest_hash,
    )
    run = SimpleNamespace(
        workspace_id=WORKSPACE_ID,
        id=run_id,
        manifest_hash=manifest_hash,
        manifest=run_manifest,
        canonical_manifest=canonical_manifest,
    )
    component = reference.components[0]
    receipt_payload = {
        "schema_version": 1,
        "workspace_id": str(WORKSPACE_ID),
        "run_id": str(run_id),
        "bundle_hash": reference.bundle_hash,
        "child_dataset_hash": component.dataset_hash,
        "interval": component.interval,
        "provider": reference.rights_provider,
        "purpose": reference.rights_purpose,
        "terms_url": rights.terms_url,
        "policy_fingerprint": rights.policy_fingerprint,
        "verified_at": rights.verified_at.isoformat(),
        "checked_at": rights.checked_at.isoformat(),
        "valid_until": rights.valid_until.isoformat(),
    }
    canonical_receipt = json.dumps(
        receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    receipt = SimpleNamespace(
        workspace_id=WORKSPACE_ID,
        run_id=run_id,
        bundle_hash=reference.bundle_hash,
        child_dataset_hash=component.dataset_hash,
        interval=component.interval,
        provider=reference.rights_provider,
        purpose=reference.rights_purpose,
        terms_url=rights.terms_url,
        policy_fingerprint=rights.policy_fingerprint,
        verified_at=rights.verified_at,
        checked_at=rights.checked_at,
        valid_until=rights.valid_until,
        evidence_json=receipt_payload,
        canonical_evidence=canonical_receipt,
        evidence_hash=hashlib.sha256(canonical_receipt.encode()).hexdigest(),
    )
    market_rows = [
        {
            "id": bar.source_identity.record_id,
            "workspace_id": WORKSPACE_ID,
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "source": bar.source,
            "logical_key": bar.source_identity.logical_key,
            "revision": bar.source_identity.revision,
            "supersedes_id": bar.source_identity.supersedes_record_id,
            "checksum": bar.source_identity.checksum,
            "raw_reference_id": None,
            "open_time": bar.open_time,
            "close_time": bar.close_time,
            "available_at": bar.available_at,
            "is_closed": bar.is_closed,
        }
        for bar in bars
    ]
    if corrupt_bar:
        market_rows[0] = {**market_rows[0], "checksum": "0" * 64}

    class Session:
        def __init__(self):
            self.scalar_values = (trial, run)
            self.scalar_calls = 0
            self.rights_receipts = (receipt,)
            self.outcome_rows = []

        async def scalar(self, statement):
            value = self.scalar_values[self.scalar_calls]
            self.scalar_calls += 1
            return value

        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: self.rights_receipts)

        async def execute(self, statement):
            if "backtest_trade_outcome" in str(statement):
                return SimpleNamespace(
                    mappings=lambda: SimpleNamespace(all=lambda: self.outcome_rows)
                )
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: market_rows))

    return Session(), reference, dataset, market_rows, run_id, manifest_hash


@pytest.mark.asyncio
async def test_training_bundle_lineage_joins_all_authoritative_market_bar_ids(monkeypatch) -> None:
    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )

    session, reference, dataset, market_rows, run_id, manifest_hash = (
        _training_bundle_repository_fixture()
    )

    async def get_bundle(self, **kwargs):
        return reference

    async def get_dataset(self, **kwargs):
        return dataset

    monkeypatch.setattr(BacktestDatasetReplayBundleRepository, "get_by_hash", get_bundle)
    monkeypatch.setattr(BacktestDatasetManifestRepository, "get_by_hash", get_dataset)
    row = PersistedBacktestTradeOutcome(
        record_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        workspace_id=WORKSPACE_ID,
        run_id=run_id,
        trial_record_id=TRIAL_RECORD_ID,
        manifest_hash=manifest_hash,
        ordinal=0,
        trade_id="trade-1",
        outcome_hash="1" * 64,
        canonical_json="{}",
        trade=_trade(),
    )
    session.outcome_rows = [
        {
            "id": row.record_id,
            "workspace_id": row.workspace_id,
            "run_id": row.run_id,
            "trial_record_id": row.trial_record_id,
            "manifest_hash": row.manifest_hash,
            "ordinal": row.ordinal,
        }
    ]
    lineage = await BacktestOutcomeRepository(session).load_training_bundle_lineage(
        workspace_id=WORKSPACE_ID,
        trial_record_id=TRIAL_RECORD_ID,
        outcomes=(row,),
    )

    assert lineage.bundle_hash == reference.bundle_hash
    assert len(lineage.source_use_lineage) == len(reference.components) == 1
    child = lineage.source_use_lineage[0]
    assert child.child_dataset_hash == dataset.dataset_hash
    assert child.policy_fingerprint == reference.policy_fingerprint
    assert child.market_bar_ids == tuple(f"market-bar-record:{item['id']}" for item in market_rows)


@pytest.mark.asyncio
async def test_training_bundle_lineage_rejects_market_bar_identity_drift(monkeypatch) -> None:
    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict

    session, reference, dataset, _rows, run_id, manifest_hash = _training_bundle_repository_fixture(
        corrupt_bar=True
    )

    async def get_bundle(self, **kwargs):
        return reference

    async def get_dataset(self, **kwargs):
        return dataset

    monkeypatch.setattr(BacktestDatasetReplayBundleRepository, "get_by_hash", get_bundle)
    monkeypatch.setattr(BacktestDatasetManifestRepository, "get_by_hash", get_dataset)
    row = PersistedBacktestTradeOutcome(
        record_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        workspace_id=WORKSPACE_ID,
        run_id=run_id,
        trial_record_id=TRIAL_RECORD_ID,
        manifest_hash=manifest_hash,
        ordinal=0,
        trade_id="trade-1",
        outcome_hash="1" * 64,
        canonical_json="{}",
        trade=_trade(),
    )
    session.outcome_rows = [
        {
            "id": row.record_id,
            "workspace_id": row.workspace_id,
            "run_id": row.run_id,
            "trial_record_id": row.trial_record_id,
            "manifest_hash": row.manifest_hash,
            "ordinal": row.ordinal,
        }
    ]

    with pytest.raises(BacktestRepositoryConflict, match="MarketBar UUID/revision set"):
        await BacktestOutcomeRepository(session).load_training_bundle_lineage(
            workspace_id=WORKSPACE_ID,
            trial_record_id=TRIAL_RECORD_ID,
            outcomes=(row,),
        )


@pytest.mark.asyncio
async def test_training_bundle_lineage_rejects_caller_outcomes_absent_from_persisted_run(
    monkeypatch,
) -> None:
    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict

    session, reference, dataset, _rows, run_id, manifest_hash = (
        _training_bundle_repository_fixture()
    )

    async def get_bundle(self, **kwargs):
        return reference

    async def get_dataset(self, **kwargs):
        return dataset

    monkeypatch.setattr(BacktestDatasetReplayBundleRepository, "get_by_hash", get_bundle)
    monkeypatch.setattr(BacktestDatasetManifestRepository, "get_by_hash", get_dataset)
    row = PersistedBacktestTradeOutcome(
        record_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        workspace_id=WORKSPACE_ID,
        run_id=run_id,
        trial_record_id=TRIAL_RECORD_ID,
        manifest_hash=manifest_hash,
        ordinal=0,
        trade_id="trade-1",
        outcome_hash="1" * 64,
        canonical_json="{}",
        trade=_trade(),
    )

    with pytest.raises(BacktestRepositoryConflict, match="outcome đã nạp"):
        await BacktestOutcomeRepository(session).load_training_bundle_lineage(
            workspace_id=WORKSPACE_ID,
            trial_record_id=TRIAL_RECORD_ID,
            outcomes=(row,),
        )


@pytest.mark.asyncio
async def test_training_bundle_lineage_rejects_missing_child_rights_receipt(monkeypatch) -> None:
    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict

    session, reference, dataset, _rows, run_id, manifest_hash = (
        _training_bundle_repository_fixture()
    )
    session.rights_receipts = ()

    async def get_bundle(self, **kwargs):
        return reference

    async def get_dataset(self, **kwargs):
        return dataset

    monkeypatch.setattr(BacktestDatasetReplayBundleRepository, "get_by_hash", get_bundle)
    monkeypatch.setattr(BacktestDatasetManifestRepository, "get_by_hash", get_dataset)
    row = PersistedBacktestTradeOutcome(
        record_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        workspace_id=WORKSPACE_ID,
        run_id=run_id,
        trial_record_id=TRIAL_RECORD_ID,
        manifest_hash=manifest_hash,
        ordinal=0,
        trade_id="trade-1",
        outcome_hash="1" * 64,
        canonical_json="{}",
        trade=_trade(),
    )
    session.outcome_rows = [
        {
            "id": row.record_id,
            "workspace_id": row.workspace_id,
            "run_id": row.run_id,
            "trial_record_id": row.trial_record_id,
            "manifest_hash": row.manifest_hash,
            "ordinal": row.ordinal,
        }
    ]

    with pytest.raises(BacktestRepositoryConflict, match="Tập receipt rights"):
        await BacktestOutcomeRepository(session).load_training_bundle_lineage(
            workspace_id=WORKSPACE_ID,
            trial_record_id=TRIAL_RECORD_ID,
            outcomes=(row,),
        )
