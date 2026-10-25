"""Ca sử dụng Admin cấu hình bot với adapter bộ nhớ: RBAC, MFA, revision, idempotency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.unit.test_bot_config_resolver import HARD_CAPS, World

from trading_bot.adapters.database.repositories.bot_config_versions import (
    BotConfigVersionConflict,
)
from trading_bot.admin import bot_service
from trading_bot.admin.bot_service import BotAdminError, BotConfigAdminService, RootContext
from trading_bot.application.runtime_risk_profile import risk_limits_from_profile
from trading_bot.auth.bot_policy import WORKSPACE_BOT_ROLES, BotPermission
from trading_bot.auth.bot_policy import role_allows_bot_permission as role_allows
from trading_bot.auth.policy import Principal, Role
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash

EMPTY = BotConfigOverlay.model_validate({"schema_version": 1})
TIGHT = BotConfigOverlay.model_validate(
    {"schema_version": 1, "risk": {"per_trade_risk_fraction": "0.005"}}
)
UNIVERSE = BotConfigOverlay.model_validate(
    {"schema_version": 1, "universe": {"symbols": ["BTCUSDT"]}}
)
TOO_LOOSE = BotConfigOverlay.model_validate({"schema_version": 1, "risk": {"max_positions": 9}})


@dataclass
class Store:
    world: World = field(default_factory=World)
    versions: list[SimpleNamespace] = field(default_factory=list)
    requests: list[SimpleNamespace] = field(default_factory=list)
    grants: set[tuple[UUID, UUID, str]] = field(default_factory=set)
    accounts: list[SimpleNamespace] = field(default_factory=list)
    bots: list[SimpleNamespace] = field(default_factory=list)
    locks: list[str] = field(default_factory=list)
    root_writes: int = 0
    root_available: bool = True


STORE = Store()


class MemoryUnitOfWork:
    instances: list[MemoryUnitOfWork] = []

    def __init__(self, sessions: object, **kwargs: Any) -> None:
        del sessions
        self.workspace_id = kwargs["workspace_id"]
        self.bot_id = kwargs.get("bot_id")
        self.actor_membership_id = kwargs.get("actor_membership_id")
        self.request_id = kwargs.get("request_id") or uuid4()
        self.session = object()
        self.audits: list[dict[str, Any]] = []
        self.events: list[Any] = []
        self.committed = False
        MemoryUnitOfWork.instances.append(self)

    async def __aenter__(self) -> MemoryUnitOfWork:
        self._snapshot = (
            [SimpleNamespace(**vars(row)) for row in STORE.versions],
            list(STORE.requests),
            list(STORE.accounts),
            list(STORE.bots),
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.committed = exc_type is None
        if exc_type is not None:
            STORE.versions, STORE.requests, STORE.accounts, STORE.bots = self._snapshot

    def record_audit(self, **kwargs: Any) -> None:
        self.audits.append(kwargs)

    def enqueue(self, event: Any) -> None:
        self.events.append(event)


class MemoryVersions:
    def __init__(self, session: object) -> None:
        del session

    async def lock_bot(self, *, workspace_id: UUID, bot_id: UUID) -> None:
        STORE.locks.append(f"bot:{bot_id}")

    def _scoped(self, workspace_id: UUID, bot_id: UUID) -> list[SimpleNamespace]:
        return [
            row
            for row in STORE.versions
            if row.workspace_id == workspace_id and row.bot_id == bot_id
        ]

    async def get(self, *, workspace_id: UUID, bot_id: UUID, version_id: UUID) -> Any:
        return next(
            (row for row in self._scoped(workspace_id, bot_id) if row.id == version_id), None
        )

    async def get_by_number(self, *, workspace_id: UUID, bot_id: UUID, version: int) -> Any:
        return next(
            (row for row in self._scoped(workspace_id, bot_id) if row.version == version), None
        )

    async def list_versions(self, *, workspace_id: UUID, bot_id: UUID) -> list[Any]:
        return sorted(self._scoped(workspace_id, bot_id), key=lambda row: -row.version)

    async def get_head(self, *, workspace_id: UUID, bot_id: UUID) -> Any:
        published = [r for r in self._scoped(workspace_id, bot_id) if r.status == "PUBLISHED"]
        return max(published, key=lambda row: row.version, default=None)

    async def create_draft(self, **kwargs: Any) -> Any:
        payload = canonical_overlay(kwargs["overlay"])
        row = SimpleNamespace(
            id=uuid4(),
            workspace_id=kwargs["workspace_id"],
            bot_id=kwargs["bot_id"],
            account_id=kwargs["account_id"],
            version=len(self._scoped(kwargs["workspace_id"], kwargs["bot_id"])) + 1,
            parent_version_id=kwargs["parent_version_id"],
            source_version_id=kwargs.get("source_version_id"),
            schema_version=1,
            overlay_json=payload,
            overlay_hash=overlay_hash(payload),
            base_settings_version_id=kwargs["base_settings_version_id"],
            base_settings_hash=kwargs["base_settings_hash"],
            status="DRAFT",
            revision=1,
            created_by=kwargs["created_by"],
            updated_by=kwargs["created_by"],
            approved_by=None,
            published_at=None,
            effective_at=None,
            created_at=datetime.now(UTC),
        )
        STORE.versions.append(row)
        return row

    async def update_draft(self, **kwargs: Any) -> Any:
        row = await self.get(
            workspace_id=kwargs["workspace_id"],
            bot_id=kwargs["bot_id"],
            version_id=kwargs["draft_id"],
        )
        if row is None or row.status != "DRAFT" or row.revision != kwargs["expected_revision"]:
            raise BotConfigVersionConflict("conflict")
        payload = canonical_overlay(kwargs["overlay"])
        row.overlay_json = payload
        row.overlay_hash = overlay_hash(payload)
        row.revision += 1
        row.updated_by = kwargs["updated_by"]
        return row

    async def publish_draft(self, **kwargs: Any) -> Any:
        row = await self.get(
            workspace_id=kwargs["workspace_id"],
            bot_id=kwargs["bot_id"],
            version_id=kwargs["draft_id"],
        )
        head = await self.get_head(workspace_id=kwargs["workspace_id"], bot_id=kwargs["bot_id"])
        if (
            row is None
            or row.status != "DRAFT"
            or row.revision != kwargs["expected_revision"]
            or (head.id if head else None) != kwargs["expected_parent_version_id"]
        ):
            raise BotConfigVersionConflict("conflict")
        moment = datetime.now(UTC)
        row.status = "PUBLISHED"
        row.approved_by = kwargs["approved_by"]
        row.published_at = moment
        row.effective_at = moment
        return row

    async def find_request(self, **kwargs: Any) -> Any:
        return next(
            (
                row
                for row in STORE.requests
                if row.workspace_id == kwargs["workspace_id"]
                and row.bot_id == kwargs["bot_id"]
                and row.operation == kwargs["operation"]
                and row.idempotency_key == kwargs["idempotency_key"]
            ),
            None,
        )

    async def record_request(self, **kwargs: Any) -> Any:
        row = SimpleNamespace(**kwargs, bot_config_version_id=kwargs["version_id"])
        STORE.requests.append(row)
        return row


class MemoryGrants:
    def __init__(self, session: object) -> None:
        del session

    async def require(
        self, principal: Principal, *, bot_id: UUID, permission: BotPermission
    ) -> None:
        if not role_allows(principal.roles, permission):
            raise PermissionError("role")
        if role_allows(principal.roles & WORKSPACE_BOT_ROLES, permission):
            return
        if (principal.membership_id, bot_id, permission.value) not in STORE.grants:
            raise PermissionError("grant")


class MemoryBots:
    def __init__(self, session: object) -> None:
        del session

    async def get_with_account(self, *, workspace_id: UUID, bot_id: UUID) -> Any:
        for bot in STORE.bots:
            if bot.workspace_id == workspace_id and bot.id == bot_id:
                account = next(a for a in STORE.accounts if a.id == bot.trading_account_id)
                return bot, account
        return None

    async def list_with_accounts(self, *, workspace_id: UUID) -> list[Any]:
        return [
            (bot, next(a for a in STORE.accounts if a.id == bot.trading_account_id))
            for bot in STORE.bots
            if bot.workspace_id == workspace_id
        ]

    async def get_for_account(self, *, workspace_id: UUID, account_id: UUID) -> Any:
        return next(
            (
                bot
                for bot in STORE.bots
                if bot.workspace_id == workspace_id and bot.trading_account_id == account_id
            ),
            None,
        )

    async def get_account_by_name(self, *, workspace_id: UUID, name: str) -> Any:
        return next(
            (a for a in STORE.accounts if a.workspace_id == workspace_id and a.name == name), None
        )

    async def create_paper_account(
        self, *, workspace_id: UUID, name: str, max_notional: Decimal
    ) -> Any:
        account = add_bot(workspace_id, name=name, max_notional=max_notional, status="DISABLED")
        return account


class MemorySettings:
    def __init__(self, session: object) -> None:
        del session

    async def lock_workspace(self, workspace_id: UUID) -> None:
        STORE.locks.append(f"workspace:{workspace_id}")


async def memory_root(session: object, *, workspace_id: UUID) -> RootContext | None:
    del session
    world = STORE.world
    if not STORE.root_available or workspace_id != world.workspace_id:
        return None
    return RootContext(world.root, world.risk_profile, world.releases)  # type: ignore[arg-type]


def add_bot(
    workspace_id: UUID,
    *,
    name: str = "paper-bot",
    max_notional: Decimal = Decimal("8000"),
    status: str = "ENABLED",
    bot_id: UUID | None = None,
    account_id: UUID | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    account = SimpleNamespace(
        id=account_id or uuid4(),
        workspace_id=workspace_id,
        name=name,
        mode="PAPER",
        venue="binance_usdm",
        environment="local",
        status=status,
        max_notional=max_notional,
    )
    bot = SimpleNamespace(
        id=bot_id or uuid4(),
        workspace_id=workspace_id,
        trading_account_id=account.id,
        name=name,
        status=status,
    )
    STORE.accounts.append(account)
    STORE.bots.append(bot)
    return bot, account


@pytest.fixture(autouse=True)
def memory_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    global STORE
    STORE = Store()
    add_bot(
        STORE.world.workspace_id,
        bot_id=STORE.world.bot_id,
        account_id=STORE.world.account_id,
    )
    MemoryUnitOfWork.instances.clear()
    monkeypatch.setattr(bot_service, "UnitOfWork", MemoryUnitOfWork)
    monkeypatch.setattr(bot_service, "BotConfigVersionRepository", MemoryVersions)
    monkeypatch.setattr(bot_service, "BotGrantRepository", MemoryGrants)
    monkeypatch.setattr(bot_service, "BotInstanceRepository", MemoryBots)
    monkeypatch.setattr(bot_service, "WorkspaceSettingsRepository", MemorySettings)
    monkeypatch.setattr(bot_service, "load_root_context", memory_root)


def actor(role: Role, *, mfa_age: timedelta = timedelta(0), workspace: UUID | None = None):
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=workspace or STORE.world.workspace_id,
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC) - mfa_age,
    )


def service() -> BotConfigAdminService:
    return BotConfigAdminService(object())  # type: ignore[arg-type]


async def draft(principal: Principal, overlay: BotConfigOverlay = EMPTY, **kwargs: Any) -> Any:
    return await service().create_draft(
        principal,
        bot_id=STORE.world.bot_id,
        overlay=overlay,
        expected_parent_version_id=kwargs.pop("expected_parent_version_id", None),
        **kwargs,
    )


async def published(principal: Principal, overlay: BotConfigOverlay = EMPTY) -> dict[str, Any]:
    head = max(
        (row for row in STORE.versions if row.status == "PUBLISHED"),
        key=lambda row: row.version,
        default=None,
    )
    created = await draft(principal, overlay, expected_parent_version_id=head.id if head else None)
    return await service().publish(
        principal,
        bot_id=STORE.world.bot_id,
        draft_id=UUID(created["id"]),
        expected_revision=created["revision"],
    )


async def test_create_draft_pins_root_and_records_audit_outbox_in_same_uow() -> None:
    principal = actor(Role.STRATEGY_ADMIN)

    created = await draft(principal, UNIVERSE)

    assert created["status"] == "DRAFT"
    assert created["base_settings_version_id"] == str(STORE.world.root.settings_version_id)
    assert created["overlay"] == {"schema_version": 1, "universe": {"symbols": ["BTCUSDT"]}}
    unit = MemoryUnitOfWork.instances[-1]
    assert unit.committed
    assert unit.bot_id == STORE.world.bot_id
    assert [audit["action"] for audit in unit.audits] == ["bot.config.draft_created"]
    assert [event.event_type for event in unit.events] == ["bot.config.draft_created"]
    assert unit.events[0].payload["bot_id"] == str(STORE.world.bot_id)
    assert STORE.root_writes == 0


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.ANALYST_REVIEWER, Role.RISK_ADMIN])
async def test_roles_without_config_write_cannot_create_draft(role: Role) -> None:
    principal = actor(role)
    STORE.grants.add((principal.membership_id, STORE.world.bot_id, "bot:config:write"))

    with pytest.raises(BotAdminError) as error:
        await draft(principal)

    assert error.value.status_code == 403
    assert STORE.versions == []


async def test_stale_parent_is_rejected_before_persistence() -> None:
    owner = actor(Role.OWNER)
    await published(owner)

    with pytest.raises(BotAdminError) as error:
        await draft(owner, expected_parent_version_id=uuid4())

    assert error.value.status_code == 409
    assert len(STORE.versions) == 1


async def test_draft_requires_published_root_settings() -> None:
    STORE.root_available = False

    with pytest.raises(BotAdminError) as error:
        await draft(actor(Role.OWNER))

    assert error.value.status_code == 409


@pytest.mark.parametrize("mfa_age", [timedelta(minutes=16), timedelta(minutes=-1)])
async def test_risk_change_requires_recent_valid_mfa(mfa_age: timedelta) -> None:
    principal = actor(Role.OWNER, mfa_age=mfa_age)

    with pytest.raises(BotAdminError) as error:
        await draft(principal, TIGHT)
    assert error.value.status_code == 403

    created = await draft(principal, UNIVERSE)
    assert created["status"] == "DRAFT"


async def test_update_draft_checks_revision_and_emits_distinct_outbox_keys() -> None:
    principal = actor(Role.STRATEGY_ADMIN)
    created = await draft(principal)
    draft_id = UUID(created["id"])

    first = await service().update_draft(
        principal,
        bot_id=STORE.world.bot_id,
        draft_id=draft_id,
        expected_revision=1,
        overlay=UNIVERSE,
    )
    second = await service().update_draft(
        principal,
        bot_id=STORE.world.bot_id,
        draft_id=draft_id,
        expected_revision=2,
        overlay=EMPTY,
    )
    with pytest.raises(BotAdminError) as error:
        await service().update_draft(
            principal,
            bot_id=STORE.world.bot_id,
            draft_id=draft_id,
            expected_revision=2,
            overlay=UNIVERSE,
        )

    assert (first["revision"], second["revision"]) == (2, 3)
    assert error.value.status_code == 409
    keys = [unit.events[0].idempotency_key for unit in MemoryUnitOfWork.instances[1:3]]
    assert len(set(keys)) == 2


async def test_validate_reports_field_errors_for_overlay_raising_root_cap() -> None:
    principal = actor(Role.STRATEGY_ADMIN)
    good = await draft(principal, UNIVERSE)
    bad = await draft(principal, TOO_LOOSE)

    valid = await service().validate_draft(
        principal, bot_id=STORE.world.bot_id, draft_id=UUID(good["id"])
    )
    invalid = await service().validate_draft(
        principal, bot_id=STORE.world.bot_id, draft_id=UUID(bad["id"])
    )

    assert valid == {"valid": True, "errors": [], "warnings": []}
    assert invalid["valid"] is False
    assert invalid["errors"][0]["field"] == "risk.max_positions"


async def test_publish_is_serialized_with_root_lock_and_never_writes_root_settings() -> None:
    owner = actor(Role.OWNER)

    version = await published(owner, TIGHT)

    assert version["status"] == "PUBLISHED"
    assert version["effective_at"] is not None
    assert version["approved_by"] == str(owner.membership_id)
    assert STORE.locks[-2:] == [
        f"workspace:{STORE.world.workspace_id}",
        f"bot:{STORE.world.bot_id}",
    ]
    unit = MemoryUnitOfWork.instances[-1]
    assert [audit["action"] for audit in unit.audits] == ["bot.config.published"]
    assert unit.events[0].event_type == "bot.config.published"
    assert STORE.root_writes == 0


async def test_publish_rejects_overlay_that_relaxes_root_cap() -> None:
    owner = actor(Role.OWNER)
    created = await draft(owner, TOO_LOOSE)

    with pytest.raises(BotAdminError) as error:
        await service().publish(
            owner, bot_id=STORE.world.bot_id, draft_id=UUID(created["id"]), expected_revision=1
        )

    assert error.value.status_code == 422
    assert error.value.errors[0]["field"] == "risk.max_positions"
    assert STORE.versions[0].status == "DRAFT"


async def test_publish_rejects_stale_revision_and_stale_parent() -> None:
    owner = actor(Role.OWNER)
    first = await draft(owner)
    second = await draft(owner)
    await service().update_draft(
        owner,
        bot_id=STORE.world.bot_id,
        draft_id=UUID(first["id"]),
        expected_revision=1,
        overlay=UNIVERSE,
    )
    with pytest.raises(BotAdminError) as stale_revision:
        await service().publish(
            owner, bot_id=STORE.world.bot_id, draft_id=UUID(first["id"]), expected_revision=1
        )
    await service().publish(
        owner, bot_id=STORE.world.bot_id, draft_id=UUID(first["id"]), expected_revision=2
    )
    with pytest.raises(BotAdminError) as stale_parent:
        await service().publish(
            owner, bot_id=STORE.world.bot_id, draft_id=UUID(second["id"]), expected_revision=1
        )

    assert stale_revision.value.status_code == 409
    assert stale_parent.value.status_code == 409


async def test_operator_and_strategy_admin_publish_permissions() -> None:
    created = await draft(actor(Role.OWNER))
    operator = actor(Role.OPERATOR)
    STORE.grants.add((operator.membership_id, STORE.world.bot_id, "bot:config:publish"))

    with pytest.raises(BotAdminError) as error:
        await service().publish(
            operator, bot_id=STORE.world.bot_id, draft_id=UUID(created["id"]), expected_revision=1
        )
    assert error.value.status_code == 403

    result = await service().publish(
        actor(Role.STRATEGY_ADMIN),
        bot_id=STORE.world.bot_id,
        draft_id=UUID(created["id"]),
        expected_revision=1,
    )
    assert result["status"] == "PUBLISHED"


async def test_idempotent_publish_retry_returns_same_version_without_new_audit() -> None:
    owner = actor(Role.OWNER)
    created = await draft(owner)
    arguments = {
        "bot_id": STORE.world.bot_id,
        "draft_id": UUID(created["id"]),
        "expected_revision": 1,
        "idempotency_key": "publish-key-0001",
    }

    first = await service().publish(owner, **arguments)
    retry = await service().publish(owner, **arguments)

    assert retry == first
    assert MemoryUnitOfWork.instances[-1].audits == []
    assert MemoryUnitOfWork.instances[-1].events == []
    with pytest.raises(BotAdminError) as error:
        await service().publish(owner, **(arguments | {"expected_revision": 2}))
    assert error.value.status_code == 422


async def test_idempotent_create_draft_retry_creates_one_draft() -> None:
    principal = actor(Role.STRATEGY_ADMIN)

    first = await draft(principal, UNIVERSE, idempotency_key="draft-key-0001")
    retry = await draft(principal, UNIVERSE, idempotency_key="draft-key-0001")

    assert retry["id"] == first["id"]
    assert len(STORE.versions) == 1
    with pytest.raises(BotAdminError) as error:
        await draft(principal, EMPTY, idempotency_key="draft-key-0001")
    assert error.value.status_code == 422


async def test_rollback_publishes_copy_of_target_with_lineage() -> None:
    owner = actor(Role.OWNER)
    v1 = await published(owner, UNIVERSE)
    v2 = await published(owner, TIGHT)

    rolled = await service().rollback(
        owner,
        bot_id=STORE.world.bot_id,
        version=v1["version"],
        expected_current_version_id=UUID(v2["id"]),
        reason="Quay lại universe cũ",
        idempotency_key="rollback-key-0001",
    )
    retry = await service().rollback(
        owner,
        bot_id=STORE.world.bot_id,
        version=v1["version"],
        expected_current_version_id=UUID(v2["id"]),
        reason="Quay lại universe cũ",
        idempotency_key="rollback-key-0001",
    )

    assert rolled["status"] == "PUBLISHED"
    assert rolled["overlay"] == v1["overlay"]
    assert rolled["source_version_id"] == v1["id"]
    assert rolled["parent_version_id"] == v2["id"]
    assert retry["id"] == rolled["id"]
    assert len(STORE.versions) == 3
    audit = MemoryUnitOfWork.instances[-2].audits[0]
    assert audit["action"] == "bot.config.rolled_back"
    assert audit["reason"] == "Quay lại universe cũ"


async def test_rollback_rejects_stale_current_and_unpublished_target() -> None:
    owner = actor(Role.OWNER)
    v1 = await published(owner, UNIVERSE)
    pending = await draft(owner, expected_parent_version_id=UUID(v1["id"]))

    with pytest.raises(BotAdminError) as stale:
        await service().rollback(
            owner,
            bot_id=STORE.world.bot_id,
            version=1,
            expected_current_version_id=uuid4(),
            reason="x",
        )
    with pytest.raises(BotAdminError) as missing:
        await service().rollback(
            owner,
            bot_id=STORE.world.bot_id,
            version=pending["version"],
            expected_current_version_id=UUID(v1["id"]),
            reason="x",
        )

    assert stale.value.status_code == 409
    assert missing.value.status_code == 404


async def test_cross_bot_draft_and_cross_workspace_bot_are_not_found() -> None:
    owner = actor(Role.OWNER)
    other_bot, _ = add_bot(STORE.world.workspace_id, name="other-bot")
    created = await draft(owner)

    with pytest.raises(BotAdminError) as cross_bot:
        await service().publish(
            owner, bot_id=other_bot.id, draft_id=UUID(created["id"]), expected_revision=1
        )
    foreign_owner = actor(Role.OWNER, workspace=uuid4())
    with pytest.raises(BotAdminError) as cross_workspace:
        await service().get_bot(foreign_owner, bot_id=STORE.world.bot_id)

    assert cross_bot.value.status_code == 404
    assert cross_workspace.value.status_code == 404


async def test_list_bots_filters_by_read_grant_for_operator() -> None:
    hidden_bot, _ = add_bot(STORE.world.workspace_id, name="hidden-bot")
    operator = actor(Role.OPERATOR)
    STORE.grants.add((operator.membership_id, STORE.world.bot_id, "bot:read"))

    visible = await service().list_bots(operator)
    everything = await service().list_bots(actor(Role.RISK_ADMIN))

    assert [bot["id"] for bot in visible] == [str(STORE.world.bot_id)]
    assert {bot["id"] for bot in everything} == {str(STORE.world.bot_id), str(hidden_bot.id)}
    with pytest.raises(BotAdminError) as error:
        await service().get_bot(operator, bot_id=hidden_bot.id)
    assert error.value.status_code == 403


async def test_create_bot_is_owner_only_with_recent_mfa_and_idempotent_by_name() -> None:
    owner = actor(Role.OWNER)

    created, was_created = await service().create_bot(
        owner, name="paper-two", max_notional=Decimal("5000")
    )
    replay, replay_created = await service().create_bot(
        owner, name="paper-two", max_notional=Decimal("5000.000")
    )

    assert was_created is True
    assert replay_created is False
    assert replay["id"] == created["id"]
    assert created["status"] == "DISABLED"
    assert created["account"]["mode"] == "PAPER"
    assert created["account"]["max_notional"] == "5000"
    assert MemoryUnitOfWork.instances[0].audits[0]["action"] == "bot.created"
    with pytest.raises(BotAdminError) as conflict:
        await service().create_bot(owner, name="paper-two", max_notional=Decimal("6000"))
    assert conflict.value.status_code == 409
    for principal in (actor(Role.STRATEGY_ADMIN), actor(Role.OWNER, mfa_age=timedelta(hours=1))):
        with pytest.raises(BotAdminError) as denied:
            await service().create_bot(principal, name="paper-three", max_notional=Decimal("1"))
        assert denied.value.status_code == 403


def test_risk_profile_fixture_matches_hard_caps() -> None:
    limits = risk_limits_from_profile(
        SimpleNamespace(
            id=uuid4(),
            workspace_id=uuid4(),
            name="x",
            version=1,
            max_risk_fraction=Decimal("0.01"),
            settings={"schema_version": 1, "hard_caps": dict(HARD_CAPS)},
        )
    ).limits
    assert limits.max_positions == 2
