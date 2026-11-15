"""Fixture license evidence tổng hợp cho probe E2E cần policy nguồn VERIFIED.

Từ khi `SourcePolicyRevision.require_license_evidence()` chặn VERIFIED mới thiếu
evidence, mọi fixture phải đi đúng chuỗi như luồng admin thật:
RIGHTS_PENDING mang `license_scope` + `evidence_ref` → ghi một dòng
`source_license_evidence` cho đúng pending đó → VERIFIED mang cùng scope cùng
`license_evidence_id`/`license_digest`/`license_scope_hash`. Module chỉ tạo dữ
liệu kỹ thuật trong database Compose tạm; không cấp quyền dữ liệu sản phẩm.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.dataset_rights_invalidation import DatasetRightsChangeConsumer
from trading_bot.application.source_policy_admin import SourceLicenseEvidence
from trading_bot.domain.research.license_scope import SourceLicenseScope, scope_sha256
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourcePolicyRevision

FIXTURE_LICENSE_BODY = b"crypidian-e2e-synthetic-license-fixture"
FIXTURE_LICENSE_DIGEST = hashlib.sha256(FIXTURE_LICENSE_BODY).hexdigest()
FIXTURE_ATTRIBUTION = "Fixture E2E tổng hợp; không cấp quyền dữ liệu sản phẩm."


def fixture_license_scope(
    source_id: str,
    *,
    purposes: frozenset[SourceUsePurpose] | None = None,
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
    intervals: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d"),
) -> SourceLicenseScope:
    """Dựng scope rộng đủ bao mọi purpose fixture cần; identity suy từ `source_id`."""
    identity = source_id.upper()
    return SourceLicenseScope(
        source_id=source_id,
        provider=identity,
        product="E2E_FIXTURE",
        venue="E2E_FIXTURE",
        contract="E2E_FIXTURE",
        symbols=symbols,
        intervals=intervals,
        data_start=datetime(2010, 1, 1, tzinfo=UTC),
        data_end=datetime(2040, 1, 1, tzinfo=UTC),
        allowed_purposes=tuple(sorted(purposes or frozenset(SourceUsePurpose))),
        allowed_outputs=(
            "aggregate_report",
            "derived_features",
            "paper_inference",
            "trained_model",
        ),
        derived_use=(
            "aggregate_reporting",
            "feature_extraction",
            "model_training",
            "paper_inference",
        ),
        max_retention_days=None,
        attribution=FIXTURE_ATTRIBUTION,
    )


def pending_license_kwargs(scope: SourceLicenseScope, evidence_ref: str) -> dict[str, Any]:
    """Tham số `transition(..., RIGHTS_PENDING)` để pending mang scope/reference."""
    return {
        "evidence_ref": evidence_ref,
        "provider": scope.provider,
        "product": scope.product,
        "venue": scope.venue,
        "contract": scope.contract,
        "license_scope": scope,
        "attribution": scope.attribution,
    }


def verified_license_kwargs(
    scope: SourceLicenseScope, evidence_ref: str, evidence_id: UUID
) -> dict[str, Any]:
    """Tham số `transition(..., VERIFIED)` khớp đúng dòng evidence của pending trước."""
    return pending_license_kwargs(scope, evidence_ref) | {
        "license_evidence_id": evidence_id,
        "license_digest": FIXTURE_LICENSE_DIGEST,
        "license_scope_hash": scope_sha256(scope),
    }


async def append_fixture_license_evidence(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    revision: SourcePolicyRevision,
    actor_id: str = "fixture-license-uploader",
) -> None:
    """Trước khi ghi VERIFIED có evidence id: ghi evidence cho pending mới nhất đã lưu.

    Đi qua `SourceRegistryRepository.append_license_evidence` thật nên vẫn chịu đủ kiểm
    pending/scope/reference ở Python lẫn trigger PostgreSQL; revision khác bỏ qua.
    """
    if revision.state is not SourcePolicyState.VERIFIED or revision.license_evidence_id is None:
        return
    repository = SourceRegistryRepository(session)
    entry = await repository.get_entry(
        workspace_id=workspace_id, source_id=revision.policy.source_id
    )
    if entry is None:
        raise AssertionError("Evidence fixture cần source đã có pending.")
    pending = entry.current
    if pending.state is not SourcePolicyState.RIGHTS_PENDING or pending.license_scope is None:
        raise AssertionError("Evidence fixture cần pending mới nhất có license_scope.")
    await repository.append_license_evidence(
        workspace_id=workspace_id,
        evidence=SourceLicenseEvidence(
            evidence_id=revision.license_evidence_id,
            source_id=pending.policy.source_id,
            policy_id=pending.policy.policy_id,
            idempotency_key=uuid4(),
            evidence_ref=pending.policy.evidence_ref,
            digest=FIXTURE_LICENSE_DIGEST,
            byte_count=len(FIXTURE_LICENSE_BODY),
            media_type="text/plain",
            license_scope=pending.license_scope,
            actor_id=actor_id,
            created_at=pending.created_at,
        ),
    )


async def consume_rights_events(
    sessions: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    worker_id: str = "e2e-rights-fixture",
) -> int:
    """Chạy consumer rights thật tới khi hết event, như `research-rights-worker` trong stack.

    Mỗi revision policy phát `research.rights_changed.v1`; gate TRAINING từ chối khi còn
    event chưa có receipt `wp06.dataset-rights-eligibility.v1`. Probe không bật worker nền
    nên gọi trực tiếp consumer để giữ thứ tự xác định.
    """
    processed = 0
    while True:
        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            now = await session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
            result = await DatasetRightsChangeConsumer(session).process_batch(
                workspace_id=workspace_id, worker_id=worker_id, now=now, batch_size=50
            )
        if result.failed:
            raise AssertionError(f"Consumer rights fixture lỗi {result.failed} event.")
        if result.claimed == 0:
            return processed
        processed += result.processed
