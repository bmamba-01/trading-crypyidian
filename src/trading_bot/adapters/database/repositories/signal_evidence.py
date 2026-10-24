"""Kho shared signal evidence bất biến, scope workspace, hỗ trợ tra cứu as-of."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.signal_entities import SharedSignalEvidenceRecord
from trading_bot.domain.market.signal_evidence import (
    SIGNAL_EVIDENCE_SCHEMA_VERSION,
    SharedSignalEvidence,
)


class SignalEvidenceRepositoryConflict(RuntimeError):
    """Cùng ID/hash nhưng payload khác, hoặc hàng đã lưu không còn khớp hash."""


@dataclass(frozen=True, slots=True)
class EvidenceAppendResult:
    """Kết quả append evidence mới hoặc nhận diện lần ghi lặp."""

    evidence_id: UUID
    evidence_hash: str
    duplicate: bool


class SharedSignalEvidenceRepository:
    """Mọi truy vấn nhận workspace tường minh; transaction do caller quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, evidence: SharedSignalEvidence) -> EvidenceAppendResult:
        """Append bất biến; ghi lặp cùng nội dung trả lại hàng hiện có."""
        canonical_json = evidence.canonical_json()
        existing = await self._find(evidence)
        if existing is not None:
            return _existing_result(existing, evidence, canonical_json)
        row = SharedSignalEvidenceRecord(
            id=evidence.evidence_id,
            workspace_id=evidence.workspace_id,
            symbol=evidence.symbol,
            setup_type=evidence.setup_type,
            observed_at=evidence.observed_at,
            available_at=evidence.available_at,
            validity_end=evidence.validity_end,
            feature_snapshot_id=evidence.feature_snapshot_id,
            source_refs=list(evidence.source_refs),
            model_release_id=evidence.model_release_id,
            evidence_hash=evidence.evidence_hash,
            schema_version=SIGNAL_EVIDENCE_SCHEMA_VERSION,
            canonical_json=canonical_json,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            existing = await self._find(evidence)
            if existing is None:
                raise SignalEvidenceRepositoryConflict(
                    "Không thể append evidence do xung đột ràng buộc khác."
                ) from error
            return _existing_result(existing, evidence, canonical_json)
        return EvidenceAppendResult(evidence.evidence_id, evidence.evidence_hash, duplicate=False)

    async def get(self, *, workspace_id: UUID, evidence_id: UUID) -> SharedSignalEvidence | None:
        """Đọc evidence theo ID trong workspace và xác minh hash/cột đã lưu."""
        row = await self._session.scalar(
            select(SharedSignalEvidenceRecord).where(
                SharedSignalEvidenceRecord.workspace_id == workspace_id,
                SharedSignalEvidenceRecord.id == evidence_id,
            )
        )
        return None if row is None else restore_evidence(row)

    async def get_as_of(
        self,
        *,
        workspace_id: UUID,
        symbol: str,
        setup_type: str,
        observed_at: datetime,
        as_of: datetime,
    ) -> SharedSignalEvidence | None:
        """Revision mới nhất của một quan sát đã khả dụng tại `as_of`; bỏ revision muộn hơn."""
        for name, value in (("observed_at", observed_at), ("as_of", as_of)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} phải có múi giờ.")
        row = await self._session.scalar(
            select(SharedSignalEvidenceRecord)
            .where(
                SharedSignalEvidenceRecord.workspace_id == workspace_id,
                SharedSignalEvidenceRecord.symbol == symbol,
                SharedSignalEvidenceRecord.setup_type == setup_type,
                SharedSignalEvidenceRecord.observed_at == observed_at,
                SharedSignalEvidenceRecord.available_at <= as_of,
            )
            .order_by(
                SharedSignalEvidenceRecord.available_at.desc(),
                SharedSignalEvidenceRecord.evidence_hash.desc(),
            )
            .limit(1)
        )
        return None if row is None else restore_evidence(row)

    async def _find(self, evidence: SharedSignalEvidence) -> SharedSignalEvidenceRecord | None:
        return await self._session.scalar(
            select(SharedSignalEvidenceRecord).where(
                SharedSignalEvidenceRecord.workspace_id == evidence.workspace_id,
                or_(
                    SharedSignalEvidenceRecord.id == evidence.evidence_id,
                    SharedSignalEvidenceRecord.evidence_hash == evidence.evidence_hash,
                ),
            )
        )


def restore_evidence(row: SharedSignalEvidenceRecord) -> SharedSignalEvidence:
    """Dựng lại evidence từ canonical JSON và đối chiếu với từng cột quan hệ."""
    try:
        evidence = SharedSignalEvidence.from_canonical_json(row.canonical_json)
    except (KeyError, TypeError, ValueError) as error:
        raise SignalEvidenceRepositoryConflict("Payload evidence đã lưu không hợp lệ.") from error
    stored = (
        row.id,
        row.workspace_id,
        row.symbol,
        row.setup_type,
        row.observed_at,
        row.available_at,
        row.validity_end,
        row.feature_snapshot_id,
        tuple(sorted(row.source_refs)),
        row.model_release_id,
        row.evidence_hash,
    )
    restored = (
        evidence.evidence_id,
        evidence.workspace_id,
        evidence.symbol,
        evidence.setup_type,
        evidence.observed_at,
        evidence.available_at,
        evidence.validity_end,
        evidence.feature_snapshot_id,
        evidence.source_refs,
        evidence.model_release_id,
        evidence.evidence_hash,
    )
    if stored != restored:
        raise SignalEvidenceRepositoryConflict("Cột evidence không khớp hash/payload canonical.")
    return evidence


def _existing_result(
    existing: SharedSignalEvidenceRecord,
    evidence: SharedSignalEvidence,
    canonical_json: str,
) -> EvidenceAppendResult:
    if (
        existing.id != evidence.evidence_id
        or existing.evidence_hash != evidence.evidence_hash
        or existing.canonical_json != canonical_json
    ):
        raise SignalEvidenceRepositoryConflict("Evidence ID/hash đã tồn tại với payload khác.")
    return EvidenceAppendResult(evidence.evidence_id, evidence.evidence_hash, duplicate=True)
