"""Lược đồ overlay bot: allowlist, chuẩn hóa Decimal, hash và chặn trường bí mật."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_bot.config.bot_settings import (
    BotConfigOverlay,
    canonical_overlay,
    overlay_hash,
    risk_section,
)


def test_empty_overlay_inherits_everything_and_hashes_stably() -> None:
    overlay = BotConfigOverlay.model_validate({"schema_version": 1})

    assert overlay.strategy is None
    assert overlay.universe is None
    assert overlay.risk is None
    assert canonical_overlay(overlay) == {"schema_version": 1}
    assert overlay_hash(canonical_overlay(overlay)) == overlay_hash({"schema_version": 1})
    assert len(overlay_hash(canonical_overlay(overlay))) == 64


def test_overlay_accepts_allowlisted_sections_and_freezes_parameters() -> None:
    overlay = BotConfigOverlay.model_validate(
        {
            "schema_version": 1,
            "strategy": {"release_id": "h03-v1", "parameters": {"donchian_lookback": 60}},
            "universe": {"symbols": ["BTCUSDT"], "timeframes": ["1h", "4h"]},
            "risk": {"per_trade_risk_fraction": "0.005", "max_positions": 1},
        }
    )

    assert overlay.strategy is not None
    assert overlay.strategy.parameters["donchian_lookback"] == 60
    with pytest.raises(TypeError):
        overlay.strategy.parameters["donchian_lookback"] = 1  # type: ignore[index]
    assert overlay.risk is not None
    assert overlay.risk.per_trade_risk_fraction == Decimal("0.005")


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "venue": "BINANCE_USDM"},
        {"schema_version": 1, "runtime": {"mode": "LIVE"}},
        {"schema_version": 1, "workspace_id": "00000000-0000-0000-0000-000000000001"},
        {"schema_version": 1, "risk": {"profile_version_id": "x"}},
        {"schema_version": 1, "universe": {"venue": "BINANCE_USDM"}},
        {"schema_version": 1, "strategy": {"release_id": "h03-v1", "mode": "LIVE"}},
        {"schema_version": 2},
    ],
)
def test_fields_outside_allowlist_are_rejected_not_ignored(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BotConfigOverlay.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "api_key": "DO_NOT_ECHO"},
        {"schema_version": 1, "risk": {"client_secret": "DO_NOT_ECHO"}},
        {
            "schema_version": 1,
            "strategy": {"release_id": "h03-v1", "parameters": {"nested": {"Token": "x"}}},
        },
        {
            "schema_version": 1,
            "strategy": {"release_id": "h03-v1", "parameters": {"list": [{"password": "x"}]}},
        },
    ],
)
def test_secret_like_fields_are_rejected_without_echoing_value(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as error:
        BotConfigOverlay.model_validate(payload)

    messages = " ".join(str(item["msg"]) for item in error.value.errors())
    assert "bí mật" in messages
    assert "DO_NOT_ECHO" not in messages


@pytest.mark.parametrize(
    "value",
    [0.01, True, "NaN", "Infinity", "0", "-0.01", "1.5", "abc"],
)
def test_risk_fraction_requires_exact_finite_value_in_unit_interval(value: object) -> None:
    with pytest.raises(ValidationError):
        BotConfigOverlay.model_validate(
            {"schema_version": 1, "risk": {"per_trade_risk_fraction": value}}
        )


def test_risk_amount_rejects_float_and_non_positive_values() -> None:
    for value in (100.0, "0", "-1"):
        with pytest.raises(ValidationError):
            BotConfigOverlay.model_validate({"schema_version": 1, "risk": {"max_notional": value}})
    with pytest.raises(ValidationError):
        BotConfigOverlay.model_validate({"schema_version": 1, "risk": {"max_positions": 0}})


def test_equal_decimal_values_produce_same_canonical_hash() -> None:
    first = BotConfigOverlay.model_validate(
        {"schema_version": 1, "risk": {"per_trade_risk_fraction": "0.0100", "max_notional": "1E+3"}}
    )
    second = BotConfigOverlay.model_validate(
        {"schema_version": 1, "risk": {"per_trade_risk_fraction": "0.01", "max_notional": 1000}}
    )

    assert canonical_overlay(first) == canonical_overlay(second)
    assert canonical_overlay(first)["risk"] == {
        "max_notional": "1000",
        "per_trade_risk_fraction": "0.01",
    }
    assert overlay_hash(canonical_overlay(first)) == overlay_hash(canonical_overlay(second))


def test_canonical_payload_round_trips_to_same_overlay() -> None:
    overlay = BotConfigOverlay.model_validate(
        {
            "schema_version": 1,
            "strategy": {"release_id": "h03-v1", "parameters": {"donchian_lookback": 60}},
            "universe": {"symbols": ["BTCUSDT"]},
            "risk": {"max_leverage": "3"},
        }
    )

    restored = BotConfigOverlay.model_validate(canonical_overlay(overlay))

    assert restored == overlay
    assert overlay_hash(canonical_overlay(restored)) == overlay_hash(canonical_overlay(overlay))


@pytest.mark.parametrize(
    "universe",
    [
        {"symbols": ["btcusdt"]},
        {"symbols": ["BTCUSDT", "BTCUSDT"]},
        {"symbols": []},
        {"timeframes": ["1h", "1h"]},
        {"timeframes": ["2m"]},
    ],
)
def test_universe_requires_valid_unique_values(universe: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BotConfigOverlay.model_validate({"schema_version": 1, "universe": universe})


def test_risk_section_is_comparable_for_mfa_gate() -> None:
    without_risk = BotConfigOverlay.model_validate({"schema_version": 1})
    tighter = BotConfigOverlay.model_validate(
        {"schema_version": 1, "risk": {"per_trade_risk_fraction": "0.005"}}
    )
    same_value = BotConfigOverlay.model_validate(
        {"schema_version": 1, "risk": {"per_trade_risk_fraction": "0.0050"}}
    )

    assert risk_section(without_risk) == {}
    assert risk_section(tighter) == {"per_trade_risk_fraction": "0.005"}
    assert risk_section(tighter) == risk_section(same_value)
