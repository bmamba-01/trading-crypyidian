from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

import pytest

from trading_bot.application import learning_dataset as learning_dataset_module
from trading_bot.application.learning_dataset import (
    DatasetEvidenceVerification,
    DatasetManifestBuilder,
    DatasetManifestBuildRequest,
)
from trading_bot.domain.learning.dataset import (
    DatasetExclusion,
    DatasetManifest,
    DatasetSample,
    DatasetSplit,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
WORKSPACE_ID = uuid4()


def sample(sample_id: str, *, source_dependent: bool = True) -> DatasetSample:
    return DatasetSample(
        sample_id=sample_id,
        decision_at=NOW,
        feature_available_at=NOW - timedelta(minutes=1),
        outcome_start=NOW,
        outcome_end=NOW + timedelta(minutes=5),
        feature_snapshot_hash="d" * 64,
        outcome_id=f"outcome-{sample_id}",
        outcome_hash="a" * 64,
        label_hash="b" * 64,
        label_value=1,
        label_version="label-v1",
        split=DatasetSplit.TRAIN,
        source_dependent=source_dependent,
        source_rights_hashes=("c" * 64,) if source_dependent else (),
        source_revision_refs=(f"source-a:revision-{sample_id}",) if source_dependent else (),
    )


def verification(
    item: DatasetSample,
    *,
    workspace_id=WORKSPACE_ID,
    as_of=NOW + timedelta(minutes=10),
    source_training_eligible=True,
    source_rights_hashes=("e" * 64,),
    source_revision_refs=None,
    feature_schema_version="features-v1",
    feature_version="feature-build-v2",
    sample_schema_version="sample-v1",
    split_policy_version="split-policy-v1",
    holdout_sealed_at=None,
) -> DatasetEvidenceVerification:
    authoritative = replace(
        item,
        source_rights_hashes=(source_rights_hashes if item.source_dependent else ()),
        source_revision_refs=(
            (item.source_revision_refs if source_revision_refs is None else source_revision_refs)
            if item.source_dependent
            else ()
        ),
    )
    return DatasetEvidenceVerification(
        workspace_id=workspace_id,
        authoritative_sample=authoritative,
        as_of=as_of,
        feature_schema_version=feature_schema_version,
        feature_version=feature_version,
        sample_schema_version=sample_schema_version,
        split_policy_version=split_policy_version,
        holdout_sealed_at=holdout_sealed_at,
        source_training_eligible=source_training_eligible,
    )


class FakeVerifier:
    def __init__(self, results: dict[str, DatasetEvidenceVerification | None]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    async def verify_sample(self, *, request, sample):
        self.calls.append((str(request.workspace_id), sample.sample_id, request.as_of.isoformat()))
        return self.results.get(sample.sample_id)


def request(samples: tuple[DatasetSample, ...]) -> DatasetManifestBuildRequest:
    return DatasetManifestBuildRequest(
        workspace_id=WORKSPACE_ID,
        query_hash="f" * 64,
        symbols=("BTCUSDT",),
        data_start=NOW - timedelta(days=1),
        data_end=NOW + timedelta(days=1),
        as_of=NOW + timedelta(minutes=10),
        feature_schema_version="features-v1",
        feature_version="feature-build-v2",
        label_version="label-v1",
        sample_schema_version="sample-v1",
        split_policy_version="split-policy-v1",
        samples=samples,
        exclusions=(),
        holdout_sealed_at=None,
        retention_policy_ref="retention-policy-v1",
    )


@pytest.mark.asyncio
async def test_builder_uses_authoritative_evidence_and_canonical_membership() -> None:
    first, second = sample("sample-a"), sample("sample-b")
    verifier = FakeVerifier(
        {
            first.sample_id: verification(first),
            second.sample_id: verification(second),
        }
    )

    result = await DatasetManifestBuilder(verifier).build(request((second, first)))

    assert isinstance(result, DatasetManifest)
    assert [item.sample_id for item in result.samples] == ["sample-a", "sample-b"]
    assert all(item.source_rights_hashes == ("e" * 64,) for item in result.samples)
    assert result.content_hash
    assert len(verifier.calls) == 2


@pytest.mark.asyncio
async def test_builder_excludes_samples_without_verified_rights_or_lineage() -> None:
    allowed = sample("allowed")
    revoked = sample("revoked")
    unavailable = sample("unavailable")
    verifier = FakeVerifier(
        {
            allowed.sample_id: verification(allowed),
            revoked.sample_id: verification(revoked, source_training_eligible=False),
            unavailable.sample_id: None,
        }
    )

    result = await DatasetManifestBuilder(verifier).build(request((allowed, revoked, unavailable)))

    assert [item.sample_id for item in result.samples] == ["allowed"]
    assert {item.sample_id: item.reasons for item in result.exclusions} == {
        "revoked": ("source_training_not_eligible",),
        "unavailable": ("provenance_unverified",),
    }


@pytest.mark.asyncio
async def test_builder_rejects_foreign_or_stale_verification_by_excluding_it() -> None:
    foreign = sample("foreign")
    stale = sample("stale")
    verifier = FakeVerifier(
        {
            foreign.sample_id: verification(foreign, workspace_id=uuid4()),
            stale.sample_id: verification(stale, as_of=NOW + timedelta(minutes=9)),
        }
    )

    result = await DatasetManifestBuilder(verifier).build(request((foreign, stale)))

    assert result.samples == ()
    assert {item.sample_id: item.reasons for item in result.exclusions} == {
        "foreign": ("provenance_scope_mismatch",),
        "stale": ("provenance_as_of_mismatch",),
    }


@pytest.mark.asyncio
async def test_wp02_only_sample_uses_verifier_without_requiring_source_rights() -> None:
    item = sample("paper-only", source_dependent=False)
    verifier = FakeVerifier(
        {item.sample_id: verification(item, source_training_eligible=None, source_rights_hashes=())}
    )

    result = await DatasetManifestBuilder(verifier).build(request((item,)))

    assert len(result.samples) == 1
    assert result.samples[0].source_revision_refs == ()
    assert result.samples[0].source_rights_hashes == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("label_value", 0),
        ("decision_at", NOW + timedelta(seconds=1)),
        ("feature_available_at", NOW - timedelta(minutes=2)),
        ("outcome_start", NOW + timedelta(seconds=1)),
        ("outcome_end", NOW + timedelta(minutes=6)),
        ("feature_snapshot_hash", "9" * 64),
        ("outcome_id", "mutated-outcome"),
        ("outcome_hash", "9" * 64),
        ("label_hash", "9" * 64),
        ("label_version", "label-v2"),
        ("source_dependent", False),
        ("split", DatasetSplit.VALIDATION),
        ("split", DatasetSplit.HOLDOUT),
    ],
)
@pytest.mark.asyncio
async def test_builder_rejects_candidate_mutations_against_authoritative_sample(
    field: str, value: object
) -> None:
    authoritative = sample("mutated")
    updates: dict[str, object] = {field: value}
    if field == "decision_at":
        updates["outcome_start"] = value
    if field == "source_dependent":
        updates["source_rights_hashes"] = ()
        updates["source_revision_refs"] = ()
    candidate = replace(authoritative, **updates)
    verifier = FakeVerifier({authoritative.sample_id: verification(authoritative)})

    result = await DatasetManifestBuilder(verifier).build(request((candidate,)))

    assert result.samples == ()
    assert result.exclusions[0].reasons == ("provenance_mismatch",)


