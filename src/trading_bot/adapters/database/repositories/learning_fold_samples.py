"""Lưu normalized payload bằng candidate builder principal; không cấp quyền trainer."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import (
    DatasetManifestRecord,
    LearningFoldResourceProfileRecord,
    LearningSplitApprovalRecord,
    NormalizedFoldAssignmentRecord,
    NormalizedLearningDatasetRecord,
    NormalizedLearningSampleRecord,
)
from trading_bot.adapters.database.models.learning_source_training_receipt_entities import (
    LearningSourceTrainingReceiptRecord,
)
from trading_bot.adapters.database.repositories.dataset_manifest_eligibility import (
    DatasetManifestEligibilityRepository,
)
from trading_bot.adapters.database.repositories.source_items import SourceItemRepository
from trading_bot.application.dataset_source_eligibility import (
    DatasetSourceEligibilityDenied,
    DatasetSourceEligibilityGate,
    DatasetSourceRightsEvidence,
)
from trading_bot.application.learning_fold_plan import ManifestFoldPlan, build_manifest_fold_plan
from trading_bot.application.learning_fold_samples import (
    NormalizedLearningDataset,
    normalized_learning_dataset_hash,
)
from trading_bot.domain.learning.approval import LearningSplitApproval
from trading_bot.domain.learning.dataset import DatasetManifest


class NormalizedLearningDatasetConflict(RuntimeError):
    """Normalized dataset không khớp manifest/approval đã seal hoặc đã lưu."""


@dataclass(frozen=True, slots=True)
class NormalizedLearningDatasetWriteResult:
    """Kết quả append chỉ trả header, không trả payload feature/label."""

    record: NormalizedLearningDatasetRecord
    is_duplicate: bool


class LearningFoldSampleRepository:
    """Ghi sample normalized và fold assignment trong một transaction builder."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_normalized_dataset(
        self,
        *,
        manifest: DatasetManifest,
        fold_plan: ManifestFoldPlan,
        approval: LearningSplitApproval,
        dataset: NormalizedLearningDataset,
    ) -> NormalizedLearningDatasetWriteResult:
        """Lưu payload duy nhất/sample và membership chính xác của mọi fold."""
        _validate_input(manifest, fold_plan, approval, dataset)
        await self._bind_workspace(manifest.workspace_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {
                "lock_key": (
                    f"learning-normalized-dataset:{manifest.workspace_id}:"
                    f"{manifest.content_hash}:{fold_plan.content_hash}"
                )
            },
        )

        stored_manifest = await self._session.scalar(
            select(DatasetManifestRecord).where(
                DatasetManifestRecord.workspace_id == manifest.workspace_id,
                DatasetManifestRecord.manifest_hash == manifest.content_hash,
            )
        )
        if (
            stored_manifest is None
            or stored_manifest.to_domain().canonical_json != manifest.canonical_json
        ):
            raise NormalizedLearningDatasetConflict(
                "Manifest authoritative chưa seal hoặc canonical bytes không khớp."
            )
        stored_approval = await self._session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == manifest.workspace_id,
                LearningSplitApprovalRecord.manifest_hash == manifest.content_hash,
            )
        )
        if stored_approval is None or stored_approval.to_domain() != approval:
            raise NormalizedLearningDatasetConflict("Approval lưu trữ không khớp dataset.")

        rights_evidence = await self._assert_source_eligibility(manifest)

        existing = await self._session.scalar(
            select(NormalizedLearningDatasetRecord).where(
                NormalizedLearningDatasetRecord.workspace_id == manifest.workspace_id,
                NormalizedLearningDatasetRecord.manifest_hash == manifest.content_hash,
                NormalizedLearningDatasetRecord.fold_plan_hash == fold_plan.content_hash,
            )
        )
        if existing is not None:
            await self._verify_existing(existing, dataset, approval)
            await self._verify_source_receipts(existing, rights_evidence)
            return NormalizedLearningDatasetWriteResult(existing, True)

        record = NormalizedLearningDatasetRecord.from_domain(
            dataset, approval_hash=approval.content_hash
        )
        self._session.add(record)
        await self._session.flush()
        self._session.add_all(
            NormalizedLearningSampleRecord.from_domain(dataset, sample)
            for sample in dataset.samples
        )
        await self._session.flush()
        self._session.add_all(
            NormalizedFoldAssignmentRecord.from_domain(dataset, assignment)
            for assignment in dataset.assignments
        )
        await self._session.flush()
        self._session.add_all(
            LearningFoldResourceProfileRecord.from_dataset(
                dataset,
                normalized_dataset_id=record.id,
                fold_id=fold.fold_id,
            )
            for fold in fold_plan.folds
        )
        await self._session.flush()
        rights_evidence = await self._assert_source_eligibility(manifest)
        self._session.add_all(
            LearningSourceTrainingReceiptRecord.from_evidence(
                evidence,
                normalized_dataset_id=record.id,
                fold_plan_hash=fold_plan.content_hash,
            )
            for evidence in rights_evidence
        )
        await self._session.flush()
        await self._assert_source_eligibility(manifest)
        return NormalizedLearningDatasetWriteResult(record, False)

    async def _assert_source_eligibility(
        self, manifest: DatasetManifest
    ) -> tuple[DatasetSourceRightsEvidence, ...]:
        """Kiểm nguồn dưới đúng khóa writer, giữ khóa tới commit caller hiện hành."""
        if not any(sample.source_dependent for sample in manifest.samples):
            return ()
        if not self._session.in_transaction():
            raise NormalizedLearningDatasetConflict(
                "Source-dependent normalized rows cần transaction builder đang mở."
            )
        isolation: object = (
            await self._session.execute(text("SHOW transaction_isolation"))
        ).scalar_one()
        tenant = (
            await self._session.execute(text("SELECT current_setting('app.workspace_id', true)"))
        ).scalar_one_or_none()
        role: object = (await self._session.execute(text("SELECT session_user"))).scalar_one()
        if (
            isolation != "read committed"
            or tenant != str(manifest.workspace_id)
            or role != "crypidian_candidate_builder"
        ):
            raise NormalizedLearningDatasetConflict(
                "Source-dependent rows cần builder tenant-bound ở READ COMMITTED."
            )
        for lock_key in await _source_advisory_lock_keys(
            self._session, manifest.workspace_id, manifest
        ):
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
        gate = DatasetSourceEligibilityGate(
            DatasetManifestEligibilityRepository(self._session),
            SourceItemRepository(self._session),
        )
        try:
            return await gate.assert_eligible(
                workspace_id=manifest.workspace_id,
                manifest=manifest,
            )
        except DatasetSourceEligibilityDenied as error:
            raise NormalizedLearningDatasetConflict(
                f"Source TRAINING rights bị từ chối ({error.reason_code})."
            ) from None

    async def _verify_source_receipts(
        self,
        dataset: NormalizedLearningDatasetRecord,
        evidence: tuple[DatasetSourceRightsEvidence, ...],
    ) -> None:
        """Retry chỉ chấp nhận receipt lineage/hash đã ghi ở lần đầu."""
        result = await self._session.scalars(
            select(LearningSourceTrainingReceiptRecord).where(
                LearningSourceTrainingReceiptRecord.workspace_id == dataset.workspace_id,
                LearningSourceTrainingReceiptRecord.normalized_dataset_id == dataset.id,
            )
        )
        stored = {
            (
                row.sample_id,
                row.source_id,
                row.item_id,
                row.revision_id,
                row.policy_id,
                row.policy_sequence,
                row.purpose,
                row.rights_hash,
            )
            for row in result.all()
        }
        expected = {
            (
                item.sample_id,
                item.reference.source_id,
                item.reference.item_id,
                item.reference.revision_id,
                item.reference.policy_id,
                item.reference.policy_sequence,
                item.reference.purpose.value,
                item.reference.rights_hash,
            )
            for item in evidence
        }
        if stored != expected:
            raise NormalizedLearningDatasetConflict(
                "Source rights receipt không khớp với lineage hiện hành."
            )

    async def _verify_existing(
        self,
        record: NormalizedLearningDatasetRecord,
        dataset: NormalizedLearningDataset,
        approval: LearningSplitApproval,
    ) -> None:
        if (
            record.content_hash != dataset.content_hash
            or record.approval_hash != approval.content_hash
            or record.split_policy_version != dataset.split_policy_version
            or record.sample_count != len(dataset.samples)
            or record.assignment_count != len(dataset.assignments)
        ):
            raise NormalizedLearningDatasetConflict(
                "Normalized dataset đã lưu có header khác payload được yêu cầu."
            )
        sample_count = await self._session.scalar(
            select(func.count())
            .select_from(NormalizedLearningSampleRecord)
            .where(
                NormalizedLearningSampleRecord.workspace_id == dataset.workspace_id,
                NormalizedLearningSampleRecord.manifest_hash == dataset.manifest_hash,
                NormalizedLearningSampleRecord.fold_plan_hash == dataset.fold_plan_hash,
            )
        )
        assignment_count = await self._session.scalar(
            select(func.count())
            .select_from(NormalizedFoldAssignmentRecord)
            .where(
                NormalizedFoldAssignmentRecord.workspace_id == dataset.workspace_id,
                NormalizedFoldAssignmentRecord.manifest_hash == dataset.manifest_hash,
                NormalizedFoldAssignmentRecord.fold_plan_hash == dataset.fold_plan_hash,
            )
        )
        if sample_count != record.sample_count or assignment_count != record.assignment_count:
            raise NormalizedLearningDatasetConflict(
                "Số hàng con normalized không khớp header bất biến."
            )
        expected_profiles = {
            fold_id: LearningFoldResourceProfileRecord.from_dataset(
                dataset,
                normalized_dataset_id=record.id,
                fold_id=fold_id,
            )
            for fold_id in {item.fold_id for item in dataset.assignments}
        }
        stored_profiles_result = await self._session.scalars(
            select(LearningFoldResourceProfileRecord).where(
                LearningFoldResourceProfileRecord.workspace_id == dataset.workspace_id,
                LearningFoldResourceProfileRecord.normalized_dataset_id == record.id,
            )
        )
        stored_profiles = {item.fold_id: item for item in stored_profiles_result.all()}
        if set(stored_profiles) != set(expected_profiles) or any(
            (
                stored_profiles[fold_id].training_samples,
                stored_profiles[fold_id].validation_samples,
                stored_profiles[fold_id].feature_count,
                stored_profiles[fold_id].feature_schema_hash,
                stored_profiles[fold_id].profile_hash,
            )
            != (
                expected.training_samples,
                expected.validation_samples,
                expected.feature_count,
                expected.feature_schema_hash,
                expected.profile_hash,
            )
            for fold_id, expected in expected_profiles.items()
        ):
            raise NormalizedLearningDatasetConflict(
                "Fold resource profile không khớp normalized rows đã lưu."
            )

    async def _bind_workspace(self, workspace_id: UUID) -> None:
        await self._session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )


