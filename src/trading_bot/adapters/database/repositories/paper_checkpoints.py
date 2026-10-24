"""Repository append-only cho PAPER account checkpoint và event dedupe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text

from trading_bot.adapters.database.models.entities import TradingAccount
from trading_bot.adapters.database.models.paper_entities import (
    FillRecord,
    OrderIntentRecord,
    PaperExecutionCheckpointRecord,
    PositionEventRecord,
    TradeCostRecord,
    TradeRecord,
)
from trading_bot.adapters.database.models.risk_entities import (
    RiskReservationRecord,
    TradePlanRecord,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_recovery import verify_paper_ledger_consistency
from trading_bot.domain.execution.paper_state import PaperExecutionSnapshot


class PaperCheckpointConflict(RuntimeError):
    """Checkpoint key bị dùng lại khác nội dung hoặc event đến sai thứ tự."""


@dataclass(frozen=True, slots=True)
class PaperCheckpointAppendResult:
    revision: int
    snapshot: PaperExecutionSnapshot
    duplicate: bool


class PaperCheckpointRepository:
    """Lưu revision snapshot account trong tenant hiện tại, không tự commit."""

    async def append(
        self,
        uow: UnitOfWork,
        *,
        account_id: UUID,
        event_key: str,
        event_hash: str,
        snapshot: PaperExecutionSnapshot,
        occurred_at: datetime,
        event_scope: str | None = None,
    ) -> PaperCheckpointAppendResult:
        _validate_request(uow, event_key, event_hash, snapshot, occurred_at, event_scope)
        await self.lock_account(uow, account_id=account_id)
        existing = await uow.session.scalar(
            select(PaperExecutionCheckpointRecord).where(
                PaperExecutionCheckpointRecord.workspace_id == uow.workspace_id,
                PaperExecutionCheckpointRecord.account_id == account_id,
                PaperExecutionCheckpointRecord.event_key == event_key,
            )
        )
        if existing is not None:
            if (
                existing.event_hash != event_hash
                or existing.event_scope != event_scope
                or existing.event_at != occurred_at
            ):
                raise PaperCheckpointConflict("Checkpoint event key đã có payload khác.")
            latest = await self.load_latest(uow, account_id=account_id)
            if latest is None:
                raise PaperCheckpointConflict("Không tìm thấy latest account checkpoint.")
            revision, latest_snapshot = latest
            return PaperCheckpointAppendResult(revision, latest_snapshot, duplicate=True)

        latest = await uow.session.scalar(
            select(PaperExecutionCheckpointRecord)
            .where(
                PaperExecutionCheckpointRecord.workspace_id == uow.workspace_id,
                PaperExecutionCheckpointRecord.account_id == account_id,
            )
            .order_by(PaperExecutionCheckpointRecord.revision.desc())
            .limit(1)
        )
        if event_scope is not None:
            latest_scope = await uow.session.scalar(
                select(PaperExecutionCheckpointRecord)
                .where(
                    PaperExecutionCheckpointRecord.workspace_id == uow.workspace_id,
                    PaperExecutionCheckpointRecord.account_id == account_id,
                    PaperExecutionCheckpointRecord.event_scope == event_scope,
                )
                .order_by(PaperExecutionCheckpointRecord.event_at.desc())
                .limit(1)
            )
            if latest_scope is not None and occurred_at < latest_scope.event_at:
                raise PaperCheckpointConflict("Market event out-of-order theo symbol.")

        revision = latest.revision + 1 if latest is not None else 1
        row = PaperExecutionCheckpointRecord(
            id=uuid4(),
            workspace_id=uow.workspace_id,
            account_id=account_id,
            mode="PAPER",
            revision=revision,
            event_key=event_key,
            event_hash=event_hash,
            event_scope=event_scope,
            event_at=occurred_at,
            schema_version=snapshot.schema_version,
            policy_hash=snapshot.policy_hash,
            snapshot_hash=snapshot.content_hash,
            snapshot_payload=snapshot.payload,
        )
        uow.session.add(row)
        await uow.session.flush()
        return PaperCheckpointAppendResult(revision, snapshot, duplicate=False)

    async def lock_account(self, uow: UnitOfWork, *, account_id: UUID) -> None:
        """Tuần tự hóa mọi transition PAPER của account trong transaction hiện tại."""
        if not isinstance(uow.workspace_id, UUID):
            raise PaperCheckpointConflict("UnitOfWork thiếu workspace UUID.")
        await uow.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"paper-checkpoint:{uow.workspace_id}:{account_id}"},
        )
        account = await uow.session.scalar(
            select(TradingAccount).where(
                TradingAccount.workspace_id == uow.workspace_id,
                TradingAccount.id == account_id,
            )
        )
        if account is None or account.mode != "PAPER" or account.status != "ENABLED":
            raise PaperCheckpointConflict("Checkpoint cần PAPER account ENABLED trong workspace.")

    async def load_latest(
        self, uow: UnitOfWork, *, account_id: UUID
    ) -> tuple[int, PaperExecutionSnapshot] | None:
        """Nạp revision mới nhất trong scope đã thiết lập bởi UnitOfWork."""
        row = await uow.session.scalar(
            select(PaperExecutionCheckpointRecord)
            .where(
                PaperExecutionCheckpointRecord.workspace_id == uow.workspace_id,
                PaperExecutionCheckpointRecord.account_id == account_id,
            )
            .order_by(PaperExecutionCheckpointRecord.revision.desc())
            .limit(1)
        )
        if row is None:
            return None
        snapshot = PaperExecutionSnapshot(
            schema_version=row.schema_version,
            policy_hash=row.policy_hash,
            payload=row.snapshot_payload,
            content_hash=row.snapshot_hash,
        )
        return row.revision, snapshot

    async def verify_ledger_consistency(
        self,
        uow: UnitOfWork,
        *,
        account_id: UUID,
        snapshot: PaperExecutionSnapshot,
    ) -> None:
        """Đọc mọi ledger trong workspace/account rồi đối soát trước khi hydrate."""
        intents = list(
            (
                await uow.session.scalars(
                    select(OrderIntentRecord).where(
                        OrderIntentRecord.workspace_id == uow.workspace_id,
                        OrderIntentRecord.account_id == account_id,
                    )
                )
            ).all()
        )
        plans = list(
            (
                await uow.session.scalars(
                    select(TradePlanRecord).where(
                        TradePlanRecord.workspace_id == uow.workspace_id,
                        TradePlanRecord.account_id == account_id,
                    )
                )
            ).all()
        )
        fills = list(
            (
                await uow.session.scalars(
                    select(FillRecord).where(
                        FillRecord.workspace_id == uow.workspace_id,
                        FillRecord.account_id == account_id,
                    )
                )
            ).all()
        )
        position_events = list(
            (
                await uow.session.scalars(
                    select(PositionEventRecord).where(
                        PositionEventRecord.workspace_id == uow.workspace_id,
                        PositionEventRecord.account_id == account_id,
                    )
                )
            ).all()
        )
        trades = list(
            (
                await uow.session.scalars(
                    select(TradeRecord).where(
                        TradeRecord.workspace_id == uow.workspace_id,
                        TradeRecord.account_id == account_id,
                    )
                )
            ).all()
        )
        costs = list(
            (
                await uow.session.scalars(
                    select(TradeCostRecord).where(
                        TradeCostRecord.workspace_id == uow.workspace_id,
                        TradeCostRecord.account_id == account_id,
                    )
                )
            ).all()
        )
        reservations = list(
            (
                await uow.session.scalars(
                    select(RiskReservationRecord).where(
                        RiskReservationRecord.workspace_id == uow.workspace_id,
                        RiskReservationRecord.account_id == account_id,
                    )
                )
            ).all()
        )
        verify_paper_ledger_consistency(
            snapshot,
            intents=intents,
            plans=plans,
            fills=fills,
            position_events=position_events,
            trades=trades,
            costs=costs,
            reservations=reservations,
        )


def _validate_request(
    uow: UnitOfWork,
    event_key: str,
    event_hash: str,
    snapshot: PaperExecutionSnapshot,
    occurred_at: datetime,
    event_scope: str | None,
) -> None:
    if not isinstance(uow.workspace_id, UUID):
        raise PaperCheckpointConflict("UnitOfWork thiếu workspace UUID.")
    if not event_key.strip() or len(event_key) > 240:
        raise PaperCheckpointConflict("Checkpoint event key trống hoặc quá dài.")
    if (
        not _is_sha256(event_hash)
        or not _is_sha256(snapshot.policy_hash)
        or not _is_sha256(snapshot.content_hash)
    ):
        raise PaperCheckpointConflict("Checkpoint event/policy/snapshot hash không hợp lệ.")
    if snapshot.schema_version < 1 or not snapshot.payload:
        raise PaperCheckpointConflict("Checkpoint payload/schema không hợp lệ.")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise PaperCheckpointConflict("Checkpoint event time cần timezone.")
    if event_scope is not None and (not event_scope.strip() or len(event_scope) > 120):
        raise PaperCheckpointConflict("Checkpoint event scope không hợp lệ.")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
