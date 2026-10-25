from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from trading_bot.admin import settings_service
from trading_bot.admin.settings_service import SettingsAdminError, SettingsAdminService, redact
from trading_bot.auth.policy import Principal, Role
from trading_bot.config.workspace_settings import WorkspaceSettings


def sample_settings() -> WorkspaceSettings:
    return WorkspaceSettings.model_validate(
        {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT"],
                "timeframes": ["5m", "1h"],
                "max_age_seconds": 30,
            },
            "features": {"formula_version": "features-v1", "sessions_timezone": "UTC"},
            "strategy": {
                "default_release": "h01-v1",
                "parameters": {},
                "entry_timeframe": "5m",
                "confirmation_timeframe": "1h",
            },
            "risk": {"profile_version_id": str(uuid4())},
            "paper_execution": {
                "quote_asset": "USDT",
                "starting_equity": "10000",
                "fee_schedule_version": "fees-v1",
                "fill_model": "next_bar_with_adverse_costs",
            },
        }
    )


def principal() -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )


class MemoryUnitOfWork:
    instances: list[MemoryUnitOfWork] = []

    def __init__(self, sessions: object, **kwargs: object) -> None:
        del sessions
        self.workspace_id = kwargs["workspace_id"]
        self.request_id = kwargs.get("request_id") or uuid4()
        self.actor_membership_id = kwargs.get("actor_membership_id")
        self.session = MemorySession()
        self.audits: list[dict[str, Any]] = []
        self.events: list[object] = []
        self.committed = False
        self.__class__.instances.append(self)

    async def __aenter__(self) -> MemoryUnitOfWork:
        self._versions_before = list(MemorySettingsRepository.versions)
        self._current_before = MemorySettingsRepository.current
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.committed = exc_type is None
        if exc_type is not None:
            MemorySettingsRepository.versions = self._versions_before
            MemorySettingsRepository.current = self._current_before

    def record_audit(self, **kwargs: Any) -> None:
        self.audits.append(kwargs)

    def enqueue(self, event: object) -> None:
        self.events.append(event)


class MemorySettingsRepository:
    versions: list[SimpleNamespace] = []
    current: SimpleNamespace | None = None
    publish_race_current: SimpleNamespace | None = None

    def __init__(self, session: object) -> None:
        del session

    async def lock_workspace_risk(self, workspace_id: object) -> None:
        del workspace_id

    async def lock_workspace(self, workspace_id: object) -> None:
        del workspace_id

    async def get_effective(self, *, workspace_id: object) -> SimpleNamespace | None:
        del workspace_id
        return self.current

    async def list_versions(self, *, workspace_id: object) -> list[SimpleNamespace]:
        del workspace_id
        return list(self.versions)

    async def create_draft(self, **kwargs: Any) -> SimpleNamespace:
        record = make_version(
            version=max((item.version for item in self.versions), default=0) + 1,
            parent_version_id=kwargs["parent_version_id"],
            settings=kwargs["settings"],
            status="DRAFT",
        )
        self.versions.append(record)
        return record

    async def publish_draft(
        self,
        *,
        draft_id: object,
        expected_parent_version_id: object,
        **kwargs: Any,
    ) -> SimpleNamespace:
        del kwargs
        if self.publish_race_current is not None:
            self.current = self.publish_race_current
        if (self.current.id if self.current else None) != expected_parent_version_id:
            from trading_bot.adapters.database.repositories.workspace_settings import (
                SettingsVersionConflict,
            )

            raise SettingsVersionConflict("Phiên bản cha đã thay đổi.")
        record = next(item for item in self.versions if item.id == draft_id)
        record.status = "PUBLISHED"
        record.approved_by = uuid4()
        record.published_at = datetime.now(UTC)
        record.effective_at = record.published_at
        self.current = record
        return record


class MemorySession:
    scalar_result: object = None

    async def scalar(self, statement: object) -> None:
        query = str(statement)
        if "count(" in query:
            return self.scalar_result
        if "risk_profile_version" in query:
            return SimpleNamespace()
        return None


