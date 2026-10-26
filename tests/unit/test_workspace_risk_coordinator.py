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
from trading_bot.application import workspace_risk as workspace_risk_module
from trading_bot.application.workspace_risk import (
    AuthoritativeCaps,
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
    ReservationConflict,
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


async def _async_false() -> bool:
    return False


async def _enabled_target(**_: Any) -> bool:
    return True


class FakeRiskSession(BotScopeSession):
    """Session giả lập hỗ trợ ghi nhận câu lệnh SQL, add records và lock ordering.

    Cap account/bot không còn được inject qua thuộc tính session: test truyền `caps_loader`
    tường minh vào constructor (xem `make_coordinator`). Đường authoritative thật được kiểm ở
    E2E `test_workspace_risk_real_paths.py`.
    """

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
        self.has_unknown_equity = False
        self.account_cap = Decimal("0.5")
        self.bot_cap: Decimal | None = None
        self.trade_rows: list[tuple[Any, ...]] = []
        self.on_workspace_lock: Any = None
        self.caps_reads: list[tuple[datetime, int]] = []
        self.records: dict[type, list[object]] = {
            RiskReservationRecord: [],
            RiskDecisionRecord: [],
            TradingAccount: [],
        }

    async def load_caps(
        self,
        request: ReservationRequest,
        published: PublishedWorkspaceSettings,
        read_at: datetime,
    ) -> AuthoritativeCaps:
        del request, published
        self.caps_reads.append((read_at, len(self.lock_keys())))
        return AuthoritativeCaps(self.account_cap, self.bot_cap)

    def lock_keys(self) -> list[str]:
        return [p["risk_lock"] for p in self.params_list if "risk_lock" in p]

    async def execute(self, statement: Any, params: object = None) -> Any:
        try:
            compiled = str(statement.compile(dialect=postgresql.dialect()))
        except Exception:
            compiled = str(statement)
        self.statements.append(compiled)
        if "FROM trade LEFT OUTER JOIN risk_reservation" in compiled:
            rows = list(self.trade_rows)
            return type("Result", (), {"all": lambda *a, **k: rows})()
        if isinstance(params, dict):
            self.params_list.append(params)
            lock = params.get("risk_lock")
            if isinstance(lock, str) and lock.startswith("workspace-risk:"):
                if self.on_workspace_lock is not None:
                    self.on_workspace_lock()
            if "get_workspace_risk_equity_aggregate" in compiled:
                aggregate = (
                    params["workspace_id"],
                    Decimal("10000"),
                    self.total_open_risk_usdt,
                    self.total_pending_risk_usdt,
                    self.active_bot_count,
                    False,
                    self.has_unknown_equity,
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
    def __init__(
        self,
        ws_cap_fraction: Decimal = Decimal("0.5"),
        session: FakeRiskSession | None = None,
    ) -> None:
        self.ws_cap_fraction = ws_cap_fraction
        self.version_id = uuid4()
        self.version_hash = "a" * 64
        self.profile_id = uuid4()
        self.as_of_values: list[datetime | None] = []
        self.session = session
        self.locks_held_at_read: list[list[str]] = []

    async def load_effective_config(
        self, *, workspace_id: UUID, as_of: datetime | None = None
    ) -> PublishedWorkspaceSettings:
        self.as_of_values.append(as_of)
        if self.session is not None:
            self.locks_held_at_read.append(self.session.lock_keys())
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


def make_coordinator(session: FakeRiskSession, **kwargs: Any) -> WorkspaceRiskCoordinator:
    """Coordinator với nguồn cap/status thay TƯỜNG MINH (không còn fallback ngầm)."""
    kwargs.setdefault("caps_loader", session.load_caps)
    kwargs.setdefault("target_status_loader", _enabled_target)
    return WorkspaceRiskCoordinator(session, **kwargs)  # type: ignore[arg-type]


async def _reserve_with_test_caps(
    coordinator: WorkspaceRiskCoordinator,
    session: FakeRiskSession,
    request: ReservationRequest,
    *,
    account_cap: Decimal,
    bot_cap: Decimal | None = None,
) -> Any:
    session.account_cap = account_cap
    session.bot_cap = bot_cap
    return await coordinator.reserve(request)


def test_default_sources_are_authoritative_and_session_attribute_cannot_inject() -> None:
    """Review: loader giả qua thuộc tính session đã che C1; giờ mặc định luôn là nguồn DB."""

    class SessionWithLegacyHook(FakeRiskSession):
        def test_scope_caps_loader(self, request: ReservationRequest) -> Any:
            raise AssertionError("Không được dùng loader ngầm từ session.")

    session = SessionWithLegacyHook()
    coordinator = WorkspaceRiskCoordinator(session)  # type: ignore[arg-type]

    assert coordinator._caps_loader == coordinator._load_authoritative_caps
    assert coordinator._target_status_loader == coordinator._load_target_status


@pytest.mark.asyncio
async def test_lock_order_workspace_then_accounts_sorted() -> None:
    """Ruling 10: Khóa cấp 1 workspace -> cấp 2 account sắp xếp UUID tăng dần."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id_1 = UUID("20000000-0000-0000-0000-000000000002")
    acc_id_2 = UUID("10000000-0000-0000-0000-000000000001")

    coordinator = make_coordinator(
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
    """Publish commit trong lúc chờ workspace lock phải được thấy: đọc cap SAU cả hai khóa."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    settings_repo = FakeSettingsRepo(ws_cap_fraction=Decimal("0.8"), session=session)
    coordinator = make_coordinator(
        session,
        clock=lambda: NOW,
        settings_repo=settings_repo,
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    # Publish siết cap còn 0.3 commit đúng lúc reservation được cấp workspace lock.
    def publish_while_waiting() -> None:
        settings_repo.ws_cap_fraction = Decimal("0.3")

    session.on_workspace_lock = publish_while_waiting

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

    result = await _reserve_with_test_caps(
        coordinator, session, request, account_cap=Decimal("0.5")
    )

    # Bị từ chối vì cap mới là 0.3 (3000 USDT) chứ không phải 0.8 (8000 USDT)
    assert not result.approved
    assert result.reason_code == BudgetReasonCode.WORKSPACE_CAP_EXCEEDED
    assert settings_repo.as_of_values == [NOW]
    # Thứ tự: root settings và cap account/bot chỉ được đọc khi đã giữ workspace + account lock.
    expected_locks = [f"workspace-risk:{ws_id}", f"account-risk:{ws_id}:{acc_id}"]
    assert settings_repo.locks_held_at_read == [expected_locks]
    assert session.caps_reads == [(NOW, 2)]


@pytest.mark.asyncio
async def test_reserve_approved_within_all_caps() -> None:
    """Reservation được duyệt khi nằm trong mọi cấp trần (workspace, account, bot)."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = make_coordinator(
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

    result = await _reserve_with_test_caps(
        coordinator,
        session,
        request,
        account_cap=Decimal("0.2"),
        bot_cap=Decimal("0.1"),
    )

    assert result.approved
    assert result.reason_code == BudgetReasonCode.APPROVED
    assert result.decision_id is not None
    assert result.reservation_id is not None
    assert len(session.added) == 2  # RiskDecisionRecord + RiskReservationRecord
    assert isinstance(session.added[0], RiskDecisionRecord)
    assert session.added[0].payload["candidate_expires_at"] == request.expires_at.isoformat()
    assert session.flushes >= 1


@pytest.mark.asyncio
async def test_reserve_fails_closed_when_workspace_aggregate_is_unavailable() -> None:
    """Không tạo entry nếu aggregate workspace không thể đọc được."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = make_coordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    async def fail_aggregate(
        *, workspace_id: UUID, as_of: datetime, max_mark_age: timedelta
    ) -> WorkspaceRiskAggregate:
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
        await _reserve_with_test_caps(coordinator, session, request, account_cap=Decimal("0.2"))
    assert session.added == []


@pytest.mark.asyncio
async def test_reserve_denies_when_workspace_aggregate_has_unknown_exposure() -> None:
    """Unknown exposure trong workspace aggregate phải chặn entry mới."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = make_coordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )

    async def unknown_aggregate(
        *, workspace_id: UUID, as_of: datetime, max_mark_age: timedelta
    ) -> WorkspaceRiskAggregate:
        return WorkspaceRiskAggregate(
            workspace_id=str(workspace_id),
            total_equity_usdt=Decimal("10000"),
            total_open_risk_usdt=Decimal("0"),
            total_pending_risk_usdt=Decimal("0"),
            active_bot_count=1,
            has_unknown_exposure=True,
            has_unknown_equity=False,
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

    result = await _reserve_with_test_caps(
        coordinator, session, request, account_cap=Decimal("0.2")
    )

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.WORKSPACE_EXPOSURE_UNKNOWN
    assert result.reservation_id is None


@pytest.mark.asyncio
async def test_reserve_denies_when_enabled_workspace_equity_is_unknown() -> None:
    """Thiếu equity một bot đang chạy thì aggregate phải fail closed."""
    session = FakeRiskSession()
    session.active_bot_count = 2
    session.has_unknown_equity = True
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()
    coordinator = make_coordinator(
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

    result = await _reserve_with_test_caps(
        coordinator, session, request, account_cap=Decimal("0.2")
    )

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.WORKSPACE_EQUITY_UNKNOWN


@pytest.mark.asyncio
async def test_reserve_denies_when_authoritative_target_bot_is_disabled() -> None:
    """Bot/account status được nạp lại trong transaction trước workspace budget."""
    session = FakeRiskSession()
    ws_id, acc_id, bot_id = uuid4(), uuid4(), uuid4()
    coordinator = make_coordinator(
        session,
        clock=lambda: NOW,
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
        target_status_loader=lambda **_: _async_false(),
    )
    request = ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key="disabled-target",
        requested_risk_usdt=Decimal("100"),
        requested_notional=Decimal("1000"),
        requested_initial_margin=Decimal("200"),
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=5),
        plan_hash="disabled-target-plan",
    )

    result = await _reserve_with_test_caps(
        coordinator, session, request, account_cap=Decimal("0.2")
    )

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.BOT_DISABLED
    assert result.reservation_id is None
    assert result.reservation_id is None


@pytest.mark.asyncio
async def test_reserve_denied_when_exceeds_bot_overlay_cap() -> None:
    """Bị từ chối khi vượt bot overlay cap dù thỏa account cap và workspace cap."""
    session = FakeRiskSession()
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()

    coordinator = make_coordinator(
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

    result = await _reserve_with_test_caps(
        coordinator,
        session,
        request,
        account_cap=Decimal("0.2"),
        bot_cap=Decimal("0.1"),
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

    coordinator = make_coordinator(
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
        workspace_aggregate=_aggregate(ws_id),
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

    coordinator = make_coordinator(
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
        workspace_aggregate=_aggregate(ws_id),
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

    coordinator = make_coordinator(
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

    coordinator = make_coordinator(
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
    res1 = await _reserve_with_test_caps(coordinator, session, req1, account_cap=Decimal("0.2"))
    assert res1.approved
    assert not res1.is_replay

    # Lần 2: Cùng key, cùng payload -> Replay
    res2 = await _reserve_with_test_caps(coordinator, session, req1, account_cap=Decimal("0.2"))
    assert res2.approved
    assert res2.is_replay
    assert res2.reservation_id == res1.reservation_id

    # Lần 3: Cùng key, khác payload -> Conflict
    req3 = replace(req1, requested_risk_usdt=Decimal("600"))
    with pytest.raises(BudgetConflictError):
        await _reserve_with_test_caps(coordinator, session, req3, account_cap=Decimal("0.2"))

    req4 = replace(req1, symbol="ETHUSDT", requested_notional=Decimal("6000"))
    with pytest.raises(BudgetConflictError):
        await _reserve_with_test_caps(coordinator, session, req4, account_cap=Decimal("0.2"))


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

    coordinator = make_coordinator(
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
    res = await _reserve_with_test_caps(coordinator, session, req, account_cap=Decimal("0.2"))
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

    coordinator = make_coordinator(
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

    result = await _reserve_with_test_caps(
        coordinator, session, request, account_cap=Decimal("0.2")
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
    coordinator = make_coordinator(
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

    result = await _reserve_with_test_caps(
        coordinator, session, request, account_cap=Decimal("0.5")
    )

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.CAPACITY_BREACH_AFTER_TIGHTENING


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

    coordinator = make_coordinator(
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

    coordinator = make_coordinator(
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


# -----------------------------------------------------------------------------
# Sửa review độc lập MB-04 (C1, C2, C3, I6) — nguồn thật ở E2E, đây là bất biến đơn vị.
# -----------------------------------------------------------------------------


def _aggregate(workspace_id: UUID, **overrides: Any) -> WorkspaceRiskAggregate:
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "total_equity_usdt": Decimal("10000"),
        "total_open_risk_usdt": ZERO,
        "total_pending_risk_usdt": ZERO,
        "active_bot_count": 1,
        "has_unknown_exposure": False,
        "has_unknown_equity": False,
    }
    values.update(overrides)
    return WorkspaceRiskAggregate(**values)


def _request(
    ws_id: UUID,
    bot_id: UUID,
    acc_id: UUID,
    *,
    key: str,
    risk: str = "500",
    as_of: datetime = NOW,
    expires_at: datetime | None = None,
) -> ReservationRequest:
    return ReservationRequest(
        workspace_id=ws_id,
        bot_id=bot_id,
        account_id=acc_id,
        idempotency_key=key,
        requested_risk_usdt=Decimal(risk),
        requested_notional=Decimal("5000"),
        requested_initial_margin=Decimal("1000"),
        as_of=as_of,
        expires_at=expires_at or as_of + timedelta(minutes=5),
        plan_hash=f"plan-{key}",
    )


def _coordinator_at(session: FakeRiskSession, clock_at: datetime, **kwargs: Any) -> Any:
    kwargs.setdefault("settings_repo", FakeSettingsRepo())
    return make_coordinator(
        session,
        clock=lambda: clock_at,
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_stale_evaluated_at_reads_caps_and_equity_at_clock_after_lock() -> None:
    """C3a: evaluated_at cũ hơn publish đã commit -> root/cap/equity đọc tại clock sau khóa."""
    session = FakeRiskSession()
    ws_id, acc_id, bot_id = uuid4(), uuid4(), uuid4()
    later = NOW + timedelta(seconds=30)
    settings_repo = FakeSettingsRepo(session=session)
    coordinator = _coordinator_at(session, later, settings_repo=settings_repo)

    result = await coordinator.reserve(_request(ws_id, bot_id, acc_id, key="stale-as-of"))

    assert result.approved
    assert settings_repo.as_of_values == [later]
    assert session.caps_reads == [(later, 2)]
    assert result.decision is not None
    assert result.decision.as_of == later
    payload = session.records[RiskDecisionRecord][0].payload  # type: ignore[attr-defined]
    assert payload["requested_as_of"] == NOW.isoformat()
    assert payload["risk_read_at"] == later.isoformat()
    aggregate_params = [p for p in session.params_list if "max_mark_age_seconds" in p]
    assert aggregate_params[0]["as_of"] == later


@pytest.mark.asyncio
async def test_future_as_of_keeps_reservations_live_by_real_clock() -> None:
    """C3c: as_of tương lai không được làm rơi RESERVED còn hạn theo đồng hồ thật."""
    session = FakeRiskSession()
    ws_id, acc_id, bot_id = uuid4(), uuid4(), uuid4()
    session.records[RiskReservationRecord].append(
        RiskReservationRecord(
            id=uuid4(),
            workspace_id=ws_id,
            bot_id=bot_id,
            account_id=acc_id,
            risk_decision_id=uuid4(),
            idempotency_key="live-by-clock",
            plan_hash="p-live",
            symbol="BTCUSDT",
            reserved_risk=Decimal("1800"),
            reserved_notional=Decimal("5000"),
            reserved_initial_margin=Decimal("1000"),
            expires_at=NOW + timedelta(seconds=30),
            status=ReservationStatus.RESERVED.value,
            resolution_reason=None,
        )
    )
    coordinator = _coordinator_at(session, NOW)
    future = NOW + timedelta(minutes=1)

    # get_by_idempotency_key của fake trả reservation có sẵn: dùng key khác qua decision trống.
    session.records[RiskReservationRecord][0].idempotency_key = "other"
    coordinator._reservations_repo.get_by_idempotency_key = _none_async
    result = await _reserve_with_test_caps(
        coordinator,
        session,
        _request(ws_id, bot_id, acc_id, key="future-as-of", risk="500", as_of=future),
        account_cap=Decimal("0.2"),
    )

    # Cap account 2000; reservation còn hạn 1800 + 500 > 2000 -> từ chối.
    assert not result.approved
    assert result.reason_code == BudgetReasonCode.ACCOUNT_CAP_EXCEEDED
    assert result.decision is not None
    assert result.decision.account_budget.pending_risk_usdt == Decimal("1800")


async def _none_async(**_: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_candidate_expired_by_clock_is_rejected_before_persistence() -> None:
    """C3: candidate đã hết hạn theo đồng hồ thật không được giữ ngân sách."""
    session = FakeRiskSession()
    ws_id, acc_id, bot_id = uuid4(), uuid4(), uuid4()
    coordinator = _coordinator_at(session, NOW + timedelta(minutes=10))

    with pytest.raises(ReservationConflict, match="hết hạn"):
        await coordinator.reserve(_request(ws_id, bot_id, acc_id, key="expired-candidate"))
    assert session.added == []


@pytest.mark.asyncio
async def test_open_trade_risk_comes_from_linked_reservation_not_lineage() -> None:
    """C2: trade OPEN chiếm cap account bằng reserved_risk của reservation liên kết."""
    session = FakeRiskSession()
    ws_id, acc_id, bot_id = uuid4(), uuid4(), uuid4()
    session.trade_rows = [(uuid4(), ws_id, bot_id, acc_id, "BTCUSDT", uuid4(), Decimal("1700"))]
    coordinator = _coordinator_at(session, NOW)

    result = await _reserve_with_test_caps(
        coordinator,
        session,
        _request(ws_id, bot_id, acc_id, key="open-trade-risk", risk="500"),
        account_cap=Decimal("0.2"),
    )

    # Cap account 2000: open 1700 + 500 > 2000.
    assert not result.approved
    assert result.reason_code == BudgetReasonCode.ACCOUNT_CAP_EXCEEDED
    assert result.decision is not None
    assert result.decision.account_budget.open_risk_usdt == Decimal("1700")
    assert any("FROM trade LEFT OUTER JOIN risk_reservation" in s for s in session.statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("risk", [None, Decimal("0"), Decimal("-5")])
async def test_open_trade_without_valid_reservation_risk_fails_closed(
    risk: Decimal | None,
) -> None:
    """C2: thiếu reservation/rủi ro hợp lệ -> exposure unknown, không bao giờ là 0."""
    session = FakeRiskSession()
    ws_id, acc_id, bot_id = uuid4(), uuid4(), uuid4()
    reservation_id = None if risk is None else uuid4()
    session.trade_rows = [(uuid4(), ws_id, bot_id, acc_id, "BTCUSDT", reservation_id, risk)]
    coordinator = _coordinator_at(session, NOW)

    result = await _reserve_with_test_caps(
        coordinator,
        session,
        _request(ws_id, bot_id, acc_id, key=f"unknown-open-{risk}", risk="10"),
        account_cap=Decimal("0.2"),
    )

    assert not result.approved
    assert result.reason_code == BudgetReasonCode.WORKSPACE_EXPOSURE_UNKNOWN
    assert result.reservation_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason", "clock_offset"),
    [
        (ReservationStatus.RELEASED.value, "CANCELLED_BEFORE_SUBMIT", timedelta(0)),
        (ReservationStatus.TRANSFERRED.value, "OPEN_RISK_RECORDED", timedelta(0)),
        (ReservationStatus.RESERVED.value, None, timedelta(minutes=6)),
    ],
)
async def test_replay_of_dead_reservation_is_a_conflict(
    status: str, reason: str | None, clock_offset: timedelta
) -> None:
    """I6: replay chỉ trả approved khi reservation còn RESERVED/UNKNOWN và chưa hết hạn."""
    session = FakeRiskSession()
    ws_id, acc_id, bot_id = uuid4(), uuid4(), uuid4()
    clock = {"now": NOW}
    coordinator = make_coordinator(
        session,
        clock=lambda: clock["now"],
        settings_repo=FakeSettingsRepo(),
        equity_repo=FakeEquityRepo({}),
        conversion_repo=FakeConversionRepo(),
    )
    request = _request(ws_id, bot_id, acc_id, key=f"dead-{status}")
    first = await _reserve_with_test_caps(coordinator, session, request, account_cap=Decimal("0.2"))
    assert first.approved
    record = session.records[RiskReservationRecord][0]
    record.status = status  # type: ignore[attr-defined]
    record.resolution_reason = reason  # type: ignore[attr-defined]
    clock["now"] = NOW + clock_offset

    with pytest.raises(ReservationConflict):
        await coordinator.reserve(request)


@pytest.mark.asyncio
async def test_replay_of_live_unknown_reservation_returns_recorded_result() -> None:
    session = FakeRiskSession()
    ws_id, acc_id, bot_id = uuid4(), uuid4(), uuid4()
    coordinator = _coordinator_at(session, NOW)
    request = _request(ws_id, bot_id, acc_id, key="live-unknown")
    first = await _reserve_with_test_caps(coordinator, session, request, account_cap=Decimal("0.2"))
    session.records[RiskReservationRecord][0].status = ReservationStatus.UNKNOWN.value  # type: ignore[attr-defined]

    replay = await coordinator.reserve(request)

    assert replay.approved
    assert replay.is_replay
    assert replay.reservation_id == first.reservation_id


@pytest.mark.asyncio
async def test_authoritative_caps_use_resolved_bot_overlay_fraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1: cap bot = `bot_max_open_risk_fraction` của overlay do resolver THẬT trả về.

    Bản lỗi trả `account_max_open_risk_fraction` (0.80) làm cap bot -> overlay 0.50 bị bỏ qua.
    """
    from tests.unit.test_workspace_risk_config import ResolverFixture

    fix = ResolverFixture(workspace_cap="1.0", account_cap="0.80")
    effective = fix.resolve(
        fix.version({"schema_version": 1, "risk": {"bot_max_open_risk_fraction": "0.50"}})
    )

    async def profile(*_: Any, **__: Any) -> Any:
        return fix.risk_profile

    async def overlay(*_: Any, **__: Any) -> Any:
        return effective

    monkeypatch.setattr(workspace_risk_module, "load_risk_profile", profile)
    monkeypatch.setattr(workspace_risk_module, "load_effective_bot_config", overlay)
    coordinator = WorkspaceRiskCoordinator(FakeRiskSession())  # type: ignore[arg-type]
    request = _request(fix.workspace_id, fix.bot_id, fix.account_id, key="c1")

    caps = await coordinator._load_authoritative_caps(request, fix.root, NOW)

    assert caps.account_max_open_risk_fraction == Decimal("0.80")
    assert caps.bot_max_open_risk_fraction == Decimal("0.50")
    assert caps.bot_config_version_id == str(effective.bot_config_version_id)
    assert caps.bot_config_hash == effective.bot_config_hash
