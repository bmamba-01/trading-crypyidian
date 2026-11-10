# WP05A Source Policy Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement task by task. Track each task with the checkboxes below. Do not commit unless explicitly requested.

**Goal:** Nối policy registry WP05A vào API và admin responsive để operator quản lý candidate/revision/quota còn reviewer độc lập duyệt quyền, hoàn toàn offline.

**Architecture:** Tách `SourcePolicyAdminService` khỏi `SourceRegistryService` hiện dùng cho quota reservation để admin-web chỉ dùng quyền policy/read/observation của role admin, không dùng ingestion writer. Application service nhận Principal, xây revision append-only, dùng idempotency UUID ổn định và `PostCommitSourceObserver` qua adapter; API và trang HTML gọi cùng service. Trang server-rendered theo design system admin hiện có.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, SQLAlchemy 2.1, PostgreSQL 18, Jinja2, Bootstrap 5.3, Playwright/pytest.

**Spec:** [WP05A source policy console design](../specs/2026-11-04-wp05a-source-policy-console-design.md).

## Global Constraints

- Nội dung giao diện, hướng dẫn, comment và docstring viết bằng tiếng Việt; identifier/API dùng tiếng Anh.
- Giữ nguyên dependency/API version; không thêm thư viện.
- Mọi workspace/actor/reviewer lấy từ `AuthIdentity.principal` hoặc history DB, không từ payload.
- Write dùng `RESEARCH_SOURCE_WRITE`; approve dùng `RESEARCH_SOURCE_APPROVE`; reviewer phải khác submitter theo user và membership; mọi mutation cần CSRF và MFA gần đây tối đa 15 phút.
- Policy và observation append-only; append commit trước, observation ở transaction sau; thiếu receipt phải giữ fail-closed. Với revision sau candidate, idempotency UUID có phạm vi workspace: retry cùng key/payload phải khôi phục receipt hoặc trả lại cùng revision; cùng key payload khác phải conflict kể cả ở source khác. Candidate đầu tiên có identity riêng là source ID + normalized create payload.
- Admin-web chỉ được đọc quota ledger và ghi policy qua grant hiện có; không tạo hoặc dùng ingestion/quota writer factory trong admin-web.
- Không fetch/crawl/provider network, không lưu credential/raw body/embedding, không gửi order/Telegram, không bật LIVE.
- Admin page tiếng Việt, Be Vietnam Pro, responsive; Playwright phải chụp screenshot desktop 1280×800 và mobile 390×844.
- Không stage/commit/push/reset/clean.

## Review Focus

1. Retry sau policy commit nhưng observation thất bại: cùng idempotency key phục hồi đúng revision/receipt, payload khác bị conflict — test ở Task 1/4.
2. Self-approval kể cả cùng user khác membership và stale pending policy — test ở Task 1/4.
3. Workspace và DB role boundary: analyst chỉ approve/read, owner write, không có cross-workspace access — Compose test ở Task 4.
4. Request giả actor/approver/workspace hoặc gửi state VERIFIED trực tiếp — schema/router fail closed ở Task 3.
5. UI không lộ credential/legal-proof claim và không tràn ngang ở mobile — Playwright assertions/screenshot ở Task 3.

---

### Task 1: Policy read port, quota snapshot và application admin service

**Files:**
- Create: `src/trading_bot/application/source_policy_admin.py`
- Create: `src/trading_bot/adapters/database/source_policy_admin.py`
- Modify: `src/trading_bot/adapters/database/repositories/source_registry.py`
- Modify: `src/trading_bot/domain/research/source_registry.py` only if a quota snapshot value type is needed
- Test: `tests/unit/test_source_policy_admin.py`
- Test: `tests/unit/test_source_registry_repository.py`

