"""WP06.14: xác nhận consumer báo cáo tách session quyền và session dữ liệu."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from trading_bot.admin import bot_service
from trading_bot.admin.bot_service import BotAdminError, BotConfigAdminService
from trading_bot.application.bot_reporting import (
    BotReportError,
    BotReportingService,
    BotReportQuery,
    ReportScope,
)
from trading_bot.application.live_monitor import LiveMonitorService
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role

WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
MEMBERSHIP = UUID("10000000-0000-4000-8000-000000000002")
BOT = UUID("10000000-0000-4000-8000-000000000003")
NOW = datetime(2026, 11, 24, tzinfo=UTC)
PRINCIPAL = Principal(
    user_id=UUID("10000000-0000-4000-8000-000000000004"),
    membership_id=MEMBERSHIP,
    workspace_id=WORKSPACE,
    roles=frozenset({Role.ANALYST_REVIEWER}),
    mfa_verified_at=NOW,
)


class FakeUnit:
    def __init__(self, sessions: str, **kwargs: Any) -> None:
        self.session = f"{sessions}-session"
        self.kwargs = kwargs

    async def __aenter__(self) -> FakeUnit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class RecordingGrants:
    sessions: list[str] = []

    def __init__(self, session: str) -> None:
        self.sessions.append(session)

    async def require(self, *args: Any, **kwargs: Any) -> None:
        return None


class RecordingReportRepository:
    sessions: list[str] = []

    def __init__(self, session: str) -> None:
        self.sessions.append(session)

    async def get_bot(self, **kwargs: Any) -> None:
        return None


class RecordingLiveRepository:
    sessions: list[str] = []

    def __init__(self, session: str) -> None:
        self.sessions.append(session)

    async def events(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def database_now(self) -> datetime:
        return NOW


class ProposalResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> ProposalResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class ProposalReaderSession:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> ProposalResult:
        self.statements.append(statement)
        return ProposalResult(self.rows)


class ProposalReadUnit:
    instances: list[ProposalReadUnit] = []

    def __init__(
        self,
        authorization_sessions: str,
        report_reader_sessions: str | None,
        *,
        unit_of_work: Any,
        **scope: Any,
    ) -> None:
        del unit_of_work
        if report_reader_sessions is None:
            raise RuntimeError("ADMIN_REPORT_READER_UNAVAILABLE")
        self.authorization_session = f"{authorization_sessions}-authorization-session"
        self.session = ProposalReaderSession(
            [
                {
                    "id": UUID("20000000-0000-4000-8000-000000000001"),
                    "source": "AI_PROPOSAL",
                    "verdict": "BETTER",
                    "created_at": NOW,
                    "proposal": {
                        "changes": [{"field": "risk.per_trade_risk_fraction", "to": "0.001"}],
                        "rationale": "Kiểm thử reader",
                    },
                    "result": {"baseline": {}, "variant": {}, "notes": [], "window": "7d"},
                }
            ]
        )
        self.scope = scope
        self.authorization_sessions = authorization_sessions
        self.report_reader_sessions = report_reader_sessions
        ProposalReadUnit.instances.append(self)

    async def __aenter__(self) -> ProposalReadUnit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class ProposalAdminUnit:
    def __init__(self, sessions: str, **kwargs: Any) -> None:
        self.session = f"{sessions}-write-session"
        self.kwargs = kwargs

    async def __aenter__(self) -> ProposalAdminUnit:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class ProposalGrants:
    checks: list[tuple[str, BotPermission]] = []

    def __init__(self, session: str) -> None:
        self.session = session

    async def require(self, _principal: Principal, *, permission: BotPermission, **_kwargs: Any):
        self.checks.append((self.session, permission))


class EmptyConfigVersions:
    def __init__(self, _session: str) -> None:
        pass

    async def get_head(self, **_kwargs: Any) -> None:
        return None


def test_bot_report_uses_admin_session_for_grant_and_reader_for_report_rows() -> None:
    RecordingGrants.sessions = []
    RecordingReportRepository.sessions = []
    service = BotReportingService(
        "admin",
        report_reader_sessions="report-reader",
        unit_of_work=FakeUnit,
        repository=RecordingReportRepository,
        grants=RecordingGrants,
    )
    query = BotReportQuery(
        workspace_id=WORKSPACE,
        bot_id=BOT,
        start=datetime(2026, 11, 1, tzinfo=UTC),
        end=datetime(2026, 11, 2, tzinfo=UTC),
        scope=ReportScope.BOT,
    )

    with pytest.raises(BotReportError) as caught:
        asyncio.run(service.get_bot_report(query, PRINCIPAL))

    assert caught.value.code == "REPORT_NOT_FOUND"
    assert RecordingGrants.sessions == ["admin-session"]
    assert RecordingReportRepository.sessions == ["report-reader-session"]


def test_exit_backtest_lookup_uses_scoped_reader_and_minimal_columns(monkeypatch) -> None:
    ProposalReadUnit.instances = []
    ProposalGrants.checks = []
    monkeypatch.setattr(bot_service, "AdminReportReadUnitOfWork", ProposalReadUnit)
    monkeypatch.setattr(bot_service, "BotGrantRepository", ProposalGrants)
    service = BotConfigAdminService("admin", report_reader_sessions="report-reader")

    result = asyncio.run(
        service._find_exit_backtest(
            PRINCIPAL,
            bot_id=BOT,
            draft=SimpleNamespace(overlay_hash="draft-hash"),
            head=SimpleNamespace(overlay_hash="head-hash"),
        )
    )

    assert result is not None
    assert result.verdict == "BETTER"
    assert ProposalGrants.checks == [("admin-authorization-session", BotPermission.REPORT_READ)]
    unit = ProposalReadUnit.instances[-1]
    assert unit.report_reader_sessions == "report-reader"
    statement = str(unit.session.statements[-1])
    assert "proposal_backtest.id" in statement
    assert "proposal_backtest.verdict" in statement
    parameters = set(unit.session.statements[-1].compile().params.values())
    assert {"draft-hash", "head-hash", "OWNER_DRAFT"} <= parameters
    assert "proposal_backtest.proposal ->>" in statement
    assert "SELECT proposal_backtest.*" not in statement


def test_exit_backtest_lookup_fails_closed_without_reader() -> None:
    service = BotConfigAdminService("admin")
    with pytest.raises(BotAdminError) as caught:
        asyncio.run(
            service._find_exit_backtest(
                PRINCIPAL,
                bot_id=BOT,
                draft=SimpleNamespace(overlay_hash="draft-hash"),
                head=None,
            )
        )
    assert caught.value.status_code == 503


def test_live_monitor_uses_admin_session_for_grant_and_reader_for_rows() -> None:
    RecordingGrants.sessions = []
    RecordingLiveRepository.sessions = []
    service = LiveMonitorService(
        "admin",
        report_reader_sessions="report-reader",
        unit_of_work=FakeUnit,
        repository=RecordingLiveRepository,
        grants=RecordingGrants,
        clock=lambda: NOW,
    )

    import asyncio

    result = asyncio.run(
        service.read(
            PRINCIPAL,
            bot_ids=[BOT],
            since=None,
            include_state=False,
            runtime={str(BOT): {"id": str(BOT)}},
        )
    )

    assert result["bot_ids"] == [str(BOT)]
    assert RecordingGrants.sessions == ["admin-session"]
    assert RecordingLiveRepository.sessions == ["report-reader-session"]


def test_ai_proposal_reads_use_report_reader_and_current_report_and_config_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProposalReadUnit.instances = []
    ProposalGrants.checks = []
    monkeypatch.setattr(bot_service, "AdminReportReadUnitOfWork", ProposalReadUnit)
    monkeypatch.setattr(bot_service, "UnitOfWork", ProposalAdminUnit)
    monkeypatch.setattr(bot_service, "BotGrantRepository", ProposalGrants)
    monkeypatch.setattr(bot_service, "BotConfigVersionRepository", EmptyConfigVersions)
    service = BotConfigAdminService("admin", report_reader_sessions="report-reader")
    backtest_id = UUID("20000000-0000-4000-8000-000000000001")

    rows = asyncio.run(service.list_proposal_backtests(PRINCIPAL, bot_id=BOT))
    assert rows[0]["id"] == str(backtest_id)
    assert rows[0]["draftable"] is True
    assert ProposalReadUnit.instances[-1].authorization_sessions == "admin"
    assert ProposalReadUnit.instances[-1].report_reader_sessions == "report-reader"
    assert ProposalReadUnit.instances[-1].scope == {
        "workspace_id": WORKSPACE,
        "bot_id": BOT,
        "actor_membership_id": MEMBERSHIP,
        "snapshot_read_only": True,
    }
    assert "proposal_backtest" in str(ProposalReadUnit.instances[-1].session.statements[0])
    assert ProposalGrants.checks[-1] == (
        "admin-authorization-session",
        BotPermission.REPORT_READ,
    )

    with pytest.raises(BotAdminError, match="Bot chưa có cấu hình đã publish"):
        asyncio.run(
            service.create_draft_from_proposal(PRINCIPAL, bot_id=BOT, backtest_id=backtest_id)
        )
    assert ProposalGrants.checks[-3:] == [
        ("admin-authorization-session", BotPermission.REPORT_READ),
        ("admin-authorization-session", BotPermission.CONFIG_WRITE),
        ("admin-write-session", BotPermission.CONFIG_WRITE),
    ]


def test_ai_proposal_read_fails_closed_without_report_reader() -> None:
    service = BotConfigAdminService("admin")
    with pytest.raises(RuntimeError, match="ADMIN_REPORT_READER_UNAVAILABLE"):
        asyncio.run(service.list_proposal_backtests(PRINCIPAL, bot_id=BOT))


def test_row_level_services_fail_closed_when_reader_session_is_missing() -> None:
    report = BotReportingService("admin", unit_of_work=FakeUnit)
    live = LiveMonitorService("admin", unit_of_work=FakeUnit)
    query = BotReportQuery(
        workspace_id=WORKSPACE,
        bot_id=BOT,
        start=datetime(2026, 11, 1, tzinfo=UTC),
        end=datetime(2026, 11, 2, tzinfo=UTC),
        scope=ReportScope.BOT,
    )

    with pytest.raises(RuntimeError, match="ADMIN_REPORT_READER_UNAVAILABLE"):
        asyncio.run(report.get_bot_report(query, PRINCIPAL))

    with pytest.raises(RuntimeError, match="ADMIN_REPORT_READER_UNAVAILABLE"):
        asyncio.run(
            live.read(
                PRINCIPAL,
                bot_ids=[BOT],
                since=None,
                include_state=False,
                runtime={str(BOT): {"id": str(BOT)}},
            )
        )
