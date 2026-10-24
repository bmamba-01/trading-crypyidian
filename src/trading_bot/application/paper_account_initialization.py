"""Khởi tạo synthetic PAPER account đúng một lần từ root settings đã publish."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.adapters.database.models.entities import PaperAccountSeed, TradingAccount
from trading_bot.adapters.database.repositories.paper_accounts import (
    PaperAccountSeedRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_execution_profile import ResolvedPaperExecutionProfile
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
from trading_bot.domain.events import EventEnvelope


class PaperAccountInitializationError(ValueError):
    """Root settings/profile không đủ điều kiện tạo PAPER account."""


@dataclass(frozen=True, slots=True)
class PaperAccountInitializationResult:
    """Account và seed ban đầu; created phân biệt lần đầu với lần retry."""

    account: TradingAccount
    seed: PaperAccountSeed
    created: bool


class PaperAccountInitializationService:
    """Kiểm tra profile đã resolve rồi ghi account/seed trong UnitOfWork."""

    def __init__(
        self,
        *,
        account_name: str = "paper-default",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not account_name.strip():
            raise ValueError("account_name không được để trống.")
        self._account_name = account_name
        self._clock = clock or (lambda: datetime.now(UTC))

    async def ensure_initialized(
        self,
        uow: UnitOfWork,
        *,
        published_settings: PublishedWorkspaceSettings,
        execution_profile: ResolvedPaperExecutionProfile,
        max_notional: Decimal,
        evaluated_at: datetime,
    ) -> PaperAccountInitializationResult:
        """Tạo seed PAPER idempotent; vốn đã tạo không bị đổi khi publish mới."""
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise PaperAccountInitializationError("evaluated_at phải có timezone.")
        if (
            published_settings.workspace_id != uow.workspace_id
            or execution_profile.workspace_id != uow.workspace_id
        ):
            raise PaperAccountInitializationError(
                "Workspace settings/profile không khớp UnitOfWork."
            )
        if (
            published_settings.settings.runtime.mode != "PAPER"
            or published_settings.effective_at is None
            or evaluated_at < published_settings.effective_at
        ):
            raise PaperAccountInitializationError(
                "Chỉ khởi tạo từ root settings PAPER đã có hiệu lực."
            )
        if (
            execution_profile.settings_version_id != published_settings.settings_version_id
            or execution_profile.settings_hash != published_settings.settings_hash
            or execution_profile.quote_asset
            != published_settings.settings.paper_execution.quote_asset
            or execution_profile.new_account_initial_equity
            != published_settings.settings.paper_execution.starting_equity
        ):
            raise PaperAccountInitializationError(
                "Execution profile không khớp phiên bản/capital root settings."
            )
        if not max_notional.is_finite() or max_notional <= 0:
            raise PaperAccountInitializationError("max_notional phải hữu hạn và dương.")

        repository_result = await PaperAccountSeedRepository(uow.session).ensure_seed(
            workspace_id=uow.workspace_id,
            account_name=self._account_name,
            quote_asset=execution_profile.quote_asset,
            initial_equity=execution_profile.new_account_initial_equity,
            max_notional=max_notional,
            source_settings_version_id=published_settings.settings_version_id,
            source_settings_hash=published_settings.settings_hash,
        )
        if repository_result.created:
            seed = repository_result.seed
            now = self._clock()
            uow.record_audit(
                action="paper.account.seeded",
                target_type="paper_account_seed",
                target_id=seed.id,
                after_state={
                    "trading_account_id": str(seed.trading_account_id),
                    "quote_asset": seed.quote_asset,
                    "initial_equity": str(seed.initial_equity),
                    "source_settings_version_id": str(seed.source_settings_version_id),
                    "source_settings_hash": seed.source_settings_hash,
                },
            )
            uow.enqueue(
                EventEnvelope(
                    event_type="paper.account.seeded",
                    workspace_id=uow.workspace_id,
                    aggregate_type="paper_account_seed",
                    aggregate_id=seed.id,
                    occurred_at=now,
                    correlation_id=uow.request_id,
                    idempotency_key=f"paper-account-seeded:{seed.id}",
                    schema_version=1,
                    payload={
                        "trading_account_id": str(seed.trading_account_id),
                        "quote_asset": seed.quote_asset,
                        "initial_equity": str(seed.initial_equity),
                        "source_settings_version_id": str(seed.source_settings_version_id),
                        "source_settings_hash": seed.source_settings_hash,
                    },
                )
            )
        return PaperAccountInitializationResult(
            repository_result.account,
            repository_result.seed,
            repository_result.created,
        )
