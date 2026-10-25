"""Hợp đồng shared signal evidence: trung lập, point-in-time, provenance và scope workspace."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from trading_bot.domain.market.feature_snapshot import (
    DataFreshness,
    FeatureSnapshot,
    MarketRegime,
    RegimeAssessment,
)
from trading_bot.domain.market.signal_evidence import (
    EvidenceSourceRef,
    ModelReleaseRef,
    SharedSignalEvidence,
    SignalEvidenceError,
    build_shared_signal_evidence,
    signal_evidence_id,
)

WORKSPACE_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
WORKSPACE_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
MODEL_RELEASE = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
OBSERVED = datetime(2026, 10, 20, 10, tzinfo=UTC)
AVAILABLE = OBSERVED + timedelta(minutes=1)
VALID_UNTIL = OBSERVED + timedelta(hours=2)
FEATURE_ID = "f" * 64


def _feature(
    *,
    workspace_id: UUID = WORKSPACE_A,
    as_of: datetime = OBSERVED,
    computed_at: datetime | None = None,
    usable: bool = True,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        snapshot_id=FEATURE_ID,
        content_hash=FEATURE_ID,
        workspace_id=workspace_id,
        symbol="BTCUSDT",
        timeframe="1h",
        as_of=as_of,
        computed_at=computed_at or as_of + timedelta(seconds=30),
        schema_hash="a" * 64,
        formula_hash="b" * 64,
        code_hash="c" * 64,
        input_bar_ids=("bar-1",),
        input_event_ids=("event-1",),
        features={"atr_14": 2.0},
        warmup={"atr_14": True},
        quality_flags=(),
        data_freshness=DataFreshness.FRESH,
        data_coverage=1.0,
        regime=RegimeAssessment(MarketRegime.TREND, 0.8, 1.0, "FIXTURE", "Dữ liệu kiểm thử."),
        is_usable=usable,
    )


def _sources(*refs: str, available_at: datetime = AVAILABLE) -> tuple[EvidenceSourceRef, ...]:
    return tuple(EvidenceSourceRef(ref=ref, available_at=available_at) for ref in refs)


def _evidence(**overrides: object) -> SharedSignalEvidence:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_A,
        "symbol": "BTCUSDT",
        "setup_type": "donchian_breakout_high",
        "observed_at": OBSERVED,
        "available_at": AVAILABLE,
        "validity_end": VALID_UNTIL,
        "feature_snapshot_id": FEATURE_ID,
        "source_refs": ("market_bar:binance:BTCUSDT:1h:2026-10-20T09",),
        "model_release_id": None,
    }
    values.update(overrides)
    return SharedSignalEvidence.create(**values)  # type: ignore[arg-type]


def test_create_derives_stable_hash_and_workspace_scoped_identity() -> None:
    first = _evidence()
    second = _evidence()

    assert first == second
    assert len(first.evidence_hash) == 64
    assert len(first.content_hash) == 64
    assert first.evidence_id == signal_evidence_id(WORKSPACE_A, first.content_hash)
    assert first.canonical_json() == second.canonical_json()


def test_same_content_in_another_workspace_has_different_hash_and_id() -> None:
    first = _evidence()
    foreign = _evidence(workspace_id=WORKSPACE_B)

    assert foreign.evidence_hash != first.evidence_hash
    assert foreign.evidence_id != first.evidence_id


def test_retry_with_later_timestamps_keeps_the_observation_identity() -> None:
    """Re-run/retry detector dùng đồng hồ wall-clock không được tạo evidence mới."""
    first = _evidence()
    retry = _evidence(
        available_at=AVAILABLE + timedelta(minutes=3),
        validity_end=VALID_UNTIL + timedelta(minutes=3),
    )

    assert retry.content_hash == first.content_hash
    assert retry.evidence_id == first.evidence_id
    assert retry.evidence_hash != first.evidence_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"observed_at": OBSERVED - timedelta(minutes=1)},
        {"setup_type": "range_reversion"},
        {"source_refs": ("market_bar:binance:BTCUSDT:1h:2026-10-20T09", "research:revised")},
        {"model_release_id": MODEL_RELEASE},
        {"feature_snapshot_id": "e" * 64},
    ],
)
def test_content_change_is_a_new_observation_identity(overrides: dict[str, object]) -> None:
    base = _evidence()
    changed = _evidence(**overrides)

    assert changed.content_hash != base.content_hash
    assert changed.evidence_id != base.evidence_id


def test_source_ref_order_does_not_change_provenance_hash() -> None:
    first = _evidence(source_refs=("source-a", "source-b"))
    second = _evidence(source_refs=("source-b", "source-a"))

    assert first.evidence_hash == second.evidence_hash
    assert first.source_refs == ("source-a", "source-b")


def test_datetimes_are_normalized_to_utc_before_hashing() -> None:
    plus_seven = timezone(timedelta(hours=7))
    shifted = _evidence(
        observed_at=OBSERVED.astimezone(plus_seven),
        available_at=AVAILABLE.astimezone(plus_seven),
        validity_end=VALID_UNTIL.astimezone(plus_seven),
    )

    assert shifted == _evidence()
    assert shifted.observed_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"observed_at": datetime(2026, 10, 20, 10)}, "múi giờ"),  # noqa: DTZ001
        ({"available_at": OBSERVED - timedelta(seconds=1)}, "observed_at"),
        ({"validity_end": AVAILABLE}, "validity_end"),
        ({"source_refs": ()}, "source"),
        ({"source_refs": ("dup", "dup")}, "source"),
        ({"source_refs": (" ",)}, "source"),
        ({"feature_snapshot_id": "not-a-hash"}, "feature_snapshot_id"),
        ({"symbol": " "}, "symbol"),
        ({"setup_type": "Breakout With Spaces"}, "setup_type"),
    ],
)
def test_invalid_causality_scope_or_provenance_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _evidence(**overrides)


def test_tampered_hash_or_identity_is_rejected() -> None:
    evidence = _evidence()

    with pytest.raises(ValueError, match="evidence_hash"):
        replace(evidence, evidence_hash="0" * 64)
    with pytest.raises(ValueError, match="evidence_id"):
        replace(evidence, evidence_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"))
    with pytest.raises(ValueError, match="evidence_hash"):
        replace(evidence, symbol="ETHUSDT")


def test_shared_evidence_has_no_bot_account_quantity_or_order_fields() -> None:
    forbidden = {
        "bot_id",
        "account_id",
        "trading_account_id",
        "quantity",
        "notional",
        "size",
        "leverage",
        "side",
        "direction",
        "order_id",
        "order_type",
        "intent_id",
        "reservation_id",
        "strategy_release_id",
        "decision",
    }
    field_names = {field.name for field in dataclasses.fields(SharedSignalEvidence)}

    assert field_names == {
        "workspace_id",
        "evidence_id",
        "symbol",
        "setup_type",
        "observed_at",
        "available_at",
        "validity_end",
        "feature_snapshot_id",
        "source_refs",
        "model_release_id",
        "evidence_hash",
    }
    assert not field_names & forbidden
    assert not set(_evidence().canonical_payload()) & forbidden


def test_evidence_is_usable_only_inside_its_validity_window() -> None:
    evidence = _evidence()

    assert evidence.unavailable_reason(AVAILABLE) is None
    assert evidence.unavailable_reason(VALID_UNTIL - timedelta(microseconds=1)) is None
    assert evidence.unavailable_reason(AVAILABLE - timedelta(microseconds=1)) == (
        "EVIDENCE_NOT_YET_AVAILABLE"
    )
    assert evidence.unavailable_reason(VALID_UNTIL) == "EVIDENCE_EXPIRED"
    with pytest.raises(SignalEvidenceError) as rejected:
        evidence.require_usable_at(VALID_UNTIL + timedelta(hours=1))
    assert rejected.value.code == "EVIDENCE_EXPIRED"
    with pytest.raises(ValueError, match="múi giờ"):
        evidence.unavailable_reason(datetime(2026, 10, 20, 10, 30))  # noqa: DTZ001


def test_builder_pins_feature_sources_and_model_release_lineage() -> None:
    evidence = build_shared_signal_evidence(
        feature_snapshot=_feature(),
        setup_type="donchian_breakout_high",
        observed_at=OBSERVED,
        available_at=AVAILABLE,
        validity_end=VALID_UNTIL,
        sources=_sources("research:macro-calendar:2026-10-20", "market_bar:BTCUSDT:1h"),
        model_release=ModelReleaseRef(release_id=MODEL_RELEASE, available_at=AVAILABLE),
    )

    assert evidence.workspace_id == WORKSPACE_A
    assert evidence.symbol == "BTCUSDT"
    assert evidence.feature_snapshot_id == FEATURE_ID
    assert evidence.model_release_id == MODEL_RELEASE
    assert evidence.source_refs == (
        "market_bar:BTCUSDT:1h",
        "research:macro-calendar:2026-10-20",
    )


@pytest.mark.parametrize(
    ("feature", "sources", "model_release", "code"),
    [
        (
            _feature(computed_at=AVAILABLE + timedelta(seconds=1)),
            _sources("market_bar:1"),
            None,
            "FEATURE_NOT_AVAILABLE_AT_EVIDENCE",
        ),
        (
            _feature(as_of=OBSERVED + timedelta(seconds=1), computed_at=AVAILABLE),
            _sources("market_bar:1"),
            None,
            "FEATURE_AFTER_OBSERVATION",
        ),
        (
            _feature(),
            _sources("research:revised", available_at=AVAILABLE + timedelta(minutes=5)),
            None,
            "SOURCE_NOT_AVAILABLE_AT_EVIDENCE",
        ),
        (
            _feature(),
            _sources("market_bar:1"),
            ModelReleaseRef(release_id=MODEL_RELEASE, available_at=AVAILABLE + timedelta(days=1)),
            "MODEL_RELEASE_NOT_AVAILABLE_AT_EVIDENCE",
        ),
        (_feature(usable=False), _sources("market_bar:1"), None, "FEATURE_SNAPSHOT_UNUSABLE"),
        (_feature(), (), None, "SOURCE_REFS_REQUIRED"),
    ],
)
def test_builder_rejects_inputs_not_available_at_evidence_time(
    feature: FeatureSnapshot,
    sources: tuple[EvidenceSourceRef, ...],
    model_release: ModelReleaseRef | None,
    code: str,
) -> None:
    with pytest.raises(SignalEvidenceError) as rejected:
        build_shared_signal_evidence(
            feature_snapshot=feature,
            setup_type="donchian_breakout_high",
            observed_at=OBSERVED,
            available_at=AVAILABLE,
            validity_end=VALID_UNTIL,
            sources=sources,
            model_release=model_release,
        )

    assert rejected.value.code == code


def test_later_revision_is_new_evidence_and_unusable_for_earlier_evaluation() -> None:
    original = _evidence()
    revision = _evidence(
        available_at=AVAILABLE + timedelta(minutes=30),
        source_refs=(*original.source_refs, "market_bar:correction:1"),
    )
    historical_time = AVAILABLE + timedelta(minutes=10)

    assert revision.evidence_id != original.evidence_id
    assert revision.content_hash != original.content_hash
    assert original.unavailable_reason(historical_time) is None
    assert revision.unavailable_reason(historical_time) == "EVIDENCE_NOT_YET_AVAILABLE"


def test_payload_round_trip_restores_identical_evidence() -> None:
    evidence = _evidence(model_release_id=MODEL_RELEASE)

    restored = SharedSignalEvidence.from_canonical_json(evidence.canonical_json())

    assert restored == evidence
