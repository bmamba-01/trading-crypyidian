from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.backtest_dataset_manifest import (
    BacktestDatasetNotReady,
    backtest_dataset_bars_hash,
    build_backtest_dataset_manifest,
)
from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
START = NOW - timedelta(minutes=4)
POLICY_FINGERPRINT = hashlib.sha256(b"backtest-rights-v1").hexdigest()
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")


def request(**changes: object) -> RestBackfillRequest:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "start": START,
        "end": NOW,
        "purpose": BackfillRightsPurpose.BACKTEST_RESEARCH,
        "page_limit": 10,
    }
    values.update(changes)
    return RestBackfillRequest(**values)  # type: ignore[arg-type]


def evidence(**changes: object) -> BackfillRightsEvidence:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "provider": "BINANCE_USDM_REST",
        "purpose": BackfillRightsPurpose.BACKTEST_RESEARCH,
        "terms_url": "https://example.com/terms",
        "policy_fingerprint": POLICY_FINGERPRINT,
        "verified_at": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
    }
    values.update(changes)
    return BackfillRightsEvidence(**values)  # type: ignore[arg-type]


def bar(open_time: datetime, **changes: object) -> OHLCVBar:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "open_time": open_time,
        "close_time": open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
        "event_at": open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
        "received_at": NOW,
        "available_at": NOW,
        "open": Decimal("100"),
        "high": Decimal("102"),
        "low": Decimal("99"),
        "close": Decimal("101"),
        "volume": Decimal("1.5"),
        "quote_volume": None,
        "is_closed": True,
        "source": "BINANCE_USDM_REST_BACKFILL",
        "source_cursor": str(int(open_time.timestamp() * 1000)),
        "checksum": hashlib.sha256(open_time.isoformat().encode()).hexdigest(),
    }
    values.update(changes)
    return OHLCVBar(**values)  # type: ignore[arg-type]


def build(
    bars: tuple[OHLCVBar, ...],
    *,
    request_value: RestBackfillRequest | None = None,
    evidence_value: BackfillRightsEvidence | None = None,
    evaluation_start: datetime | None = None,
    warmup_bars: int = 2,
    schema_version: int = 3,
):
    return build_backtest_dataset_manifest(
        request_value or request(),
        evaluation_start=evaluation_start or START + timedelta(minutes=warmup_bars),
        warmup_bars=warmup_bars,
        bars=bars,
        rights_evidence=evidence_value or evidence(),
        prepared_at=NOW,
        schema_version=schema_version,
    )


def complete_bars() -> tuple[OHLCVBar, ...]:
    return tuple(bar(START + timedelta(minutes=index)) for index in range(4))


def test_manifest_separates_warmup_and_evaluation_and_hashes_complete_dataset() -> None:
    manifest = build(complete_bars())

    assert manifest.is_ready
    assert manifest.coverage_ratio == 1.0
    assert manifest.expected_bar_count == 4
    assert manifest.bar_count == 4
    assert manifest.warmup_bar_count == 2
    assert manifest.evaluation_bar_count == 2
    assert manifest.evaluation_start == START + timedelta(minutes=2)
    assert manifest.range_end == NOW
    assert manifest.missing_open_times == ()
    assert len(manifest.dataset_hash) == 64
    assert manifest.request_fingerprint == request().fingerprint
    assert manifest.policy_fingerprint == POLICY_FINGERPRINT
    assert manifest.rights_provider == "BINANCE_USDM_REST"
    assert manifest.rights_purpose == "BACKTEST_RESEARCH"
    assert manifest.require_ready() is manifest


