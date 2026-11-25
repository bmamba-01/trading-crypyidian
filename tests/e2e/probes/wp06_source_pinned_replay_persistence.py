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
from types import SimpleNamespace
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
from trading_bot.application.binance_rest_backfill import BackfillRightsPurpose, RestBackfillRequest
from trading_bot.application.binance_rest_backfill_rights import SourceRegistryBackfillRightsGate
from trading_bot.application.candidate_universe_proof import CandidateUniverseProofService
from trading_bot.application.first_party_qa_dataset import (
    CRYPIDIAN_FIRST_PARTY_QA,
    CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF,
    CRYPIDIAN_FIRST_PARTY_QA_SOURCE,
    FirstPartyQADatasetRequest,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.market.ingestion import MarketRecordVersion, MarketStream
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

sys.path.insert(0, "/e2e-tests")
from e2e.probes.source_license_fixture import (
    append_fixture_license_evidence,
    consume_rights_events,
    fixture_license_scope,
    pending_license_kwargs,
    verified_license_kwargs,
)
from fixtures.runtime_replay import build_h03_source_pinned_dataset_replay

WORKSPACE_ID = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
BINANCE_PROVIDER = "BINANCE_USDM_REST"
# Nguồn non-QA chỉ TRAINING (không PAPER_DEMO) để E2E chạm gate PAPER của deployment.
FIXTURE_SOURCES = {
    CRYPIDIAN_FIRST_PARTY_QA: SimpleNamespace(
        market_source=CRYPIDIAN_FIRST_PARTY_QA_SOURCE,
        evidence_ref=CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF,
        request_type=FirstPartyQADatasetRequest,
        publisher="Crypidian Repository QA Fixture",
        auth_scope="repo-owned synthetic QA data; no exchange account",
    ),
    BINANCE_PROVIDER: SimpleNamespace(
        market_source="BINANCE_USDM_REST_BACKFILL",
        evidence_ref="https://example.test/wp06-binance-training-only-fixture-terms",
        request_type=RestBackfillRequest,
        publisher="Fixture Binance USDⓈ-M synthetic trong Compose tạm",
        auth_scope="synthetic fixture bars; no exchange account",
    ),
}


async def run_probe(
    *,
    scorer_seed: bool = False,
    license_fixture: bool = True,
    provider: str = CRYPIDIAN_FIRST_PARTY_QA,
) -> dict[str, object]:
    """Seed replay QA source-pinned.

    `license_fixture=True` (mặc định, chạy trực tiếp): pending/VERIFIED mang license scope
    + evidence tổng hợp từ `source_license_fixture` và consumer rights chạy hết event, đúng
    điều kiện của gate TRAINING exact-scope. Probe khác cần scope riêng (ví dụ
    `wp06_source_license_scope.py`) truyền `False` rồi tự vá transition/evidence.
    `provider=BINANCE_USDM_REST` dựng cùng replay dưới nguồn non-QA, policy chỉ
    BACKTEST_RESEARCH + TRAINING (không PAPER_DEMO).
    """
    fixture_source = FIXTURE_SOURCES[provider]
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    trade_patterns = ("loss", "flat", "profit") * 9 if scorer_seed else ("loss", "loss")
    pattern_stride = 10 if scorer_seed else 3
    bar_count = 620 + pattern_stride * len(trade_patterns)
    dataset_start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=bar_count
    )
    range_end = dataset_start + timedelta(hours=bar_count)
    qa_request = fixture_source.request_type(
        symbol="BTCUSDT",
        interval="1h",
        start=dataset_start,
        end=range_end,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    instrument_id = uuid4()
    account_id = uuid4()
    membership_id = uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO workspace (id,name,slug,mode,status) "
                "VALUES (:id,'WP06 source-pinned QA replay',:slug,'PAPER','ACTIVE')"
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
                source=fixture_source.market_source,
                idempotency_key="wp06-source-pinned-qa-fixture-instrument",
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
                event_at=dataset_start,
                received_at=dataset_start,
                available_at=dataset_start,
            )
        )
    policy_now = datetime.now(UTC)
    qa_scope = fixture_license_scope(provider)
    rights_history = (
        SourceRegistryEntry.discover(
            source_id=provider,
            publisher=fixture_source.publisher,
            endpoint_url=fixture_source.evidence_ref,
            access_method="GET",
            auth_scope=fixture_source.auth_scope,
            budget=SourceBudget(
                window=timedelta(hours=1),
                max_requests=100,
                max_bytes=1024,
                max_cost=Decimal("0"),
                cost_unit="USD",
            ),
            actor_id="fixture-operator",
            at=policy_now - timedelta(minutes=3),
            reason="Đăng ký fixture synthetic first-party trong workspace Compose tạm.",
        )
        .transition(
            policy_id=f"{provider}:source-pinned-pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="fixture-operator",
            at=policy_now - timedelta(minutes=2),
            reason="Ghi nhận nguồn QA đang chờ review trong test workspace.",
            **(
                pending_license_kwargs(qa_scope, fixture_source.evidence_ref)
                if license_fixture
                else {}
            ),
        )
        .transition(
            policy_id=f"{provider}:source-pinned-training",
            state=SourcePolicyState.VERIFIED,
            actor_id="fixture-operator",
            at=policy_now - timedelta(minutes=1),
            reason="Chỉ cấp mục đích nghiên cứu và TRAINING cho fixture first-party.",
            allowed_purposes=frozenset(
                {SourceUsePurpose.BACKTEST_RESEARCH, SourceUsePurpose.TRAINING}
            ),
            reviewed_at=policy_now - timedelta(minutes=1),
            valid_until=policy_now + timedelta(days=7),
            approved_by="fixture-reviewer",
            **(
                verified_license_kwargs(qa_scope, fixture_source.evidence_ref, uuid4())
                if license_fixture
                else {"evidence_ref": fixture_source.evidence_ref}
            ),
        )
    )
    for revision in rights_history.revisions:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            if license_fixture:
                # VERIFIED chỉ ghi được khi pending trước đó đã có dòng evidence khớp.
                await append_fixture_license_evidence(
                    uow.session, workspace_id=WORKSPACE_ID, revision=revision
                )
            await SourceRegistryRepository(uow.session).append_revision(
                workspace_id=WORKSPACE_ID, revision=revision
            )
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
            await SourceRegistryRepository(uow.session).finalize_policy(
                workspace_id=WORKSPACE_ID,
                source_id=provider,
                policy_id=revision.policy.policy_id,
            )
    if license_fixture:
        # Mỗi revision policy phát rights event; gate TRAINING đòi receipt của consumer.
        await consume_rights_events(sessions, workspace_id=WORKSPACE_ID)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as uow:
        qa_rights = await SourceRegistryBackfillRightsGate(
            workspace_id=WORKSPACE_ID,
            repository=SourceRegistryRepository(uow.session),
            source_id=provider,
        ).authorize(request=qa_request, now=datetime.now(UTC))
    bundle, comparison = build_h03_source_pinned_dataset_replay(
        WORKSPACE_ID,
        trade_patterns=trade_patterns,
        pattern_stride=pattern_stride,
        fee_rate=Decimal("0") if scorer_seed else Decimal("0.001"),
        rights_evidence=qa_rights,
        provider=provider,
        dataset_start=dataset_start,
    )
    if not comparison.is_complete or len(comparison.runs) != 1:
        raise AssertionError("Replay source-pinned phải hoàn thành đúng một trial.")
    replay = comparison.runs[0]
    if (len(replay.opportunities), len(replay.trade_outcomes)) != (
        (270, 27) if scorer_seed else (6, 2)
    ):
        raise AssertionError("Fixture replay không có đủ opportunity và closed trade đã dự kiến.")

    job_id = UUID(replay.sensitivity_run.manifest.learning_job_id or "")
    trial_record_id = None
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(WORKSPACE_ID)},
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
    if scorer_seed:
        start = bundle.evaluation_start
        split_bounds = [start + timedelta(hours=offset) for offset in (1, 60, 150, 240)]
        test_candidate_count = sum(
            item.candidate_id is not None
            for item in replay.opportunities
            if split_bounds[2] <= item.decision_at < split_bounds[3]
        )
        if test_candidate_count != 9:
            raise AssertionError("QA TEST fold phải có đúng 9 outcome source-linked.")
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
            "qa_provider": provider,
            "test_candidate_count": test_candidate_count,
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

    await engine.dispose()
    # Gate TRAINING exact-scope (b7d9f1a3c5e8) chỉ chấp nhận session_user
    # `crypidian_candidate_builder`, nên proof được dựng ở lần chạy riêng `--proof`.
    return {
        "bars": len(identity_ids),
        "bundle_round_trip": stored_bundle.bundle_hash == bundle.dataset_hash,
        "child_manifests": len(bundle.components),
        "closed_trades": len(replay.trade_outcomes),
        "linked_outcomes": linked_outcomes,
        "source_ids_exact": True,
        "trial_record_id": str(trial_record_id),
    }


async def build_proof(*, trial_record_id: UUID, child_manifests: int) -> dict[str, object]:
    """Dựng decision-opportunity proof bằng login builder thật và kiểm receipt từng child."""
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    try:
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
    finally:
        await engine.dispose()
    if receipt_count != child_manifests:
        raise AssertionError("Proof chưa có receipt hiện hành cho mọi child manifest.")
    return {
        "base_opportunity_complete": proof.proof.completeness_verified,
        "opportunities": proof.proof.opportunity_count,
        "proof_retry_idempotent": retry.duplicate and retry.record_id == proof.record_id,
        "training_receipts": receipt_count,
    }


if __name__ == "__main__":
    if "--proof" in sys.argv:
        outcome = build_proof(
            trial_record_id=UUID(os.environ["WP06_PINNED_TRIAL_RECORD_ID"]),
            child_manifests=int(os.environ["WP06_PINNED_CHILD_MANIFESTS"]),
        )
    else:
        outcome = run_probe(
            scorer_seed="--scorer-seed" in sys.argv,
            provider=BINANCE_PROVIDER if "--binance" in sys.argv else CRYPIDIAN_FIRST_PARTY_QA,
        )
    print(json.dumps(asyncio.run(outcome), sort_keys=True))
