"""Unit tests cho CRY-34 (SC-3): Quản trị công bố/thu hồi `strategy_release` trong workspace.

Bao quát:
1. Liệt kê đầy đủ catalog từ `strategy_registry()` kèm trạng thái workspace (`PUBLISHED`,
   `RETIRED`, `UNPUBLISHED`, `HASH_MISMATCH`) và cô lập giữa các workspace.
2. Owner (kèm MFA gần đây <= 15 phút) và lý do tiếng Việt công bố / thu hồi thành công,
   lấy mã băm `content_hash` trực tiếp từ catalog và ghi `AuditEvent` trong cùng transaction.
3. Từ chối fail-closed mọi trường hợp lệch mã băm catalog (`CATALOG_HASH_MISMATCH`),
   mã release ngoài catalog (`UNKNOWN_STRATEGY_RELEASE`), sai quyền (`CONTROL_PERMISSION_DENIED`),
   thiếu/hết hạn MFA (`RECENT_MFA_REQUIRED`), thiếu lý do tiếng Việt (`CONTROL_REASON_REQUIRED`),
   và thiếu CSRF token (`403`).
4. Bảo toàn nguyên tử (atomic rollback) qua `UnitOfWork` khi thao tác thất bại giữa chừng.
5. Kiểm tra luồng Admin Web HTML (`/admin/strategy-cards`, `/admin/strategy-releases/*`,
   `/admin/bots/{bot_id}`) và Admin API JSON (`/admin/api/strategy-releases*`), bao gồm
   lọc `#overlay-strategy-release` trên bot detail chỉ giữ release `PUBLISHED` của workspace.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import trading_bot.admin.strategy_release_service as release_service_module
from trading_bot.adapters.database.models import StrategyRelease
from trading_bot.admin.strategy_card_service import StrategyCardAdminService
from trading_bot.admin.strategy_release_service import (
    StrategyReleaseAdminError,
    StrategyReleaseAdminService,
)
from trading_bot.admin.web_router import router as admin_web_router
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.auth.cookies import session_cookie_name
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.security import LoginRateLimiter
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.strategy.registry import StrategyRelease as RegisteredRelease

WORKSPACE_A = UUID("4d1e2c3b-0a9f-4e8d-9c7b-6a5f4e3d2c1b")
WORKSPACE_B = UUID("8b7a6c5d-4e3f-4210-9a8b-7c6d5e4f3a2b")
BOT_ID = UUID("6f0e5d4c-3b2a-4190-8f7e-6d5c4b3a2910")


def _make_principal(
    *,
    workspace_id: UUID = WORKSPACE_A,
    roles: frozenset[Role] = frozenset({Role.OWNER}),
    mfa_verified_at: datetime | None = None,
    with_recent_mfa: bool = True,
) -> Principal:
    effective_mfa = mfa_verified_at
    if with_recent_mfa and effective_mfa is None:
        effective_mfa = datetime.now(UTC) - timedelta(minutes=2)
    return Principal(
        user_id=UUID("11111111-1111-4111-8111-111111111111"),
        membership_id=UUID("22222222-2222-4222-8222-222222222222"),
        workspace_id=workspace_id,
        roles=roles,
        mfa_verified_at=effective_mfa,
    )


def _identity(principal: Principal, email: str = "owner@crypidian.io") -> AuthIdentity:
    return AuthIdentity(principal=principal, email=email)


class _AuthStub:
    """Stub xác thực cookie + CSRF thật cho kiểm thử route Admin Web."""

    def __init__(self, identity: AuthIdentity, csrf_token: str = "valid-csrf-token") -> None:
        self.identity = identity
        self.csrf_token = csrf_token

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        if cookie_value != "valid-session-cookie":
            raise AuthenticationFailure(401, "Chưa đăng nhập.")
        return self.identity

    async def issue_csrf_token(self, cookie_value: str | None) -> str:
        await self.authenticate(cookie_value)
        return self.csrf_token

    async def verify_csrf(self, cookie_value: str | None, token: str | None) -> None:
        await self.authenticate(cookie_value)
        if not token or token != self.csrf_token:
            raise AuthenticationFailure(403, "Mã chống giả mạo yêu cầu (CSRF) không hợp lệ.")


class _BotAdminStub:
    def __init__(self, bot_id: UUID = BOT_ID) -> None:
        self.bot_id = bot_id

    async def get_bot(self, principal: Principal, *, bot_id: UUID) -> dict[str, Any]:
        assert principal.workspace_id == WORKSPACE_A
        assert bot_id == self.bot_id
        return {
            "id": str(self.bot_id),
            "name": "Bot Kiểm Chứng SC-3",
            "status": "ENABLED",
            "symbol": "BTCUSDT",
            "account": {"mode": "PAPER", "status": "ACTIVE"},
            "versions": [
                {
                    "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "version_number": 1,
                    "status": "PUBLISHED",
                    "overlay": {"schema_version": 1},
                }
            ],
        }

    async def list_proposal_backtests(
        self, principal: Principal, *, bot_id: UUID
    ) -> list[dict[str, Any]]:
        del principal, bot_id
        return []


class _BotOpsStub:
    def for_principal(self, principal: Principal) -> _BotOpsStub:
        del principal
        return self

    async def bot_status(self, bot_id: UUID) -> dict[str, Any]:
        del bot_id
        return {
            "readiness": "READY",
            "entry_state": "IDLE",
            "entry_allowed": True,
            "control_revision": 1,
            "needs_manual_enable": False,
        }


def _build_admin_test_app(
    *,
    identity: AuthIdentity,
    release_service: StrategyReleaseAdminService,
    enforce_csrf_di: bool = True,
) -> tuple[FastAPI, _AuthStub]:
    app = FastAPI()
    app.state.settings = AppSettings(app_env="test")
    app.state.templates = Jinja2Templates(directory="src/trading_bot/templates")
    app.state.login_limiter = LoginRateLimiter()
    auth_stub = _AuthStub(identity)
    app.state.auth_service = auth_stub
    app.state.strategy_card_service = StrategyCardAdminService()
    app.state.strategy_release_service = release_service
    app.state.bot_admin_service = _BotAdminStub()
    app.state.bot_operations_service = _BotOpsStub()
    app.mount("/static", StaticFiles(directory="src/trading_bot/static"), name="static")
    app.include_router(admin_web_router)
    app.dependency_overrides[current_identity] = lambda: identity
    if not enforce_csrf_di:
        app.dependency_overrides[require_csrf] = lambda: identity
    return app, auth_stub


@pytest.mark.asyncio
async def test_list_releases_includes_full_catalog_and_workspace_statuses() -> None:
    """Liệt kê toàn bộ catalog kèm nhận diện PUBLISHED, RETIRED, UNPUBLISHED và HASH_MISMATCH."""
    catalog = strategy_registry()
    h03 = catalog.get_release("h03-v2")
    h18 = catalog.get_release("h18-v1")
    h19 = catalog.get_release("h19-v1")
    sc_release = next(r for r in catalog.list_releases() if r.release_id.startswith("sc-"))

    seed_rows = [
        StrategyRelease(
            id=uuid4(),
            workspace_id=WORKSPACE_A,
            name=h03.release_id,
            version=1,
            status="PUBLISHED",
            recipe={"release_id": h03.release_id},
            content_hash=h03.content_hash,
            created_at=datetime.now(UTC),
        ),
        StrategyRelease(
            id=uuid4(),
            workspace_id=WORKSPACE_A,
            name=h18.release_id,
            version=1,
            status="RETIRED",
            recipe={"release_id": h18.release_id},
            content_hash=h18.content_hash,
            created_at=datetime.now(UTC),
        ),
        StrategyRelease(
            id=uuid4(),
            workspace_id=WORKSPACE_A,
            name=h19.release_id,
            version=1,
            status="PUBLISHED",
            recipe={"release_id": h19.release_id},
            content_hash="0" * 64,
            created_at=datetime.now(UTC),
        ),
        # Dữ liệu thuộc workspace khác không được rò sang WORKSPACE_A
        StrategyRelease(
            id=uuid4(),
            workspace_id=WORKSPACE_B,
            name=sc_release.release_id,
            version=1,
            status="PUBLISHED",
            recipe={"release_id": sc_release.release_id},
            content_hash=sc_release.content_hash,
            created_at=datetime.now(UTC),
        ),
    ]

    service = StrategyReleaseAdminService(initial_releases=seed_rows)
    views = await service.list_releases(_make_principal(workspace_id=WORKSPACE_A))
    by_id = {v.release_id: v for v in views}

    assert len(views) == len(catalog.list_releases())
    assert by_id[h03.release_id].workspace_status == "PUBLISHED"
    assert by_id[h03.release_id].is_published is True
    assert by_id[h03.release_id].hash_matches_catalog is True

    assert by_id[h18.release_id].workspace_status == "RETIRED"
    assert by_id[h18.release_id].is_retired is True
    assert by_id[h18.release_id].is_published is False

    assert by_id[h19.release_id].workspace_status == "HASH_MISMATCH"
    assert by_id[h19.release_id].hash_matches_catalog is False
    assert by_id[h19.release_id].is_published is False

    # Release ở workspace B phải hiển thị UNPUBLISHED tại workspace A
    assert by_id[sc_release.release_id].workspace_status == "UNPUBLISHED"
    assert by_id[sc_release.release_id].content_hash == sc_release.content_hash

    # Chỉ h03-v2 đủ điều kiện PUBLISHED khớp hash cho bot tại WORKSPACE_A
    published_a = await service.list_published_release_ids(
        _make_principal(workspace_id=WORKSPACE_A)
    )
    assert published_a == (h03.release_id,)


@pytest.mark.asyncio
async def test_owner_publish_and_retire_builtin_and_card_releases_with_audit() -> None:
    """Owner có MFA công bố và thu hồi thành công cả built-in rule lẫn Strategy Card + audit."""
    catalog = strategy_registry()
    sc_release = next(r for r in catalog.list_releases() if r.release_id.startswith("sc-"))
    h03 = catalog.get_release("h03-v2")
    owner = _make_principal()
    service = StrategyReleaseAdminService()

    # Công bố rule nền h03-v2 và Strategy Card
    pub_h03 = await service.publish_release(
        owner,
        release_id=h03.release_id,
        reason="Phê duyệt công bố H03 cho chế độ PAPER sau backtest đạt ngưỡng.",
    )
    pub_sc = await service.publish_release(
        owner,
        release_id=sc_release.release_id,
        reason="Công bố Strategy Card L0-L6 đầu tiên cho workspace.",
        expected_content_hash=sc_release.content_hash,
    )

    assert pub_h03.workspace_status == "PUBLISHED"
    assert pub_h03.content_hash == h03.content_hash
    assert pub_h03.workspace_content_hash == h03.content_hash
    assert pub_sc.workspace_status == "PUBLISHED"
    assert pub_sc.kind == "strategy_card"
    assert set(await service.list_published_release_ids(owner)) == {
        h03.release_id,
        sc_release.release_id,
    }

    # Thu hồi h03-v2
    ret_h03 = await service.retire_release(
        owner,
        release_id=h03.release_id,
        reason="Tạm thu hồi H03 để chuyển sang Strategy Card mới.",
        expected_content_hash=h03.content_hash,
    )
    assert ret_h03.workspace_status == "RETIRED"
    assert ret_h03.is_retired is True
    assert ret_h03.is_published is False
    assert await service.list_published_release_ids(owner) == (sc_release.release_id,)

    # Công bố lại h03-v2 sau khi thu hồi -> chuyển lại PUBLISHED cùng version
    repub_h03 = await service.publish_release(
        owner,
        release_id=h03.release_id,
        reason="Khôi phục công bố H03 sau khi rà soát xong thông số.",
    )
    assert repub_h03.workspace_status == "PUBLISHED"
    assert repub_h03.is_published is True

    # Kiểm tra audit trail đầy đủ
    audits = service.audit_events
    assert [a.action for a in audits] == [
        "strategy_release.published",
        "strategy_release.published",
        "strategy_release.retired",
        "strategy_release.published",
    ]
    assert audits[0].target_type == "strategy_release"
    assert audits[0].before_hash is None
    assert audits[0].after_hash is not None
    assert audits[2].before_hash is not None
    assert audits[2].after_hash != audits[2].before_hash
    assert "Tạm thu hồi H03" in audits[2].reason


@pytest.mark.asyncio
async def test_rejects_mismatched_catalog_hash_and_unknown_release() -> None:
    """Từ chối mọi mã băm lệch catalog, catalog giả mạo và release không tồn tại."""
    owner = _make_principal()
    catalog = strategy_registry()
    h03 = catalog.get_release("h03-v2")
    service = StrategyReleaseAdminService()

    # 1. Mã băm client gửi lệch catalog khi công bố
    with pytest.raises(StrategyReleaseAdminError) as exc_mismatch:
        await service.publish_release(
            owner,
            release_id=h03.release_id,
            reason="Thử công bố với mã băm không khớp catalog.",
            expected_content_hash="f" * 64,
        )
    assert exc_mismatch.value.status_code == 409
    assert exc_mismatch.value.reason_code == "CATALOG_HASH_MISMATCH"
    assert service.audit_events == ()

    # 2. Mã băm rỗng cũng bị từ chối khi caller truyền expected_content_hash=""
    with pytest.raises(StrategyReleaseAdminError) as exc_empty_hash:
        await service.publish_release(
            owner,
            release_id=h03.release_id,
            reason="Thử công bố với mã băm rỗng.",
            expected_content_hash="",
        )
    assert exc_empty_hash.value.reason_code == "CATALOG_HASH_MISMATCH"

    # 3. Release không tồn tại trong catalog
    with pytest.raises(StrategyReleaseAdminError) as exc_unknown:
        await service.publish_release(
            owner,
            release_id="h99-fake-v1",
            reason="Thử công bố release không có trong catalog.",
        )
    assert exc_unknown.value.status_code == 404
    assert exc_unknown.value.reason_code == "UNKNOWN_STRATEGY_RELEASE"

    # 4. Registry bị cài hash khác catalog gốc `strategy_registry()`
    class _TamperedRegistry:
        def get_release(self, _release_id: str) -> RegisteredRelease:
            return replace(h03, content_hash="a" * 64)

    tampered_service = StrategyReleaseAdminService(registry=cast(Any, _TamperedRegistry()))
    with pytest.raises(StrategyReleaseAdminError) as exc_tampered:
        await tampered_service.publish_release(
            owner,
            release_id=h03.release_id,
            reason="Thử dùng registry lệch catalog sản phẩm chuẩn.",
        )
    assert exc_tampered.value.status_code == 409
    assert exc_tampered.value.reason_code == "CATALOG_HASH_MISMATCH"

    # 5. Công bố bản mới từ catalog sẽ tự thu hồi bản cũ bị lệch hash trong workspace
    stale_row = StrategyRelease(
        id=uuid4(),
        workspace_id=WORKSPACE_A,
        name=h03.release_id,
        version=1,
        status="PUBLISHED",
        recipe={"release_id": h03.release_id},
        content_hash="9" * 64,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    heal_service = StrategyReleaseAdminService(initial_releases=[stale_row])
    assert await heal_service.list_published_release_ids(owner) == ()
    healed = await heal_service.publish_release(
        owner,
        release_id=h03.release_id,
        reason="Công bố lại theo mã băm catalog chuẩn để thay bản cũ lệch hash.",
    )
    assert healed.workspace_status == "PUBLISHED"
    assert healed.workspace_version == 2
    assert healed.content_hash == h03.content_hash

    # 6. Từ chối thu hồi khi bản phát hành đang lệch hash ở expected_content_hash hoặc chưa công bố
    with pytest.raises(StrategyReleaseAdminError) as exc_retire_hash:
        await heal_service.retire_release(
            owner,
            release_id=h03.release_id,
            reason="Thử thu hồi kèm mã băm sai.",
            expected_content_hash="0" * 64,
        )
    assert exc_retire_hash.value.reason_code == "CATALOG_HASH_MISMATCH"

    with pytest.raises(StrategyReleaseAdminError) as exc_retire_unpub:
        await StrategyReleaseAdminService().retire_release(
            owner,
            release_id="h01-v1",
            reason="Thu hồi bản chưa công bố.",
        )
    assert exc_retire_unpub.value.status_code == 409
    assert exc_retire_unpub.value.reason_code == "RELEASE_NOT_PUBLISHED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [Role.OPERATOR, Role.STRATEGY_ADMIN, Role.RISK_ADMIN, Role.ANALYST_REVIEWER],
)
async def test_non_owner_roles_cannot_publish_or_retire_releases(role: Role) -> None:
    """Chỉ Owner mới được phép công bố/thu hồi; các vai trò khác bị từ chối 403."""
    actor = _make_principal(roles=frozenset({role}))
    service = StrategyReleaseAdminService()
    with pytest.raises(StrategyReleaseAdminError) as exc_pub:
        await service.publish_release(
            actor,
            release_id="h03-v2",
            reason="Thử công bố bằng quyền không phải Owner.",
        )
    assert exc_pub.value.status_code == 403
    assert exc_pub.value.reason_code == "CONTROL_PERMISSION_DENIED"

    with pytest.raises(StrategyReleaseAdminError) as exc_ret:
        await service.retire_release(
            actor,
            release_id="h03-v2",
            reason="Thử thu hồi bằng quyền không phải Owner.",
        )
    assert exc_ret.value.status_code == 403
    assert exc_ret.value.reason_code == "CONTROL_PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_requires_recent_mfa_and_valid_vietnamese_reason() -> None:
    """Bắt buộc MFA còn hạn (<= 15 phút) và lý do từ 3 đến 500 ký tự."""
    service = StrategyReleaseAdminService()

    # Thiếu MFA
    no_mfa = _make_principal(mfa_verified_at=None, with_recent_mfa=False)
    with pytest.raises(StrategyReleaseAdminError) as exc_no_mfa:
        await service.publish_release(no_mfa, release_id="h03-v2", reason="Công bố không có MFA.")
    assert exc_no_mfa.value.status_code == 403
    assert exc_no_mfa.value.reason_code == "RECENT_MFA_REQUIRED"

    # MFA hết hạn (> 15 phút)
    stale_mfa = _make_principal(
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=16),
        with_recent_mfa=False,
    )
    with pytest.raises(StrategyReleaseAdminError) as exc_stale_mfa:
        await service.publish_release(
            stale_mfa, release_id="h03-v2", reason="Công bố với MFA đã hết hạn."
        )
    assert exc_stale_mfa.value.status_code == 403
    assert exc_stale_mfa.value.reason_code == "RECENT_MFA_REQUIRED"

    # Lý do quá ngắn hoặc rỗng
    owner = _make_principal()
    for bad_reason in ("", "   ", "ab"):
        with pytest.raises(StrategyReleaseAdminError) as exc_reason:
            await service.publish_release(owner, release_id="h03-v2", reason=bad_reason)
        assert exc_reason.value.status_code == 422
        assert exc_reason.value.reason_code == "CONTROL_REASON_REQUIRED"


@pytest.mark.asyncio
async def test_uow_session_mode_persists_release_and_audit_in_same_transaction_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiểm tra nhánh `session_factory` dùng `UnitOfWork` ghi release + audit cùng 1 giao dịch."""

    class _FakeScalarsResult:
        def __init__(self, rows: list[StrategyRelease]) -> None:
            self._rows = rows

        def all(self) -> list[StrategyRelease]:
            return list(self._rows)

    class _FakeAsyncSession:
        def __init__(self, store: list[StrategyRelease]) -> None:
            self._store = store
            self.pending_adds: list[StrategyRelease] = []

        def add(self, obj: StrategyRelease) -> None:
            self.pending_adds.append(obj)

        async def scalars(self, _stmt: Any) -> _FakeScalarsResult:
            return _FakeScalarsResult(self._store + self.pending_adds)

    committed_rows: list[StrategyRelease] = []
    committed_audits: list[dict[str, Any]] = []
    fail_on_audit = False

    class _MemoryUnitOfWork:
        def __init__(self, _sessions: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.session = _FakeAsyncSession(committed_rows)
            self._audits: list[dict[str, Any]] = []

        async def __aenter__(self) -> _MemoryUnitOfWork:
            self._row_snapshot = [
                StrategyRelease(
                    id=r.id,
                    workspace_id=r.workspace_id,
                    name=r.name,
                    version=r.version,
                    status=r.status,
                    recipe=dict(r.recipe),
                    content_hash=r.content_hash,
                    created_at=r.created_at,
                )
                for r in committed_rows
            ]
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            del exc, tb
            if exc_type is not None:
                committed_rows[:] = self._row_snapshot
                return
            committed_rows.extend(self.session.pending_adds)
            committed_audits.extend(self._audits)

        def record_audit(self, **kwargs: Any) -> None:
            if fail_on_audit:
                raise RuntimeError("Lỗi giả lập khi ghi audit")
            self._audits.append(kwargs)

    class _FakeWorkspaceRepo:
        def __init__(self, _session: Any) -> None:
            self.locked: list[UUID] = []

        async def lock_workspace(self, workspace_id: UUID) -> None:
            self.locked.append(workspace_id)

    monkeypatch.setattr(release_service_module, "UnitOfWork", _MemoryUnitOfWork)
    monkeypatch.setattr(release_service_module, "WorkspaceSettingsRepository", _FakeWorkspaceRepo)

    owner = _make_principal()
    fake_session_factory: Any = object()
    service = StrategyReleaseAdminService(fake_session_factory)

    # Thành công -> lưu cả StrategyRelease và AuditEvent trong cùng UoW
    published = await service.publish_release(
        owner,
        release_id="h03-v2",
        reason="Kiểm tra lưu release và audit cùng transaction DB.",
    )
    assert published.workspace_status == "PUBLISHED"
    assert len(committed_rows) == 1
    assert len(committed_audits) == 1
    assert committed_audits[0]["action"] == "strategy_release.published"
    assert committed_audits[0]["target_id"] == committed_rows[0].id

    # Lỗi khi ghi audit -> rollback, không tăng số row hay audit
    fail_on_audit = True
    with pytest.raises(RuntimeError, match="Lỗi giả lập"):
        await service.publish_release(
            owner,
            release_id="h18-v1",
            reason="Thao tác phải rollback hoàn toàn khi ghi audit lỗi.",
        )
    assert len(committed_rows) == 1
    assert len(committed_audits) == 1


def test_admin_web_strategy_release_flow_csrf_hash_and_bot_selector() -> None:
    """Kiểm tra end-to-end tầng HTTP router: HTML form, CSRF, JSON API và lọc bot selector."""
    owner = _make_principal()
    identity = _identity(owner)
    release_service = StrategyReleaseAdminService(workspace_scoped=True)
    app, auth_stub = _build_admin_test_app(
        identity=identity,
        release_service=release_service,
        enforce_csrf_di=True,
    )
    settings = app.state.settings
    session_cookie = session_cookie_name(settings)

    catalog = strategy_registry()
    h03 = catalog.get_release("h03-v2")

    with TestClient(app) as client:
        client.cookies.set(session_cookie, "valid-session-cookie")

        # 1. Trang /admin/strategy-cards hiển thị tab Quản lý Phát hành và danh sách catalog
        page_resp = client.get("/admin/strategy-cards")
        assert page_resp.status_code == 200
        assert 'id="tab-releases"' in page_resp.text
        assert "Quản lý Phát hành" in page_resp.text
        assert "h03-v2" in page_resp.text

        # 2. Bot detail chỉ cho chọn release đang PUBLISHED của workspace (hiện chưa có)
        bot_resp_initial = client.get(f"/admin/bots/{BOT_ID}")
        assert bot_resp_initial.status_code == 200
        select_match = re.search(
            r'<select[^>]*id="overlay-strategy-release"[^>]*>(.*?)</select>',
            bot_resp_initial.text,
            re.DOTALL,
        )
        assert select_match is not None
        assert "h03-v2" not in select_match.group(1)
        assert "h01-v1" not in select_match.group(1)

        # 3. Form POST thiếu CSRF token bị từ chối 403
        no_csrf = client.post(
            "/admin/strategy-releases/publish",
            data={
                "csrf_token": "wrong-csrf",
                "release_id": h03.release_id,
                "content_hash": h03.content_hash,
                "reason": "Thử gửi form với CSRF sai.",
            },
            follow_redirects=False,
        )
        assert no_csrf.status_code == 403

        # 4. Form POST có hash lệch catalog bị từ chối 409 kèm cảnh báo tiếng Việt
        bad_hash = client.post(
            "/admin/strategy-releases/publish",
            data={
                "csrf_token": auth_stub.csrf_token,
                "release_id": h03.release_id,
                "content_hash": "0" * 64,
                "reason": "Thử gửi form với hash bị sửa đổi.",
            },
            follow_redirects=False,
        )
        assert bad_hash.status_code == 409
        assert "Không thể cập nhật trạng thái phát hành" in bad_hash.text
        assert 'id="tab-releases"' in bad_hash.text

        # 5. Form POST hợp lệ -> 303 redirect về #tab-releases -> PUBLISHED
        ok_pub = client.post(
            "/admin/strategy-releases/publish",
            data={
                "csrf_token": auth_stub.csrf_token,
                "release_id": h03.release_id,
                "content_hash": h03.content_hash,
                "reason": "Phát hành H03 cho workspace sau kiểm định.",
            },
            follow_redirects=False,
        )
        assert ok_pub.status_code == 303
        location = ok_pub.headers["location"]
        assert location.startswith("/admin/strategy-cards?release_status=published")
        assert location.endswith("#tab-releases")

        redirected = client.get(ok_pub.headers["location"].split("#")[0])
        assert redirected.status_code == 200
        assert "Đã công bố bản phát hành chiến lược" in redirected.text
        assert "PUBLISHED" in redirected.text

        # 6. Bot detail bây giờ hiển thị h03-v2 trong #overlay-strategy-release (không có h01-v1)
        bot_resp_after_pub = client.get(f"/admin/bots/{BOT_ID}")
        select_after = re.search(
            r'<select[^>]*id="overlay-strategy-release"[^>]*>(.*?)</select>',
            bot_resp_after_pub.text,
            re.DOTALL,
        )
        assert select_after is not None
        assert 'value="h03-v2"' in select_after.group(1)
        assert 'value="h01-v1"' not in select_after.group(1)

        # 7. API JSON liệt kê và thu hồi (kèm header X-CSRF-Token)
        api_list = client.get("/admin/api/strategy-releases")
        assert api_list.status_code == 200
        api_data = api_list.json()
        assert api_data["total"] == len(catalog.list_releases())
        h03_item = next(item for item in api_data["releases"] if item["release_id"] == "h03-v2")
        assert h03_item["workspace_status"] == "PUBLISHED"

        api_retire = client.post(
            "/admin/api/strategy-releases/retire",
            headers={"X-CSRF-Token": auth_stub.csrf_token},
            json={
                "release_id": h03.release_id,
                "content_hash": h03.content_hash,
                "reason": "Thu hồi H03 qua API để kết thúc kiểm thử.",
            },
        )
        assert api_retire.status_code == 200
        assert api_retire.json()["release"]["workspace_status"] == "RETIRED"

        # 8. Sau khi thu hồi, bot detail không còn lựa chọn h03-v2
        bot_resp_after_retire = client.get(f"/admin/bots/{BOT_ID}")
        select_retired = re.search(
            r'<select[^>]*id="overlay-strategy-release"[^>]*>(.*?)</select>',
            bot_resp_after_retire.text,
            re.DOTALL,
        )
        assert select_retired is not None
        assert 'value="h03-v2"' not in select_retired.group(1)


def test_non_owner_sees_read_only_release_panel_and_gets_403_on_api() -> None:
    """Vai trò OPERATOR chỉ xem ở chế độ chỉ đọc và bị chặn 403 trên API phát hành."""
    operator = _identity(_make_principal(roles=frozenset({Role.OPERATOR})))
    release_service = StrategyReleaseAdminService(workspace_scoped=True)
    app, auth_stub = _build_admin_test_app(
        identity=operator,
        release_service=release_service,
        enforce_csrf_di=False,
    )
    settings = app.state.settings

    with TestClient(app) as client:
        client.cookies.set(session_cookie_name(settings), "valid-session-cookie")
        page = client.get("/admin/strategy-cards")
        assert page.status_code == 200
        assert "Chế độ chỉ đọc" in page.text
        assert 'action="/admin/strategy-releases/publish"' not in page.text

        res = client.post(
            "/admin/api/strategy-releases/publish",
            headers={"X-CSRF-Token": auth_stub.csrf_token},
            json={"release_id": "h03-v2", "reason": "Thử công bố bằng Operator."},
        )
        assert res.status_code == 403
        assert res.json()["code"] == "CONTROL_PERMISSION_DENIED"
