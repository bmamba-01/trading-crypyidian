"""Unit test TG-3: gửi tín hiệu/kết quả/tổng kết dạng ảnh thẻ, dự phòng văn bản khi vẽ lỗi."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from tests.unit.test_telegram_signal_delivery import _candidate, _card

from trading_bot.adapters.telegram import card_delivery
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.application.telegram_trade_results import (
    BotSummaryRow,
    PaperReportView,
    TradeNotificationKind,
    TradeResultView,
)
from trading_bot.domain.strategy.signal_card import _price_vi

PNG = b"\x89PNG\r\n\x1a\n"
NOW = datetime(2026, 11, 15, 16, 0, tzinfo=UTC)


def _photos(sender: RecordingTelegramSender) -> list:
    return list(sender.photos)


def test_signal_card_sends_png_with_caption() -> None:
    sender = RecordingTelegramSender()
    asyncio.run(card_delivery.send_signal_card(sender, 42, _candidate(card=_card())))
    [photo] = _photos(sender)
    assert photo[0] == 42
    assert photo[1].startswith(PNG)
    assert "BTC" in photo[2]
    assert sender.messages == []


def test_signal_card_falls_back_to_text_when_render_fails(monkeypatch) -> None:
    def broken(_view: object) -> bytes:
        raise ValueError("vẽ lỗi")

    monkeypatch.setattr(card_delivery, "render_signal_card", broken)
    sender = RecordingTelegramSender()
    asyncio.run(card_delivery.send_signal_card(sender, 42, _candidate(card=_card())))
    assert _photos(sender) == []
    [(chat, text)] = sender.messages
    assert chat == 42
    assert "KHÔNG PHẢI LỆNH" in text


def _trade_view(kind: TradeNotificationKind) -> TradeResultView:
    closed = kind is TradeNotificationKind.CLOSED
    return TradeResultView(
        kind=kind,
        bot_id=UUID(int=1),
        bot_name="T04 H03 1h · 4 coin",
        trade_id=UUID(int=2),
        symbol="BTWUSDT",
        direction="SHORT",
        quote_asset="USDT",
        entry_price=Decimal("0.01234"),
        entry_quantity=Decimal("1000"),
        opened_at=NOW,
        exit_price=Decimal("0.01200") if closed else None,
        closed_at=NOW + timedelta(hours=3) if closed else None,
        exit_reason_vi="Chạm cắt lỗ (SL)" if closed else None,
        net_pnl=Decimal("0.31") if closed else None,
        r_multiple=Decimal("0.5") if closed else None,
    )


def test_trade_cards_for_open_and_close_use_bot_strategy_and_timeframe() -> None:
    sender = RecordingTelegramSender()
    for kind in TradeNotificationKind:
        asyncio.run(card_delivery.send_trade_result_card(sender, 7, _trade_view(kind)))
    photos = _photos(sender)
    assert len(photos) == 2
    assert all(photo[1].startswith(PNG) for photo in photos)
    card = card_delivery.trade_card_view_from(_trade_view(TradeNotificationKind.CLOSED))
    assert (card.strategy_id, card.timeframe, card.direction, card.event) == (
        "H03",
        "1h",
        "SHORT",
        "CLOSED",
    )


def test_summary_card_maps_totals_and_drawdown_fraction() -> None:
    row = BotSummaryRow(
        bot_id=UUID(int=3),
        bot_name="T06 H03 15m · 20 coin",
        quote_asset="USDT",
        window_closed_count=1,
        window_net_pnl=Decimal("-12"),
        total_closed_count=4,
        total_winning_count=1,
        total_net_pnl=Decimal("-30"),
        open_positions=1,
        max_drawdown=Decimal("250"),
    )
    view = PaperReportView(
        window_start=NOW - timedelta(hours=4),
        window_end=NOW,
        total_start=NOW - timedelta(days=1),
        rows=(row,),
    )
    card = card_delivery.summary_card_view_from(view)
    [mapped] = card.rows
    assert (mapped.trades_closed, mapped.wins, mapped.net_pnl) == (4, 1, Decimal("-30"))
    assert mapped.max_drawdown == Decimal("0.025")
    assert (mapped.strategy_id, mapped.timeframe) == ("H03", "15m")
    sender = RecordingTelegramSender()
    asyncio.run(card_delivery.send_paper_report_card(sender, 9, view))
    assert _photos(sender)[0][1].startswith(PNG)


def test_invalidation_price_is_vietnamese_formatted() -> None:
    assert _price_vi(Decimal("96619.30")) == "96.619,3"
    assert _price_vi(Decimal("0.00001234")) == "0,00001234"
    assert _price_vi(Decimal("-1200")) == "-1.200"