@pytest.mark.asyncio
async def test_strategy_only_publish_is_not_blocked_by_active_paper_state() -> None:
    before = sample_settings()
    payload = before.model_dump(mode="json")
    payload["strategy"]["default_release"] = "h02-v1"
    next_settings = WorkspaceSettings.model_validate(payload)
    session = MemorySession()
    session.scalar_calls = 0

    async def scalar(statement: object) -> int:
        del statement
        session.scalar_calls += 1
        return 1

    session.scalar = scalar  # type: ignore[method-assign]
    await SettingsAdminService._guard_paper_policy_change(
        SimpleNamespace(session=session),
        workspace_id=uuid4(),
        current_settings=before,
        next_settings=next_settings,
        principal=principal_with(Role.OWNER),
    )

    assert session.scalar_calls == 0


@pytest.mark.asyncio
async def test_execution_profile_change_is_blocked_while_paper_state_is_active() -> None:
    before = sample_settings()
    payload = before.model_dump(mode="json")
    payload["paper_execution"]["starting_equity"] = "10001"
    next_settings = WorkspaceSettings.model_validate(payload)
    session = MemorySession()

    async def scalar(statement: object) -> int:
        del statement
        return 1

    session.scalar = scalar  # type: ignore[method-assign]
    with pytest.raises(SettingsAdminError, match="Không thể đổi PAPER execution"):
        await SettingsAdminService._guard_paper_policy_change(
            SimpleNamespace(session=session),
            workspace_id=uuid4(),
            current_settings=before,
            next_settings=next_settings,
            principal=principal_with(Role.STRATEGY_ADMIN),
        )


@pytest.mark.asyncio
async def test_execution_profile_guard_rejects_principal_without_workspace_bot_visibility() -> None:
    """RLS sẽ đếm thiếu bot sibling; guard phải từ chối trước khi đếm."""
    before = sample_settings()
    payload = before.model_dump(mode="json")
    payload["paper_execution"]["starting_equity"] = "10001"
    next_settings = WorkspaceSettings.model_validate(payload)
    session = MemorySession()
    session.scalar_calls = 0

    async def scalar(statement: object) -> int:
        del statement
        session.scalar_calls += 1
        return 0

    session.scalar = scalar  # type: ignore[method-assign]
    with pytest.raises(SettingsAdminError, match="toàn bộ bot"):
        await SettingsAdminService._guard_paper_policy_change(
            SimpleNamespace(session=session),
            workspace_id=uuid4(),
            current_settings=before,
            next_settings=next_settings,
            principal=principal_with(Role.OPERATOR),
        )
    assert session.scalar_calls == 0


def principal_with(role: Role) -> Principal:
    return Principal(uuid4(), uuid4(), uuid4(), frozenset({role}), datetime.now(UTC))


def make_version(
    *, version: int, parent_version_id: object, settings: WorkspaceSettings, status: str
) -> SimpleNamespace:
    now = datetime.now(UTC) if status == "PUBLISHED" else None
    return SimpleNamespace(
        id=uuid4(),
        version=version,
        parent_version_id=parent_version_id,
        status=status,
        revision=1,
        settings_hash="hash",
        settings_json=settings.model_dump(mode="json"),
        created_by=uuid4(),
        approved_by=uuid4() if now else None,
        published_at=now,
        effective_at=now,
    )


@pytest.fixture(autouse=True)
def memory_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    MemoryUnitOfWork.instances.clear()
    MemorySettingsRepository.versions = []
    MemorySettingsRepository.current = None
    MemorySettingsRepository.publish_race_current = None
    MemorySession.scalar_result = None
    monkeypatch.setattr(settings_service, "UnitOfWork", MemoryUnitOfWork)
    monkeypatch.setattr(settings_service, "WorkspaceSettingsRepository", MemorySettingsRepository)


@pytest.mark.asyncio
async def test_stale_parent_rejects_draft_before_persistence() -> None:
    actor = principal()
    current = make_version(
        version=1, parent_version_id=None, settings=sample_settings(), status="PUBLISHED"
    )
    MemorySettingsRepository.current = current
    MemorySettingsRepository.versions = [current]

    with pytest.raises(SettingsAdminError) as error:
        await SettingsAdminService(object()).create_draft(
            actor,
            settings=sample_settings(),
            expected_parent_version_id=uuid4(),
        )

    assert error.value.status_code == 409
    assert len(MemorySettingsRepository.versions) == 1


