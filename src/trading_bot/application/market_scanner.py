"""Bộ quét thị trường (CK-11): đọc nến đã lưu của workspace, quét và ghi nhật ký lượt quét.

Không gọi mạng sàn (không API ký, không REST): chỉ những mã đã có nến trong kho nội bộ. Quyền:
``workspace:read``. Nhật ký quét dùng ``audit_event`` (action ``market_scan.run``) — mỗi lượt bấm
"Chạy quét" ghi tiêu chí, số mã đạt và hash kết quả.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import AuditEvent
from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
from trading_bot.application.bot_reporting import preferred_market_source
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, authorize
from trading_bot.domain.market.scanner import SCAN_INTERVALS, ScanCriteria, scan_markets

SCAN_ACTION = "market_scan.run"


class MarketScannerError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _summary(result: dict[str, Any]) -> str:
    criteria = result["criteria"]
    return (
        f"khung {result['timeframe']}; thanh khoản quote 24h ≥ "
        f"{criteria['min_quote_volume_24h']:,.0f}; biên 21 ngày "
        f"{criteria['range_min_pct']:g}–{criteria['range_max_pct']:g}%; "
        f"đạt {result['passed']}/{result['universe']} mã"
    )


class MarketScannerService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        report_reader_sessions: async_sessionmaker[AsyncSession] | None = None,
        unit_of_work: Callable[..., Any] = UnitOfWork,
        repository: Callable[[Any], Any] = BotReportRepository,
    ) -> None:
        self._sessions = sessions
        self._report_reader_sessions = report_reader_sessions
        self._unit_of_work = unit_of_work
        self._repository = repository

    async def scan(
        self,
        principal: Principal,
        *,
        timeframe: str,
        criteria: ScanCriteria,
        now: datetime | None = None,
        record: bool = False,
    ) -> dict[str, Any]:
        if not authorize(principal, Permission.WORKSPACE_READ):
            raise MarketScannerError(403, "Bạn không có quyền xem bộ quét.")
        if timeframe not in SCAN_INTERVALS:
            raise MarketScannerError(422, "Bộ quét chỉ dùng khung 1h, 4h hoặc 1d.")
        if self._report_reader_sessions is None:
            raise MarketScannerError(503, "Kho dữ liệu thị trường chưa sẵn sàng.")
        instant = now or datetime.now(UTC)
        limit = 21 * 86400 // SCAN_INTERVALS[timeframe] + 12
        async with self._unit_of_work(
            self._report_reader_sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            repository = self._repository(unit.session)
            sources: dict[str, dict[str, list[str]]] = {}
            for pair in await repository.list_market_series(
                workspace_id=principal.workspace_id, as_of=instant
            ):
                symbol, frame, source = pair.split("|", 2)
                sources.setdefault(symbol, {}).setdefault(frame, []).append(source)
            series: dict[str, list[dict[str, Any]]] = {}
            for symbol in sorted(sources):
                frames = sources[symbol]
                if timeframe not in frames:
                    series[symbol] = []
                    continue
                series[symbol] = await repository.load_recent_bars(
                    workspace_id=principal.workspace_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    as_of=instant,
                    limit=limit,
                    source=preferred_market_source(frames[timeframe]),
                )
            result = scan_markets(series, timeframe=timeframe, now=instant, criteria=criteria)
            result["generated_at"] = instant.isoformat()
            result["summary"] = _summary(result)
        if record:
            async with self._unit_of_work(
                self._sessions,
                workspace_id=principal.workspace_id,
                actor_membership_id=principal.membership_id,
            ) as audit_unit:
                audit_unit.record_audit(
                    action=SCAN_ACTION,
                    target_type="market_scan",
                    reason=result["summary"],
                    after_state={
                        "criteria": result["criteria"],
                        "timeframe": timeframe,
                        "movers": [row["symbol"] for row in result["movers_7d"]],
                        "range": [row["symbol"] for row in result["top_range_21d"]],
                        "volume": [row["symbol"] for row in result["top_volume"]],
                    },
                )
        return result

    async def recent_runs(self, principal: Principal, *, limit: int = 10) -> list[dict[str, Any]]:
        """Nhật ký quét gần nhất của workspace (từ ``audit_event``)."""
        if not authorize(principal, Permission.WORKSPACE_READ):
            raise MarketScannerError(403, "Bạn không có quyền xem bộ quét.")
        async with self._unit_of_work(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            snapshot_read_only=True,
        ) as unit:
            rows = (
                await unit.session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.workspace_id == principal.workspace_id,
                        AuditEvent.action == SCAN_ACTION,
                    )
                    .order_by(AuditEvent.occurred_at.desc())
                    .limit(limit)
                )
            ).all()
        return [
            {
                "occurred_at": row.occurred_at.isoformat(),
                "summary": row.reason,
                "result_hash": row.after_hash,
            }
            for row in rows
        ]