**Interfaces:**
- `SourcePolicyAdminRepository.list_entries(*, workspace_id: UUID) -> tuple[SourceRegistryEntry, ...]`
- `SourcePolicyAdminRepository.get_entry(*, workspace_id: UUID, source_id: str) -> SourceRegistryEntry | None`
- `SourcePolicyAdminRepository.get_quota_snapshot(*, workspace_id: UUID, source_id: str) -> SourceQuotaSnapshot | None`
- `SourcePolicyAdminRepository.get_source_snapshot(*, workspace_id: UUID, source_id: str) -> tuple[SourceRegistryEntry, SourceQuotaSnapshot | None] | None` — history và quota trong cùng transaction/source lock.
- `SourcePolicyAdminRepository.find_revisions_by_idempotency_key(*, workspace_id: UUID, key: UUID) -> tuple[SourcePolicyRevision, ...]` — tìm revision theo key trong toàn workspace để từ chối tái dùng key cho payload/source khác; nhiều kết quả là trạng thái bất nhất và phải fail closed.
- `SourcePolicyAdminRepository.membership_id_for_user(*, workspace_id: UUID, user_id: UUID) -> UUID | None` — lookup workspace-scoped để duyệt lịch sử legacy chỉ lưu user ID.
- `SourcePolicyAdminRepository.append_revision_and_observe(*, workspace_id: UUID, revision: SourcePolicyRevision) -> SourceRegistryAppendResult`
- `SourcePolicyAdminService.for_principal(principal: Principal) -> PrincipalSourcePolicyAdminService`
- Principal facade methods: `list_sources()`, `get_source(source_id)`, `create_source(command, idempotency_key)`, `submit_pending(source_id, command, idempotency_key)`, `approve_pending(source_id, pending_policy_id, command, idempotency_key)`, `change_disabled_state(source_id, state, reason, idempotency_key)`.
- Initial source policy keeps `SourceRegistryEntry.discover()` identity; source ID plus normalized creation payload are its idempotency identity. Later policy IDs use `policy:{uuid.hex}:{payload_sha256}`. Revision keys are unique within a workspace: same key + same normalized payload on the same operation re-submits the exact revision through observer recovery; same key + changed payload or different source conflicts before append.

- [x] Write failing unit tests for WRITE/APPROVE separation, recent MFA, self-approval by same user or membership (including legacy actor ID lookup), latest-pending check, legal domain transitions, canonical idempotency, changed-payload conflict and post-commit retry.
- [x] Write failing repository tests for workspace-scoped list, read-your-history ordering, actor-user-to-membership lookup and current quota window computed from DB clock under source lock.
- [x] Write failing consistency test that history and quota are returned through one repository operation/transaction while source lock is held.
- [x] Implement bounded source list/details/quota read adapter and append-only application service; keep quota reservation unavailable in this service.
- [x] Run `PYTHONPATH=.:tests/unit .venv/bin/python -m pytest --import-mode=importlib tests/unit/test_source_policy_admin.py tests/unit/test_source_registry_application.py tests/unit/test_source_registry_repository.py -q --tb=short` — 35 passed across final fix round.
- [x] Run Ruff/format/mypy for Task 1 files and `git diff --check`; write report to `.superpowers/sdd/2026-11-04-wp05a-source-policy-console/task-1-report.md`.

### Task 2: Authenticated source-policy API and admin-web composition

**Files:**
- Create: `src/trading_bot/admin/research_source_schemas.py`
- Create: `src/trading_bot/admin/research_source_router.py`
- Modify: `src/trading_bot/app.py`
- Test: `tests/unit/test_research_source_admin_api.py`

**Interfaces:**
- `GET /api/v1/research-sources`
- `GET /api/v1/research-sources/{source_id}`
- `POST /api/v1/research-sources` — candidate `DISCOVERED`, required UUID `Idempotency-Key`.
- `POST /api/v1/research-sources/{source_id}/revisions` — only `RIGHTS_PENDING`, `BLOCKED`, or `REVOKED`; required UUID `Idempotency-Key`.
- `POST /api/v1/research-sources/{source_id}/revisions/{policy_id}/approve` — only a pending revision and required UUID `Idempotency-Key`.
- API mutations depend on `require_csrf`; reads return `Cache-Control: no-store`. Schema rejects identity/workspace fields and excludes direct `VERIFIED` input.
- `app.state.source_policy_admin_service` exists only in admin-web process role and is built with the policy-only adapter over the admin session factory.

- [x] Write failing route tests for auth, permission, CSRF, request validation, identity-field injection, no-store headers and 404/409 mapping.
- [x] Wire admin-web service without quota writer factory and mount API router only for non-worker roles.
- [x] Run the route/API unit suite and Task 1 regression; record actual outputs in `.superpowers/sdd/2026-11-04-wp05a-source-policy-console/task-2-report.md`.
- [x] Run Ruff/format/mypy and `git diff --check` for API/composition files.
- [x] Independent review and fix-round re-review approved. The default English attribution value is deferred minor in the SDD ledger.

### Task 3: Responsive Vietnamese policy console and Playwright evidence

