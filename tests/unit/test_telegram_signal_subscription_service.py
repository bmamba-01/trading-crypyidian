"""Kiểm thay đổi preference giữ nguyên phần state không được gửi trong request."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from trading_bot.application.telegram_signal_subscription import (
    TelegramSignalSubscriptionError,
    TelegramSignalSubscriptionService,
)
from trading_bot.application.telegram_signals import TelegramSignalFilters
from trading_bot.auth.policy import Principal, Role

WORKSPACE = uuid4()
MEMBERSHIP = uuid4()
BOT = uuid4()


class FakeUnitOfWork:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.session = object()

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def record_audit(self, **_kwargs: object) -> None:
        return None


class FakeSubscriptionRepository:
    def __init__(self, *, binding_available: bool = True) -> None:
        self.binding = (
            SimpleNamespace(id=uuid4(), telegram_user_id="987654321") if binding_available else None
        )
        self.row = SimpleNamespace(
            id=uuid4(),
            enabled=True,
            symbols=["BTCUSDT"],
            timeframes=["1h"],
            directions=["LONG"],
            strategy_ids=["H01"],
            setup_codes=["H01_BREAKOUT"],
        )

    async def active_binding(self, **_kwargs: object) -> Any:
        return self.binding

    async def get_subscription(self, **_kwargs: object) -> Any:
        return self.row

    async def set_bot_subscription_enabled(self, *, enabled: bool, **_kwargs: object) -> bool:
        self.row.enabled = enabled
        return True

    async def put_subscription(self, *, enabled: bool, filters: Any, **_kwargs: object) -> Any:
        self.row.enabled = enabled
        if filters is not None:
            for name, values in filters.as_dict().items():
                setattr(self.row, name, values)
        return self.row


def _principal() -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=MEMBERSHIP,
        workspace_id=WORKSPACE,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )


def _service(repository: FakeSubscriptionRepository) -> TelegramSignalSubscriptionService:
    service = TelegramSignalSubscriptionService(
        object(),  # type: ignore[arg-type]
        unit_of_work=FakeUnitOfWork,
        repository=lambda _session: repository,
    )

    async def allow_bot(_session: object, _principal: Principal, _bot_id: object) -> None:
        return None

    service._require_bot = allow_bot  # type: ignore[method-assign]
    return service


@pytest.mark.asyncio
async def test_filter_replacement_preserves_enabled_state_and_empty_lists() -> None:
    repository = FakeSubscriptionRepository()
    service = _service(repository)
    principal = _principal()

    disabled = await service.set_enabled(principal, bot_id=BOT, enabled=False)
    filtered = await service.set_filters(
        principal,
        bot_id=BOT,
        filters=TelegramSignalFilters(symbols=("ETHUSDT",), directions=("SHORT",)),
    )

    assert disabled.enabled is False
    assert disabled.filters.symbols == ("BTCUSDT",)
    assert filtered.enabled is False
    assert filtered.filters.symbols == ("ETHUSDT",)
    assert filtered.filters.directions == ("SHORT",)
    assert filtered.filters.timeframes == ()


@pytest.mark.asyncio
async def test_filter_update_requires_a_current_private_binding() -> None:
    service = _service(FakeSubscriptionRepository(binding_available=False))

    with pytest.raises(TelegramSignalSubscriptionError, match="liên kết Telegram riêng"):
        await service.set_filters(
            _principal(), bot_id=BOT, filters=TelegramSignalFilters(symbols=("BTCUSDT",))
        )
