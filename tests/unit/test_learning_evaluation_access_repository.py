import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_evaluation_access import (
    LearningEvaluationAccessRepository,
)
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def manifest() -> DatasetManifest:
    return DatasetManifest(
        workspace_id=uuid4(),
        query_hash="1" * 64,
        symbols=("BTCUSDT",),
        data_start=NOW,
        data_end=NOW + timedelta(days=1),
        as_of=NOW + timedelta(hours=1),
        feature_schema_version="feature-v1",
        feature_version="feature-v1",
        label_version="label-v1",
        sample_schema_version="sample-v1",
        split_policy_version="split-v1",
        samples=(
            DatasetSample(
                sample_id="sample-1",
                decision_at=NOW,
                feature_available_at=NOW,
                outcome_start=NOW,
                outcome_end=NOW + timedelta(minutes=1),
                feature_snapshot_hash="2" * 64,
                outcome_id="outcome-1",
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


class FakeResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def one(self) -> object:
        return self._row


class FakeSession:
    def __init__(self, row: object) -> None:
        self.row = row
        self.calls: list[tuple[object, object]] = []

    async def execute(self, statement: object, parameters: object = None) -> FakeResult:
        self.calls.append((statement, parameters))
        return FakeResult(self.row)


@pytest.mark.asyncio
async def test_open_access_calls_scoped_database_gateway_and_verifies_manifest() -> None:
    source = manifest()
    receipt_id = uuid4()
    session = FakeSession(
        SimpleNamespace(
            receipt_id=receipt_id,
            manifest=json.loads(source.canonical_json),
            canonical_manifest=source.canonical_json,
            split_policy_version=source.split_policy_version,
            fold_plan_hash="5" * 64,
            approval_hash="6" * 64,
        )
    )

    receipt = await LearningEvaluationAccessRepository(session).open(
        workspace_id=source.workspace_id,
        manifest_hash=source.content_hash,
        evaluation_run_id=uuid4(),
        request_fingerprint="7" * 64,
    )

    statement, parameters = session.calls[0]
    assert "wp06_open_learning_evaluation" in str(statement)
    assert "target_request_fingerprint =>" in str(statement)
    assert parameters["workspace_id"] == str(source.workspace_id)
    assert parameters["manifest_hash"] == source.content_hash
    assert receipt.receipt_id == receipt_id
    assert receipt.manifest == source
    assert receipt.fold_plan_hash == "5" * 64
    assert receipt.approval_hash == "6" * 64
