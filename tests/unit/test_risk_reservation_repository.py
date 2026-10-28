from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.bot_scope_fakes import BotScopeSession

from trading_bot.adapters.database.repositories.risk_reservations import (
    RiskReservationRepository,
    _decision_key,
)
from trading_bot.domain.risk.engine import RiskDecision
from trading_bot.domain.risk.planner import calculate_plan_hash
from trading_bot.domain.risk.reservations import (
    ReservationConflict,
    ReservationOutcome,
    ReservationReleaseReason,
    ReservationStatus,
)
from trading_bot.domain.risk.workspace_budget import MarkPolicy

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


class ReservationSession(BotScopeSession):
    """Phiên giả ghi lại SQL và trả các kết quả đã xếp sẵn cho unit test."""

    def __init__(
        self,
        scalar_results: list[object | None],
        aggregate=(Decimal("0"), 0, Decimal("0"), Decimal("0"), Decimal("0")),
    ) -> None:
        for result in scalar_results:
            if hasattr(result, "status") and not hasattr(result, "expires_at"):
                result.expires_at = NOW + timedelta(hours=1)
        self.scalar_results = iter(scalar_results)
        self.aggregate = aggregate
        self.statements: list[str] = []
        self.added: list[object] = []
        self.flushes = 0

    async def scalar(self, statement: Any) -> object | None:
        if self.is_bot_query(statement):
            return self.bot_identity(statement)
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.scalar_results)

    async def execute(self, statement: Any, params: object = None) -> SimpleNamespace:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(one=lambda: self.aggregate)

    async def scalars(self, statement: Any) -> SimpleNamespace:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(all=lambda: [])

    def add(self, record: object) -> None:
        self.added.append(record)

    async def flush(self) -> None:
        self.flushes += 1


@dataclass(frozen=True)
class EvaluationPlanFixture:
    """Plan tối thiểu để kiểm tra lưu payload canonical mà không cần database."""

    workspace_id: str
    account_id: str
    plan_hash: str
    symbol: str
    notional: Decimal
    evidence: tuple[str, ...]


def decision(
    *,
    approved: bool = True,
    workspace_id=None,
    account_id=None,
    risk: str = "60",
    open_risk: str = "20",
    pending_risk: str = "0",
    notional: str = "500",
    open_notional: str = "200",
    symbol_open_notional: str = "200",
    max_notional: str = "10000",
    max_symbol_notional: str = "5000",
    pending_notional: str = "0",
    pending_symbol_notional: str = "0",
    available_margin: str = "1000",
    initial_margin: str = "100",
    pending_margin: str = "0",
    ceiling: str = "100",
    pending_count: int = 0,
    plan_hash: str = "plan-hash-a",
    candidate_expires_at: datetime | None = None,
) -> RiskDecision:
    return RiskDecision(
        approved=approved,
        reason_code="APPROVED" if approved else "RISK_REJECTED",
        detail_vi="Đã kiểm tra giới hạn rủi ro." if approved else "Bị từ chối.",
        plan_hash=plan_hash,
        candidate_expires_at=candidate_expires_at or datetime.now(UTC) + timedelta(minutes=5),
        workspace_id=str(workspace_id or uuid4()),
        account_id=str(account_id or uuid4()),
        settings_version_id="settings-v1",
        risk_profile_version_id="risk-v1",
        requested_risk=Decimal(risk),
        symbol="BTCUSDT",
        requested_notional=Decimal(notional),
        open_notional=Decimal(open_notional),
        symbol_open_notional=Decimal(symbol_open_notional),
        max_notional=Decimal(max_notional),
        max_symbol_notional=Decimal(max_symbol_notional),
        pending_reservation_notional=Decimal(pending_notional),
        pending_symbol_reservation_notional=Decimal(pending_symbol_notional),
        open_risk=Decimal(open_risk),
        max_aggregate_open_risk=Decimal(ceiling),
        pending_reservation_risk=Decimal(pending_risk),
        pending_reservation_count=pending_count,
        available_margin=Decimal(available_margin),
        requested_initial_margin=Decimal(initial_margin),
        pending_reservation_initial_margin=Decimal(pending_margin),
    )


def persisted_decision(decision_id, risk_decision):
    return SimpleNamespace(
        id=decision_id,
        approved=risk_decision.approved,
        plan_hash=risk_decision.plan_hash,
        decision_key=_decision_key(risk_decision),
    )


