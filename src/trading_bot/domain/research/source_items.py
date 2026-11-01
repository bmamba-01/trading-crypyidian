"""Metadata bất biến của revision nguồn, kiểm tra khả dụng và quyền tại một thời điểm."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv6Address
from urllib.parse import urlsplit

from trading_bot.domain.research.source_policy import SourceRightsPolicy, SourceUsePurpose

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_INVALID_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_URI_COMPONENT = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/?]|%[0-9A-Fa-f]{2})*\Z")


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải là thời điểm có múi giờ.")


def _require_canonical_https_url(value: str) -> None:
    """Kiểm tra URL tham chiếu; không cấp quyền fetch hoặc thay cổng chống SSRF."""
    message = "canonical_url phải là URL HTTPS chuẩn, không credential hoặc fragment."
    if (
        not isinstance(value, str)
        or not value.startswith("https://")
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "\\" in value
        or "#" in value
        or _INVALID_ESCAPE.search(value)
    ):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc != parsed.netloc.lower()
            or parsed.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)
            or not _URI_COMPONENT.fullmatch(f"{parsed.path}?{parsed.query}".rstrip("?"))
        ):
            raise ValueError(message)
        if ":" in host:
            IPv6Address(host)
        elif len(host) > 253 or any(not _HOST_LABEL.fullmatch(part) for part in host.split(".")):
            raise ValueError(message)
    except ValueError as error:
        raise ValueError(message) from error


@dataclass(frozen=True, slots=True)
class SourceItemRevision:
    """Một revision chỉ chứa metadata, không chứa body/excerpt hay bằng chứng quyền.

    ID ổn định dùng 1–128 ký tự ASCII chữ/số và ``_.:-``, bắt đầu bằng chữ/số.
    Caller tạo revision mới khi sửa/xóa; object không tự chọn revision mới nhất.
    ``event_at`` có thể ở tương lai nếu nguồn thông báo sự kiện đã lên lịch.
    ``available_at=None`` biểu thị chưa xác nhận khả dụng, luôn bị loại.
    """

    source_id: str
    item_id: str
    revision_id: str
    canonical_url: str
    observed_at: datetime
    available_at: datetime | None
    event_at: datetime | None = None
    published_at: datetime | None = None
    edited_at: datetime | None = None
    tombstoned: bool = False

    def __post_init__(self) -> None:
        for name in ("source_id", "item_id", "revision_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} không phải ID hợp lệ.")
        _require_canonical_https_url(self.canonical_url)
        _require_aware(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        for name in ("available_at", "event_at", "published_at", "edited_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, name)
                object.__setattr__(self, name, value.astimezone(UTC))
        if not isinstance(self.tombstoned, bool):
            raise ValueError("tombstoned phải là boolean.")
        if self.available_at is not None and self.available_at < self.observed_at:
            raise ValueError("available_at không được trước observed_at.")
        for name in ("published_at", "edited_at"):
            value = getattr(self, name)
            if value is not None and value > self.observed_at:
                raise ValueError(f"{name} không được sau observed_at.")
        if (
            self.published_at is not None
            and self.edited_at is not None
            and self.edited_at < self.published_at
        ):
            raise ValueError("edited_at không được trước published_at.")

    def is_eligible(
        self, *, as_of: datetime, purpose: SourceUsePurpose, policy: SourceRightsPolicy
    ) -> bool:
        """Chỉ cấp đúng mục đích khi revision đã khả dụng và policy khớp còn hiệu lực.

        Caller phải cung cấp policy có thẩm quyền; hàm không tra lịch sử thu hồi,
        không xác minh evidence và không lan truyền tombstone tới revision cũ.
        """
        _require_aware(as_of, "as_of")
        instant = as_of.astimezone(UTC)
        return (
            isinstance(policy, SourceRightsPolicy)
            and policy.source_id == self.source_id
            and self.available_at is not None
            and self.available_at <= instant
            and not self.tombstoned
            and policy.permits(purpose, at=instant)
        )
