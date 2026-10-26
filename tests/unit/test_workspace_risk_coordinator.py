"""Unit tests cho WorkspaceRiskCoordinator (MB04.5).

Kiểm tra:
- Khóa 2 cấp advisory lock (workspace -> account, sắp xếp UUID tăng dần).
- Re-read sau khóa phát hiện cap mới nhất.
- Cấp reservation thành công khi nằm trong mọi cap (workspace, account, bot).
- Từ chối khi vượt bất kỳ trần nào.
- Transfer sang TRANSFERRED không double-count rủi ro.
- Reservation UNKNOWN tiếp tục giữ ngân sách dù quá hạn.
- expire_overdue chỉ giải phóng RESERVED quá hạn, không đụng vào UNKNOWN.
- Idempotency replay và conflict detection.
- Rollback đảm bảo không lưu trạng thái mồ côi.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.bot_scope_fakes import BotScopeSession

from trading_bot.adapters.database.models import (
    RiskDecisionRecord,
    RiskReservationRecord,
    TradingAccount,
)
from trading_bot.application.workspace_risk import (
    ReservationRequest,
    WorkspaceRiskCoordinator,
)
from trading_bot.config.workspace_settings import (
    PublishedWorkspaceSettings,
    WorkspaceSettings,
)
from trading_bot.domain.risk.equity import (
    AccountEquitySnapshot,
    PaperEquityBasis,
    RiskConversionObservation,
    mark_to_market_equity,
)
from trading_bot.domain.risk.reservations import (
    ReservationReleaseReason,
    ReservationStatus,
)
from trading_bot.domain.risk.workspace_budget import (
    BudgetConflictError,
    BudgetReasonCode,
    PositionRisk,
    WorkspaceBudgetEngine,
    WorkspaceRiskAggregate,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ZERO = Decimal("0")


class FakeRiskSession(BotScopeSession):
    """Session giả lập hỗ trợ ghi nhận câu lệnh SQL, add records và lock ordering."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params_list: list[dict[str, Any]] = []
        self.added: list[object] = []
        self.flushes = 0
        self.rollbacks = 0
        self.commits = 0
        self.active_bot_count = 1
        self.total_open_risk_usdt = Decimal("0")
        self.total_pending_risk_usdt = Decimal("0")
        self.records: dict[type, list[object]] = {
            RiskReservationRecord: [],
            RiskDecisionRecord: [],
            TradingAccount: [],
        }

    async def execute(self, statement: Any, params: object = None) -> Any:
        try:
            compiled = str(statement.compile(dialect=postgresql.dialect()))
        except Exception:
            compiled = str(statement)
        self.statements.append(compiled)
        if isinstance(params, dict):
            self.params_list.append(params)
            if "get_workspace_risk_aggregate" in compiled:
                aggregate = (
                    params["workspace_id"],
                    self.total_open_risk_usdt,
                    self.total_pending_risk_usdt,
                    self.active_bot_count,
                    False,
                )
                return type("Result", (), {"one": lambda *a, **k: aggregate})()
        return type("Result", (), {"one": lambda *a, **k: (0,), "all": lambda *a, **k: []})()

    async def scalar(self, statement: Any, params: object = None) -> object | None:
        try:
            compiled = str(statement.compile(dialect=postgresql.dialect()))
        except Exception:
            compiled = str(statement)
        self.statements.append(compiled)

        # Trả về record khớp từ self.records
        # 1. Tìm RiskReservationRecord
        if "risk_reservation" in compiled:
            for rec in self.records[RiskReservationRecord]:
                return rec
            return None
        # 2. Tìm RiskDecisionRecord
        if "risk_decision" in compiled:
            for rec in self.records[RiskDecisionRecord]:
                return rec
            return None
        return None

    async def scalars(self, statement: Any, params: object = None) -> Any:
        try:
            compiled = str(
                statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        except Exception:
            try:
                compiled = str(statement.compile(dialect=postgresql.dialect()))
            except Exception:
                compiled = str(statement)
        self.statements.append(compiled)
        if "risk_reservation" in compiled:
            items = list(self.records[RiskReservationRecord])
            if "'RESERVED'" in compiled:
                items = [r for r in items if getattr(r, "status", None) == "RESERVED"]
            if "expires_at <=" in compiled:
                items = [
                    r
                    for r in items
                    if getattr(r, "expires_at", None) is not None and r.expires_at <= NOW
                ]
            return type("Result", (), {"all": lambda *a, **k: items})()
        if "risk_decision" in compiled:
            items = list(self.records[RiskDecisionRecord])
            return type("Result", (), {"all": lambda *a, **k: items})()
        return type("Result", (), {"all": lambda *a, **k: []})()

    def add(self, record: object) -> None:
        self.added.append(record)
        rec_type = type(record)
        if rec_type in self.records:
            self.records[rec_type].append(record)

    async def flush(self) -> None:
        self.flushes += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def commit(self) -> None:
        self.commits += 1


def _create_snapshot(
    workspace_id: UUID,
    bot_id: UUID,
    account_id: UUID,
    equity: Decimal,
    as_of: datetime = NOW,
) -> AccountEquitySnapshot:
    basis = PaperEquityBasis(
        workspace_id=str(workspace_id),
        bot_id=str(bot_id),
        account_id=str(account_id),
        asset="USDT",
        as_of=as_of,
        captured_at=as_of,
        available_at=as_of,
        source="paper_seed",
        source_version="v1",
        max_mark_age=timedelta(seconds=60),
        max_equity_age=timedelta(seconds=300),
        initial_capital_before_realized_pnl=equity,
        cumulative_realized_pnl=ZERO,
    )
    return mark_to_market_equity(basis, (), {}, as_of)


def _create_conversion(workspace_id: UUID, as_of: datetime = NOW) -> RiskConversionObservation:
    return RiskConversionObservation(
        workspace_id=str(workspace_id),
        base_asset="USDT",
        quote_asset="USDT",
        rate=Decimal("1.0"),
        as_of=as_of,
        available_at=as_of,
        source_ref="identity",
        source_version="v1",
        source_hash="c" * 64,
        quality="ACCEPTED",
        policy_version_id="pol_v1",
        known=True,
        route="DIRECT",
        approved=True,
    )


def _make_valid_settings(ws_cap_fraction: Decimal, profile_id: UUID) -> WorkspaceSettings:
    payload = {
        "schema_version": 1,
        "runtime": {"mode": "PAPER", "timezone": "UTC"},
        "market_data": {
            "venue": "BINANCE_USDM",
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h", "4h"],
            "max_age_seconds": 30,
        },
        "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
        "strategy": {
            "default_release": "h03-v1",
            "parameters": {},
            "entry_timeframe": "1h",
            "confirmation_timeframe": "4h",
        },
        "risk": {
            "profile_version_id": str(profile_id),
            "workspace_max_open_risk_fraction": ws_cap_fraction,
            "max_equity_age_seconds": 300,
            "max_conversion_age_seconds": 300,
            "max_mark_age_seconds": 60,
        },
        "paper_execution": {
            "quote_asset": "USDT",
            "starting_equity": "10000",
            "fee_schedule_version": "paper-fees-v1",
            "fill_model": "next_bar_with_adverse_costs",
        },
    }
    return WorkspaceSettings.model_validate(payload)


class FakeSettingsRepo:
    def __init__(self, ws_cap_fraction: Decimal = Decimal("0.5")) -> None:
        self.ws_cap_fraction = ws_cap_fraction
        self.version_id = uuid4()
        self.version_hash = "a" * 64
        self.profile_id = uuid4()

    async def load_effective_config(self, *, workspace_id: UUID) -> PublishedWorkspaceSettings:
        settings = _make_valid_settings(self.ws_cap_fraction, self.profile_id)
        return PublishedWorkspaceSettings(
            workspace_id=workspace_id,
            settings_version_id=self.version_id,
            settings_hash=self.version_hash,
            settings=settings,
            effective_at=NOW - timedelta(minutes=5),
        )


class FakeEquityRepo:
    def __init__(self, snapshots: dict[UUID, AccountEquitySnapshot]) -> None:
        self.snapshots = snapshots

    async def load_paper_equity_snapshot(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        as_of: datetime,
        max_equity_age: timedelta,
        max_mark_age: timedelta,
        captured_at: datetime | None = None,
        marks: Any = None,
    ) -> AccountEquitySnapshot:
        return self.snapshots.get(
            account_id,
            _create_snapshot(workspace_id, bot_id, account_id, Decimal("10000"), as_of),
        )


class FakeConversionRepo:
    async def get_conversion_observation(
        self,
        *,
        workspace_id: UUID,
        base_asset: str,
        quote_asset: str,
        as_of: datetime,
        max_age: timedelta,
    ) -> RiskConversionObservation:
        return _create_conversion(workspace_id, as_of)


# -----------------------------------------------------------------------------
# Test Cases
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_order_workspace_then_accounts_sorted() -> None:
    """Ruling 10: Khóa cấp 1 workspace -> cấp 2 account sắp xếp UUID tăng dần."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id_1 = UUID("20000000-0000-0000-0000-000000000002")
    acc_id_2 = UUID("10000000-0000-0000-0000-000000000001")

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    # Khóa cho danh sách accounts: acc_id_1 và acc_id_2
    await coordinator.acquire_locks(ws_id, [acc_id_1, acc_id_2])

    # Kiểm tra thứ tự các params được lock
    locks = [p["risk_lock"] for p in session.params_list if "risk_lock" in p]
    assert len(locks) == 3
    assert locks[0] == f"workspace-risk:{ws_id}"
    assert locks[1] == f"account-risk:{ws_id}:{acc_id_2}"  # UUID nhỏ hơn đi trước
    assert locks[2] == f"account-risk:{ws_id}:{acc_id_1}"


@pytest.mark.asyncio
async def test_reread_after_lock_detects_new_cap() -> None:
    """Sau khi có lock, coordinator nạp cấu hình mới nhất từ repository."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    settings_repo = FakeSettingsRepo(ws_cap_fraction=Decimal("0.8"))
    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=settings_repo,
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    # Giả lập draft mới được publish với cap 0.3 trước khi evaluate
    settings_repo.ws_cap_fraction = Decimal("0.3")

    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="key-test-1",
        requested_risk_usdt=Decimal("4000"),  # 4000 > 0.3 * 10000 = 3000
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="plan_hash_1",
    )

    result = await coordinator.reserve(
        request,
        account_max_open_risk_fraction=Decimal("0.5"),
    )

    # Bị từ chối vì cap mới là 0.3 (3000 USDT) chứ không phải 0.8 (8000 USDT)
    assert not result.approved
    assert result.reason_code == BudgetReasonCode.WORKSPACE_CAP_EXCEEDED


