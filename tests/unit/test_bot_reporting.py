"""MB-06: báo cáo theo bot/tổng hợp workspace — tính toán ledger, lineage, hash và ủy quyền."""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.portfolio.reporting import (
    MAX_TRADES_PER_BOT,
    REPORT_VERSION,
    BotLedgerSnapshot,
    DecisionLineage,
    IntentLineage,
    LedgerCost,
    LedgerTrade,
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
    total = Decimal(fee) + Decimal(funding) + Decimal(slippage)
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
        net_pnl=(Decimal(gross) - total) if closed_at is not None else Decimal(0),
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
        workspace_id=W1, bot_id=BOT_A, account_id=ACC_A, start=START, end=END, max_closed=10
    )
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
    totals_sql = session.statements[-1]
    assert "trade.workspace_id =" in totals_sql
    assert "GROUP BY trade.quote_asset" in totals_sql
    assert all("UPDATE" not in s and "INSERT" not in s for s in session.statements)


@pytest.mark.asyncio
async def test_repository_switches_transaction_to_read_only() -> None:
    session = RecordingSession()
    await BotReportRepository(session).set_read_only()  # type: ignore[arg-type]
    assert session.statements == ["SET TRANSACTION READ ONLY"]


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

    async def set_read_only(self) -> None:
        STORE.calls.append("read_only")

    async def get_bot(self, *, workspace_id: UUID, bot_id: UUID) -> ReportBot | None:
        STORE.calls.append(f"get_bot:{bot_id}")
        return STORE.bots.get((workspace_id, bot_id))

    async def list_bots(self, *, workspace_id: UUID) -> list[ReportBot]:
        STORE.calls.append("list_bots")
        return sorted(
            (bot for (ws, _), bot in STORE.bots.items() if ws == workspace_id),
            key=lambda bot: str(bot.bot_id),
        )

    async def load_bot_ledger(self, **kwargs: Any) -> BotLedgerSnapshot:
        STORE.calls.append(f"ledger:{kwargs['bot_id']}")
        assert kwargs["max_closed"] == MAX_TRADES_PER_BOT
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

    assert STORE.units == [
        {
            "workspace_id": W1,
            "bot_id": BOT_A,
            "actor_membership_id": ANALYST.membership_id,
        }
    ]
    assert STORE.calls.index("read_only") < STORE.calls.index(f"ledger:{BOT_A}")
    assert STORE.calls[:2] == ["uow", "read_only"]


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
    assert STORE.units == [
        {"workspace_id": W1, "bot_id": None, "actor_membership_id": actor.membership_id}
    ]
    assert STORE.calls[:2] == ["uow", "read_only"]
    assert "ledger_totals" in STORE.calls


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


def test_report_version_constant() -> None:
    assert REPORT_VERSION == "bot-report.v1"
    assert bot_reporting.MAX_REPORT_WINDOW == timedelta(days=366)
