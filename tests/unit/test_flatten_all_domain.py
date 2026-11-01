"""MB07b.3: snapshot flatten-all phải tất định và từ chối dữ liệu không hợp lệ."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.domain.execution.flatten_all import (
    FlattenBotSnapshot,
    FlattenSnapshot,
    FlattenTarget,
)

WORKSPACE = UUID("10000000-0000-0000-0000-000000000001")
BOT = UUID("20000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("30000000-0000-0000-0000-000000000001")
POSITION = FlattenTarget(
    target_type="POSITION",
    target_id="paper:entry-1",
    symbol="BTCUSDT",
    status="OPEN",
    quantity=Decimal("0.25"),
    mark_price=Decimal("65000.10"),
    mark_record_id=UUID("40000000-0000-0000-0000-000000000001"),
    mark_revision=2,
    mark_checksum="a" * 64,
    mark_close_time=datetime(2026, 11, 1, 1, tzinfo=UTC),
)


def _snapshot(targets: tuple[FlattenTarget, ...]) -> FlattenSnapshot:
    bot = FlattenBotSnapshot(
        bot_id=BOT,
        account_id=ACCOUNT,
        bot_control_revision=3,
        workspace_control_revision=2,
        targets=targets,
    )
    return FlattenSnapshot(
        workspace_id=WORKSPACE,
        as_of=datetime(2026, 11, 1, 1, 0, 10, tzinfo=UTC),
        bots=(bot,),
    )


def test_snapshot_hash_is_stable_under_database_row_order() -> None:
    first = _snapshot((POSITION,))
    second = _snapshot((POSITION,))

    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64


def test_snapshot_hash_ignores_capture_time_but_payload_keeps_it() -> None:
    first = _snapshot((POSITION,))
    later = replace(first, as_of=first.as_of.replace(second=11))

    assert first.content_hash == later.content_hash
    assert first.to_payload()["as_of"] != later.to_payload()["as_of"]


def test_snapshot_hash_changes_when_mark_or_control_revision_changes() -> None:
    changed_mark = replace(POSITION, mark_revision=3)
    changed = _snapshot((changed_mark,))

    assert changed.content_hash != _snapshot((POSITION,)).content_hash


def test_open_position_requires_freshness_provenance_fields() -> None:
    with pytest.raises(ValueError, match="mark"):
        FlattenTarget(
            target_type="POSITION",
            target_id="paper:entry-1",
            symbol="BTCUSDT",
            status="OPEN",
            quantity=Decimal("0.25"),
            mark_price=None,
            mark_record_id=None,
            mark_revision=None,
            mark_checksum=None,
            mark_close_time=None,
        )


def test_target_rejects_invalid_amount_or_time() -> None:
    with pytest.raises(ValueError, match="quantity"):
        FlattenTarget(
            target_type="POSITION",
            target_id="paper:entry-1",
            symbol="BTCUSDT",
            status="OPEN",
            quantity=Decimal("NaN"),
            mark_price=Decimal("65000"),
            mark_record_id=POSITION.mark_record_id,
            mark_revision=1,
            mark_checksum="b" * 64,
            mark_close_time=datetime.fromisoformat("2026-11-01T01:00:00"),
        )


def test_snapshot_requires_timezone_and_unique_target_identity() -> None:
    with pytest.raises(ValueError, match="timezone"):
        FlattenSnapshot(
            workspace_id=WORKSPACE,
            as_of=datetime.fromisoformat("2026-11-01T01:00:00"),
            bots=(),
        )

    with pytest.raises(ValueError, match="duplicate"):
        _snapshot((POSITION, POSITION))
