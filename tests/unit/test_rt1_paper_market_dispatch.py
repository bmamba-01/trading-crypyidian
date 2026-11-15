"""RT-1 lát 2: context provider PAPER thật, vòng nến và mark REST backfill (unit)."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from trading_bot.app import _market_dispatch_status, _with_market_loop
from trading_bot.application import paper_lifecycle
from trading_bot.application.paper_market_context import (
    PostgresPaperDecisionContextProvider,
    effective_leverage,
    exchange_rules_from_instrument,
    position_is_flat_at,
    strategy_bar_closed,
)
from trading_bot.application.paper_market_event_loop import (
    PaperMarketEventLoop,
    _Scope,
    closed_bars_after,
    market_event_scope,
    paper_bar_from_row,
    paper_bar_key,
)
from trading_bot.application.paper_runtime_application import PaperRuntimeApplicationError
from trading_bot.config.settings import AppSettings
from trading_bot.domain.execution.paper import PaperBar
from trading_bot.domain.risk.workspace_budget import MARK_SOURCE_FALLBACKS, MarkPolicy

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
WORKSPACE = UUID("11111111-1111-1111-1111-111111111111")


def _load_migration() -> Any:
    path = ROOT / "migrations/versions/f1b3d5e7a9c2_rt1_mark_source_fallback.py"
    spec = importlib.util.spec_from_file_location("rt1_mark_fallback", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- mark source ------------------------------------------------------------------------------
def test_mark_policy_prefers_live_then_rest_backfill() -> None:
    policy = MarkPolicy(source="BINANCE_USDM", timeframes=("1m", "1h"))
    assert policy.sources == ("BINANCE_USDM", "BINANCE_USDM_REST_BACKFILL")
    assert MarkPolicy(source="OTHER", timeframes=("1m",)).sources == ("OTHER",)


def test_mark_migration_sql_matches_python_fallback_table() -> None:
    migration = _load_migration()
    assert migration.down_revision == "e3a5c7e9f1b4"
    assert dict(migration.FALLBACK_SOURCES) == {
        key: value[0] for key, value in MARK_SOURCE_FALLBACKS.items()
    }
    sql = migration.account_state_function_sql()
    assert sql.startswith("\nCREATE OR REPLACE FUNCTION public.workspace_account_risk_state(")
    assert "p_mark_source text, p_mark_timeframes text[]" in sql
    assert "(p_mark_source = 'BINANCE_USDM' AND mb.source = 'BINANCE_USDM_REST_BACKFILL')" in sql
    assert (
        "ORDER BY mb.close_time DESC, (mb.source = p_mark_source) DESC, "
        "mb.available_at DESC, mb.revision DESC" in sql
    )
    # Freshness và hình dạng nến không đổi.
    assert "p_as_of - mk.close_time > make_interval(secs => p_max_mark_age_seconds)" in sql
    assert "mb.source = p_mark_source AND mb.timeframe" not in sql


def test_flatten_mark_query_accepts_backfill_with_live_first() -> None:
    import inspect

    from trading_bot.adapters.database.repositories import flatten_snapshot
    from trading_bot.application import paper_runtime_application

    for module in (flatten_snapshot, paper_runtime_application):
        source = inspect.getsource(module)
        assert "MarketBar.source.in_(" in source
        assert "MarketBar.source == mark_policy.source" not in source.replace(
            "case((MarketBar.source == mark_policy.source", ""
        )


# --- provider pieces ----------------------------------------------------------------------------
def _instrument(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "status": "TRADING",
        "tick_size": Decimal("0.1"),
        "quantity_step": Decimal("0.001"),
        "min_notional": Decimal("5"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_exchange_rules_come_from_instrument_and_risk_caps() -> None:
    rules = exchange_rules_from_instrument(
        _instrument(), max_notional=Decimal("2000"), reference_price=Decimal("300")
    )
    assert rules.tick_size == Decimal("0.1")
    assert rules.quantity_step == Decimal("0.001")
    assert rules.min_notional == Decimal("5")
    assert rules.contract_multiplier == Decimal("1")
    assert rules.max_notional == Decimal("2000")
    # floor(2000 / 300 / 0.001) * 0.001
    assert rules.max_quantity == Decimal("6.666")


@pytest.mark.parametrize(
    ("instrument", "cap", "code"),
    [
        (_instrument(status="BREAK"), Decimal("2000"), "PAPER_INSTRUMENT_NOT_TRADING"),
        (_instrument(tick_size=Decimal("0")), Decimal("2000"), "PAPER_INSTRUMENT_RULES_INVALID"),
        (_instrument(quantity_step=None), Decimal("2000"), "PAPER_INSTRUMENT_RULES_INVALID"),
        (_instrument(), Decimal("0"), "PAPER_EXCHANGE_LIMITS_UNAVAILABLE"),
        (
            _instrument(min_notional=Decimal("5000")),
            Decimal("2000"),
            "PAPER_EXCHANGE_LIMITS_UNAVAILABLE",
        ),
    ],
)
def test_exchange_rules_fail_closed(instrument: Any, cap: Decimal, code: str) -> None:
    with pytest.raises(PaperRuntimeApplicationError) as error:
        exchange_rules_from_instrument(instrument, max_notional=cap, reference_price=Decimal("300"))
    assert error.value.reason_code == code


def test_leverage_is_effective_cap_and_fails_closed_without_one() -> None:
    assert effective_leverage(SimpleNamespace(max_leverage=Decimal("5"))) == Decimal("5")
    for value in (None, Decimal("0"), Decimal("NaN"), "5"):
        with pytest.raises(PaperRuntimeApplicationError) as error:
            effective_leverage(SimpleNamespace(max_leverage=value))
        assert error.value.reason_code == "PAPER_LEVERAGE_UNAVAILABLE"


def test_strategy_bar_closed_uses_nominal_boundaries() -> None:
    hour = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    assert strategy_bar_closed(
        opened_at=hour + timedelta(minutes=59), execution="1m", strategy="1h"
    )
    assert not strategy_bar_closed(opened_at=hour, execution="1m", strategy="1h")
    assert strategy_bar_closed(opened_at=hour, execution="1h", strategy="1h")
    assert strategy_bar_closed(
        opened_at=hour + timedelta(minutes=14), execution="1m", strategy="15m"
    )


class _ScalarSession:
    """Session giả trả lần lượt kết quả `scalar`; ghi lại câu lệnh đã compile."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.statements: list[str] = []

    async def scalar(self, statement: Any) -> Any:
        self.statements.append(
            str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
        )
        return self._results.pop(0)


