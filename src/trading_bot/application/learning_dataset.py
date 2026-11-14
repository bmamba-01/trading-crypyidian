"""Tạo manifest dataset qua verifier có giới hạn trong một event loop mỗi process."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from trading_bot.domain.learning.dataset import (
    DatasetExclusion,
    DatasetManifest,
    DatasetSample,
)
from trading_bot.domain.learning.labels import _sha256, _timestamp

MAX_DATASET_MANIFEST_RECORDS = 10_000
MAX_MANIFEST_BUILD_SECONDS = 120.0
MAX_PENDING_VERIFICATION_TASKS = 32
# Chia sẻ giữa các builder; composition root dùng một event loop cho mỗi process.
_PENDING_VERIFICATION_TASKS: set[asyncio.Task[Any]] = set()
_AUTHORITATIVE_SAMPLE_FIELDS = (
    "sample_id",
    "decision_at",
    "feature_available_at",
    "outcome_start",
    "outcome_end",
    "feature_snapshot_hash",
    "outcome_id",
    "outcome_hash",
    "label_hash",
    "label_value",
    "label_version",
    "split",
    "source_dependent",
    "source_replay_bundle_hash",
    "source_use_lineage_set_hash",
    "source_use_lineage",
)


@dataclass(frozen=True, slots=True)
class DatasetEvidenceVerification:
    """Sample chuẩn do verifier dựng từ records và split policy đáng tin cậy."""

    workspace_id: UUID
    authoritative_sample: DatasetSample
    as_of: datetime
    feature_schema_version: str
    feature_version: str
    sample_schema_version: str
    split_policy_version: str
    holdout_sealed_at: datetime | None
    source_training_eligible: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        if type(self.authoritative_sample) is not DatasetSample:
            raise ValueError("authoritative_sample phải là DatasetSample.")
        for name in (
            "feature_schema_version",
            "feature_version",
            "sample_schema_version",
            "split_policy_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} không được để trống.")
        object.__setattr__(self, "as_of", _timestamp(self.as_of, "as_of"))
        if (
            self.source_training_eligible is not None
            and type(self.source_training_eligible) is not bool
        ):
            raise ValueError("source_training_eligible phải là boolean hoặc None.")
        seal = self.holdout_sealed_at
        if seal is not None:
            object.__setattr__(self, "holdout_sealed_at", _timestamp(seal, "holdout_sealed_at"))
        sample = self.authoritative_sample
        if sample.source_dependent:
            if self.source_training_eligible is None:
                raise ValueError("Sample phụ thuộc nguồn cần kết quả rights tường minh.")
            if (
                self.source_training_eligible
                and not sample.source_references
                and sample.source_replay_bundle_hash is None
            ):
                raise ValueError("Rights được phép phải ghim typed source references.")
            if (
                self.source_training_eligible
                and sample.source_replay_bundle_hash is not None
                and not sample.source_use_lineage
                and sample.source_use_lineage_set_hash is None
            ):
                raise ValueError("Rights replay TRAINING cần exact source-use lineage hash.")
        elif (
            self.source_training_eligible is not None
            or sample.source_rights_hashes
            or sample.source_revision_refs
            or sample.source_references
            or sample.source_replay_bundle_hash is not None
            or sample.source_use_lineage_set_hash is not None
            or sample.source_use_lineage
        ):
            raise ValueError("Sample WP-02 không được mang quyền hoặc lineage nguồn.")


class DatasetEvidenceVerifier(Protocol):
    """Cổng chỉ đọc trả sample chuẩn, không echo nhãn/timestamp/split của caller.

    Adapter đọc feature/outcome/label authoritative, xác minh source dependencies
    và áp dụng split assignment theo policy đã duyệt trước khi trả sample.
    """

    async def verify_sample(
        self, *, request: DatasetManifestBuildRequest, sample: DatasetSample
    ) -> DatasetEvidenceVerification | None: ...


@dataclass(frozen=True, slots=True)
class DatasetManifestBuildRequest:
    """Phạm vi và candidate do caller yêu cầu; chưa được coi là dữ liệu đã duyệt."""

    workspace_id: UUID
    query_hash: str
    symbols: tuple[str, ...]
    data_start: datetime
    data_end: datetime
    as_of: datetime
    feature_schema_version: str
    feature_version: str
    label_version: str
    sample_schema_version: str
    split_policy_version: str
    samples: tuple[DatasetSample, ...]
    exclusions: tuple[DatasetExclusion, ...]
    holdout_sealed_at: datetime | None
    retention_policy_ref: str
    manifest_version: str = "dataset-manifest-v1"


class DatasetManifestBuilder:
    """Xác minh từng candidate; thiếu evidence thì loại, không cấp quyền mặc định."""

    def __init__(
        self,
        verifier: DatasetEvidenceVerifier,
        *,
        verification_timeout_seconds: float = 5.0,
        build_timeout_seconds: float = MAX_MANIFEST_BUILD_SECONDS,
    ) -> None:
        if not isinstance(verification_timeout_seconds, (int, float)) or isinstance(
            verification_timeout_seconds, bool
        ):
            raise ValueError("verification_timeout_seconds phải là số hữu hạn dương.")
        if not 0 < verification_timeout_seconds <= 60:
            raise ValueError("verification_timeout_seconds phải nằm trong (0, 60].")
        if not isinstance(build_timeout_seconds, (int, float)) or isinstance(
            build_timeout_seconds, bool
        ):
            raise ValueError("build_timeout_seconds phải là số hữu hạn dương.")
        if not 0 < build_timeout_seconds <= MAX_MANIFEST_BUILD_SECONDS:
            raise ValueError(
                f"build_timeout_seconds phải nằm trong (0, {MAX_MANIFEST_BUILD_SECONDS}]."
            )
        self._verifier = verifier
        self._verification_timeout_seconds = float(verification_timeout_seconds)
        self._build_timeout_seconds = float(build_timeout_seconds)

    async def build(self, request: DatasetManifestBuildRequest) -> DatasetManifest:
        """Tạo manifest canonical sau kiểm tra point-in-time và provenance authoritative.

        Port phải đọc record theo workspace, xác minh toàn bộ sample, áp dụng split
        policy được duyệt, kiểm tra mọi source revision đã khả dụng ở decision time
        và quyền TRAINING còn hiệu lực tại request.as_of. Adapter không được suy ra
        quyền từ refs hoặc hash do request mang vào; sample chuẩn gồm split assignment
        và rights lineage phải do port dựng từ nguồn authoritative.
        """
        if not isinstance(request.workspace_id, UUID):
            raise ValueError("workspace_id phải là UUID.")
        as_of = _timestamp(request.as_of, "as_of")
        data_start = _timestamp(request.data_start, "data_start")
        data_end = _timestamp(request.data_end, "data_end")
        if data_start >= data_end:
            raise ValueError("data_start phải trước data_end.")
        query_hash = _sha256(request.query_hash, "query_hash")
        candidates = tuple(request.samples)
        input_exclusions = tuple(request.exclusions)
        if len(candidates) + len(input_exclusions) > MAX_DATASET_MANIFEST_RECORDS:
            raise ValueError(f"Manifest vượt giới hạn {MAX_DATASET_MANIFEST_RECORDS} record.")
        ids = [sample.sample_id for sample in candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("Candidate dataset có sample_id duplicate.")
        excluded_ids = [item.sample_id for item in input_exclusions]
        if len(excluded_ids) != len(set(excluded_ids)):
            raise ValueError("Exclusions có sample_id duplicate.")
        if set(ids) & set(excluded_ids):
            raise ValueError("sample_id đã tồn tại trong candidate và exclusion.")

        accepted: list[DatasetSample] = []
        exclusions = list(input_exclusions)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._build_timeout_seconds
        for candidate in sorted(candidates, key=lambda item: item.sample_id):
            remaining = deadline - loop.time()
            timeout = min(self._verification_timeout_seconds, max(0.0, remaining))
            evidence = None
            if timeout > 0:
                evidence = await self._verify_candidate(
                    request,
                    candidate,
                    timeout_seconds=timeout,
                )
            reason = self._verification_reason(request, candidate, evidence, as_of)
            if reason == "manifest_policy_mismatch":
                raise ValueError("Manifest policy không khớp với evidence authoritative.")
            if reason is not None:
                exclusions.append(DatasetExclusion(candidate.sample_id, (reason,)))
                continue
            assert evidence is not None
            authoritative = evidence.authoritative_sample
            reasons = self._temporal_reasons(authoritative, request, data_start, data_end, as_of)
            if reasons:
                exclusions.append(DatasetExclusion(candidate.sample_id, tuple(reasons)))
                continue
            accepted.append(authoritative)

        return DatasetManifest(
            workspace_id=request.workspace_id,
            query_hash=query_hash,
            symbols=request.symbols,
            data_start=data_start,
            data_end=data_end,
            as_of=as_of,
            feature_schema_version=request.feature_schema_version,
            feature_version=request.feature_version,
            label_version=request.label_version,
            sample_schema_version=request.sample_schema_version,
            split_policy_version=request.split_policy_version,
            samples=tuple(accepted),
            exclusions=tuple(exclusions),
            holdout_sealed_at=request.holdout_sealed_at,
            manifest_version=request.manifest_version,
            retention_policy_ref=request.retention_policy_ref,
        )

    async def _verify_candidate(
        self,
        request: DatasetManifestBuildRequest,
        sample: DatasetSample,
        *,
        timeout_seconds: float,
    ) -> DatasetEvidenceVerification | None:
        """Giám sát verifier mà không chờ task chống cancellation hoàn tất."""
        if len(_PENDING_VERIFICATION_TASKS) >= MAX_PENDING_VERIFICATION_TASKS:
            return None
        try:
            task = asyncio.create_task(
                self._verifier.verify_sample(
                    request=request,
                    sample=sample,
                )
            )
        except Exception:
            return None
        _PENDING_VERIFICATION_TASKS.add(task)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(self._finish_verification_task)
            raise
        if not done:
            task.cancel()
            task.add_done_callback(self._finish_verification_task)
            return None
        try:
            return task.result()
        except Exception:
            return None
        finally:
            _PENDING_VERIFICATION_TASKS.discard(task)

    def _finish_verification_task(self, task: asyncio.Task[Any]) -> None:
        """Thu nhận kết quả trễ và giải phóng slot khi adapter hoàn tất thật."""
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        _PENDING_VERIFICATION_TASKS.discard(task)

    @staticmethod
    def _temporal_reasons(
        sample: DatasetSample,
        request: DatasetManifestBuildRequest,
        data_start: datetime,
        data_end: datetime,
        as_of: datetime,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if not data_start <= sample.decision_at < data_end:
            reasons.append("decision_outside_data_range")
        if sample.decision_at > as_of:
            reasons.append("decision_after_as_of")
        if sample.outcome_end > as_of:
            reasons.append("outcome_incomplete_at_as_of")
        if sample.label_version != request.label_version:
            reasons.append("label_version_mismatch")
        return tuple(reasons)

    @staticmethod
    def _verification_reason(
        request: DatasetManifestBuildRequest,
        sample: DatasetSample,
        evidence: DatasetEvidenceVerification | None,
        as_of: datetime,
    ) -> str | None:
        if type(evidence) is not DatasetEvidenceVerification:
            return "provenance_unverified"
        authoritative = evidence.authoritative_sample
        if (
            evidence.workspace_id != request.workspace_id
            or authoritative.sample_id != sample.sample_id
        ):
            return "provenance_scope_mismatch"
        if evidence.as_of != as_of:
            return "provenance_as_of_mismatch"
        if (
            evidence.feature_schema_version != request.feature_schema_version
            or evidence.feature_version != request.feature_version
            or authoritative.label_version != request.label_version
            or evidence.sample_schema_version != request.sample_schema_version
            or evidence.split_policy_version != request.split_policy_version
            or evidence.holdout_sealed_at
            != (
                _timestamp(request.holdout_sealed_at, "holdout_sealed_at")
                if request.holdout_sealed_at is not None
                else None
            )
        ):
            return "manifest_policy_mismatch"
        if any(
            getattr(sample, field) != getattr(authoritative, field)
            for field in _AUTHORITATIVE_SAMPLE_FIELDS
        ):
            return "provenance_mismatch"
        if authoritative.source_dependent:
            if request.manifest_version == "dataset-manifest-v3":
                if not authoritative.source_references:
                    return "typed_source_lineage_unverified"
            elif request.manifest_version == "dataset-manifest-v4":
                if (
                    authoritative.source_replay_bundle_hash is None
                    or not authoritative.source_use_lineage
                ):
                    return "replay_bundle_lineage_unverified"
            elif request.manifest_version == "dataset-manifest-v5":
                if (
                    authoritative.source_replay_bundle_hash is None
                    or authoritative.source_use_lineage_set_hash is None
                ):
                    return "replay_bundle_lineage_unverified"
            else:
                return "typed_source_lineage_required"
            if evidence.source_training_eligible is not True:
                return "source_training_not_eligible"
        elif evidence.source_training_eligible is not None:
            return "provenance_mismatch"
        return None
