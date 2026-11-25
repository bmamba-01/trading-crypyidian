"""Dịch vụ đọc cho chế độ theo dõi bot trực tiếp (LIVE-MON, CRY-429).

Mỗi lần đọc mở transaction quyền admin và transaction READ ONLY/REPEATABLE READ ngắn trên
pool report-reader. Session reader đặt RLS theo workspace + membership; PostgreSQL kiểm grant
hiện hành trên từng hàng trước khi trả ledger. Không giữ kết nối giữa các lần đọc, không khóa
hàng và không gọi runtime/sàn, nên không chặn runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.bot_grants import BotGrantRepository
from trading_bot.adapters.database.repositories.live_monitor import (
    EVENT_SOURCE_LIMIT,
    LiveMonitorRepository,
)
from trading_bot.application.bot_reporting import AdminReportReadUnitOfWork
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal
from trading_bot.domain.monitoring.live_monitor import (
    LiveCursor,
    build_equity_view,
    build_feed_health,
    build_live_events,
    build_position_view,
    measure_clock_skew_ms,
)

MAX_LIVE_BOTS = 50
RUNTIME_HEARTBEAT_STALE_SECONDS = 120


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except ArithmeticError, ValueError:
        return None


def _sum(values: Sequence[Decimal | None]) -> Decimal | None:
    known = [value for value in values if value is not None]
    return sum(known, Decimal(0)) if known else None


class LiveMonitorService:
    """Ghép sự kiện, vị thế, vốn và sức khỏe nguồn cho các bot principal được phép xem."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        report_reader_sessions: async_sessionmaker[AsyncSession] | None = None,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = LiveMonitorRepository,
        grants: Callable[[Any], Any] = BotGrantRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._report_reader_sessions = report_reader_sessions
        self._unit_of_work = unit_of_work
        self._repository = repository
        self._grants = grants
        self._clock = clock

    def _read_unit(self, **scope: Any) -> AdminReportReadUnitOfWork:
        return AdminReportReadUnitOfWork(
            self._sessions,
            self._report_reader_sessions,
            unit_of_work=self._unit_of_work,
            **scope,
        )

    async def read(
        self,
        principal: Principal,
        *,
        bot_ids: Sequence[UUID],
        since: LiveCursor | None,
        include_state: bool,
        runtime: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Sự kiện mới sau ``since`` (+ trạng thái nếu ``include_state``) của các bot được xem.

        ``runtime`` là trạng thái điều khiển đã lọc theo quyền ``bot:read`` (BotOperations);
        bot không có trong đó hoặc thiếu grant ``bot:report:read`` bị bỏ qua lặng lẽ.
        """
        requested = [bot_id for bot_id in dict.fromkeys(bot_ids) if str(bot_id) in runtime]
        requested = requested[:MAX_LIVE_BOTS]
        async with self._read_unit(
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            grants = self._grants(unit.authorization_session)
            allowed: list[UUID] = []
            for bot_id in requested:
                try:
                    await grants.require(
                        principal, bot_id=bot_id, permission=BotPermission.REPORT_READ
                    )
                except PermissionError:
                    continue
                allowed.append(bot_id)
            repository = self._repository(unit.session)
            now = self._clock()
            rows = await repository.events(
                workspace_id=principal.workspace_id,
                bot_ids=allowed,
                since=since.query_floor() if since else None,
            )
            state = (
                await self._state(repository, principal, allowed, runtime, now)
                if include_state
                else None
            )
            db_now = state.pop("_db_now") if state else await repository.database_now()
        events = build_live_events(rows)
        return {
            "bot_ids": [str(bot_id) for bot_id in allowed],
            "events": events,
            "cursor": _next_cursor(rows, since, db_now),
            "state": state,
        }

    async def _state(
        self,
        repository: Any,
        principal: Principal,
        bot_ids: list[UUID],
        runtime: Mapping[str, Mapping[str, Any]],
        now: datetime,
    ) -> dict[str, Any]:
        workspace_id = principal.workspace_id
        open_rows = await repository.open_positions(workspace_id=workspace_id, bot_ids=bot_ids)
        prices = await repository.latest_prices(
            workspace_id=workspace_id, symbols=[row["symbol"] for row in open_rows]
        )
        positions = [
            build_position_view(row | {"mark": prices.get(row["symbol"])}, now=now)
            for row in open_rows
        ]
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        equity_inputs = await repository.equity_inputs(
            workspace_id=workspace_id, bot_ids=bot_ids, day_start=day_start
        )
        clock = await repository.feed_clock(workspace_id=workspace_id)
        sent = self._clock()
        db_now = await repository.database_now()
        skew_ms = measure_clock_skew_ms(sent, db_now, self._clock())
        outbox = await repository.outbox_stats(workspace_id=workspace_id)
        heartbeats = await repository.runtime_heartbeats(workspace_id=workspace_id, bot_ids=bot_ids)

        seeds = equity_inputs["seeds"]
        upnl_by_bot: dict[str, list[Decimal | None]] = {}
        for position in positions:
            upnl_by_bot.setdefault(position["bot_id"], []).append(
                _decimal(position["unrealized_pnl"])
            )
        bots: list[dict[str, Any]] = []
        for bot_id in bot_ids:
            key = str(bot_id)
            stats = equity_inputs["per_bot"].get(bot_id) or {}
            seed = seeds.get(bot_id) or {}
            upnl = upnl_by_bot.get(key, [])
            equity = build_equity_view(
                seed_equity=_decimal(seed.get("initial_equity")),
                closed=[],
                unrealized=_sum(upnl) if upnl else Decimal(0),
                now=now,
                realized_before=_decimal(stats.get("realized")) or Decimal(0),
                peak_realized_before=_decimal(stats.get("peak_realized")) or Decimal(0),
            )
            control = runtime.get(key, {})
            heartbeat = heartbeats.get(bot_id)
            heartbeat_age = (
                max(0, int((now - heartbeat).total_seconds()))
                if isinstance(heartbeat, datetime)
                else None
            )
            bots.append(
                {
                    "id": key,
                    "name": control.get("name"),
                    "status": control.get("status"),
                    "entry_state": control.get("entry_state"),
                    "readiness": control.get("readiness"),
                    "readiness_reason_code": control.get("readiness_reason_code"),
                    "entry_allowed": bool(control.get("entry_allowed")),
                    "quote_asset": seed.get("quote_asset"),
                    "open_positions": len(upnl),
                    "closed_trades": int(stats.get("closed_count") or 0),
                    "net_today": _text(_decimal(equity_inputs["today"].get(bot_id))),
                    "heartbeat_age_seconds": heartbeat_age,
                    "heartbeat_stale": heartbeat_age is None
                    or heartbeat_age > RUNTIME_HEARTBEAT_STALE_SECONDS,
                    "equity": equity,
                }
            )

        seed_total = _sum([_decimal(seed.get("initial_equity")) for seed in seeds.values()])
        before = equity_inputs["before"] or {}
        all_upnl = [value for values in upnl_by_bot.values() for value in values]
        aggregate = build_equity_view(
            seed_equity=seed_total,
            closed=[
                (closed_at, _decimal(net) or Decimal(0))
                for closed_at, net in equity_inputs["curve"]
            ],
            unrealized=_sum(all_upnl) if all_upnl else Decimal(0),
            now=now,
            realized_before=_decimal(before.get("realized")) or Decimal(0),
            peak_realized_before=_decimal(before.get("peak")) or Decimal(0),
        )
        net_today = _sum([_decimal(value) for value in equity_inputs["today"].values()])
        workspace_kill = next(
            (str(row.get("workspace_kill_state")) for row in runtime.values()), "ACTIVE"
        )
        health = build_feed_health(
            now=now,
            clock_skew_ms=skew_ms,
            latest_mark_at=clock.get("latest_mark_at"),
            latest_bar_close_at=clock.get("latest_bar_close_at"),
            outbox_oldest_pending_at=outbox["oldest"],
            outbox_pending=outbox["pending"],
            outbox_unconsumed_pending=outbox["unconsumed_pending"],
            daily_loss=-(net_today or Decimal(0)),
            drawdown_pct=_decimal(aggregate["drawdown_pct"]),
            api_error_count=0,
            unprotected_positions=sum(1 for position in positions if not position["protected"]),
        )
        return {
            "_db_now": db_now,
            "observed_at": now.isoformat(),
            "workspace": {"kill_state": workspace_kill},
            "bots": bots,
            "positions": positions,
            "equity": aggregate | {"net_today": _text(net_today)},
            "health": health,
        }


def _text(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


def _next_cursor(
    rows: Sequence[Mapping[str, Any]], since: LiveCursor | None, db_now: datetime
) -> str:
    """Con trỏ kế tiếp: thời điểm ghi lớn nhất đã đọc; nguồn chạm LIMIT kéo con trỏ lùi lại.

    Không có sự kiện mới thì giữ con trỏ cũ (lần đầu dùng giờ DB) để không nhảy qua hàng
    của transaction còn đang mở.
    """
    latest: datetime | None = None
    cap: datetime | None = None
    by_source: dict[str, list[datetime]] = {}
    for row in rows:
        recorded = row.get("recorded_at")
        if not isinstance(recorded, datetime):
            continue
        by_source.setdefault(str(row.get("source")), []).append(recorded)
        latest = recorded if latest is None or recorded > latest else latest
    if since is not None:
        for values in by_source.values():
            if len(values) >= EVENT_SOURCE_LIMIT:
                last = max(values)
                cap = last if cap is None or last < cap else cap
    if latest is None:
        return (since or LiveCursor(db_now)).encode()
    if since is None:
        # Lượt đầu chỉ lấy N hàng gần nhất mỗi nguồn; từ đây trở đi đọc theo giờ DB hiện tại.
        latest = max(latest, db_now)
    if cap is not None and cap < latest:
        latest = cap
    if since is not None and latest < since.recorded_at:
        latest = since.recorded_at
    return LiveCursor(latest).encode()