def test_position_is_flat_reads_trades_then_live_or_later_intents() -> None:
    account = uuid4()
    flat_session = _ScalarSession([None, None])
    assert asyncio.run(
        position_is_flat_at(
            flat_session,  # type: ignore[arg-type]
            workspace_id=WORKSPACE,
            account_id=account,
            symbol="BTCUSDT",
            as_of=NOW,
        )
    )
    trade_sql, intent_sql = flat_session.statements
    assert "trade.status = " in trade_sql
    assert "trade.closed_at >" in trade_sql
    assert "order_intent.status IN" in intent_sql
    assert "order_intent.created_at >" in intent_sql

    for results in ([uuid4()], [None, uuid4()]):
        session = _ScalarSession(results)
        assert not asyncio.run(
            position_is_flat_at(
                session,  # type: ignore[arg-type]
                workspace_id=WORKSPACE,
                account_id=account,
                symbol="BTCUSDT",
                as_of=NOW,
            )
        )


class _ExecuteSession:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(all=lambda: self._rows)


def test_loss_windows_use_pinned_utc_day_and_rolling_window() -> None:
    provider = PostgresPaperDecisionContextProvider(None, workspace_id=WORKSPACE)
    rows = [
        (uuid4(), NOW - timedelta(hours=1), Decimal("-30")),
        (uuid4(), NOW - timedelta(hours=3), Decimal("10")),
        (uuid4(), NOW - timedelta(hours=13), Decimal("-50")),  # hôm trước theo UTC
    ]
    losses = asyncio.run(
        provider._loss_windows(
            _ExecuteSession(rows),  # type: ignore[arg-type]
            SimpleNamespace(account_id=uuid4()),
            as_of=NOW,
        )
    )
    assert losses.timezone_name == "UTC"
    assert losses.realized_loss_today == Decimal("20")
    assert losses.realized_loss_rolling == Decimal("70")