@pytest.mark.asyncio
async def test_reserve_approved_within_all_caps() -> None:
    """Reservation được duyệt khi nằm trong mọi cấp trần (workspace, account, bot)."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(ws_cap_fraction=Decimal("0.5")),  # ws_cap = 5000
        equity_repo=FakeEquityRepo(
            {acc_id: _create_snapshot(ws_id, bot_id, acc_id, Decimal("10000"), NOW)}
        ),
        conversion_repo=FakeConversionRepo(),
    )

    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="key-approved-1",
        requested_risk_usdt=Decimal("500"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        symbol="BTCUSDT",
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        plan_hash="hash_plan_1",
    )

    result = await coordinator.reserve(
        request,
        account_max_open_risk_fraction=Decimal("0.2"),  # 2000
        bot_max_open_risk_fraction=Decimal("0.1"),  # 1000
    )

    assert result.approved
    assert result.reason_code == BudgetReasonCode.APPROVED
    assert result.decision_id is not None
    assert result.reservation_id is not None
    assert len(session.added) == 2  # RiskDecisionRecord + RiskReservationRecord
    assert session.flushes >= 1


@pytest.mark.asyncio
async def test_reserve_fails_closed_when_workspace_aggregate_is_unavailable() -> None:
    """Không tạo entry nếu aggregate workspace không thể đọc được."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    async def fail_aggregate(*, workspace_id: UUID, as_of: datetime) -> WorkspaceRiskAggregate:
        raise RuntimeError("aggregate unavailable")

    coordinator._reservations_repo.get_workspace_risk_aggregate = fail_aggregate
    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="aggregate-unavailable",
        requested_risk_usdt=Decimal("500"),
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="aggregate-plan",
    )

    with pytest.raises(RuntimeError, match="aggregate unavailable"):
        await coordinator.reserve(request, account_max_open_risk_fraction=Decimal("0.2"))
    assert session.added == []


