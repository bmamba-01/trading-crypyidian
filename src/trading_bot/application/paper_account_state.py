"""Read-model vốn PAPER dựa trên seed bất biến và PnL đã chốt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from trading_bot.adapters.database.models.entities import PaperAccountSeed, TradingAccount
from trading_bot.adapters.database.models.paper_entities import TradeRecord


class PaperAccountStateError(RuntimeError):
    """Từ chối snapshot khi ledger thiếu, không nhất quán hoặc chưa rõ trạng thái."""


class PaperAccountStateLoader(Protocol):
    async def load(
        self, *, workspace_id: UUID, account_id: UUID
    ) -> tuple[TradingAccount | None, PaperAccountSeed | None, list[TradeRecord]]: ...


@dataclass(frozen=True, slots=True)
class PaperAccountState:
    """Vốn quote đã thực hiện; không gồm lãi/lỗ chưa chốt của vị thế mở."""

    workspace_id: UUID
    account_id: UUID
    quote_asset: str
    equity: Decimal
    as_of: datetime
    seed_id: UUID
    seed_settings_version_id: UUID
    seed_settings_hash: str
    closed_trade_count: int
    closed_trade_ids: tuple[UUID, ...]
    state_hash: str


class PaperAccountStateService:
    """Tính realized equity; tuyệt đối không gán vốn mặc định nếu dữ liệu thiếu."""

    def __init__(self, repository: PaperAccountStateLoader) -> None:
        self._repository = repository

    async def get_state(self, *, workspace_id: UUID, account_id: UUID) -> PaperAccountState:
        """Tính equity = seed.initial_equity + tổng net_pnl trade đã đóng."""
        account, seed, trades = await self._repository.load(
            workspace_id=workspace_id, account_id=account_id
        )
        if account is None or account.workspace_id != workspace_id or account.id != account_id:
            raise PaperAccountStateError("Trading account không tồn tại trong workspace.")
        if account.mode != "PAPER" or account.venue != "binance_usdm":
            raise PaperAccountStateError("Chỉ hỗ trợ account PAPER Binance USDⓈ-M.")
        if account.status != "ENABLED":
            raise PaperAccountStateError("Account PAPER không ở trạng thái ENABLED.")
        if (
            seed is None
            or seed.workspace_id != workspace_id
            or seed.trading_account_id != account_id
        ):
            raise PaperAccountStateError("Không có seed equity PAPER hợp lệ cho account.")
        if not _aware(seed.created_at):
            raise PaperAccountStateError("Thời điểm tạo seed không hợp lệ.")
        if not seed.initial_equity.is_finite() or seed.initial_equity <= 0:
            raise PaperAccountStateError("Seed equity phải hữu hạn và dương.")
        if not _valid_quote(seed.quote_asset):
            raise PaperAccountStateError("Seed quote asset không hợp lệ.")
        if len(seed.source_settings_hash) != 64 or any(
            char not in "0123456789abcdef" for char in seed.source_settings_hash
        ):
            raise PaperAccountStateError("Hash settings nguồn của seed không hợp lệ.")

        closed: list[TradeRecord] = []
        for trade in trades:
            if trade.workspace_id != workspace_id or trade.account_id != account_id:
                raise PaperAccountStateError("Ledger trade vượt workspace/account hiện tại.")
            if trade.mode != "PAPER" or trade.status not in {"OPEN", "CLOSED"}:
                raise PaperAccountStateError("Ledger có mode hoặc trạng thái trade chưa hỗ trợ.")
            if trade.quote_asset != seed.quote_asset:
                raise PaperAccountStateError("Quote asset của trade không khớp seed account.")
            if trade.status == "OPEN":
                if trade.closed_at is not None:
                    raise PaperAccountStateError("Trade OPEN có closed_at không hợp lệ.")
                continue
            if not _aware(trade.closed_at):
                raise PaperAccountStateError("Trade CLOSED thiếu thời điểm đóng hợp lệ.")
            assert trade.closed_at is not None
            if not trade.net_pnl.is_finite():
                raise PaperAccountStateError("Net PnL trade phải hữu hạn.")
            closed.append(trade)

        closed.sort(key=lambda trade: (trade.closed_at, str(trade.id)))
        equity = seed.initial_equity + sum((trade.net_pnl for trade in closed), Decimal(0))
        if not equity.is_finite():
            raise PaperAccountStateError("Equity tính được không hữu hạn.")
        as_of = closed[-1].closed_at if closed else seed.created_at
        assert as_of is not None
        trade_ids = tuple(trade.id for trade in closed)
        closed_trade_lineage: list[list[str]] = []
        for trade in closed:
            closed_at = trade.closed_at
            if closed_at is None:
                raise PaperAccountStateError("Trade CLOSED thiếu thời điểm đóng hợp lệ.")
            closed_trade_lineage.append([str(trade.id), closed_at.isoformat(), str(trade.net_pnl)])
        material = {
            "workspace_id": str(workspace_id),
            "account_id": str(account_id),
            "seed_id": str(seed.id),
            "seed_settings_version_id": str(seed.source_settings_version_id),
            "seed_settings_hash": seed.source_settings_hash,
            "quote_asset": seed.quote_asset,
            "initial_equity": str(seed.initial_equity),
            "closed_trades": closed_trade_lineage,
            "equity": str(equity),
            "as_of": as_of.isoformat(),
        }
        state_hash = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return PaperAccountState(
            workspace_id=workspace_id,
            account_id=account_id,
            quote_asset=seed.quote_asset,
            equity=equity,
            as_of=as_of,
            seed_id=seed.id,
            seed_settings_version_id=seed.source_settings_version_id,
            seed_settings_hash=seed.source_settings_hash,
            closed_trade_count=len(closed),
            closed_trade_ids=trade_ids,
            state_hash=state_hash,
        )


def _aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def _valid_quote(value: str) -> bool:
    return bool(value) and value.isalnum() and value == value.upper() and len(value) <= 16
