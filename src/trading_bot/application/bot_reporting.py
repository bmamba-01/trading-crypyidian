"""Ca sử dụng báo cáo theo bot và tổng hợp workspace tường minh (MB-06).

Quy tắc chính (WBS MB-06):

- Workspace luôn lấy từ phiên; ``workspace_id`` của truy vấn khác phiên → không tìm thấy.
- Báo cáo bot cần ``bot:report:read`` qua ``BotGrantRepository.require``; thiếu quyền, bot không
  tồn tại hay bot workspace khác đều trả cùng lỗi ``REPORT_NOT_FOUND`` (chống BOLA dò ID).
- Không có đường nào mặc định gộp bot: báo cáo bot bắt buộc ``bot_id``; tổng hợp workspace chỉ
  chạy khi ``scope=workspace`` tường minh và role workspace-wide có ``bot:report:read``.
- Mọi SELECT của một báo cáo chạy trong MỘT transaction ``REPEATABLE READ, READ ONLY``
  (``UnitOfWork(snapshot_read_only=True)``): một snapshot duy nhất nên trade đóng giữa hai câu
  truy vấn không làm lệch đối soát/hash; mọi ghi bị PostgreSQL từ chối. Số liệu lấy nguyên
  văn từ ledger append-only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
from trading_bot.application.market_chart import (
    MARKET_CHART_INTERVAL_SECONDS,
    MARKET_CHART_MAX_LIMIT,
    market_source_label,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import (
    WORKSPACE_BOT_ROLES,
    BotPermission,
    role_allows_bot_permission,
)
from trading_bot.auth.policy import Principal
from trading_bot.domain.portfolio.reporting import (
    MAX_BOTS_PER_WORKSPACE_REPORT,
    MAX_OPEN_TRADES_PER_BOT,
    MAX_REPORT_WINDOW,
    MAX_TRADES_PER_BOT,
    REPORT_MODE,
    REPORT_VERSION,
    SUPPORTED_REPORT_VERSIONS,
    BotReport,
    ReportBot,
    ReportIntegrityError,
    WorkspaceAggregateReport,
    build_bot_report,
    build_workspace_report,
)
from trading_bot.domain.strategy.setup_progress import monitor_verdict

__all__ = [
    "MAX_REPORT_WINDOW",
    "NOT_FOUND_MESSAGE",
    "BotReportError",
    "BotReportQuery",
    "BotReportingService",
    "ReportScope",
]

NOT_FOUND_MESSAGE = "Không tìm thấy báo cáo bot trong workspace hiện tại."
CHART_INTERVAL_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


# Nguồn nến ưu tiên cho chart: REST công khai Binance (backfill/collector SIG-0) rồi tới nguồn
# Binance khác; nguồn mẫu/kiểm thử chỉ dùng khi không có nguồn thật.
MARKET_SOURCE_PRIORITY = ("BINANCE_USDM_REST_BACKFILL", "BINANCE_USDM")


def preferred_market_source(sources: Sequence[str]) -> str:
    """Chọn một nguồn theo thứ tự ưu tiên; nguồn ngoài danh sách xếp theo tên cho ổn định."""
    for source in MARKET_SOURCE_PRIORITY:
        if source in sources:
            return source
    return sorted(sources)[0]


def build_latest_signal_view(
    rows: Sequence[Mapping[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Dựng dữ liệu thẻ tín hiệu; quyết định không qua kiểm hash bị bỏ (không hiển thị).

    ``card.is_expired`` đúng khi đã qua ``expires_at`` của candidate: thẻ vẫn hiện để giải thích
    nhưng không được vẽ lên chart như mức giá đang hiệu lực.
    """
    instant = now or datetime.now(UTC)
    from trading_bot.application.bot_dispatcher import BotDecision
    from trading_bot.domain.strategy.signal_card import SignalCardError, build_signal_card

    verified: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    rejected = 0
    for row in rows:
        try:
            BotDecision.from_payload(row["payload"], row["decision_hash"])
        except KeyError, TypeError, ValueError:
            rejected += 1
            continue
        verified.append((dict(row["payload"]), row.get("plan")))

    def summary(payload: Mapping[str, Any]) -> dict[str, Any]:
        checklist = payload.get("checklist") or {}
        return {
            "decision_id": payload.get("decision_id"),
            "status": payload.get("status"),
            "reason_code": payload.get("reason_code"),
            "detail_vi": payload.get("detail_vi"),
            "evaluated_at": payload.get("evaluated_at"),
            "strategy_release_id": payload.get("strategy_release_id"),
            "gate_details": list(payload.get("gate_details") or []),
            "checklist": list(checklist.get("items") or []),
        }

    latest = summary(verified[0][0]) if verified else None
    card: dict[str, Any] | None = None
    card_error: str | None = None
    for payload, plan in verified:
        candidate = payload.get("candidate")
        if not candidate:
            continue
        checklist = (payload.get("checklist") or {}).get("items") or []
        try:
            card = build_signal_card(candidate, checklist=checklist, plan=plan)
            card["decision_id"] = payload.get("decision_id")
            card["evaluated_at"] = payload.get("evaluated_at")
            try:
                card["is_expired"] = datetime.fromisoformat(str(card["expires_at"])) <= instant
            except ValueError:
                card["is_expired"] = True
        except SignalCardError as error:
            card_error = str(error)
        break
    return {
        "latest_decision": latest,
        "card": card,
        "card_error": card_error,
        "unverified_decisions": rejected,
    }


