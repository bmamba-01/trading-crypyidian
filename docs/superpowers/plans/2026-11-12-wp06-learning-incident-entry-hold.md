# WP06 Learning Incident Entry Hold — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Cho phép owner/risk-admin xác nhận incident dữ liệu bằng MFA để chặn entry PAPER mới qua ledger riêng, có audit, CAS, RLS, runtime gate và UI responsive.

**Architecture:** Mỗi proposal có ledger event append-only HOLD/RELEASE riêng theo workspace. HOLD chỉ chấp nhận assessment `DETECTED` mới nhất khi mọi observation trong membership ghim cùng incumbent registry/revision/artifact/pointer và scorer deployment hiện hành khớp registry ID/revision/pointer. So sánh incumbent artifact hash với `learning_model_registry.artifact_hash`; không nhầm với scorer artifact hash. Mutation tuần tự hóa với entry bằng khóa L1; runtime chỉ đọc projection trạng thái trong transaction entry. RELEASE không phụ thuộc target còn active để hold luôn tháo được.

**Tech Stack:** Python 3.14, FastAPI/Pydantic, SQLAlchemy async, Alembic, PostgreSQL, Jinja2, vanilla JavaScript, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-12-wp06-learning-incident-entry-hold-design.md`

## Global Constraints

- Comment, docstring, hướng dẫn và giao diện bằng tiếng Việt; identifiers/API bằng tiếng Anh.
- Chỉ OWNER và RISK_ADMIN được HOLD/RELEASE; yêu cầu MFA TOTP trong `RECENT_MFA_LIFETIME`, CSRF, reason 1–500 ký tự, idempotency key và expected revision.
- Ledger chỉ chứa metadata; RLS/FORCE RLS theo workspace; event bất biến, không cấp write cho trainer/evaluator/observer/runtime.
- Chỉ latest `DETECTED` assessment về quality/coverage có thể tạo HOLD; không dùng outcome/PnL, QA fixture hoặc source không có quyền làm deployment evidence.
- HOLD chỉ chặn PAPER `NEW_ENTRY`; không pause bot/workspace, không flatten, không hủy protective order. BotDecision/score-event đã ghi trước giao dịch entry vẫn được giữ nguyên.
- `SHADOW`/`PAPER_GATE` tiếp tục khóa; không gọi provider, mua dữ liệu hoặc gửi lệnh sàn.
- Chia migration sau revision `f4a6c8e0b2d5` (bản sửa scorer-rights projection đang được implement); khi branch revision thực tế khác, kiểm `alembic heads` và đặt `down_revision` vào head duy nhất.
- Không commit cho tới khi unit, PostgreSQL Compose và Playwright desktop/mobile liên quan đều đạt; commit một lần sau integrated review.

## Review Focus

1. Assessment chứa observation từ nhiều incumbent/pointer khác nhau → không hiện action và mutation trả conflict, không ghi event.
2. Scorer deployment hoặc pointer đổi giữa lúc xem trang và xác nhận → CAS phát hiện dưới khóa, không ghi HOLD.
3. Retry cùng idempotency key/payload sau khi target đã đổi → trả event gốc; dùng key với payload khác → conflict.
4. HOLD và entry cạnh tranh tại khóa L1 → chỉ một thứ tự commit hợp lệ; entry sau HOLD không tạo execution decision/order/reservation/trade.
5. Risk-admin xem được metadata incident qua quyền riêng; analyst/operator không được quản lý hold; assessment `NO_DRIFT`/`INCONCLUSIVE` không có action.
6. RELEASE sau đổi scorer target vẫn thành công; release một proposal không xóa hold đang mở khác.

## Files and Ownership

- `src/trading_bot/domain/learning/incident_entry_hold.py`: action, immutable event, canonical payload/hash và state transition.
- `src/trading_bot/auth/policy.py`: permission `LEARNING_INCIDENT_MANAGE` chỉ cấp cho OWNER/RISK_ADMIN ngoài OWNER's all-permissions.
- `src/trading_bot/adapters/database/models/learning_incident_entry_hold_entities.py`: ORM event entity; export từ `models/__init__.py`.
- `migrations/versions/<new>_wp06_learning_incident_entry_hold.py`: append-only table, RLS, trigger, ACL và runtime status function.
- `src/trading_bot/adapters/database/repositories/learning_incident_entry_holds.py`: append/history/runtime projection, locking và CAS queries.
- `src/trading_bot/application/learning_incident_entry_hold.py`: Principal-bound owner/risk-admin command/query service, MFA/reason/idempotency và audit UoW.
- `src/trading_bot/application/paper_runtime_application.py`, `src/trading_bot/domain/execution/runtime_control.py`: same-transaction `NEW_ENTRY` gate.
- `src/trading_bot/admin/learning_incident_entry_hold_{schemas,router}.py`: strict HTTP schema and endpoints; mount in `app.py`.
- `src/trading_bot/admin/learning_observations_page_router.py`, `src/trading_bot/application/learning_observations_admin.py`, `src/trading_bot/adapters/database/repositories/learning_observations_admin.py`: metadata eligibility/actions for the existing page.
- `src/trading_bot/templates/admin/learning_observations.html`, `src/trading_bot/static/learning-observations.css`: responsive Vietnamese form/history.
- `tests/unit/test_learning_incident_entry_hold.py`, `tests/unit/test_learning_incident_entry_hold_admin.py`, `tests/unit/test_paper_runtime_application.py`.
- `tests/e2e/test_wp06_learning_incident_entry_hold_schema.py`, `tests/e2e/probes/wp06_learning_incident_entry_hold_schema.py`, `tests/e2e/test_wp06_learning_incident_entry_hold_runtime.py`, `tests/e2e/probes/wp06_learning_incident_entry_hold_runtime.py`, `tests/e2e/test_wp06_learning_incident_entry_hold_admin.py`, `tests/e2e/probes/wp06_learning_incident_entry_hold_admin.py`, `tests/e2e/test_learning_incident_entry_hold_admin_ui.py`.

### Task 1: Domain contract and permissions

**Interfaces:** `LearningIncidentHoldAction` has `HOLD` and `RELEASE`; `LearningIncidentHoldEvent` validates event/proposal/assessment IDs and hashes, incumbent target tuple, sequence/predecessor, actor membership, MFA metadata, reason and idempotency. `create_hold_event(*, event_id, workspace_id, proposal, assessment, target: ScorerDeploymentSnapshot, incumbent_artifact_hash, revision, previous_event, actor_membership_id, mfa_verified_at, mfa_method, reason, idempotency_key, request_id, created_at) -> LearningIncidentHoldEvent` permits only `NONE→HOLD`; `create_release_event(*, event_id, previous_event, actor_membership_id, mfa_verified_at, mfa_method, reason, idempotency_key, request_id, created_at) -> LearningIncidentHoldEvent` permits only `HOLD→RELEASE`, terminal for that proposal.

- [x] Add unit tests for valid transitions, release terminality, cross-workspace IDs, invalid/future MFA timestamps, malformed hashes, whitespace/overlength reasons, idempotency payload hash and predecessor/revision mismatch; verify each fails first.
- [x] Implement the domain module and export it from the learning package.
- [x] Add `LEARNING_INCIDENT_MANAGE`; assert only OWNER/RISK_ADMIN authorize and existing role permissions do not change.
- [x] Run `uv run pytest tests/unit/test_learning_incident_entry_hold.py tests/unit/test_auth_security.py -q --tb=short`.

### Task 2: PostgreSQL ledger, repository and runtime projection

**Interfaces:** Repository methods `append_event`, `list_history`, `active_hold_exists`, `lock_runtime_entry_scope`, `lock_hold_governance_scope`, and `eligible_incident_target` bind `workspace_id` from the authenticated/runtime context. Runtime calls a boolean/reason projection; no application role reads the event table directly.

- [x] Add `tests/unit/test_learning_incident_entry_hold_migration.py` assertions for schema, forced RLS, immutable trigger, role grants, revision/predecessor uniqueness, idempotency uniqueness, one terminal RELEASE and runtime projection grants.
- [x] Add ORM metadata/entity and register it in model metadata.
- [x] Implement migration after the actual single current Alembic head. HOLD membership validation must require every observation to share incumbent registry ID/revision/artifact hash/pointer version; compare the hash to `learning_model_registry.artifact_hash`, while matching registry ID/revision/pointer against current scorer deployment under ordered locks. Never equate `ScorerDeploymentTarget.scorer_artifact_hash` with the incumbent model artifact hash.
- [x] Search `c3f5a7b9d1e3` references and update only assertions that mean “current Alembic head”; retain tests that intentionally refer to the historical parent revision.
- [x] Implement append/history/runtime projection repository statements. Use the existing workspace-settings L1 lock before deployment and registry locks for HOLD/RELEASE; do not change the global runtime lock order.
- [x] Add `tests/e2e/probes/wp06_learning_incident_entry_hold_schema.py` and `tests/e2e/test_wp06_learning_incident_entry_hold_schema.py` for RLS/cross-workspace denial, direct mutation denial, event chain, idempotency/CAS, target mismatch, release terminality, runtime-only projection access, atomic audit rollback and populated downgrade refusal.
- [x] Run migration/model unit tests and `uv run pytest tests/e2e/test_wp06_learning_incident_entry_hold_schema.py -q --tb=short`.

### Task 3: Principal-bound service and admin API

**Interfaces:** `LearningIncidentEntryHoldService.for_principal(principal)` returns commands `hold(proposal_id, assessment_id, assessment_hash, expected_revision, expected_deployment_revision, expected_active_pointer_version, idempotency_key, reason)` and `release(proposal_id, expected_revision, idempotency_key, reason)`, plus metadata-only history. Actor/workspace come only from `Principal`; mutation appends event and audit in one write UoW. API routes are `GET /api/v1/learning/incident-entry-holds`, `POST /api/v1/learning/incident-entry-holds/{proposal_id}/hold`, and `POST /api/v1/learning/incident-entry-holds/{proposal_id}/release`; mutations take `Idempotency-Key` header and `require_csrf` identity.

- [x] Add failing unit/HTTP tests for allowed OWNER/RISK_ADMIN, denied roles, recent MFA, reason validation, CSRF, strict body, no identity injection, latest DETECTED eligibility, target revision/CAS, exact retry and conflicting idempotency key.
- [x] Implement the service, router, schemas and application composition.
- [x] Ensure retry lookup precedes stale target/CAS checks and RELEASE remains available after target changes.
- [x] Add `tests/e2e/test_wp06_learning_incident_entry_hold_admin.py` and its Compose probe; verify authenticated role/MFA/CSRF, current-target rejection, payload allowlist, release retry/conflict and audit.
- [x] Run focused service/HTTP unit tests and `uv run pytest tests/e2e/test_wp06_learning_incident_entry_hold_admin.py -q --tb=short`.

### Task 4: PAPER runtime entry gate

**Interfaces:** `EntryGateInput.learning_incident_hold_active: bool` and `EntryGateState.learning_incident_hold_active: bool` are required fields with no fail-open default; denied `EntryGateDecision.reason` is `EntryBlockReason.LEARNING_INCIDENT_ENTRY_HOLD`. Runtime reader uses the workspace-scoped projection inside `evaluate_and_submit` after L1/fence/control checks and before planner/risk/reservation/writes. Projection failure raises `PaperRuntimeApplicationError("LEARNING_INCIDENT_HOLD_UNAVAILABLE")`.

- [x] Add tests: active hold blocks with exact reason and no entry-transaction writes; another workspace remains clear; no hold and protective exit/monitoring proceed; all holds need release; restart retains hold; governance and entry serialize on L1.
- [x] Implement the runtime state reader and stable reason code without routing through workspace pause/kill control.
- [x] Verify the gate runs before planner/risk/reservation/submission and does not change dispatcher score-event writes; assert zero writes for entry-transaction decision/order/reservation/trade.
- [x] Add `tests/e2e/test_wp06_learning_incident_entry_hold_runtime.py` and its probe for runtime-role projection, L1 serialization, restart, workspace isolation and multiple holds; unit tests verify protective exit/monitoring remain permitted.
- [x] Run focused PAPER runtime units and `uv run pytest tests/e2e/test_wp06_learning_incident_entry_hold_runtime.py -q --tb=short`.

### Task 5: Responsive incident UI

- [x] Extend page metadata projection to include full active-hold status, history and actionable latest eligible proposals without exposing observation payloads.
- [x] Add CSRF-protected hold/release forms with fresh MFA, reason, idempotency key and expected revision; show conflict/permission messages in Vietnamese.
- [x] Add desktop/mobile Playwright cases for owner and risk-admin, empty/noneligible state, role denial, hold/release/history, no horizontal overflow and screenshot evidence at 1280px and 390px.
- [x] Run `uv run pytest tests/e2e/test_learning_incident_entry_hold_admin_ui.py -q --tb=short`; inspect generated desktop/mobile screenshots.

### Task 6: Integration and WP06 traceability

- [x] Run the full affected WP06 unit set, dedicated Compose E2E and Playwright UI tests with zero skips; rerun failures to identify and fix root causes.
- [x] Run Ruff, format, mypy, `uv run alembic heads`, DB-backed `alembic check`, Compose config and `git diff --check`.
- [x] Update WP06 WBS, module, project-status, glossary and implementation-memory. Read back the new memory entry and compare every claim with test output.
- [x] Keep WP06 OPEN: this entry-hold bridge does not provide licensed non-QA active scorer evidence or complete performance/outcome attribution.
- [x] Resolve independent review findings for MFA freshness after lock waits and Unicode whitespace validation in database CHECK, trigger and ORM; verify tab-only `reason` and `request_id` are rejected by PostgreSQL.
- [x] Complete read-only independent review of the entry-hold permission/RLS/runtime/ledger paths; no remaining findings. Positive rights-bearing scorer journey and true long-lock MFA wait remain outside current synthetic fixtures.
- [x] Integrate the latest `origin/dev`, rerun affected verification, stage only intended files, commit and push to the authorized user repository `origin/dev` as a verified fast-forward.
