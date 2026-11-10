from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_fold_samples import (
    _source_advisory_lock_keys,
)
from trading_bot.application.dataset_source_eligibility import (
    DatasetSourceEligibilityDenied,
    DatasetSourceEligibilityGate,
)
from trading_bot.domain.learning.dataset import (
    DatasetManifest,
    DatasetSample,
    DatasetSourceReference,
    DatasetSplit,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

NOW = datetime(2026, 11, 3, tzinfo=UTC)
WORKSPACE_ID = uuid4()
RIGHTS_HASH = "a" * 64
REFERENCE = DatasetSourceReference(
    source_id="source-1",
    item_id="item-1",
    revision_id="revision-1",
    policy_id="policy-1",
    policy_sequence=3,
    purpose=SourceUsePurpose.TRAINING,
    rights_hash=RIGHTS_HASH,
)


def source_manifest() -> DatasetManifest:
    sample = DatasetSample(
        sample_id="sample-1",
        decision_at=NOW + timedelta(hours=1),
        feature_available_at=NOW,
        outcome_start=NOW + timedelta(hours=1),
        outcome_end=NOW + timedelta(hours=2),
        feature_snapshot_hash="b" * 64,
        outcome_id="outcome-1",
        outcome_hash="c" * 64,
        label_hash="d" * 64,
        label_value=1,
        label_version="labels-v1",
        split=DatasetSplit.TRAIN,
        source_dependent=True,
        source_rights_hashes=(),
        source_revision_refs=(),
        source_references=(REFERENCE,),
    )
    return DatasetManifest(
        workspace_id=WORKSPACE_ID,
        query_hash="e" * 64,
        symbols=("BTCUSDT",),
        data_start=NOW,
        data_end=NOW + timedelta(hours=3),
        as_of=NOW + timedelta(hours=3),
        feature_schema_version="features-v1",
        feature_version="feature-v1",
        label_version="labels-v1",
        sample_schema_version="samples-v1",
        split_policy_version="split-v1",
        samples=(sample,),
        exclusions=(),
        holdout_sealed_at=None,
        manifest_version="dataset-manifest-v3",
    )


class RightsRepository:
    def __init__(self, *, invalidated: bool = False, pending: bool = False) -> None:
        self.invalidated = invalidated
        self.pending = pending

    async def find_invalidation(self, **_: object) -> str | None:
        return "source_takedown" if self.invalidated else None

    async def has_unprocessed_rights_event(self, **_: object) -> bool:
        return self.pending


class SourceAccessReader:
    def __init__(self, envelope: object | None = None, error: Exception | None = None) -> None:
        self.envelope = envelope or SimpleNamespace(
            decision=SimpleNamespace(permitted=True, reason="allowed"),
            revision=SimpleNamespace(revision=SimpleNamespace(revision_id="revision-1")),
            historical_policy_id="policy-1",
            historical_policy_sequence=3,
            current_policy_id="policy-1",
            current_policy_sequence=3,
            rights_hash=RIGHTS_HASH,
            evaluated_at=NOW,
        )
        self.error = error

    async def select_access_envelope(self, **_: object) -> object:
        if self.error:
            raise self.error
        return self.envelope


@pytest.mark.asyncio
async def test_pending_rights_event_blocks_source_dependent_manifest_before_access_read() -> None:
    source = SourceAccessReader()
    gate = DatasetSourceEligibilityGate(RightsRepository(pending=True), source)

    with pytest.raises(DatasetSourceEligibilityDenied, match="rights_event_unprocessed"):
        await gate.assert_eligible(workspace_id=WORKSPACE_ID, manifest=source_manifest())


@pytest.mark.asyncio
async def test_persisted_invalidation_blocks_source_dependent_manifest() -> None:
    gate = DatasetSourceEligibilityGate(RightsRepository(invalidated=True), SourceAccessReader())

    with pytest.raises(DatasetSourceEligibilityDenied, match="source_takedown"):
        await gate.assert_eligible(workspace_id=WORKSPACE_ID, manifest=source_manifest())


@pytest.mark.asyncio
async def test_live_rights_hash_change_blocks_manifest_even_without_invalidation_event() -> None:
    changed_envelope = SimpleNamespace(
        decision=SimpleNamespace(permitted=True, reason="allowed"),
        revision=SimpleNamespace(revision=SimpleNamespace(revision_id="revision-1")),
        historical_policy_id="policy-1",
        historical_policy_sequence=3,
        current_policy_id="policy-2",
        current_policy_sequence=4,
        rights_hash="f" * 64,
    )
    gate = DatasetSourceEligibilityGate(
        RightsRepository(), SourceAccessReader(envelope=changed_envelope)
    )

    with pytest.raises(DatasetSourceEligibilityDenied, match="live_rights_mismatch"):
        await gate.assert_eligible(workspace_id=WORKSPACE_ID, manifest=source_manifest())


@pytest.mark.asyncio
async def test_exact_current_lineage_allows_source_dependent_manifest() -> None:
    gate = DatasetSourceEligibilityGate(RightsRepository(), SourceAccessReader())

    evidence = await gate.assert_eligible(workspace_id=WORKSPACE_ID, manifest=source_manifest())

    assert len(evidence) == 1
    assert evidence[0].workspace_id == WORKSPACE_ID
    assert evidence[0].manifest_hash == source_manifest().content_hash
    assert evidence[0].sample_id == "sample-1"
    assert evidence[0].reference == REFERENCE
    assert evidence[0].checked_at == NOW
    assert evidence[0].canonical_json == (
        '{"checked_at":"2026-11-03T00:00:00+00:00",'
        f'"manifest_hash":"{source_manifest().content_hash}",'
        '"reference":{"item_id":"item-1","policy_id":"policy-1",'
        '"policy_sequence":3,"purpose":"training","revision_id":"revision-1",'
        f'"rights_hash":"{RIGHTS_HASH}","source_id":"source-1"}},'
        '"sample_id":"sample-1",'
        f'"workspace_id":"{WORKSPACE_ID}"}}'
    )


@pytest.mark.asyncio
async def test_missing_database_checked_at_fails_closed() -> None:
    source = SourceAccessReader()
    del source.envelope.evaluated_at
    gate = DatasetSourceEligibilityGate(RightsRepository(), source)

    with pytest.raises(DatasetSourceEligibilityDenied, match="live_access_unavailable"):
        await gate.assert_eligible(workspace_id=WORKSPACE_ID, manifest=source_manifest())


@pytest.mark.asyncio
async def test_access_lookup_exception_fails_closed() -> None:
    gate = DatasetSourceEligibilityGate(
        RightsRepository(), SourceAccessReader(error=RuntimeError("database unavailable"))
    )

    with pytest.raises(DatasetSourceEligibilityDenied, match="live_access_unavailable"):
        await gate.assert_eligible(workspace_id=WORKSPACE_ID, manifest=source_manifest())


@pytest.mark.asyncio
async def test_source_free_manifest_returns_no_source_rights_evidence() -> None:
    gate = DatasetSourceEligibilityGate(RightsRepository(), SourceAccessReader())
    manifest = source_manifest()
    sample = manifest.samples[0]
    source_free_sample = DatasetSample(
        sample_id=sample.sample_id,
        decision_at=sample.decision_at,
        feature_available_at=sample.feature_available_at,
        outcome_start=sample.outcome_start,
        outcome_end=sample.outcome_end,
        feature_snapshot_hash=sample.feature_snapshot_hash,
        outcome_id=sample.outcome_id,
        outcome_hash=sample.outcome_hash,
        label_hash=sample.label_hash,
        label_value=sample.label_value,
        label_version=sample.label_version,
        split=sample.split,
        source_dependent=False,
        source_rights_hashes=(),
        source_revision_refs=(),
    )
    source_free_manifest = DatasetManifest(
        workspace_id=manifest.workspace_id,
        query_hash=manifest.query_hash,
        symbols=manifest.symbols,
        data_start=manifest.data_start,
        data_end=manifest.data_end,
        as_of=manifest.as_of,
        feature_schema_version=manifest.feature_schema_version,
        feature_version=manifest.feature_version,
        label_version=manifest.label_version,
        sample_schema_version=manifest.sample_schema_version,
        split_policy_version=manifest.split_policy_version,
        samples=(source_free_sample,),
        exclusions=(),
        holdout_sealed_at=None,
        manifest_version="dataset-manifest-v3",
    )

    assert (
        await gate.assert_eligible(workspace_id=WORKSPACE_ID, manifest=source_free_manifest) == ()
    )


@pytest.mark.asyncio
async def test_source_lock_keys_use_database_default_collation_and_are_unique() -> None:
    second_item = replace(REFERENCE, item_id="item-2", revision_id="revision-2")
    other_source = replace(
        REFERENCE,
        source_id="source-0",
        item_id="item-3",
        revision_id="revision-3",
        policy_id="policy-0",
    )
    manifest = source_manifest()
    sample = replace(
        manifest.samples[0],
        source_references=(REFERENCE, second_item, other_source),
    )
    manifest = replace(manifest, samples=(sample,))

    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: ["source-0", "source-1"])
            )
        )
    )
    lock_keys = await _source_advisory_lock_keys(session, WORKSPACE_ID, manifest)

    statement, parameters = session.execute.await_args.args
    assert "ORDER BY source_id" in str(statement)
    assert 'COLLATE "C"' not in str(statement)
    assert set(parameters["source_ids"]) == {"source-0", "source-1"}
    assert lock_keys == (
        f"research-source:{WORKSPACE_ID}:source-0",
        f"research-source:{WORKSPACE_ID}:source-1",
    )
