"""Vòng sự kiện thị trường PAPER theo tick supervisor (RT-1 lát 2).

Mỗi tick, với từng account READY:

1. Intent đã submit nhưng chưa khớp (ACCEPTED/PARTIALLY_FILLED): đưa nến đóng đầu tiên có
   `open_time ≥ eligible_at` vào `apply_entry_event`.
2. Trade OPEN: đưa lần lượt từng nến đóng mới sau nến đã xử lý vào `monitor_position` tới khi
   vị thế đóng hoặc hết nến.
3. `expire_reservations(as_of = đồng hồ DB)`.

Chỉ đọc `market_bar` đã persist (live ưu tiên, rồi REST backfill), nến phải `is_closed`, có
`close_time ≤ now` và `available_at ≤ now` theo đồng hồ DB: không bao giờ dùng nến chưa đóng hoặc
kết thúc sau hiện tại. Không gọi API sàn.

Tiến độ không lưu riêng: con trỏ là `max(event_at)` của checkpoint PAPER cùng `event_scope`
(`sha256("{paper_intent_id}:{symbol}")`) mà coordinator ghi trong cùng transaction với fill/exit.
Restart đọc lại con trỏ nên không áp lại nến (bar key xác định
`"{source}:{symbol}:{timeframe}:{open_time_iso}"`).

Khung execution cố định theo root settings: `1m` khi `market_data.timeframes` có `1m`, ngược lại
entry timeframe của release. Không trộn khung trong một vị thế (tránh đếm trùng một khoảng giá).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models.entities import StrategyRelease
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.models.paper_entities import (
    OrderIntentRecord,
    PaperExecutionCheckpointRecord,
    TradeRecord,
)
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.application.paper_market_context import BAR_INTERVALS
from trading_bot.application.paper_runtime_application import (
    LEASE_LOSS_REASONS,
    PaperEntryEventCommand,
    PaperMonitorCommand,
    PaperRuntimeApplicationError,
)
from trading_bot.application.strategy_market_requirements import entry_timeframe_for_release
from trading_bot.domain.execution.paper import PaperBar

logger = logging.getLogger(__name__)

LIVE_SOURCE = "BINANCE_USDM"
BACKFILL_SOURCE = "BINANCE_USDM_REST_BACKFILL"
BAR_SOURCES = (LIVE_SOURCE, BACKFILL_SOURCE)
EXECUTION_TIMEFRAME = "1m"
ENTRY_STATUSES = ("ACCEPTED", "PARTIALLY_FILLED")


def paper_bar_key(*, source: str, symbol: str, timeframe: str, open_time: datetime) -> str:
    """Khóa bar xác định cho checkpoint/fill; cùng nến luôn cho cùng khóa."""
    return f"{source}:{symbol}:{timeframe}:{open_time.astimezone(UTC).isoformat()}"


def market_event_scope(paper_intent_id: str, symbol: str) -> str:
    """Phải trùng `paper_lifecycle._market_event_scope` (unit test đối chiếu)."""
    return hashlib.sha256(f"{paper_intent_id}:{symbol}".encode()).hexdigest()


def paper_bar_from_row(row: Any) -> PaperBar | None:
    """Chuyển nến đã lưu thành PaperBar; nến hỏng (chưa đóng/available trước close) bị bỏ."""
    if not row.is_closed or row.available_at < row.close_time or row.close_time <= row.open_time:
        return None
    try:
        return PaperBar(
            paper_bar_key(
                source=row.source,
                symbol=row.symbol,
                timeframe=row.timeframe,
                open_time=row.open_time,
            ),
            row.symbol,
            row.open_time,
            row.close_time,
            row.available_at,
            row.open,
            row.high,
            row.low,
            row.close,
        )
    except ValueError:
        return None


async def closed_bars_after(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    symbol: str,
    timeframe: str,
    now: datetime,
    after: datetime | None,
    at_or_after: datetime | None,
    limit: int,
) -> tuple[PaperBar, ...]:
    """Nến đóng tăng dần theo open_time, một bản ghi mỗi open_time (live trước REST backfill)."""
    filters = [
        MarketBar.workspace_id == workspace_id,
        MarketBar.symbol == symbol,
        MarketBar.timeframe == timeframe,
        MarketBar.source.in_(BAR_SOURCES),
        MarketBar.is_closed.is_(True),
        MarketBar.close_time <= now,
        MarketBar.available_at <= now,
    ]
    if after is not None:
        filters.append(MarketBar.open_time > after)
    if at_or_after is not None:
        filters.append(MarketBar.open_time >= at_or_after)
    rows = await session.scalars(
        select(MarketBar)
        .where(*filters)
        .ext(distinct_on(MarketBar.open_time))
        .order_by(
            MarketBar.open_time,
            case((MarketBar.source == LIVE_SOURCE, 0), else_=1),
            MarketBar.available_at.desc(),
            MarketBar.revision.desc(),
            MarketBar.id.desc(),
        )
        .limit(limit)
    )
    bars = (paper_bar_from_row(row) for row in rows.all())
    return tuple(bar for bar in bars if bar is not None)


# Chốt an toàn RT-1 (sự cố 15/11/2026): hai vị thế seed mở trên nến fixture giá ~101 bị đóng bằng
# nến REST thật ~65.000 vì vòng sự kiện nhảy qua khoảng trống và đổi nguồn. Mỗi nến chỉ được dùng
# khi nối đúng một nhịp sau nến đã lưu liền trước và giá mở không lệch quá ngưỡng so với giá đóng
# trước đó; vi phạm thì dừng scope (vị thế giữ nguyên, readiness báo lỗi) thay vì khớp/đóng lệnh.
MAX_BAR_OPEN_JUMP = Decimal("0.20")
GAP_REASON = "PAPER_MARKET_GAP"
DISCONTINUITY_REASON = "PAPER_MARKET_DISCONTINUITY"


def continuous_prefix(
    bars: Sequence[PaperBar],
    *,
    previous: PaperBar | None,
    interval: timedelta,
    max_jump: Decimal = MAX_BAR_OPEN_JUMP,
) -> tuple[tuple[PaperBar, ...], str | None]:
    """Phần đầu liên tục của ``bars`` và mã vi phạm đầu tiên (nếu có).

    Thiếu nến liền trước cũng là vi phạm: không thể kiểm giá nên không đoán.
    """
    accepted: list[PaperBar] = []
    prior = previous
    for bar in bars:
        if prior is None or bar.opened_at - prior.opened_at != interval:
            return tuple(accepted), GAP_REASON
        if prior.close <= 0 or abs(bar.open / prior.close - 1) > max_jump:
            return tuple(accepted), DISCONTINUITY_REASON
        accepted.append(bar)
        prior = bar
    return tuple(accepted), None


async def closed_bar_before(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    symbol: str,
    timeframe: str,
    before: datetime,
) -> PaperBar | None:
    """Nến đóng liền trước ``before`` (live trước REST), để kiểm tính liên tục."""
    row = await session.scalar(
        select(MarketBar)
        .where(
            MarketBar.workspace_id == workspace_id,
            MarketBar.symbol == symbol,
            MarketBar.timeframe == timeframe,
            MarketBar.source.in_(BAR_SOURCES),
            MarketBar.is_closed.is_(True),
            MarketBar.open_time < before,
        )
        .order_by(
            MarketBar.open_time.desc(),
            case((MarketBar.source == LIVE_SOURCE, 0), else_=1),
            MarketBar.available_at.desc(),
            MarketBar.revision.desc(),
        )
        .limit(1)
    )
    return None if row is None else paper_bar_from_row(row)


@dataclass(frozen=True, slots=True)
class _Scope:
    """Một intent cần đưa nến: entry (chưa khớp) hoặc monitor (trade OPEN)."""

    intent_id: UUID
    paper_intent_id: str
    symbol: str
    timeframe: str
    feature_snapshot_id: str
    trade_id: UUID | None
    entry_pending: bool
    cursor: datetime | None


@dataclass(slots=True)
class MarketDispatchCounters:
    ticks: int = 0
    entry_events: int = 0
    entry_fills: int = 0
    monitor_events: int = 0
    closed_trades: int = 0
    expired_reservations: int = 0
    errors: int = 0
    discontinuities: int = 0


class PaperMarketEventLoop:
    """Đưa nến đã lưu vào lifecycle PAPER; lỗi một intent không chặn intent/account khác."""

    def __init__(
        self,
        *,
        application: Any,
        sessions: async_sessionmaker[AsyncSession] | None,
        workspace_id: UUID,
        max_bars_per_scope: int = 500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_bars_per_scope < 1:
            raise ValueError("max_bars_per_scope phải dương.")
        self._application = application
        self._sessions = sessions
        self._workspace_id = workspace_id
        self._max_bars = max_bars_per_scope
        self._clock = clock or (lambda: datetime.now(UTC))
        self.counters = MarketDispatchCounters()
        self.last_tick_at: datetime | None = None
        self.last_tick_succeeded: bool | None = None
        self.last_error_code: str | None = None
        # Account có entry chờ khớp mà không có nến 1m (khung execution = khung chiến lược): lệnh
        # chỉ khớp ở nến mở sau lúc submit nên luôn hết hạn trước khi khớp (sự cố 16/11/2026).
        self._missing_execution_bars: set[UUID] = set()
        self._warned_intents: set[UUID] = set()

    def status(self) -> dict[str, Any]:
        """Trạng thái cho readiness: chỉ bộ đếm/mã an toàn, không chứa dữ liệu nhạy cảm."""
        if self.last_tick_at is None:
            state = "starting"
        else:
            # Gián đoạn dữ liệu là trạng thái cần người vận hành xử lý (vị thế đang bị treo).
            healthy = (
                self.last_tick_succeeded
                and self.counters.discontinuities == 0
                and not self._missing_execution_bars
            )
            state = "running" if healthy else "degraded"
        counters = self.counters
        return {
            "state": state,
            "last_tick_at": None if self.last_tick_at is None else self.last_tick_at.isoformat(),
            "ticks": counters.ticks,
            "entry_events": counters.entry_events,
            "entry_fills": counters.entry_fills,
            "monitor_events": counters.monitor_events,
            "closed_trades": counters.closed_trades,
            "expired_reservations": counters.expired_reservations,
            "errors": counters.errors,
            "discontinuities": counters.discontinuities,
            "execution_1m_missing_accounts": len(self._missing_execution_bars),
            "last_error_code": self.last_error_code,
        }

    async def run_for_account(self, account_id: UUID) -> None:
        """Một lượt cho account; ghi nhận lỗi vào bộ đếm thay vì ném ra supervisor."""
        succeeded = True
        try:
            succeeded = await self._run(account_id)
        except Exception as error:  # noqa: BLE001 - lỗi đọc DB chỉ làm degraded
            succeeded = False
            self._record_error(error)
        self.counters.ticks += 1
        self.last_tick_at = self._clock()
        self.last_tick_succeeded = succeeded

    async def _run(self, account_id: UUID) -> bool:
        account = self._application.runtime_account(account_id)
        if account is None or self._sessions is None:
            return True
        now, scopes = await self._load_scopes(account)
        self._check_execution_bars(account_id, scopes)
        succeeded = True
        for scope in scopes:
            try:
                await self._process_scope(account, scope, now)
            except PaperRuntimeApplicationError as error:
                succeeded = False
                self._record_error(error)
                if error.reason_code in LEASE_LOSS_REASONS:
                    return False
            except Exception as error:  # noqa: BLE001
                succeeded = False
                self._record_error(error)
        if self._application.runtime_account(account_id) is None:
            return succeeded
        try:
            expired = await self._application.expire_reservations(account_id, as_of=now)
            self.counters.expired_reservations += int(expired or 0)
        except Exception as error:  # noqa: BLE001
            succeeded = False
            self._record_error(error)
        return succeeded

    def _check_execution_bars(self, account_id: UUID, scopes: Sequence[_Scope]) -> None:
        """Cảnh báo (degraded) khi entry chờ khớp phải dùng khung chiến lược vì thiếu nến 1m."""
        missing = [
            scope
            for scope in scopes
            if scope.entry_pending and scope.timeframe != EXECUTION_TIMEFRAME
        ]
        if not missing:
            self._missing_execution_bars.discard(account_id)
            return
        self._missing_execution_bars.add(account_id)
        self.last_error_code = "PAPER_EXECUTION_1M_UNAVAILABLE"
        for scope in missing:
            if scope.intent_id in self._warned_intents:
                continue
            self._warned_intents.add(scope.intent_id)
            logger.warning(
                "PAPER entry không thể khớp: root thiếu nến 1m nên khung execution = %s; "
                "reason_code=PAPER_EXECUTION_1M_UNAVAILABLE symbol=%s",
                scope.timeframe,
                scope.symbol,
            )

    async def _load_scopes(self, account: Any) -> tuple[datetime, tuple[_Scope, ...]]:
        assert self._sessions is not None
        async with UnitOfWork(
            self._sessions, workspace_id=self._workspace_id, bot_id=account.bot_id
        ) as uow:
            session = uow.session
            now = await session.scalar(select(func.clock_timestamp()))
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise PaperRuntimeApplicationError("DATABASE_CLOCK_UNAVAILABLE")
            published = await WorkspaceSettingsRepository(session).load_effective_config(
                workspace_id=self._workspace_id
            )
            if published is None:
                raise PaperRuntimeApplicationError("WORKSPACE_SETTINGS_UNAVAILABLE")
            configured = set(published.settings.market_data.timeframes)
            open_trades = {
                trade.intent_id: trade
                for trade in (
                    await session.scalars(
                        select(TradeRecord).where(
                            TradeRecord.workspace_id == self._workspace_id,
                            TradeRecord.account_id == account.account_id,
                            TradeRecord.mode == "PAPER",
                            TradeRecord.status == "OPEN",
                        )
                    )
                ).all()
            }
            intent_filter: Any = OrderIntentRecord.status.in_(ENTRY_STATUSES)
            if open_trades:
                intent_filter = or_(intent_filter, OrderIntentRecord.id.in_(tuple(open_trades)))
            intents = (
                await session.scalars(
                    select(OrderIntentRecord)
                    .where(
                        OrderIntentRecord.workspace_id == self._workspace_id,
                        OrderIntentRecord.account_id == account.account_id,
                        OrderIntentRecord.mode == "PAPER",
                        intent_filter,
                    )
                    .order_by(OrderIntentRecord.created_at, OrderIntentRecord.id)
                )
            ).all()
            scopes: list[_Scope] = []
            for intent in intents:
                release_name = await session.scalar(
                    select(StrategyRelease.name).where(
                        StrategyRelease.workspace_id == self._workspace_id,
                        StrategyRelease.id == intent.strategy_release_id,
                    )
                )
                entry_timeframe = (
                    entry_timeframe_for_release(release_name) if release_name else None
                )
                timeframe = (
                    EXECUTION_TIMEFRAME if EXECUTION_TIMEFRAME in configured else entry_timeframe
                )
                if timeframe is None or timeframe not in BAR_INTERVALS:
                    raise PaperRuntimeApplicationError("PAPER_EXECUTION_TIMEFRAME_UNAVAILABLE")
                paper_intent_id = f"paper:{intent.idempotency_key}"
                cursor = await session.scalar(
                    select(func.max(PaperExecutionCheckpointRecord.event_at)).where(
                        PaperExecutionCheckpointRecord.workspace_id == self._workspace_id,
                        PaperExecutionCheckpointRecord.account_id == account.account_id,
                        PaperExecutionCheckpointRecord.event_scope
                        == market_event_scope(paper_intent_id, intent.symbol),
                    )
                )
                trade = open_trades.get(intent.id)
                scopes.append(
                    _Scope(
                        intent_id=intent.id,
                        paper_intent_id=paper_intent_id,
                        symbol=intent.symbol,
                        timeframe=timeframe,
                        feature_snapshot_id=intent.feature_snapshot_id,
                        trade_id=trade.id if trade is not None else None,
                        entry_pending=intent.status in ENTRY_STATUSES,
                        cursor=cursor,
                    )
                )
            return now, tuple(scopes)

    async def _process_scope(self, account: Any, scope: _Scope, now: datetime) -> None:
        gateway = account.gateway
        eligible_at: datetime | None = None
        if scope.entry_pending:
            if bool(getattr(gateway, "entries_paused", False)):
                # Pause/kill: supervisor đã hủy phần chưa khớp; không ép fill.
                if scope.trade_id is None:
                    return
            else:
                try:
                    eligible_at = gateway.order(scope.paper_intent_id).eligible_at
                except KeyError:
                    raise PaperRuntimeApplicationError("PAPER_INTENT_NOT_HYDRATED") from None
        bars = await self._bars(scope, now=now, at_or_after=eligible_at)
        if bars:
            previous = await self._previous_bar(scope, before=bars[0].opened_at)
            bars, violation = continuous_prefix(
                bars, previous=previous, interval=BAR_INTERVALS[scope.timeframe]
            )
            if violation is not None:
                self.counters.discontinuities += 1
                self.last_error_code = violation
                logger.warning(
                    "PAPER market event loop dừng scope vì dữ liệu nến không liên tục; "
                    "reason_code=%s symbol=%s timeframe=%s",
                    violation,
                    scope.symbol,
                    scope.timeframe,
                )
        trade_id = scope.trade_id
        entry_pending = scope.entry_pending and eligible_at is not None
        for bar in bars:
            if entry_pending:
                entry = await self._application.apply_entry_event(
                    PaperEntryEventCommand(
                        account_id=account.account_id,
                        ledger_intent_id=scope.intent_id,
                        paper_intent_id=scope.paper_intent_id,
                        bar=bar,
                    )
                )
                self.counters.entry_events += 1
                self.counters.entry_fills += int(entry.persisted_fill_count or 0)
                if entry.trade_id is not None:
                    trade_id = entry.trade_id
                status = str(getattr(entry.order.status, "value", entry.order.status))
                entry_pending = status in ENTRY_STATUSES
                if status in ("CANCELLED", "EXPIRED", "UNKNOWN") and trade_id is None:
                    return
                # Nến khớp entry không đồng thời là nến monitor (đã có event "market" cùng mốc).
                continue
            if trade_id is None:
                return
            monitored = await self._application.monitor_position(
                PaperMonitorCommand(
                    account_id=account.account_id,
                    ledger_intent_id=scope.intent_id,
                    trade_id=trade_id,
                    paper_position_id=scope.paper_intent_id,
                    bar=bar,
                    feature_snapshot_id=scope.feature_snapshot_id,
                )
            )
            self.counters.monitor_events += 1
            if monitored.trade_closed:
                self.counters.closed_trades += 1
                return

    async def _bars(
        self, scope: _Scope, *, now: datetime, at_or_after: datetime | None
    ) -> Sequence[PaperBar]:
        assert self._sessions is not None
        async with UnitOfWork(self._sessions, workspace_id=self._workspace_id) as uow:
            return await closed_bars_after(
                uow.session,
                workspace_id=self._workspace_id,
                symbol=scope.symbol,
                timeframe=scope.timeframe,
                now=now,
                after=scope.cursor,
                at_or_after=at_or_after,
                limit=self._max_bars,
            )

    async def _previous_bar(self, scope: _Scope, *, before: datetime) -> PaperBar | None:
        assert self._sessions is not None
        async with UnitOfWork(self._sessions, workspace_id=self._workspace_id) as uow:
            return await closed_bar_before(
                uow.session,
                workspace_id=self._workspace_id,
                symbol=scope.symbol,
                timeframe=scope.timeframe,
                before=before,
            )

    def _record_error(self, error: BaseException) -> None:
        code = getattr(error, "reason_code", None)
        self.last_error_code = code if isinstance(code, str) else type(error).__name__
        self.counters.errors += 1
        logger.warning(
            "PAPER market event loop lỗi; exception_type=%s reason_code=%s",
            type(error).__name__,
            self.last_error_code,
        )