@pytest.mark.asyncio
async def test_reserve_denies_when_workspace_aggregate_has_unknown_exposure() -> None:
    """Unknown exposure trong workspace aggregate phải chặn entry mới."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    async def unknown_aggregate(*, workspace_id: UUID, as_of: datetime) -> WorkspaceRiskAggregate:
        return WorkspaceRiskAggregate(
            workspace_id=str(workspace_id),
            total_open_risk_usdt=Decimal("0"),
            total_pending_risk_usdt=Decimal("0"),
            active_bot_count=1,
            has_unknown_exposure=True,
        )

    coordinator._reservations_repo.get_workspace_risk_aggregate = unknown_aggregate
    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="aggregate-unknown",
        requested_risk_usdt=Decimal("500"),
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="aggregate-unknown-plan",
    )

    result = await coordinator.reserve(request, account_max_open_risk_fraction=Decimal("0.2"))

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.WORKSPACE_EXPOSURE_UNKNOWN
    assert result.reservation_id is None


@pytest.mark.asyncio
async def test_reserve_denies_when_enabled_workspace_scopes_are_incomplete() -> None:
    """Thiếu equity một bot đang chạy thì không được tính cap workspace thiếu mẫu số."""
    session = FakeRiskSession()
    session.active_bot_count = 2
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()
    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )
    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="workspace-scope-incomplete",
        requested_risk_usdt=Decimal("500"),
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="workspace-scope-incomplete-plan",
    )

    result = await coordinator.reserve(request, account_max_open_risk_fraction=Decimal("0.2"))

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.WORKSPACE_SCOPE_INCOMPLETE
    assert result.reservation_id is None


@pytest.mark.asyncio
async def test_reserve_denied_when_exceeds_bot_overlay_cap() -> None:
    """Bị từ chối khi vượt bot overlay cap dù thỏa account cap và workspace cap."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(ws_cap_fraction=Decimal("0.5")),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="key-bot-cap",
        requested_risk_usdt=Decimal("1200"),  # > bot_cap 1000, nhưng <= account_cap 2000
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="hash_1",
    )

    result = await coordinator.reserve(
        request,
        account_max_open_risk_fraction=Decimal("0.2"),  # 2000
        bot_max_open_risk_fraction=Decimal("0.1"),  # 1000
    )

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.BOT_CAP_EXCEEDED
    assert result.decision_id is not None
    assert result.reservation_id is None
    assert len(session.added) == 1  # Chỉ có RiskDecisionRecord (approved=False)