@pytest.mark.asyncio
async def test_builder_uses_only_authoritative_source_rights_and_revision_refs() -> None:
    authoritative = sample("rights")
    candidate = replace(
        authoritative,
        source_rights_hashes=("c" * 64,),
        source_revision_refs=("caller-controlled:revision",),
    )
    verifier = FakeVerifier(
        {
            authoritative.sample_id: verification(
                authoritative,
                source_rights_hashes=("e" * 64,),
                source_revision_refs=("source-a:verified-revision",),
            )
        }
    )

    result = await DatasetManifestBuilder(verifier).build(request((candidate,)))

    assert result.samples[0].source_rights_hashes == ("e" * 64,)
    assert result.samples[0].source_revision_refs == ("source-a:verified-revision",)


@pytest.mark.asyncio
async def test_builder_rejects_sample_id_mutation_against_authoritative_sample() -> None:
    authoritative = sample("authoritative-id")
    candidate = replace(authoritative, sample_id="caller-id")
    verifier = FakeVerifier({candidate.sample_id: verification(authoritative)})

    result = await DatasetManifestBuilder(verifier).build(request((candidate,)))

    assert result.samples == ()
    assert result.exclusions[0].reasons == ("provenance_scope_mismatch",)


@pytest.mark.asyncio
async def test_builder_fails_closed_on_verifier_exception_or_timeout() -> None:
    item = sample("sample")

    class BrokenVerifier:
        async def verify_sample(self, **_):
            raise RuntimeError("database unavailable")

    class SlowVerifier:
        async def verify_sample(self, **_):
            await asyncio.sleep(1)

    for verifier in (BrokenVerifier(), SlowVerifier()):
        result = await DatasetManifestBuilder(verifier, verification_timeout_seconds=0.01).build(
            request((item,))
        )
        assert result.samples == ()
        assert result.exclusions[0].reasons == ("provenance_unverified",)


