from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from trading_bot.adapters.database.models.learning_run_source_rights_entities import (
    LearningRunSourceRightsReceiptRecord,
)


def test_run_source_rights_receipt_contains_only_typed_metadata() -> None:
    record = LearningRunSourceRightsReceiptRecord(
        workspace_id=uuid4(),
        access_kind="evaluation",
        access_id=uuid4(),
        normalized_dataset_id=uuid4(),
        manifest_hash="a" * 64,
        fold_plan_hash="b" * 64,
        sample_id="sample-1",
        source_id="source-1",
        item_id="item-1",
        revision_id="revision-1",
        policy_id="policy-1",
        policy_sequence=1,
        purpose="training",
        rights_hash="c" * 64,
        checked_at=datetime(2026, 11, 8, tzinfo=UTC),
    )

    assert record.purpose == "training"
    assert record.checked_at.tzinfo is UTC
    columns = set(record.__table__.columns.keys())
    assert {
        "workspace_id",
        "access_kind",
        "access_id",
        "normalized_dataset_id",
        "manifest_hash",
        "fold_plan_hash",
        "sample_id",
        "source_id",
        "item_id",
        "revision_id",
        "policy_id",
        "policy_sequence",
        "purpose",
        "rights_hash",
        "checked_at",
    } <= columns
    assert not columns.intersection({"body", "content", "features", "label", "payload"})
