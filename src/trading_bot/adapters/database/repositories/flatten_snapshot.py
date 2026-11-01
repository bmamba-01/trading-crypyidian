"""Đọc snapshot flatten từ checkpoint, control và mark market theo policy workspace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from trading_bot.adapters.database.bot_scope import bind_account_bot
from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.bot_runtime_entities import BotRuntimeStateRecord
from trading_bot.adapters.database.models.entities import TradingAccount
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.models.paper_entities import OrderIntentRecord
from trading_bot.adapters.database.repositories.bot_runtime_controls import (
    BotRuntimeControlRepository,
)
from trading_bot.adapters.database.repositories.paper_checkpoints import PaperCheckpointRepository
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_execution_profile import (
    default_paper_execution_profile_catalog,
    resolve_paper_execution_profile,
)
from trading_bot.application.workspace_risk import mark_policy_from
from trading_bot.domain.execution.flatten_all import (
    FlattenBotSnapshot,
    FlattenSnapshot,
    FlattenTarget,
)
from trading_bot.domain.execution.paper import PaperExecutionGateway
from trading_bot.domain.risk.workspace_budget import MarkPolicy


class PostgresFlattenSnapshotProvider:
    """Tạo snapshot có RLS theo UoW; lỗi hydrate/readiness không bị coi là state rỗng."""

    async def capture(self, uow: UnitOfWork) -> FlattenSnapshot:
        settings_repository = WorkspaceSettingsRepository(uow.session)
        published = await settings_repository.load_effective_config(workspace_id=uow.workspace_id)
        if published is None:
            raise ValueError("Workspace cần settings PAPER đã publish.")
        profile = resolve_paper_execution_profile(
            published, default_paper_execution_profile_catalog()
        )
        mark_policy = mark_policy_from(published.settings)
        max_mark_age = timedelta(seconds=published.settings.risk.max_mark_age_seconds)
        now = await uow.session.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Không đọc được đồng hồ database.")
        now = now.astimezone(UTC)
        account_rows = await uow.session.execute(
            select(BotInstance, TradingAccount)
            .join(
                TradingAccount,
                (TradingAccount.workspace_id == BotInstance.workspace_id)
                & (TradingAccount.id == BotInstance.trading_account_id),
            )
            .where(
                BotInstance.workspace_id == uow.workspace_id,
                BotInstance.status == "ENABLED",
                TradingAccount.status == "ENABLED",
                TradingAccount.mode == "PAPER",
            )
            .order_by(BotInstance.id)
        )
        controls = BotRuntimeControlRepository(uow.session)
        checkpoint_repository = PaperCheckpointRepository()
        workspace_control = await controls.current_workspace(uow.workspace_id)
        bots: list[FlattenBotSnapshot] = []
        for bot, account in account_rows.all():
            await bind_account_bot(
                uow.session,
                workspace_id=uow.workspace_id,
                account_id=account.id,
                allow_switch=True,
            )
            latest = await checkpoint_repository.load_latest(uow, account_id=account.id)
            targets: list[FlattenTarget] = []
            if latest is not None:
                _, snapshot = latest
                await checkpoint_repository.verify_ledger_consistency(
                    uow, account_id=account.id, snapshot=snapshot
                )
                gateway = PaperExecutionGateway.restore(profile.fill_policy, snapshot)
                orders = {
                    row.idempotency_key: row
                    for row in (
                        await uow.session.scalars(
                            select(OrderIntentRecord).where(
                                OrderIntentRecord.workspace_id == uow.workspace_id,
                                OrderIntentRecord.bot_id == bot.id,
                                OrderIntentRecord.account_id == account.id,
                            )
                        )
                    ).all()
                }
                for position in sorted(gateway.open_positions(), key=lambda item: item.position_id):
                    mark = await self._mark(
                        uow,
                        symbol=position.symbol,
                        policy=mark_policy,
                        as_of=now,
                    )
                    target_fields: dict[str, Any] = {}
                    if mark is None:
                        target_fields["mark_error_code"] = "FLATTEN_MARK_UNAVAILABLE"
                    elif now - mark.close_time > max_mark_age:
                        target_fields["mark_error_code"] = "FLATTEN_MARK_STALE"
                    else:
                        target_fields |= {
                            "mark_price": mark.close,
                            "mark_record_id": mark.id,
                            "mark_revision": mark.revision,
                            "mark_checksum": mark.checksum,
                            "mark_close_time": mark.close_time,
                        }
                    order = next(
                        (
                            item
                            for item in gateway.open_orders()
                            if item.intent_id == position.intent_id
                        ),
                        None,
                    )
                    ledger_order = orders.get(order.idempotency_key) if order is not None else None
                    targets.append(
                        FlattenTarget(
                            target_type="POSITION",
                            target_id=position.position_id,
                            symbol=position.symbol,
                            status="OPEN",
                            quantity=position.quantity,
                            reservation_id=(
                                ledger_order.risk_reservation_id
                                if ledger_order is not None
                                else None
                            ),
                            **target_fields,
                        )
                    )
                for order in sorted(gateway.open_orders(), key=lambda item: item.intent_id):
                    ledger_order = orders.get(order.idempotency_key)
                    targets.append(
                        FlattenTarget(
                            target_type="OPEN_ORDER",
                            target_id=order.intent_id,
                            symbol=order.symbol,
                            status=order.status.value,
                            quantity=order.remaining_quantity,
                            reservation_id=(
                                ledger_order.risk_reservation_id
                                if ledger_order is not None
                                else None
                            ),
                        )
                    )
            state = await uow.session.scalar(
                select(BotRuntimeStateRecord).where(
                    BotRuntimeStateRecord.workspace_id == uow.workspace_id,
                    BotRuntimeStateRecord.bot_id == bot.id,
                    BotRuntimeStateRecord.account_id == account.id,
                )
            )
            bot_control = await controls.current_bot(uow.workspace_id, bot.id)
            bots.append(
                FlattenBotSnapshot(
                    bot_id=bot.id,
                    account_id=account.id,
                    bot_control_revision=bot_control.revision if bot_control is not None else 0,
                    workspace_control_revision=(
                        workspace_control.revision if workspace_control is not None else 0
                    ),
                    targets=tuple(targets),
                    readiness=state.readiness if state is not None else None,
                    activity_status=state.activity_status if state is not None else None,
                    account_status=account.status,
                    bot_entry_state=(
                        bot_control.entry_state.value if bot_control is not None else "ENABLED"
                    ),
                    workspace_killed=(
                        workspace_control is not None
                        and workspace_control.kill_state.value == "KILLED"
                    ),
                    applied_bot_control_revision=(
                        state.applied_bot_control_revision if state is not None else None
                    ),
                )
            )
        return FlattenSnapshot(uow.workspace_id, now, tuple(bots))

    async def _mark(
        self,
        uow: UnitOfWork,
        *,
        symbol: str,
        policy: MarkPolicy,
        as_of: datetime,
    ) -> MarketBar | None:
        row = await uow.session.scalar(
            select(MarketBar)
            .where(
                MarketBar.workspace_id == uow.workspace_id,
                MarketBar.symbol == symbol,
                MarketBar.source == policy.source,
                MarketBar.timeframe.in_(policy.timeframes),
                MarketBar.is_closed.is_(True),
                MarketBar.close_time <= as_of,
                MarketBar.available_at <= as_of,
            )
            .order_by(
                MarketBar.close_time.desc(),
                MarketBar.available_at.desc(),
                MarketBar.revision.desc(),
            )
            .limit(1)
        )
        return row
