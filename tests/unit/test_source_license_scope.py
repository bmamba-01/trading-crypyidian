"""Kiểm thử phạm vi quyền, chuẩn hóa và băm license thuần."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256

import pytest

from trading_bot.domain.research.license_scope import (
    SourceLicenseScope,
    SourceUseScope,
    canonical_scope_json,
    scope_sha256,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 2, 1, tzinfo=UTC)


def license_scope() -> SourceLicenseScope:
    return SourceLicenseScope(
        source_id="licensed-source",
        provider="PROVIDER",
        product="OHLCV",
        venue="BINANCE",
        contract="USD_M",
        symbols=("ETHUSDT", "BTCUSDT"),
        intervals=("15m", "1h"),
        data_start=START,
        data_end=END,
        allowed_purposes=(SourceUsePurpose.TRAINING, SourceUsePurpose.PAPER_DEMO),
        allowed_outputs=("trained_model", "derived_features"),
        max_retention_days=None,
        attribution="Nguồn đã cấp phép",
        derived_use=("model_training", "feature_extraction"),
    )


def use_scope() -> SourceUseScope:
    return SourceUseScope(
        source_id="licensed-source",
        provider="PROVIDER",
        product="OHLCV",
        venue="BINANCE",
        contract="USD_M",
        symbols=("BTCUSDT",),
        intervals=("15m",),
        data_start=START,
        data_end=END,
        purpose=SourceUsePurpose.TRAINING,
        outputs=("trained_model",),
        derived_use=("model_training",),
        retention_days=None,
    )


def test_scope_normalizes_order_and_timezone_without_mutating_inputs() -> None:
    scope = license_scope()
    shifted = replace(
        scope,
        data_start=START.astimezone(timezone(timedelta(hours=7))),
        symbols=tuple(reversed(scope.symbols)),
        intervals=tuple(reversed(scope.intervals)),
    )
    assert scope.symbols == ("BTCUSDT", "ETHUSDT")
    assert scope.allowed_purposes == (SourceUsePurpose.PAPER_DEMO, SourceUsePurpose.TRAINING)
    assert shifted == scope
    assert shifted.data_start.tzinfo is UTC
    assert canonical_scope_json(shifted) == canonical_scope_json(scope)
    with pytest.raises(FrozenInstanceError):
        scope.provider = "OTHER"  # type: ignore[misc]


@pytest.mark.parametrize(
    "field", ["symbols", "intervals", "allowed_purposes", "allowed_outputs", "derived_use"]
)
def test_license_rejects_duplicate_members_before_sorting(field: str) -> None:
    scope = license_scope()
    member = getattr(scope, field)[0]
    with pytest.raises(ValueError, match=field):
        replace(scope, **{field: (member, member)})


@pytest.mark.parametrize("field", ["symbols", "intervals", "outputs", "derived_use"])
def test_use_rejects_duplicate_and_empty_members(field: str) -> None:
    use = use_scope()
    member = getattr(use, field)[0]
    for invalid in ((member, member), ()):
        with pytest.raises(ValueError, match=field):
            replace(use, **{field: invalid})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "*"),
        ("source_id", ""),
        ("provider", "provider"),
        ("product", "ALL*"),
        ("venue", "SÀN"),
        ("contract", ""),
        ("symbols", ("btcusdt",)),
        ("symbols", ("BTC*",)),
        ("symbols", ("",)),
        ("symbols", ()),
        ("symbols", "BTCUSDT"),
        ("intervals", ("0m",)),
        ("intervals", ("01m",)),
        ("intervals", ("1H",)),
        ("intervals", ("*",)),
        ("intervals", ()),
        ("allowed_outputs", ("raw_data",)),
        ("allowed_outputs", ()),
        ("derived_use", ("redistribute",)),
        ("derived_use", ()),
        ("allowed_purposes", ("training",)),
        ("allowed_purposes", ()),
        ("attribution", "  "),
        ("max_retention_days", 0),
        ("max_retention_days", -1),
        ("max_retention_days", True),
        ("max_retention_days", 1.5),
        ("data_start", START.replace(tzinfo=None)),
        ("data_end", START),
        ("data_end", START - timedelta(microseconds=1)),
    ],
)
def test_license_rejects_invalid_or_ambiguous_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        replace(license_scope(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purpose", "training"),
        ("retention_days", 0),
        ("retention_days", True),
        ("provider", "*"),
        ("symbols", ("*",)),
        ("data_end", START),
    ],
)
def test_use_requires_complete_valid_dimensions(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        replace(use_scope(), **{field: value})


def test_scope_covers_matching_half_open_range_and_subsets() -> None:
    scope = license_scope()
    assert scope.covers(use_scope())
    assert scope.covers(
        replace(
            use_scope(),
            data_start=START + timedelta(microseconds=1),
            data_end=END - timedelta(microseconds=1),
        )
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "other-source"),
        ("provider", "OTHER"),
        ("product", "OTHER"),
        ("venue", "OTHER"),
        ("contract", "OTHER"),
        ("symbols", ("SOLUSDT",)),
        ("intervals", ("1d",)),
        ("purpose", SourceUsePurpose.REDISTRIBUTION),
        ("outputs", ("aggregate_report",)),
        ("derived_use", ("aggregate_reporting",)),
        ("data_start", START - timedelta(microseconds=1)),
        ("data_end", END + timedelta(microseconds=1)),
    ],
)
def test_scope_denies_each_uncovered_dimension(field: str, value: object) -> None:
    assert not license_scope().covers(replace(use_scope(), **{field: value}))


def test_scope_does_not_cover_range_starting_at_exclusive_end() -> None:
    assert not license_scope().covers(
        replace(use_scope(), data_start=END, data_end=END + timedelta(microseconds=1))
    )


@pytest.mark.parametrize(
    ("maximum", "requested", "expected"),
    [
        (None, None, True),
        (None, 31, True),
        (30, None, False),
        (30, 29, True),
        (30, 30, True),
        (30, 31, False),
    ],
)
def test_retention_requires_explicit_permission_for_indefinite_use(
    maximum: int | None,
    requested: int | None,
    expected: bool,
) -> None:
    assert (
        replace(license_scope(), max_retention_days=maximum).covers(
            replace(use_scope(), retention_days=requested)
        )
        is expected
    )


def test_canonical_json_and_utf8_digest_match_fixed_contract() -> None:
    expected = (
        '{"allowed_outputs":["derived_features","trained_model"],'
        '"allowed_purposes":["paper_demo","training"],"attribution":"Nguồn đã cấp phép",'
        '"contract":"USD_M","data_end":"2026-02-01T00:00:00.000000Z",'
        '"data_start":"2026-01-01T00:00:00.000000Z",'
        '"derived_use":["feature_extraction","model_training"],"intervals":["15m","1h"],'
        '"max_retention_days":null,"product":"OHLCV","provider":"PROVIDER",'
        '"scope_version":"wp06-license-scope-v1","source_id":"licensed-source",'
        '"symbols":["BTCUSDT","ETHUSDT"],"venue":"BINANCE"}'
    )
    assert canonical_scope_json(license_scope()) == expected
    assert scope_sha256(license_scope()) == sha256(expected.encode("utf-8")).hexdigest()
    assert scope_sha256(replace(license_scope(), attribution="Khác")) != scope_sha256(
        license_scope()
    )
