from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_bot.adapters.database.repositories.workspace_settings import (
    canonical_settings,
    settings_hash,
)
from trading_bot.config.workspace_settings import WorkspaceSettings


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime": {"mode": "PAPER", "timezone": "UTC"},
        "market_data": {
            "venue": "BINANCE_USDM",
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "timeframes": ["5m", "1h", "4h"],
            "max_age_seconds": 30,
        },
        "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
        "strategy": {
            "default_release": "h01-v1",
            "parameters": {},
            "entry_timeframe": "1h",
            "confirmation_timeframe": "4h",
        },
        "risk": {"profile_version_id": str(uuid4())},
        "paper_execution": {
            "quote_asset": "USDT",
            "starting_equity": "10000",
            "fee_schedule_version": "paper-fees-v1",
            "fill_model": "next_bar_with_adverse_costs",
        },
    }


def test_workspace_settings_accepts_valid_paper_config() -> None:
    settings = WorkspaceSettings.model_validate(valid_payload())

    assert settings.runtime.mode == "PAPER"
    assert settings.paper_execution.starting_equity == Decimal("10000")
    assert settings.risk.profile_version_id


@pytest.mark.parametrize("mode", ["DEMO", "LIVE", "paper"])
def test_workspace_settings_rejects_non_paper_mode(mode: str) -> None:
    payload = valid_payload()
    payload["runtime"] = {"mode": mode, "timezone": "UTC"}

    with pytest.raises(ValidationError):
        WorkspaceSettings.model_validate(payload)


def test_workspace_settings_rejects_unknown_fields_including_secret_like_values() -> None:
    payload = valid_payload()
    payload["api_secret"] = "must-not-be-accepted"

    with pytest.raises(ValidationError):
        WorkspaceSettings.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("market_data", "max_age_seconds", -1),
        ("market_data", "timeframes", ["7m"]),
        ("paper_execution", "starting_equity", "0"),
        ("risk", "profile_version_id", "not-a-uuid"),
    ],
)
def test_workspace_settings_rejects_invalid_values(section: str, field: str, value: object) -> None:
    payload = valid_payload()
    payload_section = payload[section]
    assert isinstance(payload_section, dict)
    payload_section[field] = value

    with pytest.raises(ValidationError):
        WorkspaceSettings.model_validate(payload)


def test_workspace_settings_rejects_duplicate_symbols_and_timeframes() -> None:
    payload = valid_payload()
    market_data = payload["market_data"]
    assert isinstance(market_data, dict)
    market_data["symbols"] = ["BTCUSDT", "BTCUSDT"]

    with pytest.raises(ValidationError):
        WorkspaceSettings.model_validate(payload)


def test_workspace_settings_rejects_nested_secret_like_strategy_parameters() -> None:
    payload = valid_payload()
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    strategy["parameters"] = {"vendor": {"api_key": "must-not-be-stored"}}

    with pytest.raises(ValidationError):
        WorkspaceSettings.model_validate(payload)


def test_strategy_parameters_are_deeply_immutable_and_detached_from_input() -> None:
    payload = valid_payload()
    strategy = payload["strategy"]
    assert isinstance(strategy, dict)
    parameters = {"thresholds": {"adx": 20}, "labels": ["trend", "paper"]}
    strategy["parameters"] = parameters

    settings = WorkspaceSettings.model_validate(payload)
    parameters["thresholds"]["adx"] = 99
    parameters["labels"].append("mutated")

    assert settings.strategy.parameters["thresholds"]["adx"] == 20
    assert settings.strategy.parameters["labels"] == ("trend", "paper")
    with pytest.raises(TypeError):
        settings.strategy.parameters["thresholds"]["adx"] = 30
    with pytest.raises(AttributeError):
        settings.strategy.parameters["labels"].append("mutated")

    dumped = settings.model_dump(mode="json")
    assert dumped["strategy"]["parameters"] == {
        "thresholds": {"adx": 20},
        "labels": ["trend", "paper"],
    }
    dumped["strategy"]["parameters"]["thresholds"]["adx"] = 40
    assert settings.strategy.parameters["thresholds"]["adx"] == 20


def test_workspace_settings_hash_is_stable_for_mapping_key_order() -> None:
    settings = WorkspaceSettings.model_validate(valid_payload())
    payload = canonical_settings(settings)
    reordered = dict(reversed(tuple(payload.items())))

    assert settings_hash(payload) == settings_hash(reordered)
