"""Hợp đồng erasure, retention, blob store và deletion receipt (WP-05G / CRY-308).

Nguyên tắc cốt lõi:
- Không bao giờ tuyên bố `ERASED` khi chỉ mới có event ledger mà chưa xác nhận blob/record đã xóa.
- Mọi hành động xóa đều sinh `DeletionReceipt` bất biến gắn `idempotency_key` và `receipt_hash`.
- Nếu điều khoản yêu cầu xóa cả hash/reference mà manifest bất biến WP-06 chưa hỗ trợ xóa vật lý,
  bắt buộc chuyển sang `EXCLUDED_FAIL_CLOSED` (`ERASURE_IMMUTABLE_HASH_EXCLUDED`) để loại sample
  khỏi training thay vì giả vờ đã xóa.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ErasureTargetKind(StrEnum):
    """Các tầng dữ liệu và artifact dẫn xuất phải nhận lan truyền takedown/erasure."""

    BLOB_OBJECT = "BLOB_OBJECT"
    EXTRACTED_EVENT = "EXTRACTED_EVENT"
    DATASET_MANIFEST = "DATASET_MANIFEST"
    REPORT_ARTIFACT = "REPORT_ARTIFACT"
    MODEL_REGISTRY = "MODEL_REGISTRY"


class ErasureTaskStatus(StrEnum):
    """Trạng thái thực thi erasure cho từng target."""

    PENDING = "PENDING"
    ERASED = "ERASED"
    EXCLUDED_FAIL_CLOSED = "EXCLUDED_FAIL_CLOSED"
    RETRYING = "RETRYING"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Hợp đồng thời hạn lưu giữ authoritative cho blob/body/excerpt/embedding."""

    max_retention_days: int
    require_hash_erasure: bool = False

    def __post_init__(self) -> None:
        if self.max_retention_days <= 0:
            raise ValueError("max_retention_days phải lớn hơn 0.")

    def deadline_for(self, captured_at: datetime) -> datetime:
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at phải có timezone.")
        return captured_at.astimezone(UTC) + timedelta(days=self.max_retention_days)

    def is_retention_expired(self, *, captured_at: datetime, as_of: datetime) -> bool:
        return as_of.astimezone(UTC) >= self.deadline_for(captured_at)


class BlobObjectStorePort(Protocol):
    """Giao diện object store cho blob body/excerpt/raw response."""

    def put_blob(self, object_key: str, payload: bytes) -> str: ...

    def exists(self, object_key: str) -> bool: ...

    def delete_blob(self, object_key: str) -> bool: ...

    def list_keys(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class ErasureTargetRef:
    """Tham chiếu một đối tượng cụ thể cần thực thi erasure hoặc loại trừ."""

    kind: ErasureTargetKind
    target_ref: str
    is_immutable_manifest: bool = False


@dataclass(frozen=True, slots=True)
class ErasureRequest:
    """Yêu cầu erasure/takedown gắn chặt idempotency key và chính sách retention."""

    idempotency_key: str
    workspace_id: UUID
    source_id: str
    item_id: str
    reason_code: str
    retention_policy: RetentionPolicy
    blob_object_key: str | None
    derived_targets: tuple[ErasureTargetRef, ...]


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    """Biên nhận xóa bất biến; cấm trạng thái `ERASED` nếu `confirmed_absent` là False."""

    idempotency_key: str
    workspace_id: UUID
    source_id: str
    item_id: str
    target_kind: ErasureTargetKind
    target_ref: str
    status: ErasureTaskStatus
    confirmed_absent: bool
    reason_code: str
    attempts: int
    occurred_at: datetime
    receipt_hash: str

    def __post_init__(self) -> None:
        if self.status is ErasureTaskStatus.ERASED and not self.confirmed_absent:
            raise ValueError(
                "Không được tuyên bố ERASED khi chưa xác nhận đối tượng đã vắng mặt "
                "(confirmed_absent=False)."
            )
        if len(self.receipt_hash) != 64:
            raise ValueError("receipt_hash phải là SHA-256 64 ký tự hex.")


def build_deletion_receipt(
    *,
    idempotency_key: str,
    workspace_id: UUID,
    source_id: str,
    item_id: str,
    target_kind: ErasureTargetKind,
    target_ref: str,
    status: ErasureTaskStatus,
    confirmed_absent: bool,
    reason_code: str,
    attempts: int,
    occurred_at: datetime,
) -> DeletionReceipt:
    """Tạo `DeletionReceipt` kèm SHA-256 canonical hash."""
    occ_utc = occurred_at.astimezone(UTC)
    payload = {
        "idempotency_key": idempotency_key,
        "workspace_id": str(workspace_id),
        "source_id": source_id,
        "item_id": item_id,
        "target_kind": target_kind.value,
        "target_ref": target_ref,
        "status": status.value,
        "confirmed_absent": confirmed_absent,
        "reason_code": reason_code,
        "attempts": attempts,
        "occurred_at": occ_utc.isoformat(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DeletionReceipt(
        idempotency_key=idempotency_key,
        workspace_id=workspace_id,
        source_id=source_id,
        item_id=item_id,
        target_kind=target_kind,
        target_ref=target_ref,
        status=status,
        confirmed_absent=confirmed_absent,
        reason_code=reason_code,
        attempts=attempts,
        occurred_at=occ_utc,
        receipt_hash=digest,
    )


@dataclass(frozen=True, slots=True)
class ErasureSummary:
    """Tổng hợp kết quả một lần thực thi erasure; chỉ `erased_claimed=True` khi mọi target xong."""

    idempotency_key: str
    erased_claimed: bool
    receipts: tuple[DeletionReceipt, ...]
    dead_letter_targets: tuple[str, ...]
    excluded_immutable_targets: tuple[str, ...]
