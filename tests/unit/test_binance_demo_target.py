"""Kiểm tra cấu hình execution chỉ chấp nhận cặp địa chỉ Demo đã duyệt."""

from dataclasses import FrozenInstanceError, replace

import pytest

from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget, DemoEnvironment


def test_default_target_is_demo_with_explicit_account_and_reference() -> None:
    target = BinanceDemoTarget(account_id="demo-account", credential_ref="vault:demo/key")

    assert target.environment is DemoEnvironment.DEMO
    assert target.rest_base_url == "https://demo-fapi.binance.com"
    assert target.private_ws_base_url == "wss://demo-fstream.binance.com"
    assert target.account_id == "demo-account"
    assert target.credential_ref == "vault:demo/key"


@pytest.mark.parametrize(
    "host",
    [
        "https://fapi.binance.com",
        "http://demo-fapi.binance.com",
        "wss://demo-fapi.binance.com",
        "https://demo-fapi.binance.com.evil.example",
        "https://demo-fapi.binance.com@evil.example",
        "https://key:secret@demo-fapi.binance.com",
        "https://demo-fapi.binance.com/",
        "https://demo-fapi.binance.com/fapi/v1/order",
        "https://demo-fapi.binance.com?signature=private-value",
        "https://demo-fapi.binance.com#fragment",
        "https://demo-fapi.binance.com:443",
        " https://demo-fapi.binance.com",
        "https://demo-fapi.binance.com\n",
        "https://DEMO-FAPI.BINANCE.COM",
        "",
        None,
    ],
)
def test_rejects_every_noncanonical_rest_target_without_echoing_input(host: str) -> None:
    with pytest.raises(ValueError, match="rest_base_url") as error:
        BinanceDemoTarget(
            account_id="demo-account", credential_ref="vault:demo/key", rest_base_url=host
        )

    assert "private-value" not in str(error.value)
    assert "secret@" not in str(error.value)


@pytest.mark.parametrize(
    "host",
    [
        "wss://fstream.binance.com",
        "ws://demo-fstream.binance.com",
        "https://demo-fstream.binance.com",
        "wss://demo-fstream.binance.com.evil.example",
        "wss://demo-fstream.binance.com@evil.example",
        "wss://key:secret@demo-fstream.binance.com",
        "wss://demo-fstream.binance.com/",
        "wss://demo-fstream.binance.com/private/ws/private-value",
        "wss://demo-fstream.binance.com?listenKey=private-value",
        "wss://demo-fstream.binance.com#fragment",
        "wss://demo-fstream.binance.com:443",
        " wss://demo-fstream.binance.com",
        "wss://demo-fstream.binance.com\n",
        "wss://DEMO-FSTREAM.BINANCE.COM",
        "",
        None,
    ],
)
def test_rejects_every_noncanonical_private_stream_without_echoing_input(host: str) -> None:
    with pytest.raises(ValueError, match="private_ws_base_url") as error:
        BinanceDemoTarget(
            account_id="demo-account", credential_ref="vault:demo/key", private_ws_base_url=host
        )

    assert "private-value" not in str(error.value)
    assert "secret@" not in str(error.value)


@pytest.mark.parametrize("environment", ["DEMO", "LIVE", "PAPER", "production", "", None])
def test_rejects_environment_strings_including_implicit_demo_coercion(environment: str) -> None:
    with pytest.raises(ValueError, match="environment"):
        BinanceDemoTarget(
            account_id="demo-account",
            credential_ref="vault:demo/key",
            environment=environment,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("field", ["account_id", "credential_ref"])
@pytest.mark.parametrize("value", ["", " ", "\t\n", None, 123])
def test_rejects_absent_or_nonstring_identity(field: str, value: object) -> None:
    values = {"account_id": "demo-account", "credential_ref": "vault:demo/key"}
    values[field] = value  # type: ignore[assignment]
    with pytest.raises(ValueError, match=field):
        BinanceDemoTarget(**values)


def test_target_cannot_be_mutated_or_replaced_with_production_host() -> None:
    target = BinanceDemoTarget(account_id="demo-account", credential_ref="vault:demo/key")

    with pytest.raises(FrozenInstanceError):
        target.rest_base_url = "https://fapi.binance.com"  # type: ignore[misc]
    with pytest.raises(ValueError, match="rest_base_url"):
        replace(target, rest_base_url="https://fapi.binance.com")


def test_account_and_credential_reference_are_hidden_from_representation() -> None:
    target = BinanceDemoTarget(account_id="private-account", credential_ref="vault:private-ref")

    assert "private-account" not in repr(target)
    assert "vault:private-ref" not in repr(target)


@pytest.mark.parametrize("field", ["api_key", "api_secret", "secret", "listen_key"])
def test_constructor_has_no_inline_secret_parameters(field: str) -> None:
    with pytest.raises(TypeError):
        BinanceDemoTarget(
            account_id="demo-account", credential_ref="vault:demo/key", **{field: "private-value"}
        )
