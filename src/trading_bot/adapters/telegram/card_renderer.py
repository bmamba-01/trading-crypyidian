"""Vẽ thẻ ảnh PNG cho Telegram (TG-3) bằng Pillow, phong cách tối riêng của Crypidian.

Ba thẻ: tín hiệu tham khảo, thông báo vị thế PAPER và bảng tổng kết nhiều bot. Đầu vào là view
bất biến trong ``application/telegram_cards.py``; module này chỉ lo bố cục và màu sắc.

Nguyên tắc:
- **Xác định (deterministic):** cùng view → cùng byte PNG. Không đọc đồng hồ, không ngẫu nhiên,
  layout engine cố định ``BASIC`` để không phụ thuộc libraqm có/không trên máy.
- **Tiếng Việt:** font Be Vietnam Pro (OFL 1.1) đóng gói trong ``trading_bot/assets/fonts``;
  chữ được chuẩn hóa NFC ở view. Font không có ✓ ✗ ▲ ▼ → các biểu tượng được vẽ bằng hình.
- **Không tràn chữ:** mọi chuỗi đi qua ``_fit``/``_wrap`` (cắt bằng dấu "…").
- **Giới hạn Telegram sendPhoto:** ≤ 10 MB, rộng + cao ≤ 10000, tỉ lệ ≤ 20
  (https://core.telegram.org/bots/api#sendphoto, kiểm 15/11/2026). Thẻ rộng 1080 px, cao tùy
  nội dung (tối đa ``_MAX_HEIGHT``), mục tiêu < 1 MB.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

from trading_bot.application.telegram_cards import (
    MAX_ANALYSIS_ITEMS,
    MAX_CARD_CHECKLIST,
    MAX_CARD_INVALIDATION,
    MAX_CARD_TARGETS,
    MAX_SUMMARY_ROWS,
    AnalysisCardView,
    BacktestCardView,
    SignalCardView,
    SummaryBotRow,
    SummaryCardView,
    TradeCardView,
    format_duration_vi,
    format_local_time,
    format_number_vi,
    format_percent_vi,
    format_price_vi,
    format_r_vi,
    format_usdt_vi,
    short_id,
)

CARD_WIDTH = 1080
_MAX_HEIGHT = 4200
_PAD = 48
_INNER = 32
_SUPERSAMPLE = 3

Color = tuple[int, int, int]
Weight = Literal["Regular", "Medium", "SemiBold", "Bold"]


@dataclass(frozen=True, slots=True)
class _Palette:
    """Token màu (xem ``docs/design/telegram-cards.md``)."""

    bg: Color = (11, 15, 23)
    surface: Color = (19, 26, 38)
    surface_alt: Color = (25, 34, 50)
    border: Color = (38, 50, 71)
    text: Color = (232, 237, 245)
    muted: Color = (138, 151, 173)
    faint: Color = (92, 106, 130)
    accent: Color = (124, 156, 255)
    up: Color = (34, 195, 142)
    down: Color = (240, 80, 110)
    warn: Color = (245, 184, 65)
    sample: Color = (199, 125, 255)


P = _Palette()
_AVATAR_COLORS: tuple[Color, ...] = (
    (247, 147, 26),
    (98, 126, 234),
    (38, 161, 123),
    (233, 77, 133),
    (0, 168, 204),
    (153, 102, 255),
    (240, 185, 11),
    (90, 200, 120),
)


def _mix(a: Color, b: Color, t: float) -> Color:
    """Pha màu ``a`` về ``b`` theo tỉ lệ ``t`` (0 → a, 1 → b)."""
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _tint(color: Color, strength: float = 0.16) -> Color:
    """Nền nhạt của một màu trên mặt thẻ (chip, badge)."""
    return _mix(P.surface, color, strength)


@lru_cache(maxsize=64)
def _font(weight: Weight, size: int) -> ImageFont.FreeTypeFont:
    data = (
        resources.files("trading_bot")
        .joinpath("assets", "fonts", f"BeVietnamPro-{weight}.ttf")
        .read_bytes()
    )
    return ImageFont.truetype(io.BytesIO(data), size, layout_engine=ImageFont.Layout.BASIC)


# Be Vietnam Pro không có các ký tự này (kiểm 16/11/2026 bằng Pillow getmask): thay bằng ký tự
# có trong font để không hiện ô trống (AI hay viết "→").
_GLYPH_FALLBACK = str.maketrans(
    {"→": "›", "⟶": "›", "←": "‹", "⟵": "‹", "▸": "›", "✓": "v", "✔": "v", "✗": "x", "✘": "x"}
)


def _safe_text(text: str) -> str:
    return text.translate(_GLYPH_FALLBACK)


def _fit(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    """Cắt chuỗi cho vừa ``max_width`` và thêm "…" khi phải cắt."""
    text = _safe_text(text)
    if font.getlength(text) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.getlength(text[:middle].rstrip() + "…") <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: float, max_lines: int) -> list[str]:
    """Ngắt dòng theo từ; dòng cuối mang phần còn lại và bị cắt "…" nếu vẫn dài."""
    words = _safe_text(text).split()
    lines: list[str] = []
    current = ""
    index = 0
    while index < len(words):
        candidate = f"{current} {words[index]}".strip()
        if font.getlength(candidate) <= max_width or not current:
            current = candidate
            index += 1
            continue
        lines.append(current)
        current = ""
        if len(lines) == max_lines - 1:
            break
    rest = " ".join([current, *words[index:]]).strip()
    if rest:
        lines.append(rest)
    return [_fit(line, font, max_width) for line in lines[:max_lines]]


class _Canvas:
    """Bảng vẽ cao cố định, cắt theo con trỏ ``y`` khi xuất PNG."""

    def __init__(self) -> None:
        self.image = Image.new("RGB", (CARD_WIDTH, _MAX_HEIGHT), P.bg)
        self.draw = ImageDraw.Draw(self.image)
        self.y = 0

    # -- hình khử răng cưa bằng siêu lấy mẫu ---------------------------------------------------

    def _paste_mask(
        self,
        box: tuple[int, int, int, int],
        color: Color,
        paint: Callable[[ImageDraw.ImageDraw, int, int], None],
    ) -> None:
        x0, y0, x1, y1 = box
        width, height = max(1, x1 - x0), max(1, y1 - y0)
        mask = Image.new("L", (width * _SUPERSAMPLE, height * _SUPERSAMPLE), 0)
        paint(ImageDraw.Draw(mask), width * _SUPERSAMPLE, height * _SUPERSAMPLE)
        small = mask.resize((width, height), Image.Resampling.LANCZOS)
        self.image.paste(color, (x0, y0, x0 + width, y0 + height), small)

    def rounded(
        self,
        box: tuple[int, int, int, int],
        radius: int,
        fill: Color,
        *,
        outline: Color | None = None,
        outline_width: int = 2,
    ) -> None:
        s = _SUPERSAMPLE
        if outline is not None:
            self._paste_mask(
                box,
                outline,
                lambda d, w, h: d.rounded_rectangle((0, 0, w - 1, h - 1), radius * s, fill=255),
            )
            x0, y0, x1, y1 = box
            box = (x0 + outline_width, y0 + outline_width, x1 - outline_width, y1 - outline_width)
            radius = max(0, radius - outline_width)
        self._paste_mask(
            box,
            fill,
            lambda d, w, h: d.rounded_rectangle((0, 0, w - 1, h - 1), radius * s, fill=255),
        )

    def circle(self, cx: int, cy: int, r: int, fill: Color) -> None:
        self._paste_mask(
            (cx - r, cy - r, cx + r, cy + r),
            fill,
            lambda d, w, h: d.ellipse((0, 0, w - 1, h - 1), fill=255),
        )

    def ring(self, cx: int, cy: int, r: int, color: Color, width: int) -> None:
        s = _SUPERSAMPLE
        self._paste_mask(
            (cx - r, cy - r, cx + r, cy + r),
            color,
            lambda d, w, h: d.ellipse((0, 0, w - 1, h - 1), outline=255, width=width * s),
        )

    def triangle(self, cx: int, cy: int, size: int, color: Color, *, up: bool) -> None:
        half = size // 2

        def paint(d: ImageDraw.ImageDraw, w: int, h: int) -> None:
            if up:
                d.polygon([(w / 2, 0), (w, h), (0, h)], fill=255)
            else:
                d.polygon([(0, 0), (w, 0), (w / 2, h)], fill=255)

        self._paste_mask((cx - half, cy - half, cx + half, cy + half), color, paint)

    def check_icon(self, cx: int, cy: int, r: int, passed: bool | None) -> None:
        """Biểu tượng checklist: ✓ xanh, ✗ đỏ, — xám (vẽ bằng nét, font không có glyph)."""
        color = P.up if passed is True else (P.down if passed is False else P.faint)
        self.circle(cx, cy, r, _tint(color, 0.28))
        s = _SUPERSAMPLE
        stroke = 4 * s

        def paint(d: ImageDraw.ImageDraw, w: int, h: int) -> None:
            if passed is True:
                d.line(
                    [(w * 0.24, h * 0.52), (w * 0.43, h * 0.70), (w * 0.76, h * 0.32)],
                    255,
                    stroke,
                    joint="curve",
                )
            elif passed is False:
                d.line([(w * 0.30, h * 0.30), (w * 0.70, h * 0.70)], 255, stroke)
                d.line([(w * 0.70, h * 0.30), (w * 0.30, h * 0.70)], 255, stroke)
            else:
                d.line([(w * 0.28, h * 0.5), (w * 0.72, h * 0.5)], 255, stroke)

        self._paste_mask((cx - r, cy - r, cx + r, cy + r), color, paint)

    # -- chữ -----------------------------------------------------------------------------------

    def text(
        self,
        xy: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: Color,
        anchor: str = "lm",
    ) -> None:
        self.draw.text(xy, _safe_text(text), font=font, fill=fill, anchor=anchor)

    def chip(
        self,
        x: int,
        cy: int,
        text: str,
        font: ImageFont.FreeTypeFont,
        fg: Color,
        bg: Color,
        *,
        align: Literal["left", "right"] = "left",
        height: int = 40,
        pad: int = 16,
        outline: Color | None = None,
    ) -> int:
        """Vẽ chip bo tròn, trả bề rộng; ``align=right`` nghĩa là ``x`` là mép phải."""
        width = round(font.getlength(text)) + pad * 2
        x0 = x - width if align == "right" else x
        self.rounded(
            (x0, cy - height // 2, x0 + width, cy + height // 2),
            height // 2,
            bg,
            outline=outline,
        )
        self.text((x0 + width / 2, cy), text, font, fg, anchor="mm")
        return width

    def crop(self) -> Image.Image:
        if self.y > _MAX_HEIGHT:
            raise ValueError("Nội dung thẻ vượt chiều cao tối đa.")
        return self.image.crop((0, 0, CARD_WIDTH, self.y))


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --- Khối dùng chung ---------------------------------------------------------------------------


def _top_glow(canvas: _Canvas, color: Color, height: int = 300) -> None:
    """Quầng màu mờ ở đầu thẻ theo hướng/kết quả, giúp nhận diện nhanh trong danh sách chat."""
    for row in range(height):
        strength = 0.22 * (1 - row / height) ** 2
        canvas.draw.line([(0, row), (CARD_WIDTH, row)], fill=_mix(P.bg, color, strength))
    canvas.draw.rectangle((0, 0, CARD_WIDTH, 5), fill=color)


def _brand_row(canvas: _Canvas, *, sample: bool) -> None:
    cy = canvas.y + 36
    canvas.rounded((_PAD, cy - 22, _PAD + 44, cy + 22), 12, P.accent)
    canvas.text((_PAD + 22, cy), "C", _font("Bold", 28), P.bg, anchor="mm")
    canvas.text((_PAD + 60, cy), "Crypidian", _font("SemiBold", 30), P.text)
    right = CARD_WIDTH - _PAD
    chip_font = _font("Bold", 22)
    right -= canvas.chip(right, cy, "PAPER", chip_font, P.warn, _tint(P.warn, 0.2), align="right")
    if sample:
        canvas.chip(
            right - 12,
            cy,
            "DỮ LIỆU MẪU",
            chip_font,
            P.sample,
            _tint(P.sample, 0.18),
            align="right",
            outline=P.sample,
        )
    canvas.y = cy + 36


def _disclaimer_strip(canvas: _Canvas, text: str) -> None:
    top = canvas.y + 8
    canvas.rounded(
        (_PAD, top, CARD_WIDTH - _PAD, top + 60),
        16,
        _mix(P.bg, P.warn, 0.12),
        outline=P.warn,
        outline_width=1,
    )
    font = _font("Bold", 26)
    canvas.text(
        (CARD_WIDTH / 2, top + 30),
        _fit(text, font, CARD_WIDTH - 2 * _PAD - 40),
        font,
        P.warn,
        anchor="mm",
    )
    canvas.y = top + 60 + 24


def _avatar(canvas: _Canvas, cx: int, cy: int, coin: str, radius: int = 46) -> None:
    """Ảnh đại diện coin bằng chữ (không dùng logo thương hiệu); màu cố định theo mã coin."""
    digest = hashlib.sha256(coin.encode("utf-8")).digest()
    color = _AVATAR_COLORS[digest[0] % len(_AVATAR_COLORS)]
    canvas.circle(cx, cy, radius, _mix(P.surface, color, 0.85))
    # Bỏ hệ số nhân đầu mã (1000PEPE → PEPE) để chữ trong avatar là tên coin.
    label = (coin.lstrip("0123456789") or coin)[:4]
    size = {1: 40, 2: 36, 3: 30}.get(len(label), 24)
    canvas.text((cx, cy), label, _font("Bold", size), (255, 255, 255), anchor="mm")


def _direction_badge(canvas: _Canvas, right: int, cy: int, direction: str) -> None:
    color = P.up if direction == "LONG" else P.down
    font = _font("Bold", 30)
    width = round(font.getlength(direction)) + 32 + 34
    x0 = right - width
    canvas.rounded((x0, cy - 30, right, cy + 30), 30, color)
    canvas.triangle(x0 + 34, cy, 22, P.bg, up=direction == "LONG")
    canvas.text((x0 + 56, cy + 1), direction, font, P.bg)


def _hero(canvas: _Canvas, *, coin: str, symbol: str, subtitle: str, direction: str) -> None:
    top = canvas.y
    cy = top + 56
    _avatar(canvas, _PAD + 46, cy, coin)
    badge_space = 200
    text_x = _PAD + 46 * 2 + 24
    max_text = CARD_WIDTH - _PAD - badge_space - text_x
    canvas.text(
        (text_x, cy - 18), _fit(symbol, _font("Bold", 52), max_text), _font("Bold", 52), P.text
    )
    sub_font = _font("Medium", 24)
    canvas.text((text_x, cy + 30), _fit(subtitle, sub_font, max_text), sub_font, P.muted)
    _direction_badge(canvas, CARD_WIDTH - _PAD, cy, direction)
    canvas.y = top + 56 * 2 + 20


def _panel(
    canvas: _Canvas,
    title: str,
    right_note: str | None,
    body_height: int,
    body: Callable[[int], None],
) -> None:
    """Vẽ khối nền bo góc có tiêu đề, rồi gọi ``body(y_body)`` để vẽ nội dung."""
    top = canvas.y
    height = 72 + body_height + 20
    canvas.rounded(
        (_PAD, top, CARD_WIDTH - _PAD, top + height),
        24,
        P.surface,
        outline=P.border,
        outline_width=1,
    )
    title_font = _font("SemiBold", 28)
    canvas.text((_PAD + _INNER, top + 40), title, title_font, P.text)
    if right_note:
        note_font = _font("Regular", 21)
        max_note = CARD_WIDTH - 2 * _PAD - 2 * _INNER - title_font.getlength(title) - 24
        canvas.text(
            (CARD_WIDTH - _PAD - _INNER, top + 41),
            _fit(right_note, note_font, max_note),
            note_font,
            P.muted,
            anchor="rm",
        )
    body(top + 72)
    canvas.y = top + height + 20


def _tiles(canvas: _Canvas, items: Sequence[tuple[str, str, Color]], columns: int = 2) -> None:
    """Lưới ô nhãn–giá trị; mỗi ô: nhãn nhỏ xám, giá trị đậm một dòng."""
    gap = 16
    width = (CARD_WIDTH - 2 * _PAD - gap * (columns - 1)) // columns
    height = 104
    label_font = _font("Medium", 21)
    value_font = _font("SemiBold", 30 if columns <= 2 else 28)
    for index, (label, value, color) in enumerate(items):
        row, column = divmod(index, columns)
        x0 = _PAD + column * (width + gap)
        y0 = canvas.y + row * (height + gap)
        canvas.rounded(
            (x0, y0, x0 + width, y0 + height), 18, P.surface, outline=P.border, outline_width=1
        )
        canvas.text((x0 + 24, y0 + 32), _fit(label, label_font, width - 48), label_font, P.muted)
        canvas.text((x0 + 24, y0 + 70), _fit(value, value_font, width - 48), value_font, color)
    rows = (len(items) + columns - 1) // columns
    canvas.y += rows * (height + gap) + 4


def _footer(canvas: _Canvas, left: str, right: str) -> None:
    top = canvas.y + 4
    canvas.draw.line([(_PAD, top), (CARD_WIDTH - _PAD, top)], fill=P.border, width=1)
    font = _font("Regular", 21)
    right_width = font.getlength(right)
    canvas.text(
        (_PAD, top + 34), _fit(left, font, CARD_WIDTH - 2 * _PAD - right_width - 24), font, P.muted
    )
    canvas.text((CARD_WIDTH - _PAD, top + 34), right, font, P.faint, anchor="rm")
    canvas.y = top + 70


# --- Thẻ tín hiệu ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LadderRow:
    kind: Literal["target", "entry", "stop"]
    label: str
    price_text: str
    sort_price: Decimal
    chip_text: str
    distance: Decimal | None


def _ladder_rows(view: SignalCardView) -> list[_LadderRow]:
    entry_mid = (view.entry_low + view.entry_high) / 2
    zone = format_price_vi(view.entry_low)
    if view.entry_high != view.entry_low:
        zone = f"{zone} – {format_price_vi(view.entry_high)}"
    rows = [_LadderRow("entry", "Vùng vào", zone, entry_mid, "VÀO", None)]
    if view.stop is not None:
        rows.append(
            _LadderRow(
                "stop",
                "Cắt lỗ (SL)",
                format_price_vi(view.stop),
                view.stop,
                "−1,0R",
                (view.stop - entry_mid) / entry_mid if entry_mid else None,
            )
        )
    for target in view.targets[:MAX_CARD_TARGETS]:
        chip = format_r_vi(target.r_multiple)
        if target.net_r_multiple is not None:
            chip = f"{chip} · ròng {format_r_vi(target.net_r_multiple)}"
        label = target.label
        if target.stop_to_entry_after:
            label = f"{label} · hòa vốn"
        rows.append(
            _LadderRow(
                "target",
                label,
                format_price_vi(target.price),
                target.price,
                chip,
                (target.price - entry_mid) / entry_mid if entry_mid else None,
            )
        )
    # Giá cao ở trên như trục giá của biểu đồ: LONG → mục tiêu trên, SL dưới; SHORT ngược lại.
    rows.sort(key=lambda row: row.sort_price, reverse=True)
    return rows


def _draw_ladder(canvas: _Canvas, view: SignalCardView) -> None:
    rows = _ladder_rows(view)
    row_height = 84
    has_net = any(t.net_r_multiple is not None for t in view.targets)
    note = (
        "R thô / R:R ròng sau phí"
        if (view.stop is not None and has_net)
        else ("R thô, chưa trừ phí" if view.stop is not None else "Chưa dựng được SL/TP")
    )
    rail_x = _PAD + _INNER + 22
    label_x = rail_x + 44
    right = CARD_WIDTH - _PAD - _INNER

    def body(y0: int) -> None:
        centers = [y0 + row_height * i + row_height // 2 for i in range(len(rows))]
        entry_index = next(i for i, row in enumerate(rows) if row.kind == "entry")
        for i in range(len(rows) - 1):
            # Đoạn dưới vùng vào là phía SL với LONG; với SHORT phía SL nằm trên vùng vào.
            below_entry = i >= entry_index
            toward_stop = below_entry if view.direction == "LONG" else not below_entry
            color = P.down if toward_stop else P.up
            canvas.draw.line(
                [(rail_x, centers[i]), (rail_x, centers[i + 1])],
                fill=_mix(P.surface, color, 0.55),
                width=6,
            )
        label_font = _font("Medium", 26)
        sub_font = _font("Regular", 20)
        price_font = _font("SemiBold", 32)
        chip_font = _font("Bold", 20)
        for row, cy in zip(rows, centers, strict=True):
            if row.kind == "entry":
                canvas.rounded(
                    (
                        _PAD + 12,
                        cy - row_height // 2 + 6,
                        CARD_WIDTH - _PAD - 12,
                        cy + row_height // 2 - 6,
                    ),
                    16,
                    _tint(P.accent, 0.14),
                )
                canvas.circle(rail_x, cy, 15, P.accent)
                canvas.circle(rail_x, cy, 6, P.surface)
                color = P.accent
            elif row.kind == "stop":
                canvas.circle(rail_x, cy, 13, P.down)
                color = P.down
            else:
                canvas.circle(rail_x, cy, 13, P.surface)
                canvas.ring(rail_x, cy, 13, P.up, 4)
                color = P.up
            price_width = price_font.getlength(row.price_text)
            chip_width = chip_font.getlength(row.chip_text) + 28
            max_label = right - price_width - chip_width - 32 - label_x
            if row.distance is not None:
                canvas.text(
                    (label_x, cy - 14), _fit(row.label, label_font, max_label), label_font, P.text
                )
                distance = (
                    f"cách vùng vào {format_percent_vi(row.distance, signed=True, decimals=2)}"
                )
                canvas.text(
                    (label_x, cy + 18), _fit(distance, sub_font, max_label), sub_font, P.muted
                )
            else:
                canvas.text(
                    (label_x, cy), _fit(row.label, label_font, max_label), label_font, P.text
                )
            canvas.text(
                (right, cy),
                _fit(row.price_text, price_font, right - label_x - 120),
                price_font,
                P.text,
                anchor="rm",
            )
            canvas.chip(
                round(right - price_width - 16),
                cy,
                row.chip_text,
                chip_font,
                color,
                _tint(color, 0.22),
                align="right",
                height=34,
                pad=14,
            )
        if view.stop is None and not view.targets:
            canvas.text(
                (label_x, y0 + row_height + 24),
                "Tín hiệu chưa có thang SL/TP; chỉ hiển thị giá tham chiếu.",
                _font("Regular", 22),
                P.muted,
            )

    extra = 48 if view.stop is None and not view.targets else 0
    _panel(canvas, "Thang giá", note, row_height * len(rows) + extra, body)


def _draw_checklist(canvas: _Canvas, view: SignalCardView) -> None:
    items = view.checklist[:MAX_CARD_CHECKLIST]
    hidden = len(view.checklist) - len(items)
    passed = view.checklist_passed
    total = view.checklist_total
    if passed is None:
        passed = sum(1 for item in view.checklist if item.passed is True)
    if total is None:
        total = len(view.checklist)
    row_height = 64
    right = CARD_WIDTH - _PAD - _INNER
    left = _PAD + _INNER

    def body(y0: int) -> None:
        bar_y = y0 + 2
        canvas.rounded((left, bar_y, right, bar_y + 10), 5, P.surface_alt)
        if total:
            filled = left + round((right - left) * passed / total)
            if filled - left >= 10:
                canvas.rounded((left, bar_y, filled, bar_y + 10), 5, P.up)
        label_font = _font("Medium", 25)
        value_font = _font("SemiBold", 25)
        need_font = _font("Regular", 22)
        for index, item in enumerate(items):
            cy = y0 + 28 + row_height * index + row_height // 2
            canvas.check_icon(left + 18, cy, 18, item.passed)
            need = f"cần {item.operator} {item.threshold}".replace("  ", " ")
            need_text = _fit(need, need_font, 260)
            need_width = need_font.getlength(need_text)
            actual = _fit(item.actual, value_font, 180)
            actual_width = value_font.getlength(actual)
            canvas.text((right, cy), need_text, need_font, P.muted, anchor="rm")
            color = P.up if item.passed is True else (P.down if item.passed is False else P.muted)
            canvas.text((right - need_width - 16, cy), actual, value_font, color, anchor="rm")
            max_label = right - need_width - actual_width - 40 - (left + 52)
            canvas.text(
                (left + 52, cy), _fit(item.label, label_font, max_label), label_font, P.text
            )
        if hidden > 0:
            cy = y0 + 28 + row_height * len(items) + 22
            canvas.text((left + 52, cy), f"+{hidden} điều kiện khác", _font("Regular", 22), P.muted)

    body_height = 28 + row_height * len(items) + (44 if hidden > 0 else 0)
    _panel(canvas, "Điều kiện", f"{passed}/{total} đạt", body_height, body)


def _draw_invalidation(canvas: _Canvas, view: SignalCardView) -> None:
    font = _font("Regular", 24)
    left = _PAD + _INNER
    max_width = CARD_WIDTH - 2 * _PAD - 2 * _INNER - 32
    blocks = [_wrap(line, font, max_width, 2) for line in view.invalidation[:MAX_CARD_INVALIDATION]]
    line_height = 36

    def body(y0: int) -> None:
        y = y0
        for lines in blocks:
            canvas.circle(left + 7, y + line_height // 2, 5, P.down)
            for line in lines:
                canvas.text((left + 28, y + line_height // 2), line, font, P.text)
                y += line_height
            y += 10

    height = sum(len(lines) * line_height + 10 for lines in blocks)
    _panel(canvas, "Mất hiệu lực khi", None, height, body)


def render_signal_card(view: SignalCardView) -> bytes:
    """Thẻ tín hiệu tham khảo (PNG). Không phải lệnh, không phải khuyến nghị đầu tư."""
    canvas = _Canvas()
    direction_color = P.up if view.direction == "LONG" else P.down
    _top_glow(canvas, direction_color)
    _brand_row(canvas, sample=view.sample)
    _disclaimer_strip(canvas, "TÍN HIỆU THAM KHẢO — KHÔNG PHẢI LỆNH")
    _hero(
        canvas,
        coin=view.coin,
        symbol=view.symbol,
        subtitle=f"{view.coin} · Khung {view.timeframe} · Giá tham chiếu "
        f"{format_price_vi(view.reference_price)}",
        direction=view.direction,
    )
    meta_font = _font("Regular", 23)
    meta = f"Chiến lược {view.strategy_id} · Bản {view.strategy_release_id}"
    canvas.text((_PAD, canvas.y), _fit(meta, meta_font, CARD_WIDTH - 2 * _PAD), meta_font, P.muted)
    canvas.y += 36
    reason_font = _font("Regular", 23)
    for line in _wrap(f"Vì sao: {view.detail_vi}", reason_font, CARD_WIDTH - 2 * _PAD, 2):
        canvas.text((_PAD, canvas.y), line, reason_font, P.text)
        canvas.y += 34
    canvas.y += 12
    _draw_ladder(canvas, view)
    if view.checklist:
        _draw_checklist(canvas, view)
    if view.invalidation:
        _draw_invalidation(canvas, view)
    _tiles(
        canvas,
        [
            ("Độ tin cậy", view.confidence_label, P.warn),
            ("Mã tín hiệu", f"#{short_id(view.decision_id)}", P.text),
            ("Phát tín hiệu (giờ VN)", format_local_time(view.signal_at), P.text),
            ("Hết hạn (giờ VN)", format_local_time(view.expires_at), P.text),
        ],
    )
    _footer(canvas, "PAPER · mô phỏng nội bộ, không phải khuyến nghị đầu tư.", "crypidian")
    return _png(canvas.crop())


# --- Thẻ giao dịch -----------------------------------------------------------------------------


def _range_bar(canvas: _Canvas, view: TradeCardView) -> None:
    """Thanh SL — vào — TP tỉ lệ theo giá; thẻ đóng có thêm điểm giá ra (kẹp trong thanh)."""
    if view.stop_price is None or view.target_price is None:
        return
    low_price = min(view.stop_price, view.target_price)
    high_price = max(view.stop_price, view.target_price)
    span = high_price - low_price
    if span <= 0:
        return
    left, right = _PAD + _INNER + 10, CARD_WIDTH - _PAD - _INNER - 10

    def x_of(price: Decimal) -> int:
        ratio = (min(max(price, low_price), high_price) - low_price) / span
        return round(left + (right - left) * float(ratio))

    def body(y0: int) -> None:
        cy = y0 + 24
        entry_x = x_of(view.entry_price)
        stop_x = x_of(view.stop_price or view.entry_price)
        target_x = x_of(view.target_price or view.entry_price)
        a, b = sorted((stop_x, entry_x))
        canvas.rounded((a, cy - 6, max(a + 12, b), cy + 6), 6, _mix(P.surface, P.down, 0.7))
        a, b = sorted((entry_x, target_x))
        canvas.rounded((a, cy - 6, max(a + 12, b), cy + 6), 6, _mix(P.surface, P.up, 0.7))
        canvas.circle(entry_x, cy, 13, P.accent)
        small = _font("Medium", 20)
        value = _font("SemiBold", 22)
        labels = [
            (stop_x, "SL", format_price_vi(view.stop_price), P.down),
            (entry_x, "Vào", format_price_vi(view.entry_price), P.accent),
            (target_x, "TP", format_price_vi(view.target_price), P.up),
        ]
        for x, name, price, color in labels:
            anchor_x = min(max(x, left + 70), right - 70)
            canvas.text((anchor_x, cy + 38), name, small, color, anchor="mm")
            canvas.text((anchor_x, cy + 66), price, value, P.text, anchor="mm")
        if view.event == "CLOSED" and view.exit_price is not None:
            exit_x = x_of(view.exit_price)
            canvas.triangle(exit_x, cy - 30, 22, P.text, up=False)
            canvas.text((exit_x, cy - 56), "Ra", small, P.text, anchor="mm")

    _panel(
        canvas,
        "Vùng giá vị thế",
        "tỉ lệ theo giá",
        136 if view.event == "OPENED" else 176,
        lambda y0: body(y0 + (40 if view.event == "CLOSED" else 0)),
    )


def render_trade_card(view: TradeCardView) -> bytes:
    """Thẻ thông báo vị thế PAPER đã mở/đã đóng (PNG)."""
    canvas = _Canvas()
    if view.event == "CLOSED" and view.net_pnl is not None:
        mood = P.up if view.net_pnl > 0 else (P.down if view.net_pnl < 0 else P.muted)
    else:
        mood = P.accent
    _top_glow(canvas, mood)
    _brand_row(canvas, sample=view.sample)
    _disclaimer_strip(canvas, "KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT")
    event_text = "ĐÃ MỞ VỊ THẾ" if view.event == "OPENED" else "ĐÃ ĐÓNG VỊ THẾ"
    cy = canvas.y + 20
    canvas.chip(_PAD, cy, event_text, _font("Bold", 22), mood, _tint(mood, 0.22))
    moment = view.closed_at if view.event == "CLOSED" and view.closed_at else view.opened_at
    canvas.text(
        (CARD_WIDTH - _PAD, cy),
        f"{format_local_time(moment)} (giờ VN)",
        _font("Medium", 22),
        P.muted,
        anchor="rm",
    )
    canvas.y = cy + 40
    _hero(
        canvas,
        coin=view.coin,
        symbol=view.symbol,
        subtitle=f"{view.bot_name} · {view.strategy_id} · {view.timeframe}",
        direction=view.direction,
    )
    top = canvas.y
    height = 210
    canvas.rounded(
        (_PAD, top, CARD_WIDTH - _PAD, top + height),
        24,
        _mix(P.surface, mood, 0.08),
        outline=_mix(P.border, mood, 0.4),
        outline_width=1,
    )
    label_font = _font("Medium", 24)
    hero_font = _font("Bold", 80)
    left = _PAD + _INNER
    if view.event == "CLOSED" and view.net_pnl is not None:
        canvas.text((left, top + 42), "Lãi/lỗ ròng (sau phí)", label_font, P.muted)
        pnl_text = _fit(format_usdt_vi(view.net_pnl, signed=True), hero_font, 820)
        if view.net_pnl != 0:
            canvas.triangle(left + 22, top + 112, 36, mood, up=view.net_pnl > 0)
            canvas.text((left + 56, top + 110), pnl_text, hero_font, mood)
        else:
            canvas.text((left, top + 110), pnl_text, hero_font, mood)
        chip_x = left
        chip_font = _font("Bold", 24)
        if view.r_multiple is not None:
            chip_x += (
                canvas.chip(
                    chip_x,
                    top + 172,
                    format_r_vi(view.r_multiple, signed=True),
                    chip_font,
                    mood,
                    _tint(mood, 0.25),
                )
                + 16
            )
        hold = view.hold_time
        if hold is not None:
            canvas.text(
                (chip_x, top + 172), f"Giữ {format_duration_vi(hold)}", _font("Medium", 24), P.muted
            )
    else:
        canvas.text((left, top + 42), "Giá vào", label_font, P.muted)
        canvas.text(
            (left, top + 110),
            _fit(format_price_vi(view.entry_price), hero_font, 880),
            hero_font,
            P.text,
        )
        canvas.text(
            (left, top + 172),
            f"Danh nghĩa {format_usdt_vi(view.notional)}",
            _font("Medium", 24),
            P.muted,
        )
    canvas.y = top + height + 20
    _range_bar(canvas, view)
    if view.event == "CLOSED":
        tiles: list[tuple[str, str, Color]] = [
            ("Giá vào", format_price_vi(view.entry_price), P.text),
            ("Giá ra", format_price_vi(view.exit_price), P.text),
            ("Khối lượng", format_number_vi(view.quantity, decimals=6), P.text),
            ("Giá trị danh nghĩa", format_usdt_vi(view.notional), P.text),
            ("Lý do thoát", view.exit_reason_vi or "Chưa có dữ liệu", P.text),
            (
                "Phí đã trừ",
                format_usdt_vi(view.fees) if view.fees is not None else "Chưa có dữ liệu",
                P.muted if view.fees is None else P.text,
            ),
        ]
    else:
        risk = view.risk_to_stop
        tiles = [
            ("Khối lượng", format_number_vi(view.quantity, decimals=6), P.text),
            ("Giá trị danh nghĩa", format_usdt_vi(view.notional), P.text),
            ("Cắt lỗ (SL)", format_price_vi(view.stop_price), P.down),
            ("Chốt lời (TP)", format_price_vi(view.target_price), P.up),
            ("Lỗ nếu chạm SL (thô)", format_usdt_vi(-risk) if risk else "—", P.text),
            ("Mở lúc (giờ VN)", format_local_time(view.opened_at), P.text),
        ]
    _tiles(canvas, tiles)
    _footer(
        canvas,
        f"Mã giao dịch #{short_id(view.trade_id)} · mô phỏng PAPER, không phải lệnh thật.",
        "crypidian",
    )
    return _png(canvas.crop())


# --- Thẻ tổng kết ------------------------------------------------------------------------------


def _summary_table(canvas: _Canvas, rows: Sequence[SummaryBotRow], hidden: int) -> None:
    left = _PAD + _INNER
    right = CARD_WIDTH - _PAD - _INNER
    # Mép phải từng cột số; cột bot chiếm phần còn lại bên trái.
    col_trades, col_win, col_pnl, col_open, col_dd = 548, 650, 810, 878, right
    bot_x = left + 52
    header_height = 44
    row_height = 84
    max_abs = max((abs(row.net_pnl) for row in rows), default=Decimal(0))

    def body(y0: int) -> None:
        header_font = _font("SemiBold", 19)
        hy = y0 + 14
        for x, label, anchor in (
            (left + 18, "#", "mm"),
            (bot_x, "BOT", "lm"),
            (col_trades, "LỆNH", "rm"),
            (col_win, "THẮNG", "rm"),
            (col_pnl, "PNL (USDT)", "rm"),
            (col_open, "MỞ", "rm"),
            (col_dd, "MAX DD", "rm"),
        ):
            canvas.text((x, hy), label, header_font, P.faint, anchor=anchor)
        name_font = _font("SemiBold", 25)
        sub_font = _font("Regular", 20)
        value_font = _font("SemiBold", 25)
        rank_font = _font("Bold", 22)
        for index, row in enumerate(rows):
            top = y0 + header_height + index * row_height
            cy = top + row_height // 2
            if index % 2 == 0:
                canvas.rounded(
                    (_PAD + 12, top + 4, CARD_WIDTH - _PAD - 12, top + row_height - 4),
                    14,
                    P.surface_alt,
                )
            rank_color = P.warn if index == 0 else (P.accent if index < 3 else P.faint)
            if index < 3:
                canvas.circle(left + 18, cy, 19, _tint(rank_color, 0.3))
            canvas.text((left + 18, cy), str(index + 1), rank_font, rank_color, anchor="mm")
            bot_width = col_trades - 56 - bot_x
            canvas.text(
                (bot_x, cy - 14), _fit(row.bot_name, name_font, bot_width), name_font, P.text
            )
            canvas.text(
                (bot_x, cy + 18),
                _fit(f"{row.strategy_id} · {row.timeframe}", sub_font, bot_width),
                sub_font,
                P.muted,
            )
            canvas.text((col_trades, cy), str(row.trades_closed), value_font, P.text, anchor="rm")
            canvas.text(
                (col_win, cy),
                format_percent_vi(row.win_rate, decimals=0),
                value_font,
                P.text,
                anchor="rm",
            )
            pnl_color = P.up if row.net_pnl > 0 else (P.down if row.net_pnl < 0 else P.muted)
            pnl_text = format_usdt_vi(row.net_pnl, signed=True).removesuffix(" USDT")
            canvas.text(
                (col_pnl, cy - 6),
                _fit(pnl_text, value_font, 150),
                value_font,
                pnl_color,
                anchor="rm",
            )
            if max_abs > 0 and row.net_pnl != 0:
                bar = max(6, round(130 * float(abs(row.net_pnl) / max_abs)))
                canvas.rounded(
                    (col_pnl - bar, cy + 18, col_pnl, cy + 24),
                    3,
                    _mix(P.surface_alt, pnl_color, 0.75),
                )
            canvas.text(
                (col_open, cy),
                str(row.open_positions),
                value_font,
                P.text if row.open_positions else P.faint,
                anchor="rm",
            )
            dd = format_percent_vi(-row.max_drawdown) if row.max_drawdown else "—"
            canvas.text(
                (col_dd, cy), dd, value_font, P.down if row.max_drawdown else P.faint, anchor="rm"
            )
        footer_y = y0 + header_height + len(rows) * row_height
        if not rows:
            canvas.text(
                (bot_x, footer_y + 30),
                "Chưa có bot nào trong cửa sổ này.",
                _font("Regular", 23),
                P.muted,
            )
        elif hidden > 0:
            canvas.text(
                (bot_x, footer_y + 24),
                f"+{hidden} bot khác không hiển thị",
                _font("Regular", 22),
                P.muted,
            )

    extra = 60 if not rows else (44 if hidden > 0 else 0)
    _panel(
        canvas,
        "Xếp hạng theo PnL ròng",
        "PnL sau phí · DD từ đỉnh vốn",
        header_height + row_height * len(rows) + extra,
        body,
    )


def render_summary_card(view: SummaryCardView) -> bytes:
    """Thẻ tổng kết định kỳ so sánh tối đa 10 bot PAPER (PNG)."""
    canvas = _Canvas()
    total = view.total_pnl
    mood = P.up if total > 0 else (P.down if total < 0 else P.accent)
    _top_glow(canvas, mood)
    _brand_row(canvas, sample=view.sample)
    _disclaimer_strip(canvas, "TỔNG KẾT PAPER — KHÔNG PHẢI KẾT QUẢ THẬT")
    title_font = _font("Bold", 46)
    canvas.text((_PAD, canvas.y + 26), "Bảng so sánh bot", title_font, P.text)
    sub_font = _font("Medium", 23)
    subtitle = (
        f"Cửa sổ: {view.window_label} · Tạo lúc {format_local_time(view.generated_at)} (giờ VN)"
    )
    canvas.text(
        (_PAD, canvas.y + 80), _fit(subtitle, sub_font, CARD_WIDTH - 2 * _PAD), sub_font, P.muted
    )
    canvas.y += 124
    _tiles(
        canvas,
        [
            ("PnL ròng (USDT)", format_usdt_vi(total, signed=True).removesuffix(" USDT"), mood),
            ("Lệnh đóng", str(view.total_trades), P.text),
            ("Tỉ lệ thắng", format_percent_vi(view.overall_win_rate), P.text),
            ("Vị thế mở", str(view.total_open), P.text),
        ],
        columns=4,
    )
    ranked = view.ranked()
    shown = ranked[:MAX_SUMMARY_ROWS]
    _summary_table(canvas, shown, len(ranked) - len(shown))
    _footer(canvas, "PnL tính bằng USDT · mô phỏng PAPER, không phải kết quả thật.", "crypidian")
    return _png(canvas.crop())


# --- Thẻ phân tích AI (AI-2) -------------------------------------------------------------------


def _bullet_panel(
    canvas: _Canvas,
    title: str,
    note: str | None,
    items: Sequence[tuple[str, str | None]],
    color: Color,
    *,
    max_lines: int = 3,
) -> None:
    """Khối gạch đầu dòng: dòng chính (≤ ``max_lines``) + dòng phụ xám tùy chọn."""
    font = _font("Regular", 25)
    sub_font = _font("Regular", 22)
    left = _PAD + _INNER
    width = CARD_WIDTH - 2 * _PAD - 2 * _INNER - 32
    line_height = 36
    blocks = [
        (_wrap(text, font, width, max_lines), _wrap(sub, sub_font, width, 2) if sub else [])
        for text, sub in items[:MAX_ANALYSIS_ITEMS]
    ]

    def body(y0: int) -> None:
        y = y0
        for main_lines, sub_lines in blocks:
            canvas.circle(left + 7, y + line_height // 2, 6, color)
            for line in main_lines:
                canvas.text((left + 28, y + line_height // 2), line, font, P.text)
                y += line_height
            for line in sub_lines:
                canvas.text((left + 28, y + 16), line, sub_font, P.muted)
                y += 32
            y += 12

    height = sum(len(m) * line_height + len(sub) * 32 + 12 for m, sub in blocks)
    _panel(canvas, title, note, max(height, line_height), body)


def render_analysis_card(view: AnalysisCardView) -> bytes:
    """Thẻ "Phân tích AI" (PNG): tóm tắt, nhận định, giả thuyết, đề xuất để backtest."""
    canvas = _Canvas()
    mood = {"high": P.up, "medium": P.accent}.get(view.confidence, P.warn)
    _top_glow(canvas, P.accent)
    _brand_row(canvas, sample=view.sample)
    _disclaimer_strip(canvas, "PHÂN TÍCH AI — CHỈ LÀ GỢI Ý, PHẢI BACKTEST TRƯỚC")
    title_font = _font("Bold", 46)
    canvas.text((_PAD, canvas.y + 26), f"Phân tích AI · {view.topic_label}", title_font, P.text)
    chip_font = _font("Bold", 22)
    canvas.chip(
        CARD_WIDTH - _PAD,
        canvas.y + 28,
        f"Tin cậy: {view.confidence_vi}",
        chip_font,
        mood,
        _tint(mood, 0.2),
        align="right",
    )
    sub_font = _font("Medium", 23)
    subtitle = (
        f"Cửa sổ {format_local_time(view.window_start)} – {format_local_time(view.window_end)}"
        " (giờ VN)"
    )
    canvas.text(
        (_PAD, canvas.y + 80), _fit(subtitle, sub_font, CARD_WIDTH - 2 * _PAD), sub_font, P.muted
    )
    canvas.y += 124
    summary_font = _font("Regular", 26)
    summary_lines = _wrap(view.summary, summary_font, CARD_WIDTH - 2 * _PAD - 2 * _INNER, 7)

    def summary_body(y0: int) -> None:
        for index, line in enumerate(summary_lines):
            canvas.text((_PAD + _INNER, y0 + 18 + index * 38), line, summary_font, P.text)

    _panel(canvas, "Tóm tắt", None, len(summary_lines) * 38 + 8, summary_body)
    if view.findings:
        _bullet_panel(
            canvas,
            "Nhận định",
            "mọi con số đã đối chiếu với report",
            [(text, None) for text in view.findings],
            P.accent,
        )
    if view.hypotheses:
        _bullet_panel(
            canvas,
            "Giả thuyết cần kiểm",
            None,
            [(text, f"Cách kiểm: {test}") for text, test in view.hypotheses],
            P.warn,
            max_lines=2,
        )
    if view.proposals:
        _bullet_panel(
            canvas,
            "Đề xuất để BACKTEST",
            "không tự áp vào bot",
            [(f"{bot}: {changes}", f"Rủi ro: {risk}") for bot, changes, risk in view.proposals],
            P.up,
            max_lines=2,
        )
    if view.listing_alerts:
        _bullet_panel(
            canvas,
            "Cảnh báo niêm yết Binance",
            "dữ liệu exchangeInfo, không phải AI",
            [(text, None) for text in view.listing_alerts],
            P.down,
        )
    if view.macro_events:
        title = (
            f"Lịch vĩ mô {view.macro_horizon_days} ngày tới (giờ VN)"
            if view.macro_horizon_days
            else "Lịch vĩ mô sắp tới (giờ VN)"
        )
        _bullet_panel(
            canvas,
            title,
            "lịch chính thức, không phải AI",
            [(text, None) for text in view.macro_events],
            P.down if view.macro_blackout else P.warn,
        )
    _footer(
        canvas,
        f"Report {view.report_hash[:12]} · PAPER, không phải khuyến nghị đầu tư.",
        "crypidian",
    )
    return _png(canvas.crop())


# --- Thẻ backtest đề xuất AI (AI-3) -------------------------------------------------------------


def render_backtest_card(view: BacktestCardView) -> bytes:
    """Thẻ "Backtest đề xuất AI" (PNG): bảng bản gốc vs bản đề xuất trên cùng dữ liệu."""
    canvas = _Canvas()
    mood = {"BETTER": P.up, "WORSE": P.down}.get(view.verdict, P.warn)
    _top_glow(canvas, mood)
    _brand_row(canvas, sample=view.sample)
    _disclaimer_strip(canvas, "BACKTEST ĐỀ XUẤT AI — KHÔNG TỰ ÁP VÀO BOT")
    title_font = _font("Bold", 40)
    canvas.text(
        (_PAD, canvas.y + 26),
        _fit(view.bot_name, title_font, CARD_WIDTH - 2 * _PAD - 300),
        title_font,
        P.text,
    )
    canvas.chip(
        CARD_WIDTH - _PAD,
        canvas.y + 28,
        view.verdict_vi,
        _font("Bold", 22),
        mood,
        _tint(mood, 0.2),
        align="right",
    )
    sub_font = _font("Medium", 23)
    subtitle = (
        f"Dữ liệu {format_local_time(view.window_start)} – {format_local_time(view.window_end)}"
        " (giờ VN)"
        if view.window_start and view.window_end
        else "Không có cửa sổ dữ liệu chạy được"
    )
    canvas.text(
        (_PAD, canvas.y + 80), _fit(subtitle, sub_font, CARD_WIDTH - 2 * _PAD), sub_font, P.muted
    )
    canvas.y += 124
    change_font = _font("SemiBold", 26)
    change_lines = _wrap(view.changes or "—", change_font, CARD_WIDTH - 2 * _PAD - 2 * _INNER, 3)

    def change_body(y0: int) -> None:
        for index, line in enumerate(change_lines):
            canvas.text((_PAD + _INNER, y0 + 18 + index * 38), line, change_font, P.accent)

    _panel(canvas, "Thay đổi đề xuất", None, len(change_lines) * 38 + 8, change_body)
    rows = view.rows()
    row_height = 58
    label_font = _font("Medium", 24)
    value_font = _font("SemiBold", 26)
    head_font = _font("SemiBold", 21)
    col_base = CARD_WIDTH - _PAD - _INNER - 300
    col_var = CARD_WIDTH - _PAD - _INNER

    def table_body(y0: int) -> None:
        canvas.text((col_base, y0 + 14), "BẢN GỐC", head_font, P.muted, anchor="rm")
        canvas.text((col_var, y0 + 14), "ĐỀ XUẤT", head_font, P.muted, anchor="rm")
        for index, (label, base, var) in enumerate(rows):
            cy = y0 + 64 + index * row_height
            if index % 2 == 0:
                canvas.rounded(
                    (_PAD + 12, cy - 26, CARD_WIDTH - _PAD - 12, cy + 26), 12, P.surface_alt
                )
            canvas.text((_PAD + _INNER, cy), label, label_font, P.muted, anchor="lm")
            canvas.text((col_base, cy), base, value_font, P.text, anchor="rm")
            canvas.text((col_var, cy), var, value_font, P.text, anchor="rm")

    _panel(
        canvas,
        "So sánh trên cùng dữ liệu",
        "1x, 1 vị thế/coin, có phí + trượt giá",
        40 + len(rows) * row_height,
        table_body,
    )
    notes = list(view.notes)
    if view.skipped:
        notes.append(f"{view.skipped} coin bị loại (thiếu nến hoặc không đủ dữ liệu).")
    if notes:
        _bullet_panel(canvas, "Ghi chú", None, [(text, None) for text in notes], P.warn)
    _footer(
        canvas,
        "Áp dụng: bản nháp config → publish trên Admin (MFA). PAPER.",
        "crypidian",
    )
    return _png(canvas.crop())
