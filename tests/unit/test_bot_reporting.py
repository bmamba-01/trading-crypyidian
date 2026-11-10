"""MB-06: báo cáo theo bot/tổng hợp workspace — tính toán ledger, lineage, hash và ủy quyền."""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
from trading_bot.application import bot_reporting
from trading_bot.application.bot_reporting import (
    BotReportError,
    BotReportingService,
    BotReportQuery,
    ReportScope,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.portfolio.reporting import (
    MAX_BOTS_PER_WORKSPACE_REPORT,
    MAX_OPEN_TRADES_PER_BOT,
    MAX_TRADES_PER_BOT,
    REPORT_VERSION,
    BotLedgerSnapshot,
    DecisionLineage,
    IntentLineage,
    LedgerCost,
    LedgerFill,
    LedgerTrade,
    PositionTransition,
    ReportBot,
    ReportIntegrityError,
    build_bot_report,
    build_workspace_report,
    canonical_decimal,
)

W1 = UUID("00000000-0000-4000-8000-000000000001")
W2 = UUID("00000000-0000-4000-8000-000000000002")
BOT_A = UUID("00000000-0000-4000-8000-0000000000a1")
BOT_B = UUID("00000000-0000-4000-8000-0000000000b1")
BOT_X = UUID("00000000-0000-4000-8000-0000000000c1")  # bot của W2
ACC_A = UUID("00000000-0000-4000-8000-0000000000a2")
ACC_B = UUID("00000000-0000-4000-8000-0000000000b2")
ACC_X = UUID("00000000-0000-4000-8000-0000000000c2")
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 2, tzinfo=UTC)
SETTINGS_V1 = UUID("00000000-0000-4000-8000-00000000f001")
RELEASE_H03 = UUID("00000000-0000-4000-8000-00000000f003")
MODEL_M1 = UUID("00000000-0000-4000-8000-00000000f0a1")


def make_trade(
    *,
    bot_id: UUID = BOT_A,
    account_id: UUID = ACC_A,
    workspace_id: UUID = W1,
    closed_at: datetime | None = START + timedelta(hours=1),
    opened_at: datetime | None = None,
    gross: str = "10",
    fee: str = "0.5",
    funding: str = "0",
    slippage: str = "0.1",
    quote: str = "USDT",
    candidate: str | None = None,
    trade_id: UUID | None = None,
) -> LedgerTrade:
    with localcontext(prec=76):  # như DB Numeric(38,18): không làm tròn dữ liệu test
        total = Decimal(fee) + Decimal(funding) + Decimal(slippage)
        net = Decimal(gross) - total
    return LedgerTrade(
        trade_id=trade_id or uuid4(),
        intent_id=uuid4(),
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        status="CLOSED" if closed_at is not None else "OPEN",
        symbol="BTCUSDT",
        side="LONG",
        quote_asset=quote,
        opened_at=opened_at or (START if closed_at is None else closed_at - timedelta(minutes=30)),
        closed_at=closed_at,
        gross_pnl=Decimal(gross) if closed_at is not None else Decimal(0),
        fee_total=Decimal(fee) if closed_at is not None else Decimal(0),
        funding_total=Decimal(funding) if closed_at is not None else Decimal(0),
        slippage_total=Decimal(slippage) if closed_at is not None else Decimal(0),
        total_cost=total if closed_at is not None else Decimal(0),
        net_pnl=net if closed_at is not None else Decimal(0),
        settings_version_id=SETTINGS_V1,
        strategy_release_id=RELEASE_H03,
        strategy_release_hash="a" * 64,
        feature_snapshot_id="f" * 64,
        candidate_id=candidate or uuid4().hex + uuid4().hex,
        risk_decision_id=uuid4(),
        risk_reservation_id=uuid4(),
        lineage={"plan_hash": "b" * 64, "paper_execution_profile_hash": "e" * 64},
    )


def costs_for(trade: LedgerTrade, *, split_fee: bool = True) -> list[LedgerCost]:
    """Chi phí ledger khớp cột tổng; fee tách hai dòng (entry/exit) như ledger thật."""
    rows: list[LedgerCost] = []

    def add(kind: str, amount: Decimal) -> None:
        rows.append(
            LedgerCost(
                trade_id=trade.trade_id,
                workspace_id=trade.workspace_id,
                bot_id=trade.bot_id,
                cost_type=kind,
                amount=amount,
                currency=trade.quote_asset,
            )
        )

    if split_fee:
        half = trade.fee_total / 2
        add("FEE", half)
        add("FEE", trade.fee_total - half)
    else:
        add("FEE", trade.fee_total)
    add("SLIPPAGE", trade.slippage_total)
    if trade.funding_total:
        add("FUNDING", trade.funding_total)
    return rows


def snapshot(
    trades: list[LedgerTrade],
    *,
    bot_id: UUID = BOT_A,
    account_id: UUID = ACC_A,
    workspace_id: UUID = W1,
    open_trades: list[LedgerTrade] | None = None,
    costs: list[LedgerCost] | None = None,
    intents: list[IntentLineage] | None = None,
    decisions: list[DecisionLineage] | None = None,
    fills: list[LedgerFill] | None = None,
    position_events: list[PositionTransition] | None = None,
) -> BotLedgerSnapshot:
    return BotLedgerSnapshot(
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        closed_trades=tuple(trades),
        open_trades=tuple(open_trades or ()),
        costs=tuple(costs if costs is not None else [c for t in trades for c in costs_for(t)]),
        intents=tuple(intents or ()),
        decisions=tuple(decisions or ()),
        fills=tuple(fills or ()),
        position_events=tuple(position_events or ()),
    )


def report_for(snap: BotLedgerSnapshot) -> Any:
    return build_bot_report(snapshot=snap, start=START, end=END)


# --- Tính toán ledger -------------------------------------------------------------------


def test_bot_report_sums_ledger_columns_exactly() -> None:
    hour = timedelta(hours=1)
    trades = [
        make_trade(
            gross="12.345678901234567891", fee="0.1", slippage="0.02", closed_at=START + hour
        ),
        make_trade(
            gross="-3.5", fee="0.2", funding="-0.07", slippage="0.03", closed_at=START + 2 * hour
        ),
        make_trade(
            gross="0.33", fee="0.03", funding="0.3", slippage="0", closed_at=START + 3 * hour
        ),
    ]

    report = report_for(snapshot(trades))

    usdt = report.totals["USDT"]
    assert usdt.closed_trade_count == 3
    assert usdt.gross_pnl == sum((t.gross_pnl for t in trades), Decimal(0))
    assert usdt.fee_total == Decimal("0.33")
    assert usdt.funding_total == Decimal("0.23")
    assert usdt.slippage_total == Decimal("0.05")
    assert usdt.total_cost == Decimal("0.61")
    assert usdt.net_pnl == sum((t.net_pnl for t in trades), Decimal(0))
    assert usdt.net_pnl == usdt.gross_pnl - usdt.total_cost
    # Trade 3: gross 0.33 = fee 0.03 + funding 0.3 → net đúng 0 (hòa vốn).
    assert (usdt.winning_trade_count, usdt.losing_trade_count, usdt.breakeven_trade_count) == (
        1,
        1,
        1,
    )
    payload = report.as_dict()
    assert payload["totals"]["USDT"]["net_pnl"] == canonical_decimal(usdt.net_pnl)
    assert [row["trade_id"] for row in payload["trades"]] == [str(t.trade_id) for t in trades]
    assert payload["reconciliation"] == {"closed_trade_rows": 3, "trade_cost_rows": 11}


