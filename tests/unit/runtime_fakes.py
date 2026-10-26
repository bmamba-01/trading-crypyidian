"""Session giả ghi lại câu lệnh cho unit test repository registry runtime MB-05."""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy.dialects import postgresql


def compiled(statement: Any) -> str:
    """SQL PostgreSQL của statement (hoặc text) để assert FOR SHARE/FOR UPDATE."""
    try:
        return str(statement.compile(dialect=postgresql.dialect()))
    except AttributeError:  # pragma: no cover - chuỗi thô
        return str(statement)


class RecordingSession:
    """Trả kết quả theo hàng đợi, ghi lại mọi statement; repository không được commit."""

    def __init__(self, scalar_results: Iterable[Any] = (), scalars_results: Iterable[Any] = ()):
        self.scalar_results = list(scalar_results)
        self.scalars_results = list(scalars_results)
        self.statements: list[str] = []
        self.added: list[Any] = []
        self.flushes = 0
        self.flush_error: BaseException | None = None
        self.info: dict[str, Any] = {}
        self.events: list[str] = []

    async def scalar(self, statement: Any, params: Any = None) -> Any:
        del params
        self.statements.append(compiled(statement))
        self.events.append("scalar")
        return self.scalar_results.pop(0) if self.scalar_results else None

    async def scalars(self, statement: Any, params: Any = None) -> Any:
        del params
        self.statements.append(compiled(statement))
        rows = self.scalars_results.pop(0) if self.scalars_results else []
        return SimpleNamespace(all=lambda: list(rows), first=lambda: rows[0] if rows else None)

    async def execute(self, statement: Any, params: Any = None) -> Any:
        del params
        self.statements.append(compiled(statement))
        self.events.append("execute")
        return None

    def add(self, record: Any) -> None:
        self.added.append(record)

    async def flush(self) -> None:
        self.flushes += 1
        if self.flush_error is not None:
            error, self.flush_error = self.flush_error, None
            raise error

    async def commit(self) -> None:  # pragma: no cover - phải không bao giờ được gọi
        raise AssertionError("Repository không được tự commit")

    async def rollback(self) -> None:  # pragma: no cover - phải không bao giờ được gọi
        raise AssertionError("Repository không được tự rollback")


class FakeUow:
    """UnitOfWork tối thiểu: workspace + session ghi lại câu lệnh."""

    def __init__(self, workspace_id: UUID, session: RecordingSession) -> None:
        self.workspace_id = workspace_id
        self.session = session
