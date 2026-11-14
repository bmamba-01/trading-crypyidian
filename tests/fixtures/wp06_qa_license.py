"""Fixture quyền có phạm vi cho dữ liệu tổng hợp first-party QA của WP06."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid5

from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.source_policy_admin import SourceLicenseEvidence
from trading_bot.domain.research.license_scope import SourceLicenseScope, scope_sha256
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceRegistryEntry

QA_PROVIDER = "CRYPIDIAN_FIRST_PARTY_QA"
QA_EVIDENCE_REF = (
    "https://github.com/bmamba-01/trading-crypyidian/blob/dev/"
    "docs/research/wp06-first-party-qa-fixture.md"
)
_EVIDENCE_BYTES = b"synthetic-e2e-license-for-first-party-qa"


def _scope() -> SourceLicenseScope:
    """Trả scope giới hạn cho nến BTCUSDT 1h trong fixture synthetic."""
    return SourceLicenseScope(
        source_id=QA_PROVIDER,
        provider=QA_PROVIDER,
        product="OHLCV",
        venue="CRYPIDIAN",
        contract="SPOT",
        symbols=("BTCUSDT",),
        intervals=("1h",),
        data_start=datetime(2010, 1, 1, tzinfo=UTC),
        data_end=datetime(2040, 1, 1, tzinfo=UTC),
        allowed_purposes=(SourceUsePurpose.BACKTEST_RESEARCH, SourceUsePurpose.TRAINING),
        allowed_outputs=("aggregate_report", "derived_features", "trained_model"),
        derived_use=("aggregate_reporting", "feature_extraction", "model_training"),
        max_retention_days=None,
        attribution="Dữ liệu tổng hợp first-party chỉ dùng cho kiểm thử tích hợp.",
    )


def _evidence_id(policy_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"crypidian:wp06:first-party-qa-license:{policy_id}")


@contextmanager
def licensed_qa_source_fixture() -> Iterator[None]:
    """Gắn digest/scope và ledger evidence tổng hợp chỉ cho provider QA đã allowlist."""
    pending_policy_ids: dict[str, str] = {}
    original_transition = SourceRegistryEntry.transition
    original_append = SourceRegistryRepository.append_revision

    def transition(self: SourceRegistryEntry, **kwargs: Any) -> SourceRegistryEntry:
        state = kwargs.get("state")
        if self.source_id == QA_PROVIDER and state in {
            SourcePolicyState.RIGHTS_PENDING,
            SourcePolicyState.VERIFIED,
        }:
            scope = _scope()
            kwargs.setdefault("evidence_ref", QA_EVIDENCE_REF)
            kwargs.setdefault("provider", scope.provider)
            kwargs.setdefault("product", scope.product)
            kwargs.setdefault("venue", scope.venue)
            kwargs.setdefault("contract", scope.contract)
            kwargs.setdefault("license_scope", scope)
            kwargs.setdefault("attribution", scope.attribution)
            pending_policy_id = kwargs.get("policy_id")
            if state is SourcePolicyState.RIGHTS_PENDING and isinstance(pending_policy_id, str):
                pending_policy_ids[self.source_id] = pending_policy_id
            elif state is SourcePolicyState.VERIFIED:
                pending_policy_id = pending_policy_ids.get(self.source_id)
                if pending_policy_id is None:
                    raise ValueError("Fixture QA thiếu policy pending cho license evidence.")
                evidence_id = _evidence_id(pending_policy_id)
                kwargs.setdefault("license_evidence_id", evidence_id)
                kwargs.setdefault("license_digest", hashlib.sha256(_EVIDENCE_BYTES).hexdigest())
                kwargs.setdefault("license_scope_hash", scope_sha256(scope))
        return original_transition(self, **kwargs)

    async def append_revision(self: SourceRegistryRepository, **kwargs: Any):
        result = await original_append(self, **kwargs)
        revision = kwargs["revision"]
        if (
            revision.policy.source_id == QA_PROVIDER
            and revision.state is SourcePolicyState.RIGHTS_PENDING
        ):
            await self.append_license_evidence(
                workspace_id=kwargs["workspace_id"],
                evidence=SourceLicenseEvidence(
                    evidence_id=_evidence_id(revision.policy.policy_id),
                    source_id=QA_PROVIDER,
                    policy_id=revision.policy.policy_id,
                    idempotency_key=_evidence_id(f"idempotency:{revision.policy.policy_id}"),
                    evidence_ref=QA_EVIDENCE_REF,
                    digest=hashlib.sha256(_EVIDENCE_BYTES).hexdigest(),
                    byte_count=len(_EVIDENCE_BYTES),
                    media_type="text/plain",
                    license_scope=_scope(),
                    actor_id="fixture-license-uploader",
                    created_at=revision.created_at,
                ),
            )
        return result

    with (
        patch.object(SourceRegistryEntry, "transition", transition),
        patch.object(SourceRegistryRepository, "append_revision", append_revision),
    ):
        yield
