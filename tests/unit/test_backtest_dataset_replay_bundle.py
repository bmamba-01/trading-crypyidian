from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.backtest_dataset_manifest import (
    BacktestDatasetManifest,
    BacktestDatasetNotReady,
    build_backtest_dataset_manifest,
)
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetBundleMismatch,
    BacktestDatasetReplayBundleReference,
    build_backtest_dataset_replay_bundle,
)
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.market.models import OHLCVBar

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
END = NOW
EVALUATION_START = NOW - timedelta(hours=1)
WORKSPACE_ID = uuid4()
POLICY_HASH = hashlib.sha256(b"replay-bundle-policy-v1").hexdigest()
INTERVALS = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1)}


def replay_input(
    interval: str,
    *,
    workspace_id=WORKSPACE_ID,
    symbol="BTCUSDT",
    evaluation_start=EVALUATION_START,
    policy_fingerprint=POLICY_HASH,
    incomplete=False,
) -> BacktestDatasetReplayInput:
    duration = INTERVALS[interval]
    warmup_count = 2
    start = evaluation_start - duration * warmup_count
    request = RestBackfillRequest(
        symbol=symbol,
        interval=interval,
        start=start,
        end=END,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    expected_count = int((END - start) / duration)
    bars = tuple(
        OHLCVBar(
            symbol=symbol,
            timeframe=interval,
            open_time=start + duration * index,
            close_time=start + duration * (index + 1) - timedelta(milliseconds=1),
            event_at=start + duration * (index + 1) - timedelta(milliseconds=1),
            received_at=NOW,
            available_at=NOW,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1"),
            is_closed=True,
            source="BINANCE_USDM_REST_BACKFILL",
            source_cursor=f"{interval}:{index}",
            checksum=hashlib.sha256(f"{interval}:{index}".encode()).hexdigest(),
        )
        for index in range(expected_count - int(incomplete))
    )
    rights = BackfillRightsEvidence(
        workspace_id=workspace_id,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/terms",
        policy_fingerprint=policy_fingerprint,
        verified_at=NOW - timedelta(days=1),
        checked_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )
    manifest = build_backtest_dataset_manifest(
        request,
        evaluation_start=evaluation_start,
        warmup_bars=warmup_count,
        bars=bars,
        rights_evidence=rights,
        prepared_at=NOW,
    )
    return BacktestDatasetReplayInput(manifest, bars, rights)


def test_bundle_hash_is_stable_and_components_are_sorted_by_timeframe() -> None:
    one_hour = replay_input("1h")
    fifteen_minutes = replay_input("15m")

    first = build_backtest_dataset_replay_bundle((one_hour, fifteen_minutes))
    second = build_backtest_dataset_replay_bundle((fifteen_minutes, one_hour))

    assert first.dataset_hash == second.dataset_hash
    assert tuple(item.manifest.interval for item in first.components) == ("15m", "1h")
    assert first.workspace_id == WORKSPACE_ID
    assert first.symbol == "BTCUSDT"
    assert first.evaluation_start == EVALUATION_START
    assert first.range_end == END


def test_bundle_reference_is_canonical_and_round_trips() -> None:
    one_hour = replay_input("1h")
    fifteen_minutes = replay_input("15m")
    first = build_backtest_dataset_replay_bundle((one_hour, fifteen_minutes))
    second = build_backtest_dataset_replay_bundle((fifteen_minutes, one_hour))

    reference = BacktestDatasetReplayBundleReference.from_bundle(first)
    reordered_reference = BacktestDatasetReplayBundleReference.from_bundle(second)

    assert reference.canonical_json == reordered_reference.canonical_json
    assert reference.bundle_hash == first.dataset_hash == second.dataset_hash
    assert reference.bundle_hash == hashlib.sha256(reference.canonical_json.encode()).hexdigest()
    assert reference.schema_version == 1
    assert tuple(item.interval for item in reference.components) == ("15m", "1h")
    assert (
        BacktestDatasetReplayBundleReference.from_canonical_json(
            reference.canonical_json, expected_bundle_hash=reference.bundle_hash
        )
        == reference
    )


def test_bundle_reference_copies_mutable_components_input() -> None:
    bundle = build_backtest_dataset_replay_bundle((replay_input("15m"), replay_input("1h")))
    source = BacktestDatasetReplayBundleReference.from_bundle(bundle)
    mutable_components = list(source.components)

    reference = replace(source, components=mutable_components)
    canonical_before = reference.canonical_json
    hash_before = reference.bundle_hash
    mutable_components.clear()

    assert isinstance(reference.components, tuple)
    assert reference.components == source.components
    assert reference.canonical_json == canonical_before
    assert reference.bundle_hash == hash_before


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-field",
        "missing-field",
        "duplicate-interval",
        "unsorted-interval",
        "malformed-child-hash",
        "scope-tampering",
    ],
)
def test_bundle_reference_parser_rejects_invalid_canonical_payload(mutation: str) -> None:
    bundle = build_backtest_dataset_replay_bundle((replay_input("15m"), replay_input("1h")))
    reference = BacktestDatasetReplayBundleReference.from_bundle(bundle)
    import json

    payload = json.loads(reference.canonical_json)
    if mutation == "unknown-field":
        payload["extra"] = "not allowed"
    elif mutation == "missing-field":
        del payload["symbol"]
    elif mutation == "duplicate-interval":
        payload["components"][1]["interval"] = payload["components"][0]["interval"]
    elif mutation == "unsorted-interval":
        payload["components"].reverse()
    elif mutation == "malformed-child-hash":
        payload["components"][0]["dataset_hash"] = "xyz"
    else:
        payload["workspace_id"] = str(uuid4())

    tampered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="Canonical"):
        BacktestDatasetReplayBundleReference.from_canonical_json(
            tampered, expected_bundle_hash=reference.bundle_hash
        )