**Files:**
- Create: `src/trading_bot/templates/admin/research_sources.html`
- Modify: `src/trading_bot/admin/research_source_router.py`
- Modify: `src/trading_bot/app.py` (mount the separate HTML router in admin-web only)
- Modify: `src/trading_bot/templates/admin/base.html`
- Modify: `src/trading_bot/static/admin.css`
- Test: `tests/e2e/test_research_source_admin_ui.py`
- Evidence: `tests/e2e/evidence/wp05a-source-policy-{mobile,desktop}.png`

- [x] Add a separate unprefixed HTML router for authenticated server-rendered list/detail/history/quota page and forms using the same application service; mount it in admin-web only. CSRF is verified before every mutation.
- [x] Hide write/approve actions according to Principal permissions while keeping backend authorization authoritative; render endpoint/evidence as plain safe text, not automatically navigable links.
- [x] Add Vietnamese status/purpose/reason labels, explicit unverified-evidence notice, no-store headers and mobile-first responsive layout using existing font/framework.
- [x] Add Playwright owner submit → reviewer approval flow at 390×844 and 1280×800; verify same-user denial, responsive width, Vietnamese labels, keyboard/44px controls and screenshot with source detail/history visible.
- [x] Run `PYTHONPATH=.:tests .venv/bin/python -m pytest --import-mode=importlib tests/e2e/test_research_source_admin_ui.py -q --tb=short`; save and inspect both screenshots. 2 passed; final full-page dimensions mobile 390×4065, desktop 1280×2978.
- [x] Run relevant admin unit regressions plus Ruff/format and `git diff --check`; write report to `.superpowers/sdd/2026-11-04-wp05a-source-policy-console/task-3-report.md`.
- [x] Independent task review approved without findings. Task 4 still owns PostgreSQL/RLS/auth-login/receipt E2E.

### Task 4: PostgreSQL end-to-end security and handoff

**Files:**
- Create: `tests/e2e/probes/wp05_source_policy_admin.py`
- Create: `tests/e2e/test_wp05_source_policy_admin.py`
- Modify: `docs/superpowers/plans/2026-11-01-wp05-research-wbs.md`
- Modify: `docs/implementation/05-research-ingestion.md`
- Modify: `docs/implementation/project-status.md`
- Modify: `docs/implementation/implementation-memory.md`

- [x] Run Compose against PostgreSQL with real `crypidian_admin_web` role: owner creates/updates policy, analyst reviewer approves, same user and foreign workspace denied, actor/approved_by preserved, observation failure/retry idempotent, quota read-only, append-only history and RLS remain intact.
- [x] Verify no schema migration is needed; run `alembic upgrade head`, `alembic check`, `alembic heads`, `docker compose config --quiet` and affected role regression.
- [x] Run full `tests/unit`, Compose policy E2E and relevant browser/API regression; record failures/flakes separately and resolve before completion.
- [x] Update WP05A WBS/status/memory with actual pass/fail/skip counts, limits, screenshot links and builder/reviewer findings; read back the new memory entry.
- [x] Run final Ruff/format/mypy, inspect screenshots, and `git diff --check`. Do not claim provider integration, legal rights verification or WP-05 completion.

Task 4 implementation/checks và review độc lập hoàn tất. Reviewer tìm Important về observer failure sau commit; đã thêm exception riêng, trả 503/no-store và hướng dẫn retry cùng key, giữ 404 cho nguồn thiếu; re-review xác nhận finding đã đóng, không có breakage mới. Bằng chứng: full unit 2698, role/registry/browser 4, API 11 trước fix; API/observer/service 41 và policy/registry Compose 2 sau fix. Xem `task-4-report.md`.

## Decision log

- Ruling: expose current quota usage as a read-only snapshot under DB clock/current policy window; do not allow reservation or quota spending from the admin page — keeps operator review separate from ingestion execution.
- Ruling: admin policy mutation uses a new policy-only application/adapter port over admin-web role; existing `SourceRegistryService` remains the connector/use path requiring an explicit ingestion quota factory — prevents accidental DB privilege sharing.
- Ruling: revision idempotency UUID and canonical payload derive the policy ID; workspace/key advisory lock rejects concurrent reuse, and stable same-key replay triggers observer recovery after an append/receipt gap — avoids duplicate policy revisions without adding schema. Candidate identity remains source ID + canonical create payload.
- Ruling: no new migration unless tests reveal a missing persistent invariant; all needed actor/reviewer/history/quota fields already exist.

Task 4 review fix: lỗi observer sau policy commit dùng exception riêng và HTTP503/no-store/retry cùng key; source/policy thiếu giữ404. Thêm source_observer/router/API-unit vào phạm vi fix theo review; schema không đổi.
