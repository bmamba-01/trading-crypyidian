"""Unit tests cho hợp đồng erasure WP-05G (CRY-308)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.application.source_erasure import (
    InMemoryBlobObjectStore,
    SourceErasureOrchestrator,
)
from trading_bot.domain.research.erasure import (
    ErasureRequest,
    ErasureTargetKind,
    ErasureTargetRef,
    ErasureTaskStatus,
    RetentionPolicy,
    build_deletion_receipt,
)

NOW = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
WS_ID = uuid4()


def test_cannot_claim_erased_without_confirmed_absence() -> None:
    """Không được tạo DeletionReceipt với status=ERASED khi confirmed_absent=False."""
    with pytest.raises(ValueError, match="confirmed_absent=False"):
        build_deletion_receipt(
            idempotency_key="er-1",
            workspace_id=WS_ID,
            source_id="NEWS_A",
            item_id="item-1",
            target_kind=ErasureTargetKind.BLOB_OBJECT,
            target_ref="blobs/item-1.json",
            status=ErasureTaskStatus.ERASED,
            confirmed_absent=False,
            reason_code="takedown",
            attempts=1,
            occurred_at=NOW,
        )


def test_erasure_deletes_blob_propagates_derived_is_idempotent_and_cleans_export() -> None:
    """Revoke/takedown xóa blob thật, lan truyền đủ 4 tầng dẫn xuất và idempotent khi chạy lại."""
    store = InMemoryBlobObjectStore()
    store.put_blob("blobs/item-1.bin", b"copyrighted-excerpt-body")
    store.put_blob("blobs/item-keep.bin", b"allowed-item")

    calls: list[tuple[ErasureTargetKind, str]] = []

    def _ok_handler(req: ErasureRequest, target: ErasureTargetRef):
        calls.append((target.kind, target.target_ref))
        return ErasureTaskStatus.ERASED, True, req.reason_code

    orch = SourceErasureOrchestrator(
        blob_store=store,
        derived_handlers={
            ErasureTargetKind.EXTRACTED_EVENT: _ok_handler,
            ErasureTargetKind.DATASET_MANIFEST: _ok_handler,
            ErasureTargetKind.REPORT_ARTIFACT: _ok_handler,
            ErasureTargetKind.MODEL_REGISTRY: _ok_handler,
        },
        clock=lambda: NOW,
    )
    req = ErasureRequest(
        idempotency_key="takedown-item-1",
        workspace_id=WS_ID,
        source_id="NEWS_A",
        item_id="item-1",
        reason_code="dmca_takedown",
        retention_policy=RetentionPolicy(max_retention_days=30),
        blob_object_key="blobs/item-1.bin",
        derived_targets=(
            ErasureTargetRef(ErasureTargetKind.EXTRACTED_EVENT, "ev-1"),
            ErasureTargetRef(ErasureTargetKind.DATASET_MANIFEST, "man-1"),
            ErasureTargetRef(ErasureTargetKind.REPORT_ARTIFACT, "rep-1"),
            ErasureTargetRef(ErasureTargetKind.MODEL_REGISTRY, "mod-1"),
        ),
    )

    s1 = orch.execute_erasure(req)
    assert s1.erased_claimed is True
    assert s1.dead_letter_targets == ()
    assert store.exists("blobs/item-1.bin") is False
    # Kiểm export/backup list chỉ còn item hợp lệ
    assert store.list_keys() == ("blobs/item-keep.bin",)
    assert len(s1.receipts) == 5

    # Chạy lại cùng idempotency_key -> không gọi handler lặp lại, trả cùng receipt hash
    s2 = orch.execute_erasure(req)
    assert len(calls) == 4
    assert [r.receipt_hash for r in s2.receipts] == [r.receipt_hash for r in s1.receipts]


def test_immutable_manifest_hash_erasure_fails_closed_and_excludes_from_training() -> None:
    """Khi điều khoản đòi xóa cả hash trên manifest bất biến WP-06 -> EXCLUDED_FAIL_CLOSED."""
    store = InMemoryBlobObjectStore()
    store.put_blob("blobs/item-2.bin", b"body")

    orch = SourceErasureOrchestrator(blob_store=store, clock=lambda: NOW)
    req = ErasureRequest(
        idempotency_key="takedown-hash-2",
        workspace_id=WS_ID,
        source_id="NEWS_B",
        item_id="item-2",
        reason_code="strict_hash_erasure",
        retention_policy=RetentionPolicy(max_retention_days=7, require_hash_erasure=True),
        blob_object_key="blobs/item-2.bin",
        derived_targets=(
            ErasureTargetRef(
                ErasureTargetKind.DATASET_MANIFEST,
                "wp06-immutable-manifest-v3",
                is_immutable_manifest=True,
            ),
        ),
    )
    summary = orch.execute_erasure(req)
    assert summary.erased_claimed is True
    assert summary.excluded_immutable_targets == ("wp06-immutable-manifest-v3",)
    man_rec = next(
        r for r in summary.receipts if r.target_kind is ErasureTargetKind.DATASET_MANIFEST
    )
    assert man_rec.status is ErasureTaskStatus.EXCLUDED_FAIL_CLOSED
    assert man_rec.reason_code == "ERASURE_IMMUTABLE_HASH_EXCLUDED"


def test_handler_failure_retries_then_dead_letters_and_refuses_erased_claim() -> None:
    """Lỗi handler vượt quá max_attempts -> chuyển DEAD_LETTER và KHÔNG tuyên bố đã xóa."""
    store = InMemoryBlobObjectStore()
    store.put_blob("blobs/item-3.bin", b"body")
    attempts = 0

    def _flaky_fail(_req: ErasureRequest, _target: ErasureTargetRef):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("downstream-storage-timeout")

    orch = SourceErasureOrchestrator(
        blob_store=store,
        derived_handlers={ErasureTargetKind.REPORT_ARTIFACT: _flaky_fail},
        max_attempts=3,
        clock=lambda: NOW,
    )
    req = ErasureRequest(
        idempotency_key="takedown-fail-3",
        workspace_id=WS_ID,
        source_id="NEWS_C",
        item_id="item-3",
        reason_code="policy_revoked",
        retention_policy=RetentionPolicy(max_retention_days=30),
        blob_object_key="blobs/item-3.bin",
        derived_targets=(ErasureTargetRef(ErasureTargetKind.REPORT_ARTIFACT, "rep-locked"),),
    )

    summary = orch.execute_erasure(req)
    assert attempts == 3
    assert summary.erased_claimed is False
    assert summary.dead_letter_targets == ("rep-locked",)
    assert len(orch.dead_letter_queue) == 1

    # Kiểm retention deadline
    pol = RetentionPolicy(max_retention_days=30)
    assert pol.is_retention_expired(captured_at=NOW - timedelta(days=31), as_of=NOW) is True
    assert pol.is_retention_expired(captured_at=NOW - timedelta(days=10), as_of=NOW) is False
