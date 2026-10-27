"""Ca sử dụng báo cáo theo bot và tổng hợp workspace tường minh (MB-06).

Quy tắc chính (WBS MB-06):

- Workspace luôn lấy từ phiên; ``workspace_id`` của truy vấn khác phiên → không tìm thấy.
- Báo cáo bot cần ``bot:report:read`` qua ``BotGrantRepository.require``; thiếu quyền, bot không
  tồn tại hay bot workspace khác đều trả cùng lỗi ``REPORT_NOT_FOUND`` (chống BOLA dò ID).
- Không có đường nào mặc định gộp bot: báo cáo bot bắt buộc ``bot_id``; tổng hợp workspace chỉ
  chạy khi ``scope=workspace`` tường minh và role workspace-wide có ``bot:report:read``.
- Đọc trong transaction ``READ ONLY``; số liệu lấy nguyên văn từ ledger append-only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import (
    WORKSPACE_BOT_ROLES,
    BotPermission,
    role_allows_bot_permission,
)
from trading_bot.auth.policy import Principal
from trading_bot.domain.portfolio.reporting import (
    MAX_REPORT_WINDOW,
    MAX_TRADES_PER_BOT,
    REPORT_MODE,
    REPORT_VERSION,
    BotReport,
    ReportBot,
    ReportIntegrityError,
    WorkspaceAggregateReport,
    build_bot_report,
    build_workspace_report,
)

__all__ = [
    "MAX_REPORT_WINDOW",
    "NOT_FOUND_MESSAGE",
    "BotReportError",
    "BotReportQuery",
    "BotReportingService",
    "ReportScope",
]

NOT_FOUND_MESSAGE = "Không tìm thấy báo cáo bot trong workspace hiện tại."


class ReportScope(StrEnum):
    BOT = "bot"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class BotReportQuery:
    """Truy vấn báo cáo; ``scope`` mặc định ``bot`` nên thiếu ``bot_id`` không bao giờ gộp."""

    workspace_id: UUID
    bot_id: UUID | None
    start: datetime
    end: datetime
    mode: str = REPORT_MODE
    report_version: str = REPORT_VERSION
    scope: ReportScope = ReportScope.BOT


class BotReportError(Exception):
    """Lỗi nghiệp vụ có mã HTTP và mã ổn định cho client."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def _not_found() -> BotReportError:
    return BotReportError(404, "REPORT_NOT_FOUND", NOT_FOUND_MESSAGE)