@pytest.mark.asyncio
async def test_created_draft_is_not_effective_and_audit_outbox_share_uow() -> None:
    actor = principal()

    draft = await SettingsAdminService(object()).create_draft(
        actor,
        settings=sample_settings(),
        expected_parent_version_id=None,
    )

    assert draft["status"] == "DRAFT"
    assert MemorySettingsRepository.current is None
    uow = MemoryUnitOfWork.instances[-1]
    assert uow.committed is True
    assert len(uow.audits) == 1
    assert len(uow.events) == 1


@pytest.mark.asyncio
async def test_unknown_strategy_release_cannot_validate_or_publish() -> None:
    actor = principal()
    payload = sample_settings().model_dump(mode="json")
    payload["strategy"]["default_release"] = "unregistered-v99"
    draft = await SettingsAdminService(object()).create_draft(
        actor,
        settings=WorkspaceSettings.model_validate(payload),
        expected_parent_version_id=None,
    )
    draft_id = UUID(str(draft["id"]))

    validation = await SettingsAdminService(object()).validate_draft(actor, draft_id=draft_id)
    assert validation["valid"] is False
    assert validation["errors"][0]["field"] == "strategy.default_release"

    with pytest.raises(SettingsAdminError) as error:
        await SettingsAdminService(object()).publish_draft(
            actor, draft_id=draft_id, expected_revision=1
        )
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_paper_policy_change_is_blocked_after_intents_exist() -> None:
    actor = principal()
    original = sample_settings()
    parent = make_version(version=1, parent_version_id=None, settings=original, status="PUBLISHED")
    MemorySettingsRepository.current = parent
    MemorySettingsRepository.versions = [parent]
    payload = original.model_dump(mode="json")
    payload["paper_execution"]["fee_schedule_version"] = "fees-v2"
    changed = WorkspaceSettings.model_validate(payload)
    draft = await SettingsAdminService(object()).create_draft(
        actor,
        settings=changed,
        expected_parent_version_id=parent.id,
    )
    MemorySession.scalar_result = 1

    with pytest.raises(SettingsAdminError, match="chưa kết thúc") as error:
        await SettingsAdminService(object()).publish_draft(
            actor, draft_id=UUID(str(draft["id"])), expected_revision=1
        )

    assert error.value.status_code == 409
    assert MemorySettingsRepository.current is parent


@pytest.mark.asyncio
async def test_paper_policy_change_is_allowed_when_all_account_state_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal()
    original = sample_settings()
    parent = make_version(version=1, parent_version_id=None, settings=original, status="PUBLISHED")
    MemorySettingsRepository.current = parent
    MemorySettingsRepository.versions = [parent]
    payload = original.model_dump(mode="json")
    payload["paper_execution"]["fee_schedule_version"] = "fees-v2"
    changed = WorkspaceSettings.model_validate(payload)
    draft = await SettingsAdminService(object()).create_draft(
        actor,
        settings=changed,
        expected_parent_version_id=parent.id,
    )
    MemorySession.scalar_result = 0
    monkeypatch.setattr(settings_service, "risk_limits_from_profile", lambda _profile: None)

    published = await SettingsAdminService(object()).publish_draft(
        actor, draft_id=UUID(str(draft["id"])), expected_revision=1
    )

    assert published["status"] == "PUBLISHED"
    assert published["settings"]["paper_execution"]["fee_schedule_version"] == "fees-v2"