def test_v2_trader_analytics_use_net_results_and_closed_trade_order() -> None:
    outcomes = (("10", 30), ("12", 60), ("-4", 90), ("-6", 120), ("0", 180), ("2", 240))
    trades = [
        make_trade(
            gross=net,
            fee="0",
            slippage="0",
            opened_at=START + timedelta(hours=index + 1) - timedelta(minutes=minutes),
            closed_at=START + timedelta(hours=index + 1),
            trade_id=UUID(f"00000000-0000-4000-8000-{index + 1:012d}"),
        )
        for index, (net, minutes) in enumerate(outcomes)
    ]

    report = report_for(snapshot(trades))
    totals = report.totals["USDT"]
    values = report.as_dict()["totals"]["USDT"]

    assert REPORT_VERSION == "bot-report.v2"
    assert totals.win_rate_percent == Decimal("50.00000000")
    assert totals.profit_factor == Decimal("2.40000000")
    assert totals.expectancy_per_trade == Decimal("2.33333333")
    assert totals.average_winning_trade == Decimal("8.00000000")
    assert totals.average_losing_trade == Decimal("-5.00000000")
    assert totals.max_consecutive_wins == 2
    assert totals.max_consecutive_losses == 2
    assert totals.average_holding_seconds == 7200
    assert values["win_rate_percent"] == "50"
    assert values["profit_factor"] == "2.4"
    assert values["expectancy_per_trade"] == "2.33333333"


def test_v2_daily_performance_uses_vietnam_dates_and_keeps_quote_assets_separate() -> None:
    trades = [
        make_trade(
            gross="11",
            fee="1",
            slippage="0",
            closed_at=START + timedelta(hours=16, minutes=30),
            quote="USDT",
        ),
        make_trade(
            gross="-5",
            fee="0",
            slippage="0",
            closed_at=START + timedelta(hours=17, minutes=30),
            quote="USDT",
        ),
        make_trade(
            gross="4",
            fee="0",
            slippage="0",
            closed_at=START + timedelta(hours=16, minutes=45),
            quote="USDC",
        ),
    ]

    daily = report_for(snapshot(trades)).as_dict()["daily_performance"]

    assert daily == {
        "timezone": "Asia/Ho_Chi_Minh",
        "quote_assets": {
            "USDC": [
                {
                    "date": "2026-10-01",
                    "net_pnl": "4",
                    "closed_trade_count": 1,
                    "winning_trade_count": 1,
                    "losing_trade_count": 0,
                    "breakeven_trade_count": 0,
                }
            ],
            "USDT": [
                {
                    "date": "2026-10-01",
                    "net_pnl": "10",
                    "closed_trade_count": 1,
                    "winning_trade_count": 1,
                    "losing_trade_count": 0,
                    "breakeven_trade_count": 0,
                },
                {
                    "date": "2026-10-02",
                    "net_pnl": "-5",
                    "closed_trade_count": 1,
                    "winning_trade_count": 0,
                    "losing_trade_count": 1,
                    "breakeven_trade_count": 0,
                },
            ],
        },
    }


def test_v1_report_payload_keeps_legacy_totals_shape_and_v2_adds_metrics() -> None:
    trade = make_trade(gross="4", fee="0", slippage="0")
    original = report_for(snapshot([trade])).as_dict()["totals"]["USDT"]
    legacy = build_bot_report(
        snapshot=snapshot([trade]),
        start=START,
        end=END,
        report_version="bot-report.v1",
    ).as_dict()["totals"]["USDT"]

    assert "profit_factor" in original
    assert "profit_factor" not in legacy
    assert "daily_performance" not in legacy
    assert legacy["closed_trade_count"] == 1


def test_profit_factor_and_average_outcomes_are_undefined_without_denominator() -> None:
    winners = [
        make_trade(gross="3", fee="0", slippage="0"),
        make_trade(gross="2", fee="0", slippage="0", closed_at=START + timedelta(hours=2)),
    ]
    metrics = report_for(snapshot(winners)).totals["USDT"]

    assert metrics.profit_factor is None
    assert metrics.average_losing_trade is None
    assert metrics.average_winning_trade == Decimal("2.50000000")


def test_closed_trade_with_negative_holding_duration_is_rejected() -> None:
    trade = make_trade(opened_at=START + timedelta(hours=2), closed_at=START + timedelta(hours=1))

    with pytest.raises(ReportIntegrityError, match="đóng trước khi mở"):
        report_for(snapshot([trade]))


def test_drawdown_tracks_peak_and_trough_trade_lineage() -> None:
    nets = ["10", "-4", "2", "-9", "1"]
    trades = [
        make_trade(gross=net, fee="0", slippage="0", closed_at=START + timedelta(hours=i + 1))
        for i, net in enumerate(nets)
    ]

    usdt = report_for(snapshot(trades)).totals["USDT"]

    # Tích lũy 10, 6, 8, -1, 0 → đỉnh 10 (trade 1), đáy -1 (trade 4), sụt 11.
    assert usdt.max_drawdown == Decimal("11")
    assert usdt.drawdown_peak_trade_id == trades[0].trade_id
    assert usdt.drawdown_trough_trade_id == trades[3].trade_id


def test_drawdown_starts_from_zero_and_handles_empty_window() -> None:
    losing_first = [
        make_trade(gross="-2", fee="0", slippage="0", closed_at=START + timedelta(hours=1)),
        make_trade(gross="5", fee="0", slippage="0", closed_at=START + timedelta(hours=2)),
    ]
    usdt = report_for(snapshot(losing_first)).totals["USDT"]
    assert usdt.max_drawdown == Decimal(2)
    assert usdt.drawdown_peak_trade_id is None  # đỉnh là mốc 0 đầu cửa sổ
    assert usdt.drawdown_trough_trade_id == losing_first[0].trade_id

    empty = report_for(snapshot([]))
    assert empty.totals == {}
    assert empty.as_dict()["trades"] == []
    assert empty.as_dict()["daily_performance"] == {
        "timezone": "Asia/Ho_Chi_Minh",
        "quote_assets": {},
    }


def test_window_is_half_open_on_closed_at() -> None:
    at_start = make_trade(closed_at=START)
    report = report_for(snapshot([at_start]))
    assert report.totals["USDT"].closed_trade_count == 1

    at_end = make_trade(closed_at=END)
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([at_end]))
    before = make_trade(closed_at=START - timedelta(microseconds=1))
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([before]))