class BotReportingService:
    """Dựng báo cáo từ ledger; factory tiêm được để test không cần PostgreSQL."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = BotReportRepository,
        grants: Callable[[Any], Any] = BotGrantRepository,
    ) -> None:
        self._sessions = sessions
        self._unit_of_work = unit_of_work
        self._repository = repository
        self._grants = grants

    async def get_bot_report(self, query: BotReportQuery, principal: Principal) -> BotReport:
        """Báo cáo một bot; chỉ trade của đúng workspace/bot/account trong ``[start, end)``."""
        if query.scope is not ReportScope.BOT:
            raise BotReportError(
                422, "REPORT_SCOPE_MISMATCH", "Báo cáo bot chỉ nhận scope=bot tường minh."
            )
        if query.bot_id is None:
            raise BotReportError(
                422, "BOT_ID_REQUIRED", "Báo cáo bot cần bot_id; không tự gộp nhiều bot."
            )
        _validate_query(query)
        if query.workspace_id != principal.workspace_id:
            raise _not_found()
        bot_id = query.bot_id
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repository = self._repository(unit.session)
            await repository.set_read_only()
            try:
                await self._grants(unit.session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            bot = await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id)
            if bot is None:
                raise _not_found()
            return await self._bot_report(repository, bot, query)

    async def get_workspace_report(
        self, query: BotReportQuery, principal: Principal
    ) -> WorkspaceAggregateReport:
        """Tổng hợp mọi bot của workspace; chỉ khi ``scope=workspace`` tường minh và có quyền."""
        if query.scope is not ReportScope.WORKSPACE:
            raise BotReportError(
                422,
                "WORKSPACE_SCOPE_REQUIRED",
                "Tổng hợp workspace cần scope=workspace tường minh.",
            )
        if query.bot_id is not None:
            raise BotReportError(
                422, "BOT_ID_NOT_ALLOWED", "Tổng hợp workspace không nhận bot_id; dùng scope=bot."
            )
        _validate_query(query)
        if query.workspace_id != principal.workspace_id:
            raise _not_found()
        # Grant từng bot không đủ: tổng hợp bao cả bot chưa grant/bot tạo sau.
        if not role_allows_bot_permission(
            principal.roles & WORKSPACE_BOT_ROLES, BotPermission.REPORT_READ
        ):
            raise BotReportError(
                403,
                "WORKSPACE_REPORT_FORBIDDEN",
                "Tổng hợp workspace cần role workspace có quyền bot:report:read.",
            )
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            bot_id=None,
            actor_membership_id=principal.membership_id,
        ) as unit:
            repository = self._repository(unit.session)
            await repository.set_read_only()
            grants = self._grants(unit.session)
            reports: list[BotReport] = []
            for bot in await repository.list_bots(workspace_id=principal.workspace_id):
                try:
                    await grants.require(
                        principal, bot_id=bot.bot_id, permission=BotPermission.REPORT_READ
                    )
                except PermissionError as error:  # pragma: no cover - role đã kiểm ở trên
                    raise BotReportError(
                        403, "WORKSPACE_REPORT_FORBIDDEN", "Thiếu quyền báo cáo một bot."
                    ) from error
                reports.append(await self._bot_report(repository, bot, query))
            ledger = await repository.workspace_closed_totals(
                workspace_id=principal.workspace_id, start=query.start, end=query.end
            )
            try:
                return build_workspace_report(
                    workspace_id=principal.workspace_id,
                    start=query.start,
                    end=query.end,
                    bot_reports=reports,
                    ledger=ledger,
                    mode=query.mode,
                    report_version=query.report_version,
                )
            except ReportIntegrityError as error:
                raise _integrity(error) from error

    async def _bot_report(
        self, repository: Any, bot: ReportBot, query: BotReportQuery
    ) -> BotReport:
        snapshot = await repository.load_bot_ledger(
            workspace_id=bot.workspace_id,
            bot_id=bot.bot_id,
            account_id=bot.account_id,
            start=query.start,
            end=query.end,
            max_closed=MAX_TRADES_PER_BOT,
        )
        if len(snapshot.closed_trades) > MAX_TRADES_PER_BOT:
            raise BotReportError(
                422,
                "REPORT_TOO_LARGE",
                f"Cửa sổ có hơn {MAX_TRADES_PER_BOT} trade đóng; hãy thu hẹp start/end.",
            )
        try:
            return build_bot_report(
                snapshot=snapshot,
                start=query.start,
                end=query.end,
                mode=query.mode,
                report_version=query.report_version,
            )
        except ReportIntegrityError as error:
            raise _integrity(error) from error


def _integrity(error: ReportIntegrityError) -> BotReportError:
    return BotReportError(
        409,
        "LEDGER_RECONCILIATION_FAILED",
        f"Ledger không đối soát được, không trả số liệu: {error}",
    )


def _validate_query(query: BotReportQuery) -> None:
    for value in (query.start, query.end):
        if value.tzinfo is None or value.utcoffset() is None:
            raise BotReportError(
                422, "REPORT_WINDOW_INVALID", "start/end phải có timezone (khuyến nghị UTC)."
            )
    if not query.start < query.end:
        raise BotReportError(422, "REPORT_WINDOW_INVALID", "Cần start < end (khoảng [start, end)).")
    if query.end - query.start > MAX_REPORT_WINDOW:
        raise BotReportError(
            422, "REPORT_WINDOW_TOO_LARGE", "Cửa sổ báo cáo tối đa 366 ngày; hãy chia nhỏ."
        )
    if query.mode != REPORT_MODE:
        raise BotReportError(422, "REPORT_MODE_UNSUPPORTED", "Báo cáo hiện chỉ hỗ trợ PAPER.")
    if query.report_version != REPORT_VERSION:
        raise BotReportError(
            422, "REPORT_VERSION_UNSUPPORTED", f"Chỉ hỗ trợ report_version={REPORT_VERSION}."
        )
