"""Mô-đun domain cho nhật ký giao dịch thủ công, checklist, tag setup và mistake attribution.

Thiết kế tách biệt hoàn toàn khỏi ledger bất biến:
- Nhật ký/checklist/tag/ảnh thuộc tầng rà soát (review layer), có phân quyền ``TRADE_READ`` /
  ``TRADE_REVIEW``, chế độ riêng tư (``workspace_team`` hoặc ``author_only``), chính sách lưu giữ
  (retention) và nhật ký audit append-only.
- Không bao giờ sửa đổi số liệu ledger gốc; các chỉ số chi phí lỗi (mistake attribution) và
  hiệu suất tag setup được tính đối chiếu trên snapshot báo cáo đã kiểm chứng.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

DEFAULT_JOURNAL_RETENTION_DAYS = 365
MIN_JOURNAL_RETENTION_DAYS = 7
MAX_JOURNAL_RETENTION_DAYS = 3650
MAX_NOTE_LENGTH = 4000
MAX_TAG_COUNT = 20
MAX_TAG_LENGTH = 48
MAX_CHECKLIST_ITEMS = 30
MAX_CHECKLIST_LABEL_LENGTH = 200
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MiB

ALLOWED_ATTACHMENT_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)

_TAG_PATTERN = re.compile(r"^[a-zA-Z0-9À-ỹà-ỹ _\-:/]{1,48}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_LEAK_PATTERN = re.compile(
    r"(?:api[_\- ]?key|secret[_\- ]?key|bearer\s+[a-z0-9._\-]{12,}|sk-[a-z0-9]{16,})",
    re.IGNORECASE,
)


class JournalPrivacyMode(StrEnum):
    """Phạm vi hiển thị ghi chú nhật ký trong workspace."""

    WORKSPACE_TEAM = "workspace_team"
    AUTHOR_ONLY = "author_only"


class JournalAuditAction(StrEnum):
    """Các hành động được ghi nhận trong nhật ký audit bất biến của journal."""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    ATTACHMENT_ADDED = "ATTACHMENT_ADDED"
    ATTACHMENT_REMOVED = "ATTACHMENT_REMOVED"
    SOFT_DELETED = "SOFT_DELETED"
    RETENTION_PURGED = "RETENTION_PURGED"


class JournalValidationError(ValueError):
    """Lỗi dữ liệu đầu vào nhật ký giao dịch không hợp lệ hoặc vi phạm privacy."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """Một mục kiểm tra kỷ luật thực thi của trader."""

    item_id: str
    label_vi: str
    checked: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "label_vi": self.label_vi,
            "checked": self.checked,
        }


def sanitize_note(note_vi: str | None) -> str:
    """Chuẩn hóa ghi chú thủ công và chặn rò rỉ khóa API/token."""
    if note_vi is None:
        return ""
    cleaned = note_vi.strip()
    if len(cleaned) > MAX_NOTE_LENGTH:
        raise JournalValidationError(
            "JOURNAL_NOTE_TOO_LONG",
            f"Ghi chú nhật ký tối đa {MAX_NOTE_LENGTH} ký tự.",
        )
    if _SECRET_LEAK_PATTERN.search(cleaned):
        raise JournalValidationError(
            "JOURNAL_SECRET_DETECTED",
            "Không được lưu API key, secret hoặc token vào nhật ký giao dịch.",
        )
    return cleaned


def normalize_tags(raw_tags: Sequence[str] | None, *, kind: str) -> tuple[str, ...]:
    """Chuẩn hóa danh sách tag setup hoặc mistake tag (loại trùng, giữ thứ tự)."""
    if not raw_tags:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_tags:
        tag = " ".join(str(raw).strip().split())
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH or not _TAG_PATTERN.match(tag):
            raise JournalValidationError(
                "JOURNAL_TAG_INVALID",
                f"Tag {kind} '{tag}' không hợp lệ (1-{MAX_TAG_LENGTH} ký tự an toàn).",
            )
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    if len(result) > MAX_TAG_COUNT:
        raise JournalValidationError(
            "JOURNAL_TOO_MANY_TAGS",
            f"Mỗi giao dịch tối đa {MAX_TAG_COUNT} tag {kind}.",
        )
    return tuple(result)


def normalize_checklist_items(
    raw_items: Sequence[Mapping[str, Any]] | None,
) -> tuple[ChecklistItem, ...]:
    """Kiểm định danh sách checklist của nhật ký giao dịch."""
    if not raw_items:
        return ()
    if len(raw_items) > MAX_CHECKLIST_ITEMS:
        raise JournalValidationError(
            "JOURNAL_TOO_MANY_CHECKLIST_ITEMS",
            f"Checklist tối đa {MAX_CHECKLIST_ITEMS} mục.",
        )
    normalized: list[ChecklistItem] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_items, start=1):
        raw_id = str(item.get("id") or f"item-{idx}").strip()
        label = " ".join(str(item.get("label_vi") or "").strip().split())
        if not label:
            continue
        if len(label) > MAX_CHECKLIST_LABEL_LENGTH:
            raise JournalValidationError(
                "JOURNAL_CHECKLIST_LABEL_TOO_LONG",
                f"Nhãn checklist tối đa {MAX_CHECKLIST_LABEL_LENGTH} ký tự.",
            )
        if _SECRET_LEAK_PATTERN.search(label):
            raise JournalValidationError(
                "JOURNAL_SECRET_DETECTED",
                "Không được lưu secret vào nội dung checklist.",
            )
        if raw_id in seen_ids:
            raw_id = f"{raw_id}-{idx}"
        seen_ids.add(raw_id)
        normalized.append(
            ChecklistItem(
                item_id=raw_id[:64],
                label_vi=label,
                checked=bool(item.get("checked", False)),
            )
        )
    return tuple(normalized)


def validate_retention_days(retention_days: int | None) -> int:
    """Kiểm định số ngày lưu giữ dữ liệu nhật ký theo chính sách retention."""
    days = DEFAULT_JOURNAL_RETENTION_DAYS if retention_days is None else int(retention_days)
    if days < MIN_JOURNAL_RETENTION_DAYS or days > MAX_JOURNAL_RETENTION_DAYS:
        raise JournalValidationError(
            "JOURNAL_RETENTION_INVALID",
            (
                f"Thời hạn lưu giữ phải nằm trong khoảng "
                f"{MIN_JOURNAL_RETENTION_DAYS}–{MAX_JOURNAL_RETENTION_DAYS} ngày."
            ),
        )
    return days


def compute_expires_at(created_or_updated_at: datetime, retention_days: int) -> datetime:
    """Tính thời điểm hết hạn lưu giữ (UTC)."""
    return created_or_updated_at + timedelta(days=validate_retention_days(retention_days))


def validate_attachment_metadata(
    *,
    file_name: str,
    content_type: str,
    byte_size: int,
    sha256: str,
    storage_ref: str,
    exif_stripped: bool,
) -> dict[str, Any]:
    """Kiểm định metadata ảnh đính kèm: loại tệp, dung lượng, chống traversal & EXIF privacy."""
    clean_name = PurePosixPath(file_name.replace("\\", "/")).name.strip()
    if not clean_name or clean_name in {".", ".."} or len(clean_name) > 180:
        raise JournalValidationError(
            "JOURNAL_ATTACHMENT_NAME_INVALID",
            "Tên tệp ảnh đính kèm không hợp lệ.",
        )
    normalized_type = content_type.strip().lower()
    if normalized_type not in ALLOWED_ATTACHMENT_CONTENT_TYPES:
        raise JournalValidationError(
            "JOURNAL_ATTACHMENT_TYPE_UNSUPPORTED",
            "Chỉ chấp nhận ảnh PNG, JPEG hoặc WebP.",
        )
    if byte_size <= 0 or byte_size > MAX_ATTACHMENT_BYTES:
        raise JournalValidationError(
            "JOURNAL_ATTACHMENT_SIZE_INVALID",
            f"Kích thước ảnh phải từ 1 byte đến {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB.",
        )
    normalized_hash = sha256.strip().lower()
    if not _SHA256_PATTERN.match(normalized_hash):
        raise JournalValidationError(
            "JOURNAL_ATTACHMENT_HASH_INVALID",
            "SHA-256 của ảnh đính kèm phải gồm đúng 64 ký tự hex.",
        )
    clean_ref = storage_ref.strip()
    if (
        not clean_ref
        or len(clean_ref) > 255
        or ".." in clean_ref
        or clean_ref.startswith(("http://", "https://", "javascript:", "data:"))
    ):
        raise JournalValidationError(
            "JOURNAL_ATTACHMENT_REF_INVALID",
            "Tham chiếu lưu trữ ảnh phải là khóa nội bộ hợp lệ, không dùng URL ngoài.",
        )
    if not exif_stripped:
        raise JournalValidationError(
            "JOURNAL_ATTACHMENT_EXIF_REQUIRED",
            "Ảnh đính kèm phải được xác nhận đã xóa siêu dữ liệu EXIF/GPS trước khi lưu.",
        )
    return {
        "file_name": clean_name,
        "content_type": normalized_type,
        "byte_size": int(byte_size),
        "sha256": normalized_hash,
        "storage_ref": clean_ref,
        "exif_stripped": True,
    }


def is_entry_visible_to_actor(
    entry: Mapping[str, Any],
    *,
    actor_user_id: UUID,
    is_workspace_owner: bool,
) -> bool:
    """Áp chính sách privacy: ``author_only`` chỉ cho phép tác giả hoặc OWNER xem."""
    privacy = str(entry.get("privacy_mode") or JournalPrivacyMode.WORKSPACE_TEAM)
    if privacy != JournalPrivacyMode.AUTHOR_ONLY:
        return True
    if is_workspace_owner:
        return True
    return str(entry.get("author_user_id")) == str(actor_user_id)


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def compute_mistake_attribution(
    trades: Iterable[Mapping[str, Any]],
    entries_by_trade_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Tổng hợp chi phí của từng ``mistake_tag`` trên ledger thật, tách theo ``quote_asset``.

    Theo mô hình TraderSync Mistakes Report:
    - ``loss_cost`` = tổng giá trị tuyệt đối của ``net_pnl`` các giao dịch lỗ gắn tag lỗi này.
    - ``share_of_total_loss_pct`` = tỷ lệ % ``loss_cost`` trên tổng lỗ ròng của cùng ``quote_asset``
      trong tập giao dịch đầu vào (``None`` nếu tổng lỗ = 0).
    """
    trade_list = list(trades)
    total_loss_by_quote: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for trade in trade_list:
        quote = str(trade.get("quote_asset") or "USDT")
        net_pnl = _decimal(trade.get("net_pnl"))
        if net_pnl < Decimal("0"):
            total_loss_by_quote[quote] += -net_pnl

        trade_id = str(trade.get("trade_id") or "")
        entry = entries_by_trade_id.get(trade_id)
        if not entry:
            continue
        raw_tags = entry.get("mistake_tags") or ()
        seen_tags: set[str] = set()
        for raw_tag in raw_tags:
            tag = str(raw_tag).strip()
            if not tag or tag.casefold() in seen_tags:
                continue
            seen_tags.add(tag.casefold())
            key = (quote, tag)
            bucket = buckets.setdefault(
                key,
                {
                    "tag": tag,
                    "quote_asset": quote,
                    "trade_count": 0,
                    "losing_trade_count": 0,
                    "net_pnl": Decimal("0"),
                    "loss_cost": Decimal("0"),
                    "fees_total": Decimal("0"),
                },
            )
            bucket["trade_count"] += 1
            bucket["net_pnl"] += net_pnl
            bucket["fees_total"] += _decimal(trade.get("fees"))
            if net_pnl < Decimal("0"):
                bucket["losing_trade_count"] += 1
                bucket["loss_cost"] += -net_pnl

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        quote = bucket["quote_asset"]
        total_loss = total_loss_by_quote.get(quote, Decimal("0"))
        share_pct: str | None = None
        if total_loss > Decimal("0"):
            ratio = (bucket["loss_cost"] / total_loss) * Decimal("100")
            share_pct = f"{ratio.quantize(Decimal('0.01'))}"
        rows.append(
            {
                "tag": bucket["tag"],
                "quote_asset": quote,
                "trade_count": bucket["trade_count"],
                "losing_trade_count": bucket["losing_trade_count"],
                "net_pnl": str(bucket["net_pnl"].quantize(Decimal("0.01"))),
                "loss_cost": str(bucket["loss_cost"].quantize(Decimal("0.01"))),
                "fees_total": str(bucket["fees_total"].quantize(Decimal("0.01"))),
                "share_of_total_loss_pct": share_pct,
            }
        )
    rows.sort(key=lambda r: (Decimal(r["loss_cost"]), r["trade_count"], r["tag"]), reverse=True)
    return rows