def _validate_input(
    manifest: DatasetManifest,
    fold_plan: ManifestFoldPlan,
    approval: LearningSplitApproval,
    dataset: NormalizedLearningDataset,
) -> None:
    if any(
        (
            type(manifest) is not DatasetManifest,
            type(fold_plan) is not ManifestFoldPlan,
            type(approval) is not LearningSplitApproval,
            type(dataset) is not NormalizedLearningDataset,
        )
    ):
        raise ValueError("Cần manifest, fold plan, approval và normalized dataset typed.")
    canonical_plan = build_manifest_fold_plan(
        manifest,
        folds=tuple(selection.split for selection in fold_plan.folds),
        expected_split_policy_version=manifest.split_policy_version,
        evaluation_cutoff=fold_plan.evaluation_cutoff,
    )
    if canonical_plan != fold_plan:
        raise ValueError("Fold plan không tái dựng được từ manifest authoritative.")
    if (
        dataset.workspace_id != manifest.workspace_id
        or dataset.manifest_hash != manifest.content_hash
        or dataset.split_policy_version != manifest.split_policy_version
        or dataset.fold_plan_hash != canonical_plan.content_hash
        or approval.workspace_id != manifest.workspace_id
        or approval.manifest_hash != manifest.content_hash
        or approval.split_policy_version != manifest.split_policy_version
        or approval.fold_plan_hash != canonical_plan.content_hash
    ):
        raise NormalizedLearningDatasetConflict(
            "Dataset/approval không khớp manifest và fold plan."
        )
    if dataset.content_hash != normalized_learning_dataset_hash(dataset):
        raise ValueError("Hash normalized dataset không khớp payload canonical.")

    expected_roles: dict[tuple[str, str], str] = {}
    for fold in canonical_plan.folds:
        for role, sample_ids in (
            ("train", fold.train_sample_ids),
            ("validation", fold.validation_sample_ids),
            ("test", fold.test_sample_ids),
        ):
            for sample_id in sample_ids:
                expected_roles[(fold.fold_id, sample_id)] = role
    actual_roles = {(item.fold_id, item.sample_id): item.role for item in dataset.assignments}
    if actual_roles != expected_roles or len(dataset.assignments) != len(expected_roles):
        raise ValueError("Assignment normalized không khớp toàn bộ membership canonical của fold.")
    expected_sample_ids = {sample_id for _, sample_id in expected_roles}
    sample_by_id = {sample.sample_id: sample for sample in manifest.samples}
    normalized_by_id = {sample.sample_id: sample for sample in dataset.samples}
    if (
        len(normalized_by_id) != len(dataset.samples)
        or set(normalized_by_id) != expected_sample_ids
    ):
        raise ValueError("Payload normalized không khớp tập sample được chọn trong fold plan.")
    for sample_id, normalized in normalized_by_id.items():
        original = sample_by_id[sample_id]
        if (
            normalized.outcome_id != original.outcome_id
            or normalized.outcome_hash != original.outcome_hash
            or normalized.feature_snapshot_hash != original.feature_snapshot_hash
            or normalized.label_hash != original.label_hash
            or normalized.label_value != original.label_value
        ):
            raise ValueError("Payload normalized lệch lineage feature/label của manifest.")


async def _source_advisory_lock_keys(
    session: AsyncSession, workspace_id: UUID, manifest: DatasetManifest
) -> tuple[str, ...]:
    """Sinh khóa WP05 theo collation mặc định giống cổng quyền PostgreSQL."""
    source_ids = {
        reference.source_id
        for sample in manifest.samples
        if sample.source_dependent
        for reference in sample.source_references
    }
    if not source_ids:
        return ()
    result = await session.execute(
        text(
            "SELECT DISTINCT source_id "
            "FROM unnest(CAST(:source_ids AS text[])) AS source_keys(source_id) "
            "ORDER BY source_id"
        ),
        {"source_ids": list(source_ids)},
    )
    ordered_source_ids: list[str] = list(result.scalars().all())
    return tuple(f"research-source:{workspace_id}:{source_id}" for source_id in ordered_source_ids)


__all__ = [
    "LearningFoldSampleRepository",
    "NormalizedLearningDatasetConflict",
    "NormalizedLearningDatasetWriteResult",
]
