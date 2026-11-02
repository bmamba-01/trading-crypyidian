from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.database.repositories.source_registry import (
    SourceQuotaReservationResult,
)
from trading_bot.application.source_registry import SourceRegistryService
from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.domain.research.source_policy import (
    SourcePolicyState,
    SourceRightsPolicy,
    SourceUsePurpose,
)
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourcePolicyRevision,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
WORKSPACE_ID = UUID("24cc9185-40ba-4819-845b-b2164b78d827")


def principal(
    role: Role, *, member_id: UUID | None = None, user_id: UUID | None = None
) -> Principal:
    return Principal(
        user_id=user_id or uuid4(),
        membership_id=member_id or uuid4(),
        workspace_id=WORKSPACE_ID,
        roles=frozenset({role}),
        mfa_verified_at=NOW,
    )


def revision(
    *, state: SourcePolicyState = SourcePolicyState.RIGHTS_PENDING
) -> SourcePolicyRevision:
    verified = state is SourcePolicyState.VERIFIED
    policy = SourceRightsPolicy(
        policy_id="source-1-policy-2",
        source_id="source-1",
        state=state,
        allowed_purposes=frozenset({SourceUsePurpose.METADATA}) if verified else frozenset(),
        evidence_ref="https://example.test/terms" if verified else None,
        reviewed_at=NOW - timedelta(minutes=1) if verified else None,
        valid_until=NOW + timedelta(days=30) if verified else None,
    )
    return SourcePolicyRevision(
        sequence=2,
        policy=policy,
        publisher="Example publisher",
        endpoint_url="https://example.test/api/events",
        access_method="HTTPS_API",
        auth_scope="public",
        attribution="Example publisher",
        retention_ref="metadata-only",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=10,
            max_bytes=1000,
            max_cost=Decimal("1.00"),
            cost_unit="credits",
        ),
        created_at=NOW - timedelta(minutes=1),
        actor_id="forged-request-actor",
        reason="Review source policy",
        approved_by="forged-request-reviewer" if verified else None,
    )


class Repository:
    def __init__(self) -> None:
        self.revisions: list[tuple[UUID, SourcePolicyRevision]] = []
        self.reservations: list[dict[str, object]] = []

    async def append_revision_and_observe(
        self, *, workspace_id: UUID, revision: SourcePolicyRevision
    ):
        self.revisions.append((workspace_id, revision))
        return SimpleNamespace(revision=revision, is_duplicate=False)

    async def reserve_quota(self, **kwargs: object) -> SourceQuotaReservationResult:
        self.reservations.append(kwargs)
        return SourceQuotaReservationResult(uuid4(), NOW, 3, False)


def service(repository: Repository) -> SourceRegistryService:
    return SourceRegistryService(repository)  # type: ignore[arg-type]


def test_application_rejects_raw_transaction_scoped_repository():
    from tests.unit.test_source_registry_repository import FakeSession

    from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository

    with pytest.raises(TypeError, match="append_revision_and_observe"):
        SourceRegistryService(SourceRegistryRepository(FakeSession()))


@pytest.mark.asyncio
async def test_post_commit_observation_failure_is_not_swallowed():
    class Unobserved(Repository):
        async def append_revision_and_observe(self, **kwargs):
            raise LookupError("Source đã commit nhưng receipt chưa hoàn tất.")

    with pytest.raises(LookupError, match="receipt"):
        await service(Unobserved()).for_principal(principal(Role.OWNER)).append_revision(revision())


def test_permissions_separate_source_policy_author_and_reviewer() -> None:
    assert authorize(principal(Role.OWNER), Permission.RESEARCH_SOURCE_WRITE)
    assert authorize(principal(Role.OWNER), Permission.RESEARCH_SOURCE_APPROVE)
    assert authorize(principal(Role.ANALYST_REVIEWER), Permission.RESEARCH_SOURCE_APPROVE)
    assert not authorize(principal(Role.ANALYST_REVIEWER), Permission.RESEARCH_SOURCE_WRITE)
    assert not authorize(principal(Role.OPERATOR), Permission.RESEARCH_SOURCE_APPROVE)


@pytest.mark.asyncio
async def test_append_revision_uses_authenticated_actor_and_reviewer_not_request_ids() -> None:
    repository = Repository()
    actor = principal(Role.OWNER)
    reviewer = principal(Role.ANALYST_REVIEWER)
    candidate = revision(state=SourcePolicyState.VERIFIED)

    saved = (
        await service(repository).for_principal(actor).append_revision(candidate, reviewer=reviewer)
    )

    assert saved.actor_id == str(actor.user_id)
    assert saved.approved_by == str(reviewer.user_id)
    assert repository.revisions == [(WORKSPACE_ID, saved)]


@pytest.mark.asyncio
async def test_verified_revision_requires_a_different_authenticated_reviewer() -> None:
    repository = Repository()
    actor = principal(Role.OWNER)
    principal_service = service(repository).for_principal(actor)

    with pytest.raises(PermissionError, match="reviewer"):
        await principal_service.append_revision(revision(state=SourcePolicyState.VERIFIED))
    with pytest.raises(PermissionError, match="khác người tạo"):
        await principal_service.append_revision(
            revision(state=SourcePolicyState.VERIFIED), reviewer=actor
        )
    same_user_other_membership = principal(
        Role.ANALYST_REVIEWER, user_id=actor.user_id, member_id=uuid4()
    )
    with pytest.raises(PermissionError, match="khác người tạo"):
        await principal_service.append_revision(
            revision(state=SourcePolicyState.VERIFIED), reviewer=same_user_other_membership
        )
    assert repository.revisions == []


@pytest.mark.asyncio
async def test_revision_write_rejects_unauthorized_or_cross_workspace_principals() -> None:
    repository = Repository()
    pending = revision()
    with pytest.raises(PermissionError):
        await (
            service(repository)
            .for_principal(principal(Role.ANALYST_REVIEWER))
            .append_revision(pending)
        )

    actor = principal(Role.OWNER)
    foreign_reviewer = replace(principal(Role.ANALYST_REVIEWER), workspace_id=uuid4())
    with pytest.raises(PermissionError, match="workspace"):
        await (
            service(repository)
            .for_principal(actor)
            .append_revision(revision(state=SourcePolicyState.VERIFIED), reviewer=foreign_reviewer)
        )
    assert repository.revisions == []


@pytest.mark.asyncio
async def test_quota_reservation_requires_source_use_permission_and_preserves_scope() -> None:
    repository = Repository()
    source_service = service(repository)
    operator = source_service.for_principal(principal(Role.OPERATOR))
    with pytest.raises(PermissionError):
        await operator.reserve_usage(
            source_id="source-1",
            idempotency_key="fetch-1",
            purpose=SourceUsePurpose.METADATA,
            reserved_bytes=10,
            reserved_cost=Decimal("0.1"),
        )
    assert repository.reservations == []

    owner = source_service.for_principal(principal(Role.OWNER))
    result = await owner.reserve_usage(
        source_id="source-1",
        idempotency_key="fetch-1",
        purpose=SourceUsePurpose.METADATA,
        reserved_bytes=10,
        reserved_cost=Decimal("0.1"),
    )
    assert result.policy_sequence == 3
    assert repository.reservations == [
        {
            "workspace_id": WORKSPACE_ID,
            "source_id": "source-1",
            "idempotency_key": "fetch-1",
            "purpose": SourceUsePurpose.METADATA,
            "reserved_bytes": 10,
            "reserved_cost": Decimal("0.1"),
        }
    ]
