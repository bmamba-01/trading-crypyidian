"""Kiểm API đọc/cập nhật subscription signal và tương thích payload cũ."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.application.telegram_signal_subscription import TelegramSignalSubscriptionState
from trading_bot.application.telegram_signals import TelegramSignalFilters
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

BOT_ID = uuid4()
ACTOR = AuthIdentity(
    principal=Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    ),
    email="trader@example.test",
)


class RecordingSubscriptionService:
    def __init__(self) -> None:
        self.state = TelegramSignalSubscriptionState(
            bot_id=BOT_ID,
            enabled=False,
            binding_linked=True,
            filters=TelegramSignalFilters(symbols=("BTCUSDT",)),
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_state(self, principal: Principal, *, bot_id: Any) -> Any:
        self.calls.append(("get_state", {"principal": principal, "bot_id": bot_id}))
        return self.state

    async def set_enabled(self, principal: Principal, **kwargs: Any) -> Any:
        self.calls.append(("set_enabled", {"principal": principal, **kwargs}))
        self.state = TelegramSignalSubscriptionState(
            bot_id=kwargs["bot_id"],
            enabled=kwargs["enabled"],
            binding_linked=True,
            filters=self.state.filters,
        )
        return self.state

    async def update_preference(self, principal: Principal, **kwargs: Any) -> Any:
        self.calls.append(("update_preference", {"principal": principal, **kwargs}))
        filters = kwargs.get("filters") or self.state.filters
        enabled = kwargs.get("enabled")
        self.state = TelegramSignalSubscriptionState(
            bot_id=kwargs["bot_id"],
            enabled=self.state.enabled if enabled is None else enabled,
            binding_linked=True,
            filters=filters,
        )
        return self.state


def _client(service: RecordingSubscriptionService) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    app.dependency_overrides[current_identity] = lambda: ACTOR
    app.dependency_overrides[require_csrf] = lambda: ACTOR
    app.state.telegram_signal_subscription_service = service
    return TestClient(app)


def test_get_returns_subscription_filters_and_no_store() -> None:
    service = RecordingSubscriptionService()

    response = _client(service).get(f"/api/v1/telegram-signals/{BOT_ID}/subscription")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "bot_id": str(BOT_ID),
        "enabled": False,
        "binding_linked": True,
        "filters": {
            "symbols": ["BTCUSDT"],
            "timeframes": [],
            "directions": [],
            "strategy_ids": [],
            "setup_codes": [],
        },
    }
    assert service.calls[0][0] == "get_state"
    assert service.calls[0][1]["principal"] is ACTOR.principal


def test_legacy_enabled_only_payload_preserves_existing_filter_allowlists() -> None:
    service = RecordingSubscriptionService()

    response = _client(service).put(
        f"/api/v1/telegram-signals/{BOT_ID}/subscription", json={"enabled": True}
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["filters"]["symbols"] == ["BTCUSDT"]
    assert service.calls[0][0] == "set_enabled"
    assert service.calls[0][1]["enabled"] is True


def test_filter_only_payload_updates_allowlists_without_changing_enabled_state() -> None:
    service = RecordingSubscriptionService()

    response = _client(service).put(
        f"/api/v1/telegram-signals/{BOT_ID}/subscription",
        json={"filters": {"symbols": ["ethusdt"], "directions": ["short"]}},
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["filters"]["symbols"] == ["ETHUSDT"]
    assert response.json()["filters"]["directions"] == ["SHORT"]
    assert service.calls[0][0] == "update_preference"
    assert service.calls[0][1]["enabled"] is None
    assert service.calls[0][1]["filters"].timeframes == ()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"enabled": None},
        {"filters": None},
        {"unexpected": "value"},
        {"filters": {"symbols": None}},
        {"filters": {"symbols": ["bad-symbol"]}},
        {"filters": {"directions": ["BOTH"]}},
        {"filters": {"symbols": [f"S{i:03d}" for i in range(101)]}},
    ],
)
def test_invalid_or_unbounded_preference_payload_is_rejected(payload: dict[str, object]) -> None:
    service = RecordingSubscriptionService()

    response = _client(service).put(f"/api/v1/telegram-signals/{BOT_ID}/subscription", json=payload)

    assert response.status_code == 422
    assert service.calls == []
