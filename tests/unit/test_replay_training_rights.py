"""Quyền TRAINING phải được cấp riêng cho mọi child bundle đã lưu."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.fixtures.runtime_replay import build_h01_dataset_runtime_replay
from tests.unit.test_binance_rest_backfill_rights import NOW, entry, observed

from trading_bot.domain.research.source_policy import SourceUsePurpose


@pytest.fixture
async def gate_fixture(monkeypatch):
    from tests.unit.test_candidate_universe_proof import WORKSPACE

    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_manifests import (
        BacktestDatasetManifestRepository,
    )
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
    from trading_bot.application.backtest_dataset_replay_bundle import (
        BacktestDatasetReplayBundleReference,
    )

    bundle, _ = build_h01_dataset_runtime_replay(WORKSPACE)
    state = SimpleNamespace(
        bundle=bundle,
        source=observed(entry(purposes=frozenset({SourceUsePurpose.TRAINING}))),
        now=NOW,
        missing_child=False,
    )

    async def get_bundle(self, **kwargs):
        return BacktestDatasetReplayBundleReference.from_bundle(state.bundle)

    async def get_child(self, **kwargs):
        if state.missing_child:
            return None
        return next(
            item.manifest
            for item in bundle.components
            if item.manifest.dataset_hash == kwargs["dataset_hash"]
        )

    async def get_observed(self, **kwargs):
        return state.source

    async def clock(self, **kwargs):
        return state.now

    monkeypatch.setattr(BacktestDatasetReplayBundleRepository, "get_by_hash", get_bundle)
    monkeypatch.setattr(BacktestDatasetManifestRepository, "get_by_hash", get_child)
    monkeypatch.setattr(SourceRegistryRepository, "get_observed_entry", get_observed)
    monkeypatch.setattr(SourceRegistryRepository, "database_clock", clock)
    return DatabaseReplayTrainingRightsGate(object()), state, WORKSPACE


@pytest.mark.asyncio
async def test_training_gate_issues_exact_scoped_receipt_for_every_child(gate_fixture):
    gate, state, workspace = gate_fixture
    receipts = await gate.authorize(workspace_id=workspace, bundle_hash=state.bundle.dataset_hash)
    assert len(receipts) == len(state.bundle.components) == 2
    assert {receipt.child_dataset_hash for receipt in receipts} == {
        item.manifest.dataset_hash for item in state.bundle.components
    }
    assert all(receipt.purpose is SourceUsePurpose.TRAINING for receipt in receipts)
    assert all(receipt.checked_at == NOW for receipt in receipts)
    assert all(receipt.workspace_id == workspace for receipt in receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["research_only", "missing_policy", "expired", "missing_child", "future_observer"]
)
async def test_training_gate_refuses_incomplete_or_unlicensed_scope(gate_fixture, failure):
    gate, state, workspace = gate_fixture
    if failure == "research_only":
        state.source = observed(entry())
    elif failure == "missing_policy":
        state.source = None
    elif failure == "expired":
        state.now = NOW + timedelta(days=5)
    elif failure == "missing_child":
        state.missing_child = True
    else:
        source = entry(purposes=frozenset({SourceUsePurpose.TRAINING}))
        state.source = type(observed(source))(
            source,
            {
                revision.policy.policy_id: NOW + timedelta(seconds=1)
                for revision in source.revisions
            },
        )
    with pytest.raises(PermissionError):
        await gate.authorize(workspace_id=workspace, bundle_hash=state.bundle.dataset_hash)
