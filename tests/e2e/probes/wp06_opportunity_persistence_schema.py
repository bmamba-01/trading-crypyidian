"""Smoke PostgreSQL Task2: metadata fixture, không chứng nhận proof/rights/hiệu quả model."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_bot.adapters.database.engine import create_database_engine, database_url_from_secret
from trading_bot.adapters.database.models import Workspace
from trading_bot.adapters.database.models.backtest_opportunity_entities import (
    BacktestDecisionOpportunityRecord,
)
from trading_bot.adapters.database.models.backtest_outcome_entities import (
    BacktestScenarioRecord,
    BacktestTradeOutcomeRecord,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict
from trading_bot.application.backtest import (
    BacktestRunner,
    EvaluationReport,
    ReplayManifest,
    ReplayObservation,
)
from trading_bot.application.backtest_decision_opportunities import ReplayDecisionOpportunity
from trading_bot.application.backtest_sensitivity import SensitivityRun, SensitivityScenario
from trading_bot.application.candidate_universe_proof import CandidateUniverseProof
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.evaluation import EvaluationTrade, calculate_entry_features_hash
from trading_bot.domain.strategy.contracts import DecisionSnapshot

STATE = json.loads(os.environ["WP06_OPPORTUNITY_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
NOW = datetime.fromisoformat(STATE["now"]).astimezone(UTC)
AT = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(days=1)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def case(training: bool, label: str = "base"):
    """Fixture metadata có canonical bytes; phép tính vector thực đã được C.1.1/Task1 kiểm."""
    manifest = ReplayManifest(
        dataset_hash=digest("dataset:" + label),
        range_start=AT,
        range_end=AT + timedelta(hours=4),
        settings_version_id="fixture-settings",
        risk_profile_version_id="fixture-risk",
        strategy_release_id="h01-v1",
        strategy_content_hash=digest("release"),
        feature_schema_hash=digest("schema"),
        fill_model_version="fixture-fill",
        pipeline_content_hash=digest("pipeline"),
        cost_assumptions=(("fee_rate", "0"),),
        parameter_family_hash=digest("family"),
        trial_count=1,
        trial_index=0,
        seed=7,
        learning_intent="TRAINING_BASE" if training else "RESEARCH",
        learning_job_id=str(uuid5(WORKSPACE, "job:" + label)) if training else None,
    )
    event_id = digest("event:" + label)
    snapshot_id = digest("source:" + label)
    runtime_id = digest("runtime:" + label)
    candidate_id = digest("candidate:" + label)
    vector_hash = digest("vector:" + label)
    trade = None
    if training:
        features = (("1h:atr_14", 1.0),)
        trade = EvaluationTrade(
            trade_id="trade:" + label,
            opened_at=AT + timedelta(seconds=1),
            closed_at=AT + timedelta(seconds=2),
            gross_pnl=Decimal("10"),
            fees=Decimal("1"),
            funding=Decimal("0"),
            slippage=Decimal("0"),
            exposure=Decimal("100"),
            entry_decision_snapshot_id=runtime_id,
            entry_decision_at=AT,
            entry_feature_schema_hash=manifest.feature_schema_hash,
            entry_features=features,
            entry_features_hash=calculate_entry_features_hash(
                runtime_id, AT, manifest.feature_schema_hash, features
            ),
            entry_symbol="BTCUSDT",
            entry_opportunity_event_id=event_id,
            entry_candidate_id=candidate_id,
            entry_vector_hash=vector_hash,
        )
    observation = ReplayObservation(
        event_id, AT, AT, DecisionSnapshot("BTCUSDT", AT, snapshot_id, (), ())
    )
    report = BacktestRunner().run((observation,), manifest=manifest, evaluator=lambda _: trade)
    assert isinstance(report, EvaluationReport)
    run = SensitivityRun(SensitivityScenario(label, "BASE"), manifest, report)
    if not training:
        return run, (), ()
    opportunity = ReplayDecisionOpportunity(
        workspace_id=WORKSPACE,
        manifest_hash=manifest.content_hash,
        bundle_hash=manifest.dataset_hash,
        scenario_id=label,
        symbol="BTCUSDT",
        strategy_release_id=manifest.strategy_release_id,
        strategy_content_hash=manifest.strategy_content_hash,
        decision_timeframe="1h",
        evaluation_start=AT,
        range_end=manifest.range_end,
        event_id=event_id,
        decision_ordinal=0,
        event_at=AT,
        available_at=AT,
        decision_at=AT,
        snapshot_id=snapshot_id,
        runtime_snapshot_id=runtime_id,
        feature_snapshot_ids=(("1h", digest("feature1")), ("4h", digest("feature4"))),
        source_event_digests=(("1h", digest("source1")), ("4h", digest("source4"))),
        source_refs_digest=digest(
            json.dumps(
                [
                    {"timeframe": tf, "source_event_digest": digest("source" + number)}
                    for tf, number in (("1h", "1"), ("4h", "4"))
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        feature_schema_hash=manifest.feature_schema_hash,
        vector_hash=vector_hash,
        feature_values_hash=trade.entry_features_hash,
        input_state="EVALUATED",
        strategy_state="CANDIDATE",
        decision_outcome="PAPER_INTENT_CREATED",
        reason_code="FIXTURE_ENTRY",
        candidate_id=candidate_id,
    )
    return run, (trade,), (opportunity,)


async def denied(session, statement, parameters=None, code: str | None = None):
    failure = None
    failure_type = None
    try:
        async with session.begin_nested():
            await session.execute(statement, parameters or {})
    except DBAPIError as error:
        failure = str(error.orig)
        failure_type = type(error.orig).__name__
    if failure is None:
        return False
    if code is not None:
        assert code in failure, f"{failure_type}: {failure}"
    return True


async def seed_legacy(sessions):
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        uow.session.add(
            Workspace(
                id=WORKSPACE,
                name="Opportunity schema fixture",
                slug="opportunity-" + WORKSPACE.hex,
                mode="PAPER",
                status="ACTIVE",
            )
        )
        await uow.session.flush()
        run, trades, _ = case(False)
        result = await BacktestOutcomeRepository(uow.session).append_sensitivity_run(
            workspace_id=WORKSPACE, sensitivity_run=run, trade_outcomes=trades
        )
        scenario = await uow.session.scalar(
            select(BacktestScenarioRecord).where(
                BacktestScenarioRecord.trial_record_id == result.trial_record_id
            )
        )
        proof = CandidateUniverseProof(
            WORKSPACE,
            result.run_id,
            result.trial_record_id,
            result.report_id,
            scenario.id,
            run.manifest.content_hash,
            digest("trial"),
            run.result.report_hash,
            scenario.scenario_hash,
            result.outcomes_hash,
            digest("members"),
            digest("rights"),
            0,
            0,
            0,
            (),
        )
        proof_id = uuid4()
        await uow.session.execute(
            text(
                "INSERT INTO candidate_universe_proof (id,workspace_id,run_id,trial_rec"
                "ord_id,report_id,scenario_id,query_version,manifest_hash,proof_hash,me"
                "mbership_digest,candidate_count,eligible_count,exclusion_count,canonic"
                "al_proof) VALUES (:id,:ws,:run,:trial,:report,:scenario,:query,:manife"
                "st,:hash,:members,0,0,0,:canonical)"
            ),
            {
                "id": proof_id,
                "ws": WORKSPACE,
                "run": result.run_id,
                "trial": result.trial_record_id,
                "report": result.report_id,
                "scenario": scenario.id,
                "query": proof.query_version,
                "manifest": proof.manifest_hash,
                "hash": proof.content_hash,
                "members": proof.membership_digest,
                "canonical": proof.canonical_json,
            },
        )
        assert (
            await uow.session.scalar(
                text("SELECT to_regclass('public.backtest_decision_opportunity')")
            )
            is None
        )
    return {"legacy_proof_id": str(proof_id), "legacy_proof_hash": proof.content_hash}


async def persist(sessions):
    run, trades, opportunities = case(True)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        role = await uow.session.scalar(text("SELECT current_user"))
        assert role == "crypidian_replay_writer"
        repository = BacktestOutcomeRepository(uow.session)
        first = await repository.append_sensitivity_run(
            workspace_id=WORKSPACE,
            sensitivity_run=run,
            trade_outcomes=trades,
            opportunities=opportunities,
        )
        retry = await repository.append_sensitivity_run(
            workspace_id=WORKSPACE,
            sensitivity_run=run,
            trade_outcomes=trades,
            opportunities=opportunities,
        )
        assert retry.duplicate
        assert first.outcomes_hash == retry.outcomes_hash
        loaded = await repository.load_trade_outcomes(
            workspace_id=WORKSPACE, trial_record_id=first.trial_record_id
        )
        assert loaded.trades == trades
        try:
            await repository.append_sensitivity_run(
                workspace_id=WORKSPACE,
                sensitivity_run=run,
                trade_outcomes=trades,
                opportunities=(replace(opportunities[0], reason_code="CHANGED_REASON"),),
            )
        except BacktestRepositoryConflict:
            conflict = True
        else:
            raise AssertionError("Retry khác payload phải conflict.")
    rollback_run, rollback_trades, rollback_opportunities = case(True, "rollback")
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
            await BacktestOutcomeRepository(uow.session).append_sensitivity_run(
                workspace_id=WORKSPACE,
                sensitivity_run=rollback_run,
                trade_outcomes=rollback_trades,
                opportunities=rollback_opportunities,
            )
            raise RuntimeError("rollback-fixture")
    except RuntimeError as error:
        rollback_message = str(error)
    assert rollback_message == "rollback-fixture"
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        count = await uow.session.scalar(
            select(func.count(BacktestDecisionOpportunityRecord.canonical_opportunity)).where(
                BacktestDecisionOpportunityRecord.workspace_id == WORKSPACE
            )
        )
        assert count == 1
        assert await denied(
            uow.session, text("SELECT feature_schema_hash FROM backtest_decision_opportunity")
        )
    return {
        "writer_login": role,
        "retry_idempotent": True,
        "changed_event_conflict": conflict,
        "transaction_rollback": True,
        "trial_id": str(first.trial_record_id),
        "run_id": str(first.run_id),
        "report_id": str(first.report_id),
    }


async def verify(sessions):
    results = {}
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as uow:
        session = uow.session
        legacy = (
            await session.execute(
                text(
                    "SELECT canonical_proof,opportunity_count,no_candidate_count,not_run_co"
                    "unt,input_excluded_count,unlabeled_candidate_count,outcome_link_count,"
                    "source_scope_hash,completeness_verified FROM candidate_universe_proof "
                    "WHERE id=:id"
                ),
                {"id": UUID(STATE["legacy_proof_id"])},
            )
        ).one()
        assert digest(legacy[0]) == STATE["legacy_proof_hash"]
        assert all(value is None for value in legacy[1:])
        results["legacy_v1_bytes_and_nullability_preserved"] = True
        rls = (
            await session.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='pub"
                    "lic.backtest_decision_opportunity'::regclass"
                )
            )
        ).one()
        assert rls == (True, True)
        results["force_rls"] = True
        for role in (
            "crypidian_trainer_worker",
            "crypidian_admin_web",
            "crypidian_trading_runtime",
            "crypidian_app",
        ):
            assert not await session.scalar(
                text(
                    "SELECT has_table_privilege(:role,'backtest_decision_opportunity','SELE"
                    "CT,INSERT,UPDATE,DELETE')"
                ),
                {"role": role},
            )
        assert not await session.scalar(
            text(
                "SELECT has_table_privilege('crypidian_replay_writer','backtest_decisio"
                "n_opportunity','SELECT')"
            )
        )
        assert await session.scalar(
            text(
                "SELECT has_column_privilege('crypidian_replay_writer','backtest_decisi"
                "on_opportunity','canonical_opportunity','SELECT')"
            )
        )
        assert not await session.scalar(
            text(
                "SELECT has_column_privilege('crypidian_replay_writer','backtest_decisi"
                "on_opportunity','vector_hash','SELECT')"
            )
        )
        assert await session.scalar(
            text(
                "SELECT has_table_privilege('crypidian_replay_writer','backtest_decisio"
                "n_opportunity','INSERT')"
            )
        )
        assert not await session.scalar(
            text(
                "SELECT has_table_privilege('crypidian_replay_writer','backtest_decisio"
                "n_opportunity','UPDATE,DELETE')"
            )
        )
        assert await session.scalar(
            text(
                "SELECT has_table_privilege('crypidian_candidate_builder','backtest_dec"
                "ision_opportunity','SELECT')"
            )
        )
        assert not await session.scalar(
            text(
                "SELECT has_table_privilege('crypidian_candidate_builder','backtest_dec"
                "ision_opportunity','INSERT,UPDATE,DELETE')"
            )
        )
        results["minimal_column_grants_and_role_matrix"] = True
        async with session.begin_nested():
            await session.execute(text("SET LOCAL ROLE crypidian_candidate_builder"))
            assert (
                await session.scalar(text("SELECT count(*) FROM backtest_decision_opportunity"))
                == 1
            )
            await session.execute(
                text("SELECT set_config('app.workspace_id',:ws,true)"),
                {"ws": str(uuid5(WORKSPACE, "other"))},
            )
            assert (
                await session.scalar(text("SELECT count(*) FROM backtest_decision_opportunity"))
                == 0
            )
            await session.execute(text("SELECT set_config('app.workspace_id','',true)"))
            assert (
                await session.scalar(text("SELECT count(*) FROM backtest_decision_opportunity"))
                == 0
            )
            await session.execute(text("RESET ROLE"))
            await session.execute(
                text("SELECT set_config('app.workspace_id',:ws,true)"), {"ws": str(WORKSPACE)}
            )
        results["workspace_and_missing_scope_fail_closed"] = True
        assert await denied(
            session,
            text("UPDATE backtest_decision_opportunity SET reason_code='MUTATED'"),
            code="WP06_OPPORTUNITY_IMMUTABLE",
        )
        assert await denied(
            session,
            text("DELETE FROM backtest_decision_opportunity"),
            code="WP06_OPPORTUNITY_IMMUTABLE",
        )
        results["append_only"] = True
        opportunity = await session.scalar(
            select(BacktestDecisionOpportunityRecord).where(
                BacktestDecisionOpportunityRecord.trial_record_id == UUID(STATE["trial_id"])
            )
        )
        outcome = await session.scalar(
            select(BacktestTradeOutcomeRecord).where(
                BacktestTradeOutcomeRecord.trial_record_id == UUID(STATE["trial_id"])
            )
        )
        assert opportunity is not None
        assert outcome is not None
        source = {
            column.name: getattr(opportunity, column.name)
            for column in opportunity.__table__.columns
            if column.name not in {"id", "created_at"}
        }
        # BEFORE INSERT phải chặn canonical/scope trước khi unique index được xét.
        assert await denied(
            session,
            BacktestDecisionOpportunityRecord.__table__.insert().values(
                **(source | {"id": uuid4(), "content_hash": "0" * 64})
            ),
            code="WP06_OPPORTUNITY_CANONICAL_MISMATCH",
        )
        assert await denied(
            session,
            BacktestDecisionOpportunityRecord.__table__.insert().values(
                **(source | {"id": uuid4(), "run_id": uuid4()})
            ),
            code="WP06_OPPORTUNITY_SCOPE_MISMATCH",
        )
        results["canonical_and_parent_scope_guards"] = True
        bad_ids = [["1h", "raw-feature-values-are-not-an-id"]]
        bad_document = json.loads(opportunity.canonical_opportunity)
        bad_document["feature_snapshot_ids"] = bad_ids
        bad_canonical = json.dumps(
            bad_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert await denied(
            session,
            BacktestDecisionOpportunityRecord.__table__.insert().values(
                **(
                    source
                    | {
                        "id": uuid4(),
                        "feature_snapshot_ids": bad_ids,
                        "canonical_opportunity": bad_canonical,
                        "content_hash": digest(bad_canonical),
                    }
                )
            ),
            code="WP06_OPPORTUNITY_FEATURE_IDS_INVALID",
        )
        results["feature_snapshot_metadata_ids_only"] = True
        original = {
            column.name: getattr(outcome, column.name)
            for column in outcome.__table__.columns
            if column.name not in {"id", "created_at"}
        }
        for field, value in (
            ("entry_candidate_id", "0" * 64),
            ("entry_vector_hash", "0" * 64),
            ("entry_decision_snapshot_id", "0" * 64),
            ("entry_feature_schema_hash", "0" * 64),
            ("entry_symbol", "ETHUSDT"),
            ("entry_decision_at", AT + timedelta(seconds=1)),
            ("trial_record_id", uuid4()),
        ):
            assert await denied(
                session,
                BacktestTradeOutcomeRecord.__table__.insert().values(
                    **(
                        original
                        | {"id": uuid4(), "ordinal": 1, "trade_id": "wrong:" + field, field: value}
                    )
                ),
                code="WP06_OUTCOME_OPPORTUNITY_MISMATCH",
            )
        results["exact_entry_candidate_snapshot_symbol_schema_vector_and_trial"] = True
        noncanonical = " " + outcome.canonical_json
        noncanonical_values = {
            column.name: getattr(outcome, column.name)
            for column in outcome.__table__.columns
            if column.name not in {"id", "created_at"}
        }
        assert await denied(
            session,
            BacktestTradeOutcomeRecord.__table__.insert().values(
                **(
                    noncanonical_values
                    | {
                        "id": uuid4(),
                        "ordinal": outcome.ordinal + 1,
                        "trade_id": "noncanonical-json",
                        "canonical_json": noncanonical,
                        "outcome_hash": digest(noncanonical),
                    }
                )
            ),
            code="WP06_OUTCOME_ENTRY_CANONICAL_MISMATCH",
        )
        integer_features_payload = json.loads(json.dumps(outcome.outcome))
        integer_features = integer_features_payload["entry_features"]
        integer_features[0][1] = int(integer_features[0][1])
        integer_features_canonical = json.dumps(
            integer_features_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert await denied(
            session,
            BacktestTradeOutcomeRecord.__table__.insert().values(
                **(
                    noncanonical_values
                    | {
                        "id": uuid4(),
                        "ordinal": outcome.ordinal + 1,
                        "trade_id": "integer-feature-json",
                        "outcome": integer_features_payload,
                        "canonical_json": integer_features_canonical,
                        "outcome_hash": digest(integer_features_canonical),
                        "entry_features": integer_features,
                    }
                )
            ),
            code="WP06_OUTCOME_ENTRY_CANONICAL_MISMATCH",
        )
        original_trade = case(True)[1][0]
        changed_features = tuple((key, value + 1.0) for key, value in original_trade.entry_features)
        changed_trade = replace(
            original_trade,
            trade_id="changed-features",
            entry_features=changed_features,
            entry_features_hash=calculate_entry_features_hash(
                original_trade.entry_decision_snapshot_id,
                original_trade.entry_decision_at,
                original_trade.entry_feature_schema_hash,
                changed_features,
            ),
        )
        from trading_bot.adapters.database.repositories.backtest_outcomes import _trade_payload

        changed_payload = _trade_payload(changed_trade)
        changed_canonical = json.dumps(
            changed_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        changed_values = noncanonical_values | {
            "id": uuid4(),
            "ordinal": outcome.ordinal + 1,
            "trade_id": changed_trade.trade_id,
            "outcome": changed_payload,
            "canonical_json": changed_canonical,
            "outcome_hash": digest(changed_canonical),
            "entry_features": [list(item) for item in changed_features],
            "entry_features_hash": changed_trade.entry_features_hash,
        }
        assert await denied(
            session,
            BacktestTradeOutcomeRecord.__table__.insert().values(**changed_values),
            code="WP06_OUTCOME_ENTRY_FEATURE_VALUES_MISMATCH",
        )
        stale_hash_payload = _trade_payload(original_trade)
        stale_hash_payload["trade_id"] = "changed-features-stale-hash"
        stale_hash_payload["entry_features"] = [list(item) for item in changed_features]
        stale_hash_canonical = json.dumps(
            stale_hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        stale_hash_values = noncanonical_values | {
            "id": uuid4(),
            "ordinal": outcome.ordinal + 1,
            "trade_id": stale_hash_payload["trade_id"],
            "outcome": stale_hash_payload,
            "canonical_json": stale_hash_canonical,
            "outcome_hash": digest(stale_hash_canonical),
            "entry_features": [list(item) for item in changed_features],
        }
        assert await denied(
            session,
            BacktestTradeOutcomeRecord.__table__.insert().values(**stale_hash_values),
            code="WP06_OUTCOME_ENTRY_FEATURES_HASH_MISMATCH",
        )
        results["canonical_bytes_and_feature_values_bind_to_opportunity"] = True
        unlinked_payload = dict(original["outcome"])
        for key in (
            "entry_opportunity_event_id",
            "entry_candidate_id",
            "entry_vector_hash",
        ):
            unlinked_payload.pop(key)
        unlinked_canonical = json.dumps(
            unlinked_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert await denied(
            session,
            BacktestTradeOutcomeRecord.__table__.insert().values(
                **(
                    original
                    | {
                        "id": uuid4(),
                        "ordinal": 1,
                        "trade_id": "unlinked",
                        "outcome": unlinked_payload,
                        "canonical_json": unlinked_canonical,
                        "outcome_hash": digest(unlinked_canonical),
                        "entry_opportunity_event_id": None,
                        "entry_candidate_id": None,
                        "entry_vector_hash": None,
                    }
                )
            ),
            code="WP06_OUTCOME_ENTRY_LINK_REQUIRED",
        )
        results["v2_trial_requires_entry_links"] = True
        scenario = await session.scalar(
            select(BacktestScenarioRecord).where(
                BacktestScenarioRecord.trial_record_id == UUID(STATE["trial_id"])
            )
        )
        proof_values = {
            "id": uuid4(),
            "ws": WORKSPACE,
            "run": UUID(STATE["run_id"]),
            "trial": UUID(STATE["trial_id"]),
            "report": UUID(STATE["report_id"]),
            "scenario": scenario.id,
            "hash": digest("v2-proof"),
            "manifest": opportunity.manifest_hash,
        }
        insert = text(
            "INSERT INTO candidate_universe_proof (id,workspace_id,run_id,trial_rec"
            "ord_id,report_id,scenario_id,query_version,manifest_hash,proof_hash,me"
            "mbership_digest,candidate_count,eligible_count,exclusion_count,canonic"
            "al_proof,opportunity_count,no_candidate_count,not_run_count,input_excl"
            "uded_count,unlabeled_candidate_count,outcome_link_count,source_scope_h"
            "ash,completeness_verified) VALUES (:id,:ws,:run,:trial,:report,:scenar"
            "io,'base-decision-opportunity-universe-v1',:manifest,:hash,:hash,1,0,1"
            ",'{}',1,0,0,:excluded,0,1,:hash,false)"
        )
        assert await denied(session, insert, proof_values | {"excluded": 1})
        assert await denied(session, insert, proof_values | {"excluded": None})
        await session.execute(insert, proof_values | {"excluded": 0})
        results["proof_v2_count_equations_and_required_fields"] = True
    return results


async def main():
    writer = STATE["mode"] == "persist"
    username = (
        os.environ["TRADING_BOT_BACKTEST_REPLAY_DATABASE_USER"]
        if writer
        else os.environ["TRADING_BOT_DATABASE_USER"]
    )
    password = (
        os.environ["TRADING_BOT_BACKTEST_REPLAY_DATABASE_PASSWORD_FILE"]
        if writer
        else os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]
    )
    engine = create_database_engine(
        database_url_from_secret(
            username=username, password_file=Path(password), host="postgres", database="crypidian"
        )
    )
    engine.sync_engine.hide_parameters = True
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if STATE["mode"] == "seed_legacy":
            return await seed_legacy(sessions)
        if STATE["mode"] == "persist":
            return await persist(sessions)
        return await verify(sessions)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), sort_keys=True))
