"""Snapshot PAPER có thể xác nhận trước khi bắt đầu thao tác flatten-all."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

FlattenTargetType = Literal["POSITION", "OPEN_ORDER"]


@dataclass(frozen=True, slots=True)
class FlattenTarget:
    """Một vị thế hoặc order đang mở cùng provenance cần thiết để đối chiếu."""

    target_type: FlattenTargetType
    target_id: str
    symbol: str
    status: str
    quantity: Decimal
    mark_price: Decimal | None = None
    mark_record_id: UUID | None = None
    mark_revision: int | None = None
    mark_checksum: str | None = None
    mark_close_time: datetime | None = None
    reservation_id: UUID | None = None
    mark_error_code: str | None = None

    def __post_init__(self) -> None:
        if self.target_type not in {"POSITION", "OPEN_ORDER"}:
            raise ValueError("Loại flatten target không hợp lệ.")
        if not self.target_id.strip() or len(self.target_id) > 240 or not self.symbol.strip():
            raise ValueError("Flatten target cần ID và symbol hợp lệ.")
        if not self.status.strip() or not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("Flatten target cần trạng thái và quantity hữu hạn dương.")
        if self.target_type == "POSITION":
            if self.mark_price is None:
                if not self.mark_error_code or any(
                    value is not None
                    for value in (
                        self.mark_record_id,
                        self.mark_revision,
                        self.mark_checksum,
                        self.mark_close_time,
                    )
                ):
                    raise ValueError("Vị thế thiếu mark cần mã lỗi và không được có provenance.")
            elif (
                not self.mark_price.is_finite()
                or self.mark_price <= 0
                or self.mark_record_id is None
                or self.mark_revision is None
                or self.mark_revision < 1
                or not _sha256(self.mark_checksum)
                or not _aware(self.mark_close_time)
                or self.mark_error_code is not None
            ):
                raise ValueError("Vị thế cần mark hợp lệ có provenance và thời điểm.")
        elif any(
            value is not None
            for value in (
                self.mark_price,
                self.mark_record_id,
                self.mark_revision,
                self.mark_checksum,
                self.mark_close_time,
                self.mark_error_code,
            )
        ):
            raise ValueError("Open order không nhận mark price.")


@dataclass(frozen=True, slots=True)
class FlattenBotSnapshot:
    """Bot/account cùng revision control và các mục cần xử lý."""

    bot_id: UUID
    account_id: UUID
    bot_control_revision: int
    workspace_control_revision: int
    targets: tuple[FlattenTarget, ...]
    readiness: str | None = None
    activity_status: str | None = None
    account_status: str = "ENABLED"
    bot_entry_state: str = "ENABLED"
    workspace_killed: bool = False
    applied_bot_control_revision: int | None = None

    def __post_init__(self) -> None:
        if self.bot_control_revision < 0 or self.workspace_control_revision < 0:
            raise ValueError("Flatten control revision không được âm.")
        if self.account_status != "ENABLED":
            raise ValueError("Flatten chỉ hỗ trợ account ENABLED.")


@dataclass(frozen=True, slots=True)
class FlattenSnapshot:
    """Snapshot canonical; thứ tự hàng truy vấn không ảnh hưởng fingerprint."""

    workspace_id: UUID
    as_of: datetime
    bots: tuple[FlattenBotSnapshot, ...]

    def __post_init__(self) -> None:
        if not _aware(self.as_of):
            raise ValueError("Flatten snapshot cần thời điểm có timezone.")
        bot_ids = [bot.bot_id for bot in self.bots]
        account_ids = [bot.account_id for bot in self.bots]
        if len(set(bot_ids)) != len(bot_ids) or len(set(account_ids)) != len(account_ids):
            raise ValueError("Flatten snapshot có bot/account duplicate.")
        targets = [
            (bot.bot_id, target.target_type, target.target_id)
            for bot in self.bots
            for target in bot.targets
        ]
        if len(set(targets)) != len(targets):
            raise ValueError("Flatten snapshot có target duplicate.")

    @property
    def content_hash(self) -> str:
        """SHA-256 nội dung giao dịch; thời điểm chụp không tự làm snapshot thành stale."""
        payload = self.to_payload()
        payload.pop("as_of", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_payload(self) -> dict[str, object]:
        """Chuyển snapshot thành JSON payload để lưu cùng preview hash."""
        result = _canonical(self)
        if not isinstance(result, dict):  # pragma: no cover - bất biến dataclass
            raise TypeError("Flatten snapshot serialization không phải object.")
        return result


def _canonical(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        values = [_canonical(item) for item in value]
        return sorted(values, key=lambda item: json.dumps(item, sort_keys=True))
    if hasattr(value, "__dataclass_fields__"):
        return {key: _canonical(getattr(value, key)) for key in value.__dataclass_fields__}
    return value


def _aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def _sha256(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
