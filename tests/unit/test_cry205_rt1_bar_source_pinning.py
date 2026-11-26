"""Unit test TDD cho CRY-205 (RT-1 ghim nguồn nến, ưu tiên nguồn thống nhất và receipt từ chối)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.bot_decisions import (
    BotDecisionRepository,
)
from trading_bot.app import _LOGGED_CANDIDATE_REJECTIONS, _submit_bot_candidates
from trading_bot.application.bot_dispatcher import BotDecisionStatus
from trading_bot.application.bot_reporting import (
    MARKET_SOURCE_PRIORITY,
    preferred_market_source,
)
from trading_bot.application.market_chart import REAL_MARKET_SOURCES
from trading_bot.application.market_signal_composition import (
    SIGNAL_BAR_SOURCES,
    load_closed_market_bars,
)
from trading_bot.application.paper_market_context import PostgresPaperDecisionContextProvider
from trading_bot.application.paper_market_event_loop import (
    PaperMarketEventLoop,
    _Scope,
    closed_bar_before,
    closed_bars_after,
    paper_bar_from_row,
    paper_bar_key,
)
from trading_bot.application.paper_runtime_application import PaperRuntimeApplicationError
from trading_bot.domain.execution.paper import (
    IntentConflict,
    PaperBar,
    PaperExecutionGateway,
    PaperExecutionPolicy,
)
from trading_bot.domain.execution.paper_state import create_snapshot, restore_snapshot
from trading_bot.domain.market.bar_source_policy import (
    BAR_SOURCE_PRIORITY,
    LIVE_BAR_SOURCE,
    REAL_BAR_SOURCES,
    REST_BACKFILL_BAR_SOURCE,
    allowed_bar_sources,
    extract_bar_source_from_key,
    select_primary_bar_source,
)
from trading_bot.domain.risk.planner import ExchangeRules, TradePlan, calculate_plan_hash
from trading_bot.domain.strategy.recipes import ExitRecipe

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
WORKSPACE = UUID("11111111-1111-1111-1111-111111111111")
BOT_ID = UUID("22222222-2222-2222-2222-222222222222")
ACCOUNT_ID = UUID("33333333-3333-3333-3333-333333333333")


def _policy() -> PaperExecutionPolicy:
    return PaperExecutionPolicy(
        version_id="paper-policy-v1",
        latency=timedelta(seconds=5),
        spread_rate=Decimal("0.0001"),
        slippage_rate=Decimal("0.0002"),
        fee_rate=Decimal("0.0005"),
    )


def _plan(symbol: str = "BTCUSDT", *, entry: Decimal = Decimal("100")) -> TradePlan:
    base = TradePlan(
        workspace_id=str(WORKSPACE),
        account_id=str(ACCOUNT_ID),
        settings_version_id=str(uuid4()),
        settings_hash=None,
        risk_profile_version_id="risk-v1",
        strategy_id="H03",
        strategy_release_id="h03-v1",
        release_content_hash="a" * 64,
        snapshot_id="b" * 64,
        candidate_id="c" * 64,
        symbol=symbol,
        direction="long",
        candidate_reason_code="H03_LONG_ENTRY",
        evidence_feature_ids=("b" * 64,),
        evidence_bar_keys=("entry-bar-1",),
        macro_evidence_ids=(),
        entry_reference_price=entry,
        entry_price=entry,
        stop_price=entry - Decimal("5"),
        target_price=entry + Decimal("10"),
        quantity=Decimal("1"),
        notional=entry,
        leverage=Decimal("2"),
        initial_margin=entry / Decimal("2"),
        risk_budget=Decimal("5"),
        estimated_loss=Decimal("5"),
        entry_fee_cost=Decimal("0.05"),
        exit_fee_cost=Decimal("0.05"),
        adverse_funding_cost=Decimal("0"),
        entry_slippage_cost=Decimal("0.02"),
        cost_model_version="cost-v1",
        exchange_rules=ExchangeRules(
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            contract_multiplier=Decimal("1"),
            min_notional=Decimal("5"),
            max_notional=Decimal("100000"),
            max_quantity=Decimal("1000"),
        ),
        exit_recipe=ExitRecipe("h03-exit-v1", 2.0, take_profit_atr_multiple=4.0, max_hold_bars=24),
        expires_at=NOW + timedelta(hours=2),
        source_signal_at=NOW - timedelta(minutes=5),
        plan_hash="",
    )
    return replace(base, plan_hash=calculate_plan_hash(base))


def _bar(
    *,
    source: str | None,
    opened_at: datetime,
    price: Decimal = Decimal("100"),
    high: Decimal | None = None,
    low: Decimal | None = None,
    bar_key: str | None = None,
) -> PaperBar:
    closed_at = opened_at + timedelta(minutes=1)
    key = (
        bar_key
        if bar_key is not None
        else paper_bar_key(
            source=source or LIVE_BAR_SOURCE,
            symbol="BTCUSDT",
            timeframe="1m",
            open_time=opened_at,
        )
    )
    return PaperBar(
        bar_key=key,
        symbol="BTCUSDT",
        opened_at=opened_at,
        closed_at=closed_at,
        available_at=closed_at + timedelta(seconds=1),
        open=price,
        high=high if high is not None else price + Decimal("1"),
        low=low if low is not None else price - Decimal("1"),
        close=price,
        bar_source=source,
    )


# --- 1. Chính sách ưu tiên nguồn nến thống nhất --------------------------------------------------


def test_bar_source_priority_is_unified_across_chart_and_runtime() -> None:
    assert BAR_SOURCE_PRIORITY == (LIVE_BAR_SOURCE, REST_BACKFILL_BAR_SOURCE)
    assert MARKET_SOURCE_PRIORITY == BAR_SOURCE_PRIORITY
    assert SIGNAL_BAR_SOURCES == BAR_SOURCE_PRIORITY
    assert REAL_MARKET_SOURCES == REAL_BAR_SOURCES


def test_select_primary_bar_source_prefers_ws_over_rest_over_fixture() -> None:
    assert (
        select_primary_bar_source(
            ["CRYPIDIAN_PIPELINE_FIXTURE", REST_BACKFILL_BAR_SOURCE, LIVE_BAR_SOURCE]
        )
        == LIVE_BAR_SOURCE
    )
    assert (
        select_primary_bar_source(["CRYPIDIAN_PIPELINE_FIXTURE", REST_BACKFILL_BAR_SOURCE])
        == REST_BACKFILL_BAR_SOURCE
    )
    assert (
        select_primary_bar_source(["Z_FIXTURE", "CRYPIDIAN_PIPELINE_FIXTURE"])
        == "CRYPIDIAN_PIPELINE_FIXTURE"
    )
    assert select_primary_bar_source([]) is None
    assert (
        preferred_market_source(
            [REST_BACKFILL_BAR_SOURCE, LIVE_BAR_SOURCE, "CRYPIDIAN_PIPELINE_FIXTURE"]
        )
        == LIVE_BAR_SOURCE
    )


def test_select_primary_bar_source_strictly_honors_pinned_source() -> None:
    assert (
        select_primary_bar_source(
            [LIVE_BAR_SOURCE, REST_BACKFILL_BAR_SOURCE, "CRYPIDIAN_PIPELINE_FIXTURE"],
            pinned_source="CRYPIDIAN_PIPELINE_FIXTURE",
        )
        == "CRYPIDIAN_PIPELINE_FIXTURE"
    )
    # Khi nguồn đã ghim vắng mặt, tuyệt đối không fallback sang nguồn khác.
    assert (
        select_primary_bar_source(
            [LIVE_BAR_SOURCE, REST_BACKFILL_BAR_SOURCE],
            pinned_source="CRYPIDIAN_PIPELINE_FIXTURE",
        )
        is None
    )
    assert allowed_bar_sources(pinned_source="CRYPIDIAN_PIPELINE_FIXTURE") == (
        "CRYPIDIAN_PIPELINE_FIXTURE",
    )
    assert allowed_bar_sources() == BAR_SOURCE_PRIORITY


def test_extract_bar_source_from_key() -> None:
    key = paper_bar_key(
        source="CRYPIDIAN_PIPELINE_FIXTURE",
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=NOW,
    )
    assert extract_bar_source_from_key(key) == "CRYPIDIAN_PIPELINE_FIXTURE"
    assert extract_bar_source_from_key("legacy-bar-key-1") is None


# --- 2. Ghim nguồn nến trong checkpoint và chặn monitor khác nguồn ------------------------------


def test_position_pins_bar_source_in_checkpoint_and_rejects_foreign_source_monitor() -> None:
    gateway = PaperExecutionGateway(_policy())
    plan = _plan(entry=Decimal("100"))
    order = gateway.submit(plan, "idem-pin-1", submitted_at=NOW)
    entry_bar = _bar(
        source="CRYPIDIAN_PIPELINE_FIXTURE",
        opened_at=NOW + timedelta(minutes=1),
        price=Decimal("100"),
    )
    gateway.apply_market_event(entry_bar, intent_id=order.intent_id)
    position = gateway.position(order.intent_id)
    assert position is not None
    assert position.bar_source == "CRYPIDIAN_PIPELINE_FIXTURE"

    # Snapshot giữ nguyên bar_source qua chu trình serialize/restore.
    snapshot = gateway.export_state()
    raw_body = json.loads(snapshot.payload)
    assert raw_body["positions"][0]["bar_source"] == "CRYPIDIAN_PIPELINE_FIXTURE"
    restored = PaperExecutionGateway.restore(_policy(), snapshot)
    restored_pos = restored.position(order.intent_id)
    assert restored_pos is not None
    assert restored_pos.bar_source == "CRYPIDIAN_PIPELINE_FIXTURE"

    # Nến từ nguồn khác (BINANCE_USDM giá 65.000 chạm TP/SL) bị từ chối tuyệt đối!
    foreign_bar = _bar(
        source=LIVE_BAR_SOURCE,
        opened_at=NOW + timedelta(minutes=2),
        price=Decimal("65000"),
        high=Decimal("65500"),
        low=Decimal("50"),
    )
    with pytest.raises(IntentConflict, match="nguồn"):
        restored.monitor_position(restored_pos, foreign_bar)
    still_open = restored.position(order.intent_id)
    assert still_open is not None
    assert not still_open.is_closed
    assert still_open.quantity == Decimal("1")

    # Nến đúng nguồn đã ghim vẫn monitor và đóng lệnh bình thường.
    same_source_stop_bar = _bar(
        source="CRYPIDIAN_PIPELINE_FIXTURE",
        opened_at=NOW + timedelta(minutes=2),
        price=Decimal("94"),
        high=Decimal("95"),
        low=Decimal("90"),
    )
    monitored = restored.monitor_position(still_open, same_source_stop_bar)
    assert monitored.position.is_closed
    assert monitored.exit_reason == "STOP_LOSS"


def test_legacy_checkpoint_without_bar_source_restores_compatibly() -> None:
    gateway = PaperExecutionGateway(_policy())
    plan = _plan(entry=Decimal("100"))
    order = gateway.submit(plan, "idem-legacy-1", submitted_at=NOW)
    legacy_bar = _bar(
        source=None,
        opened_at=NOW + timedelta(minutes=1),
        price=Decimal("100"),
        bar_key="legacy-entry-bar-1",
    )
    gateway.apply_market_event(legacy_bar, intent_id=order.intent_id)
    position = gateway.position(order.intent_id)
    assert position is not None
    assert position.bar_source is None

    snapshot = gateway.export_state()
    raw_body = json.loads(snapshot.payload)
    # Không thêm khóa bar_source khi None → giữ nguyên content_hash của checkpoint cũ.
    assert "bar_source" not in raw_body["positions"][0]
    _orders, _keys, positions, *_rest = restore_snapshot(_policy(), snapshot)
    assert positions[order.intent_id].bar_source is None

    # Create snapshot trực tiếp cũng giữ tương thích.
    rebuilt = create_snapshot(
        policy=_policy(),
        orders=_orders,
        keys=_keys,
        positions=positions,
        plans=_rest[0],
        close_keys=_rest[1],
        entries_paused=_rest[2],
        market_event_hashes=_rest[3],
        last_market_event=_rest[4],
    )
    assert rebuilt.content_hash == snapshot.content_hash


# --- 3. Event loop và context provider lọc đúng nguồn đã ghim ------------------------------------


class _RecordingScalarsSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = list(rows or [])
        self.statements: list[str] = []

    async def scalars(self, statement: Any) -> Any:
        self.statements.append(
            str(
                statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )
        return SimpleNamespace(all=lambda: list(self._rows))

    async def scalar(self, statement: Any) -> Any:
        self.statements.append(
            str(
                statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )
        return self._rows[0] if self._rows else None


def test_closed_bars_queries_filter_strictly_to_pinned_source() -> None:
    session = _RecordingScalarsSession()
    asyncio.run(
        closed_bars_after(
            session,  # type: ignore[arg-type]
            workspace_id=WORKSPACE,
            symbol="BTCUSDT",
            timeframe="1m",
            now=NOW,
            after=None,
            at_or_after=NOW - timedelta(minutes=5),
            limit=10,
            pinned_source="CRYPIDIAN_PIPELINE_FIXTURE",
        )
    )
    sql = session.statements[0]
    assert "'CRYPIDIAN_PIPELINE_FIXTURE'" in sql
    assert "'BINANCE_USDM'" not in sql

    before_session = _RecordingScalarsSession()
    asyncio.run(
        closed_bar_before(
            before_session,  # type: ignore[arg-type]
            workspace_id=WORKSPACE,
            symbol="BTCUSDT",
            timeframe="1m",
            before=NOW,
            pinned_source=REST_BACKFILL_BAR_SOURCE,
        )
    )
    before_sql = before_session.statements[0]
    assert "'BINANCE_USDM_REST_BACKFILL'" in before_sql
    assert "'BINANCE_USDM'" not in before_sql.replace("'BINANCE_USDM_REST_BACKFILL'", "")


def test_load_closed_market_bars_filters_to_pinned_source() -> None:
    session = _RecordingScalarsSession()
    asyncio.run(
        load_closed_market_bars(
            session,  # type: ignore[arg-type]
            workspace_id=WORKSPACE,
            symbol="BTCUSDT",
            timeframe="1h",
            as_of=NOW,
            pinned_source=REST_BACKFILL_BAR_SOURCE,
        )
    )
    sql = session.statements[0]
    assert "'BINANCE_USDM_REST_BACKFILL'" in sql
    assert "'BINANCE_USDM'" not in sql.replace("'BINANCE_USDM_REST_BACKFILL'", "")


def test_paper_bar_from_row_preserves_bar_source() -> None:
    row = SimpleNamespace(
        source=REST_BACKFILL_BAR_SOURCE,
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=NOW,
        close_time=NOW + timedelta(minutes=1),
        available_at=NOW + timedelta(minutes=1, seconds=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        is_closed=True,
    )
    bar = paper_bar_from_row(row)
    assert bar is not None
    assert bar.bar_source == REST_BACKFILL_BAR_SOURCE


def test_event_loop_skips_foreign_source_bars_after_entry_fill_in_same_tick() -> None:
    entry_bar = _bar(
        source=REST_BACKFILL_BAR_SOURCE,
        opened_at=NOW + timedelta(minutes=1),
        price=Decimal("100"),
    )
    prev_bar = _bar(
        source=REST_BACKFILL_BAR_SOURCE,
        opened_at=NOW,
        price=Decimal("100"),
    )
    foreign_monitor_bar = _bar(
        source=LIVE_BAR_SOURCE,
        opened_at=NOW + timedelta(minutes=2),
        price=Decimal("100.2"),
    )
    monitored_bars: list[PaperBar] = []

    class _App:
        async def apply_entry_event(self, command: Any) -> Any:
            return SimpleNamespace(
                trade_id=uuid4(),
                persisted_fill_count=1,
                order=SimpleNamespace(status="FILLED"),
                position=SimpleNamespace(bar_source=command.bar.bar_source),
            )

        async def monitor_position(self, command: Any) -> Any:
            monitored_bars.append(command.bar)
            return SimpleNamespace(trade_closed=False)

    loop = PaperMarketEventLoop(application=_App(), sessions=None, workspace_id=WORKSPACE)

    async def _fake_bars(scope: Any, **kwargs: Any) -> tuple[PaperBar, ...]:
        return (entry_bar, foreign_monitor_bar)

    async def _fake_prev(scope: Any, **kwargs: Any) -> PaperBar:
        return prev_bar

    async def _fake_funding(scope: Any, **kwargs: Any) -> tuple[Any, ...]:
        return ()

    loop._bars = _fake_bars  # type: ignore[method-assign]
    loop._previous_bar = _fake_prev  # type: ignore[method-assign]
    loop._funding_moments = _fake_funding  # type: ignore[method-assign]

    account = SimpleNamespace(
        account_id=ACCOUNT_ID,
        bot_id=BOT_ID,
        gateway=SimpleNamespace(
            entries_paused=False,
            order=lambda _pid: SimpleNamespace(eligible_at=NOW + timedelta(minutes=1)),
        ),
    )
    scope = _Scope(
        intent_id=uuid4(),
        paper_intent_id="paper:idem-1",
        symbol="BTCUSDT",
        timeframe="1m",
        feature_snapshot_id="f" * 64,
        trade_id=None,
        entry_pending=True,
        cursor=None,
    )
    asyncio.run(loop._process_scope(account, scope, NOW + timedelta(minutes=5)))
    assert monitored_bars == []


def test_context_provider_passes_pinned_source_to_monitor_feature_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_sources: list[str | None] = []

    async def _fake_load_bars(
        session: Any,
        *,
        workspace_id: UUID,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        max_bars: int = 768,
        pinned_source: str | None = None,
    ) -> tuple[Any, ...]:
        seen_sources.append(pinned_source)
        return ()

    monkeypatch.setattr(
        "trading_bot.application.paper_market_context.load_closed_market_bars",
        _fake_load_bars,
    )
    provider = PostgresPaperDecisionContextProvider(sessions=None, workspace_id=WORKSPACE)
    bar = _bar(
        source="CRYPIDIAN_PIPELINE_FIXTURE",
        opened_at=NOW,
        price=Decimal("100"),
    )
    position = SimpleNamespace(
        bar_source="CRYPIDIAN_PIPELINE_FIXTURE",
        entry_bar_key=bar.bar_key,
        direction="long",
        recipe=None,
        is_closed=False,
        quantity=Decimal("1"),
    )
    asyncio.run(
        provider.load_monitor_evidence(
            uow=SimpleNamespace(session=object()),
            command=SimpleNamespace(bar=bar, feature_snapshot_id="f" * 64),
            published_settings=SimpleNamespace(workspace_id=WORKSPACE),
            execution_profile=None,
            account=SimpleNamespace(account_id=ACCOUNT_ID),
            position=position,
            pinned_strategy_release_name="h03-v1",
            pinned_strategy_release_hash="a" * 64,
            pinned_strategy_settings_version_id=None,
            pinned_strategy_parameters=(),
        )
    )
    assert seen_sources == ["CRYPIDIAN_PIPELINE_FIXTURE"]


# --- 4. Receipt từ chối candidate terminal (tránh đánh giá lại mỗi tick) -------------------------


def test_pending_candidate_decisions_excludes_terminal_rejection_receipts() -> None:
    session = _RecordingScalarsSession()
    asyncio.run(
        BotDecisionRepository(session).pending_candidate_decisions(  # type: ignore[arg-type]
            workspace_id=WORKSPACE,
            bot_id=BOT_ID,
            account_id=ACCOUNT_ID,
            now=NOW,
        )
    )
    sql = session.statements[0]
    assert "paper_candidate_outcome" in sql
    assert "paper_candidate_outcome.decision_id = bot_decision.id" in sql
    assert "DATABASE_UNAVAILABLE" in sql
    assert "LEASE_EXPIRED" in sql


def test_submit_bot_candidates_records_terminal_error_but_not_transient_error() -> None:
    _LOGGED_CANDIDATE_REJECTIONS.clear()
    recorded: list[tuple[UUID, str, str]] = []

    async def _record(decision: Any, stage: str, reason_code: str) -> None:
        recorded.append((decision.decision_id, stage, reason_code))

    terminal_decision = SimpleNamespace(
        decision_id=uuid4(),
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        status=BotDecisionStatus.CANDIDATE,
    )
    transient_decision = SimpleNamespace(
        decision_id=uuid4(),
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        status=BotDecisionStatus.CANDIDATE,
    )

    class _App:
        async def evaluate_and_submit(self, command: Any) -> Any:
            if command.bot_decision_id == terminal_decision.decision_id:
                raise PaperRuntimeApplicationError("PAPER_EQUITY_UNKNOWN")
            raise PaperRuntimeApplicationError("DATABASE_UNAVAILABLE")

    asyncio.run(_submit_bot_candidates(_App(), (terminal_decision, transient_decision), _record))
    assert recorded == [(terminal_decision.decision_id, "ERROR", "PAPER_EQUITY_UNKNOWN")]
