"""Fixture anchor giả lập cho các E2E chỉ kiểm tra phần sau split approval."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.backtest_entities import (
    BacktestReportRecord,
    BacktestTrialRecord,
)
from trading_bot.adapters.database.models.backtest_outcome_entities import BacktestScenarioRecord
from trading_bot.adapters.database.models.candidate_universe_entities import (
    CandidateUniverseProofRecord,
)
from trading_bot.adapters.database.models.learning_split_candidate_entities import (
    LearningSplitCandidateRecord,
)
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.domain.learning.dataset import DatasetManifest
from trading_bot.domain.learning.evaluation import WalkForwardSplit


async def append_split_approval_anchor(
    session: AsyncSession,
    *,
    manifest: DatasetManifest,
    folds: tuple[WalkForwardSplit, ...],
    evaluation_cutoff: datetime,
    trial_record_id: UUID,
) -> None:
    """Tạo anchor metadata dưới vai trò fixture DB owner.

    Candidate proof E2E kiểm chứng luồng authoritative thật. Các E2E của
    evaluator/trainer dùng anchor tối thiểu để cô lập phần sau approval.
    """
    trial = await session.scalar(
        select(BacktestTrialRecord).where(
            BacktestTrialRecord.workspace_id == manifest.workspace_id,
            BacktestTrialRecord.id == trial_record_id,
        )
    )
    report = await session.scalar(
        select(BacktestReportRecord).where(
            BacktestReportRecord.workspace_id == manifest.workspace_id,
            BacktestReportRecord.trial_record_id == trial_record_id,
        )
    )
    scenario = await session.scalar(
        select(BacktestScenarioRecord).where(
            BacktestScenarioRecord.workspace_id == manifest.workspace_id,
            BacktestScenarioRecord.trial_record_id == trial_record_id,
        )
    )
    if trial is None or report is None or scenario is None:
        raise AssertionError("Fixture cần một trial BASE có report và scenario đã lưu.")

    proof_id = uuid4()
    proof_payload = json.dumps(
        {"fixture": "approval-anchor", "trial_record_id": str(trial_record_id)},
        sort_keys=True,
        separators=(",", ":"),
    )
    proof_hash = hashlib.sha256(proof_payload.encode("utf-8")).hexdigest()
    proof = CandidateUniverseProofRecord(
        id=proof_id,
        workspace_id=manifest.workspace_id,
        run_id=trial.run_id,
        trial_record_id=trial.id,
        report_id=report.id,
        scenario_id=scenario.id,
        query_version="base-closed-trade-universe-v1",
        manifest_hash=trial.manifest_hash,
        proof_hash=proof_hash,
        membership_digest=hashlib.sha256(b"[]").hexdigest(),
        candidate_count=0,
        eligible_count=0,
        exclusion_count=0,
        canonical_proof=proof_payload,
    )
    session.add(proof)
    await session.flush()

    plan = build_manifest_fold_plan(
        manifest,
        folds=folds,
        expected_split_policy_version=manifest.split_policy_version,
        evaluation_cutoff=evaluation_cutoff,
    )
    config = {
        "evaluation_cutoff": evaluation_cutoff.isoformat(),
        "folds": [
            {
                role: [
                    getattr(fold, role).start.isoformat(),
                    getattr(fold, role).end.isoformat(),
                ]
                for role in ("train", "validation", "test")
            }
            for fold in folds
        ],
        "holdout_start": manifest.holdout_sealed_at.isoformat(),
        "split_policy_version": manifest.split_policy_version,
    }
    session.add(
        LearningSplitCandidateRecord(
            id=uuid4(),
            workspace_id=manifest.workspace_id,
            trial_record_id=trial.id,
            proof_record_id=proof.id,
            proof_hash=proof.proof_hash,
            manifest_hash=manifest.content_hash,
            split_policy_version=manifest.split_policy_version,
            fold_plan_hash=plan.content_hash,
            evaluation_cutoff=evaluation_cutoff,
            preview={"fixture": "downstream-approval-anchor"},
            canonical_manifest=manifest.canonical_json,
            canonical_configuration=json.dumps(config, sort_keys=True, separators=(",", ":")),
        )
    )
    await session.flush()
