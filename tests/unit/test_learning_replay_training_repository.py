from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError

from trading_bot.adapters.database.repositories.learning_fold_samples import (
    LearningFoldSampleRepository,
    NormalizedLearningDatasetConflict,
)
from trading_bot.domain.learning.dataset import DatasetManifest, DatasetSample, DatasetSplit
from trading_bot.domain.learning.source_use_lineage import (
    SourceUseLineage,
    source_use_lineage_set_hash,
)

WORKSPACE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
START = datetime(2026, 1, 1, tzinfo=UTC)
BUNDLE_HASH = "1" * 64
CHILD_HASH = "2" * 64
POLICY_FINGERPRINT = "3" * 64


def _manifest() -> DatasetManifest:
    lineage = SourceUseLineage(
        source_id="BINANCE_USDM_REST",
        provider="BINANCE_USDM_REST",
        bundle_hash=BUNDLE_HASH,
        child_dataset_hash=CHILD_HASH,
        policy_fingerprint=POLICY_FINGERPRINT,
        symbol="BTCUSDT",
        interval="1h",
        data_start=START,
        data_end=START + timedelta(hours=4),
        bar_identity_hash="4" * 64,
        market_bar_ids=("market-bar-record:00000000-0000-0000-0000-000000000001",),
    )
    sample = DatasetSample(
        sample_id="sample-1",
        decision_at=START + timedelta(hours=2),
        feature_available_at=START + timedelta(hours=2) - timedelta(minutes=1),
        outcome_start=START + timedelta(hours=2),
        outcome_end=START + timedelta(hours=3),
        feature_snapshot_hash="5" * 64,
        outcome_id="trade-1",
        outcome_hash="6" * 64,
        label_hash="7" * 64,
        label_value=1,
        label_version="net-pnl-sign-v1",
        split=DatasetSplit.DEVELOPMENT,
        source_dependent=True,
        source_rights_hashes=(),
        source_revision_refs=(),
        source_replay_bundle_hash=BUNDLE_HASH,
        source_use_lineage_set_hash=source_use_lineage_set_hash((lineage,)),
    )
    return DatasetManifest(
        workspace_id=WORKSPACE_ID,
        query_hash="8" * 64,
        symbols=("BTCUSDT",),
        data_start=START,
        data_end=START + timedelta(hours=4),
        as_of=START + timedelta(hours=4),
        feature_schema_version="features-v1",
        feature_version="features-v1",
        label_version="net-pnl-sign-v1",
        sample_schema_version="entry-trade-v1",
        split_policy_version="walk-forward-v1",
        samples=(sample,),
        exclusions=(),
        holdout_sealed_at=None,
        manifest_version="dataset-manifest-v5",
        retention_policy_ref="wp06-training-retention-v1",
        source_use_lineage=(lineage,),
    )


def _current_receipt(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "bundle_hash": BUNDLE_HASH,
        "child_dataset_hash": CHILD_HASH,
        "provider": "BINANCE_USDM_REST",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "policy_fingerprint": POLICY_FINGERPRINT,
        "bars_content_hash": "9" * 64,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_replay_normalization_rechecks_every_exact_current_child(monkeypatch) -> None:
    from trading_bot.adapters.database.repositories import learning_fold_samples

    current = (_current_receipt(),)

    class Gate:
        def __init__(self, session):
            pass

        async def authorize(self, *, workspace_id, bundle_hash, operation):
            assert workspace_id == WORKSPACE_ID
            assert bundle_hash == BUNDLE_HASH
            assert operation.value == "training"
            return current

    monkeypatch.setattr(learning_fold_samples, "DatabaseReplayTrainingRightsGate", Gate)
    await LearningFoldSampleRepository(object())._assert_replay_training_eligibility(_manifest())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"child_dataset_hash": "a" * 64},
        {"bundle_hash": "b" * 64},
        {"provider": "OTHER_PROVIDER"},
        {"symbol": "ETHUSDT"},
        {"interval": "4h"},
    ],
)
async def test_replay_normalization_rejects_changed_current_child_scope(
    monkeypatch, changed
) -> None:
    from trading_bot.adapters.database.repositories import learning_fold_samples

    current = (_current_receipt(**changed),)

    class Gate:
        def __init__(self, session):
            pass

        async def authorize(self, **kwargs):
            return current

    monkeypatch.setattr(learning_fold_samples, "DatabaseReplayTrainingRightsGate", Gate)
    with pytest.raises(
        NormalizedLearningDatasetConflict,
        match="Current TRAINING scope|lệch v5 source-use lineage",
    ):
        await LearningFoldSampleRepository(object())._assert_replay_training_eligibility(
            _manifest()
        )


@pytest.mark.asyncio
async def test_replay_normalization_allows_current_policy_revision_within_scope(
    monkeypatch,
) -> None:
    from trading_bot.adapters.database.repositories import learning_fold_samples

    current = (_current_receipt(policy_fingerprint="c" * 64),)

    class Gate:
        def __init__(self, session):
            pass

        async def authorize(self, **kwargs):
            return current

    monkeypatch.setattr(learning_fold_samples, "DatabaseReplayTrainingRightsGate", Gate)
    await LearningFoldSampleRepository(object())._assert_replay_training_eligibility(_manifest())


@pytest.mark.asyncio
async def test_replay_rights_marker_is_mapped_to_normalized_conflict(monkeypatch) -> None:
    from trading_bot.adapters.database.repositories import learning_fold_samples

    class Gate:
        def __init__(self, session):
            pass

        async def authorize(self, **kwargs):
            raise DBAPIError(
                "SELECT wp06_authorize_replay_training_bundle",
                {},
                Exception("WP06_TRAINING_REPLAY_SCOPE_NOT_COVERED"),
            )

    monkeypatch.setattr(learning_fold_samples, "DatabaseReplayTrainingRightsGate", Gate)
    with pytest.raises(NormalizedLearningDatasetConflict, match="bởi verifier cơ sở dữ liệu"):
        await LearningFoldSampleRepository(object())._assert_replay_training_eligibility(
            _manifest()
        )


@pytest.mark.asyncio
async def test_unexpected_database_failure_is_not_hidden_as_rights_denial(monkeypatch) -> None:
    from trading_bot.adapters.database.repositories import learning_fold_samples

    database_error = DBAPIError("SELECT", {}, Exception("connection lost"))

    class Gate:
        def __init__(self, session):
            pass

        async def authorize(self, **kwargs):
            raise database_error

    monkeypatch.setattr(learning_fold_samples, "DatabaseReplayTrainingRightsGate", Gate)
    with pytest.raises(DBAPIError) as caught:
        await LearningFoldSampleRepository(object())._assert_replay_training_eligibility(
            _manifest()
        )
    assert caught.value is database_error


@pytest.mark.asyncio
async def test_normalization_replay_receipt_uses_database_derived_scope_only() -> None:
    class Session:
        def __init__(self):
            self.statement = None
            self.parameters = None

        async def execute(self, statement, parameters):
            self.statement = str(statement)
            self.parameters = parameters

    session = Session()
    repository = LearningFoldSampleRepository(session)  # type: ignore[arg-type]
    await repository._invoke_training_source_receipt_gate(
        manifest=_manifest(),
        normalized_dataset_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        fold_plan_hash="c" * 64,
    )

    assert "wp06_record_replay_training_receipts" in session.statement
    assert session.parameters == {
        "workspace_id": str(WORKSPACE_ID),
        "manifest_hash": _manifest().content_hash,
        "normalized_dataset_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "fold_plan_hash": "c" * 64,
    }
    assert "source_use_lineage" not in session.parameters
