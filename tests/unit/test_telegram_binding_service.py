"""Kiểm tra mã liên kết Telegram một lần và ranh giới quyền quản trị."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from trading_bot.application.telegram_binding import TelegramBindingService
from trading_bot.auth.policy import Principal, Role

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")


class BindingRepositoryStub:
    def __init__(self) -> None:
        self.created: list[tuple[UUID, UUID, str, datetime, str]] = []
        self.revoked: list[tuple[UUID, UUID, str]] = []
        self.bindings: list[dict[str, str]] = []

    async def create_link_request(
        self,
        workspace_id: UUID,
        membership_id: UUID,
        created_by: UUID,
        digest: str,
        expires_at: datetime,
        reason: str,
    ) -> None:
        assert created_by == membership_id
        self.created.append((workspace_id, membership_id, digest, expires_at, reason))

    async def revoke(
        self, workspace_id: UUID, membership_id: UUID, actor_membership_id: UUID, reason: str
    ) -> int:
        assert actor_membership_id == MEMBERSHIP
        self.revoked.append((workspace_id, membership_id, reason))
        return 1

    async def list_bindings(self, workspace_id: UUID) -> list[dict[str, str]]:
        return self.bindings


def _principal(*, role: Role = Role.OWNER, mfa_age: timedelta = timedelta(seconds=1)) -> Principal:
    return Principal(
        user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
        membership_id=MEMBERSHIP,
        workspace_id=WORKSPACE,
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC) - mfa_age,
    )


@pytest.mark.asyncio
async def test_link_code_is_high_entropy_hashed_and_deep_link_is_valid() -> None:
    repository = BindingRepositoryStub()
    service = TelegramBindingService(repository, bot_username="crypidian_test_bot")

    link = await service.create_link(_principal(), reason="Liên kết thiết bị Telegram")

    assert link.code
    assert len(link.code) >= 43
    assert len(link.code) <= 64
    assert all(character.isalnum() or character in "_-" for character in link.code)
    assert link.url == f"https://t.me/crypidian_test_bot?start={link.code}"
    assert repository.created[0][2] != link.code
    assert len(repository.created[0][2]) == 64
    assert repository.created[0][3] - datetime.now(UTC) <= timedelta(minutes=10)
    assert "code" not in repository.created[0][4].lower()


@pytest.mark.asyncio
async def test_link_requires_owner_recent_mfa_and_reason() -> None:
    service = TelegramBindingService(BindingRepositoryStub(), bot_username="crypidian_test_bot")

    with pytest.raises(ValueError, match="owner"):
        await service.create_link(_principal(role=Role.OPERATOR), reason="reason")
    with pytest.raises(ValueError, match="MFA"):
        await service.create_link(_principal(mfa_age=timedelta(minutes=16)), reason="reason")
    with pytest.raises(ValueError, match="lý do"):
        await service.create_link(_principal(), reason=" ")


@pytest.mark.asyncio
async def test_revoke_records_only_reason_and_membership_scope() -> None:
    repository = BindingRepositoryStub()
    service = TelegramBindingService(repository, bot_username="crypidian_test_bot")

    await service.revoke(_principal(), MEMBERSHIP, reason="Thu hồi do yêu cầu thành viên")

    assert repository.revoked == [(WORKSPACE, MEMBERSHIP, "Thu hồi do yêu cầu thành viên")]
