"""TG-3: view thẻ ảnh Telegram, renderer PNG (Pillow) và sendPhoto multipart."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from PIL import Image, ImageFont

from trading_bot.adapters.telegram.bot_api_sender import TelegramApiError, TelegramBotApiSender
from trading_bot.adapters.telegram.card_renderer import (
    CARD_WIDTH,
    _fit,
    _font,
    _wrap,
    render_signal_card,
    render_summary_card,
    render_trade_card,
)
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.application.telegram_cards import (
    MAX_CAPTION_CHARS,
    CardChecklistItem,
    CardTarget,
    SignalCardView,
    SummaryBotRow,
    SummaryCardView,
    TradeCardView,
    base_asset,
    format_duration_vi,
    format_local_time,
    format_percent_vi,
    format_price_vi,
    format_r_vi,
    format_usdt_vi,
    signal_card_caption,
    signal_card_view_from,
    summary_card_caption,
    trade_card_caption,
)
from trading_bot.application.telegram_signals import TelegramSignalCandidate, build_signal_message
from trading_bot.domain.strategy.signal_card import build_signal_card

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 1_000_000
NOW = datetime(2026, 11, 15, 7, 30, tzinfo=UTC)
LONG_LABEL = (
    "Điều kiện rất dài về xu hướng khung lớn kết hợp động lượng, thanh khoản, "
    "funding, open interest và độ lệch chuẩn của biến động trong phiên Á–Âu–Mỹ "
) * 3


def _image(data: bytes) -> Image.Image:
    assert data.startswith(PNG_SIGNATURE)
    assert len(data) < MAX_PNG_BYTES
    image = Image.open(io.BytesIO(data))
    image.load()
    assert image.format == "PNG"
    assert image.width == CARD_WIDTH
    # Giới hạn sendPhoto: rộng + cao ≤ 10000, tỉ lệ ≤ 20.
    assert image.width + image.height <= 10_000
    assert image.height / image.width <= 20
    return image


def _candidate(
    *,
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    reference: str = "97250.5",
    atr: str = "420.8",
    checklist: list[dict[str, object]] | None = None,
    with_card: bool = True,
    detail: str = "Giá hồi về EMA 21 trong xu hướng tăng, nến xác nhận đóng trên EMA.",
) -> TelegramSignalCandidate:
    card = None
    if with_card:
        card = build_signal_card(
            {
                "symbol": symbol,
                "direction": direction.lower(),
                "strategy_id": "ema_pullback_trend",
                "strategy_release_id": "rel-ema-v3",
                "reference_price": reference,
                "atr_value": atr,
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                "recipe": {
                    "stop_atr_multiple": "1.5",
                    "take_profit_atr_multiple": "2.5",
                    "max_hold_bars": 48,
                },
            },
            checklist=checklist or [],
        )
    return TelegramSignalCandidate(
        decision_id=UUID("3f1c9a2e-7b4d-4c1e-9a6f-2d8e5b7c4a10"),
        symbol=symbol,
        direction=direction,
        timeframe="15m",
        strategy_id="ema_pullback_trend",
        strategy_release_id="rel-ema-v3",
        reference_price=Decimal(reference),
        signal_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        reason_code="EMA_PULLBACK_CONFIRMED",
        detail_vi=detail,
        card=card,
    )


CHECKLIST: list[dict[str, object]] = [
    {"label_vi": "RSI 14", "actual": 46.2, "operator": "<=", "threshold": 55, "passed": True},
    {
        "label_vi": "Funding 8h",
        "actual": 0.031,
        "operator": "<=",
        "threshold": 0.03,
        "passed": False,
    },
    {
        "label_vi": "Tin vĩ mô",
        "actual": None,
        "operator": "==",
        "threshold": "không",
        "passed": None,
    },
]


def _trade(**overrides: object) -> TradeCardView:
    values: dict[str, object] = {
        "event": "CLOSED",
        "trade_id": "9b2f61d0-55aa-4f0e-8c11-0e5c2b7d9a33",
        "bot_name": "Bot BTC xu hướng",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "strategy_id": "ema_pullback_trend",
        "timeframe": "15m",
        "entry_price": Decimal("97262.1"),
        "quantity": Decimal("0.05"),
        "stop_price": Decimal("96619.3"),
        "target_price": Decimal("98302.5"),
        "exit_price": Decimal("98302.5"),
        "exit_reason_vi": "Chạm mục tiêu (TP)",
        "net_pnl": Decimal("47.68"),
        "r_multiple": Decimal("1.48"),
        "opened_at": NOW,
        "closed_at": NOW + timedelta(hours=3, minutes=5),
    }
    values.update(overrides)
    return TradeCardView(**values)  # type: ignore[arg-type]


def _summary(count: int = 10) -> SummaryCardView:
    rows = tuple(
        SummaryBotRow(
            bot_name=f"Bot số {index} với tên tiếng Việt rất dài để kiểm tra cắt chữ đúng",
            strategy_id="ema_pullback_trend",
            timeframe="15m",
            trades_closed=index * 3,
            wins=index,
            net_pnl=Decimal(index * 37 - 150) + Decimal("0.25"),
            open_positions=index % 3,
            max_drawdown=Decimal("0.01") * index if index else None,
        )
        for index in range(count)
    )
    return SummaryCardView(window_label="24 giờ qua", generated_at=NOW, rows=rows)


# --- Định dạng ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("97250.5"), "97.250,50"),
        (Decimal("1234567.891"), "1.234.567,89"),
        (Decimal("212.45"), "212,45"),
        (Decimal("1.23456"), "1,2346"),
        (Decimal("0.01234567"), "0,012346"),
        (Decimal("0.00001234"), "0,00001234"),
        (0.000012345678, "0,000012346"),
        (Decimal("-0.5"), "-0,5"),
        (None, "—"),
    ],
)
def test_format_price_vi_keeps_precision_for_small_coins(value: object, expected: str) -> None:
    assert format_price_vi(value) == expected


def test_money_r_percent_and_time_formatting() -> None:
    assert format_usdt_vi(Decimal("47.684"), signed=True) == "+47,68 USDT"
    assert format_usdt_vi(Decimal("-1234.5"), signed=True) == "−1.234,50 USDT"
    assert format_usdt_vi(Decimal("0.001"), signed=True) == "0,00 USDT"
    assert format_r_vi(Decimal("-1.06")) == "−1,1R"
    assert format_r_vi(Decimal("1.48"), signed=True) == "+1,5R"
    assert format_percent_vi(Decimal("0.5234")) == "52,3%"
    assert format_percent_vi(None) == "—"
    assert format_local_time(NOW) == "15/11 14:30"
    assert format_duration_vi(timedelta(hours=3, minutes=5)) == "3 giờ 05 phút"
    assert format_duration_vi(timedelta(days=2, hours=1)) == "2 ngày 1 giờ"
    assert format_duration_vi(timedelta(seconds=20)) == "dưới 1 phút"
    assert base_asset("1000PEPEUSDT") == "1000PEPE"
    with pytest.raises(ValueError, match="múi giờ"):
        format_local_time(datetime(2026, 11, 15, 7, 30))  # noqa: DTZ001


# --- View tín hiệu ------------------------------------------------------------------------------


def test_signal_view_reads_card_from_candidate() -> None:
    view = signal_card_view_from(_candidate(checklist=CHECKLIST))
    assert view.direction == "LONG"
    assert view.coin == "BTC"
    assert view.stop is not None
    assert view.stop < view.entry_low
    assert [t.source for t in view.targets].count("recipe") == 1
    assert view.checklist[0].operator == "≤"
    assert view.checklist[0].actual == "46,2"
    assert view.checklist[2].passed is None
    assert view.checklist[2].actual == "—"
    assert view.checklist_passed == 1
    assert view.checklist_total == 3
    assert view.confidence_label == "Chưa hiệu chuẩn"
    assert view.invalidation


def test_signal_view_without_card_falls_back_to_reference_price() -> None:
    candidate = _candidate(with_card=False)
    view = signal_card_view_from(candidate)
    assert view.entry_low == view.entry_high == candidate.reference_price
    assert view.stop is None
    assert view.targets == ()
    # Văn bản cũ vẫn giữ nguyên làm phương án dự phòng.
    assert "TÍN HIỆU THAM KHẢO — KHÔNG PHẢI LỆNH" in build_signal_message(candidate)
    _image(render_signal_card(view))


# --- Renderer -----------------------------------------------------------------------------------


def test_long_signal_card_is_valid_deterministic_png() -> None:
    view = signal_card_view_from(_candidate(checklist=CHECKLIST), sample=True)
    first = render_signal_card(view)
    second = render_signal_card(view)
    assert first == second
    image = _image(first)
    assert 1200 < image.height < 4200


def test_short_tiny_price_signal_card_renders() -> None:
    view = signal_card_view_from(
        _candidate(
            symbol="1000PEPEUSDT",
            direction="SHORT",
            reference="0.00001234",
            atr="0.0000002",
            checklist=CHECKLIST,
        )
    )
    assert view.stop is not None
    assert view.stop > view.entry_high
    assert all(target.price < view.entry_low for target in view.targets)
    _image(render_signal_card(view))
    assert "0,00001234" in signal_card_caption(view)


def test_signal_card_with_long_labels_and_many_items_does_not_overflow() -> None:
    view = SignalCardView(
        decision_id="3f1c9a2e-7b4d-4c1e-9a6f-2d8e5b7c4a10",
        symbol="BTCUSDT",
        direction="LONG",
        timeframe="15m",
        strategy_id="chiến_lược_" * 20,
        strategy_release_id="bản_" * 30,
        reason_code="X",
        detail_vi=LONG_LABEL,
        reference_price=Decimal("100"),
        entry_low=Decimal("100"),
        entry_high=Decimal("100.5"),
        stop=Decimal("98"),
        signal_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        targets=tuple(
            CardTarget(label=LONG_LABEL, price=Decimal(101 + i), r_multiple=Decimal(i + 1))
            for i in range(9)
        ),
        checklist=tuple(
            CardChecklistItem(
                label=LONG_LABEL,
                actual="9" * 60,
                operator="≥",
                threshold="8" * 60,
                passed=i % 3 == 0 or None,
            )
            for i in range(12)
        ),
        invalidation=tuple(LONG_LABEL for _ in range(8)),
        confidence_label="Chưa hiệu chuẩn",
    )
    _image(render_signal_card(view))
    assert len(signal_card_caption(view)) <= MAX_CAPTION_CHARS


@pytest.mark.parametrize(
    "trade",
    [
        _trade(),
        _trade(
            direction="SHORT",
            net_pnl=Decimal("-31.27"),
            r_multiple=Decimal("-1.06"),
            exit_price=Decimal("96000"),
            exit_reason_vi="Chạm cắt lỗ (SL)",
        ),
        _trade(net_pnl=Decimal("0"), r_multiple=None, stop_price=None, target_price=None),
        _trade(
            event="OPENED",
            exit_price=None,
            net_pnl=None,
            closed_at=None,
            r_multiple=None,
            exit_reason_vi=None,
        ),
        _trade(
            bot_name=LONG_LABEL,
            strategy_id=LONG_LABEL,
            exit_reason_vi=LONG_LABEL,
            net_pnl=Decimal("-123456789.99"),
        ),
    ],
)
def test_trade_cards_render_for_open_close_profit_and_loss(trade: TradeCardView) -> None:
    data = render_trade_card(trade)
    _image(data)
    assert data == render_trade_card(trade)
    caption = trade_card_caption(trade)
    assert caption.startswith("KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT")
    assert len(caption) <= MAX_CAPTION_CHARS


def test_trade_card_profit_and_loss_use_different_colors() -> None:
    win = _image(render_trade_card(_trade())).convert("RGB")
    loss = _image(render_trade_card(_trade(net_pnl=Decimal("-31.27")))).convert("RGB")
    # Viền trên cùng mang màu kết quả: xanh khi lãi, đỏ khi lỗ.
    green, red = win.getpixel((10, 2)), loss.getpixel((10, 2))
    assert isinstance(green, tuple)
    assert isinstance(red, tuple)
    assert green[1] > green[0]
    assert red[0] > red[1]


def test_trade_view_requires_close_fields_and_timezone() -> None:
    with pytest.raises(ValueError, match="exit_price"):
        _trade(exit_price=None)
    with pytest.raises(ValueError, match="múi giờ"):
        _trade(opened_at=datetime(2026, 11, 15, 7, 30))  # noqa: DTZ001
    assert _trade(event="OPENED").notional == Decimal("4863.105")


def test_summary_card_ranks_ten_bots_and_truncates_extra() -> None:
    view = _summary(10)
    ranked = view.ranked()
    assert [row.net_pnl for row in ranked] == sorted((r.net_pnl for r in ranked), reverse=True)
    ten = _image(render_summary_card(view))
    twelve = _image(render_summary_card(_summary(12)))
    # Chỉ vẽ 10 dòng; 2 bot thừa thành một dòng ghi chú ngắn.
    assert 0 < twelve.height - ten.height < 84
    empty = _image(render_summary_card(_summary(0)))
    assert empty.height < ten.height
    caption = summary_card_caption(view)
    assert "24 giờ qua" in caption
    assert len(caption) <= MAX_CAPTION_CHARS
    assert view.total_trades == sum(i * 3 for i in range(10))
    assert view.overall_win_rate == Decimal(45) / Decimal(135)


def test_summary_row_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="lệnh thắng"):
        SummaryBotRow("b", "s", "1m", trades_closed=1, wins=2, net_pnl=Decimal(0), open_positions=0)


def test_vietnamese_glyphs_exist_in_bundled_font_and_text_helpers_truncate() -> None:
    font = _font("Regular", 40)
    missing = font.getmask("").getbbox()
    for char in "ắằẳẵặấầẩẫậếềểễệốồổỗộớờởỡợứừửữựỹỷỵĐđ−–—…·≥≤≠":
        assert font.getmask(char).getbbox() != missing, char
    assert isinstance(font, ImageFont.FreeTypeFont)
    fitted = _fit(LONG_LABEL, font, 300)
    assert fitted.endswith("…")
    assert font.getlength(fitted) <= 300
    lines = _wrap(LONG_LABEL, font, 500, 2)
    assert len(lines) == 2
    assert lines[-1].endswith("…")
    assert all(font.getlength(line) <= 500 for line in lines)


# --- Sender -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_api_sender_sends_photo_as_multipart_without_parse_mode() -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 7}})

    png = render_trade_card(_trade())
    sender = TelegramBotApiSender("123456:secret", transport=httpx2.MockTransport(handler))
    try:
        await sender.send_photo(987654321, png, "Thẻ PAPER thử nghiệm")
    finally:
        await sender.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request.url.path.endswith("/bot123456:secret/sendPhoto")
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.read()
    assert b'name="chat_id"\r\n\r\n987654321' in body
    assert 'name="caption"\r\n\r\nThẻ PAPER thử nghiệm'.encode() in body
    assert b'name="photo"; filename="card.png"' in body
    assert b"Content-Type: image/png" in body
    assert png in body
    assert b"parse_mode" not in body


@pytest.mark.asyncio
async def test_bot_api_sender_photo_maps_errors_without_leaking_token() -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry later",
                "parameters": {"retry_after": 9},
            },
        )

    png = render_trade_card(_trade())
    sender = TelegramBotApiSender("123456:never-log-this", transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(TelegramApiError) as captured:
            await sender.send_photo(987654321, png, "")
    finally:
        await sender.aclose()
    assert captured.value.code == "TELEGRAM_HTTP_429"
    assert captured.value.retry_after == 9
    assert "never-log-this" not in str(captured.value)
    assert "Too Many" not in str(captured.value)


@pytest.mark.asyncio
async def test_bot_api_sender_photo_network_error_and_input_validation() -> None:
    def failing(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom 123456:never-log-this", request=request)

    sender = TelegramBotApiSender("123456:never-log-this", transport=httpx2.MockTransport(failing))
    png = render_trade_card(_trade())
    try:
        with pytest.raises(TelegramApiError) as captured:
            await sender.send_photo(1, png, "x")
        assert captured.value.code == "TELEGRAM_NETWORK_ERROR"
        assert "never-log-this" not in str(captured.value)
        with pytest.raises(ValueError, match="PNG"):
            await sender.send_photo(1, b"GIF89a", "x")
        with pytest.raises(ValueError, match="1024"):
            await sender.send_photo(1, png, "x" * 1025)
        with pytest.raises(ValueError, match="chat_id"):
            await sender.send_photo(0, png, "x")
        with pytest.raises(ValueError, match="10 MB"):
            await sender.send_photo(1, PNG_SIGNATURE + b"\0" * (10 * 1024 * 1024), "x")
    finally:
        await sender.aclose()


@pytest.mark.asyncio
async def test_recording_sender_records_photos() -> None:
    sender = RecordingTelegramSender()
    png = render_summary_card(_summary(3))
    await sender.send_photo(42, png, "tổng kết")
    assert list(sender.photos) == [(42, png, "tổng kết")]
    assert sender.messages == []


def test_json_payload_path_still_used_for_text_messages() -> None:
    # Bảo đảm refactor _post không đổi hợp đồng JSON của sendMessage.
    captured: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.read()))
        return httpx2.Response(200, json={"ok": True, "result": {}})

    import asyncio

    async def run() -> None:
        sender = TelegramBotApiSender("123456:secret", transport=httpx2.MockTransport(handler))
        try:
            await sender.send_message(5, "chào")
        finally:
            await sender.aclose()

    asyncio.run(run())
    assert captured == [{"chat_id": 5, "text": "chào"}]
