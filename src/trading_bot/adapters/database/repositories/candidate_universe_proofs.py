"""Scan authoritative toàn trial BASE và ghi proof trong cùng snapshot DB."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.learning_fold_outcome_verifier import (
    DatabaseFoldOutcomeVerifier,
    FoldOutcomeVerificationError,
)
from trading_bot.adapters.database.models.backtest_entities import (
    BacktestReportRecord,
    BacktestRunRecord,
    BacktestTrialRecord,
)
from trading_bot.adapters.database.models.backtest_job_entities import BacktestReplayJobRecord
from trading_bot.adapters.database.models.backtest_opportunity_entities import (
    BacktestDecisionOpportunityRecord,
)
from trading_bot.adapters.database.models.backtest_outcome_entities import BacktestScenarioRecord
from trading_bot.adapters.database.models.candidate_universe_entities import (
    CandidateTrainingRightsRecord,
    CandidateUniverseProofRecord,
)
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.replay_training_rights import (
    DatabaseReplayTrainingRightsGate,
    ReplayTrainingRightsReceipt,
    training_scope_digest,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import (
    BacktestOutcomeRepository,
    PersistedBacktestTradeOutcome,
)
from trading_bot.adapters.database.repositories.backtests import BacktestRepositoryConflict
from trading_bot.application.backtest import ReplayManifest
from trading_bot.application.backtest_dataset_manifest import MarketBarIdentityMetadata
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetReplayBundleReference,
)
from trading_bot.application.backtest_dataset_replay_observations import replay_decision_event_id
from trading_bot.application.binance_rest_backfill import (
    BACKTEST_RIGHTS_PROVIDERS,
    market_source_for_rights_provider,
)
from trading_bot.application.candidate_universe_proof import (
    DECISION_OPPORTUNITY_QUERY_VERSION,
    QUERY_VERSION,
    CandidateDecisionOpportunityProof,
    CandidateDecisionOpportunityProofResult,
    CandidateUniverseProof,
    CandidateUniverseProofResult,
    canonical,
    digest,
)

DECISION_PROOF_FIELDS = (
    "opportunity_count",
    "no_candidate_count",
    "not_run_count",
    "input_excluded_count",
    "unlabeled_candidate_count",
    "outcome_link_count",
    "source_scope_hash",
    "completeness_verified",
)
SOURCE_METADATA_COLUMNS = (
    "id",
    "workspace_id",
    "symbol",
    "timeframe",
    "source",
    "logical_key",
    "revision",
    "supersedes_id",
    "checksum",
    "raw_reference_id",
    "open_time",
    "close_time",
    "available_at",
    "is_closed",
)


class CandidateUniverseProofError(ValueError):
    """Không thể chứng minh coverage hoặc proof cũ không còn khớp canonical."""


class CandidateUniverseProofRepository:
    """Không có API nhận candidate/exclusion/claimed count từ caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build_and_append(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> CandidateUniverseProofResult:
        """Tái xác minh mọi row trước cả lần ghi đầu và idempotent retry."""
        if type(workspace_id) is not UUID or type(trial_record_id) is not UUID:
            raise CandidateUniverseProofError("Scope phải là UUID tường minh.")
        await self._require_snapshot(workspace_id)
        await self._lock_replay_sources(workspace_id)
        locked: object = (
            await self._session.execute(
                text("SELECT public.wp06_lock_candidate_trial(:workspace_id, :trial_id)"),
                {"workspace_id": workspace_id, "trial_id": trial_record_id},
            )
        ).scalar_one()
        if locked is not True:
            raise CandidateUniverseProofError("Không khóa được trial trong workspace.")
        try:
            proof, receipts = await self._scan(workspace_id, trial_record_id)
        except CandidateUniverseProofError:
            raise
        except (
            BacktestRepositoryConflict,
            FoldOutcomeVerificationError,
            ValueError,
            PermissionError,
            TypeError,
            KeyError,
            ArithmeticError,
        ) as error:
            raise CandidateUniverseProofError("Lineage/hash/provenance DB không hợp lệ.") from error
        await self._require_current_lease(receipts)
        lookup = select(CandidateUniverseProofRecord).where(
            CandidateUniverseProofRecord.workspace_id == workspace_id,
            CandidateUniverseProofRecord.trial_record_id == trial_record_id,
            CandidateUniverseProofRecord.query_version == QUERY_VERSION,
        )
        existing = await self._session.scalar(lookup)
        if existing is not None:
            self._validate_record(existing, proof)
            await self._validate_receipts(existing, receipts)
            await self._require_current_lease(receipts)
            return CandidateUniverseProofResult(existing.id, proof, True)
        row = CandidateUniverseProofRecord(
            id=uuid4(),
            **{
                field: getattr(proof, field)
                for field in (
                    "workspace_id",
                    "run_id",
                    "trial_record_id",
                    "report_id",
                    "scenario_id",
                    "query_version",
                    "manifest_hash",
                    "membership_digest",
                    "candidate_count",
                    "eligible_count",
                    "exclusion_count",
                )
            },
            proof_hash=proof.content_hash,
            canonical_proof=proof.canonical_json,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
                for receipt in receipts:
                    self._session.add(
                        CandidateTrainingRightsRecord(
                            id=uuid4(),
                            workspace_id=workspace_id,
                            proof_id=row.id,
                            child_dataset_hash=receipt.child_dataset_hash,
                            policy_fingerprint=receipt.policy_fingerprint,
                            purpose=receipt.purpose.value,
                            evidence_hash=digest(receipt.canonical_json),
                            canonical_evidence=receipt.canonical_json,
                        )
                    )
                await self._session.flush()
                await self._require_current_lease(receipts)
        except IntegrityError as error:
            existing = await self._session.scalar(lookup)
            if existing is None:
                # Caller retry toàn transaction nếu xung đột không có row authoritative.
                # Tuyệt đối không trả proof chưa ghi.
                raise CandidateUniverseProofError(
                    "Xung đột proof; cần retry toàn transaction."
                ) from error
            self._validate_record(existing, proof)
            await self._validate_receipts(existing, receipts)
            await self._require_current_lease(receipts)
            return CandidateUniverseProofResult(existing.id, proof, True)
        await self._require_current_lease(receipts)
        return CandidateUniverseProofResult(row.id, proof, False)

    async def build_decision_opportunity(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> CandidateDecisionOpportunityProofResult:
        """Khóa nguồn → quyền TRAINING mọi child → khóa trial → scan → lease."""
        if type(workspace_id) is not UUID or type(trial_record_id) is not UUID:
            raise CandidateUniverseProofError("Scope phải là UUID tường minh.")
        await self._require_snapshot(workspace_id)
        await self._lock_replay_sources(workspace_id)
        try:
            trial = await self._session.scalar(
                select(BacktestTrialRecord).where(
                    BacktestTrialRecord.workspace_id == workspace_id,
                    BacktestTrialRecord.id == trial_record_id,
                )
            )
            if trial is None:
                raise CandidateUniverseProofError("Không tìm thấy trial authoritative.")
            DatabaseFoldOutcomeVerifier._validate_trial(trial, workspace_id=workspace_id)
            run = await self._session.scalar(
                select(BacktestRunRecord).where(
                    BacktestRunRecord.workspace_id == workspace_id,
                    BacktestRunRecord.id == trial.run_id,
                )
            )
            if run is None:
                raise CandidateUniverseProofError("Trial thiếu run authoritative.")
            manifest = DatabaseFoldOutcomeVerifier._validate_run(run, workspace_id=workspace_id)
            await self._require_predeclared_learning_intent(workspace_id, run, manifest)
            receipts = await DatabaseReplayTrainingRightsGate(self._session).authorize(
                workspace_id=workspace_id, bundle_hash=manifest.dataset_hash
            )
            if not receipts or any(receipt.purpose.value != "training" for receipt in receipts):
                raise CandidateUniverseProofError("Thiếu receipt TRAINING hiện hành.")
            locked: object = (
                await self._session.execute(
                    text("SELECT public.wp06_lock_candidate_trial(:workspace_id, :trial_id)"),
                    {"workspace_id": workspace_id, "trial_id": trial_record_id},
                )
            ).scalar_one()
            if locked is not True:
                raise CandidateUniverseProofError("Không khóa được trial.")
            base, _ = await self._scan(workspace_id, trial_record_id, receipts)
            proof = await self._scan_decision_opportunities(base, manifest, receipts)
            await self._require_current_lease(receipts)
            return await self._append_decision_proof(proof, receipts)
        except CandidateUniverseProofError:
            raise
        except (
            BacktestRepositoryConflict,
            FoldOutcomeVerificationError,
            ValueError,
            PermissionError,
            TypeError,
            KeyError,
            ArithmeticError,
        ) as error:
            raise CandidateUniverseProofError(
                "Lineage/hash/provenance opportunity không hợp lệ."
            ) from error

    async def _append_decision_proof(
        self,
        proof: CandidateDecisionOpportunityProof,
        receipts: tuple[ReplayTrainingRightsReceipt, ...],
    ) -> CandidateDecisionOpportunityProofResult:
        """Append metadata/receipt bất biến; retry vẫn xác minh quyền và scan lại."""
        lookup = select(CandidateUniverseProofRecord).where(
            CandidateUniverseProofRecord.workspace_id == proof.workspace_id,
            CandidateUniverseProofRecord.trial_record_id == proof.trial_record_id,
            CandidateUniverseProofRecord.query_version == DECISION_OPPORTUNITY_QUERY_VERSION,
        )
        existing = await self._session.scalar(lookup)
        if existing is not None:
            self._validate_decision_record(existing, proof)
            await self._validate_receipts(existing, receipts)
            await self._require_current_lease(receipts)
            return CandidateDecisionOpportunityProofResult(existing.id, proof, True)
        names = (
            "workspace_id",
            "run_id",
            "trial_record_id",
            "report_id",
            "scenario_id",
            "query_version",
            "manifest_hash",
            "membership_digest",
            "candidate_count",
            "eligible_count",
            "exclusion_count",
            *DECISION_PROOF_FIELDS,
        )
        row = CandidateUniverseProofRecord(
            id=uuid4(),
            proof_hash=proof.content_hash,
            canonical_proof=proof.canonical_json,
            **{name: getattr(proof, name) for name in names},
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
                for receipt in receipts:
                    self._session.add(
                        CandidateTrainingRightsRecord(
                            id=uuid4(),
                            workspace_id=proof.workspace_id,
                            proof_id=row.id,
                            child_dataset_hash=receipt.child_dataset_hash,
                            policy_fingerprint=receipt.policy_fingerprint,
                            purpose=receipt.purpose.value,
                            evidence_hash=digest(receipt.canonical_json),
                            canonical_evidence=receipt.canonical_json,
                        )
                    )
                await self._session.flush()
                await self._require_current_lease(receipts)
        except IntegrityError as error:
            existing = await self._session.scalar(lookup)
            if existing is None:
                raise CandidateUniverseProofError(
                    "Xung đột proof; retry toàn transaction."
                ) from error
            self._validate_decision_record(existing, proof)
            await self._validate_receipts(existing, receipts)
            await self._require_current_lease(receipts)
            return CandidateDecisionOpportunityProofResult(existing.id, proof, True)
        await self._require_current_lease(receipts)
        return CandidateDecisionOpportunityProofResult(row.id, proof, False)

    @staticmethod
    def _validate_decision_record(
        row: CandidateUniverseProofRecord, proof: CandidateDecisionOpportunityProof
    ) -> None:
        CandidateUniverseProofRepository._validate_record(row, proof)
        if any(getattr(row, name) != getattr(proof, name) for name in DECISION_PROOF_FIELDS):
            raise CandidateUniverseProofError("Cột proof opportunity lệch canonical.")

    async def _decision_source_inventory(
        self,
        workspace_id: UUID,
        manifest: ReplayManifest,
        receipts: tuple[ReplayTrainingRightsReceipt, ...],
    ) -> tuple[
        BacktestDatasetReplayBundleReference,
        str,
        dict[str, tuple[MarketBarIdentityMetadata, ...]],
        tuple[datetime, ...],
        bool,
        str,
    ]:
        """Đọc riêng metadata cùng bộ lọc/cutoff/revision của replay loader."""
        from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
            BacktestDatasetManifestRepository,
        )
        from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
            BacktestDatasetReplayBundleRepository,
        )
        from trading_bot.application.backtest_dataset_manifest import (
            MarketBarIdentityMetadata,
            backtest_dataset_identity_metadata_hash,
        )
        from trading_bot.application.strategy_market_requirements import (
            entry_timeframe_for_release,
            feature_requirements_for_release,
        )
        from trading_bot.domain.market.models import MarketBarSourceIdentity
        from trading_bot.domain.strategy.registry import StrategyRegistry

        reference = await BacktestDatasetReplayBundleRepository(self._session).get_by_hash(
            workspace_id=workspace_id, bundle_hash=manifest.dataset_hash
        )
        if (
            reference is None
            or reference.workspace_id != workspace_id
            or reference.bundle_hash != manifest.dataset_hash
            or reference.evaluation_start != manifest.range_start
            or reference.range_end != manifest.range_end
            or StrategyRegistry().get_release(manifest.strategy_release_id).content_hash
            != manifest.strategy_content_hash
        ):
            raise CandidateUniverseProofError("Bundle/strategy/window không khớp replay BASE.")
        timeframe = entry_timeframe_for_release(manifest.strategy_release_id)
        required = feature_requirements_for_release(manifest.strategy_release_id)
        if timeframe is None or not set(required).issubset(
            {child.interval for child in reference.components}
        ):
            raise CandidateUniverseProofError("Bundle thiếu timeframe strategy.")
        job = await self._session.scalar(
            select(BacktestReplayJobRecord).where(
                BacktestReplayJobRecord.workspace_id == workspace_id,
                BacktestReplayJobRecord.id == UUID(manifest.learning_job_id),
            )
        )
        from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission

        if job is None:
            raise CandidateUniverseProofError("Thiếu job BASE authoritative.")
        request = BacktestReplayJobSubmission.from_payload(job.request_payload)
        if (
            request.execution_interval != timeframe
            or job.execution_interval != request.execution_interval
            or job.bundle_hash != manifest.dataset_hash
            or job.strategy_release_id != manifest.strategy_release_id
        ):
            raise CandidateUniverseProofError("Request BASE lệch timeframe/bundle/strategy.")
        by_child = {receipt.child_dataset_hash: receipt for receipt in receipts}
        if len(by_child) != len(receipts) or set(by_child) != {
            child.dataset_hash for child in reference.components
        }:
            raise CandidateUniverseProofError("Receipt TRAINING thiếu/trùng child.")
        inventory = {}
        decision_grid: tuple[datetime, ...] = ()
        scope = []
        complete = True
        for child in reference.components:
            dataset = await BacktestDatasetManifestRepository(self._session).get_by_hash(
                workspace_id=workspace_id, dataset_hash=child.dataset_hash
            )
            if (
                dataset is None
                or not dataset.has_pinned_source_identities
                or dataset.rights_workspace_id != workspace_id
                or dataset.dataset_hash != child.dataset_hash
                or dataset.interval != child.interval
                or dataset.symbol != reference.symbol
                or dataset.evaluation_start != reference.evaluation_start
                or dataset.range_end != reference.range_end
                or dataset.bars_content_hash != child.bars_content_hash
                or dataset.policy_fingerprint != reference.policy_fingerprint
                or dataset.rights_provider != reference.rights_provider
                or dataset.rights_purpose != reference.rights_purpose
            ):
                raise CandidateUniverseProofError(
                    "Child manifest thiếu identity v4 hoặc lệch scope."
                )
            expected_market_source = market_source_for_rights_provider(dataset.rights_provider)
            receipt = by_child[child.dataset_hash]
            if (
                receipt.workspace_id != workspace_id
                or receipt.bundle_hash != manifest.dataset_hash
                or receipt.interval != child.interval
                or receipt.symbol != reference.symbol
                or receipt.bars_content_hash != child.bars_content_hash
                or receipt.provider != reference.rights_provider
            ):
                raise CandidateUniverseProofError("Receipt TRAINING lệch child scope.")
            columns = [getattr(MarketBar, key) for key in SOURCE_METADATA_COLUMNS]
            mappings = (
                (
                    await self._session.execute(
                        select(*columns)
                        .where(
                            MarketBar.workspace_id == workspace_id,
                            MarketBar.symbol == dataset.symbol,
                            MarketBar.timeframe == dataset.interval,
                            MarketBar.source == expected_market_source,
                            MarketBar.open_time >= dataset.warmup_start,
                            MarketBar.open_time < dataset.range_end,
                            MarketBar.available_at <= dataset.prepared_at,
                            MarketBar.is_closed.is_(True),
                        )
                        .order_by(MarketBar.open_time, MarketBar.revision.desc())
                    )
                )
                .mappings()
                .all()
            )
            latest: dict[str, RowMapping] = {}
            for row in sorted(mappings, key=lambda row: (row["open_time"], -row["revision"])):
                if (
                    row["workspace_id"] != workspace_id
                    or row["symbol"] != dataset.symbol
                    or row["timeframe"] != dataset.interval
                    or row["source"] != expected_market_source
                    or not dataset.warmup_start <= row["open_time"] < dataset.range_end
                    or row["available_at"] > dataset.prepared_at
                    or row["is_closed"] is not True
                ):
                    raise CandidateUniverseProofError("Metadata nguồn vượt child scope.")
                latest.setdefault(row["logical_key"], row)
            bars = tuple(
                MarketBarIdentityMetadata(
                    MarketBarSourceIdentity(
                        workspace_id,
                        row["id"],
                        row["logical_key"],
                        row["revision"],
                        row["supersedes_id"],
                        row["checksum"],
                    ),
                    row["source"],
                    row["symbol"],
                    row["timeframe"],
                    row["open_time"],
                    row["close_time"],
                    row["available_at"],
                    row["is_closed"],
                    None if row["raw_reference_id"] is None else str(row["raw_reference_id"]),
                )
                for row in latest.values()
            )
            observed_hash = backtest_dataset_identity_metadata_hash(bars, workspace_id=workspace_id)
            interval = (dataset.range_end - dataset.warmup_start) / dataset.expected_bar_count
            ordered_times = tuple(
                dataset.warmup_start + interval * index
                for index in range(dataset.expected_bar_count)
            )
            expected_times = set(ordered_times)
            if child.interval == timeframe:
                decision_grid = tuple(
                    timestamp
                    for timestamp in ordered_times
                    if dataset.evaluation_start <= timestamp < dataset.range_end
                )
                if (
                    len(decision_grid) != dataset.evaluation_bar_count
                    or not decision_grid
                    or decision_grid[0] != dataset.evaluation_start
                ):
                    raise CandidateUniverseProofError("Decision grid lệch scope/count manifest.")
            actual_times = {bar.open_time for bar in bars}
            if len(actual_times) != len(bars) or not actual_times.issubset(expected_times):
                raise CandidateUniverseProofError(
                    "Nguồn duplicate hoặc ngoài ordered grid đã ghim."
                )
            gap = actual_times != expected_times or not dataset.is_ready
            if not gap and observed_hash != dataset.bar_identity_hash:
                raise CandidateUniverseProofError("Identity/revision nguồn lệch hash manifest v4.")
            complete = complete and not gap
            inventory[child.interval] = bars
            scope.append(
                {
                    "child_dataset_hash": dataset.dataset_hash,
                    "bar_identity_hash": dataset.bar_identity_hash,
                    "observed_identity_hash": observed_hash,
                    "expected_count": dataset.expected_bar_count,
                    "observed_count": len(bars),
                    "source_gap": gap,
                }
            )
        return reference, timeframe, inventory, decision_grid, complete, digest(canonical(scope))

    async def _scan_decision_opportunities(
        self,
        base: CandidateUniverseProof,
        manifest: ReplayManifest,
        receipts: tuple[ReplayTrainingRightsReceipt, ...],
    ) -> CandidateDecisionOpportunityProof:
        """Đối chiếu tập event/ordinal chính xác; missing input không thành no-candidate."""
        from trading_bot.application.backtest_decision_opportunities import (
            ReplayDecisionOpportunity,
        )
        from trading_bot.application.decision_snapshot_builder import (
            canonical_market_bar_metadata_event_id,
        )

        (
            reference,
            timeframe,
            inventory,
            decision_grid,
            sources_complete,
            source_hash,
        ) = await self._decision_source_inventory(base.workspace_id, manifest, receipts)
        scenario = await self._session.scalar(
            select(BacktestScenarioRecord).where(
                BacktestScenarioRecord.workspace_id == base.workspace_id,
                BacktestScenarioRecord.id == base.scenario_id,
            )
        )
        if scenario is None:
            raise CandidateUniverseProofError("Thiếu scenario BASE authoritative.")
        decision_bars = {bar.open_time: bar for bar in inventory[timeframe]}
        expected = {}
        # Grid đầy đủ lấy từ manifest; nến đọc được chỉ dùng đối chiếu nguồn.
        # Nguồn bị thiếu không được làm co tập event hoặc đổi decision ordinal.
        for ordinal, open_time in enumerate(decision_grid):
            event_id = replay_decision_event_id(
                bundle_hash=manifest.dataset_hash,
                strategy_release_id=manifest.strategy_release_id,
                decision_timeframe=timeframe,
                decision_open_time=open_time,
            )
            expected[event_id] = (ordinal, decision_bars.get(open_time))
        rows = tuple(
            (
                await self._session.scalars(
                    select(BacktestDecisionOpportunityRecord)
                    .where(
                        BacktestDecisionOpportunityRecord.workspace_id == base.workspace_id,
                        BacktestDecisionOpportunityRecord.trial_record_id == base.trial_record_id,
                    )
                    .order_by(BacktestDecisionOpportunityRecord.decision_ordinal)
                )
            ).all()
        )
        if (
            len({row.event_id for row in rows}) != len(rows)
            or len({row.decision_ordinal for row in rows}) != len(rows)
            or len({row.id for row in rows}) != len(rows)
        ):
            raise CandidateUniverseProofError("Opportunity duplicate event/ordinal/record.")
        outcomes = await BacktestOutcomeRepository(self._session).load_trade_outcome_rows(
            workspace_id=base.workspace_id, trial_record_id=base.trial_record_id
        )
        by_outcome: dict[str, PersistedBacktestTradeOutcome] = {}
        for outcome in outcomes.rows:
            entry_event_id = outcome.trade.entry_opportunity_event_id
            if entry_event_id is None or entry_event_id in by_outcome:
                raise CandidateUniverseProofError("Outcome orphan hoặc duplicate entry candidate.")
            by_outcome[entry_event_id] = outcome
        members = []
        candidate_ids = set()
        candidate_count = no_candidate_count = not_run_count = excluded_input = linked = 0
        for row in sorted(rows, key=lambda row: row.decision_ordinal):
            values = {
                field.name: getattr(
                    row, "scenario_key" if field.name == "scenario_id" else field.name
                )
                for field in fields(ReplayDecisionOpportunity)
                if field.init
            }
            values["feature_snapshot_ids"] = tuple(map(tuple, values["feature_snapshot_ids"]))
            values["source_event_digests"] = tuple(map(tuple, values["source_event_digests"]))
            opportunity = ReplayDecisionOpportunity(**values)
            if (
                row.canonical_opportunity != opportunity.canonical_json()
                or row.content_hash != opportunity.content_hash
                or row.idempotency_key != opportunity.idempotency_key
                or row.workspace_id != base.workspace_id
                or row.run_id != base.run_id
                or row.trial_record_id != base.trial_record_id
                or row.scenario_id != base.scenario_id
                or row.scenario_key != scenario.scenario_id
                or row.manifest_hash != base.manifest_hash
                or row.bundle_hash != manifest.dataset_hash
                or row.strategy_release_id != manifest.strategy_release_id
                or row.strategy_content_hash != manifest.strategy_content_hash
                or row.symbol != reference.symbol
                or row.decision_timeframe != timeframe
                or row.evaluation_start != manifest.range_start
                or row.range_end != manifest.range_end
                or (
                    row.feature_schema_hash is not None
                    and row.feature_schema_hash != manifest.feature_schema_hash
                )
            ):
                raise CandidateUniverseProofError(
                    "Opportunity canonical/hash/BASE scope không khớp."
                )
            source = expected.get(row.event_id)
            if source is None:
                raise CandidateUniverseProofError("Opportunity event ngoài grid manifest đã ghim.")
            ordinal, bar = source
            if row.decision_ordinal != ordinal:
                raise CandidateUniverseProofError("Opportunity ordinal lệch grid manifest.")
            # Khi bar vắng chỉ giữ diagnostic incomplete; không xác minh thời gian
            # hoặc digest nguồn bằng giá trị tự khai trên opportunity đó.
            if bar is not None and (
                row.event_at != bar.close_time
                or row.available_at != bar.available_at
                or row.decision_at != bar.available_at
            ):
                raise CandidateUniverseProofError("Opportunity thời gian lệch source event đã đọc.")
            source_digests = []
            for part_timeframe, _ in opportunity.source_event_digests:
                if part_timeframe not in inventory:
                    raise CandidateUniverseProofError(
                        "Opportunity dùng timeframe nguồn ngoài bundle."
                    )
                visible = [
                    bar
                    for bar in inventory[part_timeframe]
                    if bar.is_closed
                    and bar.close_time <= row.decision_at
                    and bar.available_at <= row.decision_at
                ]
                source_digests.append(
                    (
                        part_timeframe,
                        digest(
                            canonical(
                                {
                                    "timeframe": part_timeframe,
                                    "source_event_ids": [
                                        canonical_market_bar_metadata_event_id(
                                            bar, workspace_id=base.workspace_id
                                        )
                                        for bar in visible
                                    ],
                                    "source_bar_ids": [
                                        f"market-bar-record:{bar.source_identity.record_id}"
                                        for bar in visible
                                    ],
                                }
                            )
                        ),
                    )
                )
            if sources_complete and tuple(source_digests) != opportunity.source_event_digests:
                raise CandidateUniverseProofError(
                    "Opportunity digest nguồn không khớp pinned bars."
                )
            from trading_bot.application.strategy_market_requirements import (
                feature_requirements_for_release,
            )

            if tuple(tf for tf, _ in opportunity.source_event_digests) != tuple(
                feature_requirements_for_release(manifest.strategy_release_id)
            ):
                raise CandidateUniverseProofError(
                    "Opportunity thiếu/thừa ordered timeframe lineage."
                )
            linked_outcome = by_outcome.pop(row.event_id, None)
            if row.strategy_state == "CANDIDATE":
                candidate_count += 1
                if row.candidate_id in candidate_ids:
                    raise CandidateUniverseProofError("Candidate ID bị trùng opportunity.")
                candidate_ids.add(row.candidate_id)
            elif row.strategy_state == "NO_CANDIDATE":
                no_candidate_count += 1
            else:
                not_run_count += 1
            excluded_input += int(row.input_state == "INPUT_EXCLUDED")
            if linked_outcome is not None:
                trade = linked_outcome.trade
                if (
                    row.strategy_state != "CANDIDATE"
                    or row.input_state != "EVALUATED"
                    or row.decision_outcome != "PAPER_INTENT_CREATED"
                    or not trade.has_entry_provenance
                    or trade.entry_candidate_id != row.candidate_id
                    or trade.entry_decision_snapshot_id != row.runtime_snapshot_id
                    or trade.entry_decision_at != row.decision_at
                    or trade.entry_symbol != row.symbol
                    or trade.entry_feature_schema_hash != row.feature_schema_hash
                    or trade.entry_vector_hash != row.vector_hash
                    or trade.entry_features_hash != row.feature_values_hash
                ):
                    raise CandidateUniverseProofError(
                        "Outcome không khớp exact opportunity entry snapshot/vector."
                    )
                linked += 1
            reason = (
                "closed_outcome_missing"
                if row.strategy_state == "CANDIDATE" and linked_outcome is None
                else None
            )
            members.append(
                {
                    "opportunity_record_id": str(row.id),
                    "event_id": row.event_id,
                    "decision_ordinal": row.decision_ordinal,
                    "opportunity_hash": row.content_hash,
                    "input_state": row.input_state,
                    "strategy_state": row.strategy_state,
                    "candidate_id": row.candidate_id,
                    "outcome_record_id": str(linked_outcome.record_id) if linked_outcome else None,
                    "outcome_hash": linked_outcome.outcome_hash if linked_outcome else None,
                    "exclusion_reason": reason,
                }
            )
        if by_outcome:
            raise CandidateUniverseProofError("Outcome entry không có opportunity cùng trial.")
        unlabeled = candidate_count - linked
        common = {
            field: getattr(base, field)
            for field in (
                "workspace_id",
                "run_id",
                "trial_record_id",
                "report_id",
                "scenario_id",
                "manifest_hash",
                "trial_hash",
                "report_hash",
                "scenario_hash",
                "outcomes_hash",
                "training_rights_digest",
            )
        }
        return CandidateDecisionOpportunityProof(
            **common,
            membership_digest=digest(canonical(members)),
            candidate_count=candidate_count,
            eligible_count=linked,
            exclusion_count=unlabeled,
            exclusion_reason_counts=(("closed_outcome_missing", unlabeled),) if unlabeled else (),
            opportunity_count=len(rows),
            no_candidate_count=no_candidate_count,
            not_run_count=not_run_count,
            input_excluded_count=excluded_input,
            unlabeled_candidate_count=unlabeled,
            outcome_link_count=linked,
            source_scope_hash=source_hash,
            completeness_verified=(
                sources_complete
                and not excluded_input
                and {(row.event_id, row.decision_ordinal) for row in rows}
                == {(event_id, ordinal) for event_id, (ordinal, _) in expected.items()}
            ),
        )

    async def _require_current_lease(
        self, receipts: tuple[ReplayTrainingRightsReceipt, ...]
    ) -> None:
        """Kiểm DB clock sau mọi flush/read chờ khóa, ngay trước trả kết quả."""
        persisted_at: object = (
            await self._session.execute(text("SELECT clock_timestamp()"))
        ).scalar_one()
        if (
            not isinstance(persisted_at, datetime)
            or persisted_at.tzinfo is None
            or any(
                not receipt.checked_at <= persisted_at < receipt.valid_until for receipt in receipts
            )
        ):
            raise CandidateUniverseProofError(
                "Lease TRAINING đã hết hạn trước khi transaction kết thúc."
            )

    async def _require_snapshot(self, workspace_id: UUID) -> None:
        if (
            not self._session.in_transaction()
            or self._session.info.get("workspace_id") != workspace_id
        ):
            raise CandidateUniverseProofError("Thiếu transaction hoặc tenant session context.")
        isolation: object = (
            await self._session.execute(text("SHOW transaction_isolation"))
        ).scalar_one()
        tenant = (
            await self._session.execute(text("SELECT current_setting('app.workspace_id', true)"))
        ).scalar_one_or_none()
        if isolation != "read committed" or tenant != str(workspace_id):
            raise CandidateUniverseProofError(
                "Proof yêu cầu snapshot DB nhất quán và tenant binding."
            )

    async def _lock_replay_sources(self, workspace_id: UUID) -> None:
        """Khóa hai nguồn replay allowlist theo thứ tự cố định trước khi đọc rights."""
        for source_id in sorted(BACKTEST_RIGHTS_PROVIDERS):
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"research-source:{workspace_id}:{source_id}"},
            )

    async def _scan(
        self,
        workspace_id: UUID,
        trial_record_id: UUID,
        authorized_receipts: tuple[ReplayTrainingRightsReceipt, ...] | None = None,
    ) -> tuple[CandidateUniverseProof, tuple[ReplayTrainingRightsReceipt, ...]]:
        trial = await self._session.scalar(
            select(BacktestTrialRecord).where(
                BacktestTrialRecord.workspace_id == workspace_id,
                BacktestTrialRecord.id == trial_record_id,
            )
        )
        if trial is None or trial.id != trial_record_id:
            raise CandidateUniverseProofError("Không tìm thấy trial authoritative.")
        DatabaseFoldOutcomeVerifier._validate_trial(trial, workspace_id=workspace_id)
        trial_payload = json.loads(trial.canonical_trial)
        if any(
            type(trial_payload[field]) is not int
            for field in (
                "trial_index",
                "trial_count",
                "observation_count",
                "completed_observation_count",
                "trade_count",
            )
        ):
            raise CandidateUniverseProofError(
                "Canonical trial counts phải là số nguyên tường minh."
            )
        run = await self._session.scalar(
            select(BacktestRunRecord).where(
                BacktestRunRecord.workspace_id == workspace_id, BacktestRunRecord.id == trial.run_id
            )
        )
        if run is None:
            raise CandidateUniverseProofError("Trial thiếu run.")
        manifest = DatabaseFoldOutcomeVerifier._validate_run(run, workspace_id=workspace_id)
        if (
            run.id != trial.run_id
            or run.manifest_hash != trial.manifest_hash
            or manifest.parameter_family_hash != trial.parameter_family_hash
            or manifest.trial_index != trial.trial_index
            or manifest.trial_count != trial.trial_count
        ):
            raise CandidateUniverseProofError("Trial/run/manifest parameter lineage không khớp.")
        await self._require_predeclared_learning_intent(workspace_id, run, manifest)
        receipts = authorized_receipts
        if receipts is None:
            receipts = await DatabaseReplayTrainingRightsGate(self._session).authorize(
                workspace_id=workspace_id, bundle_hash=manifest.dataset_hash
            )
        if not receipts:
            raise CandidateUniverseProofError("Gate TRAINING không trả receipt đầy đủ.")
        reports = tuple(
            (
                await self._session.scalars(
                    select(BacktestReportRecord).where(
                        BacktestReportRecord.workspace_id == workspace_id,
                        BacktestReportRecord.trial_record_id == trial_record_id,
                    )
                )
            ).all()
        )
        scenarios = tuple(
            (
                await self._session.scalars(
                    select(BacktestScenarioRecord).where(
                        BacktestScenarioRecord.workspace_id == workspace_id,
                        BacktestScenarioRecord.trial_record_id == trial_record_id,
                    )
                )
            ).all()
        )
        if len(reports) != 1 or len(scenarios) != 1:
            raise CandidateUniverseProofError("Cần duy nhất report và scenario cho trial BASE.")
        report, scenario = reports[0], scenarios[0]
        for item in (report, scenario):
            if (
                item.workspace_id != workspace_id
                or item.run_id != run.id
                or item.trial_record_id != trial.id
                or item.manifest_hash != run.manifest_hash
            ):
                raise CandidateUniverseProofError(
                    "Report/scenario vượt run/trial/workspace/manifest scope."
                )
        expected_scenario = {
            "scenario_id": scenario.scenario_id,
            "kind": "BASE",
            "cost_overrides": [],
            "parameter_overrides": [],
        }
        if (
            scenario.kind != "BASE"
            or scenario.scenario != expected_scenario
            or canonical(expected_scenario) != scenario.canonical_scenario
            or digest(scenario.canonical_scenario) != scenario.scenario_hash
        ):
            raise CandidateUniverseProofError("Scenario không phải BASE canonical không override.")
        payload = json.loads(report.canonical_json)
        expected_keys = {
            "manifest_hash",
            "report_hash",
            "observation_count",
            "usable_observation_count",
            "trade_count",
            "coverage",
            "trade_frequency",
            "gross_pnl",
            "net_pnl",
            "fees",
            "funding",
            "slippage",
            "expectancy",
            "max_drawdown",
            "average_exposure",
            "confidence_interval",
            "caveat_vi",
        }
        if (
            type(payload) is not dict
            or set(payload) != expected_keys
            or payload != report.report
            or canonical(payload) != report.canonical_json
        ):
            raise CandidateUniverseProofError("Report sai schema/canonical/JSONB.")
        if any(
            type(payload[field]) is not int
            for field in ("observation_count", "usable_observation_count", "trade_count")
        ):
            raise CandidateUniverseProofError(
                "Canonical report counts phải là số nguyên tường minh."
            )
        for field in (
            "manifest_hash",
            "report_hash",
            "observation_count",
            "usable_observation_count",
            "trade_count",
            "coverage",
        ):
            if payload[field] != getattr(report, field):
                raise CandidateUniverseProofError("Cột report lệch canonical.")
        metrics_payload = {
            key: value for key, value in payload.items() if key not in ("report_hash", "caveat_vi")
        }
        if (
            digest(canonical(metrics_payload)) != report.report_hash
            or report.observation_count != trial.observation_count
            or report.usable_observation_count != trial.completed_observation_count
        ):
            raise CandidateUniverseProofError("Report hash/count lệch trial.")
        persisted = await BacktestOutcomeRepository(self._session).load_trade_outcome_rows(
            workspace_id=workspace_id, trial_record_id=trial_record_id
        )
        rows = persisted.rows
        if (
            len(rows) != trial.trade_count
            or len(rows) != report.trade_count
            or len({row.record_id for row in rows}) != len(rows)
            or len({row.trade_id for row in rows}) != len(rows)
        ):
            raise CandidateUniverseProofError("Outcome count/uniqueness không khớp trial/report.")
        members = []
        excluded = 0
        for ordinal, row in enumerate(rows):
            if (
                row.workspace_id != workspace_id
                or row.run_id != run.id
                or row.trial_record_id != trial_record_id
                or row.manifest_hash != run.manifest_hash
                or row.ordinal != ordinal
            ):
                raise CandidateUniverseProofError(
                    "Outcome run/manifest/ordinal lineage không khớp."
                )
            trade = row.trade
            if not manifest.range_start <= trade.opened_at <= trade.closed_at < manifest.range_end:
                raise CandidateUniverseProofError("Trade nằm ngoài range replay đã đóng băng.")
            if trade.has_entry_provenance and (
                trade.entry_feature_schema_hash != manifest.feature_schema_hash
                or trade.entry_symbol not in {receipt.symbol for receipt in receipts}
                or trade.entry_decision_at is None
                or not manifest.range_start <= trade.entry_decision_at <= trade.opened_at
            ):
                raise CandidateUniverseProofError(
                    "Entry provenance lệch schema/symbol/thời gian replay."
                )
            reason = None if trade.has_entry_provenance else "entry_provenance_missing"
            excluded += int(reason is not None)
            members.append(
                {
                    "ordinal": ordinal,
                    "outcome_record_id": str(row.record_id),
                    "outcome_hash": row.outcome_hash,
                    "eligible": reason is None,
                    "exclusion_reason": reason,
                }
            )
        return CandidateUniverseProof(
            workspace_id=workspace_id,
            run_id=run.id,
            trial_record_id=trial.id,
            report_id=report.id,
            scenario_id=scenario.id,
            manifest_hash=run.manifest_hash,
            trial_hash=trial.content_hash,
            report_hash=report.report_hash,
            scenario_hash=scenario.scenario_hash,
            outcomes_hash=persisted.outcomes_hash,
            membership_digest=digest(canonical(members)),
            training_rights_digest=training_scope_digest(receipts),
            candidate_count=len(rows),
            eligible_count=len(rows) - excluded,
            exclusion_count=excluded,
            exclusion_reason_counts=(("entry_provenance_missing", excluded),) if excluded else (),
        ), receipts

    async def _require_predeclared_learning_intent(
        self, workspace_id: UUID, run: BacktestRunRecord, manifest: ReplayManifest
    ) -> None:
        """Chỉ tạo candidate từ run có intent TRAINING_BASE ghim trước replay."""
        if manifest.learning_intent != "TRAINING_BASE" or manifest.learning_job_id is None:
            raise CandidateUniverseProofError(
                "Replay thiếu learning intent TRAINING_BASE đã khai báo trước khi chạy."
            )
        try:
            learning_job_id = UUID(manifest.learning_job_id)
        except (TypeError, ValueError) as error:
            raise CandidateUniverseProofError("Learning intent thiếu job UUID hợp lệ.") from error
        job = await self._session.scalar(
            select(BacktestReplayJobRecord).where(
                BacktestReplayJobRecord.workspace_id == workspace_id,
                BacktestReplayJobRecord.id == learning_job_id,
            )
        )
        if job is None:
            raise CandidateUniverseProofError("Không tìm thấy job học đã ghim vào replay.")
        request_payload = job.request_payload
        try:
            canonical_request = json.dumps(
                request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as error:
            raise CandidateUniverseProofError("Request job học không hợp lệ.") from error
        if (
            job.canonical_request != canonical_request
            or request_payload.get("learning_intent") != "TRAINING_BASE"
            or job.status != "SUCCEEDED"
            or str(run.id) not in tuple(str(item) for item in job.result_run_ids)
            or job.created_at is None
            or run.created_at is None
            or job.created_at > run.created_at
        ):
            raise CandidateUniverseProofError(
                "learning intent không khớp request bất biến trước replay."
            )
        if (
            request_payload.get("bundle_hash") != manifest.dataset_hash
            or request_payload.get("strategy_release_id") != manifest.strategy_release_id
            or request_payload.get("seed") != manifest.seed
        ):
            raise CandidateUniverseProofError(
                "Learning job không khớp bundle, strategy hoặc seed của replay."
            )

    @staticmethod
    def _validate_record(row: CandidateUniverseProofRecord, proof: CandidateUniverseProof) -> None:
        fields = (
            "workspace_id",
            "run_id",
            "trial_record_id",
            "report_id",
            "scenario_id",
            "query_version",
            "manifest_hash",
            "membership_digest",
            "candidate_count",
            "eligible_count",
            "exclusion_count",
        )
        if (
            row.canonical_proof != proof.canonical_json
            or row.proof_hash != proof.content_hash
            or any(getattr(row, field) != getattr(proof, field) for field in fields)
        ):
            raise CandidateUniverseProofError("Proof đã lưu xung đột canonical/hash/cột scope.")

    async def _validate_receipts(
        self,
        proof_row: CandidateUniverseProofRecord,
        receipts: tuple[ReplayTrainingRightsReceipt, ...],
    ) -> None:
        rows = tuple(
            (
                await self._session.scalars(
                    select(CandidateTrainingRightsRecord).where(
                        CandidateTrainingRightsRecord.workspace_id == proof_row.workspace_id,
                        CandidateTrainingRightsRecord.proof_id == proof_row.id,
                    )
                )
            ).all()
        )
        expected = {receipt.child_dataset_hash: receipt for receipt in receipts}
        if len(rows) != len(expected) or {row.child_dataset_hash for row in rows} != set(expected):
            raise CandidateUniverseProofError("Receipt TRAINING đã lưu thiếu/trùng child.")
        for row in rows:
            current = expected[row.child_dataset_hash]
            try:
                payload = json.loads(row.canonical_evidence)
                if type(payload) is not dict:
                    raise ValueError("Receipt phải là object.")
                checked = datetime.fromisoformat(payload["checked_at"])
                expires = datetime.fromisoformat(payload["valid_until"])
                verified = datetime.fromisoformat(payload["verified_at"])
                if (
                    checked.tzinfo is None
                    or expires.tzinfo is None
                    or verified.tzinfo is None
                    or not verified <= checked < expires
                ):
                    raise ValueError("Receipt sai thời gian snapshot.")
            except (ValueError, TypeError, KeyError) as error:
                raise CandidateUniverseProofError(
                    "Receipt TRAINING sai schema/thời gian."
                ) from error
            scope = {
                key: value
                for key, value in payload.items()
                if key not in ("checked_at", "valid_until")
            }
            if (
                row.workspace_id != proof_row.workspace_id
                or row.proof_id != proof_row.id
                or scope != current.scope_payload
                or canonical(payload) != row.canonical_evidence
                or digest(row.canonical_evidence) != row.evidence_hash
                or row.policy_fingerprint != current.policy_fingerprint
                or row.purpose != "training"
            ):
                raise CandidateUniverseProofError(
                    "Receipt TRAINING sai scope/purpose/canonical/hash."
                )
