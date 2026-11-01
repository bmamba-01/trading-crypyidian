"""Kiểm tra khóa membership tuần tự hóa thao tác revoke Telegram."""

from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.repositories.telegram import TelegramBindingRepository

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")


class _Result:
    def scalars(self) -> _Result:
        return self

    def all(self) -> list[UUID]:
        return [UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")]


class _Session:
    def __init__(self) -> None:
        self.lock_statement: str | None = None
        self.update_statements: list[str] = []

    async def scalar(self, statement: object) -> UUID:
        self.lock_statement = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
        return MEMBERSHIP

    async def execute(self, statement: object) -> _Result:
        self.update_statements.append(str(statement.compile(dialect=postgresql.dialect())))  # type: ignore[attr-defined]
        return _Result()


@pytest.mark.asyncio
async def test_revoke_exclusively_locks_membership_before_invalidating_link_state() -> None:
    session = _Session()
    repository = TelegramBindingRepository(session)  # type: ignore[arg-type]

    revoked = await repository.revoke(WORKSPACE, MEMBERSHIP)

    assert revoked == 1
    assert session.lock_statement is not None
    assert "FROM user_membership" in session.lock_statement
    assert "FOR UPDATE OF user_membership" in session.lock_statement
    assert len(session.update_statements) == 3
