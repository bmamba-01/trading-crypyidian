"""Lưu một replay H03 source-pinned thật qua repository PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.database.models import (
    BacktestDatasetManifestRecord,
    BacktestDatasetReplayBundleRecord,
    BacktestDecisionOpportunityRecord,
    BacktestTradeOutcomeRecord,
    MarketBar,
    MarketInstrument,
)
from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.market_data import MarketDataRepository
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundleReference,
)
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.candidate_universe_proof import CandidateUniverseProofService
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.market.ingestion import MarketRecordVersion, MarketStream
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

sys.path.insert(0, "/e2e-tests")
from fixtures.runtime_replay import build_h03_source_pinned_dataset_replay

WORKSPACE_ID = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])


async def run_probe(*, scorer_seed: bool = False) -> dict[str, object]:
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    bundle, comparison = build_h03_source_pinned_dataset_replay(
        WORKSPACE_ID,
        **(
            {
                "trade_patterns": ("loss", "flat", "profit") * 9,
                "pattern_stride": 10,
                "fee_rate": Decimal("0"),
            }
            if scorer_seed
            else {}
        ),
    )
    if not comparison.is_complete or len(comparison.runs) != 1:
        raise AssertionError("Replay source-pinned phải hoàn thành đúng một trial.")
    replay = comparison.runs[0]
    if (len(replay.opportunities), len(replay.trade_outcomes)) != (
        (270, 27) if scorer_seed else (6, 2)
    ):
        raise AssertionError("Fixture replay không có đủ opportunity và closed trade đã dự kiến.")

    instrument_id = uuid4()
    account_id = uuid4()
    membership_id = uuid4()
    job_id = UUID(replay.sensitivity_run.manifest.learning_job_id or "")
    trial_record_id = None
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO workspace (id,name,slug,mode,status) "
                "VALUES (:id,'WP06 source-pinned replay',:slug,'PAPER','ACTIVE')"
            ),
            {"id": WORKSPACE_ID, "slug": f"wp06-source-pinned-{WORKSPACE_ID.hex}"},
        )
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(WORKSPACE_ID)},
        )
        await session.execute(
            text(
                "INSERT INTO user_account(id,email_normalized,password_hash,status) "
                "VALUES (:id,:email,'fixture-only-not-a-credential','ACTIVE')"
            ),
            {"id": account_id, "email": f"{account_id}@example.invalid"},
        )
        await session.execute(
            text(
                "INSERT INTO user_membership(id,workspace_id,user_id,status) "
                "VALUES (:id,:workspace,:user_id,'ACTIVE')"
            ),
            {"id": membership_id, "workspace": WORKSPACE_ID, "user_id": account_id},
        )
        session.add(
            MarketInstrument(
                id=instrument_id,
                workspace_id=WORKSPACE_ID,
                source="BINANCE_USDM_REST_BACKFILL",
                idempotency_key="wp06-source-pinned-fixture-instrument",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                tick_size="0.1",
                quantity_step="0.001",
                min_notional="5",
                status="TRADING",
                revision=1,
                checksum="a" * 64,
                supersedes_id=None,
                event_at=bundle.components[0].manifest.warmup_start,
                received_at=bundle.components[0].manifest.warmup_start,
                available_at=bundle.components[0].manifest.warmup_start,
            )
        )
        await session.flush()

        market_repository = MarketDataRepository(session)
        identity_ids = set()
        for component in bundle.components:
            for bar in component.bars:
                identity = bar.source_identity
                if identity is None:
                    raise AssertionError("Mỗi bar của bundle phải ghim source identity.")
                identity_ids.add(identity.record_id)
                version = MarketRecordVersion(
                    record_id=identity.record_id,
                    stream=MarketStream(bar.source, bar.symbol, bar.timeframe),
                    logical_key=identity.logical_key,
                    idempotency_key=identity.logical_key,
                    revision=identity.revision,
                    content_checksum=identity.checksum,
                    event_at=bar.event_at,
                    received_at=bar.received_at,
                    available_at=bar.available_at,
                    supersedes_record_id=identity.supersedes_record_id,
                )
                await market_repository.append_bar(
                    workspace_id=WORKSPACE_ID,
                    instrument_id=instrument_id,
                    bar=bar,
                    version=version,
                )
            session.add(
                BacktestDatasetManifestRecord.from_manifest(
                    workspace_id=WORKSPACE_ID, manifest=component.manifest
                )
            )
        bundle_reference = BacktestDatasetReplayBundleReference.from_bundle(bundle)
        session.add(
            BacktestDatasetReplayBundleRecord.from_reference(
                workspace_id=WORKSPACE_ID,
                reference=bundle_reference,
            )
        )
        await session.flush()

        manifest = replay.sensitivity_run.manifest
        request = BacktestReplayJobSubmission(
            bundle_hash=bundle.dataset_hash,
            strategy_release_id=manifest.strategy_release_id,
            execution_interval="1h",
            starting_equity=Decimal("10000"),
            per_trade_risk_fraction=Decimal("0.001"),
            fee_rate=Decimal("0") if scorer_seed else Decimal("0.001"),
            slippage_rate=Decimal("0"),
            seed=manifest.seed,
            learning_intent="TRAINING_BASE",
        )
        request_payload = request.to_payload()
        job = BacktestReplayJobRecord(
            id=job_id,
            workspace_id=WORKSPACE_ID,
            requested_by=membership_id,
            bundle_hash=request.bundle_hash,
            strategy_release_id=request.strategy_release_id,
            execution_interval=request.execution_interval,
            request_payload=request_payload,
            canonical_request=json.dumps(
                request_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            status="RUNNING",
            stage="persist",
            progress=80,
            cancel_requested=False,
            attempt_count=1,
            lease_owner="source-pinned-fixture-worker",
            lease_generation=1,
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
            result_run_ids=[],
        )
        session.add(job)
        await session.flush()

        append_result = await BacktestOutcomeRepository(session).append_sensitivity_run(
            workspace_id=WORKSPACE_ID,
            sensitivity_run=replay.sensitivity_run,
            trade_outcomes=replay.trade_outcomes,
            dataset_bundle=bundle,
            opportunities=replay.opportunities,
        )
        trial_record_id = append_result.trial_record_id
        job.status = "SUCCEEDED"
        job.stage = "completed"
        job.progress = 100
        job.lease_owner = None
        job.lease_until = None
        job.result_run_ids = [str(append_result.run_id)]
        await session.flush()

        bars = tuple(
            (
                await session.scalars(
                    select(MarketBar).where(MarketBar.workspace_id == WORKSPACE_ID)
                )
            ).all()
        )
        opportunity_rows = tuple(
            (
                await session.scalars(
                    select(BacktestDecisionOpportunityRecord).where(
                        BacktestDecisionOpportunityRecord.workspace_id == WORKSPACE_ID
                    )
                )
            ).all()
        )
        outcome_rows = tuple(
            (
                await session.scalars(
                    select(BacktestTradeOutcomeRecord).where(
                        BacktestTradeOutcomeRecord.workspace_id == WORKSPACE_ID
                    )
                )
            ).all()
        )
        bundle_row = await session.scalar(
            select(BacktestDatasetReplayBundleRecord).where(
                BacktestDatasetReplayBundleRecord.workspace_id == WORKSPACE_ID,
                BacktestDatasetReplayBundleRecord.bundle_hash == bundle.dataset_hash,
            )
        )
        if bundle_row is None:
            raise AssertionError("Bundle replay chưa được lưu.")
        stored_bundle = bundle_row.to_reference()
        opportunities_by_event = {row.event_id: row for row in opportunity_rows}
        linked_outcomes = 0
        for outcome in outcome_rows:
            opportunity = opportunities_by_event.get(outcome.entry_opportunity_event_id)
            if opportunity is None:
                raise AssertionError("Outcome không nối tới opportunity đã persist.")
            if (
                opportunity.candidate_id != outcome.entry_candidate_id
                or opportunity.vector_hash != outcome.entry_vector_hash
                or opportunity.runtime_snapshot_id != outcome.entry_decision_snapshot_id
                or opportunity.feature_schema_hash != outcome.entry_feature_schema_hash
            ):
                raise AssertionError("Entry lineage DB không khớp opportunity runtime.")
            linked_outcomes += 1

        actual_ids = {row.id for row in bars}
        if actual_ids != identity_ids:
            raise AssertionError("market_bar identity IDs không khớp child manifest.")

    if trial_record_id is None:
        raise AssertionError("Repository chưa trả trial ID.")
    provider = bundle.components[0].manifest.rights_provider
    now = datetime.now(UTC)
    rights_history = (
        SourceRegistryEntry.discover(
            source_id=provider,
            publisher="Fixture",
            endpoint_url="https://example.invalid/source-pinned-bars",
            access_method="GET",
            auth_scope="synthetic fixture",
            budget=SourceBudget(
                window=timedelta(hours=1),
                max_requests=100,
                max_bytes=1024,
                max_cost=Decimal("0"),
                cost_unit="USD",
            ),
            actor_id="fixture-operator",
            at=now - timedelta(minutes=3),
            reason="Đăng ký nguồn synthetic phục vụ kiểm thử WP06.",
        )
        .transition(
            policy_id=f"{provider}:source-pinned-pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="fixture-operator",
            at=now - timedelta(minutes=2),
            reason="Đang xác minh quyền dùng dữ liệu fixture.",
        )
        .transition(
            policy_id=f"{provider}:source-pinned-training",
            state=SourcePolicyState.VERIFIED,
            actor_id="fixture-operator",
            at=now - timedelta(minutes=1),
            reason="Chỉ cấp TRAINING cho dữ liệu synthetic của kiểm thử.",
            allowed_purposes=frozenset({SourceUsePurpose.TRAINING}),
            evidence_ref="https://example.invalid/source-pinned-training-fixture",
            reviewed_at=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            approved_by="fixture-reviewer",
        )
    )
    for revision in rights_history.revisions:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            await SourceRegistryRepository(uow.session).append_revision(
                workspace_id=WORKSPACE_ID, revision=revision
            )
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            await SourceRegistryRepository(uow.session).finalize_policy(
                workspace_id=WORKSPACE_ID,
                source_id=provider,
                policy_id=revision.policy.policy_id,
            )

    if scorer_seed:
        start = bundle.evaluation_start
        split_bounds = [start + timedelta(hours=offset) for offset in (1, 60, 150, 240)]
        await engine.dispose()
        return {
            "workspace_id": str(WORKSPACE_ID),
            "account_id": str(account_id),
            "membership_id": str(membership_id),
            "trial_record_id": str(trial_record_id),
            "bundle_hash": bundle.dataset_hash,
            "bars": len(identity_ids),
            "linked_outcomes": linked_outcomes,
            "opportunities": len(replay.opportunities),
            "expected_test_inventory": {
                hashlib.sha256(item.event_id.encode()).hexdigest(): {
                    "candidate_id_hash": item.candidate_id,
                    "vector_hash": item.vector_hash if item.candidate_id else None,
                    "status": "candidate" if item.candidate_id else "no_candidate",
                }
                for item in replay.opportunities
                if split_bounds[2] <= item.decision_at < split_bounds[3]
            },
            "config": {
                "holdout_start": split_bounds[-1].isoformat(),
                "evaluation_cutoff": split_bounds[-1].isoformat(),
                "folds": [
                    {
                        role: [split_bounds[index].isoformat(), split_bounds[index + 1].isoformat()]
                        for index, role in enumerate(("train", "validation", "test"))
                    }
                ],
            },
        }

    proof_service = CandidateUniverseProofService(sessions)
    proof = await proof_service.build_decision_opportunity(
        workspace_id=WORKSPACE_ID, trial_record_id=trial_record_id
    )
    retry = await proof_service.build_decision_opportunity(
        workspace_id=WORKSPACE_ID, trial_record_id=trial_record_id
    )
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        receipt_count = await uow.session.scalar(
            text("SELECT count(*) FROM candidate_training_rights WHERE proof_id=:proof_id"),
            {"proof_id": proof.record_id},
        )
    if receipt_count != len(bundle.components):
        raise AssertionError("Proof chưa có receipt hiện hành cho mọi child manifest.")
    await engine.dispose()
    return {
        "bars": len(identity_ids),
        "base_opportunity_complete": proof.proof.completeness_verified,
        "bundle_round_trip": stored_bundle.bundle_hash == bundle.dataset_hash,
        "closed_trades": len(replay.trade_outcomes),
        "linked_outcomes": linked_outcomes,
        "opportunities": proof.proof.opportunity_count,
        "proof_retry_idempotent": retry.duplicate and retry.record_id == proof.record_id,
        "source_ids_exact": True,
        "training_receipts": receipt_count,
    }


if __name__ == "__main__":
    print(
        json.dumps(asyncio.run(run_probe(scorer_seed="--scorer-seed" in sys.argv)), sort_keys=True)
    )