@pytest.mark.asyncio
async def test_reserve_locks_workspace_account_and_persists_scoped_idempotent_reservation() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    account = SimpleNamespace(id=account_id, workspace_id=workspace_id)
    approved = decision(workspace_id=workspace_id, account_id=account_id)
    session = ReservationSession([account, persisted_decision(decision_id, approved), None])
    repository = RiskReservationRepository(session)  # type: ignore[arg-type]

    result = await repository.reserve(
        approved, idempotency_key="intent-1", risk_decision_id=decision_id
    )

    assert result.status is ReservationStatus.RESERVED
    assert result.workspace_id == str(workspace_id)
    assert result.account_id == str(account_id)
    assert result.reserved_risk == Decimal("60")
    assert result.reserved_initial_margin == Decimal("100")
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "pg_advisory_xact_lock" in session.statements[1]
    assert "trading_account" in session.statements[2]
    assert "FOR UPDATE" in session.statements[2]
    assert "workspace_id" in session.statements[2]
    assert len(session.added) == 1
    assert session.added[0].risk_decision_id == decision_id
    assert session.added[0].expires_at == approved.candidate_expires_at
    assert result.expires_at == approved.candidate_expires_at
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_record_evaluation_persists_canonical_plan_and_decision_even_when_rejected() -> None:
    workspace_id, account_id = uuid4(), uuid4()
    plan = EvaluationPlanFixture(
        workspace_id=str(workspace_id),
        account_id=str(account_id),
        plan_hash="",
        symbol="BTCUSDT",
        notional=Decimal("500"),
        evidence=("feature-1", "bar-1"),
    )
    plan = replace(plan, plan_hash=calculate_plan_hash(plan))
    rejected = replace(
        decision(
            approved=False,
            workspace_id=workspace_id,
            account_id=account_id,
            plan_hash=plan.plan_hash,
        ),
        reason_code="ACCOUNT_STATE_STALE",
        snapshot_id="snapshot-1",
    )
    session = ReservationSession([SimpleNamespace(id=account_id), None, None])
    repository = RiskReservationRepository(session)  # type: ignore[arg-type]

    records = await repository.record_evaluation(plan=plan, decision=rejected)  # type: ignore[arg-type]

    assert records.trade_plan_id == session.added[0].id
    assert records.risk_decision_id == session.added[1].id
    assert session.added[0].payload["notional"] == "500"
    assert session.added[1].approved is False
    assert session.added[1].reason_code == "ACCOUNT_STATE_STALE"
    assert session.flushes == 2


@pytest.mark.asyncio
async def test_record_evaluation_reuses_same_plan_and_decision_payload() -> None:
    workspace_id, account_id = uuid4(), uuid4()
    plan = EvaluationPlanFixture(
        workspace_id=str(workspace_id),
        account_id=str(account_id),
        plan_hash="",
        symbol="BTCUSDT",
        notional=Decimal("500"),
        evidence=("feature-1",),
    )
    plan = replace(plan, plan_hash=calculate_plan_hash(plan))
    risk_decision = replace(
        decision(workspace_id=workspace_id, account_id=account_id, plan_hash=plan.plan_hash),
        snapshot_id="snapshot-1",
    )
    first_session = ReservationSession([SimpleNamespace(id=account_id), None, None])
    first_records = await RiskReservationRepository(first_session).record_evaluation(
        plan=plan, decision=risk_decision
    )  # type: ignore[arg-type]
    saved_plan, saved_decision = first_session.added
    second_session = ReservationSession(
        [SimpleNamespace(id=account_id), saved_plan, saved_decision]
    )

    repeated = await RiskReservationRepository(second_session).record_evaluation(
        plan=plan, decision=risk_decision
    )  # type: ignore[arg-type]

    assert repeated == first_records
    assert not second_session.added
    assert second_session.flushes == 0


