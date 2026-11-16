"""Xuất ảnh mẫu thẻ Telegram (TG-3) vào ``docs/design/telegram-cards/`` để owner duyệt.

Dữ liệu là DỮ LIỆU MẪU giả lập (không lấy từ ledger/DB); mọi ảnh có chip "DỮ LIỆU MẪU".
Chạy: ``PYTHONPATH=. uv run python scripts/render_telegram_card_samples.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from trading_bot.adapters.telegram.card_renderer import (
    render_signal_card,
    render_summary_card,
    render_trade_card,
)
from trading_bot.application.telegram_cards import (
    SummaryBotRow,
    SummaryCardView,
    TradeCardView,
    signal_card_view_from,
)
from trading_bot.application.telegram_signals import TelegramSignalCandidate
from trading_bot.domain.strategy.signal_card import build_signal_card

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "design" / "telegram-cards"
SIGNAL_AT = datetime(2026, 11, 15, 7, 30, tzinfo=UTC)


def _signal(
    *, symbol: str, direction: str, reference: str, atr: str, checklist: list[dict[str, object]]
) -> TelegramSignalCandidate:
    card = build_signal_card(
        {
            "symbol": symbol,
            "direction": direction.lower(),
            "strategy_id": "ema_pullback_trend",
            "strategy_release_id": "rel-2026-11-14-ema-pullback-v3",
            "reason_code": "EMA_PULLBACK_CONFIRMED",
            "signal_at": SIGNAL_AT.isoformat(),
            "expires_at": (SIGNAL_AT + timedelta(hours=1)).isoformat(),
            "reference_price": reference,
            "atr_value": atr,
            "recipe": {
                "stop_atr_multiple": "1.5",
                "take_profit_atr_multiple": "2.5",
                "trailing_atr_multiple": "1.0",
                "max_hold_bars": 48,
            },
        },
        checklist=checklist,
    )
    return TelegramSignalCandidate(
        decision_id=UUID("3f1c9a2e-7b4d-4c1e-9a6f-2d8e5b7c4a10"),
        symbol=symbol,
        direction=direction,
        timeframe="15m",
        strategy_id="ema_pullback_trend",
        strategy_release_id="rel-2026-11-14-ema-pullback-v3",
        reference_price=Decimal(reference),
        signal_at=SIGNAL_AT,
        expires_at=SIGNAL_AT + timedelta(hours=1),
        reason_code="EMA_PULLBACK_CONFIRMED",
        detail_vi="Giá hồi về EMA 21 trong xu hướng tăng khung 1h, nến xác nhận đóng trên EMA "
        "với khối lượng cao hơn trung bình 20 nến.",
        card=card,
    )


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    long_checklist: list[dict[str, object]] = [
        {
            "label_vi": "Xu hướng 1h: EMA 50 trên EMA 200",
            "actual": True,
            "operator": "==",
            "threshold": True,
            "passed": True,
        },
        {
            "label_vi": "RSI 14 trong vùng hồi",
            "actual": 46.2,
            "operator": "<=",
            "threshold": 55,
            "passed": True,
        },
        {
            "label_vi": "Khối lượng / TB 20 nến",
            "actual": 1.38,
            "operator": ">=",
            "threshold": 1.2,
            "passed": True,
        },
        {
            "label_vi": "Spread hiện tại (bps)",
            "actual": 1.1,
            "operator": "<=",
            "threshold": 3,
            "passed": True,
        },
        {
            "label_vi": "Funding rate 8h",
            "actual": 0.031,
            "operator": "<=",
            "threshold": 0.03,
            "passed": False,
        },
        {
            "label_vi": "Tin vĩ mô trong 30 phút tới",
            "actual": None,
            "operator": "==",
            "threshold": "không",
            "passed": None,
        },
    ]
    signal = signal_card_view_from(
        _signal(
            symbol="BTCUSDT",
            direction="LONG",
            reference="97250.5",
            atr="420.8",
            checklist=long_checklist,
        ),
        sample=True,
    )
    (OUTPUT / "signal-card-sample.png").write_bytes(render_signal_card(signal))

    short_signal = signal_card_view_from(
        _signal(
            symbol="1000PEPEUSDT",
            direction="SHORT",
            reference="0.01234567",
            atr="0.00021",
            checklist=long_checklist[:4],
        ),
        sample=True,
    )
    (OUTPUT / "signal-card-short-sample.png").write_bytes(render_signal_card(short_signal))

    opened = TradeCardView(
        event="OPENED",
        trade_id="9b2f61d0-55aa-4f0e-8c11-0e5c2b7d9a33",
        bot_name="Bot BTC xu hướng",
        symbol="BTCUSDT",
        direction="LONG",
        strategy_id="ema_pullback_trend",
        timeframe="15m",
        entry_price=Decimal("97262.1"),
        quantity=Decimal("0.05"),
        stop_price=Decimal("96619.3"),
        target_price=Decimal("98302.5"),
        opened_at=SIGNAL_AT + timedelta(minutes=15),
        sample=True,
    )
    (OUTPUT / "trade-opened-sample.png").write_bytes(render_trade_card(opened))
    closed = TradeCardView(
        event="CLOSED",
        trade_id="9b2f61d0-55aa-4f0e-8c11-0e5c2b7d9a33",
        bot_name="Bot BTC xu hướng",
        symbol="BTCUSDT",
        direction="LONG",
        strategy_id="ema_pullback_trend",
        timeframe="15m",
        entry_price=Decimal("97262.1"),
        quantity=Decimal("0.05"),
        stop_price=Decimal("96619.3"),
        target_price=Decimal("98302.5"),
        exit_price=Decimal("98302.5"),
        exit_reason_vi="Chạm mục tiêu (TP)",
        net_pnl=Decimal("47.68"),
        r_multiple=Decimal("1.48"),
        fees=Decimal("4.34"),
        opened_at=SIGNAL_AT + timedelta(minutes=15),
        closed_at=SIGNAL_AT + timedelta(hours=3, minutes=20),
        sample=True,
    )
    (OUTPUT / "trade-closed-sample.png").write_bytes(render_trade_card(closed))
    loss = TradeCardView(
        event="CLOSED",
        trade_id="0c7e2a91-1f3b-4d6e-a8b2-6f4d1c9e7b55",
        bot_name="Bot SOL đảo chiều",
        symbol="SOLUSDT",
        direction="SHORT",
        strategy_id="rsi_mean_reversion",
        timeframe="5m",
        entry_price=Decimal("212.45"),
        quantity=Decimal("12"),
        stop_price=Decimal("214.9"),
        target_price=Decimal("208.1"),
        exit_price=Decimal("214.9"),
        exit_reason_vi="Chạm cắt lỗ (SL)",
        net_pnl=Decimal("-31.27"),
        r_multiple=Decimal("-1.06"),
        fees=Decimal("1.87"),
        opened_at=SIGNAL_AT,
        closed_at=SIGNAL_AT + timedelta(minutes=47),
        sample=True,
    )
    (OUTPUT / "trade-closed-loss-sample.png").write_bytes(render_trade_card(loss))

    names = [
        ("Bot BTC xu hướng", "ema_pullback_trend", "15m", 14, 9, "312.40", 1, "0.031"),
        ("Bot ETH breakout", "donchian_breakout", "1h", 6, 3, "88.15", 0, "0.022"),
        ("Bot SOL đảo chiều", "rsi_mean_reversion", "5m", 22, 10, "-64.90", 2, "0.058"),
        ("Bot BNB kênh giá", "keltner_channel", "15m", 9, 5, "41.02", 0, "0.017"),
        ("Bot XRP scalping phiên Á", "vwap_scalp", "1m", 37, 19, "-12.33", 1, "0.044"),
        ("Bot DOGE động lượng", "momentum_roc", "5m", 11, 4, "-102.75", 0, "0.091"),
        ("Bot ADA hồi quy", "zscore_reversion", "15m", 8, 5, "23.60", 1, "0.012"),
        ("Bot LINK xu hướng dài", "ema_pullback_trend", "4h", 2, 2, "57.80", 1, "0.008"),
        ("Bot AVAX phá nền", "range_breakout", "1h", 5, 1, "-48.10", 0, "0.064"),
        ("Bot 1000PEPE thử nghiệm", "volume_spike", "5m", 0, 0, "0", 0, None),
    ]
    summary = SummaryCardView(
        window_label="24 giờ qua",
        generated_at=SIGNAL_AT + timedelta(hours=13),
        rows=tuple(
            SummaryBotRow(
                bot_name=name,
                strategy_id=strategy,
                timeframe=timeframe,
                trades_closed=trades,
                wins=wins,
                net_pnl=Decimal(pnl),
                open_positions=open_positions,
                max_drawdown=Decimal(dd) if dd else None,
            )
            for name, strategy, timeframe, trades, wins, pnl, open_positions, dd in names
        ),
        sample=True,
    )
    (OUTPUT / "summary-card-sample.png").write_bytes(render_summary_card(summary))
    for path in sorted(OUTPUT.glob("*.png")):
        print(path.relative_to(OUTPUT.parents[2]), path.stat().st_size)


if __name__ == "__main__":
    main()
