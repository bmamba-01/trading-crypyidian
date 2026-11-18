"""Kiểm định TDD (SEC-20 / CRY-139) cho ActionChallengeService và step-up theo hành động."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.action_challenge import (
    ACTION_BOT_ENABLE,
    ACTION_BOT_GRANT_WRITE,
    ACTION_BOT_RUNTIME_RESUME,
    ACTION_RISK_PROFILE_CREATE,
    ACTION_SETTINGS_PUBLISH,
    ACTION_SETTINGS_ROLLBACK,
    ACTION_TELEGRAM_LINK_CREATE,
    ACTION_WORKSPACE_FLATTEN_CONFIRM,
    ACTION_WORKSPACE_RUNTIME_RELEASE,
    MAX_ACTION_CHALLENGE_ATTEMPTS,
    MAX_ACTION_CHALLENGE_TTL_SECONDS,
    ActionChallengeError,
    ActionChallengeService,
    canonical_action_payload,
    hash_action_payload,
    risk_profile_create_payload,
    settings_publish_payload,
)
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity, get_auth_service, require_csrf
from trading_bot.auth.security import TotpManager
from trading_bot.auth.service import AuthIdentity, hash_token
from trading_bot.config.settings import AppSettings
from trading_bot.ports.secrets import SecretProvider

WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE = UUID("20000000-0000-4000-8000-000000000002")
USER_ID = UUID("30000000-0000-4000-8000-000000000001")
MEMBERSHIP_ID = UUID("40000000-0000-4000-8000-000000000001")
OTHER_MEMBERSHIP_ID = UUID("40000000-0000-4000-8000-000000000002")
SESSION_A = UUID("50000000-0000-4000-8000-00000000000a")
SESSION_B = UUID("50000000-0000-4000-8000-00000000000b")
BASE_TIME = datetime(2026, 11, 18, 9, 0, 0, tzinfo=UTC)


class _MemorySecretProvider(SecretProvider):
    def __init__(self, secrets_map: dict[tuple[UUID, str], str]) -> None:
        self._secrets = dict(secrets_map)

    def store_mfa_secret(self, workspace_id: UUID, secret: str) -> str:
        ref = f"ref-{uuid4().hex}"
        self._secrets[(workspace_id, ref)] = secret
        return ref

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str:
        return self._secrets[(workspace_id, reference)]

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None:
        self._secrets.pop((workspace_id, reference), None)


class _MemoryChallengeStore:
    def __init__(self) -> None:
        self.sessions: dict[UUID, Any] = {}
        self.factors: dict[tuple[UUID, UUID], Any] = {}
        self.challenges: dict[UUID, Any] = {}
        self.locks: dict[UUID, asyncio.Lock] = {}
        self.audits: list[dict[str, Any]] = []


class _FakeUnitOfWork:
    def __init__(
        self,
        store: _MemoryChallengeStore,
        *,
        workspace_id: UUID,
        actor_membership_id: UUID | None = None,
        request_id: UUID | None = None,
        **_: Any,
    ) -> None:
        self.store = store
        self.workspace_id = workspace_id
        self.actor_membership_id = actor_membership_id
        self.request_id = request_id or uuid4()
        self.session = self
        self._pending_audits: list[dict[str, Any]] = []
        self._locked: list[asyncio.Lock] = []

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc, tb
        try:
            if exc_type is None:
                self.store.audits.extend(self._pending_audits)
            self._pending_audits.clear()
        finally:
            while self._locked:
                self._locked.pop().release()

    def record_audit(self, **kwargs: Any) -> None:
        self._pending_audits.append({"workspace_id": self.workspace_id, **kwargs})


class _FakeChallengeRepo:
    def __init__(self, uow: _FakeUnitOfWork) -> None:
        self._uow = uow
        self._store = uow.store

    async def get_active_session(
        self,
        *,
        workspace_id: UUID,
        membership_id: UUID,
        session_id: UUID,
        now: datetime,
    ) -> Any:
        row = self._store.sessions.get(session_id)
        if row is None:
            return None
        if (
            row.workspace_id != workspace_id
            or row.membership_id != membership_id
            or row.revoked_at is not None
            or row.expires_at <= now
        ):
            return None
        return row

    async def create(
        self,
        *,
        workspace_id: UUID,
        membership_id: UUID,
        session_id: UUID,
        action: str,
        payload_hash: str,
        created_at: datetime,
        expires_at: datetime,
        max_attempts: int,
    ) -> Any:
        record = SimpleNamespace(
            id=uuid4(),
            created_at=created_at,
            workspace_id=workspace_id,
            membership_id=membership_id,
            session_id=session_id,
            action=action,
            payload_hash=payload_hash,
            status="PENDING",
            failed_attempts=0,
            max_attempts=max_attempts,
            verification_token_hash=None,
            expires_at=expires_at,
            verified_at=None,
            consumed_at=None,
            cancelled_at=None,
        )
        self._store.challenges[record.id] = record
        self._store.locks[record.id] = asyncio.Lock()
        return record

    async def get_for_update(self, *, workspace_id: UUID, challenge_id: UUID) -> Any:
        lock = self._store.locks.get(challenge_id)
        if lock is not None:
            await lock.acquire()
            self._uow._locked.append(lock)
        row = self._store.challenges.get(challenge_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return row

    async def get_totp_factor_for_update(self, *, workspace_id: UUID, membership_id: UUID) -> Any:
        return self._store.factors.get((workspace_id, membership_id))


class _Clock:
    def __init__(self, initial: datetime = BASE_TIME) -> None:
        self.now = initial

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


def _principal(
    *,
    session_id: UUID | None = SESSION_A,
    membership_id: UUID = MEMBERSHIP_ID,
    workspace_id: UUID = WORKSPACE,
    roles: frozenset[Role] = frozenset({Role.OWNER}),
) -> Principal:
    return Principal(
        user_id=USER_ID,
        membership_id=membership_id,
        workspace_id=workspace_id,
        roles=roles,
        mfa_verified_at=BASE_TIME,
        mfa_method=MfaMethod.TOTP,
        session_id=session_id,
    )


def _setup_service() -> tuple[ActionChallengeService, _MemoryChallengeStore, _Clock, str]:
    totp = TotpManager()
    secret = totp.generate_secret()
    clock = _Clock()
    store = _MemoryChallengeStore()
    store.sessions[SESSION_A] = SimpleNamespace(
        id=SESSION_A,
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP_ID,
        revoked_at=None,
        expires_at=BASE_TIME + timedelta(hours=12),
    )
    store.sessions[SESSION_B] = SimpleNamespace(
        id=SESSION_B,
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP_ID,
        revoked_at=None,
        expires_at=BASE_TIME + timedelta(hours=12),
    )
    ref = "mfa-ref-1"
    store.factors[(WORKSPACE, MEMBERSHIP_ID)] = SimpleNamespace(
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP_ID,
        factor_type="TOTP",
        secret_reference=ref,
        verified_at=BASE_TIME - timedelta(days=1),
        last_used_counter=None,
    )
    secrets_provider = _MemorySecretProvider({(WORKSPACE, ref): secret})
    service = ActionChallengeService(
        sessions=object(),  # type: ignore[arg-type]
        secret_provider=secrets_provider,
        totp=totp,
        clock=clock,
        unit_of_work_factory=lambda _s, **kw: _FakeUnitOfWork(store, **kw),
        repository_factory=lambda uow: _FakeChallengeRepo(uow),  # type: ignore[arg-type]
    )
    return service, store, clock, secret


def test_canonical_payload_hash_normalizes_keys_and_decimals() -> None:
    payload_1 = {
        "b": Decimal("0.0100"),
        "a": UUID("10000000-0000-4000-8000-000000000001"),
        "nested": {"z": 2, "x": 1},
    }
    payload_2 = {
        "nested": {"x": 1, "z": 2},
        "a": "10000000-0000-4000-8000-000000000001",
        "b": "0.01",
    }

    assert canonical_action_payload(payload_1) == canonical_action_payload(payload_2)
    assert hash_action_payload(payload_1) == hash_action_payload(payload_2)
    assert hash_action_payload({"b": Decimal("0.02"), "a": "x"}) != hash_action_payload(payload_1)


@pytest.mark.asyncio
async def test_create_rejects_ttl_over_5_minutes_and_missing_session() -> None:
    service, _, _, _ = _setup_service()
    principal = _principal()

    with pytest.raises(ActionChallengeError) as over_ttl:
        await service.create_challenge(
            principal,
            action=ACTION_SETTINGS_PUBLISH,
            payload={"draft_id": "1"},
            ttl_seconds=MAX_ACTION_CHALLENGE_TTL_SECONDS + 1,
        )
    assert over_ttl.value.status_code == 422
    assert over_ttl.value.code == "STEP_UP_TTL_INVALID"

    with pytest.raises(ActionChallengeError) as no_session:
        await service.create_challenge(
            _principal(session_id=None),
            action=ACTION_SETTINGS_PUBLISH,
            payload={"draft_id": "1"},
        )
    assert no_session.value.status_code == 403
    assert no_session.value.code == "STEP_UP_SESSION_REQUIRED"


@pytest.mark.asyncio
async def test_expired_challenge_is_rejected_after_5_minutes() -> None:
    service, _, clock, secret = _setup_service()
    principal = _principal()
    payload = {"bot_id": str(uuid4()), "expected_revision": 1, "reason": "Khôi phục"}
    created = await service.create_challenge(
        principal, action=ACTION_BOT_RUNTIME_RESUME, payload=payload, ttl_seconds=300
    )

    clock.advance(301)
    code = TotpManager().code_at(secret, clock())
    with pytest.raises(ActionChallengeError) as exc:
        await service.verify_and_consume(
            principal,
            challenge_id=created.challenge_id,
            action=ACTION_BOT_RUNTIME_RESUME,
            payload=payload,
            code=code,
        )
    assert exc.value.status_code == 403
    assert exc.value.code == "STEP_UP_CHALLENGE_EXPIRED"


@pytest.mark.asyncio
async def test_replay_and_concurrent_race_consume_only_once() -> None:
    service, store, clock, secret = _setup_service()
    principal = _principal()
    payload = {"bot_id": str(uuid4()), "reason": "Bật bot"}
    created = await service.create_challenge(principal, action=ACTION_BOT_ENABLE, payload=payload)
    code = TotpManager().code_at(secret, clock())

    # 1. Verify & consume concurrent race: chỉ 1 thắng, 1 bị từ chối replay.
    results = await asyncio.gather(
        service.verify_and_consume(
            principal,
            challenge_id=created.challenge_id,
            action=ACTION_BOT_ENABLE,
            payload=payload,
            code=code,
        ),
        service.verify_and_consume(
            principal,
            challenge_id=created.challenge_id,
            action=ACTION_BOT_ENABLE,
            payload=payload,
            code=code,
        ),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, ActionChallengeError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 403
    assert store.challenges[created.challenge_id].status == "CONSUMED"

    # 2. Replay sau đó vẫn bị 403 STEP_UP_CHALLENGE_ALREADY_USED.
    clock.advance(35)
    with pytest.raises(ActionChallengeError) as replay_err:
        await service.verify_and_consume(
            principal,
            challenge_id=created.challenge_id,
            action=ACTION_BOT_ENABLE,
            payload=payload,
            code=TotpManager().code_at(secret, clock()),
        )
    assert replay_err.value.status_code == 403
    assert replay_err.value.code == "STEP_UP_CHALLENGE_ALREADY_USED"


@pytest.mark.asyncio
async def test_two_step_verify_and_atomic_consume_prevents_token_replay() -> None:
    service, _, clock, secret = _setup_service()
    principal = _principal()
    payload = {"expected_revision": 4, "reason": "Mở khóa workspace"}
    created = await service.create_challenge(
        principal, action=ACTION_WORKSPACE_RUNTIME_RELEASE, payload=payload
    )
    verified = await service.verify_challenge(
        principal,
        challenge_id=created.challenge_id,
        code=TotpManager().code_at(secret, clock()),
        action=ACTION_WORKSPACE_RUNTIME_RELEASE,
        payload=payload,
    )
    assert verified.step_up_token

    # Verify lần 2 trên cùng challenge phải bị chặn.
    clock.advance(35)
    with pytest.raises(ActionChallengeError) as second_verify:
        await service.verify_challenge(
            principal,
            challenge_id=created.challenge_id,
            code=TotpManager().code_at(secret, clock()),
        )
    assert second_verify.value.status_code == 403

    consumed = await service.consume_challenge(
        principal,
        challenge_id=created.challenge_id,
        step_up_token=verified.step_up_token,
        action=ACTION_WORKSPACE_RUNTIME_RELEASE,
        payload=payload,
    )
    assert consumed.action == ACTION_WORKSPACE_RUNTIME_RELEASE

    # Replay step_up_token bị chặn.
    with pytest.raises(ActionChallengeError) as replay:
        await service.consume_challenge(
            principal,
            challenge_id=created.challenge_id,
            step_up_token=verified.step_up_token,
            action=ACTION_WORKSPACE_RUNTIME_RELEASE,
            payload=payload,
        )
    assert replay.value.status_code == 403
    assert replay.value.code == "STEP_UP_CHALLENGE_ALREADY_USED"


@pytest.mark.asyncio
async def test_wrong_payload_hash_action_and_session_are_rejected() -> None:
    service, _, clock, secret = _setup_service()
    principal_a = _principal(session_id=SESSION_A)
    principal_b = _principal(session_id=SESSION_B)
    payload = {"draft_id": str(uuid4()), "expected_revision": 1, "settings_hash": "a" * 64}
    created = await service.create_challenge(
        principal_a, action=ACTION_SETTINGS_PUBLISH, payload=payload
    )
    code = TotpManager().code_at(secret, clock())

    # Sai payload (TOCTOU WYSIWYS) -> 403
    with pytest.raises(ActionChallengeError) as bad_payload:
        await service.verify_and_consume(
            principal_a,
            challenge_id=created.challenge_id,
            action=ACTION_SETTINGS_PUBLISH,
            payload={**payload, "settings_hash": "b" * 64},
            code=code,
        )
    assert bad_payload.value.status_code == 403
    assert bad_payload.value.code == "STEP_UP_PAYLOAD_MISMATCH"

    # Sai action -> 403
    with pytest.raises(ActionChallengeError) as bad_action:
        await service.verify_and_consume(
            principal_a,
            challenge_id=created.challenge_id,
            action=ACTION_SETTINGS_ROLLBACK,
            payload=payload,
            code=code,
        )
    assert bad_action.value.status_code == 403
    assert bad_action.value.code == "STEP_UP_ACTION_MISMATCH"

    # Sai session (cùng membership nhưng khác session_id) -> 403 và không hủy challenge của A
    with pytest.raises(ActionChallengeError) as bad_session:
        await service.verify_and_consume(
            principal_b,
            challenge_id=created.challenge_id,
            action=ACTION_SETTINGS_PUBLISH,
            payload=payload,
            code=code,
        )
    assert bad_session.value.status_code == 403
    assert bad_session.value.code == "STEP_UP_SESSION_MISMATCH"

    # Session A vẫn dùng được challenge của chính mình.
    consumed = await service.verify_and_consume(
        principal_a,
        challenge_id=created.challenge_id,
        action=ACTION_SETTINGS_PUBLISH,
        payload=payload,
        code=code,
    )
    assert consumed.payload_hash == created.payload_hash


@pytest.mark.asyncio
async def test_five_wrong_codes_cancel_challenge_and_require_new_challenge() -> None:
    service, store, clock, secret = _setup_service()
    principal = _principal()
    payload = {
        "bot_id": str(uuid4()),
        "membership_id": str(OTHER_MEMBERSHIP_ID),
        "permission": "bot:runtime:resume",
    }
    created = await service.create_challenge(
        principal, action=ACTION_BOT_GRANT_WRITE, payload=payload
    )

    for attempt in range(1, MAX_ACTION_CHALLENGE_ATTEMPTS):
        with pytest.raises(ActionChallengeError) as err:
            await service.verify_and_consume(
                principal,
                challenge_id=created.challenge_id,
                action=ACTION_BOT_GRANT_WRITE,
                payload=payload,
                code="918273",
            )
        assert err.value.status_code == 403
        assert err.value.code == "STEP_UP_INVALID_CODE"
        assert err.value.remaining_attempts == MAX_ACTION_CHALLENGE_ATTEMPTS - attempt

    # Lần sai thứ 5 tự động hủy/khóa challenge.
    with pytest.raises(ActionChallengeError) as locked:
        await service.verify_and_consume(
            principal,
            challenge_id=created.challenge_id,
            action=ACTION_BOT_GRANT_WRITE,
            payload=payload,
            code="918273",
        )
    assert locked.value.status_code == 403
    assert locked.value.code == "STEP_UP_CHALLENGE_LOCKED"
    assert store.challenges[created.challenge_id].status == "CANCELLED"
    assert store.challenges[created.challenge_id].cancelled_at is not None

    # Dù nhập mã ĐÚNG ngay sau đó vẫn bị từ chối, buộc tạo challenge mới.
    with pytest.raises(ActionChallengeError) as after_lock:
        await service.verify_and_consume(
            principal,
            challenge_id=created.challenge_id,
            action=ACTION_BOT_GRANT_WRITE,
            payload=payload,
            code=TotpManager().code_at(secret, clock()),
        )
    assert after_lock.value.status_code == 403
    assert after_lock.value.code == "STEP_UP_CHALLENGE_LOCKED"

    # Tạo challenge mới với mã đúng thì thành công.
    replacement = await service.create_challenge(
        principal, action=ACTION_BOT_GRANT_WRITE, payload=payload
    )
    consumed = await service.verify_and_consume(
        principal,
        challenge_id=replacement.challenge_id,
        action=ACTION_BOT_GRANT_WRITE,
        payload=payload,
        code=TotpManager().code_at(secret, clock()),
    )
    assert consumed.action == ACTION_BOT_GRANT_WRITE

    # Audit không chứa mã TOTP hay token thô.
    joined_audits = repr(store.audits)
    assert "918273" not in joined_audits
    assert hash_token("dummy") not in joined_audits


def test_production_settings_forbid_disabling_action_step_up() -> None:
    with pytest.raises(ValueError, match="Step-up theo hành động"):
        AppSettings(app_env="production", action_step_up_required=False)
    assert AppSettings(app_env="production").is_action_step_up_required is True
    assert (
        AppSettings(app_env="test", action_step_up_required=True).is_action_step_up_required is True
    )


def test_http_challenge_endpoints_and_high_risk_enforcement() -> None:
    service, _, clock, secret = _setup_service()
    principal = _principal(roles=frozenset({Role.OWNER}))
    app = create_app(AppSettings(app_env="test", action_step_up_required=True))
    app.state.action_challenge_service = service

    class _AuthStub:
        async def create_risk_profile(self, *_args: Any, **_kwargs: Any) -> UUID:
            return uuid4()

    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    app.dependency_overrides[require_csrf] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    app.dependency_overrides[get_auth_service] = lambda: _AuthStub()

    client = TestClient(app)

    # 1. Thiếu step-up -> 403
    no_stepup = client.post(
        "/api/v1/risk-profiles",
        json={"name": "Strict", "max_risk_fraction": "0.02", "settings": {}},
    )
    assert no_stepup.status_code == 403
    assert no_stepup.json()["detail"]["code"] == "STEP_UP_REQUIRED"

    # 2. Tạo challenge qua HTTP API -> verify lấy token -> gọi thành công -> replay -> 403
    payload = risk_profile_create_payload(
        name="Strict", max_risk_fraction=Decimal("0.02"), settings={}
    )
    created = client.post(
        "/api/v1/auth/challenges",
        json={"action": ACTION_RISK_PROFILE_CREATE, "payload": payload, "ttl_seconds": 300},
    )
    assert created.status_code == 201, created.text
    challenge_id = created.json()["challenge"]["id"]
    assert created.json()["challenge"]["payload_hash"] == hash_action_payload(payload)

    verified = client.post(
        f"/api/v1/auth/challenges/{challenge_id}/verify",
        json={"code": TotpManager().code_at(secret, clock())},
    )
    assert verified.status_code == 200, verified.text
    step_up_token = verified.json()["authorization"]["step_up_token"]

    # Gửi sai payload với token vừa verify -> 403 WYSIWYS
    tampered = client.post(
        "/api/v1/risk-profiles",
        json={"name": "Strict", "max_risk_fraction": "0.20", "settings": {}},
        headers={
            "X-Step-Up-Challenge-Id": challenge_id,
            "X-Step-Up-Token": step_up_token,
        },
    )
    assert tampered.status_code == 403
    assert tampered.json()["detail"]["code"] == "STEP_UP_PAYLOAD_MISMATCH"

    # Gửi đúng payload -> 201
    ok = client.post(
        "/api/v1/risk-profiles",
        json={"name": "Strict", "max_risk_fraction": "0.02", "settings": {}},
        headers={
            "X-Step-Up-Challenge-Id": challenge_id,
            "X-Step-Up-Token": step_up_token,
        },
    )
    assert ok.status_code == 201, ok.text

    # Replay lần 2 -> 403
    replayed = client.post(
        "/api/v1/risk-profiles",
        json={"name": "Strict", "max_risk_fraction": "0.02", "settings": {}},
        headers={
            "X-Step-Up-Challenge-Id": challenge_id,
            "X-Step-Up-Token": step_up_token,
        },
    )
    assert replayed.status_code == 403
    assert replayed.json()["detail"]["code"] == "STEP_UP_CHALLENGE_ALREADY_USED"

    # 3. Direct one-step (X-Step-Up-Challenge-Id + X-Step-Up-Code) & cancel flow
    clock.advance(35)
    direct = client.post(
        "/api/v1/auth/challenges",
        json={"action": ACTION_RISK_PROFILE_CREATE, "payload": payload, "ttl_seconds": 300},
    )
    direct_id = direct.json()["challenge"]["id"]
    ok_direct = client.post(
        "/api/v1/risk-profiles",
        json={"name": "Strict", "max_risk_fraction": "0.02", "settings": {}},
        headers={
            "X-Step-Up-Challenge-Id": direct_id,
            "X-Step-Up-Code": TotpManager().code_at(secret, clock()),
        },
    )
    assert ok_direct.status_code == 201, ok_direct.text

    # Cancel challenge -> không dùng được nữa
    to_cancel = client.post(
        "/api/v1/auth/challenges",
        json={"action": ACTION_RISK_PROFILE_CREATE, "payload": payload, "ttl_seconds": 300},
    )
    cancel_id = to_cancel.json()["challenge"]["id"]
    cancelled = client.post(f"/api/v1/auth/challenges/{cancel_id}/cancel")
    assert cancelled.status_code == 204
    after_cancel = client.post(
        f"/api/v1/auth/challenges/{cancel_id}/verify",
        json={"code": TotpManager().code_at(secret, clock())},
    )
    assert after_cancel.status_code == 403
    assert after_cancel.json()["detail"]["code"] == "STEP_UP_CHALLENGE_LOCKED"

    # Malformed step-up headers -> 403 STEP_UP_INVALID_PROOF
    bad_hdr = client.post(
        "/api/v1/risk-profiles",
        json={"name": "Strict", "max_risk_fraction": "0.02", "settings": {}},
        headers={"X-Step-Up-Challenge-Id": "not-a-uuid", "X-Step-Up-Code": "123456"},
    )
    assert bad_hdr.status_code == 403
    assert bad_hdr.json()["detail"]["code"] == "STEP_UP_INVALID_PROOF"


@pytest.mark.asyncio
async def test_sect19_settings_publish_strategy_and_symbols_requires_stepup() -> None:
    """SECT-19: đổi strategy.parameters hoặc market_data.symbols không step-up -> 403."""
    from trading_bot.admin.settings_service import SettingsAdminService
    from trading_bot.config.workspace_settings import WorkspaceSettings

    service, store, clock, secret = _setup_service()
    strategy_admin = _principal(roles=frozenset({Role.STRATEGY_ADMIN}))
    app = create_app(AppSettings(app_env="test", action_step_up_required=True))
    app.state.action_challenge_service = service

    settings_v1 = WorkspaceSettings.model_validate(
        {
            "schema_version": 1,
            "runtime": {"mode": "PAPER", "timezone": "UTC"},
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT"],
                "timeframes": ["1h", "4h"],
                "max_age_seconds": 30,
            },
            "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
            "strategy": {
                "default_release": "h03-v1",
                "parameters": {"ema_fast": 12},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
            "risk": {"profile_version_id": str(uuid4())},
            "paper_execution": {
                "quote_asset": "USDT",
                "starting_equity": "10000",
                "fee_schedule_version": "paper-fees-v1",
                "fill_model": "next_bar_with_adverse_costs",
            },
        }
    )
    settings_v2 = WorkspaceSettings.model_validate(
        settings_v1.model_dump(mode="json")
        | {
            "market_data": {
                "venue": "BINANCE_USDM",
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "timeframes": ["1h", "4h"],
                "max_age_seconds": 30,
            },
            "strategy": {
                "default_release": "h03-v1",
                "parameters": {"ema_fast": 21},
                "entry_timeframe": "1h",
                "confirmation_timeframe": "4h",
            },
        }
    )
    draft_id = uuid4()
    parent_id = uuid4()
    settings_hash_v2 = hash_action_payload(settings_v2.model_dump(mode="json"))

    class _StubSettingsService(SettingsAdminService):
        def __init__(self) -> None:
            super().__init__(
                object(),  # type: ignore[arg-type]
                action_challenge_service=service,
                require_action_step_up=True,
            )

        async def publish_draft(
            self,
            principal: Principal,
            *,
            draft_id: UUID,
            expected_revision: int,
            request_id: UUID | None = None,
            step_up: Any = None,
            _verified_step_up: bool = False,
        ) -> dict[str, Any]:
            del request_id, _verified_step_up
            assert self._requires_publish_step_up(settings_v1, settings_v2) is True
            await self._verify_publish_step_up(
                principal,
                action=ACTION_SETTINGS_PUBLISH,
                payload=settings_publish_payload(
                    draft_id=draft_id,
                    expected_revision=expected_revision,
                    settings_hash=settings_hash_v2,
                ),
                risk_changed=False,
                step_up=step_up,
            )
            return {
                "id": str(draft_id),
                "parent_version_id": str(parent_id),
                "version": 2,
                "status": "PUBLISHED",
                "revision": 2,
                "settings_hash": settings_hash_v2,
            }

    app.state.settings_admin_service = _StubSettingsService()
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=strategy_admin, email="strategy@example.test"
    )
    app.dependency_overrides[require_csrf] = lambda: AuthIdentity(
        principal=strategy_admin, email="strategy@example.test"
    )

    client = TestClient(app)
    no_step = client.post(
        f"/api/v1/settings/drafts/{draft_id}/publish",
        json={"expected_revision": 1},
    )
    assert no_step.status_code == 403
    assert no_step.json()["detail"]["code"] == "STEP_UP_REQUIRED"

    # Tạo challenge gắn payload publish -> verify -> publish thành công
    payload = settings_publish_payload(
        draft_id=draft_id, expected_revision=1, settings_hash=settings_hash_v2
    )
    challenge = client.post(
        "/api/v1/auth/challenges",
        json={"action": ACTION_SETTINGS_PUBLISH, "payload": payload, "ttl_seconds": 300},
    )
    assert challenge.status_code == 201
    cid = challenge.json()["challenge"]["id"]
    ok = client.post(
        f"/api/v1/settings/drafts/{draft_id}/publish",
        json={"expected_revision": 1},
        headers={
            "X-Step-Up-Challenge-Id": cid,
            "X-Step-Up-Code": TotpManager().code_at(secret, clock()),
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["version"]["status"] == "PUBLISHED"
    assert store.challenges[UUID(cid)].status == "CONSUMED"


def test_high_risk_control_flatten_and_telegram_require_stepup_when_enforced() -> None:
    """Kiểm tra tích hợp step-up cho resume/enable/release, flatten/confirm và telegram link."""
    from trading_bot.application.bot_operations import BotOperationsService
    from trading_bot.application.bot_runtime_control import BotRuntimeControlService
    from trading_bot.application.flatten_all import FlattenAllService
    from trading_bot.application.telegram_binding import TelegramBindingService
    from trading_bot.domain.execution.flatten_all import FlattenSnapshot
    from trading_bot.domain.execution.runtime_control import (
        BotControlResult,
        BotEntryState,
        WorkspaceControlResult,
        WorkspaceKillState,
    )

    service, _, clock, secret = _setup_service()
    principal = _principal(roles=frozenset({Role.OWNER}))
    bot_id = uuid4()

    app = create_app(AppSettings(app_env="test", action_step_up_required=True))
    app.state.action_challenge_service = service
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )
    app.dependency_overrides[require_csrf] = lambda: AuthIdentity(
        principal=principal, email="owner@example.test"
    )

    class _FakeControl(BotRuntimeControlService):
        def __init__(self) -> None:
            super().__init__(
                object(),  # type: ignore[arg-type]
                action_challenge_service=service,
                require_action_step_up=True,
            )

        def for_principal(self, p: Principal) -> Any:
            svc = self

            class _Ops:
                async def resume_bot(
                    self, b_id: UUID, reason: str, expected_revision: int | None
                ) -> BotControlResult:
                    from trading_bot.auth.action_challenge import (
                        bot_runtime_resume_payload,
                        verify_service_step_up,
                    )

                    try:
                        await verify_service_step_up(
                            p,
                            action_challenge_service=svc._action_challenge_service,
                            require_action_step_up=svc._require_action_step_up,
                            action=ACTION_BOT_RUNTIME_RESUME,
                            payload=bot_runtime_resume_payload(
                                bot_id=b_id,
                                expected_revision=expected_revision or 0,
                                reason=reason,
                            ),
                        )
                    except ActionChallengeError as err:
                        from trading_bot.application.bot_runtime_control import (
                            BotRuntimeControlError,
                        )

                        raise BotRuntimeControlError(err.code) from None
                    return BotControlResult(b_id, BotEntryState.ENABLED, 2, True)

                async def release_workspace(
                    self, reason: str, expected_revision: int | None
                ) -> WorkspaceControlResult:
                    from trading_bot.auth.action_challenge import (
                        verify_service_step_up,
                        workspace_release_payload,
                    )

                    try:
                        await verify_service_step_up(
                            p,
                            action_challenge_service=svc._action_challenge_service,
                            require_action_step_up=svc._require_action_step_up,
                            action=ACTION_WORKSPACE_RUNTIME_RELEASE,
                            payload=workspace_release_payload(
                                expected_revision=expected_revision or 0, reason=reason
                            ),
                        )
                    except ActionChallengeError as err:
                        from trading_bot.application.bot_runtime_control import (
                            BotRuntimeControlError,
                        )

                        raise BotRuntimeControlError(err.code) from None
                    return WorkspaceControlResult(
                        p.workspace_id, WorkspaceKillState.ACTIVE, 2, True
                    )

            return _Ops()

    class _FakeBotOps(BotOperationsService):
        def __init__(self) -> None:
            super().__init__(
                object(),  # type: ignore[arg-type]
                control_service=_FakeControl(),
                action_challenge_service=service,
                require_action_step_up=True,
            )

        def for_principal(self, p: Principal) -> Any:
            ctrl = self._control.for_principal(p)
            parent = self

            class _P:
                async def resume_bot(
                    self, b_id: UUID, reason: str, expected_revision: int | None
                ) -> BotControlResult:
                    return await ctrl.resume_bot(b_id, reason, expected_revision)

                async def release_workspace(
                    self, reason: str, expected_revision: int | None
                ) -> WorkspaceControlResult:
                    return await ctrl.release_workspace(reason, expected_revision)

                async def enable_bot(self, b_id: UUID, reason: str) -> Any:
                    from trading_bot.application.bot_operations import BotEnableResult
                    from trading_bot.application.bot_runtime_control import (
                        BotRuntimeControlError,
                    )
                    from trading_bot.auth.action_challenge import (
                        bot_enable_payload,
                        verify_service_step_up,
                    )

                    try:
                        await verify_service_step_up(
                            p,
                            action_challenge_service=parent._action_challenge_service,
                            require_action_step_up=parent._require_action_step_up,
                            action=ACTION_BOT_ENABLE,
                            payload=bot_enable_payload(bot_id=b_id, reason=reason),
                        )
                    except ActionChallengeError as err:
                        raise BotRuntimeControlError(err.code) from None
                    return BotEnableResult(b_id, "ENABLED", True)

            return _P()

    class _FakeTgRepo:
        async def create_link_request(self, *_args: Any) -> None:
            return None

        async def revoke(self, *_args: Any) -> int:
            return 1

        async def list_bindings(self, _ws: UUID) -> list[dict[str, str]]:
            return []

        async def binding_chat(self, _ws: UUID, _mid: UUID) -> int | None:
            return None

        async def set_topics(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

    class _FakeSnapProvider:
        async def capture(self, _uow: Any) -> FlattenSnapshot:
            raise AssertionError("not used")

    class _FakeFlatten(FlattenAllService):
        def __init__(self) -> None:
            super().__init__(
                object(),  # type: ignore[arg-type]
                snapshot_provider=_FakeSnapProvider(),
                action_challenge_service=service,
                require_action_step_up=True,
            )

        def for_principal(self, p: Principal) -> Any:
            parent = self

            class _PF:
                async def confirm(
                    self,
                    *,
                    preview_id: UUID,
                    preview_hash: str,
                    idempotency_key: str,
                    reason: str,
                ) -> Any:
                    del idempotency_key
                    from trading_bot.application.flatten_all import (
                        FlattenAllError,
                        FlattenConfirmResult,
                    )
                    from trading_bot.auth.action_challenge import (
                        flatten_confirm_payload,
                        verify_service_step_up,
                    )

                    try:
                        await verify_service_step_up(
                            p,
                            action_challenge_service=parent._action_challenge_service,
                            require_action_step_up=parent._require_action_step_up,
                            action=ACTION_WORKSPACE_FLATTEN_CONFIRM,
                            payload=flatten_confirm_payload(
                                preview_id=preview_id, preview_hash=preview_hash, reason=reason
                            ),
                        )
                    except ActionChallengeError as err:
                        raise FlattenAllError(err.code) from None
                    return FlattenConfirmResult(uuid4(), "QUEUED", True)

            return _PF()

    app.state.bot_operations_service = _FakeBotOps()
    app.state.flatten_all_service = _FakeFlatten()
    app.state.telegram_binding_service = TelegramBindingService(
        _FakeTgRepo(),
        bot_username="crypidian_test_bot",
        action_challenge_service=service,
        require_action_step_up=True,
    )

    client = TestClient(app)

    # 1. Resume / Enable / Release / Flatten / Telegram link đều 403 khi thiếu step-up
    assert (
        client.post(
            f"/api/v1/bots/{bot_id}/runtime/resume",
            json={"reason": "Mở lại", "expected_revision": 1},
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/bots/{bot_id}/enable", json={"reason": "Bật bot"}).status_code == 403
    )
    assert (
        client.post(
            "/api/v1/workspace/runtime/release",
            json={"reason": "Gỡ kill", "expected_revision": 1},
        ).status_code
        == 403
    )
    preview_id = uuid4()
    preview_hash = "c" * 64
    assert (
        client.post(
            "/api/v1/workspace/flatten/confirm",
            json={
                "preview_id": str(preview_id),
                "preview_hash": preview_hash,
                "idempotency_key": "flat-1",
                "reason": "Đóng khẩn",
            },
        ).status_code
        == 403
    )
    assert (
        client.post("/api/v1/telegram-binding/link", json={"reason": "Liên kết mới"}).status_code
        == 403
    )

    # 2. Thực hiện qua challenge gắn payload cho từng hành động
    actions_and_calls: list[tuple[str, dict[str, Any], str, dict[str, Any]]] = [
        (
            ACTION_BOT_RUNTIME_RESUME,
            {"bot_id": str(bot_id), "expected_revision": 1, "reason": "Mở lại"},
            f"/api/v1/bots/{bot_id}/runtime/resume",
            {"reason": "Mở lại", "expected_revision": 1},
        ),
        (
            ACTION_BOT_ENABLE,
            {"bot_id": str(bot_id), "reason": "Bật bot"},
            f"/api/v1/bots/{bot_id}/enable",
            {"reason": "Bật bot"},
        ),
        (
            ACTION_WORKSPACE_RUNTIME_RELEASE,
            {"expected_revision": 1, "reason": "Gỡ kill"},
            "/api/v1/workspace/runtime/release",
            {"reason": "Gỡ kill", "expected_revision": 1},
        ),
        (
            ACTION_WORKSPACE_FLATTEN_CONFIRM,
            {
                "preview_id": str(preview_id),
                "preview_hash": preview_hash,
                "reason": "Đóng khẩn",
            },
            "/api/v1/workspace/flatten/confirm",
            {
                "preview_id": str(preview_id),
                "preview_hash": preview_hash,
                "idempotency_key": "flat-1",
                "reason": "Đóng khẩn",
            },
        ),
        (
            ACTION_TELEGRAM_LINK_CREATE,
            {"reason": "Liên kết mới"},
            "/api/v1/telegram-binding/link",
            {"reason": "Liên kết mới"},
        ),
    ]
    for action, challenge_payload, path, body in actions_and_calls:
        clock.advance(35)
        ch = client.post(
            "/api/v1/auth/challenges",
            json={"action": action, "payload": challenge_payload, "ttl_seconds": 300},
        )
        assert ch.status_code == 201, ch.text
        cid = ch.json()["challenge"]["id"]
        res = client.post(
            path,
            json=body,
            headers={
                "X-Step-Up-Challenge-Id": cid,
                "X-Step-Up-Code": TotpManager().code_at(secret, clock()),
            },
        )
        assert res.status_code == 200, (path, res.text)