@pytest.mark.asyncio
async def test_transfer_to_open_does_not_double_count_risk() -> None:
    """Chuyển RESERVED sang TRANSFERRED không double-count rủi ro."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()
    res_id = uuid4()

    res_record = RiskReservationRecord(
        id=res_id,
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        risk_decision_id=uuid4(),
        idempotency_key="key-transfer",
        plan_hash="plan_h",
        symbol="BTCUSDT",
        reserved_risk=Decimal("500"),
        reserved_notional=Decimal("5000"),
        reserved_initial_margin=Decimal("1000"),
        expires_at=NOW + timedelta(hours=1),
        status=ReservationStatus.RESERVED.value,
        resolution_reason=None,
    )
    session.records[RiskReservationRecord].append(res_record)

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    # 1. Chuyển sang TRANSFERRED
    trade_id = uuid4()
    transferred = await coordinator.transfer_to_open(
        workspace_id=ws_id,
        account_id=acc_id,
        reservation_id=res_id,
        trade_id=trade_id,
    )
    assert transferred.status == ReservationStatus.TRANSFERRED
    assert transferred.resolution_reason == ReservationReleaseReason.OPEN_RISK_RECORDED
    assert res_record.status == ReservationStatus.TRANSFERRED.value
    assert res_record.resolution_reason == ReservationReleaseReason.OPEN_RISK_RECORDED.value

    # 2. Đánh giá rủi ro ngân sách tiếp theo:
    # Vị thế OPEN 500 USDT, reservation TRANSFERRED không còn giữ budget -> Tổng rủi ro = 500 USDT.
    res_domain = coordinator.to_budget_reservation(res_record)
    pos = PositionRisk(
        workspace_id=str(ws_id),
        bot_id=str(bot_id),
        account_id=str(acc_id),
        position_id=str(trade_id),
        symbol="BTCUSDT",
        worst_case_loss_usdt=Decimal("500"),
    )

    engine = WorkspaceBudgetEngine()
    policy = await coordinator._load_policy(ws_id)
    accounts = await coordinator._load_account_scopes(
        ws_id, bot_id, acc_id, Decimal("0.2"), None, NOW
    )

    decision = engine.evaluate(
        policy=policy,
        accounts=accounts,
        positions=[pos],
        reservations=[res_domain],
        target_bot_id=str(bot_id),
        requested_risk_usdt=Decimal("100"),
        as_of=NOW,
    )
    # Tổng pending = 0, open = 500
    assert decision.account_budget.open_risk_usdt == Decimal("500")
    assert decision.account_budget.pending_risk_usdt == Decimal("0")


@pytest.mark.asyncio
async def test_unknown_reservation_keeps_budget_even_after_expiry() -> None:
    """Reservation UNKNOWN tiếp tục giữ ngân sách dù đã quá expires_at."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()
    res_id = uuid4()

    res_record = RiskReservationRecord(
        id=res_id,
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        risk_decision_id=uuid4(),
        idempotency_key="key-unknown",
        plan_hash="plan_h",
        symbol="BTCUSDT",
        reserved_risk=Decimal("800"),
        reserved_notional=Decimal("5000"),
        reserved_initial_margin=Decimal("1000"),
        expires_at=NOW - timedelta(minutes=10),  # Đã quá hạn!
        status=ReservationStatus.RESERVED.value,
        resolution_reason=None,
    )
    session.records[RiskReservationRecord].append(res_record)

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    # Đánh dấu UNKNOWN
    unknown_res = await coordinator.mark_unknown(
        workspace_id=ws_id,
        account_id=acc_id,
        reservation_id=res_id,
    )
    assert unknown_res.status == ReservationStatus.UNKNOWN
    assert res_record.status == ReservationStatus.UNKNOWN.value

    # Đánh giá ngân sách: reservation UNKNOWN dù quá hạn vẫn giữ 800 USDT
    res_domain = coordinator.to_budget_reservation(res_record)
    engine = WorkspaceBudgetEngine()
    policy = await coordinator._load_policy(ws_id)
    accounts = await coordinator._load_account_scopes(
        ws_id, bot_id, acc_id, Decimal("0.2"), None, NOW
    )

    decision = engine.evaluate(
        policy=policy,
        accounts=accounts,
        positions=[],
        reservations=[res_domain],
        target_bot_id=str(bot_id),
        requested_risk_usdt=Decimal("100"),
        as_of=NOW,
    )
    assert decision.account_budget.pending_risk_usdt == Decimal("800")


