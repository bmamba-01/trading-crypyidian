"""Probe PostgreSQL WP-03; mỗi phase chạy trong container migrator."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    BotInstance,
    RiskDecisionRecord,
    RiskReservationRecord,
    TradePlanRecord,
    TradingAccount,
    UserAccount,
    UserMembership,
    Workspace,
    WorkspaceSettingsVersion,
)
from trading_bot.adapters.database.repositories.venue_order_ledger import (
    VenueOrderLedgerConflict,
    VenueOrderLedgerRepository,
    VenueOrderTransitionCommand,
)
from trading_bot.domain.execution.venue_orders import VenueOrderIntent, VenueOrderState

STATE = json.loads(os.environ.get("WP03_STATE", "{}"))
PHASE = os.environ.get("WP03_PHASE", "")
RUNTIME = "crypidian_trading_runtime"


def _engine(role: str | None = None):
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    connect_args = {"options": f"-c role={role}"} if role else {}
    return create_async_engine(url, connect_args=connect_args)


async def _seed() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    account_id = UUID(STATE["account_id"])
    other_account_id = UUID(STATE["other_account_id"])
    settings_id = UUID(STATE["settings_version_id"])
    user_id = UUID(STATE["user_id"])
    membership_id = UUID(STATE["membership_id"])
    plan_hash = STATE["plan_hash"]
    risk_decision_id = UUID(STATE["risk_decision_id"])
    reservation_id = UUID(STATE["reservation_id"])
    other_reservation_id = UUID(STATE["other_reservation_id"])
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        session.add(
            Workspace(
                id=workspace_id,
                name="WP03 venue ledger",
                slug=f"wp03-{str(workspace_id)[:8]}",
            )
        )
        session.add(
            UserAccount(
                id=user_id,
                email_normalized=f"wp03-{str(user_id)[:8]}@example.test",
                password_hash="test-only-not-a-login-hash",
                status="ACTIVE",
            )
        )
        await session.flush()
        session.add(
            UserMembership(
                id=membership_id,
                workspace_id=workspace_id,
                user_id=user_id,
                status="ACTIVE",
            )
        )
        await session.flush()
        settings = WorkspaceSettingsVersion(
            id=settings_id,
            workspace_id=workspace_id,
            version=1,
            parent_version_id=None,
            schema_version=1,
            settings_json={"test_fixture": True},
            settings_hash="a" * 64,
            status="DRAFT",
            revision=1,
            created_by=membership_id,
            approved_by=None,
        )
        session.add(settings)
        session.add(
            TradingAccount(
                id=account_id,
                workspace_id=workspace_id,
                name="WP03 ledger PAPER account",
                venue="binance_usdm",
                mode="PAPER",
                environment="local",
                status="DISABLED",
                max_notional=Decimal("1000"),
            )
        )
        session.add(
            TradingAccount(
                id=other_account_id,
                workspace_id=workspace_id,
                name="WP03 second PAPER account",
                venue="binance_usdm",
                mode="PAPER",
                environment="local",
                status="DISABLED",
                max_notional=Decimal("1000"),
            )
        )
        await session.flush()
        bot_id = await session.scalar(
            select(BotInstance.id).where(
                BotInstance.workspace_id == workspace_id,
                BotInstance.trading_account_id == account_id,
            )
        )
        assert bot_id is not None
        await session.execute(
            text("SELECT set_config('app.bot_id', :bot_id, true)"),
            {"bot_id": str(bot_id)},
        )
        other_bot_id = await session.scalar(
            select(BotInstance.id).where(
                BotInstance.workspace_id == workspace_id,
                BotInstance.trading_account_id == other_account_id,
            )
        )
        assert other_bot_id is not None
        session.add(
            TradePlanRecord(
                workspace_id=workspace_id,
                account_id=account_id,
                plan_hash=plan_hash,
                symbol="BTCUSDT",
                notional=Decimal("100"),
                payload={"fixture": "wp03"},
            )
        )
        await session.flush()
        db_now = await session.scalar(select(func.clock_timestamp()))
        assert db_now is not None
        settings.published_at = db_now
        settings.effective_at = db_now
        settings.status = "PUBLISHED"
        settings.approved_by = membership_id
        await session.flush()
        session.add(
            RiskDecisionRecord(
                id=risk_decision_id,
                workspace_id=workspace_id,
                account_id=account_id,
                decision_key=f"wp03-risk-{str(risk_decision_id)[:8]}",
                plan_hash=plan_hash,
                snapshot_id="wp03-snapshot",
                approved=True,
                reason_code="APPROVED",
                detail_vi="Fixture E2E venue ledger.",
                payload={"fixture": True},
            )
        )
        await session.flush()
        session.add(
            RiskReservationRecord(
                id=reservation_id,
                workspace_id=workspace_id,
                account_id=account_id,
                risk_decision_id=risk_decision_id,
                idempotency_key=f"wp03-reservation-{str(reservation_id)[:8]}",
                plan_hash=plan_hash,
                symbol="BTCUSDT",
                reserved_risk=Decimal("1"),
                reserved_notional=Decimal("100"),
                reserved_initial_margin=Decimal("20"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                status="RESERVED",
            )
        )
        session.add(
            RiskReservationRecord(
                id=other_reservation_id,
                workspace_id=workspace_id,
                account_id=account_id,
                risk_decision_id=risk_decision_id,
                idempotency_key=f"wp03-reservation-{str(other_reservation_id)[:8]}",
                plan_hash=plan_hash,
                symbol="BTCUSDT",
                reserved_risk=Decimal("1"),
                reserved_notional=Decimal("100"),
                reserved_initial_margin=Decimal("20"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                status="RESERVED",
            )
        )
        session.add(
            RiskReservationRecord(
                id=UUID(STATE["paper_transfer_reservation_id"]),
                workspace_id=workspace_id,
                account_id=account_id,
                risk_decision_id=risk_decision_id,
                idempotency_key=(f"wp03-reservation-{STATE['paper_transfer_reservation_id'][:8]}"),
                plan_hash=plan_hash,
                symbol="BTCUSDT",
                reserved_risk=Decimal("1"),
                reserved_notional=Decimal("100"),
                reserved_initial_margin=Decimal("20"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                status="RESERVED",
            )
        )
        await session.flush()
        await session.execute(
            text("SELECT set_config('app.bot_id', :bot_id, true)"),
            {"bot_id": str(other_bot_id)},
        )
        other_risk_decision_id = UUID(STATE["other_risk_decision_id"])
        session.add(
            TradePlanRecord(
                workspace_id=workspace_id,
                account_id=other_account_id,
                plan_hash=plan_hash,
                symbol="BTCUSDT",
                notional=Decimal("100"),
                payload={"fixture": "wp03-other-bot"},
            )
        )
        await session.flush()
        session.add(
            RiskDecisionRecord(
                id=other_risk_decision_id,
                workspace_id=workspace_id,
                account_id=other_account_id,
                decision_key=f"wp03-risk-{str(other_risk_decision_id)[:8]}",
                plan_hash=plan_hash,
                snapshot_id="wp03-other-snapshot",
                approved=True,
                reason_code="APPROVED",
                detail_vi="Fixture bot scope thứ hai.",
                payload={"fixture": True},
            )
        )
        await session.flush()
        session.add(
            RiskReservationRecord(
                id=UUID(STATE["cross_bot_reservation_id"]),
                workspace_id=workspace_id,
                account_id=other_account_id,
                risk_decision_id=other_risk_decision_id,
                idempotency_key=f"wp03-reservation-{STATE['cross_bot_reservation_id'][:8]}",
                plan_hash=plan_hash,
                symbol="BTCUSDT",
                reserved_risk=Decimal("1"),
                reserved_notional=Decimal("100"),
                reserved_initial_margin=Decimal("20"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                status="RESERVED",
            )
        )
        await session.flush()
        seeded_reservations = await session.scalar(
            select(func.count(RiskReservationRecord.id)).where(
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.account_id == account_id,
            )
        )
        assert seeded_reservations == 3, seeded_reservations
    await engine.dispose()
    return {"seeded": True, "bot_id": str(bot_id), "other_bot_id": str(other_bot_id)}


def _intent() -> VenueOrderIntent:
    return VenueOrderIntent(
        intent_id=UUID(STATE["intent_id"]),
        workspace_id=STATE["workspace_id"],
        account_id=STATE["account_id"],
        idempotency_key="wp03-e2e-intent-key",
        reservation_id=UUID(STATE["reservation_id"]),
        plan_hash=STATE["plan_hash"],
        settings_version=STATE["settings_version_id"],
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.01"),
        client_order_id=STATE["client_order_id"],
        created_at=datetime.now(UTC),
    )


async def _ledger() -> dict[str, object]:
    engine = _engine(RUNTIME)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    account_id = UUID(STATE["account_id"])
    intent = _intent()
    command = VenueOrderTransitionCommand(
        event_key="wp03-e2e-submit-attempt-1",
        actor_id="runtime-e2e",
        source="SUBMISSION",
        state=VenueOrderState.SUBMITTING,
        attempt_count=1,
        last_attempt_at=datetime.now(UTC),
        retry_not_before=None,
        venue_order_id=None,
        filled_quantity=Decimal("0"),
        last_event_at=None,
    )
    try:
        async with sessions() as session, session.begin():
            session.info["bot_id"] = UUID(STATE["bot_id"])
            repository = VenueOrderLedgerRepository(session)
            first = await repository.create_intent(
                workspace_id=workspace_id,
                account_id=account_id,
                reservation_id=UUID(STATE["reservation_id"]),
                intent=intent,
                actor_id="runtime-e2e",
            )
            duplicate = await repository.create_intent(
                workspace_id=workspace_id,
                account_id=account_id,
                reservation_id=UUID(STATE["reservation_id"]),
                intent=intent,
                actor_id="runtime-e2e",
            )
            assert duplicate.is_duplicate
            assert not first.is_duplicate
            await session.execute(
                text(
                    "UPDATE risk_reservation SET status = 'UNKNOWN', resolution_reason = NULL "
                    "WHERE workspace_id = :workspace_id AND id = :reservation_id"
                ),
                {
                    "workspace_id": workspace_id,
                    "reservation_id": UUID(STATE["other_reservation_id"]),
                },
            )
            await session.execute(
                text(
                    "UPDATE risk_reservation SET status = 'RELEASED', "
                    "resolution_reason = 'ORDER_ABSENT' "
                    "WHERE workspace_id = :workspace_id AND id = :reservation_id"
                ),
                {
                    "workspace_id": workspace_id,
                    "reservation_id": UUID(STATE["other_reservation_id"]),
                },
            )
            paper_unknown_release_succeeded = await session.scalar(
                text(
                    "SELECT status = 'RELEASED' FROM risk_reservation "
                    "WHERE workspace_id = :workspace_id AND id = :reservation_id"
                ),
                {
                    "workspace_id": workspace_id,
                    "reservation_id": UUID(STATE["other_reservation_id"]),
                },
            )
            await session.execute(
                text(
                    "UPDATE risk_reservation SET status = 'UNKNOWN', resolution_reason = NULL "
                    "WHERE workspace_id = :workspace_id AND id = :reservation_id"
                ),
                {
                    "workspace_id": workspace_id,
                    "reservation_id": UUID(STATE["paper_transfer_reservation_id"]),
                },
            )
            await session.execute(
                text(
                    "UPDATE risk_reservation SET status = 'TRANSFERRED', "
                    "resolution_reason = 'OPEN_RISK_RECORDED' "
                    "WHERE workspace_id = :workspace_id AND id = :reservation_id"
                ),
                {
                    "workspace_id": workspace_id,
                    "reservation_id": UUID(STATE["paper_transfer_reservation_id"]),
                },
            )
            paper_unknown_transfer_succeeded = await session.scalar(
                text(
                    "SELECT status = 'TRANSFERRED' FROM risk_reservation "
                    "WHERE workspace_id = :workspace_id AND id = :reservation_id"
                ),
                {
                    "workspace_id": workspace_id,
                    "reservation_id": UUID(STATE["paper_transfer_reservation_id"]),
                },
            )
            reservation_mismatch_rejected = False
            alternate_reservation_intent = VenueOrderIntent(
                intent_id=UUID(STATE["intent_id"]),
                workspace_id=STATE["workspace_id"],
                account_id=STATE["account_id"],
                idempotency_key="wp03-e2e-intent-key",
                reservation_id=UUID(STATE["other_reservation_id"]),
                plan_hash=STATE["plan_hash"],
                settings_version=STATE["settings_version_id"],
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.01"),
                client_order_id=STATE["client_order_id"],
                created_at=datetime.now(UTC),
            )
            try:
                async with session.begin_nested():
                    await repository.create_intent(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        reservation_id=UUID(STATE["other_reservation_id"]),
                        intent=alternate_reservation_intent,
                        actor_id="runtime-e2e",
                    )
            except VenueOrderLedgerConflict as error:
                if "reservation khác" not in str(error):
                    raise
                reservation_mismatch_rejected = True
            transition = await repository.transition(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent.intent_id,
                expected_sequence=1,
                command=command,
            )
            assert transition.record.state == VenueOrderState.SUBMITTING.value
            transition_state = transition.record.state
            reservation_after_submit = await session.scalar(
                select(RiskReservationRecord.status).where(
                    RiskReservationRecord.workspace_id == workspace_id,
                    RiskReservationRecord.account_id == account_id,
                    RiskReservationRecord.id == UUID(STATE["reservation_id"]),
                )
            )
            assert reservation_after_submit == "UNKNOWN"
            duplicate_transition = await repository.transition(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent.intent_id,
                expected_sequence=1,
                command=command,
            )
            assert duplicate_transition.is_duplicate
            transitions = await repository.list_transitions(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent.intent_id,
            )
            transition_count = len(transitions)
            transition_sequence = transition.record.transition_sequence
            invalid_transition_rejected = False
            try:
                async with session.begin_nested():
                    await repository.transition(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        intent_id=intent.intent_id,
                        expected_sequence=2,
                        command=VenueOrderTransitionCommand(
                            event_key="wp03-e2e-invalid-fill",
                            actor_id="runtime-e2e",
                            source="SUBMISSION",
                            state=VenueOrderState.FILLED,
                            attempt_count=1,
                            last_attempt_at=command.last_attempt_at,
                            retry_not_before=None,
                            venue_order_id="venue-order-1",
                            filled_quantity=Decimal("0.01"),
                            last_event_at=datetime.now(UTC),
                        ),
                    )
            except DBAPIError as error:
                if "WP03_VENUE_ORDER_TRANSITION_INVALID" not in str(error.orig):
                    raise RuntimeError("Database trả mã lỗi transition ngoài dự kiến.") from error
                invalid_transition_rejected = True

            same_state_rejected = False
            try:
                async with session.begin_nested():
                    await repository.transition(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        intent_id=intent.intent_id,
                        expected_sequence=2,
                        command=VenueOrderTransitionCommand(
                            event_key="wp03-e2e-invalid-repeat-submit",
                            actor_id="runtime-e2e",
                            source="SUBMISSION",
                            state=VenueOrderState.SUBMITTING,
                            attempt_count=1,
                            last_attempt_at=command.last_attempt_at,
                            retry_not_before=None,
                            venue_order_id=None,
                            filled_quantity=Decimal("0"),
                            last_event_at=None,
                        ),
                    )
            except DBAPIError as error:
                if "WP03_VENUE_ORDER_TRANSITION_INVALID" not in str(error.orig):
                    raise RuntimeError(
                        "Database trả mã lỗi repeat-submit ngoài dự kiến."
                    ) from error
                same_state_rejected = True

            retry_without_deadline_rejected = False
            try:
                async with session.begin_nested():
                    await repository.transition(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        intent_id=intent.intent_id,
                        expected_sequence=2,
                        command=VenueOrderTransitionCommand(
                            event_key="wp03-e2e-retry-no-deadline",
                            actor_id="runtime-e2e",
                            source="SUBMISSION",
                            state=VenueOrderState.RETRYABLE,
                            attempt_count=1,
                            last_attempt_at=command.last_attempt_at,
                            retry_not_before=None,
                            venue_order_id=None,
                            filled_quantity=Decimal("0"),
                            last_event_at=None,
                        ),
                    )
            except DBAPIError as error:
                if "WP03_VENUE_ORDER_RETRY_BACKOFF_REQUIRED" not in str(error.orig):
                    raise RuntimeError("Database không chặn retry thiếu deadline.") from error
                retry_without_deadline_rejected = True

            await repository.transition(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent.intent_id,
                expected_sequence=2,
                command=VenueOrderTransitionCommand(
                    event_key="wp03-e2e-submit-unknown",
                    actor_id="runtime-e2e",
                    source="SUBMISSION",
                    state=VenueOrderState.UNKNOWN,
                    attempt_count=1,
                    last_attempt_at=command.last_attempt_at,
                    retry_not_before=None,
                    venue_order_id=None,
                    filled_quantity=Decimal("0"),
                    last_event_at=datetime.now(UTC),
                ),
            )
            reservation_release_blocked = False
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "UPDATE risk_reservation SET status = 'RELEASED', "
                            "resolution_reason = 'ORDER_ABSENT' "
                            "WHERE workspace_id = :workspace_id AND id = :reservation_id"
                        ),
                        {
                            "workspace_id": workspace_id,
                            "reservation_id": UUID(STATE["reservation_id"]),
                        },
                    )
            except DBAPIError as error:
                if "WP03_VENUE_ORDER_RESERVATION_AUTHENTICATED_RECONCILIATION_REQUIRED" not in str(
                    error.orig
                ):
                    raise RuntimeError(
                        "Database không giữ reservation sau khi bắt đầu gửi."
                    ) from error
                reservation_release_blocked = True

            reservation_transfer_blocked = False
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "UPDATE risk_reservation SET status = 'TRANSFERRED', "
                            "resolution_reason = 'OPEN_RISK_RECORDED' "
                            "WHERE workspace_id = :workspace_id AND id = :reservation_id"
                        ),
                        {
                            "workspace_id": workspace_id,
                            "reservation_id": UUID(STATE["reservation_id"]),
                        },
                    )
            except DBAPIError as error:
                if "WP03_VENUE_ORDER_TRANSFER_NEEDS_OPEN_RISK_RECORD" not in str(error.orig):
                    raise RuntimeError(
                        "Database cho phép chuyển risk chưa được ghi nhận."
                    ) from error
                reservation_transfer_blocked = True
            unknown_retry_blocked = False
            try:
                async with session.begin_nested():
                    await repository.transition(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        intent_id=intent.intent_id,
                        expected_sequence=3,
                        command=VenueOrderTransitionCommand(
                            event_key="wp03-e2e-unknown-retry-unverified",
                            actor_id="runtime-e2e",
                            source="RECONCILIATION",
                            state=VenueOrderState.RETRYABLE,
                            attempt_count=1,
                            last_attempt_at=command.last_attempt_at,
                            retry_not_before=datetime.now(UTC) + timedelta(minutes=1),
                            venue_order_id=None,
                            filled_quantity=Decimal("0"),
                            last_event_at=datetime.now(UTC),
                            evidence_hash="f" * 64,
                        ),
                    )
            except DBAPIError as error:
                if "WP03_VENUE_ORDER_AUTHENTICATED_RECONCILIATION_REQUIRED" not in str(error.orig):
                    raise RuntimeError("Database tin retry UNKNOWN chỉ từ hash tùy ý.") from error
                unknown_retry_blocked = True
            last_event_clear_rejected = False
            try:
                async with session.begin_nested():
                    await repository.transition(
                        workspace_id=workspace_id,
                        account_id=account_id,
                        intent_id=intent.intent_id,
                        expected_sequence=3,
                        command=VenueOrderTransitionCommand(
                            event_key="wp03-e2e-clear-last-event",
                            actor_id="runtime-e2e",
                            source="USER_STREAM",
                            state=VenueOrderState.UNKNOWN,
                            attempt_count=1,
                            last_attempt_at=command.last_attempt_at,
                            retry_not_before=None,
                            venue_order_id=None,
                            filled_quantity=Decimal("0"),
                            last_event_at=None,
                        ),
                    )
            except DBAPIError as error:
                if "WP03_VENUE_ORDER_EVENT_TIME_NOT_MONOTONIC" not in str(error.orig):
                    raise RuntimeError("Database cho phép xóa last_event_at đã ghi.") from error
                last_event_clear_rejected = True

            event_mutation_rejected = False
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "UPDATE venue_order_transition SET actor_id = actor_id "
                            "WHERE workspace_id = :workspace_id AND intent_id = :intent_id"
                        ),
                        {"workspace_id": workspace_id, "intent_id": intent.intent_id},
                    )
            except DBAPIError:
                event_mutation_rejected = True

            try:
                cross_workspace = await repository.get_intent(
                    workspace_id=UUID(STATE["other_workspace_id"]),
                    account_id=account_id,
                    intent_id=intent.intent_id,
                )
            except VenueOrderLedgerConflict:
                cross_workspace = None
            session.info["bot_id"] = UUID(STATE["bot_id"])
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot_id, true)"),
                {"bot_id": STATE["bot_id"]},
            )
            other_intent = VenueOrderIntent(
                intent_id=UUID(STATE["other_intent_id"]),
                workspace_id=STATE["workspace_id"],
                account_id=STATE["other_account_id"],
                idempotency_key="wp03-e2e-other-bot-intent",
                reservation_id=UUID(STATE["cross_bot_reservation_id"]),
                plan_hash=STATE["plan_hash"],
                settings_version=STATE["settings_version_id"],
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.01"),
                client_order_id=STATE["other_client_order_id"],
                created_at=datetime.now(UTC),
            )
            session.info["bot_id"] = UUID(STATE["other_bot_id"])
            await repository.create_intent(
                workspace_id=workspace_id,
                account_id=UUID(STATE["other_account_id"]),
                reservation_id=UUID(STATE["cross_bot_reservation_id"]),
                intent=other_intent,
                actor_id="runtime-e2e",
            )
            session.info["bot_id"] = UUID(STATE["bot_id"])
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot_id, true)"),
                {"bot_id": STATE["bot_id"]},
            )
            cross_bot_count = await session.scalar(
                text(
                    "SELECT count(*) FROM venue_order_intent "
                    "WHERE workspace_id = :workspace_id AND account_id = :account_id"
                ),
                {"workspace_id": workspace_id, "account_id": UUID(STATE["other_account_id"])},
            )
        return {
            "first_create": first.is_duplicate,
            "duplicate_create": duplicate.is_duplicate,
            "transition_state": transition_state,
            "transition_sequence": transition_sequence,
            "transition_count": transition_count,
            "invalid_transition_rejected": invalid_transition_rejected,
            "same_state_rejected": same_state_rejected,
            "reservation_mismatch_rejected": reservation_mismatch_rejected,
            "retry_without_deadline_rejected": retry_without_deadline_rejected,
            "reservation_release_blocked": reservation_release_blocked,
            "reservation_transfer_blocked": reservation_transfer_blocked,
            "reservation_after_submit": reservation_after_submit,
            "paper_unknown_release_succeeded": paper_unknown_release_succeeded,
            "paper_unknown_transfer_succeeded": paper_unknown_transfer_succeeded,
            "unknown_retry_blocked": unknown_retry_blocked,
            "last_event_clear_rejected": last_event_clear_rejected,
            "event_mutation_rejected": event_mutation_rejected,
            "cross_workspace_visible": cross_workspace is not None,
            "cross_bot_visible": bool(cross_bot_count),
        }
    finally:
        await engine.dispose()


async def _reconcile() -> dict[str, object]:
    from trading_bot.adapters.database.venue_reconciliation_ledger import (
        DatabaseVenueOrderLedgerPort,
    )
    from trading_bot.application.venue_order_reconciliation import (
        VenueOrderReconciliationService,
    )
    from trading_bot.domain.execution.venue_orders import (
        ReconciliationOutcome,
        ReconciliationSource,
        VenueReconciliation,
    )
    from trading_bot.ports.venue_reconciliation import VerifiedVenueReconciliation

    engine = _engine(RUNTIME)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = UUID(STATE["workspace_id"])
    bot_id = UUID(STATE["bot_id"])
    account_id = UUID(STATE["account_id"])
    intent_id = UUID(STATE["intent_id"])
    reservation_id = UUID(STATE["reservation_id"])
    ledger = DatabaseVenueOrderLedgerPort(
        sessions=sessions,
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
    )

    class FixedVerifier:
        def __init__(self, outcome: ReconciliationOutcome) -> None:
            self.outcome = outcome

        async def verify(self, *, intent):
            now = datetime.now(UTC)
            absent = self.outcome is ReconciliationOutcome.ORDER_ABSENT
            evidence = VenueReconciliation(
                outcome=self.outcome,
                source=(
                    ReconciliationSource.ORDER_QUERY_AND_TRADE_HISTORY
                    if absent
                    else ReconciliationSource.REST_ORDER_QUERY
                ),
                source_ref="wp03-e2e-verified-source",
                workspace_id=str(workspace_id),
                account_id=str(account_id),
                client_order_id=intent.client_order_id,
                checked_at=now,
                cumulative_filled=Decimal("0") if absent else Decimal("0.003"),
                venue_order_id=None if absent else "wp03-e2e-venue-order",
            )
            return VerifiedVenueReconciliation(
                intent_id=intent.id,
                workspace_id=workspace_id,
                bot_id=bot_id,
                account_id=account_id,
                reservation_id=reservation_id,
                evidence=evidence,
            )

    absent_service = VenueOrderReconciliationService(
        ledger=ledger,
        verifier=FixedVerifier(ReconciliationOutcome.ORDER_ABSENT),
    )
    absent_result = await absent_service.reconcile_unknown(
        workspace_id=workspace_id,
        account_id=account_id,
        intent_id=intent_id,
    )
    async with sessions() as session, session.begin():
        session.info["bot_id"] = bot_id
        repository = VenueOrderLedgerRepository(session)
        after_absent = await repository.get_intent(
            workspace_id=workspace_id,
            account_id=account_id,
            intent_id=intent_id,
        )
        assert after_absent is not None
        absent_transition_count = len(
            await repository.list_transitions(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent_id,
            )
        )

    partial_service = VenueOrderReconciliationService(
        ledger=ledger,
        verifier=FixedVerifier(ReconciliationOutcome.ORDER_OPEN),
    )
    partial_result = await partial_service.reconcile_unknown(
        workspace_id=workspace_id,
        account_id=account_id,
        intent_id=intent_id,
    )
    async with sessions() as session, session.begin():
        session.info["bot_id"] = bot_id
        repository = VenueOrderLedgerRepository(session)
        final = await repository.get_intent(
            workspace_id=workspace_id,
            account_id=account_id,
            intent_id=intent_id,
        )
        assert final is not None
        transition_count = len(
            await repository.list_transitions(
                workspace_id=workspace_id,
                account_id=account_id,
                intent_id=intent_id,
            )
        )
        reservation_status = await session.scalar(
            select(RiskReservationRecord.status).where(
                RiskReservationRecord.workspace_id == workspace_id,
                RiskReservationRecord.account_id == account_id,
                RiskReservationRecord.id == reservation_id,
            )
        )
    await engine.dispose()
    return {
        "absent_status": absent_result.status.value,
        "absent_reason": absent_result.reason,
        "absent_transition_count": absent_transition_count,
        "partial_status": partial_result.status.value,
        "partial_state": final.state,
        "partial_transition_count": transition_count,
        "reservation_status": reservation_status,
    }


async def main() -> None:
    if PHASE == "seed":
        result = await _seed()
    elif PHASE == "ledger":
        result = await _ledger()
    elif PHASE == "reconcile":
        result = await _reconcile()
    else:
        raise AssertionError(f"Unknown WP03 E2E phase: {PHASE}")
    print(json.dumps(result, default=str, sort_keys=True))


asyncio.run(main())
