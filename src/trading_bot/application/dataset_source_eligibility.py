"""Fail-closed gate cho dataset có lineage tới nguồn research."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSourceReference
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.source_revision_access import SourceAccessEnvelope


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


class DatasetRightsLedgerReader(Protocol):
    """Đọc invalidation và event rights đang chờ trong tenant hiện hành."""

    async def find_invalidation(
        self,
        *,
        workspace_id: UUID,
        manifest_hash: str,
        sample_id: str,
        reference: DatasetSourceReference,
    ) -> str | None: ...

    async def has_unprocessed_rights_event(
        self, *, workspace_id: UUID, source_id: str, item_id: str
    ) -> bool: ...


class SourceAccessEnvelopeReader(Protocol):
    """Nala authoritative access envelope cho một source/item theo DB clock."""

    async def select_access_envelope(
        self,
        *,
        workspace_id: UUID,
        source_id: str,
        item_id: str,
        purpose: SourceUsePurpose,
    ) -> SourceAccessEnvelope: ...


class DatasetSourceEligibilityGate:
    """Kiểm tra lineage, event backlog và quyền hiện hành trước mỗi lần dùng."""

    def __init__(
        self,
        rights_ledger: DatasetRightsLedgerReader,
        source_access: SourceAccessEnvelopeReader,
    ) -> None:
        self._rights_ledger = rights_ledger
        self._source_access = source_access

    async def assert_eligible(
        self, *, workspace_id: UUID, manifest: DatasetManifest
    ) -> tuple[DatasetSourceRightsEvidence, ...]:
        """Từ chối quyền lỗi thời; trả metadata receipt sau khi mọi reference hợp lệ."""
        if type(workspace_id) is not UUID or type(manifest) is not DatasetManifest:
            raise DatasetSourceEligibilityDenied("request_invalid")
        if manifest.workspace_id != workspace_id:
            raise DatasetSourceEligibilityDenied("workspace_mismatch")

        source_samples = tuple(sample for sample in manifest.samples if sample.source_dependent)
        if not source_samples:
            return ()
        if manifest.manifest_version != "dataset-manifest-v3":
            raise DatasetSourceEligibilityDenied("legacy_source_lineage_ambiguous")

        evidence: list[DatasetSourceRightsEvidence] = []
        for sample in source_samples:
            if not sample.source_references:
                raise DatasetSourceEligibilityDenied(
                    "source_lineage_missing", sample_id=sample.sample_id
                )
            for reference in sample.source_references:
                try:
                    if await self._rights_ledger.has_unprocessed_rights_event(
                        workspace_id=workspace_id,
                        source_id=reference.source_id,
                        item_id=reference.item_id,
                    ):
                        raise DatasetSourceEligibilityDenied(
                            "rights_event_unprocessed", sample_id=sample.sample_id
                        )
                    invalidation = await self._rights_ledger.find_invalidation(
                        workspace_id=workspace_id,
                        manifest_hash=manifest.content_hash,
                        sample_id=sample.sample_id,
                        reference=reference,
                    )
                    if invalidation is not None:
                        raise DatasetSourceEligibilityDenied(
                            invalidation, sample_id=sample.sample_id
                        )
                    envelope = await self._source_access.select_access_envelope(
                        workspace_id=workspace_id,
                        source_id=reference.source_id,
                        item_id=reference.item_id,
                        purpose=SourceUsePurpose.TRAINING,
                    )
                except DatasetSourceEligibilityDenied:
                    raise
                except Exception:
                    raise DatasetSourceEligibilityDenied(
                        "live_access_unavailable", sample_id=sample.sample_id
                    ) from None

                if not envelope.decision.permitted:
                    raise DatasetSourceEligibilityDenied(
                        "live_access_denied", sample_id=sample.sample_id
                    )
                current_revision_id = (
                    envelope.revision.revision.revision_id
                    if envelope.revision is not None
                    else None
                )
                if (
                    current_revision_id != reference.revision_id
                    or envelope.historical_policy_id != reference.policy_id
                    or envelope.historical_policy_sequence != reference.policy_sequence
                    or envelope.current_policy_id != reference.policy_id
                    or envelope.current_policy_sequence != reference.policy_sequence
                    or envelope.rights_hash != reference.rights_hash
                ):
                    raise DatasetSourceEligibilityDenied(
                        "live_rights_mismatch", sample_id=sample.sample_id
                    )
                evaluated_at = getattr(envelope, "evaluated_at", None)
                if (
                    not isinstance(evaluated_at, datetime)
                    or evaluated_at.tzinfo is None
                    or evaluated_at.utcoffset() is None
                ):
                    raise DatasetSourceEligibilityDenied(
                        "live_access_unavailable", sample_id=sample.sample_id
                    )
                evidence.append(
                    DatasetSourceRightsEvidence(
                        workspace_id=workspace_id,
                        manifest_hash=manifest.content_hash,
                        sample_id=sample.sample_id,
                        reference=reference,
                        checked_at=evaluated_at,
                    )
                )
        return tuple(
            sorted(
                evidence,
                key=lambda item: (
                    item.reference.source_id,
                    item.sample_id,
                    item.reference.item_id,
                    item.reference.revision_id,
                ),
            )
        )
