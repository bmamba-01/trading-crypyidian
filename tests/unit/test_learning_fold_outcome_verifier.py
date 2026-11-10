from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from trading_bot.adapters.database.learning_fold_outcome_verifier import (
    DatabaseFoldOutcomeVerifier,
)
from trading_bot.domain.learning.dataset import DatasetManifest


class _ScalarResult:
    def __init__(self, value: str) -> None:
        self.value = value

    def scalar_one(self) -> str:
        return self.value

    def scalar_one_or_none(self) -> str:
        return self.value


class _ReadCommittedSession:
    def __init__(self, workspace_id) -> None:
        self.info = {"workspace_id": workspace_id}

    def in_transaction(self) -> bool:
        return True

    async def execute(self, statement):
        sql = str(statement)
        if "transaction_isolation" in sql:
            return _ScalarResult("read committed")
        if "transaction_read_only" in sql:
            return _ScalarResult("off")
        if "current_setting" in sql:
            return _ScalarResult(str(self.info["workspace_id"]))
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_verifier_accepts_read_committed_for_rights_serialized_evaluation() -> None:
    workspace_id = uuid4()
    verifier = DatabaseFoldOutcomeVerifier(
        _ReadCommittedSession(workspace_id),
        access_receipt_id=uuid4(),
        manifest=DatasetManifest(
            workspace_id=workspace_id,
            query_hash="1" * 64,
            symbols=("BTCUSDT",),
            data_start=datetime(2026, 1, 1, tzinfo=UTC),
            data_end=datetime(2026, 1, 2, tzinfo=UTC),
            as_of=datetime(2026, 1, 2, tzinfo=UTC),
            feature_schema_version="features-v1",
            feature_version="feature-v1",
            label_version="labels-v1",
            sample_schema_version="samples-v1",
            split_policy_version="split-v1",
            samples=(),
            exclusions=(),
            holdout_sealed_at=None,
            manifest_version="dataset-manifest-v1",
        ),
    )

    await verifier._require_receipt_snapshot(workspace_id)
