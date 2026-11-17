"""AI-4: cổng quyền dùng nguồn chung (fail-closed) cho nguồn không phải nến."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.unit.test_binance_rest_backfill_rights import (
    NOW,
    SOURCE_ID,
    WORKSPACE_ID,
    FakeRightsRepository,
    entry,
    observed,
)

from trading_bot.application.source_use_gate import SourceUseGate
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.research.source_registry import source_rights_hash

PAPER = frozenset({SourceUsePurpose.PAPER_DEMO})


def _gate(repository: FakeRightsRepository, source_id: str = SOURCE_ID) -> SourceUseGate:
    return SourceUseGate(workspace_id=WORKSPACE_ID, source_id=source_id, repository=repository)


@pytest.mark.asyncio
async def test_gate_returns_evidence_for_verified_purpose() -> None:
    source = entry(purposes=PAPER)
    evidence = await _gate(FakeRightsRepository(observed(source))).authorize(
        SourceUsePurpose.PAPER_DEMO
    )
    assert evidence.policy_fingerprint == source_rights_hash(source.current)
    assert evidence.checked_at == NOW
    assert evidence.terms_url.startswith("https://")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "purpose", "match"),
    [
        (entry(purposes=PAPER), SourceUsePurpose.TRAINING, "mục đích"),
        (entry(purposes=PAPER, valid_until=NOW - timedelta(seconds=1)), None, "hết hạn"),
        (entry(purposes=PAPER, evidence_ref="http://x.test/terms"), None, "HTTPS"),
    ],
)
async def test_gate_fails_closed(source, purpose, match) -> None:
    gate = _gate(FakeRightsRepository(observed(source)))
    with pytest.raises(PermissionError, match=match):
        await gate.authorize(purpose or SourceUsePurpose.PAPER_DEMO)


@pytest.mark.asyncio
async def test_gate_rejects_missing_or_mismatched_source() -> None:
    with pytest.raises(PermissionError, match="observer"):
        await _gate(FakeRightsRepository(None)).authorize(SourceUsePurpose.PAPER_DEMO)
    other = _gate(FakeRightsRepository(observed(entry(purposes=PAPER))), "BEA_RELEASE_SCHEDULE")
    with pytest.raises(PermissionError, match="không khớp"):
        await other.authorize(SourceUsePurpose.PAPER_DEMO)
