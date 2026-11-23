"""Unit test TDD cho CRY-197 ([FX-2/con-lai]):
1. Biên gap vượt SL trong cổng lỗ xấu nhất (RiskLimits.stop_gap_buffer_fraction).
2. Lỗ ngày theo múi giờ cấu hình workspace (RiskSettings.loss_window_timezone).
3. Replay/backtest áp drawdown breaker và múi giờ workspace (ReplayAccountState).
4. Phân biệt telemetry thật vs fallback trên /admin/api/operations/health.
5. Hiển thị trạng thái drawdown breaker và hướng dẫn mở lại trên trang bot detail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.application.runtime_replay_evaluator import (
    ReplayAccountState,
    ReplayEvaluationContext,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.config.workspace_settings import (
    FeatureSettings,
    MarketDataSettings,
    PaperExecutionSettings,
    PublishedWorkspaceSettings,
    RiskSettings,
    RuntimeSettings,
    StrategySettings,
    WorkspaceSettings,
)
from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.risk.drawdown_breaker import DrawdownBreakerState
from trading_bot.domain.risk.engine import RiskEngine, RiskState
from trading_bot.domain.risk.planner import (
    AccountSnapshot,
    CostModel,
    ExchangeRules,
    PlannerSettings,
    RiskLimits,
    TradePlan,
    calculate_plan_hash,
)
from trading_bot.domain.risk.risk_windows import (
    RISK_LOSS_WINDOW_POLICY,
    RealizedTradePnlEvent,
    calculate_loss_windows,
    loss_window_policy_for,
)
from trading_bot.domain.strategy.recipes import ExitRecipe

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
BOT_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")


def _sample_limits(**overrides: Any) -> RiskLimits:
    base: dict[str, Any] = {
        "per_trade_risk_fraction": Decimal("0.05"),
        "max_aggregate_open_risk": Decimal("500"),
        "max_notional": Decimal("50000"),
        "max_leverage": Decimal("5"),
        "max_positions": 5,
        "max_symbol_notional": Decimal("25000"),
        "max_concentration_fraction": Decimal("0.50"),
        "max_daily_loss": Decimal("1000"),
        "max_rolling_loss": Decimal("2000"),
        "max_account_age": timedelta(days=1),
        "max_data_age": timedelta(hours=1),
    }
    base.update(overrides)
    return RiskLimits(**base)


def _sample_exchange() -> ExchangeRules:
    return ExchangeRules(
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        min_notional=Decimal("5"),
        max_notional=Decimal("100000"),
        max_quantity=Decimal("100"),
        contract_multiplier=Decimal("1"),
    )


def _sample_plan(now: datetime, *, estimated_loss: Decimal = Decimal("100")) -> TradePlan:
    recipe = ExitRecipe("h01-exit-v1", 2.0, trailing_atr_multiple=3.0)
    plan = TradePlan(
        workspace_id=str(WORKSPACE),
        account_id="acc-1",
        settings_version_id="sv-1",
        settings_hash="a" * 64,
        risk_profile_version_id="rp-1",
        strategy_id="H01",
        strategy_release_id="h01-v1",
        release_content_hash="b" * 64,
        snapshot_id="c" * 64,
        candidate_id="d" * 64,
        symbol="BTCUSDT",
        direction="long",
        candidate_reason_code="H01_LONG_ENTRY",
        evidence_feature_ids=("f-1",),
        evidence_bar_keys=("BTCUSDT|1h|t1",),
        macro_evidence_ids=(),
        entry_reference_price=Decimal("50000"),
        entry_price=Decimal("50000"),
        stop_price=Decimal("49000"),
        target_price=Decimal("53000"),
        quantity=Decimal("0.1"),
        notional=Decimal("5000"),
        leverage=Decimal("2"),
        initial_margin=Decimal("2500"),
        risk_budget=Decimal("200"),
        estimated_loss=estimated_loss,
        entry_fee_cost=Decimal("0"),
        exit_fee_cost=Decimal("0"),
        adverse_funding_cost=Decimal("0"),
        entry_slippage_cost=Decimal("0"),
        cost_model_version="cost-v1",
        exchange_rules=_sample_exchange(),
        exit_recipe=recipe,
        expires_at=now + timedelta(hours=1),
        source_signal_at=now - timedelta(minutes=1),
        plan_hash="",
    )
    return TradePlan(
        **{
            **{f: getattr(plan, f) for f in plan.__dataclass_fields__},
            "plan_hash": calculate_plan_hash(plan),
        }
    )


def _sample_published_settings(
    *,
    max_drawdown_fraction: Decimal | None = Decimal("0.10"),
    loss_window_timezone: str | None = None,
) -> PublishedWorkspaceSettings:
    ws = WorkspaceSettings(
        schema_version=1,
        runtime=RuntimeSettings(mode="PAPER", timezone="UTC"),
        market_data=MarketDataSettings(
            venue="BINANCE_USDM",
            symbols=("BTCUSDT",),
            timeframes=("1h", "4h"),
            max_age_seconds=300,
        ),
        features=FeatureSettings(formula_version="crypidian-formulas-v1", sessions_timezone="UTC"),
        strategy=StrategySettings(
            default_release="h01-v1",
            entry_timeframe="1h",
            confirmation_timeframe="4h",
        ),
        risk=RiskSettings(
            profile_version_id=uuid4(),
            max_drawdown_fraction=max_drawdown_fraction,
            loss_window_timezone=loss_window_timezone,
        ),
        paper_execution=PaperExecutionSettings(
            quote_asset="USDT",
            starting_equity=Decimal("10000"),
            fee_schedule_version="paper-fee-v1",
            fill_model="next_bar_with_adverse_costs",
        ),
    )
    return PublishedWorkspaceSettings(
        workspace_id=WORKSPACE,
        settings_version_id=uuid4(),
        settings_hash="a" * 64,
        settings=ws,
        effective_at=datetime(2026, 11, 22, 0, 0, tzinfo=UTC),
    )


# --- 1. Gap vượt SL trong cổng lỗ xấu nhất ------------------------------------------------


def test_stop_gap_buffer_validation_on_risk_limits() -> None:
    """stop_gap_buffer_fraction phải nằm trong [0, 1) và hữu hạn."""
    assert _sample_limits().stop_gap_buffer_fraction == Decimal("0")
    buffered = _sample_limits(stop_gap_buffer_fraction=Decimal("0.25"))
    assert buffered.stop_gap_buffer_fraction == Decimal("0.25")
    with pytest.raises(ValueError, match="gap"):
        _sample_limits(stop_gap_buffer_fraction=Decimal("-0.01"))
    with pytest.raises(ValueError, match="gap"):
        _sample_limits(stop_gap_buffer_fraction=Decimal("1.0"))


def test_worst_case_loss_gate_includes_stop_gap_buffer() -> None:
    """Cổng lỗ xấu nhất phải cộng thêm biên trượt gap vượt SL cho cả daily và rolling."""
    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    plan = _sample_plan(now, estimated_loss=Decimal("100"))
    exchange = _sample_exchange()

    # Không có gap buffer: 890 + 100 = 990 <= 1000 -> APPROVED
    limits_no_gap = _sample_limits(stop_gap_buffer_fraction=Decimal("0"))
    settings_no_gap = PlannerSettings(
        workspace_id=str(WORKSPACE),
        settings_version_id="sv-1",
        risk_profile_version_id="rp-1",
        published=True,
        active_release_ids=("h01-v1",),
        leverage=Decimal("2"),
        exchange=exchange,
        limits=limits_no_gap,
        release_content_hashes=(("h01-v1", "b" * 64),),
        settings_hash="a" * 64,
    )
    state = RiskState(
        workspace_id=str(WORKSPACE),
        account_id="acc-1",
        settings_version_id="sv-1",
        risk_profile_version_id="rp-1",
        evaluated_at=now,
        account_captured_at=now,
        market_data_as_of=now,
        equity=Decimal("10000"),
        available_margin=Decimal("8000"),
        open_exposures=(),
        realized_loss_today=Decimal("890"),
        realized_loss_rolling=Decimal("890"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=Decimal("0"),
    )
    engine = RiskEngine()
    assert engine.evaluate(plan, state, settings_no_gap).approved is True

    # Có gap buffer 20%: worst_case = 100 * 1.20 = 120 -> 890 + 120 = 1010 > 1000 -> REJECTED
    limits_with_gap = _sample_limits(stop_gap_buffer_fraction=Decimal("0.20"))
    settings_with_gap = PlannerSettings(
        workspace_id=str(WORKSPACE),
        settings_version_id="sv-1",
        risk_profile_version_id="rp-1",
        published=True,
        active_release_ids=("h01-v1",),
        leverage=Decimal("2"),
        exchange=exchange,
        limits=limits_with_gap,
        release_content_hashes=(("h01-v1", "b" * 64),),
        settings_hash="a" * 64,
    )
    decision = engine.evaluate(plan, state, settings_with_gap)
    assert decision.approved is False
    assert decision.reason_code == "DAILY_LOSS_CAP_WOULD_BREACH"

    # Kiểm tra tương tự cho ROLLING_LOSS_CAP_WOULD_BREACH
    rolling_state = RiskState(
        **{
            **{f: getattr(state, f) for f in state.__dataclass_fields__},
            "realized_loss_today": Decimal("0"),
            "realized_loss_rolling": Decimal("1890"),
        }
    )
    rolling_decision = engine.evaluate(plan, rolling_state, settings_with_gap)
    assert rolling_decision.approved is False
    assert rolling_decision.reason_code == "ROLLING_LOSS_CAP_WOULD_BREACH"


# --- 2. Lỗ ngày theo múi giờ cấu hình workspace -------------------------------------------


def test_workspace_loss_window_timezone_setting_and_policy() -> None:
    """RiskSettings.loss_window_timezone hỗ trợ múi giờ IANA, lược khỏi JSON khi None."""
    default_risk = RiskSettings(profile_version_id=uuid4())
    assert default_risk.loss_window_timezone is None
    assert "loss_window_timezone" not in default_risk.model_dump(mode="json")
    assert loss_window_policy_for(None) == RISK_LOSS_WINDOW_POLICY

    vn_risk = RiskSettings(profile_version_id=uuid4(), loss_window_timezone="Asia/Ho_Chi_Minh")
    assert vn_risk.loss_window_timezone == "Asia/Ho_Chi_Minh"
    assert vn_risk.model_dump(mode="json")["loss_window_timezone"] == "Asia/Ho_Chi_Minh"

    with pytest.raises(ValueError, match="Múi giờ"):
        RiskSettings(profile_version_id=uuid4(), loss_window_timezone="Invalid/Zone")

    # 18:00 UTC ngày 22/11 = 01:00+07 ngày 23/11. Tại 02:00 UTC ngày 23/11 (= 09:00+07 ngày 23/11):
    # - Theo UTC: lệnh thuộc ngày 22/11 -> lỗ ngày = 0.
    # - Theo Asia/Ho_Chi_Minh: lệnh thuộc ngày 23/11 -> lỗ ngày = 150.
    event = RealizedTradePnlEvent(
        trade_id="t-1",
        closed_at=datetime(2026, 11, 22, 18, 0, tzinfo=UTC),
        net_pnl=Decimal("-150"),
    )
    as_of = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)
    utc_snap = calculate_loss_windows([event], as_of=as_of, policy=loss_window_policy_for(None))
    vn_snap = calculate_loss_windows(
        [event], as_of=as_of, policy=loss_window_policy_for("Asia/Ho_Chi_Minh")
    )
    assert utc_snap.realized_loss_today == Decimal("0")
    assert vn_snap.realized_loss_today == Decimal("150")
    assert vn_snap.timezone_name == "Asia/Ho_Chi_Minh"


# --- 3. Replay áp drawdown breaker và múi giờ workspace -----------------------------------


def test_replay_account_state_trips_drawdown_breaker_and_uses_workspace_timezone() -> None:
    """ReplayAccountState phải bật drawdown_breaker_tripped khi sụt vốn >= ngưỡng và áp múi giờ."""
    t0 = datetime(2026, 11, 22, 17, 0, tzinfo=UTC)
    t_close = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)  # 01:00+07 ngày 23/11
    t_eval = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)    # 09:00+07 ngày 23/11

    published = _sample_published_settings(
        max_drawdown_fraction=Decimal("0.10"),
        loss_window_timezone="Asia/Ho_Chi_Minh",
    )
    limits = _sample_limits()
    planner_settings = PlannerSettings(
        workspace_id=str(WORKSPACE),
        settings_version_id=str(published.settings_version_id),
        risk_profile_version_id=str(published.settings.risk.profile_version_id),
        published=True,
        active_release_ids=("h01-v1",),
        leverage=Decimal("2"),
        exchange=_sample_exchange(),
        limits=limits,
        release_content_hashes=(("h01-v1", "b" * 64),),
        settings_hash=published.settings_hash,
    )
    account = AccountSnapshot(
        workspace_id=str(WORKSPACE),
        account_id="acc-1",
        settings_version_id=str(published.settings_version_id),
        captured_at=t0,
        equity=Decimal("10000"),
        available_margin=Decimal("10000"),
        is_known=True,
    )
    risk_state = RiskState(
        workspace_id=str(WORKSPACE),
        account_id="acc-1",
        settings_version_id=str(published.settings_version_id),
        risk_profile_version_id=str(published.settings.risk.profile_version_id),
        evaluated_at=t_eval,
        account_captured_at=t0,
        market_data_as_of=t_eval,
        equity=Decimal("10000"),
        available_margin=Decimal("10000"),
        open_exposures=(),
        realized_loss_today=Decimal("0"),
        realized_loss_rolling=Decimal("0"),
        open_position_count=0,
        is_known=True,
        pending_reservation_initial_margin=Decimal("0"),
    )
    ctx = ReplayEvaluationContext(
        published_settings=published,
        evaluated_at=t_eval,
        planner_settings=planner_settings,
        account=account,
        risk_state=risk_state,
        open_exposures=(),
        cost_model=CostModel("cost-v1", Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        feature_schema_hash="f" * 64,
        pipeline_content_hash="p" * 64,
        execution_events=(),
        reserve_risk=lambda _d, _k: True,
    )
    book = ReplayAccountState(ctx)
    # Ghi nhận một lệnh đóng lỗ -1100 (11% >= ngưỡng 10%) lúc 18:00 UTC (01:00+07 hôm sau)
    book._closed_pnl["pos-loss-1"] = Decimal("-1100")
    book._closed_at["pos-loss-1"] = t_close

    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            "paper-fill-v1", timedelta(0), Decimal("0"), Decimal("0"), Decimal("0")
        )
    )
    projected_account, projected_risk, _ = book.project(
        ctx, gateway, reservations={}, candidate_symbol="BTCUSDT"
    )
    assert projected_account.equity == Decimal("8900")
    assert projected_risk.drawdown_breaker_tripped is True
    # Theo múi giờ Asia/Ho_Chi_Minh, khoản lỗ lúc 18:00 UTC vẫn nằm trong ngày giao dịch hiện tại
    assert projected_risk.realized_loss_today == Decimal("1100")


# --- 4. Telemetry thật vs fallback trên /admin/api/operations/health ----------------------


class _StubBotAdminService:
    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        return [
            {
                "id": str(BOT_ID),
                "name": "BTC Trend H1",
                "status": "ENABLED",
                "account": {"id": str(uuid4()), "mode": "PAPER", "status": "ACTIVE"},
                "versions": [],
            }
        ]

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        return {
            "id": str(bot_id),
            "name": "BTC Trend H1",
            "status": "ENABLED",
            "account": {"id": str(uuid4()), "mode": "PAPER", "status": "ACTIVE"},
            "versions": [],
        }


class _StubOperationsService:
    async def list_bots(self) -> list[dict[str, Any]]:
        return [
            {
                "id": str(BOT_ID),
                "name": "BTC Trend H1",
                "status": "ENABLED",
                "readiness": "READY",
                "entry_state": "READY",
                "entry_allowed": True,
                "control_revision": 1,
                "error_count": 0,
            }
        ]

    async def bot_status(self, bot_id: UUID) -> dict[str, Any]:
        return (await self.list_bots())[0]

    async def workspace_status(self) -> dict[str, Any]:
        return {"kill_state": "ACTIVE", "revision": 2}


class _StubOperationsRoot:
    def for_principal(self, principal: Principal) -> _StubOperationsService:
        return _StubOperationsService()


class _StubAuthService:
    async def issue_csrf_token(self, cookie: str | None) -> str:
        return "csrf-test-token"


class _StubDrawdownProvider:
    def __init__(self, state: DrawdownBreakerState) -> None:
        self._state = state

    async def load_bot_drawdown_breaker(
        self, *, workspace_id: UUID, bot_id: UUID, account_id: UUID | None
    ) -> DrawdownBreakerState:
        return self._state


def _build_admin_client(*, drawdown_state: DrawdownBreakerState | None = None) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        user_id=USER_ID,
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    identity = AuthIdentity(principal=principal, email="owner@crypidian.io")
    app.dependency_overrides[current_identity] = lambda: identity
    app.state.bot_admin_service = _StubBotAdminService()
    app.state.bot_operations_service = _StubOperationsRoot()
    app.state.auth_service = _StubAuthService()
    if drawdown_state is not None:
        app.state.bot_drawdown_breaker_provider = _StubDrawdownProvider(drawdown_state)
    return TestClient(app)


def test_operations_health_fallback_marks_telemetry_unavailable() -> None:
    """Khi thiếu DB/OperationalHealthService, phải gắn cờ telemetry_available=False tường minh."""
    client = _build_admin_client()
    resp = client.get("/admin/api/operations/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["telemetry_available"] is False
    assert body["metadata"]["telemetry_source"] == "unavailable_no_db"


# --- 5. Bot detail hiển thị trạng thái breaker/drawdown và hướng dẫn mở lại ---------------


def test_bot_detail_renders_tripped_drawdown_breaker_and_resume_guidance() -> None:
    """Khi cầu dao sụt vốn đã ngắt mạch, bot detail phải hiển thị cảnh báo và hướng dẫn mở lại."""
    tripped_state = DrawdownBreakerState(
        peak_equity=Decimal("10000"),
        current_equity=Decimal("8800"),
        drawdown_fraction=Decimal("0.12"),
        tripped=True,
        baseline_at=datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
        policy_version="fx2-drawdown-v1",
        max_drawdown_fraction=Decimal("0.10"),
    )
    client = _build_admin_client(drawdown_state=tripped_state)
    resp = client.get(f"/admin/bots/{BOT_ID}")
    assert resp.status_code == 200
    html = resp.text
    assert "bot-drawdown-breaker" in html
    assert "Đã ngắt mạch" in html
    assert "12.00%" in html or "12,00%" in html
    assert "10.00%" in html or "10,00%" in html
    # Hướng dẫn mở lại bằng tay: Tạm dừng rồi Tiếp tục bot (MFA + lý do)
    assert "Tạm dừng" in html
    assert "Tiếp tục" in html
    assert "MFA" in html


def test_bot_detail_renders_normal_drawdown_breaker_state() -> None:
    """Khi chưa chạm ngưỡng, bot detail hiển thị trạng thái bình thường kèm số đo thực tế."""
    ok_state = DrawdownBreakerState(
        peak_equity=Decimal("10000"),
        current_equity=Decimal("9700"),
        drawdown_fraction=Decimal("0.03"),
        tripped=False,
        baseline_at=None,
        policy_version="fx2-drawdown-v1",
        max_drawdown_fraction=Decimal("0.10"),
    )
    client = _build_admin_client(drawdown_state=ok_state)
    resp = client.get(f"/admin/bots/{BOT_ID}")
    assert resp.status_code == 200
    html = resp.text
    assert "bot-drawdown-breaker" in html
    assert "Bình thường" in html
