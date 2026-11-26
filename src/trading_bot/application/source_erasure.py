"""Worker điều phối erasure, idempotency, retry/dead-letter và lan truyền artifact (CRY-308)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from trading_bot.domain.research.erasure import (
    BlobObjectStorePort,
    DeletionReceipt,
    ErasureRequest,
    ErasureSummary,
    ErasureTargetKind,
    ErasureTargetRef,
    ErasureTaskStatus,
    build_deletion_receipt,
)


class InMemoryBlobObjectStore(BlobObjectStorePort):
    """Object store cô lập phục vụ kiểm thử và diễn tập xóa blob/export an toàn."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_blob(self, object_key: str, payload: bytes) -> str:
        self._objects[object_key] = payload
        return hashlib.sha256(payload).hexdigest()

    def exists(self, object_key: str) -> bool:
        return object_key in self._objects

    def delete_blob(self, object_key: str) -> bool:
        return self._objects.pop(object_key, None) is not None

    def list_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._objects.keys()))


class DerivedArtifactErasureHandler(Protocol):
    """Handler thực thi xóa/ẩn/loại trừ trên từng loại artifact dẫn xuất."""

    def __call__(
        self, request: ErasureRequest, target: ErasureTargetRef
    ) -> tuple[ErasureTaskStatus, bool, str]: ...


