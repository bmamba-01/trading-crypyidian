"""Kiểm tra ca sử dụng quét policy hết hạn theo workspace."""

from __future__ import annotations

from uuid import uuid4

import pytest

from trading_bot.application.source_policy_expiry import SourcePolicyExpiryScanner


class RecordingExpiryWriter:
    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[tuple[object, int]] = []

    async def enqueue_expired_policy_events(self, *, workspace_id, batch_size: int) -> int:
        self.calls.append((workspace_id, batch_size))
        return self.result


@pytest.mark.asyncio
async def test_expiry_scanner_delegates_workspace_and_batch_to_database_writer() -> None:
    workspace_id = uuid4()
    writer = RecordingExpiryWriter(result=2)

    count = await SourcePolicyExpiryScanner(writer).scan_workspace(workspace_id, batch_size=7)

    assert count == 2
    assert writer.calls == [(workspace_id, 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace_id", "batch_size"),
    [(None, 10), (uuid4(), 0), (uuid4(), 501), (uuid4(), True)],
)
async def test_expiry_scanner_rejects_invalid_scope_or_unbounded_batch(
    workspace_id, batch_size
) -> None:
    writer = RecordingExpiryWriter()

    with pytest.raises(ValueError, match="(workspace_id|batch_size)"):
        await SourcePolicyExpiryScanner(writer).scan_workspace(workspace_id, batch_size=batch_size)

    assert writer.calls == []


@pytest.mark.asyncio
async def test_expiry_scanner_rejects_impossible_repository_count() -> None:
    writer = RecordingExpiryWriter(result=2)

    with pytest.raises(RuntimeError, match="batch size"):
        await SourcePolicyExpiryScanner(writer).scan_workspace(uuid4(), batch_size=1)