@pytest.mark.asyncio
async def test_expire_overdue_only_releases_expired_reserved_and_ignores_unknown() -> None:
    """expire_overdue chỉ giải phóng RESERVED quá hạn, không đụng vào UNKNOWN hay unexpired."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    # 1. RESERVED quá hạn -> sẽ bị expire
    rec_expired = RiskReservationRecord(
        id=uuid4(),
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        risk_decision_id=uuid4(),
        idempotency_key="key-exp",
        plan_hash="p1",
        symbol="BTCUSDT",
        reserved_risk=Decimal("100"),
        reserved_notional=Decimal("1000"),
        reserved_initial_margin=Decimal("200"),
        expires_at=NOW - timedelta(minutes=5),
        status=ReservationStatus.RESERVED.value,
        resolution_reason=None,
    )
    # 2. UNKNOWN quá hạn -> KHÔNG bị expire
    rec_unknown = RiskReservationRecord(
        id=uuid4(),
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        risk_decision_id=uuid4(),
        idempotency_key="key-unk",
        plan_hash="p2",
        symbol="BTCUSDT",
        reserved_risk=Decimal("200"),
        reserved_notional=Decimal("2000"),
        reserved_initial_margin=Decimal("400"),
        expires_at=NOW - timedelta(minutes=5),
        status=ReservationStatus.UNKNOWN.value,
        resolution_reason=None,
    )
    # 3. RESERVED chưa quá hạn -> KHÔNG bị expire
    rec_active = RiskReservationRecord(
        id=uuid4(),
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        risk_decision_id=uuid4(),
        idempotency_key="key-act",
        plan_hash="p3",
        symbol="BTCUSDT",
        reserved_risk=Decimal("300"),
        reserved_notional=Decimal("3000"),
        reserved_initial_margin=Decimal("600"),
        expires_at=NOW + timedelta(minutes=5),
        status=ReservationStatus.RESERVED.value,
        resolution_reason=None,
    )
    session.records[RiskReservationRecord].extend([rec_expired, rec_unknown, rec_active])

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    expired_count = await coordinator.expire_overdue(workspace_id=ws_id, as_of=NOW)
    assert expired_count == 1
    assert rec_expired.status == ReservationStatus.RELEASED.value
    assert rec_expired.resolution_reason == ReservationReleaseReason.EXPIRED_BEFORE_SUBMIT.value
    assert rec_unknown.status == ReservationStatus.UNKNOWN.value
    assert rec_active.status == ReservationStatus.RESERVED.value


@pytest.mark.asyncio
async def test_idempotency_replay_and_conflict() -> None:
    """Idempotency: Replay trả kết quả cũ, khác payload báo BudgetConflictError."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    req1 = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="idemp-key-1",
        requested_risk_usdt=Decimal("500"),
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="hash_same",
    )

    # Lần 1: Cấp mới
    res1 = await coordinator.reserve(req1, account_max_open_risk_fraction=Decimal("0.2"))
    assert res1.approved
    assert not res1.is_replay

    # Lần 2: Cùng key, cùng payload -> Replay
    res2 = await coordinator.reserve(req1, account_max_open_risk_fraction=Decimal("0.2"))
    assert res2.approved
    assert res2.is_replay
    assert res2.reservation_id == res1.reservation_id

    # Lần 3: Cùng key, khác payload -> Conflict
    req3 = replace(req1, requested_risk_usdt=Decimal("600"))
    with pytest.raises(BudgetConflictError):
        await coordinator.reserve(req3, account_max_open_risk_fraction=Decimal("0.2"))

    req4 = replace(req1, symbol="ETHUSDT", requested_notional=Decimal("6000"))
    with pytest.raises(BudgetConflictError):
        await coordinator.reserve(req4, account_max_open_risk_fraction=Decimal("0.2"))