@pytest.mark.asyncio
async def test_reserve_returns_existing_reservation_for_same_key_and_plan() -> None:
    workspace_id, account_id, reservation_id, decision_id = uuid4(), uuid4(), uuid4(), uuid4()
    risk_decision = decision(
        workspace_id=workspace_id,
        account_id=account_id,
        candidate_expires_at=NOW,
    )
    existing = SimpleNamespace(
        id=reservation_id,
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="intent-1",
        plan_hash="plan-hash-a",
        symbol="BTCUSDT",
        reserved_risk=Decimal("60"),
        reserved_notional=Decimal("500"),
        reserved_initial_margin=Decimal("100"),
        risk_decision_id=decision_id,
        status="UNKNOWN",
        resolution_reason=None,
    )
    session = ReservationSession(
        [
            SimpleNamespace(id=account_id),
            persisted_decision(decision_id, risk_decision),
            existing,
        ]
    )
    result = await RiskReservationRepository(session, clock=lambda: NOW).reserve(
        risk_decision,
        idempotency_key="intent-1",
        risk_decision_id=decision_id,
    )  # type: ignore[arg-type]

    assert result.reservation_id == reservation_id
    assert result.status is ReservationStatus.UNKNOWN
    assert len(session.statements) == 6
    assert not session.added


