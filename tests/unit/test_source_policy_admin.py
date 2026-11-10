from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.source_policy_admin import (
    SourceApprovalCommand,
    SourceCreateCommand,
    SourcePolicyAdminService,
    SourceRevisionCommand,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

NOW = datetime.now(UTC)
WORKSPACE = uuid4()
BUDGET = SourceBudget(timedelta(hours=1), 3, 1000, Decimal("1"), "USD")


def principal(role=Role.OWNER, *, user=None, membership=None, mfa=None):
    return Principal(
        user or uuid4(), membership or uuid4(), WORKSPACE, frozenset({role}), mfa or NOW
    )


class Repository:
    def __init__(self):
        self.entries = {}
        self.calls = 0
        self.fail_once = False
        self.memberships = {}

    async def list_entries(self, *, workspace_id):
        return tuple(self.entries.values())

    async def get_entry(self, *, workspace_id, source_id):
        return self.entries.get(source_id)

    async def get_quota_snapshot(self, *, workspace_id, source_id):
        return None

    async def get_source_snapshot(self, *, workspace_id, source_id):
        entry = self.entries.get(source_id)
        return (entry, None) if entry else None

    async def membership_id_for_user(self, *, workspace_id, user_id):
        return self.memberships.get(user_id)

    async def find_revisions_by_idempotency_key(self, *, workspace_id, key):
        prefix = f"policy:{key.hex}:"
        return tuple(
            revision
            for entry in self.entries.values()
            for revision in entry.revisions
            if revision.policy.policy_id.startswith(prefix)
        )

    async def append_revision_and_observe(self, *, workspace_id, revision):
        self.calls += 1
        entry = self.entries.get(revision.policy.source_id)
        duplicate = entry is not None and any(
            r.policy.policy_id == revision.policy.policy_id for r in entry.revisions
        )
        if not duplicate:
            self.entries[revision.policy.source_id] = (
                SourceRegistryEntry(revision.policy.source_id, (*entry.revisions, revision))
                if entry
                else SourceRegistryEntry(revision.policy.source_id, (revision,))
            )
        if self.fail_once:
            self.fail_once = False
            raise LookupError("receipt incomplete")
        from trading_bot.adapters.database.repositories.source_registry import (
            SourceRegistryAppendResult,
        )

        return SourceRegistryAppendResult(revision, duplicate)


@pytest.mark.asyncio
async def test_write_approve_separation_mfa_and_recovery():
    repo = Repository()
    service = SourcePolicyAdminService(repo)
    owner = principal()
    reviewer = principal(Role.ANALYST_REVIEWER)
    create = SourceCreateCommand(
        "calendar", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
    )
    key = uuid4()
    with pytest.raises(PermissionError):
        await service.for_principal(reviewer).create_source(create, key)
    with pytest.raises(PermissionError):
        await service.for_principal(
            replace(owner, mfa_verified_at=NOW - timedelta(hours=1))
        ).create_source(create, key)
    repo.fail_once = True
    with pytest.raises(LookupError):
        await service.for_principal(owner).create_source(create, key)
    assert repo.calls == 1
    await service.for_principal(owner).create_source(create, key)
    assert repo.calls == 2
    assert len(repo.entries["calendar"].revisions) == 1
    with pytest.raises(ValueError, match="payload khác"):
        await service.for_principal(owner).create_source(replace(create, publisher="Changed"), key)

    pending = await service.for_principal(owner).submit_pending(
        "calendar",
        SourceRevisionCommand(SourcePolicyState.RIGHTS_PENDING, "Submitted", evidence_ref="terms"),
        uuid4(),
    )
    with pytest.raises(PermissionError):
        await service.for_principal(owner).approve_pending(
            "calendar",
            pending.policy.policy_id,
            SourceApprovalCommand(
                frozenset({SourceUsePurpose.METADATA}), NOW + timedelta(days=1), "Approved"
            ),
            uuid4(),
        )
    approval = await service.for_principal(reviewer).approve_pending(
        "calendar",
        pending.policy.policy_id,
        SourceApprovalCommand(
            frozenset({SourceUsePurpose.METADATA}), NOW + timedelta(days=1), "Approved"
        ),
        uuid4(),
    )
    assert approval.state is SourcePolicyState.VERIFIED
    assert approval.actor_id == pending.actor_id
    assert approval.approved_by == str(reviewer.user_id)
    with pytest.raises(ValueError, match="hiện hành"):
        await service.for_principal(reviewer).approve_pending(
            "calendar",
            pending.policy.policy_id,
            SourceApprovalCommand(
                frozenset({SourceUsePurpose.METADATA}), NOW + timedelta(days=1), "Again"
            ),
            uuid4(),
        )


@pytest.mark.asyncio
async def test_pending_retry_payload_membership_and_approval_observer_recovery() -> None:
    repo = Repository()
    service = SourcePolicyAdminService(repo)
    owner = principal()
    create = SourceCreateCommand(
        "feed", "Publisher", "https://example.org/feed", "GET", "public", BUDGET, "Candidate"
    )
    await service.for_principal(owner).create_source(create, uuid4())
    key = uuid4()
    command = SourceRevisionCommand(
        SourcePolicyState.RIGHTS_PENDING, "Submitted", evidence_ref="terms"
    )
    repo.fail_once = True
    with pytest.raises(LookupError, match="receipt"):
        await service.for_principal(owner).submit_pending("feed", command, key)
    assert len(repo.entries["feed"].revisions) == 2
    pending = await service.for_principal(owner).submit_pending("feed", command, key)
    assert pending.sequence == 2
    with pytest.raises(ValueError, match="payload khác"):
        await service.for_principal(owner).submit_pending(
            "feed", replace(command, evidence_ref="changed"), key
        )
    shared_membership = principal(Role.ANALYST_REVIEWER, membership=owner.membership_id)
    approval_command = SourceApprovalCommand(
        frozenset({SourceUsePurpose.METADATA}), NOW + timedelta(days=1), "Approved"
    )
    with pytest.raises(PermissionError, match="Reviewer"):
        await service.for_principal(shared_membership).approve_pending(
            "feed", pending.policy.policy_id, approval_command, uuid4()
        )
    reviewer = principal(Role.ANALYST_REVIEWER)
    approval_key = uuid4()
    repo.fail_once = True
    with pytest.raises(LookupError, match="receipt"):
        await service.for_principal(reviewer).approve_pending(
            "feed", pending.policy.policy_id, approval_command, approval_key
        )
    assert len(repo.entries["feed"].revisions) == 3
    approved = await service.for_principal(reviewer).approve_pending(
        "feed", pending.policy.policy_id, approval_command, approval_key
    )
    assert approved.sequence == 3
    assert len(repo.entries["feed"].revisions) == 3
    assert len(await service.for_principal(reviewer).list_sources()) == 1
    assert (await service.for_principal(reviewer).get_source("feed"))[0].current == approved


@pytest.mark.asyncio
async def test_candidate_key_is_stable_and_reuse_with_changed_payload_conflicts() -> None:
    repo = Repository()
    owner = principal()
    facade = SourcePolicyAdminService(repo).for_principal(owner)
    command = SourceCreateCommand(
        "initial", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
    )
    key = uuid4()
    revision = await facade.create_source(command, key)
    assert revision.policy.policy_id == "initial:policy:1"
    assert (await facade.create_source(command, key)).policy.policy_id == revision.policy.policy_id
    assert (
        await facade.create_source(command, uuid4())
    ).policy.policy_id == revision.policy.policy_id
    with pytest.raises(ValueError, match="payload khác"):
        await facade.create_source(replace(command, publisher="Changed"), key)


@pytest.mark.asyncio
async def test_retry_rejects_changed_optional_payload_even_when_effective_revision_matches() -> (
    None
):
    repo = Repository()
    owner = principal()
    facade = SourcePolicyAdminService(repo).for_principal(owner)
    create = SourceCreateCommand(
        "defaults", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
    )
    await facade.create_source(create, uuid4())
    key = uuid4()
    command = SourceRevisionCommand(
        SourcePolicyState.RIGHTS_PENDING, "Submitted", evidence_ref="terms"
    )
    await facade.submit_pending("defaults", command, key)
    with pytest.raises(ValueError, match="payload khác"):
        await facade.submit_pending("defaults", replace(command, publisher="Publisher"), key)


@pytest.mark.asyncio
async def test_legacy_pending_actor_requires_workspace_membership_resolution() -> None:
    repo = Repository()
    owner = principal()
    reviewer = principal(Role.ANALYST_REVIEWER, membership=owner.membership_id)
    facade = SourcePolicyAdminService(repo).for_principal(reviewer)
    create = SourceRegistryEntry.discover(
        source_id="legacy",
        publisher="Publisher",
        endpoint_url="https://example.org/",
        access_method="GET",
        auth_scope="public",
        budget=BUDGET,
        actor_id=str(owner.user_id),
        at=NOW,
        reason="Candidate",
    )
    pending = create.transition(
        policy_id="legacy-pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id=str(owner.user_id),
        at=NOW,
        reason="Submitted",
        evidence_ref="terms",
    )
    repo.entries["legacy"] = pending
    command = SourceApprovalCommand(
        frozenset({SourceUsePurpose.METADATA}), NOW + timedelta(days=1), "Approved"
    )
    with pytest.raises(PermissionError, match="membership"):
        await facade.approve_pending("legacy", "legacy-pending", command, uuid4())
    repo.memberships[owner.user_id] = owner.membership_id
    with pytest.raises(PermissionError, match="Reviewer"):
        await facade.approve_pending("legacy", "legacy-pending", command, uuid4())


@pytest.mark.asyncio
async def test_revision_key_reuse_across_sources_conflicts_before_append() -> None:
    repo = Repository()
    owner = principal()
    facade = SourcePolicyAdminService(repo).for_principal(owner)
    for source_id in ("first", "second"):
        await facade.create_source(
            SourceCreateCommand(
                source_id, "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
            ),
            uuid4(),
        )
    key = uuid4()
    command = SourceRevisionCommand(
        SourcePolicyState.RIGHTS_PENDING, "Submitted", evidence_ref="terms"
    )
    first = await facade.submit_pending("first", command, key)
    count = repo.calls
    with pytest.raises(ValueError, match="payload khác"):
        await facade.submit_pending("second", command, key)
    assert repo.calls == count
    assert len(repo.entries["second"].revisions) == 1
    retry = await facade.submit_pending("first", command, key)
    assert retry.policy.policy_id == first.policy.policy_id
    assert repo.calls == count + 1
