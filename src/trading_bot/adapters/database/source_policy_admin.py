"""Adapter đọc policy/quota và ghi policy với quyền admin-web hiện hành."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.source_registry import (
    SourceRegistryAppendResult,
    SourceRegistryRepository,
)
from trading_bot.adapters.database.source_observer import PostCommitSourceObserver
from trading_bot.application.source_policy_admin import SourceLicenseEvidence
from trading_bot.domain.research.source_registry import (
    SourcePolicyRevision,
    SourceQuotaSnapshot,
    SourceRegistryEntry,
)


class DatabaseSourcePolicyAdminRepository:
    """Tách cổng admin khỏi factory ghi quota ingestion."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._observer = PostCommitSourceObserver(sessions)

    async def list_entries(self, *, workspace_id: UUID) -> tuple[SourceRegistryEntry, ...]:
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).list_entries(workspace_id=workspace_id)

    async def get_entry(self, *, workspace_id: UUID, source_id: str) -> SourceRegistryEntry | None:
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).get_entry(
                workspace_id=workspace_id, source_id=source_id
            )

    async def get_quota_snapshot(
        self, *, workspace_id: UUID, source_id: str
    ) -> SourceQuotaSnapshot | None:
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).get_quota_snapshot(
                workspace_id=workspace_id, source_id=source_id
            )

    async def get_source_snapshot(
        self, *, workspace_id: UUID, source_id: str
    ) -> tuple[SourceRegistryEntry, SourceQuotaSnapshot | None] | None:
        """Giữ history/quota trong cùng session và transaction."""
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).get_source_snapshot(
                workspace_id=workspace_id, source_id=source_id
            )

    async def find_revisions_by_idempotency_key(
        self, *, workspace_id: UUID, key: UUID
    ) -> tuple[SourcePolicyRevision, ...]:
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).find_revisions_by_idempotency_key(
                workspace_id=workspace_id, key=key
            )

    async def membership_id_for_user(self, *, workspace_id: UUID, user_id: UUID) -> UUID | None:
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).membership_id_for_user(
                workspace_id=workspace_id, user_id=user_id
            )

    async def append_revision_and_observe(
        self, *, workspace_id: UUID, revision: SourcePolicyRevision
    ) -> SourceRegistryAppendResult:
        return await self._observer.append_revision_and_observe(
            workspace_id=workspace_id, revision=revision
        )

    async def append_license_evidence(
        self, *, workspace_id: UUID, evidence: SourceLicenseEvidence
    ) -> SourceLicenseEvidence:
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).append_license_evidence(
                workspace_id=workspace_id, evidence=evidence
            )

    async def evidence_for_pending(
        self, *, workspace_id: UUID, source_id: str, policy_id: str
    ) -> SourceLicenseEvidence | None:
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).evidence_for_pending(
                workspace_id=workspace_id, source_id=source_id, policy_id=policy_id
            )

    async def evidence_by_id(
        self, *, workspace_id: UUID, source_id: str, evidence_id: UUID
    ) -> SourceLicenseEvidence | None:
        async with self._sessions() as session, session.begin():
            return await SourceRegistryRepository(session).evidence_by_id(
                workspace_id=workspace_id,
                source_id=source_id,
                evidence_id=evidence_id,
            )