def test_load_context_requires_bot_decision() -> None:
    provider = PostgresPaperDecisionContextProvider(None, workspace_id=WORKSPACE)
    with pytest.raises(PaperRuntimeApplicationError) as error:
        asyncio.run(
            provider._load_decision_evidence(
                _ScalarSession([]),  # type: ignore[arg-type]
                SimpleNamespace(bot_decision_id=None),
                SimpleNamespace(account_id=uuid4(), bot_id=uuid4()),
            )
        )
    assert error.value.reason_code == "PAPER_CONTEXT_BOT_DECISION_REQUIRED"


def test_load_context_rejects_evidence_lineage_mismatch() -> None:
    provider = PostgresPaperDecisionContextProvider(None, workspace_id=WORKSPACE)
    decision = SimpleNamespace(evidence_id=uuid4(), evaluated_at=NOW)
    evidence = SimpleNamespace(feature_snapshot_id="b" * 64, observed_at=NOW)
    with pytest.raises(PaperRuntimeApplicationError) as error:
        asyncio.run(
            provider._load_decision_evidence(
                _ScalarSession([decision, evidence]),  # type: ignore[arg-type]
                SimpleNamespace(
                    bot_decision_id=uuid4(), feature_snapshot_id="a" * 64, evaluated_at=NOW
                ),
                SimpleNamespace(account_id=uuid4(), bot_id=uuid4()),
            )
        )
    assert error.value.reason_code == "PAPER_DECISION_CONTEXT_MISMATCH"


