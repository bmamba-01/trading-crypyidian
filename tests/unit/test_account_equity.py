"""Hợp đồng vốn đúng thời điểm và quy đổi fail-closed của MB04.1."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest

from trading_bot.domain.risk.equity import (
    EquityValidationError,
    MarketMark,
    OpenPositionValuation,
    PaperEquityBasis,
    RiskConversionObservation,
    VenueEquityBasis,
    mark_to_market_equity,
    normalize_equity_to_usdt,
)

D = Decimal
NOW = datetime(2026, 10, 25, 8, tzinfo=UTC)
AGE = timedelta(seconds=30)


def basis() -> PaperEquityBasis:
    return PaperEquityBasis(
        workspace_id="w",
        bot_id="b",
        account_id="a",
        asset="USDT",
        as_of=NOW,
        captured_at=NOW,
        available_at=NOW,
        source="paper-ledger",
        source_version="v1",
        max_mark_age=AGE,
        max_equity_age=AGE,
        initial_capital_before_realized_pnl=D("1000"),
        cumulative_realized_pnl=D("-12.5"),
    )


def position(direction: str = "long") -> OpenPositionValuation:
    return OpenPositionValuation(
        "w", "b", "a", "p", "BTCUSDT", "USDT", direction, D("2"), D("100"), D("1"), NOW
    )


def mark() -> MarketMark:
    return MarketMark(
        "w", "b", "a", "BTCUSDT", "USDT", D("110"), NOW, NOW, "public-mark", "v1", "raw-hash"
    )


def conversion() -> RiskConversionObservation:
    return RiskConversionObservation(
        "w",
        "BTC",
        "USDT",
        D("3.333333333333"),
        NOW,
        NOW,
        "public-fx",
        "v1",
        "raw-fx",
        "policy-v1",
        "DIRECT",
        True,
    )


def assert_code(code: str, call: object) -> None:
    with pytest.raises(EquityValidationError) as error:
        call()  # type: ignore[operator]
    assert error.value.reason_code == code


@pytest.mark.parametrize(
    ("direction", "price", "expected"),
    [
        ("long", "110", "1007.5"),
        ("long", "90", "967.5"),
        ("short", "110", "967.5"),
        ("short", "90", "1007.5"),
    ],
)
def test_paper_signed_mark_to_market(direction: str, price: str, expected: str) -> None:
    result = mark_to_market_equity(
        basis(), [position(direction)], {"BTCUSDT": replace(mark(), price=D(price))}, NOW
    )
    assert result.equity_amount == D(expected)
    assert result.cumulative_realized_pnl == D("-12.5")
    assert normalize_equity_to_usdt(result, None, NOW, AGE) == D(expected)
    with pytest.raises(FrozenInstanceError):
        result.asset = "BTC"  # type: ignore[misc]


def test_multiple_positions_and_venue_realized_not_duplicated() -> None:
    paper = basis()
    venue = VenueEquityBasis(
        **{
            key: getattr(paper, key)
            for key in (
                "workspace_id",
                "bot_id",
                "account_id",
                "asset",
                "as_of",
                "captured_at",
                "available_at",
                "source",
                "source_version",
                "max_mark_age",
                "max_equity_age",
            )
        },
        total_equity_including_realized_and_excluding_unrealized=D("987.5"),
    )
    positions = [position(), replace(position("short"), position_id="p2", quantity=D("1"))]
    assert mark_to_market_equity(venue, positions, {"BTCUSDT": mark()}, NOW).equity_amount == D(
        "997.5"
    )
    assert mark_to_market_equity(paper, positions, {"BTCUSDT": mark()}, NOW).equity_amount == D(
        "997.5"
    )


def test_lineage_is_order_independent_and_changes_with_observation() -> None:
    positions = [position(), replace(position("short"), position_id="p2")]
    first = mark_to_market_equity(basis(), positions, {"BTCUSDT": mark()}, NOW)
    assert (
        first.source_hash
        == mark_to_market_equity(basis(), positions[::-1], {"BTCUSDT": mark()}, NOW).source_hash
    )
    changed = replace(mark(), source_hash="another")
    assert (
        first.source_hash
        != mark_to_market_equity(basis(), positions, {"BTCUSDT": changed}, NOW).source_hash
    )
    assert first.mark_refs[0].source_hash == "raw-hash"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("price", 1.0, "DECIMAL_REQUIRED"),
        ("price", D("NaN"), "AMOUNT_INVALID"),
        ("price", D("Infinity"), "AMOUNT_INVALID"),
        ("price", D("0"), "AMOUNT_INVALID"),
        ("price", D("-1"), "AMOUNT_INVALID"),
        ("asset", "BTC", "CURRENCY_MISMATCH"),
        ("workspace_id", "other", "SCOPE_MISMATCH"),
        ("as_of", NOW - AGE - timedelta(microseconds=1), "MARK_STALE"),
        ("as_of", NOW + timedelta(seconds=1), "OBSERVATION_FUTURE"),
        ("available_at", NOW + timedelta(seconds=1), "OBSERVATION_FUTURE"),
        ("as_of", NOW.replace(tzinfo=None), "TIMESTAMP_INVALID"),
    ],
)
def test_invalid_marks(field: str, value: object, code: str) -> None:
    assert_code(
        code,
        lambda: mark_to_market_equity(
            basis(), [position()], {"BTCUSDT": replace(mark(), **{field: value})}, NOW
        ),
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("quantity", 1.0, "DECIMAL_REQUIRED"),
        ("entry_price", D("0"), "AMOUNT_INVALID"),
        ("contract_multiplier", D("-1"), "AMOUNT_INVALID"),
        ("account_id", "other", "SCOPE_MISMATCH"),
        ("asset", "BTC", "CURRENCY_MISMATCH"),
        ("direction", "BUY", "DIRECTION_INVALID"),
        ("opened_at", NOW + timedelta(seconds=1), "OBSERVATION_FUTURE"),
    ],
)
def test_invalid_positions(field: str, value: object, code: str) -> None:
    assert_code(
        code,
        lambda: mark_to_market_equity(
            basis(), [replace(position(), **{field: value})], {"BTCUSDT": mark()}, NOW
        ),
    )


def test_missing_unknown_duplicate_and_time_fail_closed() -> None:
    assert_code("MARK_MISSING", lambda: mark_to_market_equity(basis(), [position()], {}, NOW))
    assert_code(
        "BASIS_UNKNOWN", lambda: mark_to_market_equity(replace(basis(), known=False), [], {}, NOW)
    )
    assert_code("BASIS_UNKNOWN", lambda: mark_to_market_equity(None, [], {}, NOW))
    assert_code(
        "POSITION_DUPLICATE",
        lambda: mark_to_market_equity(basis(), [position(), position()], {"BTCUSDT": mark()}, NOW),
    )
    assert_code("AS_OF_MISMATCH", lambda: mark_to_market_equity(basis(), [], {}, NOW + AGE))
    assert_code(
        "TIMESTAMP_INVALID",
        lambda: mark_to_market_equity(basis(), [], {}, NOW.replace(tzinfo=None)),
    )
    assert_code(
        "OBSERVATION_FUTURE",
        lambda: mark_to_market_equity(replace(basis(), captured_at=NOW + AGE), [], {}, NOW),
    )
    assert_code(
        "EQUITY_STALE",
        lambda: mark_to_market_equity(
            replace(basis(), captured_at=NOW - AGE - AGE, available_at=NOW - AGE - AGE), [], {}, NOW
        ),
    )


@pytest.mark.parametrize("value", [1.0, D("NaN"), D("Infinity"), D("-1")])
def test_invalid_capital(value: object) -> None:
    assert_code(
        "DECIMAL_REQUIRED" if isinstance(value, float) else "AMOUNT_INVALID",
        lambda: mark_to_market_equity(
            replace(basis(), initial_capital_before_realized_pnl=value), [], {}, NOW
        ),
    )


def test_conversion_rounds_down_and_is_independent_of_decimal_context() -> None:
    snap = mark_to_market_equity(replace(basis(), asset="BTC"), [], {}, NOW)
    with localcontext() as context:
        context.prec = 4
        assert normalize_equity_to_usdt(snap, conversion(), NOW, AGE) == D("3291.66666666")
        assert mark_to_market_equity(
            basis(), [position()], {"BTCUSDT": mark()}, NOW
        ).equity_amount == D("1007.5")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("rate", 1.0, "DECIMAL_REQUIRED"),
        ("rate", D("0"), "AMOUNT_INVALID"),
        ("rate", D("NaN"), "AMOUNT_INVALID"),
        ("rate", D("Infinity"), "AMOUNT_INVALID"),
        ("rate", D("-1"), "AMOUNT_INVALID"),
        ("known", False, "CONVERSION_UNKNOWN"),
        ("approved", False, "CONVERSION_REJECTED"),
        ("route", "MULTIHOP", "ROUTE_UNSUPPORTED"),
        ("base_asset", "ETH", "CURRENCY_MISMATCH"),
        ("quote_asset", "BTC", "CURRENCY_MISMATCH"),
        ("workspace_id", "other", "SCOPE_MISMATCH"),
        ("as_of", NOW - AGE - timedelta(microseconds=1), "CONVERSION_STALE"),
        ("as_of", NOW + AGE, "OBSERVATION_FUTURE"),
        ("available_at", NOW + AGE, "OBSERVATION_FUTURE"),
        ("available_at", NOW.replace(tzinfo=None), "TIMESTAMP_INVALID"),
    ],
)
def test_conversion_rejections(field: str, value: object, code: str) -> None:
    snap = mark_to_market_equity(replace(basis(), asset="BTC"), [], {}, NOW)
    assert_code(
        code,
        lambda: normalize_equity_to_usdt(snap, replace(conversion(), **{field: value}), NOW, AGE),
    )


def test_snapshot_conversion_boundaries_and_unknown() -> None:
    snap = mark_to_market_equity(replace(basis(), asset="BTC"), [], {}, NOW)
    assert_code("CONVERSION_MISSING", lambda: normalize_equity_to_usdt(snap, None, NOW, AGE))
    assert_code(
        "SNAPSHOT_UNKNOWN",
        lambda: normalize_equity_to_usdt(replace(snap, known=False), None, NOW, AGE),
    )
    assert_code("SNAPSHOT_UNKNOWN", lambda: normalize_equity_to_usdt(None, None, NOW, AGE))
    assert_code(
        "AS_OF_MISMATCH", lambda: normalize_equity_to_usdt(snap, conversion(), NOW + AGE, AGE)
    )
    assert_code("POLICY_INVALID", lambda: normalize_equity_to_usdt(snap, conversion(), NOW, -AGE))
    assert normalize_equity_to_usdt(snap, replace(conversion(), as_of=NOW - AGE), NOW, AGE) == D(
        "3291.66666666"
    )


@pytest.mark.parametrize("field", ["workspace_id", "bot_id", "account_id", "source_hash"])
def test_snapshot_tampering_is_rejected(field: str) -> None:
    snap = mark_to_market_equity(basis(), [], {}, NOW)
    assert_code(
        "LINEAGE_INVALID",
        lambda: normalize_equity_to_usdt(replace(snap, **{field: "tampered"}), None, NOW, AGE),
    )


def test_decimal_equivalent_hashes_and_conversion_lineage() -> None:
    first = mark_to_market_equity(basis(), [], {}, NOW)
    equivalent = replace(basis(), initial_capital_before_realized_pnl=D("1000.0000"))
    assert first.source_hash == mark_to_market_equity(equivalent, [], {}, NOW).source_hash
    assert (
        conversion().observation_hash
        == replace(conversion(), rate=D("3.3333333333330")).observation_hash
    )
    assert (
        conversion().observation_hash
        != replace(conversion(), policy_version_id="v2").observation_hash
    )


def test_negative_final_equity_and_signed_realized_pnl() -> None:
    assert_code(
        "AMOUNT_INVALID",
        lambda: mark_to_market_equity(
            replace(basis(), cumulative_realized_pnl=D("-1001")), [], {}, NOW
        ),
    )
    result = mark_to_market_equity(replace(basis(), cumulative_realized_pnl=D("12.5")), [], {}, NOW)
    assert result.equity_amount == D("1012.5")
    assert_code(
        "AMOUNT_INVALID",
        lambda: mark_to_market_equity(
            replace(basis(), cumulative_realized_pnl=D("NaN")), [], {}, NOW
        ),
    )


def test_mark_freshness_boundary_and_provenance() -> None:
    at_boundary = replace(mark(), as_of=NOW - AGE)
    assert mark_to_market_equity(
        basis(), [position()], {"BTCUSDT": at_boundary}, NOW
    ).equity_amount == D("1007.5")
    assert_code(
        "LINEAGE_INVALID",
        lambda: mark_to_market_equity(
            basis(), [position()], {"BTCUSDT": replace(mark(), source_hash="")}, NOW
        ),
    )


def test_fractional_quantity_and_contract_multiplier_are_exact() -> None:
    fractional = replace(
        position(), quantity=D("0.3"), entry_price=D("100.125"), contract_multiplier=D("10")
    )
    result = mark_to_market_equity(
        basis(), [fractional], {"BTCUSDT": replace(mark(), price=D("100.175"))}, NOW
    )
    assert result.unrealized_pnl == D("0.15000")
    assert result.equity_amount == D("987.65000")


def test_usdt_identity_does_not_quantize_equity() -> None:
    exact = D("1.123456789012345678901234567890123456789")
    snap = mark_to_market_equity(
        replace(basis(), initial_capital_before_realized_pnl=exact, cumulative_realized_pnl=D("0")),
        [],
        {},
        NOW,
    )
    assert snap.equity_amount == exact
    assert normalize_equity_to_usdt(snap, None, NOW, AGE) == exact
