"""TG-3: lệnh Telegram `/report` và service tổng hợp PAPER theo quyền của principal."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from tests.unit.bot_operations_fakes import build_operations, member, seeded_store

from trading_bot.application.bot_reporting import BotReportError
from trading_bot.application.telegram_paper_report import TelegramPaperReportService
from trading_bot.application.telegram_trade_results import (
    BotSummaryRow,
    PaperReportView,
    build_paper_report_view,
)
from trading_bot.auth.policy import Principal
from trading_bot.telegram.bot_commands import (
    BotCommand,
    BotCommandHandler,
    BotCommandParseError,
    parse_bot_command,
    telegram_principal,
)

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
BOT_X = UUID("11111111-1111-4111-8111-111111111111")
BOT_Y = UUID("22222222-2222-4222-8222-222222222222")


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _telegram(store: Any, kind: str) -> Principal:
    web = member(store, kind)
    return telegram_principal(
        user_id=web.user_id,
        membership_id=web.membership_id,
        workspace_id=web.workspace_id,
        roles=web.roles,
    )


@pytest.mark.parametrize("text", ["/report", "/report@CrypidianBot", "/REPORT"])
def test_report_command_parses_without_arguments(text: str) -> None:
    parsed = parse_bot_command(text)

    assert (parsed.command, parsed.bot_ref) == (BotCommand.REPORT, None)


def test_report_command_rejects_arguments() -> None:
    with pytest.raises(BotCommandParseError) as error:
        parse_bot_command("/report all")

    assert error.value.code == "TELEGRAM_COMMAND_INVALID"


class FakeProvider:
    def __init__(self, view: PaperReportView | None = None, error: Exception | None = None):
        self.view = view
        self.error = error
        self.principals: list[Principal] = []

    async def build_view(self, principal: Principal) -> PaperReportView:
        self.principals.append(principal)
        if self.error is not None:
            raise self.error
        assert self.view is not None
        return self.view


def _view() -> PaperReportView:
    row = BotSummaryRow(
        bot_id=BOT_X,
        bot_name="Bot X",
        quote_asset="USDT",
        window_closed_count=1,
        window_net_pnl=Decimal("3"),
        total_closed_count=2,
        total_winning_count=1,
        total_net_pnl=Decimal("3"),
        open_positions=1,
        max_drawdown=Decimal("0"),
    )
    return build_paper_report_view(
        window_start=NOW - timedelta(hours=4), window_end=NOW, rows=[row]
    )


def test_report_handler_returns_summary_built_for_the_telegram_principal() -> None:
    store = seeded_store()
    provider = FakeProvider(_view())
    actor = _telegram(store, "operator")
    result = _run(
        BotCommandHandler(build_operations(store), paper_report=provider).handle_bot_command(
            actor, BotCommand.REPORT, None
        )
    )

    assert result.ok is True
    assert result.code == "OK"
    assert result.message.startswith("TỔNG HỢP KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT")
    assert "1. Bot X" in result.message
    assert result.bot_ids == (str(BOT_X),)
    assert provider.principals == [actor]


@pytest.mark.parametrize(
    "error",
    [
        BotReportError(409, "LEDGER_RECONCILIATION_FAILED", "DETAIL_LEAK"),
        PermissionError("DETAIL_LEAK"),
    ],
)
def test_report_handler_maps_report_errors_to_stable_code(error: Exception) -> None:
    store = seeded_store()
    result = _run(
        BotCommandHandler(
            build_operations(store), paper_report=FakeProvider(error=error)
        ).handle_bot_command(_telegram(store, "owner"), BotCommand.REPORT, None)
    )

    assert (result.ok, result.code) == (False, "REPORT_UNAVAILABLE")
    assert "DETAIL_LEAK" not in result.message


def test_report_handler_without_provider_is_unavailable() -> None:
    store = seeded_store()
    result = _run(
        BotCommandHandler(build_operations(store)).handle_bot_command(
            _telegram(store, "owner"), BotCommand.REPORT, None
        )
    )

    assert (result.ok, result.code) == (False, "REPORT_UNAVAILABLE")


# --- TelegramPaperReportService --------------------------------------------------------------


@dataclass(frozen=True)
class _Status:
    bot_id: UUID
    name: str


@dataclass(frozen=True)
class _Trade:
    trade_id: UUID
    net_pnl: Decimal
    closed_at: datetime


@dataclass(frozen=True)
class _Report:
    bot_id: UUID
    closed_trades: tuple[_Trade, ...]
    totals: dict[str, Any]
    payload: dict[str, Any]


class _Operations:
    def __init__(self, bots: list[_Status]) -> None:
        self.bots = bots
        self.principal: Principal | None = None

    def for_principal(self, principal: Principal) -> _Operations:
        self.principal = principal
        return self

    async def list_bots(self) -> list[_Status]:
        return self.bots


class _Reporting:
    def __init__(self, reports: list[_Report]) -> None:
        self.reports = reports
        self.calls: list[tuple[Any, Any, Any]] = []

    async def get_dashboard_reports(self, query: Any, principal: Any, *, bot_ids: Any) -> Any:
        self.calls.append((query, principal, bot_ids))
        # Mô phỏng kiểm quyền: chỉ trả bot được phép báo cáo.
        return tuple(report for report in self.reports if report.bot_id in bot_ids)


def _principal() -> Principal:
    store = seeded_store()
    return _telegram(store, "owner")


def test_service_queries_one_366_day_snapshot_for_visible_bots_and_sorts_rows() -> None:
    principal = _principal()
    operations = _Operations([_Status(BOT_X, "Bot X"), _Status(BOT_Y, "Bot Y")])
    reporting = _Reporting(
        [
            _Report(
                BOT_X,
                (_Trade(UUID(int=1), Decimal("-4"), NOW - timedelta(days=3)),),
                {"USDT": {}},
                {"open_trades": []},
            ),
            _Report(
                BOT_Y,
                (
                    _Trade(UUID(int=2), Decimal("5"), NOW - timedelta(days=10)),
                    _Trade(UUID(int=3), Decimal("2"), NOW - timedelta(hours=1)),
                ),
                {"USDT": {}},
                {"open_trades": [{"trade_id": "t"}]},
            ),
        ]
    )
    service = TelegramPaperReportService(
        reporting=reporting,  # type: ignore[arg-type]
        operations=operations,
        interval=timedelta(hours=4),
        now=lambda: NOW,
    )

    view = _run(service.build_view(principal))

    [(query, used_principal, bot_ids)] = reporting.calls
    assert used_principal is principal
    assert operations.principal is principal
    assert query.workspace_id == principal.workspace_id
    assert query.bot_id is None
    assert (query.start, query.end) == (NOW - timedelta(days=366), NOW)
    assert bot_ids == frozenset({BOT_X, BOT_Y})
    assert [row.bot_name for row in view.rows] == ["Bot Y", "Bot X"]
    top = view.rows[0]
    assert (top.window_closed_count, top.total_closed_count, top.open_positions) == (1, 2, 1)
    assert top.total_net_pnl == Decimal("7")
    assert (view.window_start, view.window_end) == (NOW - timedelta(hours=4), NOW)


def test_service_without_visible_bots_skips_report_query() -> None:
    reporting = _Reporting([])
    service = TelegramPaperReportService(
        reporting=reporting,  # type: ignore[arg-type]
        operations=_Operations([]),
        now=lambda: NOW,
    )

    view = _run(service.build_view(_principal()))

    assert view.rows == ()
    assert reporting.calls == []