def test_bundle_reference_parser_rejects_wrong_expected_hash() -> None:
    bundle = build_backtest_dataset_replay_bundle((replay_input("1h"),))
    reference = BacktestDatasetReplayBundleReference.from_bundle(bundle)

    with pytest.raises(ValueError, match="hash"):
        BacktestDatasetReplayBundleReference.from_canonical_json(
            reference.canonical_json, expected_bundle_hash="0" * 64
        )


def test_bundle_accepts_single_timeframe_strategy_input() -> None:
    source = replay_input("1h")

    bundle = build_backtest_dataset_replay_bundle((source,))

    assert bundle.components == (source,)
    assert len(bundle.dataset_hash) == 64


@pytest.mark.parametrize(
    "inputs",
    [
        lambda: (replay_input("1h"), replay_input("1h")),
        lambda: (replay_input("1h"), replay_input("15m", workspace_id=uuid4())),
        lambda: (replay_input("1h"), replay_input("15m", symbol="ETHUSDT")),
        lambda: (
            replay_input("1h"),
            replay_input("15m", evaluation_start=NOW - timedelta(hours=2)),
        ),
        lambda: (
            replay_input("1h"),
            replay_input("15m", policy_fingerprint=hashlib.sha256(b"policy-v2").hexdigest()),
        ),
    ],
    ids=("duplicate-timeframe", "workspace", "symbol", "evaluation-window", "rights-policy"),
)
def test_bundle_rejects_components_with_mismatched_identity_or_rights(inputs) -> None:
    with pytest.raises(BacktestDatasetBundleMismatch):
        build_backtest_dataset_replay_bundle(inputs())


def test_bundle_rejects_incomplete_child_manifest() -> None:
    incomplete = replay_input("1h", incomplete=True)
    assert isinstance(incomplete.manifest, BacktestDatasetManifest)

    with pytest.raises(BacktestDatasetNotReady, match="chưa sẵn sàng"):
        build_backtest_dataset_replay_bundle((incomplete, replay_input("15m")))


def test_bundle_rejects_current_rights_or_bar_payload_changed_after_loader() -> None:
    first = replay_input("1h")
    second = replay_input("15m")
    changed_rights = replace(
        second,
        current_rights=replace(
            second.current_rights,
            policy_fingerprint=hashlib.sha256(b"policy-v2").hexdigest(),
        ),
    )
    with pytest.raises(BacktestDatasetBundleMismatch, match="Rights evidence"):
        build_backtest_dataset_replay_bundle((first, changed_rights))

    changed_bar = replace(second.bars[0], close=Decimal("100.5"))
    changed_bars = replace(second, bars=(changed_bar, *second.bars[1:]))
    with pytest.raises(BacktestDatasetBundleMismatch, match="Hash bars"):
        build_backtest_dataset_replay_bundle((first, changed_bars))