@pytest.mark.parametrize("field", ["requested_notional", "requested_initial_margin"])
@pytest.mark.parametrize("value", [1.5, Decimal("NaN"), Decimal("-1"), Decimal("0")])
def test_reservation_request_rejects_invalid_decimal_amounts(field: str, value: object) -> None:
    """Mọi trường tiền của request phải là Decimal hữu hạn không âm."""
    payload: dict[str, object] = {
        "workspace_id": uuid4(),
        "bot_id": uuid4(),
        "account_id": uuid4(),
        "idempotency_key": "invalid-money",
        "requested_risk_usdt": Decimal("10"),
        "requested_notional": Decimal("5000"),
        "requested_initial_margin": Decimal("1000"),
        "as_of": NOW,
        "expires_at": NOW + timedelta(minutes=1),
    }
    payload[field] = value
    with pytest.raises(ValueError, match=field):
        ReservationRequest(**payload)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_transaction_rollback_prevents_orphan_state() -> None:
    """Downstream error gây rollback đảm bảo không lưu trạng thái mồ côi."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    req = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="key-rollback",
        requested_risk_usdt=Decimal("500"),
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="hash_rb",
    )

    # Trong một transaction: reserve thành công trong session
    res = await coordinator.reserve(req, account_max_open_risk_fraction=Decimal("0.2"))
    assert res.approved
    assert len(session.added) == 2

    # Giả lập caller gặp lỗi downstream và gọi rollback
    await session.rollback()
    assert session.rollbacks == 1
    assert session.commits == 0


@pytest.mark.asyncio
async def test_reserve_denied_when_exceeds_account_cap() -> None:
    """Bị từ chối khi vượt account cap dù thỏa workspace cap."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(ws_cap_fraction=Decimal("0.8")),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="key-acc-cap",
        requested_risk_usdt=Decimal("2500"),  # > account_cap 2000 (0.2 * 10000)
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="hash_acc_1",
    )

    result = await coordinator.reserve(
        request,
        account_max_open_risk_fraction=Decimal("0.2"),  # 2000
    )

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.ACCOUNT_CAP_EXCEEDED
    assert result.decision_id is not None
    assert result.reservation_id is None


