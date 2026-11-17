"""Ghi đè thông số thoát lệnh theo bot (CRY-428, quyết định owner 17/11/2026).

Owner chọn: chỉ được ghi đè **trường release vốn có** (SL × ATR, trailing × ATR, TP × ATR, giữ
tối đa, thang TP theo R); release có SL theo cấu trúc (H18/H20) và Strategy Card không chỉnh
được SL. Mọi thay đổi phải có backtest bản gốc vs bản mới trước khi publish (gate ở tầng admin).

Ghi đè được áp **sau** khi rule sinh candidate và **trước** planner, nên:
- ``release_content_hash`` của release không đổi (mã rule không đổi);
- ``candidate_id`` đổi theo vì băm cả recipe; ``TradePlan.exit_recipe`` và vị thế PAPER giữ
  recipe đã ghi đè, nên lineage quyết định luôn ghi đúng thông số đã dùng;
- ``recipe_id`` giữ nguyên để tra bằng chứng thoát (kênh/feature) của release như cũ.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from trading_bot.domain.risk.take_profit import LadderStep, TakeProfitLadder
from trading_bot.domain.strategy.recipes import ExitRecipe

EXIT_OVERRIDE_VERSION = "cry428-exit-override-v1"
MIN_ATR_MULTIPLE = Decimal("0.25")
MAX_ATR_MULTIPLE = Decimal("10")
MIN_R_MULTIPLE = Decimal("0.25")
MAX_R_MULTIPLE = Decimal("10")
MAX_HOLD_BARS_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class ExitOverride:
    """Thông số thoát ghi đè; None = giữ giá trị của release."""

    stop_atr_multiple: Decimal | None = None
    trailing_atr_multiple: Decimal | None = None
    take_profit_atr_multiple: Decimal | None = None
    max_hold_bars: int | None = None
    ladder: TakeProfitLadder | None = None

    def __post_init__(self) -> None:
        for name in ("stop_atr_multiple", "trailing_atr_multiple", "take_profit_atr_multiple"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} phải là Decimal hữu hạn.")
            if not MIN_ATR_MULTIPLE <= value <= MAX_ATR_MULTIPLE:
                raise ValueError(
                    f"{name} phải nằm trong [{MIN_ATR_MULTIPLE}, {MAX_ATR_MULTIPLE}] × ATR."
                )
        if self.max_hold_bars is not None and (
            type(self.max_hold_bars) is not int
            or not 1 <= self.max_hold_bars <= MAX_HOLD_BARS_LIMIT
        ):
            raise ValueError(f"max_hold_bars phải là số nguyên trong [1, {MAX_HOLD_BARS_LIMIT}].")
        if self.ladder is not None and any(
            not MIN_R_MULTIPLE <= step.r_multiple <= MAX_R_MULTIPLE for step in self.ladder.steps
        ):
            raise ValueError(f"Bậc thang TP phải trong [{MIN_R_MULTIPLE}, {MAX_R_MULTIPLE}]R.")
        if self.is_empty():
            raise ValueError("Ghi đè thoát lệnh cần ít nhất một trường.")

    def is_empty(self) -> bool:
        return all(
            getattr(self, name) is None
            for name in (
                "stop_atr_multiple",
                "trailing_atr_multiple",
                "take_profit_atr_multiple",
                "max_hold_bars",
                "ladder",
            )
        )

    def payload(self) -> dict[str, Any]:
        """JSON canonical: chỉ trường có giá trị, Decimal dạng chuỗi chuẩn hóa."""
        data: dict[str, Any] = {}
        for name in ("stop_atr_multiple", "trailing_atr_multiple", "take_profit_atr_multiple"):
            value = getattr(self, name)
            if value is not None:
                data[name] = _decimal_text(value)
        if self.max_hold_bars is not None:
            data["max_hold_bars"] = self.max_hold_bars
        if self.ladder is not None:
            data["ladder"] = {
                "steps": [
                    {
                        "r_multiple": _decimal_text(step.r_multiple),
                        "fraction": _decimal_text(step.fraction),
                    }
                    for step in self.ladder.steps
                ],
                "breakeven_after_first": self.ladder.breakeven_after_first,
            }
        return data

    def content_hash(self) -> str:
        canonical = json.dumps(
            {"version": EXIT_OVERRIDE_VERSION, "override": self.payload()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def apply_to_recipe(self, recipe: ExitRecipe) -> ExitRecipe:
        """Recipe mới với trường đã ghi đè; recipe_id giữ nguyên (tra bằng chứng thoát)."""
        changes: dict[str, Any] = {}
        if self.stop_atr_multiple is not None:
            changes["stop_atr_multiple"] = float(self.stop_atr_multiple)
        if self.trailing_atr_multiple is not None:
            changes["trailing_atr_multiple"] = float(self.trailing_atr_multiple)
        if self.take_profit_atr_multiple is not None:
            changes["take_profit_atr_multiple"] = float(self.take_profit_atr_multiple)
        if self.max_hold_bars is not None:
            changes["max_hold_bars"] = self.max_hold_bars
        return replace(recipe, **changes) if changes else recipe

    def apply_to_ladder(self, ladder: TakeProfitLadder | None) -> TakeProfitLadder | None:
        """Thang mới chỉ thay khi release vốn có thang (validator đã chặn trường hợp còn lại)."""
        if self.ladder is None or ladder is None:
            return ladder
        return self.ladder


def exit_override_from_payload(payload: Any) -> ExitOverride | None:
    """Dựng từ JSON canonical (overlay/job); rỗng hoặc None → None."""
    if not payload:
        return None
    if not isinstance(payload, dict):
        raise ValueError("Ghi đè thoát lệnh phải là object JSON.")
    unknown = set(payload) - {
        "stop_atr_multiple",
        "trailing_atr_multiple",
        "take_profit_atr_multiple",
        "max_hold_bars",
        "ladder",
    }
    if unknown:
        raise ValueError(f"Trường ghi đè thoát lệnh không hỗ trợ: {', '.join(sorted(unknown))}.")
    ladder = None
    raw_ladder = payload.get("ladder")
    if raw_ladder is not None:
        if not isinstance(raw_ladder, dict) or not isinstance(raw_ladder.get("steps"), list):
            raise ValueError("Thang TP cần danh sách steps.")
        ladder = TakeProfitLadder(
            steps=tuple(
                LadderStep(_decimal(step.get("r_multiple")), _decimal(step.get("fraction")))
                for step in raw_ladder["steps"]
            ),
            breakeven_after_first=bool(raw_ladder.get("breakeven_after_first", False)),
        )
    hold = payload.get("max_hold_bars")
    return ExitOverride(
        stop_atr_multiple=_optional_decimal(payload.get("stop_atr_multiple")),
        trailing_atr_multiple=_optional_decimal(payload.get("trailing_atr_multiple")),
        take_profit_atr_multiple=_optional_decimal(payload.get("take_profit_atr_multiple")),
        max_hold_bars=hold if hold is None else _int(hold),
        ladder=ladder,
    )


def _int(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError("max_hold_bars phải là số nguyên.")


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise ValueError("Giá trị thập phân phải là chuỗi hoặc số nguyên.")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("Giá trị thập phân phải hữu hạn.")
    return parsed


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", "0"} else text