class SourceErasureOrchestrator:
    """Điều phối xóa blob + lan truyền takedown có idempotency, retry và dead-letter."""

    def __init__(
        self,
        *,
        blob_store: BlobObjectStorePort,
        derived_handlers: dict[ErasureTargetKind, DerivedArtifactErasureHandler] | None = None,
        max_attempts: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts phải >= 1.")
        self._blob_store = blob_store
        self._derived_handlers = derived_handlers or {}
        self._max_attempts = max_attempts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._receipts: dict[tuple[str, ErasureTargetKind, str], DeletionReceipt] = {}
        self._dead_letter: list[DeletionReceipt] = []

    @property
    def dead_letter_queue(self) -> tuple[DeletionReceipt, ...]:
        return tuple(self._dead_letter)

    def execute_erasure(self, request: ErasureRequest) -> ErasureSummary:
        """Thực thi erasure idempotent cho blob và toàn bộ artifact dẫn xuất."""
        now = self._clock()
        receipts: list[DeletionReceipt] = []
        dead_letters: list[str] = []
        excluded_immutable: list[str] = []

        # 1. Xóa blob trong object store và xác minh lại vắng mặt
        if request.blob_object_key is not None:
            r_blob = self._erase_blob(request, request.blob_object_key, now)
            receipts.append(r_blob)
            if r_blob.status is ErasureTaskStatus.DEAD_LETTER:
                dead_letters.append(r_blob.target_ref)

        # 2. Lan truyền tới từng artifact dẫn xuất (event, manifest, report, model registry)
        for target in request.derived_targets:
            r_derived = self._erase_derived(request, target, now)
            receipts.append(r_derived)
            if r_derived.status is ErasureTaskStatus.DEAD_LETTER:
                dead_letters.append(r_derived.target_ref)
            elif r_derived.status is ErasureTaskStatus.EXCLUDED_FAIL_CLOSED:
                excluded_immutable.append(r_derived.target_ref)

        # Chỉ tuyên bố đã hoàn tất erasure khi không có target nào bị DEAD_LETTER / PENDING
        erased_claimed = bool(receipts) and all(
            r.status in (ErasureTaskStatus.ERASED, ErasureTaskStatus.EXCLUDED_FAIL_CLOSED)
            for r in receipts
        )
        return ErasureSummary(
            idempotency_key=request.idempotency_key,
            erased_claimed=erased_claimed,
            receipts=tuple(receipts),
            dead_letter_targets=tuple(dead_letters),
            excluded_immutable_targets=tuple(excluded_immutable),
        )

    def _erase_blob(
        self, request: ErasureRequest, object_key: str, now: datetime
    ) -> DeletionReceipt:
        cache_key = (request.idempotency_key, ErasureTargetKind.BLOB_OBJECT, object_key)
        if cache_key in self._receipts:
            return self._receipts[cache_key]

        last_err = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._blob_store.delete_blob(object_key)
                if not self._blob_store.exists(object_key):
                    rec = build_deletion_receipt(
                        idempotency_key=request.idempotency_key,
                        workspace_id=request.workspace_id,
                        source_id=request.source_id,
                        item_id=request.item_id,
                        target_kind=ErasureTargetKind.BLOB_OBJECT,
                        target_ref=object_key,
                        status=ErasureTaskStatus.ERASED,
                        confirmed_absent=True,
                        reason_code=request.reason_code,
                        attempts=attempt,
                        occurred_at=now,
                    )
                    self._receipts[cache_key] = rec
                    return rec
                last_err = "BLOB_STILL_EXISTS_AFTER_DELETE"
            except Exception as exc:
                last_err = str(exc)

        dlq = build_deletion_receipt(
            idempotency_key=request.idempotency_key,
            workspace_id=request.workspace_id,
            source_id=request.source_id,
            item_id=request.item_id,
            target_kind=ErasureTargetKind.BLOB_OBJECT,
            target_ref=object_key,
            status=ErasureTaskStatus.DEAD_LETTER,
            confirmed_absent=False,
            reason_code=f"ERASURE_BLOB_FAILED:{last_err}",
            attempts=self._max_attempts,
            occurred_at=now,
        )
        self._dead_letter.append(dlq)
        return dlq

    def _erase_derived(
        self, request: ErasureRequest, target: ErasureTargetRef, now: datetime
    ) -> DeletionReceipt:
        cache_key = (request.idempotency_key, target.kind, target.target_ref)
        if cache_key in self._receipts:
            return self._receipts[cache_key]

        # Nếu điều khoản yêu cầu xóa cả hash/reference nhưng manifest bất biến WP-06 không hỗ trợ
        # xóa vật lý -> loại sample khỏi training và ghi lý do fail-closed thay vì giả vờ đã xóa.
        if (
            target.kind is ErasureTargetKind.DATASET_MANIFEST
            and target.is_immutable_manifest
            and request.retention_policy.require_hash_erasure
        ):
            rec = build_deletion_receipt(
                idempotency_key=request.idempotency_key,
                workspace_id=request.workspace_id,
                source_id=request.source_id,
                item_id=request.item_id,
                target_kind=target.kind,
                target_ref=target.target_ref,
                status=ErasureTaskStatus.EXCLUDED_FAIL_CLOSED,
                confirmed_absent=False,
                reason_code="ERASURE_IMMUTABLE_HASH_EXCLUDED",
                attempts=1,
                occurred_at=now,
            )
            self._receipts[cache_key] = rec
            return rec

        handler = self._derived_handlers.get(target.kind)
        if handler is None:
            dlq = build_deletion_receipt(
                idempotency_key=request.idempotency_key,
                workspace_id=request.workspace_id,
                source_id=request.source_id,
                item_id=request.item_id,
                target_kind=target.kind,
                target_ref=target.target_ref,
                status=ErasureTaskStatus.DEAD_LETTER,
                confirmed_absent=False,
                reason_code="ERASURE_HANDLER_MISSING",
                attempts=1,
                occurred_at=now,
            )
            self._dead_letter.append(dlq)
            return dlq

        last_err = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                status, absent, code = handler(request, target)
                rec = build_deletion_receipt(
                    idempotency_key=request.idempotency_key,
                    workspace_id=request.workspace_id,
                    source_id=request.source_id,
                    item_id=request.item_id,
                    target_kind=target.kind,
                    target_ref=target.target_ref,
                    status=status,
                    confirmed_absent=absent,
                    reason_code=code,
                    attempts=attempt,
                    occurred_at=now,
                )
                self._receipts[cache_key] = rec
                return rec
            except Exception as exc:
                last_err = str(exc)

        dlq = build_deletion_receipt(
            idempotency_key=request.idempotency_key,
            workspace_id=request.workspace_id,
            source_id=request.source_id,
            item_id=request.item_id,
            target_kind=target.kind,
            target_ref=target.target_ref,
            status=ErasureTaskStatus.DEAD_LETTER,
            confirmed_absent=False,
            reason_code=f"ERASURE_HANDLER_FAILED:{last_err}",
            attempts=self._max_attempts,
            occurred_at=now,
        )
        self._dead_letter.append(dlq)
        return dlq
