from datetime import UTC, datetime
from uuid import uuid4

from trading_bot.adapters.database.models import LearningSourceTrainingReceiptRecord
from trading_bot.application.dataset_source_eligibility import DatasetSourceRightsEvidence
from trading_bot.domain.learning.dataset import DatasetSourceReference
from trading_bot.domain.research.source_policy import SourceUsePurpose


def test_source_training_receipt_persists_only_typed_rights_metadata() -> None:
    workspace_id = uuid4()
    dataset_id = uuid4()
    reference = DatasetSourceReference(
        source_id="macro-source",
        item_id="calendar-item",
        revision_id="revision-2",
        policy_id="policy-7",
        policy_sequence=7,
        purpose=SourceUsePurpose.TRAINING,
        rights_hash="a" * 64,
    )
    evidence = DatasetSourceRightsEvidence(
        workspace_id=workspace_id,
        manifest_hash="b" * 64,
        sample_id="sample-1",
        reference=reference,
        checked_at=datetime(2026, 11, 8, 10, tzinfo=UTC),
    )

    record = LearningSourceTrainingReceiptRecord.from_evidence(
        evidence, normalized_dataset_id=dataset_id, fold_plan_hash="c" * 64
    )

    assert record.workspace_id == workspace_id
    assert record.normalized_dataset_id == dataset_id
    assert record.manifest_hash == "b" * 64
    assert record.fold_plan_hash == "c" * 64
    assert record.sample_id == "sample-1"
    assert record.source_id == "macro-source"
    assert record.item_id == "calendar-item"
    assert record.revision_id == "revision-2"
    assert record.policy_id == "policy-7"
    assert record.policy_sequence == 7
    assert record.purpose == "training"
    assert record.rights_hash == "a" * 64
    assert record.checked_at == datetime(2026, 11, 8, 10, tzinfo=UTC)
    assert record.canonical_evidence == evidence.canonical_json
    assert record.evidence_hash == evidence.evidence_hash
    assert not {"body", "payload", "features", "label_value"} & set(
        LearningSourceTrainingReceiptRecord.__table__.c.keys()
    )
