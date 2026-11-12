# WP06 Task7B bounded observation window — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chỉ đánh giá chất lượng dữ liệu trên observation của 30 ngày gần nhất theo đồng hồ DB và lưu rõ policy/window lineage.

**Architecture:** Policy server-side được nâng từ `coverage-v1` lên `coverage-v2-30d`. Sau khi khóa rights/cohort, application lấy mốc PostgreSQL transaction time, repository lọc member theo cửa sổ 30 ngày, rồi persist assessment cùng policy duration và exact membership. Performance vẫn `INCONCLUSIVE`; không đọc label/outcome và không thay đổi runtime scoring hoặc entry behavior.

**Tech Stack:** Python 3.14, SQLAlchemy async, PostgreSQL, Alembic, FastAPI/Jinja, pytest, Playwright.

**Spec:** [bounded window design](../specs/2026-11-12-wp06-task7b-bounded-window-design.md)

## Global Constraints

- `observation_window_days=30`; `minimum_observations=3`; `minimum_snapshot_coverage=0.8`; `minimum_quality_rate=0.9`.
- Version chính sách: `coverage-v2-30d`; persisted policy duration phải bằng 30 ngày.
- Cửa sổ được tính từ PostgreSQL `transaction_timestamp()` sau source-rights lock và cohort lock.
- Truy vấn giữ workspace, registry, revision; lọc `observed_at >= as_of - 30 ngày AND observed_at <= as_of`.
- Mọi assessment ghim exact member IDs/hash; observation ngoài cửa sổ không được đọc vào cohort/assessment.
- Performance drift luôn `INCONCLUSIVE`; không dùng nhãn/outcome/PnL/holdout để suy ra hiệu năng.
- Không tự enqueue/retrain/promote, đổi scorer mode/pointer hoặc chặn entry.
- Giải thích, docs, comment và docstring bằng tiếng Việt; mã định danh bằng tiếng Anh; thuật ngữ AI mới vào glossary EN→VI kèm nguồn.

## Review Focus

- Observation đúng tại cận cửa sổ và ngay ngoài cận: regression test kiểm hai phía với DB time cố định.
- Member mới commit sau transaction timestamp: không thể lọt vào snapshot window; test truy vấn/cùng transaction khóa thứ tự.
- Retry cùng membership và policy: trả assessment cũ; khi member rời cửa sổ, digest/idempotency đổi.
- Mọi challenger `ABSTAIN`: performance `INCONCLUSIVE`, còn data-quality chỉ đánh giá coverage/quality metadata.
- Cross-workspace hoặc rights bị thu hồi: source lock/RLS hiện hành vẫn từ chối trước persistence.

---

### Task 1: Versioned bounded drift cohort

**Files:**

- Modify: `src/trading_bot/domain/learning/drift.py`
- Modify: `src/trading_bot/application/learning_drift.py`
- Modify: `src/trading_bot/adapters/database/repositories/learning_drift.py`
- Modify: `src/trading_bot/adapters/database/models/learning_shadow_drift_entities.py`
- Create: next Alembic migration after current single head; add bounded-window duration and cohort time index, preserving populated downgrade protection.
- Modify: drift admin read model/schema/template/static only as required to display the persisted 30-day policy.
- Test: `tests/unit/test_learning_drift.py`, `tests/unit/test_learning_drift_repository.py`, `tests/unit/test_learning_drift_application.py`, `tests/e2e/test_wp06_shadow_drift_persistence.py` and its probe, plus relevant Playwright unit/E2E and screenshots if UI changes.
- Modify: `docs/superpowers/plans/2026-11-08-wp06-task7-shadow-drift-plan.md`, WP06 WBS, `docs/implementation/06-ai-learning.md`, `docs/implementation/project-status.md`, `docs/research/glossary-vi.md`, and implementation memory.

**Interfaces:**

- `DriftPolicy` gains validated `observation_window_days: int`, fixed to `30` in `LearningDriftRepository.server_policy()` with version `coverage-v2-30d`.
- `LearningDriftRepository.transaction_timestamp(workspace_id)` supplies the DB `as_of`; call after `lock_cohort` and before reading members.
- `LearningDriftRepository.list_cohort(...)` accepts exact `window_start` and `window_end` UTC timestamps and returns only rows inside the closed observation interval and existing workspace/registry/revision scope.
- `LearningDriftAssessmentRecord` persists `observation_window_days=30`; migration/database checks bind that value to `coverage-v2-30d` and retain RLS/append-only/downgrade behavior.

- [x] **Step 1: Write failing tests.** Pin policy version/duration; query bounds; application order `rights → cohort lock → DB as-of → bounded read`; old member exclusion; minimum-N behavior; exact retry/member digest; migration/Compose assertions for policy duration and index; admin page shows 30-day policy without row payload.
- [x] **Step 2: Run focused tests and capture RED.** Run affected unit cases and the focused Compose test before production edits. Confirm the existing unbounded query includes the deliberately stale member or lacks the policy field.
- [x] **Step 3: Implement policy and bounds.** Use the DB time after both locks. Compute `window_start = as_of - timedelta(days=30)`, filter repository query inclusively at both observed-time bounds, and keep the existing deterministic ordering. Keep the domain's data-quality and performance status semantics separate.
- [x] **Step 4: Persist and display the exact versioned window.** Add the migration, ORM mapping, guarded downgrade, administrative metadata field and Vietnamese label. The UI receives only policy duration, bounds, counts and status.
- [x] **Step 5: Run affected tests.** Run all touched WP06 drift units, PostgreSQL Compose ACL/RLS/retry/expiry tests and responsive Playwright desktop/mobile if UI changed; require zero skips and inspect screenshots.
- [x] **Step 6: Static and database checks.** Run Ruff/format, mypy, Alembic single-head/check and `git diff --check`.
- [x] **Step 7: Update/read back traceability.** Record exact source URL/date, policy values, RED/GREEN output, migration head, behavior/limits in memory; reconcile module, drift plan, WBS, project status and glossary; read the memory entry back against `git diff` and test output.
- [x] **Step 8: Commit task.** Stage only this task’s files and commit after unit/E2E and readback succeed. Do not activate SHADOW/PAPER_GATE.

**Kiểm chứng 12/11/2026:** full unit 3558 passed/106.95s; focused drift/admin unit + Playwright 54 passed/2.91s; drift Compose 1 passed/26.33s; integrated Task9 1 passed/104.44s; 0 skipped. Ruff/format15 files, mypy6 files, sole head `c3f5a7b9d1e3` và DB-backed Alembic check đạt. Chi tiết RED/GREEN, legacy policy, index tái sử dụng và giới hạn nằm trong implementation memory ngày 12/11/2026.
