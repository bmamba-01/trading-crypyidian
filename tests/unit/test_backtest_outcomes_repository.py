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
        return BacktestAppendResult(RUN_ID, TRIAL_RECORD_ID, REPORT_ID, False)

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
