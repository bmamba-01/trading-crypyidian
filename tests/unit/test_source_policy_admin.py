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
        self.evidence = []

    async def append_license_evidence(self, *, workspace_id, evidence):
        self.evidence.append(evidence)
        return evidence

    async def evidence_for_pending(self, *, workspace_id, source_id, policy_id):
        return next(
            (e for e in self.evidence if e.source_id == source_id and e.policy_id == policy_id),
            None,
        )

    async def evidence_by_id(self, *, workspace_id, source_id, evidence_id):
        if workspace_id != WORKSPACE:
            return None
        return next(
            (e for e in self.evidence if e.source_id == source_id and e.evidence_id == evidence_id),
            None,
        )

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
        pending_command("calendar"),
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
    await upload_fixture(service.for_principal(owner), "calendar", pending)
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
    command = pending_command("feed")
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
    await upload_fixture(service.for_principal(owner), "feed", pending)
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
    command = pending_command("defaults")
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
    command = pending_command("first")
    first = await facade.submit_pending("first", command, key)
    count = repo.calls
    with pytest.raises(ValueError, match="payload khác"):
        await facade.submit_pending("second", pending_command("second"), key)
    assert repo.calls == count
    assert len(repo.entries["second"].revisions) == 1
    retry = await facade.submit_pending("first", command, key)
    assert retry.policy.policy_id == first.policy.policy_id
    assert repo.calls == count + 1


def license_scope(source_id="feed"):
    from trading_bot.domain.research.license_scope import SourceLicenseScope

    return SourceLicenseScope(
        source_id=source_id,
        provider="PROVIDER",
        product="CALENDAR",
        venue="PUBLIC",
        contract="RESEARCH",
        symbols=("BTCUSDT",),
        intervals=("1h",),
        data_start=NOW - timedelta(days=1),
        data_end=NOW + timedelta(days=1),
        allowed_purposes=(SourceUsePurpose.METADATA,),
        allowed_outputs=("aggregate_report",),
        derived_use=("aggregate_reporting",),
        attribution="Nguồn fixture",
        max_retention_days=None,
    )


@pytest.mark.asyncio
async def test_pending_requires_exact_typed_scope_and_preserves_identity():
    repo = Repository()
    facade = SourcePolicyAdminService(repo).for_principal(principal())
    await facade.create_source(
        SourceCreateCommand(
            "feed", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
        ),
        uuid4(),
    )
    with pytest.raises(ValueError, match="Pending"):
        await facade.submit_pending(
            "feed",
            SourceRevisionCommand(
                SourcePolicyState.RIGHTS_PENDING, "Missing scope", evidence_ref="archive:1"
            ),
            uuid4(),
        )
    scope = license_scope()
    command = SourceRevisionCommand(
        SourcePolicyState.RIGHTS_PENDING,
        "Submit",
        evidence_ref="archive:1",
        license_scope=scope,
        provider=scope.provider,
        product=scope.product,
        venue=scope.venue,
        contract=scope.contract,
        attribution=scope.attribution,
    )
    with pytest.raises(ValueError, match="Pending"):
        await facade.submit_pending("feed", replace(command, provider="OTHER"), uuid4())
    pending = await facade.submit_pending("feed", command, uuid4())
    assert pending.license_scope == scope
    assert pending.provider == scope.provider
    disabled = await facade.change_disabled_state(
        "feed", SourcePolicyState.BLOCKED, "Stop", uuid4()
    )
    assert disabled.provider == scope.provider


