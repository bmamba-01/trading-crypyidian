"""Lịch sử append-only cho revision metadata của cùng một item nguồn."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from trading_bot.domain.research.source_items import SourceItemRevision

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class SourceItemHistoryConflict(RuntimeError):
    """Từ chối revision trùng khác payload hoặc nối sai predecessor."""


@dataclass(frozen=True, slots=True)
class SourceItemRevisionEntry:
    """Revision kèm predecessor và hash để phát hiện sửa lịch sử."""

    revision: SourceItemRevision
    predecessor_revision_id: str | None
    predecessor_hash: str | None
    revision_hash: str

    def __post_init__(self) -> None:
        if type(self.revision) is not SourceItemRevision:
            raise ValueError("revision phải là SourceItemRevision.")
        predecessor_id = self.predecessor_revision_id
        predecessor_hash = self.predecessor_hash
        if (predecessor_id is None) != (predecessor_hash is None):
            raise ValueError("predecessor_revision_id và predecessor_hash phải đi cùng nhau.")
        if predecessor_id is not None and not _IDENTIFIER.fullmatch(predecessor_id):
            raise ValueError("predecessor_revision_id không hợp lệ.")
        if predecessor_hash is not None and not _is_sha256(predecessor_hash):
            raise ValueError("predecessor_hash phải là SHA-256 hex.")
        if not _is_sha256(self.revision_hash):
            raise ValueError("revision_hash phải là SHA-256 hex.")
        expected_hash = _revision_digest(self.revision, predecessor_hash)
        if self.revision_hash != expected_hash:
            raise ValueError("revision_hash không khớp metadata và predecessor.")

    @classmethod
    def create(
        cls,
        revision: SourceItemRevision,
        *,
        predecessor_revision_id: str | None,
        predecessor_hash: str | None,
    ) -> SourceItemRevisionEntry:
        """Sinh entry đã băm từ metadata và predecessor đã biết."""
        return cls(
            revision=revision,
            predecessor_revision_id=predecessor_revision_id,
            predecessor_hash=predecessor_hash,
            revision_hash=_revision_digest(revision, predecessor_hash),
        )


@dataclass(frozen=True, slots=True)
class SourceItemHistory:
    """Chuỗi revision cho đúng một source/item; không chứa nội dung bài viết."""

    source_id: str
    item_id: str
    entries: tuple[SourceItemRevisionEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not _IDENTIFIER.fullmatch(self.source_id):
            raise ValueError("source_id không hợp lệ.")
        if not isinstance(self.item_id, str) or not _IDENTIFIER.fullmatch(self.item_id):
            raise ValueError("item_id không hợp lệ.")
        if type(self.entries) is not tuple:
            raise ValueError("entries phải là tuple bất biến.")
        seen_ids: set[str] = set()
        prior_id: str | None = None
        prior_hash: str | None = None
        prior_observed_at: datetime | None = None
        tombstoned = False
        for entry in self.entries:
            if type(entry) is not SourceItemRevisionEntry:
                raise ValueError("Mọi entry phải là SourceItemRevisionEntry.")
            revision = entry.revision
            if (revision.source_id, revision.item_id) != (self.source_id, self.item_id):
                raise ValueError("Revision không thuộc source/item của history.")
            if revision.revision_id in seen_ids:
                raise ValueError("revision_id bị trùng trong history.")
            if entry.predecessor_revision_id != prior_id or entry.predecessor_hash != prior_hash:
                raise ValueError("Chuỗi predecessor của history không liên tục.")
            if prior_observed_at is not None and revision.observed_at < prior_observed_at:
                raise ValueError("observed_at phải không giảm theo thứ tự append.")
            if tombstoned:
                raise ValueError("Không thể append revision sau tombstone.")
            seen_ids.add(revision.revision_id)
            prior_id = revision.revision_id
            prior_hash = entry.revision_hash
            prior_observed_at = revision.observed_at
            tombstoned = revision.tombstoned

    @classmethod
    def empty(cls, *, source_id: str, item_id: str) -> SourceItemHistory:
        """Tạo lịch sử rỗng cho cặp source/item đã chuẩn hóa."""
        return cls(source_id=source_id, item_id=item_id)

    @property
    def latest(self) -> SourceItemRevisionEntry | None:
        """Revision append mới nhất, không suy diễn rằng nó đã khả dụng."""
        return self.entries[-1] if self.entries else None

    def append(
        self,
        revision: SourceItemRevision,
        *,
        predecessor_revision_id: str | None,
    ) -> SourceItemHistory:
        """Append revision mới; retry đồng nhất idempotent, payload khác bị từ chối."""
        if type(revision) is not SourceItemRevision:
            raise ValueError("revision phải là SourceItemRevision.")
        if (revision.source_id, revision.item_id) != (self.source_id, self.item_id):
            raise SourceItemHistoryConflict("Revision không thuộc source/item của history.")
        for existing in self.entries:
            if existing.revision.revision_id == revision.revision_id:
                if (
                    existing.revision == revision
                    and existing.predecessor_revision_id == predecessor_revision_id
                ):
                    return self
                raise SourceItemHistoryConflict("revision_id đã có payload khác.")

        latest = self.latest
        expected_predecessor_id = latest.revision.revision_id if latest is not None else None
        expected_predecessor_hash = latest.revision_hash if latest is not None else None
        if predecessor_revision_id != expected_predecessor_id:
            raise SourceItemHistoryConflict("predecessor_revision_id không khớp revision mới nhất.")
        if latest is not None:
            if latest.revision.tombstoned:
                raise SourceItemHistoryConflict("Không thể append sau tombstone của item.")
            if revision.observed_at < latest.revision.observed_at:
                raise SourceItemHistoryConflict("observed_at không được lùi so với revision trước.")
        entry = SourceItemRevisionEntry.create(
            revision,
            predecessor_revision_id=expected_predecessor_id,
            predecessor_hash=expected_predecessor_hash,
        )
        return SourceItemHistory(self.source_id, self.item_id, (*self.entries, entry))

    def select_as_of(
        self,
        as_of: datetime,
        *,
        evaluated_at: datetime,
        current_takedown_at: datetime | None = None,
    ) -> SourceItemRevision | None:
        """Chọn revision khả dụng tại as-of, trừ khi tombstone/takedown hiện hành.

        Takedown và tombstone được xem là trạng thái hiện hành, nên không làm lộ lại
        revision cũ trong historical replay. Rights policy vẫn phải được kiểm tra
        riêng bằng `evaluate_source_revision_access`.
        """
        instant = _utc(as_of, "as_of")
        evaluated = _utc(evaluated_at, "evaluated_at")
        withdrawn = (
            _utc(current_takedown_at, "current_takedown_at")
            if current_takedown_at is not None
            else None
        )
        if instant > evaluated:
            raise ValueError("as_of không được sau evaluated_at.")
        if withdrawn is not None and withdrawn <= evaluated:
            return None

        tombstones = [
            entry.revision.available_at
            for entry in self.entries
            if entry.revision.tombstoned
            and entry.revision.available_at is not None
            and entry.revision.available_at <= evaluated
        ]
        if tombstones:
            return None

        eligible = [
            entry
            for entry in self.entries
            if entry.revision.available_at is not None and entry.revision.available_at <= instant
        ]
        if not eligible:
            return None
        selected = eligible[-1].revision
        return None if selected.tombstoned else selected


def _revision_digest(revision: SourceItemRevision, predecessor_hash: str | None) -> str:
    payload = {
        "source_id": revision.source_id,
        "item_id": revision.item_id,
        "revision_id": revision.revision_id,
        "canonical_url": revision.canonical_url,
        "observed_at": _timestamp(revision.observed_at),
        "available_at": _timestamp(revision.available_at),
        "event_at": _timestamp(revision.event_at),
        "published_at": _timestamp(revision.published_at),
        "edited_at": _timestamp(revision.edited_at),
        "tombstoned": revision.tombstoned,
        "predecessor_hash": predecessor_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
