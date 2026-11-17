"""TG-4: Admin tạo hai topic (Tín hiệu / PAPER report) trong chat riêng của binding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from trading_bot.adapters.telegram.bot_api_sender import TelegramApiError
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.application.telegram_binding import (
    REPORT_TOPIC_NAME,
    SIGNAL_TOPIC_NAME,
    TelegramBindingService,
    TelegramTopicSetupError,
)
from trading_bot.auth.policy import Principal, Role

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
OWNER = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
MEMBER = UUID("b93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
CHAT = 987654321


class TopicRepositoryStub:
    def __init__(self, chat: int | None = CHAT) -> None:
        self.chat = chat
        self.saved: list[tuple[UUID, UUID, UUID, int | None, int | None]] = []
        self.save_result = True

    async def create_link_request(self, *_args: object) -> None:
        raise AssertionError

    async def revoke(self, *_args: object) -> int:
        raise AssertionError

    async def list_bindings(self, workspace_id: UUID) -> list[dict[str, str]]:
        return []

    async def binding_chat(self, workspace_id: UUID, membership_id: UUID) -> int | None:
        assert workspace_id == WORKSPACE
        return self.chat if membership_id == MEMBER else None

    async def set_topics(
        self,
        workspace_id: UUID,
        membership_id: UUID,
        actor_membership_id: UUID,
        *,
        signal_thread_id: int | None,
        report_thread_id: int | None,
    ) -> bool:
        self.saved.append(
            (workspace_id, membership_id, actor_membership_id, signal_thread_id, report_thread_id)
        )
        return self.save_result


class FailingCreator:
    def __init__(self, fail_on: int) -> None:
        self.calls = 0
        self.fail_on = fail_on

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        self.calls += 1
        if self.calls == self.fail_on:
            raise TelegramApiError(code="TELEGRAM_HTTP_400", status_code=400)
        return 500 + self.calls


def _principal(*, role: Role = Role.OWNER, mfa_age: timedelta = timedelta(seconds=1)) -> Principal:
    return Principal(
        user_id=UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57"),
        membership_id=OWNER,
        workspace_id=WORKSPACE,
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC) - mfa_age,
    )


def _service(repository: TopicRepositoryStub, creator: object | None) -> TelegramBindingService:
    return TelegramBindingService(
        repository,  # type: ignore[arg-type]
        bot_username="crypidian_test_bot",
        topic_creator=lambda: creator,  # type: ignore[arg-type,return-value]
    )


@pytest.mark.asyncio
async def test_create_topics_creates_signal_then_report_in_member_chat_and_saves_ids() -> None:
    repository = TopicRepositoryStub()
    sender = RecordingTelegramSender()

    topics = await _service(repository, sender).create_topics(_principal(), MEMBER)

    assert [(chat, name) for chat, name, _ in sender.topics] == [
        (CHAT, SIGNAL_TOPIC_NAME),
        (CHAT, REPORT_TOPIC_NAME),
    ]
    signal_id, report_id = (thread for *_, thread in sender.topics)
    assert (topics.signal_thread_id, topics.report_thread_id) == (signal_id, report_id)
    assert repository.saved == [(WORKSPACE, MEMBER, OWNER, signal_id, report_id)]
    assert SIGNAL_TOPIC_NAME == "Tín hiệu"
    assert REPORT_TOPIC_NAME == "PAPER report"


@pytest.mark.asyncio
async def test_create_topics_requires_owner_recent_mfa_and_active_binding() -> None:
    sender = RecordingTelegramSender()
    with pytest.raises(ValueError, match="owner"):
        await _service(TopicRepositoryStub(), sender).create_topics(
            _principal(role=Role.OPERATOR), MEMBER
        )
    with pytest.raises(ValueError, match="MFA"):
        await _service(TopicRepositoryStub(), sender).create_topics(
            _principal(mfa_age=timedelta(minutes=16)), MEMBER
        )
    with pytest.raises(ValueError, match="liên kết"):
        await _service(TopicRepositoryStub(chat=None), sender).create_topics(_principal(), MEMBER)
    assert sender.topics == []


@pytest.mark.asyncio
async def test_create_topics_fails_closed_without_sender() -> None:
    repository = TopicRepositoryStub()
    with pytest.raises(TelegramTopicSetupError) as captured:
        await _service(repository, None).create_topics(_principal(), MEMBER)
    assert captured.value.code == "TELEGRAM_SENDER_UNAVAILABLE"
    assert repository.saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on", [1, 2])
async def test_telegram_rejection_saves_nothing_and_keeps_old_routing(fail_on: int) -> None:
    repository = TopicRepositoryStub()
    creator = FailingCreator(fail_on)

    with pytest.raises(TelegramTopicSetupError) as captured:
        await _service(repository, creator).create_topics(_principal(), MEMBER)

    # Không lưu nửa vời: binding giữ cấu hình cũ (chat chính hoặc topic cũ).
    assert captured.value.code == "TELEGRAM_HTTP_400"
    assert repository.saved == []


@pytest.mark.asyncio
async def test_binding_revoked_during_setup_is_reported() -> None:
    repository = TopicRepositoryStub()
    repository.save_result = False
    with pytest.raises(ValueError, match="thu hồi"):
        await _service(repository, RecordingTelegramSender()).create_topics(_principal(), MEMBER)


@pytest.mark.asyncio
async def test_clear_topics_returns_to_main_chat() -> None:
    repository = TopicRepositoryStub()
    sender = RecordingTelegramSender()

    cleared = await _service(repository, sender).clear_topics(_principal(), MEMBER)

    assert cleared is True
    assert repository.saved == [(WORKSPACE, MEMBER, OWNER, None, None)]
    assert sender.topics == []
    with pytest.raises(ValueError, match="owner"):
        await _service(repository, sender).clear_topics(
            _principal(role=Role.ANALYST_REVIEWER), MEMBER
        )
