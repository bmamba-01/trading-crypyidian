"""Kiểm thử service Strategy Card khi đọc phiên bản đang kích hoạt."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

import trading_bot.admin.strategy_card_service as strategy_card_service
from trading_bot.admin.strategy_card_service import (
    StrategyCardAdminService,
    build_default_demo_cards,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.strategy_card.schema import StrategyCard

WORKSPACE_ID = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")


@pytest.mark.asyncio
async def test_get_domain_card_reads_the_active_card_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = build_default_demo_cards()[0]

    class RepositoryStub:
        def __init__(self, _session: object) -> None:
            pass

        async def get_active_card(self, *, workspace_id: UUID, card_id: str) -> StrategyCard | None:
            assert workspace_id == WORKSPACE_ID
            assert card_id == card.card_id
            return card

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(strategy_card_service, "StrategyCardRepository", RepositoryStub)
    service = StrategyCardAdminService(session_factory=cast(Any, lambda: SessionContext()))
    principal = Principal(
        workspace_id=WORKSPACE_ID,
        membership_id=MEMBERSHIP_ID,
        user_id=USER_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )

    assert await service.get_domain_card(principal, card.card_id) is card
