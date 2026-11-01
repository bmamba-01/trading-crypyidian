"""Kiểm tra chính sách mark khi retry mục flatten đã khớp một phần."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from trading_bot.application.paper_runtime_application import _flatten_mark_failure_code

NOW = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
MARK_ID = UUID("50000000-0000-0000-0000-000000000001")


def _item(*, partial: bool = False) -> SimpleNamespace:
    result = (
        {
            "outcome": "PARTIALLY_CLOSED",
            "mark_record_id": str(MARK_ID),
            "mark_close_time": (NOW - timedelta(seconds=5)).isoformat(),
        }
        if partial
        else {}
    )
    return SimpleNamespace(
        mark_price=Decimal("100"),
        mark_record_id=MARK_ID,
        mark_revision=1,
        mark_checksum="a" * 64,
        mark_close_time=NOW - timedelta(seconds=5),
        result=result,
    )


def _mark(
    *,
    mark_id: UUID = MARK_ID,
    revision: int = 1,
    checksum: str = "a" * 64,
    close_time: datetime = NOW - timedelta(seconds=5),
) -> SimpleNamespace:
    return SimpleNamespace(
        id=mark_id,
        revision=revision,
        checksum=checksum,
        close_time=close_time,
    )


def test_partial_retry_waits_for_mark_after_last_partial_attempt() -> None:
    item = _item(partial=True)

    same_mark = _flatten_mark_failure_code(
        item, _mark(), now=NOW, max_mark_age=timedelta(seconds=60)
    )
    new_mark = _flatten_mark_failure_code(
        item,
        _mark(mark_id=UUID("50000000-0000-0000-0000-000000000002"), close_time=NOW),
        now=NOW,
        max_mark_age=timedelta(seconds=60),
    )

    assert same_mark == "FLATTEN_MARK_NOT_ADVANCED"
    assert new_mark is None


def test_first_attempt_still_requires_the_preview_mark() -> None:
    failure = _flatten_mark_failure_code(
        _item(),
        _mark(mark_id=UUID("50000000-0000-0000-0000-000000000002"), close_time=NOW),
        now=NOW,
        max_mark_age=timedelta(seconds=60),
    )

    assert failure == "FLATTEN_MARK_CHANGED"
