"""Dựng và lưu inventory opportunity TEST scorer từ dữ liệu authoritative."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    BacktestDecisionOpportunityRecord,
    BacktestRunRecord,
    BacktestTradeOutcomeRecord,
    BacktestTrialRecord,
    LearningScorerArtifactRecord,
    LearningScorerTestOpportunityProofRecord,
    LearningSplitApprovalRecord,
    LearningSplitCandidateRecord,
    LearningTrainingAttemptRecord,
    NormalizedFoldAssignmentRecord,
    NormalizedLearningDatasetRecord,
    NormalizedLearningSampleRecord,
)
from trading_bot.adapters.database.replay_training_rights import (
    SOURCE_ID,
    DatabaseReplayTrainingRightsGate,
    ReplayTrainingRightsReceipt,
    training_scope_digest,
)
from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
    CandidateUniverseProofRepository,
)
from trading_bot.application.backtest_dataset_replay_observations import (
    replay_decision_event_id,
)
from trading_bot.application.candidate_universe_proof import canonical, digest
from trading_bot.application.learning_fold_plan import ManifestFoldSelection
from trading_bot.application.learning_scorer_evaluation import (
    ScorerTestOpportunityEntry,
    ScorerTestOpportunityProof,
)
from trading_bot.application.learning_split_candidate import LearningSplitCandidate

_CANDIDATE_BUILDER = "crypidian_candidate_builder"


class LearningScorerTestOpportunityProofConflict(RuntimeError):
    """Proof C.3 lệch scope authoritative, bytes canonical hoặc retry bất biến."""


@dataclass(frozen=True, slots=True)
class LearningScorerTestOpportunityProofWriteResult:
    """Proof TEST đã append hoặc đọc lại từ cùng canonical bytes."""

    record: LearningScorerTestOpportunityProofRecord
    proof: ScorerTestOpportunityProof
    is_duplicate: bool


@dataclass(frozen=True, slots=True)
class _TestMembershipRow:
    sample_id: str
    sample_hash: str
    outcome_id: UUID
    outcome_hash: str
    feature_snapshot_hash: str
    feature_schema_hash: str


class LearningScorerTestOpportunityProofRepository:
    """Chỉ nhận UUID; dựng grid TEST và event/outcome mapping bên trong DB boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build_and_append(
        self,
        *,
        workspace_id: UUID,
        evaluation_run_id: UUID,
        attempt_id: UUID,
    ) -> LearningScorerTestOpportunityProofWriteResult:
        """Quét pinned bundle/TEST window rồi append một proof bất biến duy nhất."""
        if any(type(value) is not UUID for value in (workspace_id, evaluation_run_id, attempt_id)):
            raise ValueError("Workspace, evaluation run và attempt phải là UUID.")
        await self._require_builder_snapshot(workspace_id)
        if not self._session.in_transaction():
            raise LearningScorerTestOpportunityProofConflict(
                "Proof TEST cần transaction caller đang mở."
            )
        isolation: str = (
            await self._session.execute(text("SHOW transaction_isolation"))
        ).scalar_one()
        if isolation != "read committed":
            raise LearningScorerTestOpportunityProofConflict(
                "Proof TEST cần READ COMMITTED để chờ source rights lock."
            )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"wp06-scorer-test-proof:{workspace_id}:{attempt_id}"},
        )

        try:
            proof, rights_receipts = await self._build_authoritative_proof(
                workspace_id=workspace_id,
                evaluation_run_id=evaluation_run_id,
                attempt_id=attempt_id,
            )
        except LearningScorerTestOpportunityProofConflict:
            raise
        except (LookupError, PermissionError, ValueError, TypeError, KeyError) as error:
            raise LearningScorerTestOpportunityProofConflict(
                "Không dựng được inventory TEST từ lineage authoritative."
            ) from error

        lookup = select(LearningScorerTestOpportunityProofRecord).where(
            LearningScorerTestOpportunityProofRecord.workspace_id == workspace_id,
            LearningScorerTestOpportunityProofRecord.attempt_id == attempt_id,
            LearningScorerTestOpportunityProofRecord.fold_id == proof.fold_id,
        )
        existing = await self._session.scalar(lookup)
        if existing is not None:
            self.validate_record(existing)
            if (
                existing.evaluation_run_id != evaluation_run_id
                or existing.canonical_proof != proof.canonical_json
                or existing.proof_hash != proof.content_hash
            ):
                raise LearningScorerTestOpportunityProofConflict(
                    "Attempt/fold đã có proof TEST khác; không thể mở scope mới."
                )
            await self._require_current_lease(rights_receipts)
            return LearningScorerTestOpportunityProofWriteResult(existing, proof, True)

        row = LearningScorerTestOpportunityProofRecord.from_proof(proof)
        self.validate_record(row)
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
                await self._require_current_lease(rights_receipts)
        except IntegrityError as error:
            existing = await self._session.scalar(lookup)
            if existing is None:
                raise LearningScorerTestOpportunityProofConflict(
                    "Xung đột unique proof; cần retry toàn transaction."
                ) from error
            self.validate_record(existing)
            if (
                existing.evaluation_run_id != evaluation_run_id
                or existing.canonical_proof != proof.canonical_json
                or existing.proof_hash != proof.content_hash
            ):
                raise LearningScorerTestOpportunityProofConflict(
                    "Attempt/fold đã có proof TEST khác; không thể thay thế."
                ) from error
            await self._require_current_lease(rights_receipts)
            return LearningScorerTestOpportunityProofWriteResult(existing, proof, True)
        await self._require_current_lease(rights_receipts)
        return LearningScorerTestOpportunityProofWriteResult(row, proof, False)

    async def get_for_attempt(
        self, *, workspace_id: UUID, attempt_id: UUID
    ) -> LearningScorerTestOpportunityProofRecord | None:
        """Nạp proof metadata duy nhất để receipt gateway có thể ghim hash."""
        if type(workspace_id) is not UUID or type(attempt_id) is not UUID:
            raise ValueError("Workspace và attempt phải là UUID.")
        await self._bind_workspace(workspace_id)
        record = await self._session.scalar(
            select(LearningScorerTestOpportunityProofRecord).where(
                LearningScorerTestOpportunityProofRecord.workspace_id == workspace_id,
                LearningScorerTestOpportunityProofRecord.attempt_id == attempt_id,
            )
        )
        if record is not None:
            self.validate_record(record)
        return record

    async def _build_authoritative_proof(
        self,
        *,
        workspace_id: UUID,
        evaluation_run_id: UUID,
        attempt_id: UUID,
    ) -> tuple[ScorerTestOpportunityProof, tuple[ReplayTrainingRightsReceipt, ...]]:
        attempt = await self._session.scalar(
            select(LearningTrainingAttemptRecord).where(
                LearningTrainingAttemptRecord.workspace_id == workspace_id,
                LearningTrainingAttemptRecord.id == attempt_id,
            )
        )
        scorer = await self._session.scalar(
            select(LearningScorerArtifactRecord).where(
                LearningScorerArtifactRecord.workspace_id == workspace_id,
                LearningScorerArtifactRecord.attempt_id == attempt_id,
            )
        )
        if attempt is None or scorer is None:
            raise LookupError("Thiếu attempt hoặc scorer artifact authoritative.")
        attempt.to_domain()
        from trading_bot.adapters.database.repositories.learning_scorer_artifacts import (
            LearningScorerArtifactRepository,
        )

        LearningScorerArtifactRepository.validate_record(scorer)
        if (
            attempt.workspace_id != workspace_id
            or scorer.workspace_id != workspace_id
            or attempt.normalized_dataset_id != scorer.normalized_dataset_id
            or attempt.manifest_hash != scorer.manifest_hash
            or attempt.fold_plan_hash != scorer.fold_plan_hash
            or attempt.fold_id != scorer.fold_id
            or attempt.approval_hash != scorer.approval_hash
            or scorer.state != "ready_for_evaluation"
        ):
            raise ValueError("Attempt/scorer artifact không pin cùng fold và approval.")

        dataset = await self._session.scalar(
            select(NormalizedLearningDatasetRecord).where(
                NormalizedLearningDatasetRecord.workspace_id == workspace_id,
                NormalizedLearningDatasetRecord.id == attempt.normalized_dataset_id,
                NormalizedLearningDatasetRecord.manifest_hash == attempt.manifest_hash,
                NormalizedLearningDatasetRecord.fold_plan_hash == attempt.fold_plan_hash,
            )
        )
        approval = await self._session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == workspace_id,
                LearningSplitApprovalRecord.manifest_hash == attempt.manifest_hash,
            )
        )
        if dataset is None or approval is None:
            raise LookupError("Thiếu normalized dataset hoặc split approval.")
        approved = approval.to_domain()
        if (
            dataset.approval_hash != attempt.approval_hash
            or dataset.split_policy_version != attempt.split_policy_version
            or dataset.fold_plan_hash != attempt.fold_plan_hash
            or approved.content_hash != attempt.approval_hash
            or approved.fold_plan_hash != attempt.fold_plan_hash
            or approved.split_policy_version != attempt.split_policy_version
        ):
            raise ValueError("Approval/dataset không khớp attempt scorer.")

        samples = await self._load_test_membership(
            workspace_id=workspace_id,
            manifest_hash=attempt.manifest_hash,
            fold_plan_hash=attempt.fold_plan_hash,
            fold_id=attempt.fold_id,
        )
        if not samples:
            raise ValueError("Fold TEST membership không được để trống.")
        outcome_ids = {item.outcome_id for item in samples}
        outcomes_result = await self._session.execute(
            select(
                BacktestTradeOutcomeRecord.id,
                BacktestTradeOutcomeRecord.workspace_id,
                BacktestTradeOutcomeRecord.run_id,
                BacktestTradeOutcomeRecord.trial_record_id,
                BacktestTradeOutcomeRecord.manifest_hash,
                BacktestTradeOutcomeRecord.outcome_hash,
                BacktestTradeOutcomeRecord.entry_opportunity_event_id,
                BacktestTradeOutcomeRecord.entry_candidate_id,
                BacktestTradeOutcomeRecord.entry_vector_hash,
                BacktestTradeOutcomeRecord.entry_decision_snapshot_id,
                BacktestTradeOutcomeRecord.entry_decision_at,
                BacktestTradeOutcomeRecord.entry_feature_schema_hash,
                BacktestTradeOutcomeRecord.entry_features_hash,
                BacktestTradeOutcomeRecord.entry_symbol,
            ).where(
                BacktestTradeOutcomeRecord.workspace_id == workspace_id,
                BacktestTradeOutcomeRecord.id.in_(outcome_ids),
            )
        )
        test_outcome_rows: tuple[Any, ...] = tuple(outcomes_result.all())
        if {row.id for row in test_outcome_rows} != outcome_ids:
            raise ValueError("TEST membership tham chiếu outcome thiếu hoặc sai workspace.")
        source_trials = {row.trial_record_id for row in test_outcome_rows}
        source_runs = {row.run_id for row in test_outcome_rows}
        if len(source_trials) != 1 or len(source_runs) != 1:
            raise ValueError("TEST outcomes không thuộc cùng một BASE trial/run.")
        trial_id = next(iter(source_trials))
        trial = await self._session.scalar(
            select(BacktestTrialRecord).where(
                BacktestTrialRecord.workspace_id == workspace_id,
                BacktestTrialRecord.id == trial_id,
            )
        )
        run_id = next(iter(source_runs))
        run = await self._session.scalar(
            select(BacktestRunRecord).where(
                BacktestRunRecord.workspace_id == workspace_id,
                BacktestRunRecord.id == run_id,
            )
        )
        if trial is None or run is None:
            raise LookupError("TEST outcome thiếu trial/run authoritative.")
        from trading_bot.adapters.database.learning_fold_outcome_verifier import (
            DatabaseFoldOutcomeVerifier,
        )

        DatabaseFoldOutcomeVerifier._validate_trial(trial, workspace_id=workspace_id)
        replay_manifest = DatabaseFoldOutcomeVerifier._validate_run(run, workspace_id=workspace_id)
        if (
            trial.status != "COMPLETED"
            or trial.run_id != run.id
            or trial.manifest_hash != run.manifest_hash
            or replay_manifest.learning_intent != "TRAINING_BASE"
        ):
            raise ValueError("TEST sample không bắt nguồn từ BASE run đã hoàn tất.")

        candidate_record = await self._session.scalar(
            select(LearningSplitCandidateRecord).where(
                LearningSplitCandidateRecord.workspace_id == workspace_id,
                LearningSplitCandidateRecord.trial_record_id == trial.id,
                LearningSplitCandidateRecord.manifest_hash == attempt.manifest_hash,
                LearningSplitCandidateRecord.fold_plan_hash == attempt.fold_plan_hash,
            )
        )
        if candidate_record is None:
            raise LookupError("Thiếu split candidate đã pin cửa sổ TEST authoritative.")
        candidate = candidate_record.to_domain()
        if (
            candidate.manifest.content_hash != attempt.manifest_hash
            or candidate.fold_plan.content_hash != attempt.fold_plan_hash
            or candidate.proof_record_id is None
            or candidate.trial_record_id != trial.id
        ):
            raise ValueError("Split candidate không khớp manifest/fold/trial.")
        fold = self._select_fold(candidate, attempt.fold_id)
        if {sample.sample_id for sample in samples} != set(fold.test_sample_ids):
            raise ValueError("Normalized TEST assignments lệch fold plan đã duyệt.")
        self._validate_test_samples(candidate, samples, test_outcome_rows)

        source_lock = f"research-source:{workspace_id}:{SOURCE_ID}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": source_lock},
        )
        await self._verify_approved_base_proofs(
            workspace_id=workspace_id, trial_record_id=trial.id, candidate=candidate
        )
        rights_receipts = await DatabaseReplayTrainingRightsGate(self._session).authorize(
            workspace_id=workspace_id,
            bundle_hash=replay_manifest.dataset_hash,
        )
        if not rights_receipts or any(
            receipt.purpose.value != "training" for receipt in rights_receipts
        ):
            raise PermissionError("Thiếu current TRAINING rights cho mọi child bundle.")

        source_repository = CandidateUniverseProofRepository(self._session)
        (
            reference,
            timeframe,
            inventory,
            decision_grid,
            sources_complete,
            identity_scope_hash,
        ) = await source_repository._decision_source_inventory(
            workspace_id,
            replay_manifest,
            rights_receipts,
        )
        source_gap_count = await self._source_gap_count(
            workspace_id=workspace_id,
            reference=reference,
            inventory=inventory,
        )
        if sources_complete != (source_gap_count == 0):
            raise ValueError("Source completeness lệch số identity bar bị thiếu.")
        expected_events = await self._expected_test_events(
            session=self._session,
            workspace_id=workspace_id,
            replay_manifest=replay_manifest,
            candidate=candidate,
            fold=fold,
            reference=reference,
            timeframe=timeframe,
            inventory=inventory,
            decision_grid=decision_grid,
        )
        if not expected_events:
            raise ValueError("Test window không có decision event trong pinned bundle.")
        opportunity_rows = await self._load_trial_opportunities(
            workspace_id=workspace_id,
            trial_record_id=trial.id,
        )
        opportunity_by_event = {row.event_id: row for row in opportunity_rows}
        outcome_by_event = {
            row.entry_opportunity_event_id: row
            for row in await self._load_trial_outcome_metadata(
                workspace_id=workspace_id,
                trial_record_id=trial.id,
            )
            if row.entry_opportunity_event_id is not None
        }
        membership_by_outcome = {sample.outcome_id: sample for sample in samples}
        self._validate_test_vector_schemas(
            scorer_vector_schema_hash=scorer.vector_schema_hash,
            samples=samples,
            expected_events=expected_events,
            opportunity_by_event=opportunity_by_event,
        )
        test_entries, missing_events, unlabeled_candidates, candidate_ids = (
            _build_test_inventory_entries(
                expected_events=expected_events,
                opportunity_by_event=opportunity_by_event,
                outcome_by_event=outcome_by_event,
                membership_by_outcome=membership_by_outcome,
            )
        )

        membership_digest = _membership_digest(samples, test_outcome_rows)
        source_scope_hash = digest(
            canonical(
                {
                    "identity_scope_hash": identity_scope_hash,
                    "training_rights_scope_hash": training_scope_digest(rights_receipts),
                }
            )
        )
        proof = ScorerTestOpportunityProof(
            record_id=uuid4(),
            workspace_id=workspace_id,
            evaluation_run_id=evaluation_run_id,
            attempt_id=attempt_id,
            normalized_dataset_id=attempt.normalized_dataset_id,
            manifest_hash=attempt.manifest_hash,
            fold_plan_hash=attempt.fold_plan_hash,
            fold_id=attempt.fold_id,
            approval_hash=attempt.approval_hash,
            bundle_hash=replay_manifest.dataset_hash,
            vector_schema_version=scorer.vector_schema_version or "",
            vector_schema_hash=scorer.vector_schema_hash,
            test_membership_digest=membership_digest,
            source_scope_hash=source_scope_hash,
            expected_opportunity_count=len(expected_events),
            source_gap_count=source_gap_count,
            membership_mismatch_count=missing_events,
            unlabeled_candidate_count=unlabeled_candidates,
            entries=test_entries,
        )
        if proof.candidate_count != len(candidate_ids):
            raise ValueError("Candidate inventory TEST có duplicate hoặc count sai.")
        return proof, rights_receipts

    async def _verify_approved_base_proofs(
        self,
        *,
        workspace_id: UUID,
        trial_record_id: UUID,
        candidate: LearningSplitCandidate,
    ) -> None:
        """Xác minh proof đã duyệt, rồi kiểm inventory nguồn bằng proof BASE riêng."""
        repository = CandidateUniverseProofRepository(self._session)
        approved_result = await repository.build_and_append(
            workspace_id=workspace_id, trial_record_id=trial_record_id
        )
        if (
            approved_result.record_id != candidate.proof_record_id
            or approved_result.proof.content_hash != candidate.proof_hash
        ):
            raise ValueError("BASE proof TRAINING rights/provenance khác split candidate.")
        # Candidate split ghim proof closed-trade v1; proof opportunity v2 có ID/hash
        # riêng và chỉ xác minh toàn bộ inventory trước khi dựng scope TEST.
        await repository.build_decision_opportunity(
            workspace_id=workspace_id, trial_record_id=trial_record_id
        )

    async def _load_test_membership(
        self,
        *,
        workspace_id: UUID,
        manifest_hash: str,
        fold_plan_hash: str,
        fold_id: str,
    ) -> tuple[_TestMembershipRow, ...]:
        result = await self._session.execute(
            select(
                NormalizedFoldAssignmentRecord.sample_id,
                NormalizedLearningSampleRecord.sample_hash,
                NormalizedLearningSampleRecord.outcome_id,
                NormalizedLearningSampleRecord.outcome_hash,
                NormalizedLearningSampleRecord.feature_snapshot_hash,
                NormalizedLearningSampleRecord.feature_schema_hash,
            )
            .join(
                NormalizedLearningSampleRecord,
                (
                    NormalizedLearningSampleRecord.workspace_id
                    == NormalizedFoldAssignmentRecord.workspace_id
                )
                & (
                    NormalizedLearningSampleRecord.manifest_hash
                    == NormalizedFoldAssignmentRecord.manifest_hash
                )
                & (
                    NormalizedLearningSampleRecord.fold_plan_hash
                    == NormalizedFoldAssignmentRecord.fold_plan_hash
                )
                & (
                    NormalizedLearningSampleRecord.sample_id
                    == NormalizedFoldAssignmentRecord.sample_id
                ),
            )
            .where(
                NormalizedFoldAssignmentRecord.workspace_id == workspace_id,
                NormalizedFoldAssignmentRecord.manifest_hash == manifest_hash,
                NormalizedFoldAssignmentRecord.fold_plan_hash == fold_plan_hash,
                NormalizedFoldAssignmentRecord.fold_id == fold_id,
                NormalizedFoldAssignmentRecord.role == "test",
            )
            .order_by(NormalizedFoldAssignmentRecord.sample_id)
        )
        rows = tuple(result.all())
        values = []
        for row in rows:
            try:
                outcome_id = UUID(row.outcome_id)
            except (TypeError, ValueError) as error:
                raise ValueError("Normalized TEST sample outcome_id không phải UUID.") from error
            values.append(
                _TestMembershipRow(
                    sample_id=row.sample_id,
                    sample_hash=row.sample_hash,
                    outcome_id=outcome_id,
                    outcome_hash=row.outcome_hash,
                    feature_snapshot_hash=row.feature_snapshot_hash,
                    feature_schema_hash=row.feature_schema_hash,
                )
            )
        if len({row.sample_id for row in values}) != len(values):
            raise ValueError("Normalized TEST membership sample_id bị lặp.")
        if len({row.outcome_id for row in values}) != len(values):
            raise ValueError("Normalized TEST membership outcome bị lặp.")
        return tuple(values)

    async def _load_trial_opportunities(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> tuple[Any, ...]:
        result = await self._session.execute(
            select(
                BacktestDecisionOpportunityRecord.event_id,
                BacktestDecisionOpportunityRecord.input_state,
                BacktestDecisionOpportunityRecord.strategy_state,
                BacktestDecisionOpportunityRecord.candidate_id,
                BacktestDecisionOpportunityRecord.vector_hash,
                BacktestDecisionOpportunityRecord.runtime_snapshot_id,
                BacktestDecisionOpportunityRecord.decision_at,
                BacktestDecisionOpportunityRecord.feature_values_hash,
                BacktestDecisionOpportunityRecord.feature_schema_hash,
                BacktestDecisionOpportunityRecord.symbol,
                BacktestDecisionOpportunityRecord.decision_ordinal,
            )
            .where(
                BacktestDecisionOpportunityRecord.workspace_id == workspace_id,
                BacktestDecisionOpportunityRecord.trial_record_id == trial_record_id,
            )
            .order_by(BacktestDecisionOpportunityRecord.decision_ordinal)
        )
        rows: tuple[Any, ...] = tuple(result.all())
        if len({row.event_id for row in rows}) != len(rows) or len(
            {row.decision_ordinal for row in rows}
        ) != len(rows):
            raise ValueError("BASE source opportunity event/ordinal bị lặp.")
        return rows

    async def _source_gap_count(
        self,
        *,
        workspace_id: UUID,
        reference: Any,
        inventory: dict[str, tuple[Any, ...]],
    ) -> int:
        """Đếm mỗi identity source bar thiếu trong toàn bộ bundle đã ghim."""
        from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
            BacktestDatasetManifestRepository,
        )

        manifest_repository = BacktestDatasetManifestRepository(self._session)
        gap_count = 0
        for child in reference.components:
            manifest = await manifest_repository.get_by_hash(
                workspace_id=workspace_id,
                dataset_hash=child.dataset_hash,
            )
            bars = inventory.get(child.interval)
            if (
                manifest is None
                or manifest.schema_version != 4
                or manifest.rights_workspace_id != workspace_id
                or manifest.dataset_hash != child.dataset_hash
                or bars is None
            ):
                raise ValueError("Source child manifest/inventory lệch TEST bundle.")
            missing = manifest.expected_bar_count - len(bars)
            if missing < 0 or (not manifest.is_ready and missing == 0):
                raise ValueError("Source gap count không khớp child manifest authoritative.")
            gap_count += missing
        return gap_count

    async def _load_trial_outcome_metadata(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> tuple[Any, ...]:
        result = await self._session.execute(
            select(
                BacktestTradeOutcomeRecord.id,
                BacktestTradeOutcomeRecord.outcome_hash,
                BacktestTradeOutcomeRecord.entry_opportunity_event_id,
                BacktestTradeOutcomeRecord.entry_candidate_id,
                BacktestTradeOutcomeRecord.entry_vector_hash,
                BacktestTradeOutcomeRecord.entry_decision_snapshot_id,
                BacktestTradeOutcomeRecord.entry_decision_at,
                BacktestTradeOutcomeRecord.entry_feature_schema_hash,
                BacktestTradeOutcomeRecord.entry_features_hash,
                BacktestTradeOutcomeRecord.entry_symbol,
            ).where(
                BacktestTradeOutcomeRecord.workspace_id == workspace_id,
                BacktestTradeOutcomeRecord.trial_record_id == trial_record_id,
            )
        )
        rows = tuple(result.all())
        by_event = [
            row.entry_opportunity_event_id for row in rows if row.entry_opportunity_event_id
        ]
        if len(by_event) != len(set(by_event)):
            raise ValueError("BASE outcome entry event bị lặp.")
        return rows

    @staticmethod
    def _select_fold(candidate: LearningSplitCandidate, fold_id: str) -> ManifestFoldSelection:
        folds = tuple(item for item in candidate.fold_plan.folds if item.fold_id == fold_id)
        if len(folds) != 1:
            raise ValueError("fold_id không tồn tại duy nhất trong split candidate.")
        return folds[0]

    @staticmethod
    def _validate_test_samples(
        candidate: LearningSplitCandidate,
        samples: tuple[_TestMembershipRow, ...],
        outcome_rows: tuple[Any, ...],
    ) -> None:
        outcomes = {row.id: row for row in outcome_rows}
        manifest_samples = {sample.sample_id: sample for sample in candidate.manifest.samples}
        for sample in samples:
            manifest_sample = manifest_samples.get(sample.sample_id)
            outcome = outcomes.get(sample.outcome_id)
            if (
                manifest_sample is None
                or outcome is None
                or manifest_sample.outcome_id != str(sample.outcome_id)
                or manifest_sample.outcome_hash != sample.outcome_hash
                or outcome.outcome_hash != sample.outcome_hash
                or manifest_sample.feature_snapshot_hash != sample.feature_snapshot_hash
                or outcome.entry_features_hash != sample.feature_snapshot_hash
                or outcome.entry_opportunity_event_id is None
                or outcome.entry_candidate_id is None
                or outcome.entry_vector_hash is None
            ):
                raise ValueError("Normalized TEST sample lệch manifest/outcome entry lineage.")

    @staticmethod
    def _validate_test_vector_schemas(
        *,
        scorer_vector_schema_hash: str | None,
        samples: tuple[_TestMembershipRow, ...],
        expected_events: tuple[tuple[str, int], ...],
        opportunity_by_event: dict[str, Any],
    ) -> None:
        """Kiểm mọi schema vector TEST dùng trong sample và decision grid."""
        if not scorer_vector_schema_hash:
            raise ValueError("Scorer vector schema hash bị thiếu.")
        if any(sample.feature_schema_hash != scorer_vector_schema_hash for sample in samples):
            raise ValueError("Normalized TEST sample lệch scorer vector schema.")
        for event_id, _ordinal in expected_events:
            opportunity = opportunity_by_event.get(event_id)
            if opportunity is None:
                continue
            if opportunity.strategy_state not in {"CANDIDATE", "NO_CANDIDATE"}:
                continue
            if opportunity.feature_schema_hash != scorer_vector_schema_hash:
                raise ValueError("Decision opportunity TEST lệch scorer vector schema.")

    @staticmethod
    async def _expected_test_events(
        *,
        session: AsyncSession,
        workspace_id: UUID,
        replay_manifest: object,
        candidate: LearningSplitCandidate,
        fold: ManifestFoldSelection,
        reference: object,
        timeframe: str,
        inventory: dict[str, tuple[Any, ...]],
        decision_grid: tuple[datetime, ...],
    ) -> tuple[tuple[str, int], ...]:
        from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
            BacktestDatasetManifestRepository,
        )
        from trading_bot.application.backtest import ReplayManifest
        from trading_bot.application.backtest_dataset_replay_bundle import (
            BacktestDatasetReplayBundleReference,
        )

        if (
            type(replay_manifest) is not ReplayManifest
            or type(reference) is not BacktestDatasetReplayBundleReference
            or replay_manifest.dataset_hash != reference.bundle_hash
            or replay_manifest.range_start != reference.evaluation_start
            or replay_manifest.range_end != reference.range_end
            or fold.split.test.start < replay_manifest.range_start
            or fold.split.test.end > replay_manifest.range_end
        ):
            raise ValueError("TEST fold window lệch replay bundle authoritative.")
        bars_by_time = {bar.open_time: bar for bar in inventory[timeframe]}
        child = next((item for item in reference.components if item.interval == timeframe), None)
        if child is None:
            raise ValueError("Bundle thiếu decision timeframe TEST.")
        child_manifest = await BacktestDatasetManifestRepository(session).get_by_hash(
            workspace_id=workspace_id, dataset_hash=child.dataset_hash
        )
        if (
            child_manifest is None
            or child_manifest.schema_version != 4
            or child_manifest.rights_workspace_id != workspace_id
            or child_manifest.interval != timeframe
            or child_manifest.symbol != reference.symbol
            or child_manifest.evaluation_start != reference.evaluation_start
            or child_manifest.range_end != reference.range_end
            or child_manifest.expected_bar_count < child_manifest.evaluation_bar_count
        ):
            raise ValueError("Decision child identity manifest không khớp TEST bundle.")
        # Dựng lại toàn bộ decision grid từ expected count manifest; observed bar rows
        # chỉ bổ sung available_at khi hiện diện, không làm co membership khi thiếu nguồn.
        step = (
            child_manifest.range_end - child_manifest.warmup_start
        ) / child_manifest.expected_bar_count
        if step <= timedelta(0):
            raise ValueError("Decision child manifest có interval không hợp lệ.")
        expected_grid = tuple(
            child_manifest.warmup_start + step * index
            for index in range(child_manifest.expected_bar_count)
            if child_manifest.evaluation_start
            <= child_manifest.warmup_start + step * index
            < child_manifest.range_end
        )
        if (
            len(expected_grid) != child_manifest.evaluation_bar_count
            or not expected_grid
            or expected_grid[0] != child_manifest.evaluation_start
            or expected_grid != decision_grid
        ):
            raise ValueError("Decision grid dựng lại lệch manifest hoặc source proof.")
        # Dùng available_at nguồn khi bar tồn tại. Bar thiếu dùng biên kết thúc ô,
        # đồng thời source_gap_count buộc proof chưa đầy đủ.
        expected = []
        for ordinal, open_time in enumerate(decision_grid):
            bar = bars_by_time.get(open_time)
            decision_at = bar.available_at if bar is not None else open_time + step
            if fold.split.test.start <= decision_at < fold.split.test.end:
                event_id = replay_decision_event_id(
                    bundle_hash=replay_manifest.dataset_hash,
                    strategy_release_id=replay_manifest.strategy_release_id,
                    decision_timeframe=timeframe,
                    decision_open_time=open_time,
                )
                expected.append((event_id, ordinal))
        if not expected:
            raise ValueError("Test window không chứa decision event pinned.")
        if len({event_id for event_id, _ in expected}) != len(expected):
            raise ValueError("Pinned TEST grid chứa event_id trùng.")
        if candidate.workspace_id != workspace_id:
            raise ValueError("Split candidate nằm ngoài workspace.")
        return tuple(expected)

    async def _require_builder_snapshot(self, workspace_id: UUID) -> None:
        await self._bind_workspace(workspace_id)
        tenant = (
            await self._session.execute(text("SELECT current_setting('app.workspace_id', true)"))
        ).scalar_one_or_none()
        role: str = (await self._session.execute(text("SELECT session_user"))).scalar_one()
        if tenant != str(workspace_id) or role != _CANDIDATE_BUILDER:
            raise LearningScorerTestOpportunityProofConflict(
                "TEST proof chỉ được dựng bởi builder tenant-bound."
            )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    async def _require_current_lease(
        self, receipts: tuple[ReplayTrainingRightsReceipt, ...]
    ) -> None:
        persisted_at: datetime | None = (
            await self._session.execute(text("SELECT clock_timestamp()"))
        ).scalar_one()
        if (
            not isinstance(persisted_at, datetime)
            or persisted_at.tzinfo is None
            or any(
                not receipt.checked_at <= persisted_at < receipt.valid_until for receipt in receipts
            )
        ):
            raise LearningScorerTestOpportunityProofConflict(
                "Current TRAINING rights lease hết hạn trước khi proof commit."
            )

    @staticmethod
    def validate_record(record: LearningScorerTestOpportunityProofRecord) -> None:
        """Tái dựng typed proof và so canonical/hash với mọi cột đã chỉ mục."""
        if type(record) is not LearningScorerTestOpportunityProofRecord:
            raise LearningScorerTestOpportunityProofConflict("Proof TEST record sai kiểu.")
        try:
            payload = json.loads(record.canonical_proof)
            canonical_payload = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if canonical_payload != record.canonical_proof:
                raise ValueError("canonical bytes không chuẩn")
            entries = tuple(
                ScorerTestOpportunityEntry(
                    stable_id_hash=item["stable_id_hash"],
                    status=item["status"],
                    candidate_id_hash=item["candidate_id_hash"],
                    outcome_id_hash=item["outcome_id_hash"],
                    outcome_hash=item["outcome_hash"],
                    sample_id_hash=item["sample_id_hash"],
                    sample_hash=item["sample_hash"],
                    vector_hash=item["vector_hash"],
                )
                for item in payload["entries"]
            )
            proof = ScorerTestOpportunityProof(
                record_id=record.id,
                workspace_id=UUID(payload["workspace_id"]),
                evaluation_run_id=UUID(payload["evaluation_run_id"]),
                attempt_id=UUID(payload["attempt_id"]),
                normalized_dataset_id=UUID(payload["normalized_dataset_id"]),
                manifest_hash=payload["manifest_hash"],
                fold_plan_hash=payload["fold_plan_hash"],
                fold_id=payload["fold_id"],
                approval_hash=payload["approval_hash"],
                bundle_hash=payload["bundle_hash"],
                vector_schema_version=payload["vector_schema_version"],
                vector_schema_hash=payload["vector_schema_hash"],
                test_membership_digest=payload["test_membership_digest"],
                source_scope_hash=payload["source_scope_hash"],
                expected_opportunity_count=payload["expected_opportunity_count"],
                source_gap_count=payload["source_gap_count"],
                membership_mismatch_count=payload["membership_mismatch_count"],
                unlabeled_candidate_count=payload["unlabeled_candidate_count"],
                entries=entries,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LearningScorerTestOpportunityProofConflict(
                "Canonical proof TEST không hợp lệ hoặc có hash sai."
            ) from error
        indexed = (
            ("workspace_id", record.workspace_id, proof.workspace_id),
            ("evaluation_run_id", record.evaluation_run_id, proof.evaluation_run_id),
            ("attempt_id", record.attempt_id, proof.attempt_id),
            ("normalized_dataset_id", record.normalized_dataset_id, proof.normalized_dataset_id),
            ("manifest_hash", record.manifest_hash, proof.manifest_hash),
            ("fold_plan_hash", record.fold_plan_hash, proof.fold_plan_hash),
            ("fold_id", record.fold_id, proof.fold_id),
            ("approval_hash", record.approval_hash, proof.approval_hash),
            ("bundle_hash", record.bundle_hash, proof.bundle_hash),
            ("vector_schema_version", record.vector_schema_version, proof.vector_schema_version),
            ("vector_schema_hash", record.vector_schema_hash, proof.vector_schema_hash),
            ("test_membership_digest", record.test_membership_digest, proof.test_membership_digest),
            ("source_scope_hash", record.source_scope_hash, proof.source_scope_hash),
            (
                "expected_opportunity_count",
                record.expected_opportunity_count,
                proof.expected_opportunity_count,
            ),
            ("candidate_count", record.candidate_count, proof.candidate_count),
            ("no_candidate_count", record.no_candidate_count, proof.no_candidate_count),
            ("input_excluded_count", record.input_excluded_count, proof.input_excluded_count),
            ("source_gap_count", record.source_gap_count, proof.source_gap_count),
            (
                "membership_mismatch_count",
                record.membership_mismatch_count,
                proof.membership_mismatch_count,
            ),
            (
                "unlabeled_candidate_count",
                record.unlabeled_candidate_count,
                proof.unlabeled_candidate_count,
            ),
            ("completeness_verified", record.completeness_verified, proof.completeness_verified),
        )
        if (
            any(expected != actual for _, expected, actual in indexed)
            or hashlib.sha256(record.canonical_proof.encode("utf-8")).hexdigest()
            != record.proof_hash
            or proof.canonical_json != record.canonical_proof
            or proof.content_hash != record.proof_hash
        ):
            raise LearningScorerTestOpportunityProofConflict(
                "Cột proof TEST lệch canonical scope/count/hash."
            )


def _membership_digest(samples: tuple[_TestMembershipRow, ...], outcomes: tuple[Any, ...]) -> str:
    outcome_by_id = {row.id: row for row in outcomes}
    members = []
    for sample in sorted(samples, key=lambda item: item.sample_id):
        outcome = outcome_by_id.get(sample.outcome_id)
        if (
            outcome is None
            or not isinstance(outcome.entry_vector_hash, str)
            or len(outcome.entry_vector_hash) != 64
        ):
            raise ValueError("TEST membership thiếu vector hash authoritative.")
        members.append(
            {
                "sample_id": sample.sample_id,
                "sample_hash": sample.sample_hash,
                "vector_hash": outcome.entry_vector_hash,
            }
        )
    return digest(canonical(members))


def _build_test_inventory_entries(
    *,
    expected_events: tuple[tuple[str, int], ...],
    opportunity_by_event: dict[str, Any],
    outcome_by_event: dict[str, Any],
    membership_by_outcome: dict[UUID, _TestMembershipRow],
) -> tuple[tuple[ScorerTestOpportunityEntry, ...], int, int, set[str]]:
    """Ghép TEST grid với exact event/candidate/outcome/vector lineage."""
    entries: list[ScorerTestOpportunityEntry] = []
    missing_events = 0
    unlabeled_candidates = 0
    candidate_ids: set[str] = set()
    for event_id, _ordinal in expected_events:
        row = opportunity_by_event.get(event_id)
        if row is None:
            missing_events += 1
            continue
        stable_id_hash = _sha256(event_id)
        if row.input_state == "INPUT_EXCLUDED" or row.strategy_state == "NOT_RUN":
            entries.append(
                ScorerTestOpportunityEntry(
                    stable_id_hash=stable_id_hash,
                    status="input_excluded",
                    candidate_id_hash=None,
                )
            )
            continue
        if row.strategy_state == "NO_CANDIDATE":
            entries.append(
                ScorerTestOpportunityEntry(
                    stable_id_hash=stable_id_hash,
                    status="no_candidate",
                    candidate_id_hash=None,
                )
            )
            continue
        if row.strategy_state != "CANDIDATE" or row.candidate_id is None:
            raise ValueError("Opportunity TEST có strategy state không hợp lệ.")
        if row.candidate_id in candidate_ids:
            raise ValueError("Candidate ID bị lặp trong decision grid TEST.")
        candidate_ids.add(row.candidate_id)
        linked_outcome = outcome_by_event.get(event_id)
        sample = None if linked_outcome is None else membership_by_outcome.get(linked_outcome.id)
        has_exact_label = not (
            linked_outcome is None
            or sample is None
            or linked_outcome.outcome_hash != sample.outcome_hash
            or linked_outcome.entry_candidate_id != row.candidate_id
            or linked_outcome.entry_vector_hash != row.vector_hash
            or linked_outcome.entry_decision_snapshot_id != row.runtime_snapshot_id
            or linked_outcome.entry_decision_at != row.decision_at
            or linked_outcome.entry_features_hash != row.feature_values_hash
            or linked_outcome.entry_feature_schema_hash != row.feature_schema_hash
            or linked_outcome.entry_symbol != row.symbol
            or sample.feature_snapshot_hash != linked_outcome.entry_features_hash
            or sample.feature_schema_hash != linked_outcome.entry_feature_schema_hash
        )
        if not has_exact_label:
            unlabeled_candidates += 1
        else:
            if linked_outcome is None or sample is None:
                raise ValueError("Exact TEST label linkage thiếu outcome hoặc sample.")
            outcome_id_hash: str | None = _sha256(str(linked_outcome.id))
            outcome_hash: str | None = linked_outcome.outcome_hash
            sample_id_hash: str | None = _sha256(sample.sample_id)
            sample_hash: str | None = sample.sample_hash
            vector_hash: str | None = linked_outcome.entry_vector_hash
        if not has_exact_label:
            outcome_id_hash = None
            outcome_hash = None
            sample_id_hash = None
            sample_hash = None
            vector_hash = None
        entries.append(
            ScorerTestOpportunityEntry(
                stable_id_hash=stable_id_hash,
                status="candidate",
                candidate_id_hash=row.candidate_id,
                outcome_id_hash=outcome_id_hash,
                outcome_hash=outcome_hash,
                sample_id_hash=sample_id_hash,
                sample_hash=sample_hash,
                vector_hash=vector_hash,
            )
        )
    return (
        tuple(sorted(entries, key=lambda item: item.stable_id_hash)),
        missing_events,
        unlabeled_candidates,
        candidate_ids,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "LearningScorerTestOpportunityProofConflict",
    "LearningScorerTestOpportunityProofRepository",
    "LearningScorerTestOpportunityProofWriteResult",
]