@pytest.mark.parametrize(
    ("role", "mfa_age_seconds"),
    [(Role.STRATEGY_ADMIN, 0), (Role.OWNER, 16 * 60), (Role.OWNER, -60)],
)
@pytest.mark.asyncio
async def test_risk_reference_change_requires_permission_and_recent_mfa(
    role: Role, mfa_age_seconds: int
) -> None:
    original = sample_settings()
    parent = make_version(version=1, parent_version_id=None, settings=original, status="PUBLISHED")
    MemorySettingsRepository.current = parent
    MemorySettingsRepository.versions = [parent]
    actor = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC) - timedelta(seconds=mfa_age_seconds),
    )
    changed_payload = original.model_dump(mode="json")
    changed_payload["risk"]["profile_version_id"] = str(uuid4())
    changed = WorkspaceSettings.model_validate(changed_payload)

    with pytest.raises(SettingsAdminError) as error:
        await SettingsAdminService(object()).create_draft(
            actor,
            settings=changed,
            expected_parent_version_id=parent.id,
        )

    assert error.value.status_code == 403
    assert len(MemorySettingsRepository.versions) == 1


@pytest.mark.asyncio
async def test_publish_maps_racing_head_change_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = principal()
    settings = sample_settings()
    parent = make_version(version=1, parent_version_id=None, settings=settings, status="PUBLISHED")
    draft = make_version(version=3, parent_version_id=parent.id, settings=settings, status="DRAFT")
    competing = make_version(
        version=2, parent_version_id=parent.id, settings=settings, status="PUBLISHED"
    )
    MemorySettingsRepository.versions = [draft, parent]
    MemorySettingsRepository.current = parent
    MemorySettingsRepository.publish_race_current = competing

    async def risk_profile_exists(self: object, statement: object) -> object:
        del self, statement
        return object()

    monkeypatch.setattr(MemorySession, "scalar", risk_profile_exists)
    monkeypatch.setattr(settings_service, "risk_limits_from_profile", lambda profile: profile)

    with pytest.raises(SettingsAdminError) as error:
        await SettingsAdminService(object()).publish_draft(
            actor, draft_id=draft.id, expected_revision=draft.revision
        )

    assert error.value.status_code == 409
    assert draft.status == "DRAFT"


@pytest.mark.asyncio
async def test_rollback_uses_publish_parent_guard_on_race() -> None:
    actor = principal()
    earlier = make_version(
        version=1, parent_version_id=None, settings=sample_settings(), status="PUBLISHED"
    )
    current = make_version(
        version=2, parent_version_id=earlier.id, settings=sample_settings(), status="PUBLISHED"
    )
    competing = make_version(
        version=3, parent_version_id=current.id, settings=sample_settings(), status="PUBLISHED"
    )
    MemorySettingsRepository.versions = [earlier, current]
    MemorySettingsRepository.current = current
    MemorySettingsRepository.publish_race_current = competing

    with pytest.raises(SettingsAdminError) as error:
        await SettingsAdminService(object()).rollback(
            actor, version=earlier.version, reason="Kiểm tra cạnh tranh publish"
        )

    assert error.value.status_code == 409
    assert not any(item.status == "DRAFT" for item in MemorySettingsRepository.versions)


@pytest.mark.asyncio
async def test_rollback_publishes_new_version_and_writes_audit_and_outbox() -> None:
    actor = principal()
    earlier = make_version(
        version=1, parent_version_id=None, settings=sample_settings(), status="PUBLISHED"
    )
    current = make_version(
        version=2, parent_version_id=earlier.id, settings=sample_settings(), status="PUBLISHED"
    )
    MemorySettingsRepository.versions = [earlier, current]
    MemorySettingsRepository.current = current

    result = await SettingsAdminService(object()).rollback(
        actor, version=earlier.version, reason="Khôi phục cấu hình đã kiểm tra"
    )

    assert result["version"] == 3
    assert result["id"] not in {str(earlier.id), str(current.id)}
    assert MemorySettingsRepository.versions[-1].parent_version_id == current.id
    uow = MemoryUnitOfWork.instances[-1]
    assert uow.committed is True
    assert len(uow.audits) == 1
    assert uow.audits[0]["action"] == "settings.rolled_back"
    assert len(uow.events) == 1


def test_response_redacts_secret_named_fields_recursively() -> None:
    assert redact({"nested": {"api_key": "raw", "mode": "PAPER"}}) == {
        "nested": {"api_key": "[đã ẩn]", "mode": "PAPER"}
    }
