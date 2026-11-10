from collections import Counter
from decimal import Decimal
from uuid import UUID

from tests.fixtures.runtime_replay import build_h03_source_pinned_dataset_replay


def test_source_pinned_h03_replay_persists_exact_opportunity_to_outcome_lineage() -> None:
    workspace_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    bundle, comparison = build_h03_source_pinned_dataset_replay(workspace_id)

    run = comparison.runs[0]
    assert bundle.components
    assert all(item.manifest.has_pinned_source_identities for item in bundle.components)
    assert all(bar.raw_reference is None for item in bundle.components for bar in item.bars)
    assert len(run.opportunities) == 6
    assert len(run.trade_outcomes) == 2

    candidates = {
        item.event_id: item for item in run.opportunities if item.candidate_id is not None
    }
    assert len(candidates) == 2
    for trade in run.trade_outcomes:
        opportunity = candidates[trade.entry_opportunity_event_id]
        assert opportunity.candidate_id == trade.entry_candidate_id
        assert opportunity.vector_hash == trade.entry_vector_hash
        assert opportunity.runtime_snapshot_id == trade.entry_decision_snapshot_id
        assert opportunity.feature_schema_hash == trade.entry_feature_schema_hash


def test_source_pinned_replay_builds_all_three_labels_from_runtime_cost_contract() -> None:
    workspace_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    bundle, comparison = build_h03_source_pinned_dataset_replay(
        workspace_id,
        trade_patterns=("loss", "flat", "profit"),
        pattern_stride=3,
        fee_rate=Decimal("0"),
    )

    run = comparison.runs[0]
    labels = tuple((trade.net_pnl > 0) - (trade.net_pnl < 0) for trade in run.trade_outcomes)
    assert labels == (-1, 0, 1)
    assert len(run.opportunities) == 9
    assert all(
        bar.source_identity is not None for component in bundle.components for bar in component.bars
    )


def test_source_pinned_replay_builds_policy_sized_class_support_from_h03_runtime() -> None:
    workspace_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    patterns = ("loss", "flat", "profit") * 9
    _, comparison = build_h03_source_pinned_dataset_replay(
        workspace_id,
        trade_patterns=patterns,
        pattern_stride=10,
        fee_rate=Decimal("0"),
    )

    outcomes = comparison.runs[0].trade_outcomes
    label_counts = Counter((trade.net_pnl > 0) - (trade.net_pnl < 0) for trade in outcomes)
    assert len(outcomes) == 27
    assert label_counts == {-1: 9, 0: 9, 1: 9}
