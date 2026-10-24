from uuid import uuid4

import pytest

from trading_bot.application.outbox import UnitOfWork


class FakeSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.events: list[str] = []

    async def begin(self) -> None:
        self.events.append("begin")

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        self.events.append("scope")

    async def commit(self) -> None:
        self.events.append("commit")
        if self.fail_commit:
            raise RuntimeError("commit failed")

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def close(self) -> None:
        self.events.append("close")


@pytest.mark.asyncio
async def test_uow_publishes_callbacks_only_after_successful_commit() -> None:
    session = FakeSession()
    published: list[str] = []
    uow = UnitOfWork(lambda: session, workspace_id=uuid4())  # type: ignore[arg-type]
    async with uow:
        uow.after_commit(lambda: published.append("visible"))
        assert published == []

    assert session.events == ["begin", "scope", "commit", "close"]
    assert published == ["visible"]


@pytest.mark.asyncio
async def test_uow_does_not_publish_callbacks_when_work_rolls_back() -> None:
    session = FakeSession()
    published: list[str] = []
    uow = UnitOfWork(lambda: session, workspace_id=uuid4())  # type: ignore[arg-type]

    async def failing_work() -> None:
        async with uow:
            uow.after_commit(lambda: published.append("visible"))
            raise RuntimeError("work failed")

    with pytest.raises(RuntimeError, match="work failed"):
        await failing_work()

    assert session.events == ["begin", "scope", "rollback", "close"]
    assert published == []


@pytest.mark.asyncio
async def test_uow_does_not_publish_callbacks_when_commit_fails() -> None:
    session = FakeSession(fail_commit=True)
    published: list[str] = []
    uow = UnitOfWork(lambda: session, workspace_id=uuid4())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="commit failed"):
        async with uow:
            uow.after_commit(lambda: published.append("visible"))

    assert session.events == ["begin", "scope", "commit", "rollback", "close"]
    assert published == []


def test_uow_rejects_registration_before_enter() -> None:
    uow = UnitOfWork(lambda: FakeSession(), workspace_id=uuid4())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="async with"):
        uow.after_commit(lambda: None)


@pytest.mark.asyncio
async def test_uow_identifies_publish_failure_after_database_commit() -> None:
    session = FakeSession()
    uow = UnitOfWork(lambda: session, workspace_id=uuid4())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="commit"):
        async with uow:
            uow.after_commit(lambda: (_ for _ in ()).throw(RuntimeError("publish failed")))

    assert session.events == ["begin", "scope", "commit", "close"]