@pytest.mark.asyncio
async def test_upload_stream_hashes_boundary_and_rejects_without_append():
    import hashlib

    from starlette.requests import ClientDisconnect

    from trading_bot.application.source_policy_admin import LicenseEvidenceUploadError

    repo = Repository()
    facade = SourcePolicyAdminService(repo).for_principal(principal())
    scope = license_scope()
    await facade.create_source(
        SourceCreateCommand(
            "feed", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
        ),
        uuid4(),
    )
    pending = await facade.submit_pending(
        "feed",
        SourceRevisionCommand(
            SourcePolicyState.RIGHTS_PENDING,
            "Submit",
            evidence_ref="archive:1",
            license_scope=scope,
            provider=scope.provider,
            product=scope.product,
            venue=scope.venue,
            contract=scope.contract,
            attribution=scope.attribution,
        ),
        uuid4(),
    )

    async def chunks(*values):
        for value in values:
            if isinstance(value, Exception):
                raise value
            yield value

    for values, length, media, expected in (
        ((b"",), None, "text/plain", 400),
        ((b"a",), "bad", "text/plain", 400),
        ((b"a",), "-1", "text/plain", 400),
        ((b"a",), "2", "text/plain", 400),
        ((b"a",), None, "image/png", 415),
        ((b"a", ClientDisconnect()), None, "text/plain", 400),
        ((b"a" * (10 * 1024 * 1024), b"b"), "1", "text/plain", 413),
        ((b"a",), str(10 * 1024 * 1024 + 1), "text/plain", 413),
    ):
        with pytest.raises(LicenseEvidenceUploadError) as error:
            await facade.upload_license_evidence(
                "feed",
                pending.policy.policy_id,
                chunks(*values),
                media_type=media,
                content_length=length,
                idempotency_key=uuid4(),
            )
        assert error.value.status_code == expected
        assert repo.evidence == []
    body = b"a" * (10 * 1024 * 1024)
    uploaded = await facade.upload_license_evidence(
        "feed",
        pending.policy.policy_id,
        chunks(body[:123], body[123:]),
        media_type="text/plain",
        content_length=None,
        idempotency_key=uuid4(),
    )
    assert uploaded.digest == hashlib.sha256(body).hexdigest()
    assert uploaded.byte_count == len(body)
    assert repo.entries["feed"].current.state is SourcePolicyState.RIGHTS_PENDING
    assert len(repo.evidence) == 1


def pending_command(source_id):
    scope = license_scope(source_id)
    return SourceRevisionCommand(
        SourcePolicyState.RIGHTS_PENDING,
        "Submitted",
        evidence_ref="terms",
        license_scope=scope,
        provider=scope.provider,
        product=scope.product,
        venue=scope.venue,
        contract=scope.contract,
        attribution=scope.attribution,
    )


async def upload_fixture(facade, source_id, pending):
    async def chunks():
        yield b"Fixture license"

    return await facade.upload_license_evidence(
        source_id,
        pending.policy.policy_id,
        chunks(),
        media_type="text/plain",
        content_length=None,
        idempotency_key=uuid4(),
    )


@pytest.mark.asyncio
async def test_reviewer_cannot_approve_evidence_they_uploaded_for_another_operator():
    repo = Repository()
    service = SourcePolicyAdminService(repo)
    owner, other = principal(), principal()
    await service.for_principal(owner).create_source(
        SourceCreateCommand(
            "feed", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
        ),
        uuid4(),
    )
    pending = await service.for_principal(owner).submit_pending(
        "feed", pending_command("feed"), uuid4()
    )
    await upload_fixture(service.for_principal(other), "feed", pending)
    with pytest.raises(PermissionError, match="Reviewer"):
        await service.for_principal(other).approve_pending(
            "feed",
            pending.policy.policy_id,
            SourceApprovalCommand(frozenset({SourceUsePurpose.METADATA}), None, "Duyệt"),
            uuid4(),
        )


