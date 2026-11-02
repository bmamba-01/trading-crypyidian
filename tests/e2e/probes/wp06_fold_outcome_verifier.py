"""Probe Compose kiểm tra bộ xác minh outcome fold WP06.4."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.database.learning_fold_outcome_verifier import (
    DatabaseFoldOutcomeVerifier,
    FoldOutcomeVerificationError,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.dataset_manifest import DatasetManifestRepository
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.learning_fold_evaluation import (
    FoldOutcomeExpectation,
    FoldOutcomeVerificationRequest,
    build_walk_forward_evaluation_report,
)
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit
from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit

STATE = json.loads(os.environ.get("WP06_STATE", "{}"))
WORKSPACE_ID = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])


def _engine():
    url = database_url_from_secret(
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    return create_database_engine(url)


async def _seed() -> dict[str, object]:
    engine = _engine()
    sessions = create_session_factory(engine)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO workspace (id, name, slug, mode, status) "
                "VALUES (:id, 'WP06 verifier E2E', :slug, 'PAPER', 'ACTIVE')"
            ),
            {"id": str(WORKSPACE_ID), "slug": f"wp06-fold-{str(WORKSPACE_ID)[:8]}"},
        )
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(WORKSPACE_ID)},
        )
        sys.path.insert(0, "/e2e-tests")
        from fixtures.runtime_replay import build_h03_runtime_sensitivity

        persisted_rows = []
        for index in range(4):
            start_at = datetime(2026, 10, 20, tzinfo=UTC) + timedelta(days=2 * index)
            replay = build_h03_runtime_sensitivity(
                WORKSPACE_ID,
                (SensitivityScenario(scenario_id=f"base-{index}", kind="BASE"),),
                start_at=start_at,
            )
            if not replay.is_complete or len(replay.runs) != 1:
                raise AssertionError("PAPER replay fixture chưa hoàn tất đúng một trial.")
            run = replay.runs[0]
            receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
                workspace_id=WORKSPACE_ID,
                sensitivity_run=run.sensitivity_run,
                trade_outcomes=tuple(
                    replace(trade, trade_id=f"fold-fixture-{index}-{trade.trade_id}")
                    for trade in run.trade_outcomes
                ),
            )
            persisted = await BacktestOutcomeRepository(session).load_trade_outcome_rows(
                workspace_id=WORKSPACE_ID,
                trial_record_id=receipt.trial_record_id,
            )
            if len(persisted.rows) != 1:
                raise AssertionError("Mỗi PAPER trial fixture phải có đúng một trade.")
            persisted_rows.append(persisted.rows[0])

        samples = []
        roles = (
            ("train-sample", DatasetSplit.TRAIN),
            ("validation-sample", DatasetSplit.VALIDATION),
            ("test-sample", DatasetSplit.TEST),
            ("sealed-holdout-sample", DatasetSplit.HOLDOUT),
        )
        for index, (sample_id, split) in enumerate(roles):
            outcome = persisted_rows[index]
            decision = outcome.trade.opened_at - timedelta(hours=1)
            samples.append(
                DatasetSample(
                    sample_id=sample_id,
                    decision_at=decision,
                    feature_available_at=decision - timedelta(minutes=1),
                    outcome_start=outcome.trade.opened_at,
                    outcome_end=outcome.trade.closed_at,
                    feature_snapshot_hash=hex(index + 2)[2:] * 64,
                    outcome_id=str(outcome.record_id),
                    outcome_hash=outcome.outcome_hash,
                    label_hash=hex(index + 6)[2:] * 64,
                    label_value=1 if index < 3 else -1,
                    label_version="e2e-label-v1",
                    split=split,
                    source_dependent=False,
                    source_rights_hashes=(),
                    source_revision_refs=(),
                )
            )
        data_start = samples[0].decision_at - timedelta(hours=1)
        data_end = persisted_rows[-1].trade.closed_at + timedelta(hours=1)
        manifest = DatasetManifest(
            workspace_id=WORKSPACE_ID,
            query_hash="1" * 64,
            symbols=("BTCUSDT",),
            data_start=data_start,
            data_end=data_end,
            as_of=data_end,
            feature_schema_version="e2e-feature-schema-v1",
            feature_version="e2e-feature-v1",
            label_version="e2e-label-v1",
            sample_schema_version="e2e-sample-v1",
            split_policy_version="e2e-split-v1",
            samples=tuple(samples),
            exclusions=(),
            holdout_sealed_at=data_end,
            retention_policy_ref="e2e-retention-v1",
        )
        await DatasetManifestRepository(session).append(
            workspace_id=WORKSPACE_ID,
            manifest=manifest,
        )
    await engine.dispose()
    return {"manifest_hash": manifest.content_hash, "workspace_id": str(WORKSPACE_ID)}


def _plan(manifest: DatasetManifest):
    train = next(item for item in manifest.samples if item.sample_id == "train-sample")
    validation = next(item for item in manifest.samples if item.sample_id == "validation-sample")
    test = next(item for item in manifest.samples if item.sample_id == "test-sample")
    split = WalkForwardSplit(
        train=TemporalWindow(
            train.decision_at - timedelta(hours=1), train.decision_at + timedelta(hours=1)
        ),
        validation=TemporalWindow(
            validation.decision_at - timedelta(hours=1),
            validation.decision_at + timedelta(hours=1),
        ),
        test=TemporalWindow(
            test.decision_at - timedelta(hours=1), test.decision_at + timedelta(hours=1)
        ),
    )
    return build_manifest_fold_plan(
        manifest,
        folds=(split,),
        expected_split_policy_version=manifest.split_policy_version,
        evaluation_cutoff=manifest.as_of,
    )


async def _verify() -> dict[str, object]:
    engine = _engine()
    sessions = create_session_factory(engine)
    workspace_id = UUID(STATE["workspace_id"])
    async with UnitOfWork(
        sessions,
        workspace_id=workspace_id,
        snapshot_read_only=True,
    ) as uow:
        manifest = await DatasetManifestRepository(uow.session).get_by_hash(
            workspace_id=workspace_id,
            manifest_hash=STATE["manifest_hash"],
        )
        if manifest is None:
            raise AssertionError("Trainer worker không thấy manifest trong workspace.")
        plan = _plan(manifest)
        report = await build_walk_forward_evaluation_report(
            manifest,
            plan,
            verifier=DatabaseFoldOutcomeVerifier(uow.session),
            seed=19,
        )

        selected_fold = plan.folds[0]
        train = next(item for item in manifest.samples if item.sample_id == "train-sample")
        expectation = FoldOutcomeExpectation(
            sample_id=train.sample_id,
            role="train",
            outcome_id=train.outcome_id,
            outcome_hash=train.outcome_hash,
            outcome_start=train.outcome_start,
            outcome_end=train.outcome_end,
        )
        mismatched_workspace_rejected = False
        try:
            await DatabaseFoldOutcomeVerifier(uow.session).load_verified_fold_outcomes(
                FoldOutcomeVerificationRequest(
                    workspace_id=uuid4(),
                    manifest_hash=manifest.content_hash,
                    split_policy_version=manifest.split_policy_version,
                    fold_plan=plan,
                    fold_plan_hash=plan.content_hash,
                    fold_id=selected_fold.fold_id,
                    expected_outcomes=(expectation,),
                )
            )
        except FoldOutcomeVerificationError:
            mismatched_workspace_rejected = True

    read_only_required = False
    async with UnitOfWork(
        sessions,
        workspace_id=workspace_id,
        snapshot_read_only=False,
    ) as uow:
        try:
            await build_walk_forward_evaluation_report(
                manifest,
                plan,
                verifier=DatabaseFoldOutcomeVerifier(uow.session),
                seed=19,
            )
        except ValueError as error:
            read_only_required = "verifier thất bại" in str(error).lower()
    await engine.dispose()
    evaluated_fold = report.fold_evaluations[0]
    return {
        "manifest_hash": report.manifest_hash,
        "report_hash": report.report_hash,
        "train_sample_ids": list(evaluated_fold.train.sample_ids),
        "validation_sample_ids": list(evaluated_fold.validation.sample_ids),
        "test_sample_ids": list(evaluated_fold.test.sample_ids),
        "train_trade_count": evaluated_fold.train.metrics.trade_count,
        "train_net_pnl": str(evaluated_fold.train.metrics.net_pnl),
        "validation_trade_count": evaluated_fold.validation.metrics.trade_count,
        "test_trade_count": evaluated_fold.test.metrics.trade_count,
        "candidate_completeness_verified": report.candidate_completeness_verified,
        "holdout_payload_in_report": "sealed-holdout-sample" in str(report.fold_evaluations),
        "mismatched_workspace_rejected": mismatched_workspace_rejected,
        "read_only_snapshot_required": read_only_required,
    }


async def main() -> None:
    phase = sys.argv[1]
    result = await (_seed() if phase == "seed" else _verify())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
