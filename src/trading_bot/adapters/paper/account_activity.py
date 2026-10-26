"""Adapter `AccountActivityPort` cho PAPER (MB05.1, ruling 12).

- `PaperLedgerAccountActivityAdapter`: production PAPER, suy ra snapshot từ gateway đã hydrate
  của chính bot (ledger/checkpoint) nên luôn khớp state đã biết.
- `ScriptedAccountActivityAdapter`: thuần bộ nhớ cho unit test và probe E2E; wiring `app.py`
  không import/không chọn được (test tĩnh khóa).
- `venue_account_activity_adapter()`: bị khóa tới WP-03, luôn raise
  `VENUE_ACTIVITY_ADAPTER_DISABLED`; không có biến môi trường bật đường giả lập.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

from trading_bot.ports.account_activity import (
    AccountActivityError,
    AccountActivitySnapshot,
    KnownAccountState,
    ObservedOrder,
    ObservedPosition,
)


def _side(direction: str) -> str:
    return str(direction).upper()


class PaperLedgerAccountActivityAdapter:
    """Snapshot activity từ gateway PAPER đã hydrate theo account của bot."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        gateways: Mapping[UUID, Any],
        clock: Callable[[], datetime],
    ) -> None:
        self._workspace_id = workspace_id
        self._gateways = gateways
        self._clock = clock

    def _gateway(self, account_id: UUID) -> Any:
        gateway = self._gateways.get(account_id)
        if gateway is None:
            raise AccountActivityError("ACTIVITY_ADAPTER_UNAVAILABLE")
        return gateway

    def _orders(self, account_id: UUID) -> tuple[ObservedOrder, ...]:
        return tuple(
            ObservedOrder(
                client_order_id=str(order.intent_id),
                symbol=str(order.symbol),
                side=_side(order.direction),
                # Khối lượng yêu cầu ổn định (remaining có thể bằng 0 khi order UNKNOWN đã khớp).
                quantity=order.requested_quantity,
                status=str(getattr(order.status, "value", order.status)),
            )
            for order in self._gateway(account_id).open_orders()
        )

    def _positions(self, account_id: UUID) -> tuple[ObservedPosition, ...]:
        return tuple(
            ObservedPosition(
                symbol=str(position.symbol),
                side=_side(position.direction),
                quantity=position.quantity,
            )
            for position in self._gateway(account_id).open_positions()
        )

    async def inspect(self, account_id: UUID, cursor: str | None) -> AccountActivitySnapshot:
        return AccountActivitySnapshot(
            workspace_id=self._workspace_id,
            account_id=account_id,
            source="PAPER_LEDGER",
            cursor=cursor,
            observed_at=self._clock(),
            complete=True,
            orders=self._orders(account_id),
            positions=self._positions(account_id),
        )

    def known_state(self, account_id: UUID) -> KnownAccountState:
        """State có lineage của bot, cùng nguồn gateway nên PAPER luôn CLEAN."""
        return KnownAccountState(
            workspace_id=self._workspace_id,
            account_id=account_id,
            known_client_order_ids=frozenset(
                order.client_order_id for order in self._orders(account_id)
            ),
            open_positions=self._positions(account_id),
        )


class ScriptedAccountActivityAdapter:
    """Adapter kịch bản trong bộ nhớ; chỉ dùng cho test/probe, không bao giờ wiring production."""

    def __init__(self, snapshots: Mapping[UUID, AccountActivitySnapshot] | None = None) -> None:
        self._snapshots: dict[UUID, AccountActivitySnapshot] = dict(snapshots or {})
        self._failures: dict[UUID, str] = {}

    def script(self, snapshot: AccountActivitySnapshot) -> None:
        self._failures.pop(snapshot.account_id, None)
        self._snapshots[snapshot.account_id] = snapshot

    def fail_with(self, account_id: UUID, reason_code: str) -> None:
        self._failures[account_id] = reason_code

    async def inspect(self, account_id: UUID, cursor: str | None) -> AccountActivitySnapshot:
        del cursor
        if account_id in self._failures:
            raise AccountActivityError(self._failures[account_id])
        snapshot = self._snapshots.get(account_id)
        if snapshot is None:
            raise AccountActivityError("ACTIVITY_ADAPTER_UNAVAILABLE")
        return snapshot


def venue_account_activity_adapter() -> NoReturn:
    """Activity venue thật thuộc WP-03; trước đó luôn từ chối (readiness venue `disabled`)."""
    raise AccountActivityError("VENUE_ACTIVITY_ADAPTER_DISABLED")
