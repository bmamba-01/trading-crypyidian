"""MB05.6 ruling 29: không disable bot/account khi còn vị thế, lệnh chưa khớp hoặc reservation."""

from __future__ import annotations

import asyncio
import inspect
from uuid import uuid4

import pytest
from tests.unit.runtime_fakes import RecordingSession

from trading_bot.application.bot_lifecycle_guard import BotLifecycleGuard, BotNotFlatError

WORKSPACE, BOT = uuid4(), uuid4()


def _guard(bot: object | None, positions: int, orders: int, reservations: int):
    session = RecordingSession(scalar_results=[bot, positions, orders, reservations])
    return BotLifecycleGuard(session), session  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("positions", "orders", "reservations"),
    [(1, 0, 0), (0, 1, 0), (0, 0, 1), (2, 1, 1)],
    ids=["open-position", "open-order", "held-reservation", "all"],
)
def test_not_flat_bot_raises_with_counts(positions: int, orders: int, reservations: int) -> None:
    guard, session = _guard(object(), positions, orders, reservations)

    with pytest.raises(BotNotFlatError) as error:
        asyncio.run(guard.ensure_flat_for_disable(WORKSPACE, BOT))

    assert error.value.reason_code == "BOT_NOT_FLAT"
    assert (error.value.open_positions, error.value.open_orders) == (positions, orders)
    assert error.value.held_reservations == reservations
    assert str(BOT) not in str(error.value)


def test_flat_bot_passes_and_queries_only_that_bot_scope() -> None:
    guard, session = _guard(object(), 0, 0, 0)

    assert asyncio.run(guard.ensure_flat_for_disable(WORKSPACE, BOT)) is None

    # [0] L1 advisory lock (M7), [1] tra bot, sau đó ba câu đếm theo bot.
    statements = session.statements[2:]
    assert all("bot_id" in statement for statement in statements)
    assert "trade" in statements[0]
    assert "order_intent" in statements[1]
    assert "risk_reservation" in statements[2]
    assert all("status" in statement for statement in statements)


def test_guard_status_sets_cover_unknown_states() -> None:
    from trading_bot.application import bot_lifecycle_guard as module

    assert module.OPEN_ORDER_STATUSES == frozenset({"ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN"})
    assert module.HELD_RESERVATION_STATUSES == frozenset({"RESERVED", "UNKNOWN"})


def test_invisible_bot_is_not_found() -> None:
    guard, _ = _guard(None, 0, 0, 0)

    with pytest.raises(BotNotFlatError) as error:
        asyncio.run(guard.ensure_flat_for_disable(WORKSPACE, BOT))
    assert error.value.reason_code == "BOT_NOT_FOUND"


def test_guard_has_no_exit_only_mode() -> None:
    parameters = inspect.signature(BotLifecycleGuard.ensure_flat_for_disable).parameters
    assert set(parameters) == {"self", "workspace_id", "bot_id"}
