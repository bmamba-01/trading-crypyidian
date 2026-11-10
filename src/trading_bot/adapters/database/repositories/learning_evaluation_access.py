"""Gọi database gateway để cấp payload evaluator qua receipt một lần."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models import LearningEvaluationAccessReceiptRecord
from trading_bot.domain.learning.dataset import DatasetManifest


class LearningEvaluationAccessConflict(RuntimeError):
    """Run đã tiêu thụ receipt hoặc không khớp scope/fingerprint authoritative."""


@dataclass(frozen=True, slots=True)
class LearningEvaluationAccess:
    """Payload manifest đã xác minh cùng receipt và hash approval trong DB."""

    receipt_id: UUID
    manifest: DatasetManifest
    split_policy_version: str
    fold_plan_hash: str
    approval_hash: str


class LearningEvaluationAccessRepository:
    """Repository chỉ dùng gateway SQL; không SELECT payload manifest/outcome trực tiếp."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open(
        self,
        *,
        workspace_id: UUID,
        manifest_hash: str,
        evaluation_run_id: UUID,
        request_fingerprint: str,
    ) -> LearningEvaluationAccess:
        """Tạo receipt trong transaction hiện hành và nhận manifest đã seal."""
        if type(workspace_id) is not UUID or type(evaluation_run_id) is not UUID:
            raise ValueError("Workspace và evaluation run phải là UUID.")
        if not _is_sha256(manifest_hash) or not _is_sha256(request_fingerprint):
            raise ValueError("Manifest hash và request fingerprint phải là SHA-256.")
        try:
            result = await self._session.execute(
                text(
                    "SELECT * FROM public.wp06_open_learning_evaluation("
                    "target_workspace => CAST(:workspace_id AS uuid), "
                    "target_manifest_hash => CAST(:manifest_hash AS text), "
                    "target_evaluation_run_id => CAST(:evaluation_run_id AS uuid), "
                    "target_request_fingerprint => CAST(:request_fingerprint AS text))"
                ),
                {
                    "workspace_id": str(workspace_id),
                    "manifest_hash": manifest_hash,
                    "evaluation_run_id": str(evaluation_run_id),
                    "request_fingerprint": request_fingerprint,
                },
            )
            row = result.one()
        except DBAPIError as error:
            message = str(error.orig).upper()
            failure_code = next(
                (
                    token
                    for token in (
                        "UQ_LEARNING_EVALUATION_ACCESS_RECEIPT_RUN",
                        "WP06_EVALUATION_MANIFEST_NOT_FOUND",
                        "WP06_EVALUATION_SOURCE_RIGHTS_FAIL_CLOSED",
                        "WP06_SOURCE_TRAINING_RIGHTS_CALLER_DENIED",
                        "WP06_SOURCE_TRAINING_RIGHTS_WORKSPACE_DENIED",
                        "WP06_SOURCE_TRAINING_RIGHTS_SCOPE_INVALID",
                        "WP06_SOURCE_TRAINING_RIGHTS_MANIFEST_MISSING",
                        "WP06_SOURCE_TRAINING_RIGHTS_LINEAGE_INVALID",
                        "WP06_SOURCE_TRAINING_RIGHTS_BUILDER_RECEIPT_MISMATCH",
                        "WP06_SOURCE_TRAINING_RIGHTS_INVALIDATED",
                        "WP06_SOURCE_TRAINING_RIGHTS_OBSERVATION_MISSING",
                        "WP06_SOURCE_TRAINING_RIGHTS_POLICY_DENIED",
                        "WP06_SOURCE_TRAINING_RIGHTS_ITEM_DENIED",
                        "WP06_EVALUATION_APPROVAL_NOT_FOUND",
                        "WP06_EVALUATION_NORMALIZED_DATASET_NOT_FOUND",
                        "WP06_EVALUATION_FOLD_MEMBERSHIP_MISMATCH",
                        "WP06_EVALUATION_RECEIPT_NOT_FOUND",
                        "WP06_EVALUATOR_CALLER_DENIED",
                        "WP06_EVALUATOR_WORKSPACE_DENIED",
                        "WP06_EVALUATOR_REQUEST_FINGERPRINT_INVALID",
                    )
                    if token in message
                ),
                None,
            )
            if failure_code is not None:
                raise LearningEvaluationAccessConflict(
                    f"Không thể mở receipt evaluator (mã {failure_code})."
                ) from None
            raise

        try:
            canonical = row.canonical_manifest
            manifest = DatasetManifest.from_canonical_json(
                canonical,
                expected_content_hash=manifest_hash,
            )
            if json.loads(canonical) != row.manifest:
                raise ValueError("JSONB manifest khác canonical bytes.")
            if manifest.workspace_id != workspace_id:
                raise ValueError("Manifest workspace khác request.")
            receipt_id = row.receipt_id
            policy = row.split_policy_version
            fold_plan_hash = row.fold_plan_hash
            approval_hash = row.approval_hash
            if (
                type(receipt_id) is not UUID
                or not isinstance(policy, str)
                or not _is_sha256(fold_plan_hash)
                or not _is_sha256(approval_hash)
            ):
                raise ValueError("Metadata receipt evaluator không hợp lệ.")
        except AttributeError, TypeError, ValueError, json.JSONDecodeError:
            raise LearningEvaluationAccessConflict(
                "Gateway evaluator trả manifest hoặc receipt không hợp lệ."
            ) from None
        return LearningEvaluationAccess(
            receipt_id=receipt_id,
            manifest=manifest,
            split_policy_version=policy,
            fold_plan_hash=fold_plan_hash,
            approval_hash=approval_hash,
        )

    async def get_for_run(
        self,
        *,
        workspace_id: UUID,
        evaluation_run_id: UUID,
    ) -> LearningEvaluationAccessReceiptRecord | None:
        """Đọc metadata receipt để retry report đã persist, không mở lại payload."""
        if type(workspace_id) is not UUID or type(evaluation_run_id) is not UUID:
            raise ValueError("Workspace và evaluation run phải là UUID.")
        return await self._session.scalar(
            select(LearningEvaluationAccessReceiptRecord).where(
                LearningEvaluationAccessReceiptRecord.workspace_id == workspace_id,
                LearningEvaluationAccessReceiptRecord.evaluation_run_id == evaluation_run_id,
            )
        )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