@pytest.mark.asyncio
async def test_reserve_rejects_duplicate_key_with_different_plan() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(workspace_id=workspace_id, account_id=account_id)
    existing = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="intent-1",
        plan_hash="another-plan",
        symbol="BTCUSDT",
        reserved_risk=Decimal("60"),
        reserved_notional=Decimal("500"),
        status="RESERVED",
        resolution_reason=None,
        reserved_initial_margin=Decimal("100"),
        risk_decision_id=decision_id,
    )
    session = ReservationSession(
        [
            SimpleNamespace(id=account_id),
            persisted_decision(decision_id, risk_decision),
            existing,
        ]
    )

    with pytest.raises(ReservationConflict, match="idempotency"):
        await RiskReservationRepository(session).reserve(
            risk_decision,
            idempotency_key="intent-1",
            risk_decision_id=decision_id,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reserve_fails_closed_when_decision_pending_count_is_stale() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(workspace_id=workspace_id, account_id=account_id)
    session = ReservationSession(
        [SimpleNamespace(id=account_id), persisted_decision(decision_id, risk_decision), None],
        (Decimal("60"), 1, Decimal("500"), Decimal("500"), Decimal("100")),
    )

    with pytest.raises(ReservationConflict, match="Snapshot"):
        await RiskReservationRepository(session).reserve(
            risk_decision,
            idempotency_key="intent-2",
            risk_decision_id=decision_id,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reserve_enforces_aggregate_cap_after_locking_account() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(
        workspace_id=workspace_id,
        account_id=account_id,
        open_risk="30",
        pending_risk="10",
        pending_count=1,
        ceiling="100",
    )
    session = ReservationSession(
        [SimpleNamespace(id=account_id), persisted_decision(decision_id, risk_decision), None],
        (Decimal("40"), 1, Decimal("200"), Decimal("200"), Decimal("0")),
    )

    with pytest.raises(ReservationConflict, match="Aggregate"):
        await RiskReservationRepository(session).reserve(
            risk_decision,
            idempotency_key="intent-2",
            risk_decision_id=decision_id,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reserve_replaces_snapshot_pending_risk_without_double_counting() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(
        workspace_id=workspace_id,
        account_id=account_id,
        risk="60",
        open_risk="30",
        pending_risk="20",
        pending_count=1,
        ceiling="100",
    )
    session = ReservationSession(
        [SimpleNamespace(id=account_id), persisted_decision(decision_id, risk_decision), None],
        (Decimal("20"), 1, Decimal("20"), Decimal("20"), Decimal("0")),
    )
    repository = RiskReservationRepository(session)  # type: ignore[arg-type]

    result = await repository.reserve(
        risk_decision,
        idempotency_key="intent-after-snapshot",
        risk_decision_id=decision_id,
    )

    # Tổng tại thời điểm reserve: 30 - 20 + 20 + 60 = 90.
    assert result.status is ReservationStatus.RESERVED


@pytest.mark.parametrize(
    ("aggregate", "decision_values", "expected"),
    [
        (
            (Decimal("0"), 0, Decimal("9600"), Decimal("100"), Decimal("0")),
            {"open_notional": "100", "symbol_open_notional": "100", "notional": "500"},
            "Tổng notional",
        ),
        (
            (Decimal("0"), 0, Decimal("100"), Decimal("4800"), Decimal("0")),
            {"open_notional": "100", "symbol_open_notional": "100", "notional": "500"},
            "Notional tập trung",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reserve_rechecks_locked_account_notional_and_symbol_caps(
    aggregate, decision_values: dict[str, str], expected: str
) -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(workspace_id=workspace_id, account_id=account_id, **decision_values)
    session = ReservationSession(
        [SimpleNamespace(id=account_id), persisted_decision(decision_id, risk_decision), None],
        aggregate,
    )

    with pytest.raises(ReservationConflict, match=expected):
        await RiskReservationRepository(session).reserve(
            risk_decision,
            idempotency_key="intent-capped",
            risk_decision_id=decision_id,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reserve_rechecks_initial_margin_from_locked_database_aggregate() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(
        workspace_id=workspace_id,
        account_id=account_id,
        initial_margin="60",
        pending_margin="50",
        available_margin="100",
        pending_count=1,
    )
    session = ReservationSession(
        [SimpleNamespace(id=account_id), persisted_decision(decision_id, risk_decision), None],
        (Decimal("0"), 1, Decimal("0"), Decimal("0"), Decimal("50")),
    )

    with pytest.raises(ReservationConflict, match="Initial margin"):
        await RiskReservationRepository(session).reserve(
            risk_decision,
            idempotency_key="intent-margin-capped",
            risk_decision_id=decision_id,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reserve_requires_a_persisted_approved_matching_decision() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(workspace_id=workspace_id, account_id=account_id)
    session = ReservationSession([SimpleNamespace(id=account_id), None])

    with pytest.raises(ReservationConflict, match="decision đã lưu"):
        await RiskReservationRepository(session).reserve(
            risk_decision,
            idempotency_key="intent-without-decision",
            risk_decision_id=decision_id,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reserve_rejects_expired_candidate_at_exact_boundary_with_injected_clock() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(
        workspace_id=workspace_id,
        account_id=account_id,
        candidate_expires_at=NOW,
    )
    session = ReservationSession(
        [SimpleNamespace(id=account_id), persisted_decision(decision_id, risk_decision), None]
    )
    repository = RiskReservationRepository(session, clock=lambda: NOW)  # type: ignore[arg-type]

    with pytest.raises(ReservationConflict, match="hết hạn"):
        await repository.reserve(
            risk_decision,
            idempotency_key="intent-expired-at-reserve",
            risk_decision_id=decision_id,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reserve_allows_candidate_before_expiry_with_injected_clock() -> None:
    workspace_id, account_id, decision_id = uuid4(), uuid4(), uuid4()
    risk_decision = decision(
        workspace_id=workspace_id,
        account_id=account_id,
        candidate_expires_at=NOW,
    )
    session = ReservationSession(
        [SimpleNamespace(id=account_id), persisted_decision(decision_id, risk_decision), None]
    )
    repository = RiskReservationRepository(session, clock=lambda: NOW - timedelta(microseconds=1))  # type: ignore[arg-type]

    result = await repository.reserve(
        risk_decision,
        idempotency_key="intent-before-expiry",
        risk_decision_id=decision_id,
    )

    assert result.status is ReservationStatus.RESERVED


@pytest.mark.asyncio
async def test_expire_due_releases_only_unsubmitted_reserved_rows_and_persists_reason() -> None:
    workspace_id, account_id, reservation_id = uuid4(), uuid4(), uuid4()
    expired = SimpleNamespace(
        id=reservation_id,
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="expired-intent",
        plan_hash="plan-hash-a",
        risk_decision_id=uuid4(),
        symbol="BTCUSDT",
        reserved_risk=Decimal("20"),
        reserved_notional=Decimal("200"),
        reserved_initial_margin=Decimal("40"),
        expires_at=NOW,
        status="RESERVED",
        resolution_reason=None,
    )

    class ExpirySession(ReservationSession):
        async def scalars(self, statement: Any) -> SimpleNamespace:
            self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return SimpleNamespace(all=lambda: [expired])

    session = ExpirySession([SimpleNamespace(id=account_id)])
    repository = RiskReservationRepository(session, clock=lambda: NOW)  # type: ignore[arg-type]

    count = await repository.expire_due(workspace_id=workspace_id, account_id=account_id)

    assert count == 1
    assert expired.status == ReservationStatus.RELEASED.value
    assert expired.resolution_reason == ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT.value
    assert session.flushes == 1
    assert "expires_at <=" in session.statements[-1]
    assert "NOT (EXISTS" in session.statements[-1]


@pytest.mark.asyncio
async def test_release_reserved_requires_confirmed_not_submitted_order() -> None:
    workspace_id, account_id, reservation_id = uuid4(), uuid4(), uuid4()
    row = SimpleNamespace(
        id=reservation_id,
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="intent-expired",
        plan_hash="plan-hash-a",
        symbol="BTCUSDT",
        reserved_risk=Decimal("60"),
        reserved_notional=Decimal("500"),
        status="RESERVED",
        resolution_reason=None,
        reserved_initial_margin=Decimal("100"),
        risk_decision_id=uuid4(),
    )
    session = ReservationSession(
        [SimpleNamespace(id=account_id), row, SimpleNamespace(id=account_id), row]
    )
    repository = RiskReservationRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ReservationConflict, match="đã được submit"):
        await repository.release_before_submit(
            workspace_id=workspace_id,
            account_id=account_id,
            reservation_id=reservation_id,
            reason=ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT,
            order_was_submitted=True,
        )

    released = await repository.release_before_submit(
        workspace_id=workspace_id,
        account_id=account_id,
        reservation_id=reservation_id,
        reason=ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT,
        order_was_submitted=False,
    )

    assert released.status is ReservationStatus.RELEASED
    assert released.resolution_reason is ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT
    assert row.resolution_reason == ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT.value
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_unknown_remains_held_and_reconcile_absent_releases_it() -> None:
    workspace_id, account_id, reservation_id = uuid4(), uuid4(), uuid4()
    unknown_row = SimpleNamespace(
        id=reservation_id,
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="intent-1",
        plan_hash="plan-hash-a",
        reserved_risk=Decimal("60"),
        status="UNKNOWN",
        symbol="BTCUSDT",
        reserved_notional=Decimal("500"),
        resolution_reason=None,
        reserved_initial_margin=Decimal("100"),
        risk_decision_id=uuid4(),
    )
    session = ReservationSession(
        [SimpleNamespace(id=account_id), unknown_row, SimpleNamespace(id=account_id), unknown_row]
    )
    repository = RiskReservationRepository(session)  # type: ignore[arg-type]

    unresolved = await repository.reconcile(
        workspace_id=workspace_id,
        account_id=account_id,
        reservation_id=reservation_id,
        outcome=ReservationOutcome.UNRESOLVED,
    )

    assert unresolved.status is ReservationStatus.UNKNOWN
    released = await repository.reconcile(
        workspace_id=workspace_id,
        account_id=account_id,
        reservation_id=reservation_id,
        outcome=ReservationOutcome.ORDER_ABSENT,
    )

    assert released.status is ReservationStatus.RELEASED
    assert unknown_row.status == ReservationStatus.RELEASED.value
    assert unknown_row.resolution_reason == "ORDER_ABSENT"
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_transfer_requires_same_transaction_open_risk_persistence_callback() -> None:
    workspace_id, account_id, reservation_id = uuid4(), uuid4(), uuid4()
    row = SimpleNamespace(
        id=reservation_id,
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="intent-1",
        plan_hash="plan-hash-a",
        reserved_risk=Decimal("60"),
        status="UNKNOWN",
        symbol="BTCUSDT",
        reserved_notional=Decimal("500"),
        resolution_reason=None,
        reserved_initial_margin=Decimal("100"),
        risk_decision_id=uuid4(),
    )
    session = ReservationSession(
        [SimpleNamespace(id=account_id), row, SimpleNamespace(id=account_id), row]
    )
    repository = RiskReservationRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ReservationConflict, match="cùng transaction"):
        await repository.reconcile(
            workspace_id=workspace_id,
            account_id=account_id,
            reservation_id=reservation_id,
            outcome=ReservationOutcome.OPEN_RISK_RECORDED,
        )

    persisted: list[bool] = []

    async def persist_open_risk() -> None:
        persisted.append(True)

    transferred = await repository.reconcile(
        workspace_id=workspace_id,
        account_id=account_id,
        reservation_id=reservation_id,
        outcome=ReservationOutcome.OPEN_RISK_RECORDED,
        persist_open_risk=persist_open_risk,
    )

    assert persisted == [True]
    assert transferred.status is ReservationStatus.TRANSFERRED
    assert row.status == ReservationStatus.TRANSFERRED.value


@pytest.mark.asyncio
async def test_terminal_reconcile_is_idempotent_only_for_matching_outcome() -> None:
    workspace_id, account_id, reservation_id = uuid4(), uuid4(), uuid4()
    released_row = SimpleNamespace(
        id=reservation_id,
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="intent-terminal",
        plan_hash="plan-hash-a",
        reserved_risk=Decimal("60"),
        status="RELEASED",
        symbol="BTCUSDT",
        reserved_notional=Decimal("500"),
        reserved_initial_margin=Decimal("100"),
        risk_decision_id=uuid4(),
        resolution_reason="ORDER_ABSENT",
    )
    repeat_session = ReservationSession([SimpleNamespace(id=account_id), released_row])
    same = await RiskReservationRepository(repeat_session).reconcile(
        workspace_id=workspace_id,
        account_id=account_id,
        reservation_id=reservation_id,
        outcome=ReservationOutcome.ORDER_ABSENT,
    )  # type: ignore[arg-type]
    assert same.status is ReservationStatus.RELEASED

    conflict_session = ReservationSession([SimpleNamespace(id=account_id), released_row])
    with pytest.raises(ReservationConflict, match="mâu thuẫn"):
        await RiskReservationRepository(conflict_session).reconcile(
            workspace_id=workspace_id,
            account_id=account_id,
            reservation_id=reservation_id,
            outcome=ReservationOutcome.OPEN_RISK_RECORDED,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_release_rejects_submit_flag_even_when_row_is_already_released() -> None:
    workspace_id, account_id, reservation_id = uuid4(), uuid4(), uuid4()
    released_row = SimpleNamespace(
        id=reservation_id,
        workspace_id=workspace_id,
        account_id=account_id,
        idempotency_key="intent-released",
        plan_hash="plan-hash-a",
        reserved_risk=Decimal("60"),
        status="RELEASED",
        symbol="BTCUSDT",
        reserved_notional=Decimal("500"),
        reserved_initial_margin=Decimal("100"),
        risk_decision_id=uuid4(),
        resolution_reason="CANCELLED_BEFORE_SUBMIT",
    )
    session = ReservationSession([SimpleNamespace(id=account_id), released_row])

    with pytest.raises(ReservationConflict, match="đã được submit"):
        await RiskReservationRepository(session).release_before_submit(
            workspace_id=workspace_id,
            account_id=account_id,
            reservation_id=reservation_id,
            reason=ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT,
            order_was_submitted=True,
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_workspace_risk_aggregate_queries_and_maps_dto() -> None:
    workspace_id = uuid4()
    as_of = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

    class AggregateSession:
        def __init__(self) -> None:
            self.executed_statements = []

        async def execute(self, statement, params=None):
            self.executed_statements.append((statement, params))

            class _Result:
                def one(self):
                    return (
                        workspace_id,
                        Decimal("25000"),
                        Decimal("1500.50"),
                        Decimal("500.25"),
                        3,
                        False,
                        False,
                    )

            return _Result()

    session = AggregateSession()
    repo = RiskReservationRepository(session)  # type: ignore[arg-type]

    agg = await repo.get_workspace_risk_aggregate(
        workspace_id=workspace_id,
        as_of=as_of,
        max_mark_age=timedelta(seconds=60),
        mark_policy=MarkPolicy("BINANCE_USDM", ("1m", "1h")),
    )

    assert agg.workspace_id == workspace_id
    assert agg.total_equity_usdt == Decimal("25000")
    assert agg.total_open_risk_usdt == Decimal("1500.50")
    assert agg.total_pending_risk_usdt == Decimal("500.25")
    assert agg.active_bot_count == 3
    assert agg.has_unknown_exposure is False
    assert agg.has_unknown_equity is False
    assert len(session.executed_statements) == 1
    stmt, params = session.executed_statements[0]
    assert "get_workspace_risk_equity_aggregate" in str(stmt)
    assert params == {
        "workspace_id": workspace_id,
        "as_of": as_of,
        "max_mark_age_seconds": 60,
        "mark_source": "BINANCE_USDM",
        "mark_timeframes": ["1m", "1h"],
    }


@pytest.mark.asyncio
async def test_get_workspace_risk_aggregate_rejects_naive_as_of() -> None:
    session = ReservationSession([])
    repo = RiskReservationRepository(session)  # type: ignore[arg-type]
    naive_as_of = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)

    with pytest.raises(ReservationConflict, match="as_of"):
        await repo.get_workspace_risk_aggregate(
            workspace_id=uuid4(),
            as_of=naive_as_of,
            max_mark_age=timedelta(seconds=60),
            mark_policy=MarkPolicy("BINANCE_USDM", ("1m",)),
        )