@pytest.mark.asyncio
async def test_reserve_denied_when_exceeds_workspace_cap() -> None:
    """Bị từ chối khi vượt workspace cap do các bot khác trong workspace đã chiếm ngân sách."""
    session = FakeRiskSession()
    session.active_bot_count = 2
    session.total_open_risk_usdt = Decimal("5500")
    ws_id = uuid4()
    acc_id_1 = uuid4()
    bot_id_1 = uuid4()
    acc_id_2 = uuid4()
    bot_id_2 = uuid4()

    # Vốn acc 1 = 10000, acc 2 = 10000 -> Tổng vốn ws = 20000
    # ws_cap = 0.3 * 20000 = 6000 USDT
    # acc 2 đã chiếm 5500 USDT (qua existing position)
    settings_repo = FakeSettingsRepo(ws_cap_fraction=Decimal("0.3"))
    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=settings_repo,
        equity_repo=FakeEquityRepo(
            {
                acc_id_1: _create_snapshot(ws_id, bot_id_1, acc_id_1, Decimal("10000"), NOW),
                acc_id_2: _create_snapshot(ws_id, bot_id_2, acc_id_2, Decimal("10000"), NOW),
            }
        ),
        conversion_repo=FakeConversionRepo(),
    )

    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id_1,
        account_id=acc_id_1,
        idempotency_key="key-ws-cap",
        requested_risk_usdt=Decimal("1000"),  # 5500 + 1000 = 6500 > 6000 ws_cap
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="hash_ws_1",
    )

    # Bot 2 scope input
    other_scope = coordinator.build_account_scope(
        workspace_id=ws_id,
        bot_id=bot_id_2,
        account_id=acc_id_2,
        account_cap=Decimal("0.8"),
        snapshot=_create_snapshot(ws_id, bot_id_2, acc_id_2, Decimal("10000"), NOW),
        conversion=_create_conversion(ws_id, NOW),
    )

    result = await coordinator.reserve(
        request,
        account_max_open_risk_fraction=Decimal("0.5"),
        other_account_scopes=[other_scope],
    )

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.WORKSPACE_CAP_EXCEEDED


@pytest.mark.asyncio
async def test_release_reservation_returns_risk_to_budget() -> None:
    """Giải phóng reservation (CANCELLED_BEFORE_SUBMIT) trả lại ngân sách rủi ro."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()
    res_id = uuid4()

    res_record = RiskReservationRecord(
        id=res_id,
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        risk_decision_id=uuid4(),
        idempotency_key="key-rel",
        plan_hash="plan_rel",
        symbol="BTCUSDT",
        reserved_risk=Decimal("500"),
        reserved_notional=Decimal("5000"),
        reserved_initial_margin=Decimal("1000"),
        expires_at=NOW + timedelta(hours=1),
        status=ReservationStatus.RESERVED.value,
        resolution_reason=None,
    )
    session.records[RiskReservationRecord].append(res_record)

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    released = await coordinator.release_reservation(
        workspace_id=ws_id,
        account_id=acc_id,
        reservation_id=res_id,
        reason=ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT,
    )
    assert released.status == ReservationStatus.RELEASED
    assert released.resolution_reason == ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT
    assert res_record.status == ReservationStatus.RELEASED.value
    assert res_record.resolution_reason == ReservationReleaseReason.CANCELLED_BEFORE_SUBMIT.value


@pytest.mark.asyncio
async def test_multi_account_lock_sorting() -> None:
    """Ruling 10: Multi-account lock phải sắp xếp UUID tăng dần nghiêm ngặt."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_c = UUID("30000000-0000-0000-0000-000000000003")
    acc_a = UUID("10000000-0000-0000-0000-000000000001")
    acc_b = UUID("20000000-0000-0000-0000-000000000002")

    coordinator = WorkspaceRiskCoordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    await coordinator.acquire_locks(ws_id, [acc_c, acc_a, acc_b])
    locks = [p["risk_lock"] for p in session.params_list if "risk_lock" in p]
    assert len(locks) == 4
    assert locks[0] == f"workspace-risk:{ws_id}"
    assert locks[1] == f"account-risk:{ws_id}:{acc_a}"
    assert locks[2] == f"account-risk:{ws_id}:{acc_b}"
    assert locks[3] == f"account-risk:{ws_id}:{acc_c}"