def compute_setup_tag_breakdown(
    trades: Iterable[Mapping[str, Any]],
    entries_by_trade_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Tổng hợp hiệu suất và tỷ lệ hoàn thành checklist theo từng ``setup_tag``."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for trade in trades:
        trade_id = str(trade.get("trade_id") or "")
        entry = entries_by_trade_id.get(trade_id)
        if not entry:
            continue
        raw_tags = entry.get("setup_tags") or ()
        checklist = entry.get("checklist_items") or ()
        checklist_ratio: Decimal | None = None
        if isinstance(checklist, Sequence) and len(checklist) > 0:
            done_count = sum(
                1 for item in checklist if isinstance(item, Mapping) and item.get("checked")
            )
            checklist_ratio = Decimal(done_count) / Decimal(len(checklist))

        quote = str(trade.get("quote_asset") or "USDT")
        net_pnl = _decimal(trade.get("net_pnl"))
        seen_tags: set[str] = set()
        for raw_tag in raw_tags:
            tag = str(raw_tag).strip()
            if not tag or tag.casefold() in seen_tags:
                continue
            seen_tags.add(tag.casefold())
            bucket = buckets.setdefault(
                (quote, tag),
                {
                    "tag": tag,
                    "quote_asset": quote,
                    "trade_count": 0,
                    "win_count": 0,
                    "net_pnl": Decimal("0"),
                    "checklist_ratios": [],
                },
            )
            bucket["trade_count"] += 1
            if net_pnl > Decimal("0"):
                bucket["win_count"] += 1
            bucket["net_pnl"] += net_pnl
            if checklist_ratio is not None:
                bucket["checklist_ratios"].append(checklist_ratio)

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        count = bucket["trade_count"]
        win_rate = f"{(Decimal(bucket['win_count']) / Decimal(count)).quantize(Decimal('0.0001'))}"
        ratios: list[Decimal] = bucket["checklist_ratios"]
        avg_checklist: str | None = None
        if ratios:
            avg = sum(ratios, Decimal("0")) / Decimal(len(ratios))
            avg_checklist = f"{(avg * Decimal('100')).quantize(Decimal('0.01'))}"
        rows.append(
            {
                "tag": bucket["tag"],
                "quote_asset": quote if (quote := bucket["quote_asset"]) else "USDT",
                "trade_count": count,
                "win_count": bucket["win_count"],
                "win_rate": win_rate,
                "net_pnl": str(bucket["net_pnl"].quantize(Decimal("0.01"))),
                "checklist_completion_pct": avg_checklist,
            }
        )
    rows.sort(key=lambda r: (Decimal(r["net_pnl"]), r["trade_count"], r["tag"]), reverse=True)
    return rows