@pytest.mark.asyncio
async def test_upload_rejects_permissions_and_noncurrent_policy_before_consuming_stream():
    repo = Repository()
    owner = principal()
    service = SourcePolicyAdminService(repo)
    facade = service.for_principal(owner)
    await facade.create_source(
        SourceCreateCommand(
            "feed", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
        ),
        uuid4(),
    )
    pending = await facade.submit_pending("feed", pending_command("feed"), uuid4())
    consumed = []

    async def chunks():
        consumed.append(True)
        yield b"private evidence"

    for actor in (
        principal(Role.OPERATOR),
        principal(Role.ANALYST_REVIEWER),
        replace(owner, mfa_verified_at=NOW - timedelta(hours=1)),
    ):
        with pytest.raises(PermissionError):
            await service.for_principal(actor).upload_license_evidence(
                "feed",
                pending.policy.policy_id,
                chunks(),
                media_type="text/plain",
                content_length=None,
                idempotency_key=uuid4(),
            )
    for source_id, policy_id, error in (
        ("missing", "pending", LookupError),
        ("feed", "wrong", ValueError),
    ):
        with pytest.raises(error):
            await facade.upload_license_evidence(
                source_id,
                policy_id,
                chunks(),
                media_type="text/plain",
                content_length=None,
                idempotency_key=uuid4(),
            )
    assert consumed == []
    assert repo.evidence == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [None, "reference", "scope", "purpose", "source", "digest", "uploader"]
)
async def test_later_pending_can_select_prior_immutable_evidence(change):
    repo = Repository()
    service = SourcePolicyAdminService(repo)
    owner, other, reviewer = principal(), principal(), principal(Role.ANALYST_REVIEWER)
    facade = service.for_principal(owner)
    await facade.create_source(
        SourceCreateCommand(
            "feed", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
        ),
        uuid4(),
    )
    pending = await facade.submit_pending("feed", pending_command("feed"), uuid4())
    evidence = await upload_fixture(facade, "feed", pending)
    approved = await service.for_principal(reviewer).approve_pending(
        "feed",
        pending.policy.policy_id,
        SourceApprovalCommand(frozenset({SourceUsePurpose.METADATA}), None, "Duyệt"),
        uuid4(),
    )
    assert approved.license_evidence_id == evidence.evidence_id
    command = pending_command("feed")
    if change == "reference":
        command = replace(command, evidence_ref="archive:changed")
    if change == "scope":
        command = replace(
            command, license_scope=replace(command.license_scope, symbols=("ETHUSDT",))
        )
    if change == "source":
        await service.for_principal(other).create_source(
            SourceCreateCommand(
                "second", "Publisher", "https://example.org/", "GET", "public", BUDGET, "Candidate"
            ),
            uuid4(),
        )
        command = pending_command("second")
    source_id = "second" if change == "source" else "feed"
    later = await service.for_principal(other).submit_pending(source_id, command, uuid4())
    if change == "digest":
        current = await upload_fixture(service.for_principal(other), source_id, later)
        repo.evidence[-1] = replace(current, digest="b" * 64)
    approval = SourceApprovalCommand(
        frozenset(
            {SourceUsePurpose.TRAINING if change == "purpose" else SourceUsePurpose.METADATA}
        ),
        None,
        "Duyệt lại",
        license_evidence_id=evidence.evidence_id,
    )
    actor = owner if change == "uploader" else reviewer
    if change:
        error = (
            PermissionError
            if change == "uploader"
            else LookupError
            if change == "source"
            else ValueError
        )
        with pytest.raises(error):
            await service.for_principal(actor).approve_pending(
                source_id, later.policy.policy_id, approval, uuid4()
            )
        assert repo.entries[source_id].current == later
    else:
        key = uuid4()
        reused = await service.for_principal(actor).approve_pending(
            source_id, later.policy.policy_id, approval, key
        )
        retry = await service.for_principal(actor).approve_pending(
            source_id, later.policy.policy_id, approval, key
        )
        assert reused == retry
        assert reused.license_evidence_id == evidence.evidence_id
        assert reused.license_digest == evidence.digest
        assert reused.license_scope_hash == evidence.scope_hash
        assert len(repo.evidence) == 1


def test_default_approval_idempotency_payload_remains_compatible():
    from trading_bot.application.source_policy_admin import _canonical, _policy_id

    command = SourceApprovalCommand(frozenset({SourceUsePurpose.METADATA}), None, "Duyệt")
    canonical = _canonical(command)
    assert canonical == {"allowed_purposes": ["metadata"], "valid_until": None, "reason": "Duyệt"}
    key = uuid4()
    from dataclasses import make_dataclass

    legacy = make_dataclass("LegacyApproval", ("allowed_purposes", "valid_until", "reason"))(
        command.allowed_purposes,
        command.valid_until,
        command.reason,
    )
    assert _policy_id(key, ("feed", "pending", command)) == _policy_id(
        key, ("feed", "pending", legacy)
    )
