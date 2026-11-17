"""Gate quyền sống cho evaluator với manifest v5 có lineage replay bundle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from trading_bot.adapters.database import replay_training_rights
from trading_bot.adapters.database.replay_training_rights import (
    DatabaseReplayEvaluationRightsGate,
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


def _lineage() -> SourceUseLineage:
    return SourceUseLineage(
        source_id="BINANCE_USDM_REST",
        provider="BINANCE_USDM_REST",
        bundle_hash=BUNDLE_HASH,
        child_dataset_hash=CHILD_HASH,
        policy_fingerprint="3" * 64,
        symbol="BTCUSDT",
        interval="1h",
        data_start=START,
        data_end=START + timedelta(hours=4),
        bar_identity_hash="4" * 64,
        market_bar_ids=("market-bar-record:00000000-0000-0000-0000-000000000001",),
    )


def _sample(*, source_dependent: bool) -> DatasetSample:
    lineage = _lineage()
    return DatasetSample(
        sample_id="sample-1",
        decision_at=START + timedelta(hours=2),
        feature_available_at=START + timedelta(hours=2),
        outcome_start=START + timedelta(hours=2),
        outcome_end=START + timedelta(hours=3),
        feature_snapshot_hash="5" * 64,
        outcome_id="trade-1",
        outcome_hash="6" * 64,
        label_hash="7" * 64,
        label_value=1,
        label_version="net-pnl-sign-v1",
        split=DatasetSplit.DEVELOPMENT,
        source_dependent=source_dependent,
        source_rights_hashes=(),
        source_revision_refs=(),
        source_replay_bundle_hash=BUNDLE_HASH if source_dependent else None,
        source_use_lineage_set_hash=(
            source_use_lineage_set_hash((lineage,)) if source_dependent else None
        ),
    )


def _manifest(*, source_dependent: bool = True) -> DatasetManifest:
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
        samples=(_sample(source_dependent=source_dependent),),
        exclusions=(),
        holdout_sealed_at=None,
        manifest_version="dataset-manifest-v5",
        retention_policy_ref="wp06-training-retention-v1",
        source_use_lineage=(_lineage(),) if source_dependent else (),
    )


def _receipt(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "bundle_hash": BUNDLE_HASH,
        "child_dataset_hash": CHILD_HASH,
        "provider": "BINANCE_USDM_REST",
        "symbol": "BTCUSDT",
        "interval": "1h",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_gate(monkeypatch, receipts, calls):
    class Gate:
        def __init__(self, session):
            pass

        async def authorize(self, *, workspace_id, bundle_hash, operation):
            calls.append((workspace_id, bundle_hash, operation.value))
            return receipts

    monkeypatch.setattr(replay_training_rights, "DatabaseReplayTrainingRightsGate", Gate)


@pytest.mark.asyncio
async def test_evaluation_gate_authorizes_exact_child_set_for_evaluation(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []
    _patch_gate(monkeypatch, (_receipt(),), calls)

    await DatabaseReplayEvaluationRightsGate(object()).assert_eligible(  # type: ignore[arg-type]
        workspace_id=WORKSPACE_ID, manifest=_manifest()
    )

    assert calls == [(WORKSPACE_ID, BUNDLE_HASH, "evaluation")]


@pytest.mark.asyncio
async def test_evaluation_gate_skips_source_free_manifest(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []
    _patch_gate(monkeypatch, (), calls)

    await DatabaseReplayEvaluationRightsGate(object()).assert_eligible(  # type: ignore[arg-type]
        workspace_id=WORKSPACE_ID, manifest=_manifest(source_dependent=False)
    )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"child_dataset_hash": "a" * 64},
        {"bundle_hash": "b" * 64},
        {"provider": "OTHER_PROVIDER"},
        {"symbol": "ETHUSDT"},
        {"interval": "4h"},
        {"workspace_id": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")},
    ],
)
async def test_evaluation_gate_rejects_receipt_outside_manifest_lineage(
    monkeypatch, changed
) -> None:
    _patch_gate(monkeypatch, (_receipt(**changed),), [])

    with pytest.raises(PermissionError):
        await DatabaseReplayEvaluationRightsGate(object()).assert_eligible(  # type: ignore[arg-type]
            workspace_id=WORKSPACE_ID, manifest=_manifest()
        )


@pytest.mark.asyncio
async def test_evaluation_gate_rejects_legacy_source_dependent_manifest(monkeypatch) -> None:
    _patch_gate(monkeypatch, (_receipt(),), [])
    legacy = _manifest()
    # Giả lập manifest đã lưu theo contract cũ; domain hiện tại không dựng v4 có lineage v5.
    object.__setattr__(legacy, "manifest_version", "dataset-manifest-v4")

    with pytest.raises(PermissionError, match="v5"):
        await DatabaseReplayEvaluationRightsGate(object()).assert_eligible(  # type: ignore[arg-type]
            workspace_id=WORKSPACE_ID, manifest=legacy
        )


@pytest.mark.asyncio
async def test_evaluation_gate_rejects_workspace_mismatch(monkeypatch) -> None:
    _patch_gate(monkeypatch, (_receipt(),), [])

    with pytest.raises(PermissionError):
        await DatabaseReplayEvaluationRightsGate(object()).assert_eligible(  # type: ignore[arg-type]
            workspace_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"), manifest=_manifest()
        )
