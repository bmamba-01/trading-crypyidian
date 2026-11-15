"""Gửi tin Telegram dạng **ảnh thẻ** (PNG + caption), dự phòng văn bản khi vẽ ảnh lỗi (TG-3).

Owner yêu cầu 15/11/2026: tín hiệu, thông báo mở/đóng lệnh và tổng kết gửi dạng thẻ có style thay
vì văn bản thuần. Mỗi hàm ở đây thay đúng một hàm gửi văn bản của worker:

- ``send_signal_card`` ↔ ``build_signal_message`` (worker tín hiệu);
- ``send_trade_result_card`` ↔ ``send_trade_result`` (worker kết quả);
- ``send_paper_report_card`` ↔ ``send_paper_report`` (tổng kết định kỳ).

Vẽ ảnh lỗi (dữ liệu thiếu/không hợp lệ, Pillow lỗi) → gửi văn bản cũ, không mất tin. Lỗi API
Telegram (``TelegramApiError``) **không** bị nuốt: worker vẫn ghi FAILED/retry như trước.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from trading_bot.adapters.telegram.card_renderer import (
    render_signal_card,
    render_summary_card,
    render_trade_card,
)
from trading_bot.application.telegram_cards import (
    SummaryBotRow,
    SummaryCardView,
    TradeCardView,
    signal_card_caption,
    signal_card_view_from,
    summary_card_caption,
    trade_card_caption,
)
from trading_bot.application.telegram_signals import (
    TelegramSignalCandidate,
    build_signal_message,
)
from trading_bot.application.telegram_trade_results import (
    PaperReportView,
    TradeResultView,
    format_paper_report_text,
    format_trade_result_text,
)
from trading_bot.ports.telegram import TelegramMessageSender

logger = logging.getLogger(__name__)

# Tên bot đợt thử có dạng "T01 H03 15m · 20 coin"; trích chiến lược/khung để hiển thị trên thẻ.
_BOT_NAME_STRATEGY = re.compile(r"\b(H\d{2}|CARD)\s+(\d+[mhd])\b")
_RENDER_ERRORS = (ValueError, TypeError, KeyError, ArithmeticError, OSError)


def _strategy_and_timeframe(bot_name: str) -> tuple[str, str]:
    match = _BOT_NAME_STRATEGY.search(bot_name)
    return (match.group(1), match.group(2)) if match else ("PAPER", "—")


async def send_signal_card(
    sender: TelegramMessageSender, chat_id: int, candidate: TelegramSignalCandidate
) -> None:
    """Thẻ tín hiệu tham khảo; dự phòng văn bản ``build_signal_message``."""
    try:
        view = signal_card_view_from(candidate)
        png = render_signal_card(view)
        caption = signal_card_caption(view)
    except _RENDER_ERRORS as error:
        logger.warning("Không vẽ được thẻ tín hiệu; gửi văn bản. error=%s", type(error).__name__)
        await sender.send_message(chat_id, build_signal_message(candidate))
        return
    await sender.send_photo(chat_id, png, caption)


def trade_card_view_from(view: TradeResultView) -> TradeCardView:
    """Chuyển view kết quả trade (ledger) sang view thẻ ảnh."""
    strategy_id, timeframe = _strategy_and_timeframe(view.bot_name)
    return TradeCardView(
        event="CLOSED" if view.kind.value == "CLOSED" else "OPENED",
        trade_id=str(view.trade_id),
        bot_name=view.bot_name,
        symbol=view.symbol,
        direction="SHORT" if view.direction.upper() == "SHORT" else "LONG",
        strategy_id=strategy_id,
        timeframe=timeframe,
        entry_price=view.entry_price,
        quantity=view.entry_quantity,
        opened_at=view.opened_at,
        exit_price=view.exit_price,
        exit_reason_vi=view.exit_reason_vi,
        net_pnl=view.net_pnl,
        r_multiple=view.r_multiple,
        closed_at=view.closed_at,
    )


async def send_trade_result_card(
    sender: TelegramMessageSender, chat_id: int, view: TradeResultView
) -> None:
    """Thẻ mở/đóng vị thế PAPER; dự phòng văn bản ``format_trade_result_text``."""
    try:
        card = trade_card_view_from(view)
        png = render_trade_card(card)
        caption = trade_card_caption(card)
    except _RENDER_ERRORS as error:
        logger.warning("Không vẽ được thẻ kết quả; gửi văn bản. error=%s", type(error).__name__)
        await sender.send_message(chat_id, format_trade_result_text(view))
        return
    await sender.send_photo(chat_id, png, caption)


def summary_card_view_from(view: PaperReportView) -> SummaryCardView:
    """Bảng so sánh bot: số liệu lũy kế (lệnh đóng, tỷ lệ thắng, PnL ròng) + vị thế mở.

    ``max_drawdown`` của báo cáo là số tiền (USDT) trên đường PnL đã chốt, còn thẻ hiển thị phân
    số; quy đổi theo vốn seed 10.000 USDT của đợt thử PAPER (giả định ghi rõ trong memory).
    """
    hours = (view.window_end - view.window_start).total_seconds() / 3600
    rows = []
    for row in view.rows:
        strategy_id, timeframe = _strategy_and_timeframe(row.bot_name)
        rows.append(
            SummaryBotRow(
                bot_name=row.bot_name,
                strategy_id=strategy_id,
                timeframe=timeframe,
                trades_closed=row.total_closed_count,
                wins=row.total_winning_count,
                net_pnl=row.total_net_pnl,
                open_positions=row.open_positions,
                max_drawdown=(row.max_drawdown / Decimal("10000")) if row.max_drawdown else None,
            )
        )
    return SummaryCardView(
        window_label=f"lũy kế; cửa sổ {hours:g} giờ qua",
        generated_at=view.window_end,
        rows=tuple(rows),
    )


async def send_paper_report_card(
    sender: TelegramMessageSender, chat_id: int, view: PaperReportView
) -> None:
    """Thẻ tổng kết; dự phòng văn bản ``format_paper_report_text``."""
    try:
        card = summary_card_view_from(view)
        png = render_summary_card(card)
        caption = summary_card_caption(card)
    except _RENDER_ERRORS as error:
        logger.warning("Không vẽ được thẻ tổng kết; gửi văn bản. error=%s", type(error).__name__)
        await sender.send_message(chat_id, format_paper_report_text(view))
        return
    await sender.send_photo(chat_id, png, caption)


__all__ = [
    "send_paper_report_card",
    "send_signal_card",
    "send_trade_result_card",
    "summary_card_view_from",
    "trade_card_view_from",
]