def test_rows_of_another_bot_account_or_workspace_are_rejected() -> None:
    for foreign in (
        make_trade(bot_id=BOT_B, account_id=ACC_B),
        make_trade(account_id=ACC_B),
        make_trade(workspace_id=W2),
    ):
        with pytest.raises(ReportIntegrityError):
            report_for(snapshot([foreign]))
    trade = make_trade()
    foreign_cost = replace(costs_for(trade)[0], bot_id=BOT_B)
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([trade], costs=[foreign_cost, *costs_for(trade)[1:]]))


def test_cost_rows_must_reconcile_with_trade_totals() -> None:
    trade = make_trade(fee="0.5", slippage="0.1")
    short = costs_for(trade, split_fee=False)
    short[0] = replace(short[0], amount=Decimal("0.4"))
    with pytest.raises(ReportIntegrityError, match="trade_cost"):
        report_for(snapshot([trade], costs=short))

    wrong_currency = [replace(row, currency="USDC") for row in costs_for(trade)]
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([trade], costs=wrong_currency))


def test_inconsistent_trade_row_is_rejected_not_recomputed() -> None:
    trade = replace(make_trade(), net_pnl=Decimal("999"))
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([trade], costs=costs_for(make_trade())))
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([replace(make_trade(), status="OPEN")]))


def test_report_carries_config_strategy_model_lineage() -> None:
    candidate = "c" * 64
    trade = make_trade(candidate=candidate)
    config_version = uuid4()
    decision = DecisionLineage(
        decision_id=uuid4(),
        bot_id=BOT_A,
        candidate_id=candidate,
        decision_hash="d" * 64,
        bot_config_version_id=config_version,
        bot_config_hash="9" * 64,
        root_settings_version_id=SETTINGS_V1,
        model_release_id=MODEL_M1,
    )
    intent = IntentLineage(
        intent_id=trade.intent_id,
        bot_id=BOT_A,
        evidence={
            "bot_config_version_id": str(config_version),
            "bot_config_hash": "9" * 64,
            "effective_config_hash": "8" * 64,
            "settings_hash": "7" * 64,
            "ignored_key": "x",
        },
    )

    payload = report_for(snapshot([trade], intents=[intent], decisions=[decision])).as_dict()

    lineage = payload["trades"][0]["lineage"]
    assert lineage == {
        "settings_version_id": str(SETTINGS_V1),
        "settings_hash": "7" * 64,
        "strategy_release_id": str(RELEASE_H03),
        "strategy_release_hash": "a" * 64,
        "feature_snapshot_id": "f" * 64,
        "candidate_id": candidate,
        "risk_decision_id": str(trade.risk_decision_id),
        "risk_reservation_id": str(trade.risk_reservation_id),
        "plan_hash": "b" * 64,
        "paper_execution_profile_hash": "e" * 64,
        "bot_config_version_id": str(config_version),
        "bot_config_hash": "9" * 64,
        "effective_config_hash": "8" * 64,
        "bot_decision_ids": [str(decision.decision_id)],
        "model_release_ids": [str(MODEL_M1)],
    }
    summary = payload["lineage"]
    assert summary["settings_version_ids"] == [str(SETTINGS_V1)]
    assert summary["strategy_releases"] == [{"id": str(RELEASE_H03), "hash": "a" * 64}]
    assert summary["bot_config_versions"] == [{"id": str(config_version), "hash": "9" * 64}]
    assert summary["effective_config_hashes"] == ["8" * 64]
    assert summary["model_release_ids"] == [str(MODEL_M1)]
    assert summary["trades_without_bot_config_lineage"] == 0


def test_report_explains_trade_entry_and_exit_from_persisted_evidence() -> None:
    trade = make_trade(candidate="c" * 64)
    decision = DecisionLineage(
        decision_id=uuid4(),
        bot_id=BOT_A,
        candidate_id=trade.candidate_id,
        decision_hash="d" * 64,
        bot_config_version_id=uuid4(),
        bot_config_hash="9" * 64,
        root_settings_version_id=SETTINGS_V1,
        model_release_id=None,
        candidate={
            "strategy_id": "H03",
            "reason_code": "DONCHIAN_BREAKOUT_CONFIRMED",
            "detail_vi": "Nến đóng cửa vượt đỉnh kênh Donchian.",
            "reference_price": "101.25",
            "signal_at": "2026-10-01T00:15:00+00:00",
        },
    )
    opened_at = trade.opened_at
    closed_at = trade.closed_at
    assert closed_at is not None
    fills = [
        LedgerFill(
            fill_key="entry-fill-1",
            intent_id=trade.intent_id,
            workspace_id=trade.workspace_id,
            bot_id=trade.bot_id,
            account_id=trade.account_id,
            role="ENTRY",
            quantity=Decimal("0.2"),
            price=Decimal("100.5"),
            occurred_at=opened_at,
            fee_amount=Decimal("0.25"),
            slippage_amount=Decimal("0.05"),
            bar_key="entry-bar",
        ),
        LedgerFill(
            fill_key="exit-fill-1",
            intent_id=trade.intent_id,
            workspace_id=trade.workspace_id,
            bot_id=trade.bot_id,
            account_id=trade.account_id,
            role="EXIT",
            quantity=Decimal("0.2"),
            price=Decimal("110.5"),
            occurred_at=closed_at,
            fee_amount=Decimal("0.25"),
            slippage_amount=Decimal("0.05"),
            bar_key="exit-bar",
        ),
    ]
    position_events = [
        PositionTransition(
            event_key="exit-fill:exit-fill-1",
            intent_id=trade.intent_id,
            workspace_id=trade.workspace_id,
            bot_id=trade.bot_id,
            account_id=trade.account_id,
            event_type="CLOSED",
            occurred_at=closed_at,
            payload={
                "fill_key": "exit-fill-1",
                "exit_reason": "STOP_LOSS",
                "bar_key": "exit-bar",
            },
        )
    ]

    payload = report_for(
        snapshot([trade], decisions=[decision], fills=fills, position_events=position_events)
    ).as_dict()

    row = payload["trades"][0]
    assert row["entry_reason"] == {
        "strategy_id": "H03",
        "reason_code": "DONCHIAN_BREAKOUT_CONFIRMED",
        "detail_vi": "Nến đóng cửa vượt đỉnh kênh Donchian.",
        "reference_price": "101.25",
        "signal_at": "2026-10-01T00:15:00+00:00",
    }
    assert row["exit_reason"] == "STOP_LOSS"
    assert row["fills"] == [
        {
            "fill_key": "entry-fill-1",
            "role": "ENTRY",
            "position_event_type": None,
            "quantity": "0.2",
            "price": "100.5",
            "occurred_at": opened_at.isoformat(),
            "fee_amount": "0.25",
            "slippage_amount": "0.05",
            "bar_key": "entry-bar",
        },
        {
            "fill_key": "exit-fill-1",
            "role": "EXIT",
            "position_event_type": "CLOSED",
            "quantity": "0.2",
            "price": "110.5",
            "occurred_at": closed_at.isoformat(),
            "fee_amount": "0.25",
            "slippage_amount": "0.05",
            "bar_key": "exit-bar",
        },
    ]


