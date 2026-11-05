"""Fail-closed gate cho dataset có lineage tới nguồn research."""

from __future__ import annotations

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

    async def assert_eligible(self, *, workspace_id: UUID, manifest: DatasetManifest) -> None:
        """Từ chối thiếu lineage, invalidation, event chưa xử lý hoặc quyền đã đổi."""
        if type(workspace_id) is not UUID or type(manifest) is not DatasetManifest:
            raise DatasetSourceEligibilityDenied("request_invalid")
        if manifest.workspace_id != workspace_id:
            raise DatasetSourceEligibilityDenied("workspace_mismatch")

        source_samples = tuple(sample for sample in manifest.samples if sample.source_dependent)
        if not source_samples:
            return
        if manifest.manifest_version != "dataset-manifest-v3":
            raise DatasetSourceEligibilityDenied("legacy_source_lineage_ambiguous")

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
