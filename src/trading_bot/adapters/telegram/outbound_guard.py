"""Bảo vệ nội dung Telegram outbound: chặn/che chi tiết vị thế và số dư khi gửi vào group."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from trading_bot.adapters.telegram.errors import TelegramSensitivityBlockedError

GroupSensitivityMode = Literal["block", "redact"]
ContentSensitivity = Literal["auto", "public", "private_only"]

REDACTED_GROUP_PLACEHOLDER = "[ĐÃ CHE DỮ LIỆU NHẠY CẢM]"

# Các mẫu nhận diện nội dung nhạy cảm về vị thế, số dư, PnL, ký quỹ và bí mật xác thực;
# không khớp với thẻ tín hiệu công khai (symbol, hướng, vùng vào, SL/TP tham khảo).
_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "TRADE_RESULT_HEADER",
        re.compile(r"\b(?:KẾT\s+QUẢ\s+PAPER|TỔNG\s+HỢP\s+KẾT\s+QUẢ\s+PAPER)\b", re.IGNORECASE),
    ),
    (
        "POSITION_DETAILS",
        re.compile(
            r"(?:"
            r"mở\s+vị\s+thế|đóng\s+lệnh|vị\s+thế\s+(?:đang\s+)?mở|chi\s+tiết\s+vị\s+thế|"
            r"open\s+positions?|position\s*(?:size|qty|quantity|details?|notional|value)|"
            r"khối\s+lượng|\bnotional\b|\bentry_quantity\b|\bexit_quantity\b"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "ACCOUNT_BALANCE",
        re.compile(
            r"(?:"
            r"số\s+dư(?:\s+khả\s+dụng|\s+ví|\s+tài\s+khoản)?|"
            r"vốn\s+khả\s+dụng|tài\s+sản\s+ròng|"
            r"(?:account|wallet|available|free|total|margin)?\s*balance\s*[:=]?|"
            r"account\s+equity|\bequity\s*[:=]"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "PNL_AND_RISK_METRICS",
        re.compile(
            r"(?:"
            r"\b(?:unrealized|realized|net)?[_\s]*pnl\b|"
            r"lãi\s*/\s*lỗ|lãi\s+ròng|lỗ\s+ròng|"
            r"rủi\s+ro\s+dự\s+kiến|\breserved_risk\b|bội\s+số\s+r\s*:|"
            r"tỷ\s+lệ\s+thắng|\bwin[_\s]*rate\b|max[_\s]*drawdown|sụt\s+giảm\s+tối\s+đa"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "MARGIN_LEVERAGE_LIQUIDATION",
        re.compile(
            r"(?:"
            r"ký\s+quỹ|tỷ\s+lệ\s+ký\s+quỹ|"
            r"(?:initial|maintenance|used|available|free)\s+margin\b|"
            r"\bmargin\s*(?:[:=]|ratio|level|call|used|balance)|"
            r"đòn\s+bẩy|\bleverage\s*[:=]|"
            r"giá\s+thanh\s+lý|liquidation\s+price|\bliq\s+price\b"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "SECRET_OR_CREDENTIAL",
        re.compile(
            r"(?:"
            r"\bbot\d{5,}:[A-Za-z0-9_-]{10,}\b|"
            r"\b(?:api[_\s-]*key|api[_\s-]*secret|listen[_\s-]*key|secret[_\s-]*token)\b|"
            r"\bbearer\s+[A-Za-z0-9._-]{8,}"
            r")",
            re.IGNORECASE,
        ),
    ),
)


def is_group_chat(chat_id: int, *, is_group: bool | None = None) -> bool:
    """Xác định đích gửi có phải group/supergroup/channel hay không (Telegram dùng ID âm)."""
    if is_group is not None:
        return is_group
    return chat_id < 0


def detect_sensitive_group_content(text: str) -> tuple[str, ...]:
    """Trả về danh sách mã quy tắc khớp nội dung nhạy cảm; không lưu chuỗi giá trị thô."""
    if not text:
        return ()
    matched: list[str] = []
    for rule_name, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text) is not None:
            matched.append(rule_name)
    return tuple(matched)


def redact_sensitive_group_text(text: str) -> tuple[str, tuple[str, ...]]:
    """Che từng dòng chứa dữ liệu vị thế/số dư/bí mật nhạy cảm trước khi phát vào group."""
    matched_rules: list[str] = []
    seen_rules: set[str] = set()
    redacted_lines: list[str] = []
    for line in text.splitlines():
        line_rules = detect_sensitive_group_content(line)
        if line_rules:
            for rule in line_rules:
                if rule not in seen_rules:
                    seen_rules.add(rule)
                    matched_rules.append(rule)
            redacted_lines.append(REDACTED_GROUP_PLACEHOLDER)
        else:
            redacted_lines.append(line)
    # Kiểm tra lại toàn văn bản sau khi che từng dòng để phòng mẫu vắt qua nhiều dòng.
    remaining = "\n".join(redacted_lines)
    whole_rules = detect_sensitive_group_content(remaining)
    if whole_rules:
        for rule in whole_rules:
            if rule not in seen_rules:
                seen_rules.add(rule)
                matched_rules.append(rule)
        return REDACTED_GROUP_PLACEHOLDER, tuple(matched_rules)
    return remaining, tuple(matched_rules)


@dataclass(frozen=True, slots=True)
class GroupSensitivityGuard:
    """Chính sách ngăn rò rỉ vị thế/số dư/bí mật khi gửi tin hoặc ảnh vào group chat."""

    mode: GroupSensitivityMode = "block"

    def __post_init__(self) -> None:
        if self.mode not in {"block", "redact"}:
            raise ValueError("Chế độ bảo vệ group phải là 'block' hoặc 'redact'.")

    def inspect_text(
        self,
        chat_id: int,
        text: str,
        *,
        is_group: bool | None = None,
        sensitivity: ContentSensitivity = "auto",
    ) -> str:
        """Kiểm tra văn bản trước khi gửi; chat riêng đi thẳng, group bị chặn hoặc che."""
        if not is_group_chat(chat_id, is_group=is_group):
            return text
        if sensitivity == "private_only":
            raise TelegramSensitivityBlockedError(matched_rules=("PRIVATE_ONLY_CONTENT",))
        matched = detect_sensitive_group_content(text)
        if not matched:
            return text
        if self.mode == "redact":
            redacted, _ = redact_sensitive_group_text(text)
            return redacted
        raise TelegramSensitivityBlockedError(matched_rules=matched)

    def inspect_photo_caption(
        self,
        chat_id: int,
        caption: str,
        *,
        is_group: bool | None = None,
        sensitivity: ContentSensitivity = "auto",
    ) -> str:
        """Ảnh PNG không thể che pixel bằng regex nên mọi ảnh nhạy cảm vào group đều bị chặn."""
        if not is_group_chat(chat_id, is_group=is_group):
            return caption
        if sensitivity == "private_only":
            raise TelegramSensitivityBlockedError(matched_rules=("PRIVATE_ONLY_CONTENT",))
        matched = detect_sensitive_group_content(caption)
        if matched:
            raise TelegramSensitivityBlockedError(matched_rules=matched)
        return caption


__all__ = [
    "ContentSensitivity",
    "GroupSensitivityGuard",
    "GroupSensitivityMode",
    "REDACTED_GROUP_PLACEHOLDER",
    "TelegramSensitivityBlockedError",
    "detect_sensitive_group_content",
    "is_group_chat",
    "redact_sensitive_group_text",
]