def test_report_marks_missing_trade_explanation_without_guessing() -> None:
    payload = report_for(snapshot([make_trade()])).as_dict()
    row = payload["trades"][0]
    assert row["entry_reason"] is None
    assert row["exit_reason"] is None
    assert row["fills"] == []


def test_report_distinguishes_partial_reduction_from_final_close_fill() -> None:
    trade = make_trade(candidate="c" * 64)
    closed_at = trade.closed_at
    assert closed_at is not None
    fills = [
        LedgerFill(
            fill_key="exit-partial",
            intent_id=trade.intent_id,
            workspace_id=trade.workspace_id,
            bot_id=trade.bot_id,
            account_id=trade.account_id,
            role="EXIT",
            quantity=Decimal("0.1"),
            price=Decimal("105"),
            occurred_at=closed_at - timedelta(minutes=1),
            fee_amount=Decimal("0.1"),
            slippage_amount=Decimal("0.02"),
            bar_key="partial-bar",
        ),
        LedgerFill(
            fill_key="exit-final",
            intent_id=trade.intent_id,
            workspace_id=trade.workspace_id,
            bot_id=trade.bot_id,
            account_id=trade.account_id,
            role="EXIT",
            quantity=Decimal("0.1"),
            price=Decimal("110"),
            occurred_at=closed_at,
            fee_amount=Decimal("0.1"),
            slippage_amount=Decimal("0.02"),
            bar_key="final-bar",
        ),
    ]
    events = [
        PositionTransition(
            event_key="exit-fill:exit-partial",
            intent_id=trade.intent_id,
            workspace_id=trade.workspace_id,
            bot_id=trade.bot_id,
            account_id=trade.account_id,
            event_type="REDUCED",
            occurred_at=closed_at - timedelta(minutes=1),
            payload={"fill_key": "exit-partial"},
        ),
        PositionTransition(
            event_key="exit-fill:exit-final",
            intent_id=trade.intent_id,
            workspace_id=trade.workspace_id,
            bot_id=trade.bot_id,
            account_id=trade.account_id,
            event_type="CLOSED",
            occurred_at=closed_at,
            payload={"fill_key": "exit-final", "exit_reason": "TAKE_PROFIT"},
        ),
    ]

    row = report_for(snapshot([trade], fills=fills, position_events=events)).as_dict()["trades"][0]

    assert [fill["position_event_type"] for fill in row["fills"]] == ["REDUCED", "CLOSED"]


def test_missing_lineage_sources_stay_null_without_guessing() -> None:
    trade = make_trade()
    # Decision của bot khác cùng candidate không được gắn vào bot này.
    foreign = DecisionLineage(
        decision_id=uuid4(),
        bot_id=BOT_B,
        candidate_id=trade.candidate_id,
        decision_hash="d" * 64,
        bot_config_version_id=uuid4(),
        bot_config_hash="9" * 64,
        root_settings_version_id=SETTINGS_V1,
        model_release_id=MODEL_M1,
    )
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([trade], decisions=[foreign]))

    payload = report_for(snapshot([trade])).as_dict()
    lineage = payload["trades"][0]["lineage"]
    assert lineage["bot_config_version_id"] is None
    assert lineage["effective_config_hash"] is None
    assert lineage["bot_decision_ids"] == []
    assert lineage["model_release_ids"] == []
    assert payload["lineage"]["trades_without_bot_config_lineage"] == 1


def test_report_hash_is_deterministic_and_order_independent() -> None:
    trades = [make_trade(closed_at=START + timedelta(hours=i + 1)) for i in range(5)]
    first = report_for(snapshot(trades))
    shuffled = trades[:]
    random.Random(7).shuffle(shuffled)
    costs = [c for t in shuffled for c in costs_for(t)]
    second = report_for(snapshot(shuffled, costs=costs[::-1]))

    assert first.report_hash == second.report_hash
    assert len(first.report_hash) == 64
    assert first.as_dict()["report_hash"] == first.report_hash
    assert first.as_dict() == second.as_dict()

    # Decimal khác scale nhưng cùng giá trị (DB trả 18 chữ số thập phân) → cùng hash.
    scaled = [
        replace(
            t,
            gross_pnl=t.gross_pnl.quantize(Decimal("1E-18")),
            net_pnl=t.net_pnl.quantize(Decimal("1E-18")),
        )
        for t in trades
    ]
    assert report_for(snapshot(scaled)).report_hash == first.report_hash

    changed = [replace(trades[0], gross_pnl=trades[0].gross_pnl + 1, net_pnl=trades[0].net_pnl + 1)]
    assert report_for(snapshot(changed + trades[1:])).report_hash != first.report_hash


def test_totals_are_grouped_per_quote_asset_without_conversion() -> None:
    usdt = make_trade(gross="10", quote="USDT")
    usdc = make_trade(gross="7", quote="USDC")

    totals = report_for(snapshot([usdt, usdc])).totals

    assert set(totals) == {"USDT", "USDC"}
    assert totals["USDT"].net_pnl == usdt.net_pnl
    assert totals["USDC"].net_pnl == usdc.net_pnl


def test_open_trades_at_end_are_listed_without_pnl() -> None:
    open_now = make_trade(closed_at=None, opened_at=START + timedelta(hours=3))
    closed_after = make_trade(
        closed_at=END + timedelta(hours=1), opened_at=END - timedelta(hours=1)
    )
    closed_after = replace(closed_after, status="CLOSED")

    payload = report_for(snapshot([], open_trades=[open_now, closed_after])).as_dict()

    assert payload["open_trades"] == sorted(
        (
            {
                "trade_id": str(t.trade_id),
                "symbol": "BTCUSDT",
                "side": "LONG",
                "quote_asset": "USDT",
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            }
            for t in (open_now, closed_after)
        ),
        key=lambda row: (row["opened_at"], row["trade_id"]),
    )
    late = make_trade(closed_at=None, opened_at=END)
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([], open_trades=[late]))
    closed_inside = make_trade(closed_at=END - timedelta(minutes=1))
    with pytest.raises(ReportIntegrityError):
        report_for(snapshot([], open_trades=[closed_inside]))