@pytest.mark.asyncio
async def test_builder_returns_at_deadline_when_verifier_resists_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = sample("sample")

    class CancellationResistantVerifier:
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()
        task: asyncio.Task[object] | None = None

        async def verify_sample(self, **_):
            self.task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellation_seen.set()
                await self.release.wait()
                return verification(item)

    verifier = CancellationResistantVerifier()
    monkeypatch.setattr(learning_dataset_module, "MAX_PENDING_VERIFICATION_TASKS", 1)
    builder = DatasetManifestBuilder(
        verifier,
        verification_timeout_seconds=0.02,
        build_timeout_seconds=0.2,
    )
    started_at = monotonic()

    result = await builder.build(request((item,)))

    assert monotonic() - started_at < 0.15
    await asyncio.wait_for(verifier.cancellation_seen.wait(), timeout=0.1)
    assert verifier.task is not None
    assert not verifier.task.done()
    assert result.samples == ()
    assert result.exclusions[0].reasons == ("provenance_unverified",)
    assert len(learning_dataset_module._PENDING_VERIFICATION_TASKS) == 1

    second = sample("second")
    second_verifier = FakeVerifier({second.sample_id: verification(second)})
    second_result = await DatasetManifestBuilder(second_verifier).build(request((second,)))
    assert second_result.samples == ()
    assert second_verifier.calls == []

    verifier.release.set()
    await asyncio.wait_for(verifier.task, timeout=0.1)
    await asyncio.sleep(0)
    assert learning_dataset_module._PENDING_VERIFICATION_TASKS == set()


@pytest.mark.asyncio
async def test_builder_fails_closed_on_malformed_verifier_result() -> None:
    item = sample("sample")

    class MalformedVerifier:
        async def verify_sample(self, **_):
            return {"source_training_eligible": True}

    result = await DatasetManifestBuilder(MalformedVerifier()).build(request((item,)))

    assert result.samples == ()
    assert result.exclusions[0].reasons == ("provenance_unverified",)


@pytest.mark.asyncio
async def test_builder_rejects_duplicate_candidate_and_exclusion_ids() -> None:
    item = sample("duplicate")
    verifier = FakeVerifier({item.sample_id: verification(item)})

    with pytest.raises(ValueError, match="duplicate"):
        await DatasetManifestBuilder(verifier).build(request((item, item)))

    duplicate_exclusion = replace(
        request((item,)),
        exclusions=(DatasetExclusion(item.sample_id, ("manual_exclusion",)),),
    )
    with pytest.raises(ValueError, match="đã tồn tại"):
        await DatasetManifestBuilder(verifier).build(duplicate_exclusion)


@pytest.mark.asyncio
async def test_builder_rejects_manifest_over_record_limit_before_verification() -> None:
    item = sample("repeated")
    verifier = FakeVerifier({})

    with pytest.raises(ValueError, match="10000"):
        await DatasetManifestBuilder(verifier).build(request((item,) * 10_001))

    assert verifier.calls == []


@pytest.mark.asyncio
async def test_builder_checks_time_after_authoritative_verification() -> None:
    item = sample("too-late")
    bad_range = replace(
        item,
        decision_at=NOW + timedelta(days=2),
        outcome_start=NOW + timedelta(days=2),
        outcome_end=NOW + timedelta(days=2, hours=1),
    )
    verifier = FakeVerifier({bad_range.sample_id: verification(bad_range)})

    result = await DatasetManifestBuilder(verifier).build(request((bad_range,)))

    assert result.samples == ()
    assert result.exclusions[0].reasons == (
        "decision_after_as_of",
        "decision_outside_data_range",
        "outcome_incomplete_at_as_of",
    )
    assert len(verifier.calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("feature_schema_version", "features-v2"),
        ("feature_version", "feature-build-v3"),
        ("label_version", "label-v2"),
        ("sample_schema_version", "sample-v2"),
        ("split_policy_version", "split-policy-v2"),
        ("holdout_sealed_at", NOW),
    ],
)
@pytest.mark.asyncio
async def test_builder_rejects_mutated_manifest_policy_attestation(
    field: str, value: object
) -> None:
    item = sample("policy")
    verifier = FakeVerifier({item.sample_id: verification(item)})

    with pytest.raises(ValueError, match="policy không khớp"):
        await DatasetManifestBuilder(verifier).build(replace(request((item,)), **{field: value}))
