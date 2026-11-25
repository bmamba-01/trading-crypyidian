"""Fail-closed gate cho dataset có lineage tới nguồn research."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSourceReference


class DatasetSourceEligibilityDenied(RuntimeError):
    """Từ chối dùng manifest khi trạng thái quyền không thể được xác minh."""

    def __init__(self, reason_code: str, *, sample_id: str | None = None) -> None:
        self.reason_code = reason_code
        self.sample_id = sample_id
        scope = f"sample {sample_id}: " if sample_id is not None else ""
        super().__init__(f"{scope}{reason_code}")


@dataclass(frozen=True, slots=True)
class DatasetSourceRightsEvidence:
    """Bằng chứng metadata về lần kiểm quyền; không thay thế cổng quyền hiện hành."""

    workspace_id: UUID
    manifest_hash: str
    sample_id: str
    reference: DatasetSourceReference
    checked_at: datetime

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id evidence phải là UUID.")
        if (
            not isinstance(self.manifest_hash, str)
            or len(self.manifest_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.manifest_hash)
        ):
            raise ValueError("manifest_hash evidence phải là SHA-256 hex thường.")
        if not isinstance(self.sample_id, str) or not self.sample_id.strip():
            raise ValueError("sample_id evidence không được để trống.")
        if type(self.reference) is not DatasetSourceReference:
            raise ValueError("reference evidence phải là DatasetSourceReference.")
        if (
            not isinstance(self.checked_at, datetime)
            or self.checked_at.tzinfo is None
            or self.checked_at.utcoffset() is None
        ):
            raise ValueError("checked_at phải có timezone.")
        object.__setattr__(self, "checked_at", self.checked_at.astimezone(UTC))

    @property
    def canonical_json(self) -> str:
        """Chuỗi canonical chỉ gồm lineage, purpose và thời điểm DB đã kiểm."""
        return json.dumps(
            {
                "checked_at": self.checked_at.isoformat(),
                "manifest_hash": self.manifest_hash,
                "reference": {
                    "item_id": self.reference.item_id,
                    "policy_id": self.reference.policy_id,
                    "policy_sequence": self.reference.policy_sequence,
                    "purpose": self.reference.purpose.value,
                    "revision_id": self.reference.revision_id,
                    "rights_hash": self.reference.rights_hash,
                    "source_id": self.reference.source_id,
                },
                "sample_id": self.sample_id,
                "workspace_id": str(self.workspace_id),
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def evidence_hash(self) -> str:
        """SHA-256 của canonical receipt metadata."""
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


class DatasetSourceEligibilityGate:
    """Fail closed với source-item lineage chưa đủ phạm vi cho normalized TRAINING."""

    async def assert_eligible(
        self, *, workspace_id: UUID, manifest: DatasetManifest
    ) -> tuple[DatasetSourceRightsEvidence, ...]:
        """Chỉ source-free data đi qua; source data cần exact replay lineage và gate riêng."""
        if type(workspace_id) is not UUID or type(manifest) is not DatasetManifest:
            raise DatasetSourceEligibilityDenied("request_invalid")
        if manifest.workspace_id != workspace_id:
            raise DatasetSourceEligibilityDenied("workspace_mismatch")

        source_samples = tuple(sample for sample in manifest.samples if sample.source_dependent)
        if not source_samples:
            return ()
        if manifest.manifest_version != "dataset-manifest-v3":
            raise DatasetSourceEligibilityDenied("legacy_source_lineage_ambiguous")
        # Source-reference v3 chỉ ghim item/revision/policy; nó không chứng minh symbol,
        # timeframe, khoảng dữ liệu hay MarketBar đã dùng để tạo normalized payload.
        # Chỉ replay bundle v5 có typed source-use lineage mới đủ điều kiện TRAINING;
        # đường v5 được xử lý bởi replay rights gate riêng.
        raise DatasetSourceEligibilityDenied("source_use_lineage_missing")
