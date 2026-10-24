"""Persistence des synthetic PAPER account và số dư seed bất biến."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.bot_scope import bind_account_bot
from trading_bot.adapters.database.models.entities import (
    PaperAccountSeed,
    TradingAccount,
)


class PaperAccountSeedConflict(RuntimeError):
    """Tài khoản PAPER hiện có khác cấu hình hoặc seed đã ghi."""


@dataclass(frozen=True, slots=True)
class PaperAccountSeedResult:
    """Account/seed được tạo mới hoặc đọc lại sau một lần gọi lặp."""

    account: TradingAccount
    seed: PaperAccountSeed
    created: bool


class PaperAccountSeedRepository:
    """Tạo account và seed trong transaction do application caller quản lý."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_seed(
        self,
        *,
        workspace_id: UUID,
        account_name: str,
        quote_asset: str,
        initial_equity: Decimal,
        max_notional: Decimal,
        source_settings_version_id: UUID,
        source_settings_hash: str,
    ) -> PaperAccountSeedResult:
        """Khởi tạo tối đa một seed cho account; retry không ghi đè số dư."""
        if not account_name.strip():
            raise ValueError("account_name không được để trống.")
        if not quote_asset.isalnum() or quote_asset != quote_asset.upper():
            raise ValueError("quote_asset phải là mã viết hoa hợp lệ.")
        if not initial_equity.is_finite() or initial_equity <= 0:
            raise ValueError("initial_equity phải hữu hạn và dương.")
        if not max_notional.is_finite() or max_notional <= 0:
            raise ValueError("max_notional phải hữu hạn và dương.")
        if len(source_settings_hash) != 64 or any(
            character not in "0123456789abcdef" for character in source_settings_hash
        ):
            raise ValueError("source_settings_hash phải là SHA-256 hex chữ thường.")

        # Advisory lock hỗ trợ lần đầu khi chưa có account row để khóa; runtime role
        # không cần quyền UPDATE trên bảng workspace.
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:account_seed_lock, 0))"),
            {"account_seed_lock": f"paper-account-seed:{workspace_id}:{account_name}"},
        )
        account = await self._session.scalar(
            select(TradingAccount)
            .where(
                TradingAccount.workspace_id == workspace_id,
                TradingAccount.name == account_name,
            )
            .with_for_update()
        )
        if account is None:
            account = TradingAccount(
                id=uuid4(),
                workspace_id=workspace_id,
                name=account_name,
                venue="binance_usdm",
                mode="PAPER",
                environment="local",
                status="ENABLED",
                max_notional=max_notional,
            )
            self._session.add(account)
            await self._session.flush()
        elif (
            account.workspace_id != workspace_id
            or account.mode != "PAPER"
            or account.venue != "binance_usdm"
            or account.environment != "local"
            or account.status != "ENABLED"
        ):
            raise PaperAccountSeedConflict("Account cùng tên không phải PAPER local đang bật.")

        bot_id = await bind_account_bot(
            self._session, workspace_id=workspace_id, account_id=account.id
        )
        idempotency_key = f"paper-seed:{account.id}"
        existing = await self._session.scalar(
            select(PaperAccountSeed).where(
                PaperAccountSeed.workspace_id == workspace_id,
                PaperAccountSeed.trading_account_id == account.id,
            )
        )
        if existing is not None:
            if (
                existing.quote_asset != quote_asset
                or existing.initial_equity != initial_equity
                or existing.idempotency_key != idempotency_key
            ):
                raise PaperAccountSeedConflict(
                    "Root settings đổi quote asset hoặc vốn khởi tạo; không được reset seed."
                )
            return PaperAccountSeedResult(account, existing, False)

        seed = PaperAccountSeed(
            bot_id=bot_id,
            id=uuid4(),
            workspace_id=workspace_id,
            trading_account_id=account.id,
            quote_asset=quote_asset,
            initial_equity=initial_equity,
            source_settings_version_id=source_settings_version_id,
            source_settings_hash=source_settings_hash,
            idempotency_key=idempotency_key,
        )
        self._session.add(seed)
        await self._session.flush()
        return PaperAccountSeedResult(account, seed, True)