def test_workspace_report_sums_bots_and_reconciles_with_ledger() -> None:
    a_trades = [
        make_trade(gross="5", fee="0", slippage="0", closed_at=START + timedelta(hours=1)),
        make_trade(gross="-8", fee="0", slippage="0", closed_at=START + timedelta(hours=3)),
    ]
    b_trades = [
        make_trade(
            bot_id=BOT_B,
            account_id=ACC_B,
            gross="4",
            fee="0",
            slippage="0",
            closed_at=START + timedelta(hours=2),
        )
    ]
    report_a = report_for(snapshot(a_trades))
    report_b = report_for(snapshot(b_trades, bot_id=BOT_B, account_id=ACC_B))
    ledger = {"USDT": (3, Decimal(1))}

    aggregate = build_workspace_report(
        workspace_id=W1, start=START, end=END, bot_reports=[report_a, report_b], ledger=ledger
    )

    usdt = aggregate.totals["USDT"]
    assert usdt.closed_trade_count == 3
    assert usdt.net_pnl == report_a.totals["USDT"].net_pnl + report_b.totals["USDT"].net_pnl
    assert usdt.net_pnl == Decimal(1)
    # Chuỗi gộp: +5, +4 (B), -8 → tích lũy 5, 9, 1 → sụt 8 từ trade B tới trade A thứ hai.
    assert usdt.max_drawdown == Decimal(8)
    assert usdt.drawdown_peak_trade_id == b_trades[0].trade_id
    assert usdt.drawdown_trough_trade_id == a_trades[1].trade_id
    payload = aggregate.as_dict()
    assert payload["scope"] == "workspace"
    assert payload["daily_performance"]["quote_assets"]["USDT"] == [
        {
            "date": "2026-10-01",
            "net_pnl": "1",
            "closed_trade_count": 3,
            "winning_trade_count": 2,
            "losing_trade_count": 1,
            "breakeven_trade_count": 0,
        }
    ]
    assert [row["bot_id"] for row in payload["bots"]] == sorted([str(BOT_A), str(BOT_B)])
    assert {row["bot_id"]: row["report_hash"] for row in payload["bots"]} == {
        str(BOT_A): report_a.report_hash,
        str(BOT_B): report_b.report_hash,
    }
    reordered = build_workspace_report(
        workspace_id=W1, start=START, end=END, bot_reports=[report_b, report_a], ledger=ledger
    )
    assert reordered.report_hash == aggregate.report_hash


def test_workspace_report_rejects_ledger_mismatch_or_foreign_bot_report() -> None:
    report_a = report_for(snapshot([make_trade(gross="5", fee="0", slippage="0")]))
    with pytest.raises(ReportIntegrityError):
        build_workspace_report(
            workspace_id=W1,
            start=START,
            end=END,
            bot_reports=[report_a],
            ledger={"USDT": (2, Decimal(5))},
        )
    with pytest.raises(ReportIntegrityError):
        build_workspace_report(
            workspace_id=W1,
            start=START,
            end=END,
            bot_reports=[report_a],
            ledger={"USDT": (1, Decimal("5.01"))},
        )
    foreign = report_for(snapshot([], bot_id=BOT_X, account_id=ACC_X, workspace_id=W2))
    with pytest.raises(ReportIntegrityError):
        build_workspace_report(
            workspace_id=W1,
            start=START,
            end=END,
            bot_reports=[report_a, foreign],
            ledger={"USDT": (1, Decimal(5))},
        )


# --- Repository: bộ lọc workspace/bot/account và cửa sổ ---------------------------------


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: Any, _params: Any = None) -> Any:
        self.statements.append(str(statement.compile(compile_kwargs={"literal_binds": False})))

        class _Result:
            def all(self) -> list[Any]:
                return []

            def scalars(self) -> Any:
                return self

        return _Result()

    async def scalars(self, statement: Any) -> Any:
        self.statements.append(str(statement.compile()))

        class _Scalars:
            def all(self) -> list[Any]:
                return []

        return _Scalars()


@pytest.mark.asyncio
async def test_repository_queries_filter_scope_and_half_open_window() -> None:
    session = RecordingSession()
    repository = BotReportRepository(session)  # type: ignore[arg-type]

    loaded = await repository.load_bot_ledger(
        workspace_id=W1,
        bot_id=BOT_A,
        account_id=ACC_A,
        start=START,
        end=END,
        max_closed=10,
        max_open=5,
    )
    await repository.list_bots(workspace_id=W1, limit=3)
    await repository.workspace_closed_totals(workspace_id=W1, start=START, end=END)

    assert loaded.closed_trades == ()
    closed_sql = next(s for s in session.statements if "trade.status =" in s)
    for fragment in (
        "trade.workspace_id =",
        "trade.bot_id =",
        "trade.account_id =",
        "trade.closed_at >=",
        "trade.closed_at <",
        "ORDER BY trade.closed_at, trade.id",
        "LIMIT",
    ):
        assert fragment in closed_sql, fragment
    open_sql = next(s for s in session.statements if "trade.opened_at <" in s)
    assert "trade.bot_id =" in open_sql
    assert "trade.closed_at IS NULL" in open_sql
    assert "LIMIT" in open_sql  # cap trade mở (max_open + 1)
    bots_sql = next(s for s in session.statements if "FROM bot_instance" in s)
    assert "bot_instance.workspace_id =" in bots_sql
    assert "LIMIT" in bots_sql  # cap bot của báo cáo tổng hợp (limit + 1)
    totals_sql = session.statements[-1]
    assert "trade.workspace_id =" in totals_sql
    assert "GROUP BY trade.quote_asset" in totals_sql
    assert all("UPDATE" not in s and "INSERT" not in s for s in session.statements)


@pytest.mark.asyncio
async def test_repository_chart_bars_use_workspace_symbol_timeframe_and_asof() -> None:
    session = RecordingSession()
    repository = BotReportRepository(session)  # type: ignore[arg-type]

    await repository.load_trade_bars(
        workspace_id=W1,
        symbol="BTCUSDT",
        timeframe="1h",
        start=START,
        end=END,
        as_of=END + timedelta(hours=1),
        limit=1000,
    )

    sql = session.statements[-1]
    for fragment in (
        "market_bar.workspace_id =",
        "market_bar.symbol =",
        "market_bar.timeframe =",
        "market_bar.open_time >=",
        "market_bar.open_time <",
        "market_bar.available_at <=",
        "market_bar.is_closed IS true",
        "ORDER BY market_bar.open_time, market_bar.revision DESC",
        "LIMIT",
    ):
        assert fragment in sql, fragment
    assert "INSERT" not in sql
    assert "UPDATE" not in sql