def test_manifest_hash_preimage_removes_embedded_hash_without_reordering_bytes() -> None:
    manifest = build(complete_bars())
    member = f',"dataset_hash":"{manifest.dataset_hash}"'
    canonical_payload = manifest.canonical_json
    preimage = canonical_payload.replace(member, "")
    payload_without_hash = json.loads(canonical_payload)
    payload_without_hash.pop("dataset_hash")
    expected_preimage = json.dumps(
        payload_without_hash, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    assert canonical_payload.count(member) == 1
    assert preimage == expected_preimage
    assert hashlib.sha256(preimage.encode("utf-8")).hexdigest() == manifest.dataset_hash


def test_manifest_supports_an_explicitly_empty_warmup_window() -> None:
    manifest = build(complete_bars(), evaluation_start=START, warmup_bars=0)

    assert manifest.is_ready
    assert manifest.warmup_bar_count == 0
    assert manifest.warmup_observed_bar_count == 0
    assert manifest.evaluation_bar_count == 4
    assert manifest.evaluation_observed_bar_count == 4


def test_manifest_uses_authoritative_rights_check_time_as_snapshot_floor() -> None:
    checked_at = NOW + timedelta(seconds=2)

    manifest = build(
        complete_bars(),
        evidence_value=evidence(checked_at=checked_at),
    )

    assert manifest.prepared_at == checked_at
    assert manifest.rights_checked_at == checked_at


def test_manifest_reports_exact_gap_and_refuses_incomplete_dataset_for_replay() -> None:
    bars = tuple(item for item in complete_bars() if item.open_time != START + timedelta(minutes=2))

    manifest = build(bars)

    assert not manifest.is_ready
    assert manifest.coverage_ratio == 0.75
    assert manifest.bar_count == 3
    assert manifest.missing_open_times == (START + timedelta(minutes=2),)
    with pytest.raises(BacktestDatasetNotReady, match="thiếu nến"):
        manifest.require_ready()


def test_manifest_rejects_rights_that_are_expired_unverified_for_purpose_or_mismatched() -> None:
    with pytest.raises(PermissionError, match="hết hiệu lực"):
        build(complete_bars(), evidence_value=evidence(valid_until=NOW))
    with pytest.raises(PermissionError, match="chưa có hiệu lực"):
        build(complete_bars(), evidence_value=evidence(verified_at=NOW + timedelta(seconds=1)))
    with pytest.raises(PermissionError, match="mục đích"):
        build(complete_bars(), evidence_value=evidence(purpose=BackfillRightsPurpose.PAPER_DEMO))
    with pytest.raises(PermissionError, match="mục đích"):
        build(
            complete_bars(),
            request_value=request(purpose=BackfillRightsPurpose.PAPER_DEMO),
        )
    with pytest.raises(ValueError, match="provider"):
        build(complete_bars(), evidence_value=evidence(provider="OTHER"))


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ({"is_closed": False}, "chưa đóng"),
        ({"available_at": NOW + timedelta(microseconds=1)}, "chưa available"),
        ({"source": "BINANCE_USDM"}, "source"),
        ({"symbol": "ETHUSDT"}, "symbol/timeframe"),
        ({"timeframe": "5m"}, "symbol/timeframe"),
    ],
)
def test_manifest_rejects_bars_not_eligible_for_the_requested_snapshot(
    changed: dict[str, object], message: str
) -> None:
    bars = list(complete_bars())
    bars[0] = replace(bars[0], **changed)

    with pytest.raises(ValueError, match=message):
        build(tuple(bars))


def test_manifest_rejects_duplicate_and_off_grid_bars_instead_of_silently_deduplicating() -> None:
    bars = complete_bars()

    with pytest.raises(ValueError, match="trùng"):
        build((*bars, bars[0]))
    with pytest.raises(ValueError, match="lưới interval"):
        build((*bars[:1], bar(START + timedelta(seconds=30)), *bars[1:]))


def test_manifest_rejects_bar_outside_requested_range_or_with_wrong_close_boundary() -> None:
    with pytest.raises(ValueError, match="ngoài khoảng"):
        build(
            (
                *complete_bars(),
                bar(
                    NOW,
                    received_at=NOW + timedelta(minutes=1),
                    available_at=NOW + timedelta(minutes=1),
                ),
            )
        )

    bars = list(complete_bars())
    bars[0] = replace(bars[0], close_time=bars[0].close_time - timedelta(milliseconds=1))
    with pytest.raises(ValueError, match="close_time"):
        build(tuple(bars))


def test_manifest_rejects_misaligned_evaluation_start_and_warmup_mismatch() -> None:
    with pytest.raises(ValueError, match="warm-up"):
        build(complete_bars(), evaluation_start=START + timedelta(minutes=1))
    with pytest.raises(ValueError, match="warmup_bars"):
        build(complete_bars(), warmup_bars=-1)


def test_manifest_rejects_a_requested_backtest_range_that_extends_into_the_future() -> None:
    future_request = request(end=NOW + timedelta(minutes=1))

    with pytest.raises(ValueError, match="range_end phải không muộn hơn prepared_at"):
        build(complete_bars(), request_value=future_request)


def test_manifest_rejects_warmup_larger_than_request_before_timedelta_arithmetic() -> None:
    with pytest.raises(ValueError, match="warmup_bars"):
        build(complete_bars(), evaluation_start=START, warmup_bars=10**1000)


def test_manifest_hash_changes_when_bar_or_policy_provenance_changes() -> None:
    first = build(complete_bars())
    changed_bar = replace(complete_bars()[0], close=Decimal("100.5"))
    changed_data = build((changed_bar, *complete_bars()[1:]))
    changed_policy = build(
        complete_bars(),
        evidence_value=evidence(
            policy_fingerprint=hashlib.sha256(b"backtest-rights-v2").hexdigest()
        ),
    )

    assert len({first.dataset_hash, changed_data.dataset_hash, changed_policy.dataset_hash}) == 3


