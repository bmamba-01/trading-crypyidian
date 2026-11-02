from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models import DatasetManifestRecord
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def manifest() -> DatasetManifest:
    return DatasetManifest(
        workspace_id=uuid4(),
        query_hash="1" * 64,
        symbols=("BTCUSDT",),
        data_start=NOW - timedelta(days=1),
        data_end=NOW + timedelta(days=1),
        as_of=NOW + timedelta(minutes=1),
        feature_schema_version="feature-schema-v1",
        feature_version="feature-v1",
        label_version="label-v1",
        sample_schema_version="sample-schema-v1",
        split_policy_version="split-v1",
        samples=(
            DatasetSample(
                sample_id="sample-1",
                decision_at=NOW,
                feature_available_at=NOW - timedelta(minutes=1),
                outcome_start=NOW,
                outcome_end=NOW + timedelta(seconds=1),
                feature_snapshot_hash="2" * 64,
                outcome_id="trade-1",
                outcome_hash="3" * 64,
                label_hash="4" * 64,
                label_value=1,
                label_version="label-v1",
                split=DatasetSplit.TRAIN,
                source_dependent=False,
                source_rights_hashes=(),
                source_revision_refs=(),
            ),
        ),
        exclusions=(),
        holdout_sealed_at=None,
        retention_policy_ref="retention-v1",
    )


def test_manifest_record_round_trips_domain_bytes_and_counts() -> None:
    original = manifest()

    record = DatasetManifestRecord.from_domain(original)
    restored = record.to_domain()

    assert record.workspace_id == original.workspace_id
    assert record.manifest_hash == original.content_hash
    assert record.sample_count == 1
    assert record.exclusion_count == 0
    assert record.manifest["samples"][0]["sample_id"] == "sample-1"
    assert restored == original


def test_manifest_record_rejects_non_domain_payload() -> None:
    with pytest.raises(ValueError, match="DatasetManifest"):
        DatasetManifestRecord.from_domain(object())


def test_manifest_record_detects_mutated_jsonb_or_denormalized_index_fields() -> None:
    record = DatasetManifestRecord.from_domain(manifest())
    record.manifest = {"query_hash": "tampered"}
    with pytest.raises(ValueError, match="JSONB"):
        record.to_domain()

    record = DatasetManifestRecord.from_domain(manifest())
    record.sample_count = 99
    with pytest.raises(ValueError, match="chỉ mục"):
        record.to_domain()