class TransactionLogSession:
    """Session giả ghi thứ tự BEGIN/câu lệnh để kiểm tra snapshot được đặt trước truy vấn."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.info: dict[str, Any] = {}

    async def begin(self) -> None:
        self.log.append("BEGIN")

    async def execute(self, statement: Any, _params: Any = None) -> None:
        self.log.append(str(statement))

    async def commit(self) -> None:
        self.log.append("COMMIT")

    async def rollback(self) -> None:
        self.log.append("ROLLBACK")

    async def close(self) -> None:
        self.log.append("CLOSE")


@pytest.mark.asyncio
async def test_snapshot_unit_sets_repeatable_read_only_before_first_query() -> None:
    session = TransactionLogSession()

    async with UnitOfWork(
        lambda: session,  # type: ignore[arg-type]
        workspace_id=W1,
        snapshot_read_only=True,
    ):
        pass

    # Mức cô lập chỉ đổi được trước truy vấn đầu tiên (kể cả SELECT set_config của UoW).
    assert session.log[0] == "BEGIN"
    assert session.log[1] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert session.log[2].startswith("SELECT set_config('app.workspace_id'")


@pytest.mark.asyncio
async def test_default_unit_of_work_keeps_default_transaction_mode() -> None:
    session = TransactionLogSession()

    async with UnitOfWork(lambda: session, workspace_id=W1):  # type: ignore[arg-type]
        pass

    assert session.log[0] == "BEGIN"
    assert session.log[1].startswith("SELECT set_config('app.workspace_id'")
    assert not any("SET TRANSACTION" in entry for entry in session.log)


# --- Service: ma trận ủy quyền và scope tường minh ---------------------------------------


@dataclass
class Store:
    bots: dict[tuple[UUID, UUID], ReportBot] = field(default_factory=dict)
    snapshots: dict[UUID, BotLedgerSnapshot] = field(default_factory=dict)
    grants: set[tuple[UUID, UUID, str]] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    units: list[dict[str, Any]] = field(default_factory=list)
    ledger: dict[str, tuple[int, Decimal]] | None = None


STORE = Store()


class FakeUnitOfWork:
    def __init__(self, sessions: object, **kwargs: Any) -> None:
        del sessions
        STORE.units.append(kwargs)
        self.session = object()

    async def __aenter__(self) -> FakeUnitOfWork:
        STORE.calls.append("uow")
        return self

    async def __aexit__(self, *exc: object) -> None:
        STORE.calls.append("exit")


class FakeRepository:
    def __init__(self, session: object) -> None:
        del session

    async def get_bot(self, *, workspace_id: UUID, bot_id: UUID) -> ReportBot | None:
        STORE.calls.append(f"get_bot:{bot_id}")
        return STORE.bots.get((workspace_id, bot_id))

    async def list_bots(self, *, workspace_id: UUID, limit: int) -> list[ReportBot]:
        STORE.calls.append("list_bots")
        assert limit == MAX_BOTS_PER_WORKSPACE_REPORT + 1
        return sorted(
            (bot for (ws, _), bot in STORE.bots.items() if ws == workspace_id),
            key=lambda bot: str(bot.bot_id),
        )[:limit]

    async def load_bot_ledger(self, **kwargs: Any) -> BotLedgerSnapshot:
        STORE.calls.append(f"ledger:{kwargs['bot_id']}")
        assert kwargs["max_closed"] == MAX_TRADES_PER_BOT
        assert kwargs["max_open"] == MAX_OPEN_TRADES_PER_BOT
        return STORE.snapshots.get(kwargs["bot_id"]) or snapshot(
            [], bot_id=kwargs["bot_id"], account_id=kwargs["account_id"]
        )

    async def workspace_closed_totals(self, **kwargs: Any) -> dict[str, tuple[int, Decimal]]:
        STORE.calls.append("ledger_totals")
        if STORE.ledger is not None:
            return STORE.ledger
        totals: dict[str, tuple[int, Decimal]] = {}
        for snap in STORE.snapshots.values():
            if snap.workspace_id != kwargs["workspace_id"]:
                continue
            for trade in snap.closed_trades:
                count, net = totals.get(trade.quote_asset, (0, Decimal(0)))
                totals[trade.quote_asset] = (count + 1, net + trade.net_pnl)
        return totals


class StoreGrants(BotGrantRepository):
    """Dùng logic `require` thật; chỉ thay nguồn grant bằng bộ nhớ."""

    async def _active(self, **kwargs: Any) -> Any:  # type: ignore[override]
        key = (kwargs["bot_id"], kwargs["membership_id"], kwargs["permission"].value)
        return object() if key in STORE.grants else None


def principal(
    *roles: Role, workspace_id: UUID = W1, membership_id: UUID | None = None
) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=membership_id or uuid4(),
        workspace_id=workspace_id,
        roles=frozenset(roles),
        mfa_verified_at=datetime.now(UTC),
    )


ANALYST = principal(Role.ANALYST_REVIEWER)
OPERATOR = principal(Role.OPERATOR)
MULTI = principal(Role.OPERATOR, Role.ANALYST_REVIEWER)
OWNER = principal(Role.OWNER)
STRATEGY = principal(Role.STRATEGY_ADMIN)
RISK = principal(Role.RISK_ADMIN)
OWNER_W2 = principal(Role.OWNER, workspace_id=W2)


@pytest.fixture(autouse=True)
def fresh_store() -> None:
    reset_store()


def reset_store() -> Store:
    """Dữ liệu chuẩn giống QA dataset; test API dùng lại để đi qua service thật."""
    global STORE
    STORE = Store()
    STORE.bots = {
        (W1, BOT_A): ReportBot(workspace_id=W1, bot_id=BOT_A, account_id=ACC_A),
        (W1, BOT_B): ReportBot(workspace_id=W1, bot_id=BOT_B, account_id=ACC_B),
        (W2, BOT_X): ReportBot(workspace_id=W2, bot_id=BOT_X, account_id=ACC_X),
    }
    a_trade = make_trade(gross="5", fee="0.1", slippage="0.05")
    b_trade = make_trade(bot_id=BOT_B, account_id=ACC_B, gross="-2", fee="0.2", slippage="0")
    x_trade = make_trade(bot_id=BOT_X, account_id=ACC_X, workspace_id=W2, gross="100")
    STORE.snapshots = {
        BOT_A: snapshot([a_trade]),
        BOT_B: snapshot([b_trade], bot_id=BOT_B, account_id=ACC_B),
        BOT_X: snapshot([x_trade], bot_id=BOT_X, account_id=ACC_X, workspace_id=W2),
    }
    # Như QA dataset: analyst có bot:read + bot:report:read trên A; operator không có report.
    STORE.grants = {
        (BOT_A, ANALYST.membership_id, BotPermission.READ.value),
        (BOT_A, ANALYST.membership_id, BotPermission.REPORT_READ.value),
        (BOT_A, OPERATOR.membership_id, BotPermission.READ.value),
        (BOT_A, OPERATOR.membership_id, BotPermission.RUNTIME_PAUSE.value),
    }
    return STORE


def service() -> BotReportingService:
    return BotReportingService(
        object(),  # type: ignore[arg-type]
        unit_of_work=FakeUnitOfWork,
        repository=FakeRepository,
        grants=StoreGrants,
    )


def bot_query(bot_id: UUID | None, *, workspace_id: UUID = W1, **kwargs: Any) -> BotReportQuery:
    return BotReportQuery(workspace_id=workspace_id, bot_id=bot_id, start=START, end=END, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "bot_id", "allowed"),
    [
        (OWNER, BOT_A, True),
        (OWNER, BOT_B, True),
        (STRATEGY, BOT_B, True),
        (RISK, BOT_A, True),
        (ANALYST, BOT_A, True),
        (ANALYST, BOT_B, False),
        (OPERATOR, BOT_A, False),  # có bot:read/pause nhưng thiếu grant bot:report:read
        (MULTI, BOT_A, False),  # nhiều role không mượn quyền workspace
        (OWNER, uuid4(), False),  # bot không tồn tại
        (OWNER, BOT_X, False),  # bot thuộc workspace khác
    ],
)
async def test_bot_report_authorization_matrix(
    actor: Principal, bot_id: UUID, allowed: bool
) -> None:
    if allowed:
        report = await service().get_bot_report(bot_query(bot_id), actor)
        assert report.bot_id == bot_id
        assert report.as_dict()["trades"][0]["bot_id"] == str(bot_id)
        return
    with pytest.raises(BotReportError) as caught:
        await service().get_bot_report(bot_query(bot_id), actor)
    assert (caught.value.status_code, caught.value.code, caught.value.message) == (
        404,
        "REPORT_NOT_FOUND",
        bot_reporting.NOT_FOUND_MESSAGE,
    )
    assert not any(call.startswith("ledger:") for call in STORE.calls)


@pytest.mark.asyncio
async def test_query_for_other_workspace_is_not_found_before_touching_database() -> None:
    with pytest.raises(BotReportError) as caught:
        await service().get_bot_report(bot_query(BOT_X, workspace_id=W2), OWNER)
    assert caught.value.code == "REPORT_NOT_FOUND"
    assert STORE.units == []

    report = await service().get_bot_report(bot_query(BOT_X, workspace_id=W2), OWNER_W2)
    assert report.workspace_id == W2


@pytest.mark.asyncio
async def test_bot_report_runs_in_read_only_bot_scoped_unit() -> None:
    await service().get_bot_report(bot_query(BOT_A), ANALYST)

    # Một snapshot REPEATABLE READ, READ ONLY cho mọi SELECT của báo cáo (đặt khi mở UoW).
    assert STORE.units == [
        {
            "workspace_id": W1,
            "bot_id": BOT_A,
            "actor_membership_id": ANALYST.membership_id,
            "snapshot_read_only": True,
        }
    ]
    assert STORE.calls[0] == "uow"
    assert STORE.calls.index("uow") < STORE.calls.index(f"ledger:{BOT_A}")


@pytest.mark.asyncio
async def test_implicit_bot_omission_is_rejected() -> None:
    with pytest.raises(BotReportError) as caught:
        await service().get_bot_report(bot_query(None), OWNER)
    assert (caught.value.status_code, caught.value.code) == (422, "BOT_ID_REQUIRED")
    with pytest.raises(BotReportError) as caught:
        await service().get_bot_report(bot_query(BOT_A, scope=ReportScope.WORKSPACE), OWNER)
    assert caught.value.code == "REPORT_SCOPE_MISMATCH"
    assert STORE.units == []


@pytest.mark.asyncio
async def test_workspace_report_requires_explicit_scope() -> None:
    # Query mặc định (scope=bot, không bot) không bao giờ được hiểu là tổng hợp workspace.
    with pytest.raises(BotReportError) as caught:
        await service().get_workspace_report(bot_query(None), OWNER)
    assert (caught.value.status_code, caught.value.code) == (422, "WORKSPACE_SCOPE_REQUIRED")
    with pytest.raises(BotReportError) as caught:
        await service().get_workspace_report(bot_query(BOT_A, scope=ReportScope.WORKSPACE), OWNER)
    assert caught.value.code == "BOT_ID_NOT_ALLOWED"
    assert STORE.units == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [OWNER, STRATEGY, RISK])
async def test_workspace_report_for_workspace_report_roles(actor: Principal) -> None:
    report = await service().get_workspace_report(
        bot_query(None, scope=ReportScope.WORKSPACE), actor
    )

    payload = report.as_dict()
    assert [row["bot_id"] for row in payload["bots"]] == sorted([str(BOT_A), str(BOT_B)])
    expected = STORE.snapshots[BOT_A].closed_trades[0].net_pnl + (
        STORE.snapshots[BOT_B].closed_trades[0].net_pnl
    )
    assert report.totals["USDT"].net_pnl == expected
    # Ledger từng bot và tổng ledger đối soát đọc trong CÙNG một snapshot (một UoW).
    assert STORE.units == [
        {
            "workspace_id": W1,
            "bot_id": None,
            "actor_membership_id": actor.membership_id,
            "snapshot_read_only": True,
        }
    ]
    assert STORE.calls[0] == "uow"
    assert STORE.calls[-2:] == ["ledger_totals", "exit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [ANALYST, OPERATOR, MULTI])
async def test_workspace_report_forbidden_without_workspace_report_permission(
    actor: Principal,
) -> None:
    # Kể cả khi có grant bot:report:read trên mọi bot hiện có.
    for bot in (BOT_A, BOT_B):
        STORE.grants.add((bot, actor.membership_id, BotPermission.REPORT_READ.value))
    with pytest.raises(BotReportError) as caught:
        await service().get_workspace_report(bot_query(None, scope=ReportScope.WORKSPACE), actor)
    assert (caught.value.status_code, caught.value.code) == (403, "WORKSPACE_REPORT_FORBIDDEN")
    assert STORE.units == []


@pytest.mark.asyncio
async def test_workspace_report_other_workspace_is_not_found() -> None:
    with pytest.raises(BotReportError) as caught:
        await service().get_workspace_report(
            bot_query(None, workspace_id=W2, scope=ReportScope.WORKSPACE), OWNER
        )
    assert (caught.value.status_code, caught.value.code) == (404, "REPORT_NOT_FOUND")


@pytest.mark.asyncio
async def test_workspace_report_ledger_mismatch_fails_closed() -> None:
    STORE.ledger = {"USDT": (3, Decimal(0))}
    with pytest.raises(BotReportError) as caught:
        await service().get_workspace_report(bot_query(None, scope=ReportScope.WORKSPACE), OWNER)
    assert (caught.value.status_code, caught.value.code) == (409, "LEDGER_RECONCILIATION_FAILED")


@pytest.mark.asyncio
async def test_bot_report_integrity_error_maps_to_conflict() -> None:
    trade = STORE.snapshots[BOT_A].closed_trades[0]
    STORE.snapshots[BOT_A] = snapshot([replace(trade, bot_id=BOT_B)])
    with pytest.raises(BotReportError) as caught:
        await service().get_bot_report(bot_query(BOT_A), OWNER)
    assert (caught.value.status_code, caught.value.code) == (409, "LEDGER_RECONCILIATION_FAILED")


@pytest.mark.asyncio
async def test_report_too_large_is_rejected() -> None:
    trades = tuple(
        make_trade(closed_at=START + timedelta(seconds=i)) for i in range(MAX_TRADES_PER_BOT + 1)
    )
    STORE.snapshots[BOT_A] = replace(STORE.snapshots[BOT_A], closed_trades=trades)
    with pytest.raises(BotReportError) as caught:
        await service().get_bot_report(bot_query(BOT_A), OWNER)
    assert (caught.value.status_code, caught.value.code) == (422, "REPORT_TOO_LARGE")


@pytest.mark.asyncio
async def test_too_many_open_trades_is_rejected() -> None:
    opened = tuple(
        make_trade(closed_at=None, opened_at=START + timedelta(seconds=i))
        for i in range(MAX_OPEN_TRADES_PER_BOT + 1)
    )
    STORE.snapshots[BOT_A] = replace(STORE.snapshots[BOT_A], open_trades=opened)
    with pytest.raises(BotReportError) as caught:
        await service().get_bot_report(bot_query(BOT_A), OWNER)
    assert (caught.value.status_code, caught.value.code) == (422, "REPORT_TOO_MANY_OPEN_TRADES")


@pytest.mark.asyncio
async def test_workspace_report_caps_bot_count() -> None:
    for _ in range(MAX_BOTS_PER_WORKSPACE_REPORT):
        bot_id = uuid4()
        STORE.bots[(W1, bot_id)] = ReportBot(workspace_id=W1, bot_id=bot_id, account_id=uuid4())
    with pytest.raises(BotReportError) as caught:
        await service().get_workspace_report(bot_query(None, scope=ReportScope.WORKSPACE), OWNER)
    assert (caught.value.status_code, caught.value.code) == (422, "REPORT_TOO_MANY_BOTS")
    assert not any(call.startswith("ledger:") for call in STORE.calls)


def test_sums_use_exact_wide_decimal_context() -> None:
    # Numeric(38,18): 38 chữ số có nghĩa; context mặc định (prec 28) sẽ làm tròn âm thầm.
    big = "12345678901234567890.123456789012345678"
    trades = [
        make_trade(gross=big, fee="0", slippage="0", closed_at=START + timedelta(hours=1)),
        make_trade(gross=big, fee="0", slippage="0", closed_at=START + timedelta(hours=2)),
    ]
    usdt = report_for(snapshot(trades)).totals["USDT"]
    assert usdt.net_pnl == Decimal("24691357802469135780.246913578024691356")
    assert usdt.gross_pnl == usdt.net_pnl
    assert report_for(snapshot(trades)).as_dict()["totals"]["USDT"]["net_pnl"] == (
        "24691357802469135780.246913578024691356"
    )


def test_inexact_decimal_arithmetic_fails_closed() -> None:
    huge = make_trade(gross="1E+80", fee="0", slippage="0", closed_at=START + timedelta(hours=1))
    tiny = make_trade(
        gross="0.000000000000000001", fee="0", slippage="0", closed_at=START + timedelta(hours=2)
    )
    with pytest.raises(ReportIntegrityError, match="Decimal"):
        report_for(snapshot([huge, tiny]))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"start": START.replace(tzinfo=None)}, "REPORT_WINDOW_INVALID"),
        ({"end": END.replace(tzinfo=None)}, "REPORT_WINDOW_INVALID"),
        ({"end": START}, "REPORT_WINDOW_INVALID"),
        ({"end": START + timedelta(days=367)}, "REPORT_WINDOW_TOO_LARGE"),
        ({"mode": "LIVE"}, "REPORT_MODE_UNSUPPORTED"),
        ({"report_version": "bot-report.v0"}, "REPORT_VERSION_UNSUPPORTED"),
    ],
)
async def test_query_validation(changes: dict[str, Any], code: str) -> None:
    query = replace(bot_query(BOT_A), **changes)
    with pytest.raises(BotReportError) as caught:
        await service().get_bot_report(query, OWNER)
    assert (caught.value.status_code, caught.value.code) == (422, code)
    assert STORE.units == []


@pytest.mark.asyncio
async def test_legacy_report_v1_remains_supported_with_original_totals_shape() -> None:
    query = replace(bot_query(BOT_A), report_version="bot-report.v1")

    report = await service().get_bot_report(query, OWNER)

    payload = report.as_dict()
    assert payload["report_version"] == "bot-report.v1"
    assert "profit_factor" not in payload["totals"]["USDT"]


def test_report_version_constant() -> None:
    assert REPORT_VERSION == "bot-report.v2"
    assert bot_reporting.MAX_REPORT_WINDOW == timedelta(days=366)


@pytest.mark.asyncio
async def test_dashboard_batch_uses_one_read_snapshot_and_only_granted_bot_ledgers() -> None:
    reports = await service().get_dashboard_reports(
        BotReportQuery(workspace_id=W1, bot_id=None, start=START, end=END), ANALYST
    )
    assert [r.bot_id for r in reports] == [BOT_A]
    assert reports[0].totals["USDT"].net_pnl == Decimal("4.85")
    assert len(STORE.units) == 1
    assert STORE.units[0]["snapshot_read_only"] is True
    assert STORE.units[0]["workspace_id"] == W1
    assert f"ledger:{BOT_B}" not in STORE.calls
    assert f"ledger:{BOT_X}" not in STORE.calls
    assert "ledger_totals" not in STORE.calls


@pytest.mark.asyncio
async def test_dashboard_batch_workspace_owner_gets_separate_reports_without_aggregation() -> None:
    reports = await service().get_dashboard_reports(
        BotReportQuery(workspace_id=W1, bot_id=None, start=START, end=END), OWNER
    )
    assert [r.bot_id for r in reports] == [BOT_A, BOT_B]
    assert [r.totals["USDT"].net_pnl for r in reports] == [Decimal("4.85"), Decimal("-2.2")]
    assert len(STORE.units) == 1
    assert "ledger_totals" not in STORE.calls


@pytest.mark.asyncio
async def test_dashboard_batch_rejects_cross_workspace_before_reading() -> None:
    with pytest.raises(BotReportError, match="Không tìm thấy"):
        await service().get_dashboard_reports(
            BotReportQuery(workspace_id=W2, bot_id=None, start=START, end=END), OWNER
        )
    assert STORE.units == []


@pytest.mark.asyncio
async def test_dashboard_batch_applies_active_bot_selection_without_widening_grants() -> None:
    reports = await service().get_dashboard_reports(
        BotReportQuery(workspace_id=W1, bot_id=None, start=START, end=END),
        ANALYST,
        bot_ids=frozenset({BOT_B, BOT_X}),
    )
    assert reports == ()
    assert not any(c.startswith("ledger:") for c in STORE.calls)


@pytest.mark.asyncio
async def test_dashboard_batch_refuses_cap_and_invalid_scope() -> None:
    query = BotReportQuery(workspace_id=W1, bot_id=None, start=START, end=END)
    with pytest.raises(BotReportError):
        await service().get_dashboard_reports(replace(query, scope=ReportScope.WORKSPACE), OWNER)
    for _ in range(MAX_BOTS_PER_WORKSPACE_REPORT):
        bot_id = uuid4()
        STORE.bots[(W1, bot_id)] = ReportBot(workspace_id=W1, bot_id=bot_id, account_id=uuid4())
    with pytest.raises(BotReportError) as error:
        await service().get_dashboard_reports(query, OWNER)
    assert error.value.code == "REPORT_TOO_MANY_BOTS"
    assert not any(c.startswith("ledger:") for c in STORE.calls)