def test_manifest_v3_hash_binds_quote_volume_and_keeps_legacy_hash_version() -> None:
    bars_without_quote = complete_bars()
    bars_with_quote = tuple(
        replace(item, quote_volume=Decimal("150.25")) for item in bars_without_quote
    )

    assert backtest_dataset_bars_hash(
        bars_without_quote, schema_version=2
    ) == backtest_dataset_bars_hash(bars_with_quote, schema_version=2)
    assert backtest_dataset_bars_hash(
        bars_without_quote, schema_version=3
    ) != backtest_dataset_bars_hash(bars_with_quote, schema_version=3)


def test_manifest_reader_preserves_schema_v2_canonical_payload() -> None:
    current = build(complete_bars())
    payload = json.loads(current.canonical_json)
    payload["schema_version"] = 2
    payload["bars_content_hash"] = backtest_dataset_bars_hash(complete_bars(), schema_version=2)
    payload.pop("dataset_hash")
    legacy_canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    legacy_hash = hashlib.sha256(legacy_canonical.encode("utf-8")).hexdigest()
    payload["dataset_hash"] = legacy_hash
    legacy_canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    restored = type(current).from_canonical_json(
        legacy_canonical,
        expected_dataset_hash=legacy_hash,
    )

    assert restored.schema_version == 2
    assert restored.canonical_json == legacy_canonical


def test_manifest_rejects_a_tampered_canonical_hash() -> None:
    manifest = build(complete_bars())

    with pytest.raises(ValueError, match="dataset_hash không khớp"):
        replace(manifest, dataset_hash="0" * 64)


def test_manifest_rejects_non_bar_input_with_domain_error() -> None:
    with pytest.raises(ValueError, match="chỉ được chứa OHLCVBar"):
        build((*complete_bars(), object()))  # type: ignore[arg-type]


def test_manifest_round_trips_canonical_json_and_checks_expected_hash() -> None:
    manifest = build(complete_bars())

    restored = type(manifest).from_canonical_json(
        manifest.canonical_json,
        expected_dataset_hash=manifest.dataset_hash,
    )

    assert restored == manifest
    assert restored.canonical_json == manifest.canonical_json
    with pytest.raises(ValueError, match="expected_dataset_hash"):
        type(manifest).from_canonical_json(
            manifest.canonical_json,
            expected_dataset_hash="0" * 64,
        )


def test_manifest_v4_binds_ordered_market_bar_record_identities() -> None:
    bars = list(complete_bars())
    for index, item in enumerate(bars):
        checksum = item.checksum
        assert checksum is not None
        bars[index] = replace(
            item,
            source_identity=MarketBarSourceIdentity(
                workspace_id=WORKSPACE_ID,
                record_id=UUID(f"00000000-0000-4000-8000-{index + 1:012d}"),
                logical_key=f"BINANCE_USDM_REST_BACKFILL:BTCUSDT:1m:{item.open_time.isoformat()}",
                revision=1,
                supersedes_record_id=None,
                checksum=checksum,
            ),
        )

    manifest = build(tuple(bars), schema_version=4)

    assert manifest.schema_version == 4
    assert manifest.bar_identity_hash is not None
    assert manifest.has_pinned_source_identities
    changed_identity = replace(
        bars[0],
        source_identity=replace(
            bars[0].source_identity,
            record_id=UUID("00000000-0000-4000-8000-000000000099"),
        ),
    )
    changed_manifest = build((changed_identity, *bars[1:]), schema_version=4)
    assert changed_manifest.bar_identity_hash != manifest.bar_identity_hash
    assert changed_manifest.dataset_hash != manifest.dataset_hash


def test_manifest_v4_fails_closed_without_authoritative_market_bar_identity() -> None:
    with pytest.raises(ValueError, match="(?i)identity nguồn.*bắt buộc"):
        build(complete_bars(), schema_version=4)


@pytest.mark.parametrize("schema_version", [2, 3])
def test_legacy_manifest_versions_are_not_pinned_to_source_identities(
    schema_version: int,
) -> None:
    manifest = build(complete_bars(), schema_version=schema_version)

    assert manifest.bar_identity_hash is None
    assert not manifest.has_pinned_source_identities


def test_manifest_canonical_json_parser_rejects_noncanonical_or_tampered_payload() -> None:
    manifest = build(complete_bars())
    tampered = manifest.canonical_json.replace('"status":"READY"', '"status":"INCOMPLETE"')

    with pytest.raises(ValueError, match="canonical|hash|status"):
        type(manifest).from_canonical_json(
            tampered,
            expected_dataset_hash=manifest.dataset_hash,
        )