# --- event loop ---------------------------------------------------------------------------------
def _row(minute: int, **overrides: Any) -> SimpleNamespace:
    opened = NOW - timedelta(minutes=30) + timedelta(minutes=minute)
    values: dict[str, Any] = {
        "source": "BINANCE_USDM_REST_BACKFILL",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "open_time": opened,
        "close_time": opened + timedelta(seconds=59, milliseconds=999),
        "available_at": opened + timedelta(minutes=1, seconds=1),
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100"),
        "is_closed": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_paper_bar_key_is_deterministic_and_rows_must_be_closed() -> None:
    row = _row(0)
    bar = paper_bar_from_row(row)
    assert bar is not None
    assert bar.bar_key == paper_bar_key(
        source=row.source, symbol="BTCUSDT", timeframe="1m", open_time=row.open_time
    )
    assert bar.bar_key.split(":")[:3] == ["BINANCE_USDM_REST_BACKFILL", "BTCUSDT", "1m"]
    assert paper_bar_from_row(_row(0, is_closed=False)) is None
    assert paper_bar_from_row(_row(0, available_at=row.close_time - timedelta(seconds=1))) is None


def test_event_scope_matches_lifecycle_cursor_scope() -> None:
    assert market_event_scope("paper:key", "BTCUSDT") == paper_lifecycle._market_event_scope(
        "paper:key", "BTCUSDT"
    )


def test_closed_bar_query_never_reads_unclosed_or_future_bars() -> None:
    captured: list[str] = []

    class _Session:
        async def scalars(self, statement: Any) -> Any:
            captured.append(
                str(
                    statement.compile(  # type: ignore[no-untyped-call]
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                )
            )
            return SimpleNamespace(all=lambda: [_row(0), _row(1, is_closed=False)])

    bars = asyncio.run(
        closed_bars_after(
            _Session(),  # type: ignore[arg-type]
            workspace_id=WORKSPACE,
            symbol="BTCUSDT",
            timeframe="1m",
            now=NOW,
            after=NOW - timedelta(hours=1),
            at_or_after=None,
            limit=10,
        )
    )
    assert len(bars) == 1
    sql = captured[0]
    assert "market_bar.is_closed IS true" in sql
    assert "market_bar.close_time <=" in sql
    assert "market_bar.available_at <=" in sql
    assert "market_bar.open_time >" in sql
    assert "DISTINCT ON (market_bar.open_time)" in sql
    assert "'BINANCE_USDM', 'BINANCE_USDM_REST_BACKFILL'" in sql


class _FakeApplication:
    """Application giả: khớp entry ở bar đầu, đóng vị thế khi low ≤ stop; ghi con trỏ."""

    def __init__(self, stop: Decimal) -> None:
        self.account = SimpleNamespace(
            account_id=uuid4(),
            bot_id=uuid4(),
            gateway=SimpleNamespace(
                entries_paused=False,
                order=lambda _pid: SimpleNamespace(eligible_at=NOW - timedelta(minutes=30)),
            ),
        )
        self.stop = stop
        self.entries: list[PaperBar] = []
        self.monitors: list[PaperBar] = []
        self.expired_at: list[datetime] = []
        self.trade_id = uuid4()

    def runtime_account(self, account_id: UUID) -> Any:
        return self.account if account_id == self.account.account_id else None

    async def apply_entry_event(self, command: Any) -> Any:
        self.entries.append(command.bar)
        return SimpleNamespace(
            order=SimpleNamespace(status=SimpleNamespace(value="FILLED")),
            trade_id=self.trade_id,
            persisted_fill_count=1,
        )

    async def monitor_position(self, command: Any) -> Any:
        assert command.trade_id == self.trade_id
        self.monitors.append(command.bar)
        return SimpleNamespace(trade_closed=command.bar.low <= self.stop)

    async def expire_reservations(self, account_id: UUID, *, as_of: datetime) -> int:
        self.expired_at.append(as_of)
        return 0


class _ScriptedLoop(PaperMarketEventLoop):
    """Thay phần đọc DB bằng dữ liệu kịch bản; con trỏ = open_time bar cuối đã áp."""

    def __init__(self, application: _FakeApplication, rows: list[SimpleNamespace]) -> None:
        super().__init__(application=application, sessions=object(), workspace_id=WORKSPACE)  # type: ignore[arg-type]
        self.rows = rows
        self.cursor: datetime | None = None
        self.app = application
        self.filled = False
        self.closed = False

    async def _load_scopes(self, account: Any) -> tuple[datetime, tuple[_Scope, ...]]:
        if self.closed:
            return NOW, ()
        applied = [*self.app.entries, *self.app.monitors]
        self.cursor = max((bar.opened_at for bar in applied), default=None)
        self.filled = bool(self.app.entries)
        return NOW, (
            _Scope(
                intent_id=uuid4(),
                paper_intent_id="paper:key",
                symbol="BTCUSDT",
                timeframe="1m",
                feature_snapshot_id="f" * 64,
                trade_id=self.app.trade_id if self.filled else None,
                entry_pending=not self.filled,
                cursor=self.cursor,
            ),
        )

    async def _bars(
        self, scope: _Scope, *, now: datetime, at_or_after: datetime | None
    ) -> tuple[PaperBar, ...]:
        bars = []
        for row in self.rows:
            bar = paper_bar_from_row(row)
            if bar is None or row.close_time > now or row.available_at > now:
                continue
            if scope.cursor is not None and bar.opened_at <= scope.cursor:
                continue
            if at_or_after is not None and bar.opened_at < at_or_after:
                continue
            bars.append(bar)
        return tuple(bars)

    async def _previous_bar(self, scope: _Scope, *, before: datetime) -> PaperBar | None:
        """Nến kịch bản liền trước (chốt liên tục dùng như với DB thật)."""
        earlier = [
            bar
            for bar in (paper_bar_from_row(row) for row in self.rows)
            if bar is not None and bar.opened_at < before
        ]
        return max(earlier, key=lambda bar: bar.opened_at, default=None)

    async def _process_scope(self, account: Any, scope: _Scope, now: datetime) -> None:
        await super()._process_scope(account, scope, now)
        self.closed = self.counters.closed_trades > 0


def test_loop_fills_on_first_bar_then_monitors_until_stop_and_is_idempotent() -> None:
    app = _FakeApplication(stop=Decimal("95"))
    rows = [
        _row(-1),  # trước eligible_at: bỏ qua
        _row(0),  # khớp entry
        _row(1),
        _row(2, low=Decimal("90")),  # chạm stop
        _row(3),
        _row(40),  # chưa đóng tại NOW: không dùng
    ]
    loop = _ScriptedLoop(app, rows)
    asyncio.run(loop.run_for_account(app.account.account_id))

    assert [bar.opened_at for bar in app.entries] == [rows[1].open_time]
    assert [bar.opened_at for bar in app.monitors] == [rows[2].open_time, rows[3].open_time]
    assert loop.counters.entry_fills == 1
    assert loop.counters.closed_trades == 1
    assert app.expired_at == [NOW]
    assert loop.status()["state"] == "running"

    # Chạy lại: không áp lại nến nào.
    asyncio.run(loop.run_for_account(app.account.account_id))
    assert len(app.entries) == 1
    assert len(app.monitors) == 2
    assert loop.status()["ticks"] == 2


def test_loop_monitors_bar_by_bar_across_ticks() -> None:
    app = _FakeApplication(stop=Decimal("50"))
    rows = [_row(-1), _row(0), _row(1)]  # _row(-1): nến lịch sử liền trước cho chốt liên tục
    loop = _ScriptedLoop(app, rows)
    asyncio.run(loop.run_for_account(app.account.account_id))
    assert len(app.entries) == 1
    assert len(app.monitors) == 1
    rows.append(_row(2))
    asyncio.run(loop.run_for_account(app.account.account_id))
    assert [bar.opened_at for bar in app.monitors] == [rows[2].open_time, rows[3].open_time]


def test_loop_records_errors_without_raising() -> None:
    app = _FakeApplication(stop=Decimal("95"))

    async def boom(_command: Any) -> Any:
        raise PaperRuntimeApplicationError("PAPER_COMMAND_REJECTED")

    app.apply_entry_event = boom  # type: ignore[method-assign]
    loop = _ScriptedLoop(app, [_row(-1), _row(0)])
    asyncio.run(loop.run_for_account(app.account.account_id))
    status = loop.status()
    assert status["state"] == "degraded"
    assert status["last_error_code"] == "PAPER_COMMAND_REJECTED"
    assert status["errors"] == 1
    assert app.expired_at == [NOW]


def test_loop_skips_entries_while_paused() -> None:
    app = _FakeApplication(stop=Decimal("95"))
    app.account.gateway.entries_paused = True
    loop = _ScriptedLoop(app, [_row(0)])
    asyncio.run(loop.run_for_account(app.account.account_id))
    assert app.entries == []
    assert loop.status()["state"] == "running"


# --- wiring, settings, readiness -----------------------------------------------------------------
def test_market_dispatch_setting_is_runtime_paper_only() -> None:
    workspace = UUID("22222222-2222-2222-2222-222222222222")
    enabled = AppSettings(
        process_role="trading-runtime",
        runtime_workspace_id=workspace,
        runtime_market_dispatch_enabled=True,
    )
    assert enabled.runtime_market_dispatch_enabled is True
    assert (
        AppSettings(
            process_role="trading-runtime", runtime_workspace_id=workspace
        ).runtime_market_dispatch_enabled
        is False
    )
    with pytest.raises(ValidationError, match="runtime_market_dispatch_enabled"):
        AppSettings(process_role="admin-web", runtime_market_dispatch_enabled=True)


def test_market_dispatch_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_BOT_PROCESS_ROLE", "trading-runtime")
    monkeypatch.setenv("TRADING_BOT_RUNTIME_WORKSPACE_ID", str(uuid4()))
    monkeypatch.setenv("TRADING_BOT_RUNTIME_MARKET_DISPATCH_ENABLED", "true")
    assert AppSettings().runtime_market_dispatch_enabled is True


def test_readiness_reports_dispatch_state() -> None:
    assert _market_dispatch_status(SimpleNamespace(state=SimpleNamespace())) == (  # type: ignore[arg-type]
        "not_configured",
        None,
    )
    loop = PaperMarketEventLoop(application=None, sessions=None, workspace_id=WORKSPACE)
    app = SimpleNamespace(state=SimpleNamespace(paper_market_loop=loop))
    state, detail = _market_dispatch_status(app)  # type: ignore[arg-type]
    assert state == "starting"
    assert detail is not None
    assert detail["ticks"] == 0
    loop.last_tick_at, loop.last_tick_succeeded = NOW, False
    state, detail = _market_dispatch_status(app)  # type: ignore[arg-type]
    assert state == "degraded"
    assert detail is not None
    assert detail["last_tick_at"] == NOW.isoformat()


def test_market_loop_runs_even_when_candidate_submission_fails() -> None:
    calls: list[str] = []

    async def failing(_account: Any, _now: Any) -> Any:
        calls.append("submit")
        raise PaperRuntimeApplicationError("PAPER_EQUITY_UNKNOWN")

    class _Loop:
        async def run_for_account(self, _account: Any) -> None:
            calls.append("loop")

    processor = _with_market_loop(failing, _Loop())
    with pytest.raises(PaperRuntimeApplicationError):
        asyncio.run(processor(uuid4(), NOW))
    assert calls == ["submit", "loop"]


def test_enabled_runtime_injects_postgres_context_provider() -> None:
    from fastapi.testclient import TestClient

    from trading_bot.app import create_app

    workspace = uuid4()
    accounts = {uuid4(): object()}
    result = SimpleNamespace(
        workspace_id=workspace,
        ready=True,
        database_ready=True,
        error_code=None,
        account_states={key: SimpleNamespace(status="READY") for key in accounts},
        active_accounts=accounts,
    )

    class _Bootstrap:
        async def bootstrap(self) -> Any:
            return result

    app = create_app(
        AppSettings(
            process_role="trading-runtime",
            runtime_workspace_id=workspace,
            runtime_market_dispatch_enabled=True,
        ),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
    )
    with TestClient(app):
        provider = app.state.paper_application._context_provider
        assert isinstance(provider, PostgresPaperDecisionContextProvider)


# --- Chốt an toàn liên tục nến (sự cố 15/11/2026) ----------------------------------------------


def _pbar(minute: int, open_: str, close: str | None = None):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from trading_bot.domain.execution.paper import PaperBar

    opened = datetime(2026, 11, 15, 12, 0, tzinfo=UTC) + timedelta(minutes=minute)
    o = Decimal(open_)
    c = Decimal(close or open_)
    return PaperBar(
        f"k{minute}",
        "BTCUSDT",
        opened,
        opened + timedelta(minutes=1),
        opened + timedelta(minutes=1),
        o,
        max(o, c),
        min(o, c),
        c,
    )


def test_continuous_prefix_accepts_contiguous_bars() -> None:
    from datetime import timedelta

    from trading_bot.application.paper_market_event_loop import continuous_prefix

    bars = (_pbar(1, "100"), _pbar(2, "101"), _pbar(3, "100.5"))
    accepted, violation = continuous_prefix(
        bars, previous=_pbar(0, "100"), interval=timedelta(minutes=1)
    )
    assert accepted == bars
    assert violation is None


def test_continuous_prefix_stops_at_gap_missing_previous_and_price_jump() -> None:
    from datetime import timedelta

    from trading_bot.application.paper_market_event_loop import (
        DISCONTINUITY_REASON,
        GAP_REASON,
        continuous_prefix,
    )

    minute = timedelta(minutes=1)
    # Khoảng trống giữa nến trước và nến đầu.
    assert continuous_prefix((_pbar(5, "100"),), previous=_pbar(0, "100"), interval=minute) == (
        (),
        GAP_REASON,
    )
    # Không có nến liền trước để kiểm giá.
    assert continuous_prefix((_pbar(1, "100"),), previous=None, interval=minute) == ((), GAP_REASON)
    # Đúng sự cố: nến fixture ~101 rồi nến REST thật ~65.000.
    bars = (_pbar(1, "101"), _pbar(2, "65000"))
    accepted, violation = continuous_prefix(bars, previous=_pbar(0, "101"), interval=minute)
    assert accepted == bars[:1]
    assert violation == DISCONTINUITY_REASON


def test_status_is_degraded_after_discontinuity() -> None:
    from datetime import UTC, datetime

    from trading_bot.application.paper_market_event_loop import PaperMarketEventLoop

    loop = PaperMarketEventLoop.__new__(PaperMarketEventLoop)
    from trading_bot.application.paper_market_event_loop import MarketDispatchCounters

    loop.counters = MarketDispatchCounters(discontinuities=1)
    loop.last_tick_at = datetime(2026, 11, 15, tzinfo=UTC)
    loop.last_tick_succeeded = True
    loop.last_error_code = "PAPER_MARKET_DISCONTINUITY"
    status = loop.status()
    assert status["state"] == "degraded"
    assert status["discontinuities"] == 1
