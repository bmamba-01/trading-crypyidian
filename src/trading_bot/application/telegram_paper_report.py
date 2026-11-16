"""TG-3: dựng tin tổng hợp PAPER theo yêu cầu cho lệnh Telegram ``/report``.

Đi qua cùng service với Admin Web nên giữ nguyên kiểm quyền:

- ``BotOperationsService.list_bots`` chỉ trả bot thành viên được ``bot:read`` (lấy tên bot).
- ``BotReportingService.get_dashboard_reports`` kiểm lại ``bot:report:read`` từng bot trong một
  snapshot chỉ đọc; bot thiếu quyền bị bỏ qua, không lộ số liệu.

Số liệu dùng cùng định nghĩa với tin định kỳ của signal-worker: cửa sổ ``[now - interval, now)``
và "tổng" là 366 ngày gần nhất theo ``trade.closed_at``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_bot.application.bot_reporting import BotReportingService, BotReportQuery
from trading_bot.application.telegram_trade_results import (
    SUMMARY_TOTAL_WINDOW,
    BotSummaryRow,
    ClosedTradeFact,
    PaperReportView,
    build_bot_summary_row,
    build_paper_report_view,
)
from trading_bot.auth.policy import Principal


class TelegramPaperReportService:
    """Tổng hợp kết quả PAPER của các bot người dùng được xem báo cáo."""

    def __init__(
        self,
        *,
        reporting: BotReportingService,
        operations: Any,
        interval: timedelta = timedelta(hours=4),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if interval < timedelta(minutes=1):
            raise ValueError("Chu kỳ tổng hợp tối thiểu 1 phút.")
        self._reporting = reporting
        self._operations = operations
        self._interval = interval
        self._now = now or (lambda: datetime.now(UTC))

    async def build_view(self, principal: Principal) -> PaperReportView:
        """View model tổng hợp cho ``principal``; bot không có quyền báo cáo bị bỏ qua."""
        end = self._now()
        window_start = end - self._interval
        total_start = end - SUMMARY_TOTAL_WINDOW
        bots = await self._operations.for_principal(principal).list_bots()
        names = {item.bot_id: item.name for item in bots}
        if not names:
            return build_paper_report_view(
                window_start=window_start, window_end=end, rows=(), total_start=total_start
            )
        reports = await self._reporting.get_dashboard_reports(
            BotReportQuery(
                workspace_id=principal.workspace_id,
                bot_id=None,
                start=total_start,
                end=end,
            ),
            principal,
            bot_ids=frozenset(names),
        )
        rows: list[BotSummaryRow] = []
        for report in reports:
            closed = [
                ClosedTradeFact(trade.trade_id, trade.net_pnl, trade.closed_at)
                for trade in report.closed_trades
                if trade.closed_at is not None
            ]
            quotes = sorted(report.totals)
            rows.append(
                build_bot_summary_row(
                    bot_id=report.bot_id,
                    bot_name=names.get(report.bot_id, str(report.bot_id)),
                    closed_trades=closed,
                    open_positions=len(report.payload.get("open_trades") or ()),
                    window_start=window_start,
                    window_end=end,
                    quote_asset=quotes[0] if len(quotes) == 1 else "USDT",
                )
            )
        return build_paper_report_view(
            window_start=window_start, window_end=end, rows=rows, total_start=total_start
        )