def build_setup_progress(
    rows: Sequence[Mapping[str, Any]],
    *,
    bonus: Sequence[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Tiến độ setup k/n (CK-6) từ các quyết định đã xác minh hash, mới nhất trước.

    Chỉ đọc payload đã lưu; không ghi lại và không đổi hash. Kèm thẻ tín hiệu của candidate gần
    nhất (nếu có) để có thể ghim kịch bản lên chart.
    """
    from trading_bot.application.bot_dispatcher import BotDecision
    from trading_bot.domain.strategy.setup_progress import setup_progress

    verified: list[dict[str, Any]] = []
    rejected = 0
    release_id: str | None = None
    for row in rows:
        try:
            BotDecision.from_payload(row["payload"], row["decision_hash"])
        except KeyError, TypeError, ValueError:
            rejected += 1
            continue
        payload = row["payload"]
        release_id = release_id or str(payload.get("strategy_release_id") or "")
        verified.append(
            {
                "evaluated_at": payload.get("evaluated_at"),
                "status": payload.get("status"),
                "reason_code": payload.get("reason_code"),
                "detail_vi": payload.get("detail_vi"),
                "checklist": list((payload.get("checklist") or {}).get("items") or []),
                "candidate": payload.get("candidate"),
            }
        )
    progress = setup_progress(
        strategy_release_id=release_id or "—", decisions=verified, bonus=bonus, now=now
    )
    progress["unverified_decisions"] = rejected
    card = build_latest_signal_view(rows, now=now)["card"]
    progress["card"] = card if card and not card.get("is_expired", True) else None
    return progress


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


class AdminReportReadUnitOfWork:
    """Ghép transaction kiểm quyền admin với transaction đọc ledger bằng login riêng.

    RLS của PostgreSQL trên reader vẫn phải kiểm quyền hiện hành theo từng hàng; grant check ở
    session admin phục vụ phản hồi API nhất quán, không thay thế hàng rào DB trong reader.
    """

    def __init__(
        self,
        authorization_sessions: async_sessionmaker[AsyncSession],
        report_reader_sessions: async_sessionmaker[AsyncSession] | None,
        *,
        unit_of_work: Callable[..., Any],
        **scope: Any,
    ) -> None:
        if report_reader_sessions is None:
            raise RuntimeError("ADMIN_REPORT_READER_UNAVAILABLE")
        self._authorization_unit = unit_of_work(authorization_sessions, **scope)
        self._reader_unit = unit_of_work(report_reader_sessions, **scope)
        self.authorization_session: AsyncSession
        self.session: AsyncSession

    async def __aenter__(self) -> Self:
        await self._authorization_unit.__aenter__()
        try:
            await self._reader_unit.__aenter__()
        except BaseException as error:
            await self._authorization_unit.__aexit__(type(error), error, error.__traceback__)
            raise
        self.authorization_session = self._authorization_unit.session
        self.session = self._reader_unit.session
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            await self._reader_unit.__aexit__(exc_type, exc, traceback)
        except BaseException as error:
            await self._authorization_unit.__aexit__(type(error), error, error.__traceback__)
            raise
        await self._authorization_unit.__aexit__(exc_type, exc, traceback)


class BotReportingService:
    """Dựng báo cáo từ ledger; factory tiêm được để test không cần PostgreSQL."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        report_reader_sessions: async_sessionmaker[AsyncSession] | None = None,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = BotReportRepository,
        grants: Callable[[Any], Any] = BotGrantRepository,
        bot_config_loader: Callable[..., Any] | None = None,
        drawdown_breaker_provider: Callable[..., Any] | None = None,
    ) -> None:
        self._sessions = sessions
        self._report_reader_sessions = report_reader_sessions
        self._bot_config_loader = bot_config_loader
        self._drawdown_breaker_provider = drawdown_breaker_provider
        self._unit_of_work = unit_of_work
        self._repository = repository
        self._grants = grants

    def _read_unit(self, **scope: Any) -> AdminReportReadUnitOfWork:
        return AdminReportReadUnitOfWork(
            self._sessions,
            self._report_reader_sessions,
            unit_of_work=self._unit_of_work,
            **scope,
        )

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
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            bot = await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id)
            if bot is None:
                raise _not_found()
            return await self._bot_report(repository, bot, query)

    async def get_trade_plan_summaries(
        self, query: BotReportQuery, principal: Principal, *, trade_ids: Sequence[UUID]
    ) -> dict[UUID, dict[str, Any]]:
        """Kế hoạch đã được risk gate duyệt của các trade trong phạm vi bot được phép xem.

        Dùng cùng kiểm tra workspace/grant như báo cáo bot. Trade mà ledger không ghim kế hoạch
        không xuất hiện trong kết quả.
        """
        if query.scope is not ReportScope.BOT or query.bot_id is None:
            raise BotReportError(422, "BOT_ID_REQUIRED", "Kế hoạch trade cần scope=bot rõ ràng.")
        if query.workspace_id != principal.workspace_id:
            raise _not_found()
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=query.bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=query.bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            bot = await repository.get_bot(workspace_id=principal.workspace_id, bot_id=query.bot_id)
            if bot is None:
                raise _not_found()
            plans: dict[UUID, dict[str, Any]] = await repository.load_trade_plans(
                workspace_id=principal.workspace_id,
                bot_id=bot.bot_id,
                account_id=bot.account_id,
                trade_ids=trade_ids,
            )
            return plans

    async def get_account_seed(
        self, query: BotReportQuery, principal: Principal
    ) -> dict[str, Any] | None:
        """Vốn ban đầu của tài khoản PAPER, cùng kiểm workspace/grant `REPORT_READ` như báo cáo."""
        if query.scope is not ReportScope.BOT or query.bot_id is None:
            raise BotReportError(422, "BOT_ID_REQUIRED", "Vốn ban đầu cần scope=bot rõ ràng.")
        if query.workspace_id != principal.workspace_id:
            raise _not_found()
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=query.bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=query.bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            seed: dict[str, Any] | None = await repository.load_account_seed(
                workspace_id=principal.workspace_id, bot_id=query.bot_id
            )
            return seed

    async def get_trade_chart(
        self,
        query: BotReportQuery,
        principal: Principal,
        *,
        trade_id: UUID,
        timeframe: str = "1h",
    ) -> dict[str, Any]:
        """Lấy OHLCV nội bộ và sự kiện ledger của một trade đã thuộc báo cáo được phép."""
        if timeframe not in CHART_INTERVAL_SECONDS:
            raise BotReportError(
                422, "CHART_TIMEFRAME_UNSUPPORTED", "Khung biểu đồ không được hỗ trợ."
            )
        if query.scope is not ReportScope.BOT or query.bot_id is None:
            raise BotReportError(422, "BOT_ID_REQUIRED", "Biểu đồ trade cần scope=bot rõ ràng.")
        report = await self.get_bot_report(query, principal)
        trade = next(
            (
                row
                for row in report.as_dict().get("trades", [])
                if row.get("trade_id") == str(trade_id)
            ),
            None,
        )
        if trade is None:
            raise _not_found()
        opened_at = datetime.fromisoformat(trade["opened_at"])
        closed_at = datetime.fromisoformat(trade["closed_at"])
        interval = timedelta(seconds=CHART_INTERVAL_SECONDS[timeframe])
        if closed_at - opened_at > interval * 900:
            chart_start = closed_at - interval * 899
            chart_notice = (
                "Giao dịch dài hơn 900 nến ở khung này; phần biểu đồ được giới hạn "
                "quanh thời điểm đóng. "
                "Chọn khung lớn hơn để xem cả điểm vào và điểm đóng."
            )
        else:
            chart_start = opened_at - interval * 20
            chart_notice = None
        chart_end = closed_at + interval * 20
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=query.bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=query.bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            bars = await repository.load_trade_bars(
                workspace_id=principal.workspace_id,
                symbol=trade["symbol"],
                timeframe=timeframe,
                start=chart_start,
                end=chart_end,
                as_of=datetime.now(UTC),
                limit=1000,
            )
        return {
            "symbol": trade["symbol"],
            "side": trade["side"],
            "timeframe": timeframe,
            "trade": trade,
            "bars": bars,
            "notice": chart_notice,
            "window": {
                "start": chart_start.isoformat(),
                "end": chart_end.isoformat(),
            },
            "data_quality": build_trade_chart_data_quality(
                bars=bars,
                chart_start=chart_start,
                chart_end=chart_end,
                timeframe=timeframe,
            ),
            "close_event": extract_close_event_provenance(trade),
        }

    async def get_latest_signal(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        """Thẻ tín hiệu (SIG-3) từ quyết định CANDIDATE gần nhất đã xác minh hash, cùng quyết định
        gần nhất (có thể là không vào lệnh) và checklist của nó. Kiểm ``REPORT_READ`` như báo cáo.
        """
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            if await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id) is None:
                raise _not_found()
            rows: list[dict[str, Any]] = await repository.latest_bot_decisions(
                workspace_id=principal.workspace_id, bot_id=bot_id, limit=20
            )
        return build_latest_signal_view(rows)

    async def get_decision_at(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        bar_open: datetime,
        interval: timedelta,
    ) -> dict[str, Any] | None:
        """Quyết định của bot đánh giá tại nến ``[bar_open, bar_open + interval)`` (CK-7).

        Quyết định được đánh giá khi nến đóng nên cửa sổ là ``(bar_open, bar_open + interval]``.
        Chỉ trả quyết định khớp hash; ``None`` khi bot không đánh giá ở nến đó.
        """
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            if await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id) is None:
                raise _not_found()
            rows: list[dict[str, Any]] = await repository.bot_decisions_between(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                start=bar_open,
                end=bar_open + interval,
            )
        view = build_latest_signal_view(rows)
        latest: dict[str, Any] | None = view["latest_decision"]
        if latest is not None:
            latest["unverified_decisions"] = view["unverified_decisions"]
        return latest

    async def validate_chart(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        symbol: str,
        timeframe: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Nút "Kiểm dữ liệu" (CK-9): kiểm tối đa ``MARKET_CHART_MAX_LIMIT`` nến đang xem."""
        from trading_bot.domain.market.chart_quality import inspect_chart_bars

        if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
            raise BotReportError(
                422, "CHART_TIMEFRAME_UNSUPPORTED", "Khung biểu đồ không được hỗ trợ."
            )
        instant = now or datetime.now(UTC)
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            if await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id) is None:
                raise _not_found()
            sources = [
                pair.split("|", 2)[2]
                for pair in await repository.list_market_series(
                    workspace_id=principal.workspace_id, as_of=instant
                )
                if pair.split("|", 2)[:2] == [symbol, timeframe]
            ]
            source = preferred_market_source(sources) if sources else None
            bars: list[dict[str, Any]] = []
            revised = 0
            if source is not None:
                bars = await repository.load_recent_bars(
                    workspace_id=principal.workspace_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    as_of=instant,
                    limit=MARKET_CHART_MAX_LIMIT,
                    source=source,
                )
                if bars:
                    revised = await repository.bar_revision_count(
                        workspace_id=principal.workspace_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        source=source,
                        since=datetime.fromtimestamp(int(bars[0]["time"]), UTC),
                    )
            gaps = await repository.open_ingestion_gaps(
                workspace_id=principal.workspace_id, symbol=symbol, timeframe=timeframe
            )
        report = inspect_chart_bars(
            bars,
            interval_seconds=MARKET_CHART_INTERVAL_SECONDS[timeframe],
            now=instant,
            revised_bars=revised,
        )
        report["source"] = source
        report["source_label"] = market_source_label(source) if source else "—"
        report["open_ingestion_gaps"] = gaps
        if gaps:
            report["ok"] = False
            report["summary"] += f" Có {len(gaps)} khoảng trống ingest chưa lấp."
        return report

    async def get_decision_log(
        self, principal: Principal, *, bot_id: UUID, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        """Lấy một trang nhật ký quyết định đã khớp hash, mới nhất trước."""
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise ValueError("Trang nhật ký quyết định nằm ngoài giới hạn.")
        from trading_bot.application.bot_dispatcher import BotDecision

        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            if await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id) is None:
                raise _not_found()
            rows: list[dict[str, Any]] = await repository.latest_bot_decisions(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                limit=limit + 1,
                offset=offset,
            )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items: list[dict[str, Any]] = []
        rejected = 0
        for row in page_rows:
            try:
                BotDecision.from_payload(row["payload"], row["decision_hash"])
            except KeyError, TypeError, ValueError:
                rejected += 1
                continue
            payload = row["payload"]
            candidate = payload.get("candidate") or {}
            items.append(
                {
                    "evaluated_at": payload.get("evaluated_at"),
                    "status": payload.get("status"),
                    "reason_code": payload.get("reason_code"),
                    "detail_vi": payload.get("detail_vi"),
                    "direction": candidate.get("direction"),
                    "strategy_release_id": payload.get("strategy_release_id"),
                }
            )
        return {
            "decisions": items,
            "unverified_decisions": rejected,
            "has_more": has_more,
            "next_offset": offset + len(page_rows) if has_more else None,
        }

    async def get_trade_history(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Lịch sử lệnh PAPER đã đóng của bot cho mã đang xem (CK-9), lấy từ báo cáo ledger."""
        report = await self.get_bot_report(
            BotReportQuery(
                workspace_id=principal.workspace_id, bot_id=bot_id, start=start, end=end
            ),
            principal,
        )
        return [
            {
                key: trade.get(key)
                for key in (
                    "trade_id",
                    "symbol",
                    "side",
                    "quote_asset",
                    "opened_at",
                    "closed_at",
                    "net_pnl",
                    "total_cost",
                    "exit_reason",
                )
            }
            for trade in report.as_dict().get("trades", [])
            if trade.get("symbol") == symbol
        ]

    async def get_setup_board(
        self,
        principal: Principal,
        *,
        bots: Sequence[tuple[UUID, str]],
        bonus: Sequence[Mapping[str, Any]],
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Bảng Entry (CK-6): tiến độ setup của các bot người xem có ``REPORT_READ``.

        Bot thiếu quyền hoặc không tồn tại bị bỏ qua lặng lẽ (không lộ sự tồn tại).
        """
        instant = now or datetime.now(UTC)
        board: list[dict[str, Any]] = []
        for bot_id, bot_name in bots:
            async with self._read_unit(
                workspace_id=principal.workspace_id,
                bot_id=bot_id,
                actor_membership_id=principal.membership_id,
                snapshot_read_only=True,
            ) as unit:
                repository = self._repository(unit.session)
                try:
                    await self._grants(unit.authorization_session).require(
                        principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                    )
                except PermissionError:
                    continue
                if (
                    await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id)
                    is None
                ):
                    continue
                rows: list[dict[str, Any]] = await repository.latest_bot_decisions(
                    workspace_id=principal.workspace_id, bot_id=bot_id, limit=20
                )
                protection = await self._bot_protection(
                    unit.authorization_session,
                    repository,
                    principal=principal,
                    bot_id=bot_id,
                    now=instant,
                )
            progress = build_setup_progress(rows, bonus=bonus, now=instant)
            board.append(
                {"bot_id": str(bot_id), "bot_name": bot_name, "protection": protection}
                | progress
                | {"verdict": monitor_verdict(progress)}
            )
        return board

    async def _bot_protection(
        self,
        session: Any,
        repository: Any,
        *,
        principal: Principal,
        bot_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        """Bảo vệ vốn (CK-12): chỗ chạy và lỗ ròng hôm nay/24 giờ so với giới hạn hiệu lực."""
        from trading_bot.application.bot_config_resolver import (
            BotConfigResolutionError,
            load_effective_bot_config,
        )

        loader = self._bot_config_loader or load_effective_bot_config
        from trading_bot.domain.risk.protection_view import protection_view
        from trading_bot.domain.risk.risk_windows import daily_window_start

        bot = await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id)
        limits = None
        limits_note: str | None = None
        loss_tz: str | None = None
        try:
            config = await loader(
                session, workspace_id=principal.workspace_id, account_id=bot.account_id, as_of=now
            )
        except BotConfigResolutionError as error:
            limits_note = f"Không đọc được giới hạn rủi ro ({error})."
        else:
            if config is None:
                limits_note = "Bot chưa publish cấu hình nên chưa có giới hạn hiệu lực."
            else:
                limits = config.risk_limits
                loss_tz = getattr(config, "loss_window_timezone", None)
        day_start = daily_window_start(now, loss_tz)
        today = await repository.closed_net_since(
            workspace_id=principal.workspace_id, bot_id=bot_id, since=day_start
        )
        rolling = await repository.closed_net_since(
            workspace_id=principal.workspace_id, bot_id=bot_id, since=now - timedelta(hours=24)
        )
        asset = next(iter(sorted({*today, *rolling})), "USDT")

        breaker_view: dict[str, Any] | None = None
        drawdown_provider = self._drawdown_breaker_provider
        if drawdown_provider is not None:
            try:
                breaker_view = await drawdown_provider(
                    session=session,
                    workspace_id=principal.workspace_id,
                    bot_id=bot_id,
                    account_id=bot.account_id,
                    as_of=now,
                )
            except Exception:
                breaker_view = None

        view = protection_view(
            open_positions=await repository.count_open_trades(
                workspace_id=principal.workspace_id, bot_id=bot_id
            ),
            max_positions=limits.max_positions if limits else None,
            net_today=today.get(asset, Decimal(0)),
            net_24h=rolling.get(asset, Decimal(0)),
            max_daily_loss=limits.max_daily_loss if limits else None,
            max_rolling_loss=limits.max_rolling_loss if limits else None,
            quote_asset=asset,
            timezone_name=loss_tz or "UTC",
            breaker=breaker_view,
        )
        view["note"] = limits_note
        return view

    async def list_market_series(
        self, principal: Principal, *, bot_id: UUID
    ) -> dict[str, list[str]]:
        """Symbol → các khung có nến đã đóng trong workspace, sau khi kiểm ``REPORT_READ``.

        Nến thị trường là dữ liệu chung của workspace; chart yêu cầu người xem có quyền đọc
        báo cáo của ít nhất bot đang chọn để không mở dữ liệu cho người ngoài phạm vi bot.
        """
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            if await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id) is None:
                raise _not_found()
            pairs: list[str] = await repository.list_market_series(
                workspace_id=principal.workspace_id, as_of=datetime.now(UTC)
            )
        series: dict[str, list[str]] = {}
        for pair in pairs:
            symbol, timeframe, _source = pair.split("|", 2)
            frames = (
                series.setdefault(symbol, []) if timeframe in MARKET_CHART_INTERVAL_SECONDS else []
            )
            if timeframe in MARKET_CHART_INTERVAL_SECONDS and timeframe not in frames:
                frames.append(timeframe)
        order = list(MARKET_CHART_INTERVAL_SECONDS)
        return {symbol: sorted(frames, key=order.index) for symbol, frames in series.items()}

    async def get_market_bars(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Nến đã đóng gần nhất của một symbol cho chart thị trường (SIG-7).

        Dùng cùng kiểm workspace/grant ``REPORT_READ`` như báo cáo, theo bot đang chọn.
        """
        if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
            raise BotReportError(
                422, "CHART_TIMEFRAME_UNSUPPORTED", "Khung biểu đồ không được hỗ trợ."
            )
        if not 1 <= limit <= MARKET_CHART_MAX_LIMIT:
            raise BotReportError(422, "CHART_LIMIT_INVALID", "Số nến yêu cầu không hợp lệ.")
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            if await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id) is None:
                raise _not_found()
            # Một chart chỉ đọc một nguồn để không trộn nến thật với dữ liệu mẫu/kiểm thử.
            sources = [
                pair.split("|", 2)[2]
                for pair in await repository.list_market_series(
                    workspace_id=principal.workspace_id, as_of=datetime.now(UTC)
                )
                if pair.split("|", 2)[:2] == [symbol, timeframe]
            ]
            if not sources:
                return []
            bars: list[dict[str, Any]] = await repository.load_recent_bars(
                workspace_id=principal.workspace_id,
                symbol=symbol,
                timeframe=timeframe,
                as_of=datetime.now(UTC),
                limit=limit,
                source=preferred_market_source(sources),
            )
            return bars

    async def get_market_overview(
        self,
        principal: Principal,
        *,
        bot_id: UUID,
        symbol: str,
        timeframe: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Dải thông số (CK-1) và ma trận đa khung (CK-2) của một symbol, chỉ đọc.

        Cùng kiểm ``REPORT_READ`` theo bot đang chọn như chart; mọi SELECT trong một snapshot
        để giá trực tiếp, mark và nến các khung cùng thời điểm đọc.
        """
        from trading_bot.application.market_overview import (
            MTF_ROLES,
            OVERVIEW_BAR_LIMIT,
            build_market_overview,
        )

        if timeframe not in MARKET_CHART_INTERVAL_SECONDS:
            raise BotReportError(
                422, "CHART_TIMEFRAME_UNSUPPORTED", "Khung biểu đồ không được hỗ trợ."
            )
        instant = now or datetime.now(UTC)
        frames = [timeframe, *(frame for frame, _ in MTF_ROLES if frame != timeframe)]
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=bot_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            try:
                await self._grants(unit.authorization_session).require(
                    principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                )
            except PermissionError as error:
                raise _not_found() from error
            if await repository.get_bot(workspace_id=principal.workspace_id, bot_id=bot_id) is None:
                raise _not_found()
            sources: dict[str, list[str]] = {}
            for pair in await repository.list_market_series(
                workspace_id=principal.workspace_id, as_of=instant
            ):
                pair_symbol, pair_frame, pair_source = pair.split("|", 2)
                if pair_symbol == symbol:
                    sources.setdefault(pair_frame, []).append(pair_source)
            bars_by_timeframe: dict[str, list[dict[str, Any]]] = {}
            for frame in frames:
                if frame not in sources:
                    continue
                bars_by_timeframe[frame] = await repository.load_recent_bars(
                    workspace_id=principal.workspace_id,
                    symbol=symbol,
                    timeframe=frame,
                    as_of=instant,
                    limit=OVERVIEW_BAR_LIMIT,
                    source=preferred_market_source(sources[frame]),
                )
            quote = await repository.latest_live_quote(
                workspace_id=principal.workspace_id, symbol=symbol
            )
            mark = await repository.latest_mark_price(
                workspace_id=principal.workspace_id, symbol=symbol, as_of=instant
            )
            positions = await repository.open_positions(
                workspace_id=principal.workspace_id, bot_id=bot_id, symbol=symbol
            )
            open_gaps = await repository.open_ingestion_gaps(
                workspace_id=principal.workspace_id, symbol=symbol, timeframe=timeframe
            )
        return build_market_overview(
            symbol=symbol,
            timeframe=timeframe,
            now=instant,
            bars_by_timeframe=bars_by_timeframe,
            quote=quote,
            mark=mark,
            positions=positions,
            open_gaps=open_gaps,
            source_label=(
                market_source_label(preferred_market_source(sources[timeframe]))
                if timeframe in sources
                else "—"
            ),
        )

    async def get_dashboard_reports(
        self,
        query: BotReportQuery,
        principal: Principal,
        *,
        bot_ids: frozenset[UUID] | None = None,
    ) -> tuple[BotReport, ...]:
        """Đọc các báo cáo bot được cấp quyền trong cùng snapshot, không tạo tổng workspace.

        Tập bot từ màn hình chỉ thu hẹp lựa chọn; quyền báo cáo vẫn được kiểm lại bên trong
        snapshot. Mọi ledger và grant dùng chung giao dịch chỉ đọc của MB-06.
        """
        if query.scope is not ReportScope.BOT or query.bot_id is not None:
            raise BotReportError(
                422,
                "REPORT_SCOPE_MISMATCH",
                "Thống kê theo lô chỉ nhận phạm vi bot, không nhận mã bot đơn lẻ.",
            )
        _validate_query(query)
        if query.workspace_id != principal.workspace_id:
            raise _not_found()
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=None,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            grants = self._grants(unit.authorization_session)
            bots = await repository.list_bots(
                workspace_id=principal.workspace_id, limit=MAX_BOTS_PER_WORKSPACE_REPORT + 1
            )
            if len(bots) > MAX_BOTS_PER_WORKSPACE_REPORT:
                raise BotReportError(
                    422,
                    "REPORT_TOO_MANY_BOTS",
                    "Phạm vi thống kê quá lớn; hãy xem báo cáo từng bot.",
                )
            reports: list[BotReport] = []
            for bot in bots:
                if bot_ids is not None and bot.bot_id not in bot_ids:
                    continue
                try:
                    await grants.require(
                        principal, bot_id=bot.bot_id, permission=BotPermission.REPORT_READ
                    )
                except PermissionError:
                    continue
                reports.append(await self._bot_report(repository, bot, query))
            return tuple(reports)

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
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            bot_id=None,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            grants = self._grants(unit.authorization_session)
            bots = await repository.list_bots(
                workspace_id=principal.workspace_id, limit=MAX_BOTS_PER_WORKSPACE_REPORT + 1
            )
            if len(bots) > MAX_BOTS_PER_WORKSPACE_REPORT:
                raise BotReportError(
                    422,
                    "REPORT_TOO_MANY_BOTS",
                    f"Workspace có hơn {MAX_BOTS_PER_WORKSPACE_REPORT} bot; tổng hợp bị từ chối, "
                    "hãy dùng báo cáo theo từng bot.",
                )
            reports: list[BotReport] = []
            for bot in bots:
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
            max_open=MAX_OPEN_TRADES_PER_BOT,
        )
        if len(snapshot.closed_trades) > MAX_TRADES_PER_BOT:
            raise BotReportError(
                422,
                "REPORT_TOO_LARGE",
                f"Cửa sổ có hơn {MAX_TRADES_PER_BOT} trade đóng; hãy thu hẹp start/end.",
            )
        if len(snapshot.open_trades) > MAX_OPEN_TRADES_PER_BOT:
            raise BotReportError(
                422,
                "REPORT_TOO_MANY_OPEN_TRADES",
                f"Bot có hơn {MAX_OPEN_TRADES_PER_BOT} trade mở tại end; báo cáo bị từ chối, "
                "cần điều tra ledger trước.",
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
    if query.report_version not in SUPPORTED_REPORT_VERSIONS:
        raise BotReportError(
            422,
            "REPORT_VERSION_UNSUPPORTED",
            f"Phiên bản báo cáo phải thuộc {', '.join(sorted(SUPPORTED_REPORT_VERSIONS))}.",
        )


def build_trade_chart_data_quality(
    *,
    bars: list[dict[str, Any]],
    chart_start: datetime,
    chart_end: datetime,
    timeframe: str,
) -> dict[str, Any]:
    """Đo độ phủ dữ liệu nến thật trong cửa sổ biểu đồ và trả cảnh báo tiếng Việt khi thiếu."""
    interval_seconds = CHART_INTERVAL_SECONDS[timeframe]
    span_seconds = max(0.0, (chart_end - chart_start).total_seconds())
    expected_bars = max(1, int(span_seconds // interval_seconds))
    actual_bars = len(bars)
    missing_bars = max(0, expected_bars - actual_bars)
    coverage_ratio = round(min(1.0, actual_bars / expected_bars), 4)
    has_gaps = actual_bars == 0 or missing_bars > 0
    warning_vi: str | None = None
    if actual_bars == 0:
        warning_vi = (
            f"Không có dữ liệu nến {timeframe} trong khoảng thời gian biểu đồ "
            f"(kỳ vọng khoảng {expected_bars} nến)."
        )
    elif missing_bars > 0:
        warning_vi = (
            f"Thiếu dữ liệu nến {timeframe}: hiện có {actual_bars}/{expected_bars} nến "
            f"(thiếu khoảng {missing_bars} nến) trong khoảng thời gian biểu đồ."
        )
    return {
        "expected_bars": expected_bars,
        "actual_bars": actual_bars,
        "missing_bars": missing_bars,
        "coverage_ratio": coverage_ratio,
        "has_gaps": has_gaps,
        "warning_vi": warning_vi,
    }


def extract_close_event_provenance(trade: dict[str, Any]) -> dict[str, Any]:
    """Trích thông tin đóng lệnh từ dữ liệu ledger/explanation thật; giữ None nếu trường thiếu."""
    raw_fills = trade.get("fills")
    fills = raw_fills if isinstance(raw_fills, list) else []
    exit_fills = [fill for fill in fills if isinstance(fill, dict) and fill.get("role") != "ENTRY"]
    closed_fill = next(
        (fill for fill in reversed(exit_fills) if fill.get("position_event_type") == "CLOSED"),
        exit_fills[-1] if exit_fills else None,
    )
    raw_exit_key = None
    if closed_fill is not None:
        raw_exit_key = closed_fill.get("fill_key") or closed_fill.get("fill_id")
    raw_expl = trade.get("explanation")
    explanation: dict[str, Any] = raw_expl if isinstance(raw_expl, dict) else {}
    raw_lineage = trade.get("lineage")
    lineage: dict[str, Any] = raw_lineage if isinstance(raw_lineage, dict) else {}
    raw_bar_key = (
        trade.get("bar_key")
        or (closed_fill.get("bar_key") if closed_fill is not None else None)
        or explanation.get("bar_key")
    )
    raw_provenance = (
        trade.get("provenance")
        or lineage.get("provenance")
        or explanation.get("provenance")
        or explanation.get("evidence_id")
    )
    return {
        "closed_at": trade.get("closed_at"),
        "exit_reason": trade.get("exit_reason"),
        "exit_fill_key": str(raw_exit_key) if raw_exit_key is not None else None,
        "bar_key": str(raw_bar_key) if raw_bar_key is not None else None,
        "provenance": str(raw_provenance) if raw_provenance is not None else None,
    }
