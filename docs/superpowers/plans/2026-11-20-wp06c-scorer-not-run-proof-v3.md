# WP06C Scorer TEST NOT_RUN Proof v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist scorer TEST opportunities as distinct `candidate`, `no_candidate`, `input_excluded`, and `not_run` states with exact reason codes, while preserving proof v2 history.

**Architecture:** The proof builder derives all statuses and reasons from persisted opportunity rows after existing rights/lineage checks. A new migration extends the immutable proof table and validator to accept v3 without changing v2 bytes; admin reads only the v3 count after the existing current-rights recheck.

**Tech Stack:** Python 3.14, SQLAlchemy async, PostgreSQL 18/Alembic, pytest, Docker Compose, FastAPI/Jinja, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-20-wp06c-scorer-not-run-proof-v3-design.md`

## Global Constraints

- Preserve v2 canonical proof bytes, hashes, status meanings and existing reports.
- Candidate builder reads `reason_code` only from the persisted opportunity table's existing metadata access; no database grant is expanded.
- No opportunity state becomes a trade, outcome label, or synthetic PnL/class zero.
- Proof completeness is false when any input is excluded or strategy is not run; evaluation remains `INCONCLUSIVE`.
- No new entitlement, no non-QA positive-rights claim, and no `SHADOW`/`PAPER_GATE` activation.
- Vietnamese for comments/docs/UI; English identifiers; E2E affected UI in desktop/mobile.
- Update and re-read implementation memory, WBS, module, project status, and Itsaplan evidence.

## Review Focus

- A valid `EVALUATED + NOT_RUN` must retain vector lineage and reason and must not increment `input_excluded_count`.
- A true `INPUT_EXCLUDED + NOT_RUN` must remain separately counted and carry its own reason.
- A v2 proof must reproduce its exact canonical bytes/hash and remain immutable after migration.
- A v3 entry with missing reason, wrong state, or mismatched indexed count must fail in both domain validation and PostgreSQL trigger.
- A v3 `not_run` entry must retain its vector hash while carrying no candidate/outcome/sample link; input-excluded entries must not claim vector lineage.
- The builder must fail closed on an old v2 proof that cannot prove v3 state/reason parity; it must not return it as a v3 duplicate.
- Admin v3 count read must occur after rights recheck; v2 must render unknown, and both layouts must remain within the viewport.

---

### Task 1: Domain proof contract and canonical compatibility

**Files:**
- Modify: `src/trading_bot/application/learning_scorer_evaluation.py`
- Test: `tests/unit/test_learning_scorer_test_opportunity_proofs.py`
- Test: `tests/unit/test_learning_scorer_evaluation.py`

**Interfaces:**
- `ScorerTestOpportunityEntry` gains v3 `not_run` state and optional `reason_code`; v2 entries remain representable without it.
- `ScorerTestOpportunityProof` gains `proof_schema_version`, `not_run_count`, and exact version-specific canonical serialization.

- [x] Add tests for `not_run`/`input_excluded` reason validation, separate counts, and v3 completeness false.
- [x] Run focused tests and confirm RED.
- [x] Add v2/v3 domain validation; v2 canonical serialization omits all new keys and matches its golden bytes/hash.
- [x] Run the focused unit files and prove v2/v3 serialization and count invariants.

### Task 2: PostgreSQL proof schema, immutable validator, and least-privilege source access

**Files:**
- Create: `migrations/versions/f0a2c4e6b8d1_wp06_scorer_test_proof_v3.py` after current sole head `c6e8a0b2d4f9`
- Modify: `src/trading_bot/adapters/database/models/learning_scorer_test_opportunity_entities.py`
- Test: `tests/unit/test_wp06_scorer_test_proof_v3_migration.py`
- Test: `tests/unit/test_wp06_scorer_current_rights_migration.py`

**Interfaces:**
- Add nullable integer `not_run_count`; `NULL` means legacy v2 and a nonnegative integer means v3.
- Verify the existing candidate-builder SELECT permission supports `reason_code` and remains metadata-only; do not change grants.

- [x] Add migration-contract tests for v2/v3 constraints, existing metadata ACL, trigger replacement, and populated-v3 downgrade refusal.
- [x] Run the focused migration tests and confirm RED.
- [x] Implement a migration after the current sole Alembic head; keep existing migration files unchanged. Validator checks exact v2 and v3 object/entry key sets, reason rules, indexed counts and completeness.
- [x] Make downgrade reject any v3 proof row before changing validator state; when only v2 rows exist, preserve them and restore the v2 validator.
- [x] Run migration unit tests, `uv run alembic heads`, Ruff and `git diff --check`.

### Task 3: Authoritative repository projection and retry behavior

**Files:**
- Modify: `src/trading_bot/adapters/database/repositories/learning_scorer_test_opportunity_proofs.py`
- Test: `tests/unit/test_learning_scorer_test_opportunity_proofs.py`
- Test: `tests/e2e/probes/wp06_scorer_test_opportunity_proof.py`
- Test: `tests/e2e/test_wp06_scorer_test_opportunity_proof.py`

**Interfaces:**
- `_load_trial_opportunities()` selects `reason_code` through candidate-builder's existing table metadata SELECT.
- `_build_test_inventory_entries()` emits explicit `not_run` and `input_excluded` entries from authoritative `input_state`, `strategy_state`, and `reason_code`.

- [x] Add RED unit coverage for the previously conflated `EVALUATED + NOT_RUN` fixture and for true `INPUT_EXCLUDED + NOT_RUN`.
- [x] Implement separate statuses/reasons, count propagation, and fail-closed legacy-v2 duplicate handling.
- [x] Add Compose E2E through the actual database candidate-builder role: v3 trigger accepts both valid states/reasons and exact not-run vector before the intentionally unseeded attempt FK rejects; tampered state/reason/count is denied by the trigger. Source-linked Compose covers the actual builder/repository with complete persisted lineage, v2 compatibility, ACL read/write denial, and downgrade/upgrade behavior.
- [x] Run `tests/unit/test_learning_scorer_test_opportunity_proofs.py` and `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_test_opportunity_proof.py`: PostgreSQL proof E2E **1 passed/31.86s/0 skipped**; final focused suite below.

### Task 4: Admin projection and responsive display

**Files:**
- Modify: `src/trading_bot/domain/learning/scorer_report_summary.py`
- Modify: `src/trading_bot/adapters/database/repositories/learning_scorer_report_read_model.py`
- Modify: `src/trading_bot/templates/admin/learning_scoring.html`
- Test: `tests/unit/test_learning_scorer_report_read_model.py`
- Test: `tests/unit/test_learning_scorer_admin_page.py`
- Test: `tests/e2e/test_learning_scorer_admin_ui.py`

- [x] Add unit cases for v3 integer count and v2 `NULL`/unknown, preserving the current-rights-before-metadata query order.
- [x] Run affected unit tests and confirm RED.
- [x] Project only `proof.not_run_count`; render a Vietnamese count for v3 and an explicit “chưa được tách ở proof v2” message for legacy rows.
- [x] Assert mobile and desktop text, layout, and no horizontal page overflow in Playwright.
- [x] Run focused unit tests and `uv run pytest -q -rs --tb=short tests/e2e/test_learning_scorer_admin_ui.py`: **96 related units passed overall; Playwright 2 passed/5.32s/0 skipped**; generated desktop/mobile screenshots were inspected.

### Task 5: Integrated regression and traceability

**Files:**
- Modify: `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md`
- Modify: `docs/implementation/06-ai-learning.md`
- Modify: `docs/implementation/project-status.md`
- Modify: `docs/implementation/implementation-memory.md`
- Modify: `docs/superpowers/plans/2026-11-20-wp06c-scorer-not-run-proof-v3.md`
- Modify: `src/trading_bot/application/learning_evaluation_inputs.py` for version-aware proof reconstruction.
- Test: `tests/unit/test_learning_evaluation_cli.py`, including v2/v3 canonical round-trip.
- Test: `tests/unit/test_sec33_audit_hash_chain.py` and `tests/unit/test_sig0_taker_flow.py` for the current sole Alembic head and parent edge.

- [x] Run related scorer proof/access/report units and affected PostgreSQL Compose E2Es; all recorded tests have zero skips, no unresolved flakes.
- [x] Run Ruff check/format, mypy, Alembic sole-head/check in Compose, and `git diff --check`.

### Kiểm tra bổ sung sau review — NULL discriminator và migration head (20/11/2026)

- RED PostgreSQL E2E tái hiện proof có `schema_version: null`: validator cũ không phát mã canonical-invalid mà đi tiếp tới kiểm tra FK/attempt. Nguyên nhân là `NOT IN` trả SQL `NULL`, còn `IF NULL` trong PL/pgSQL không vào nhánh từ chối.
- Validator hiện so version bằng `IS DISTINCT FROM` và yêu cầu bộ khóa bắt buộc thỏa `IS NOT TRUE`; Compose xác nhận proof thiếu discriminator bị từ chối với `WP06_SCORER_TEST_PROOF_CANONICAL_INVALID`. Không đổi hash/canonical v2 hợp lệ hoặc ACL.
- Regression đơn vị proof/migration/parser **41 passed/1.10s/0 skipped**; PostgreSQL Compose `tests/e2e/test_wp06_scorer_test_opportunity_proof.py` **1 passed/28.11s/0 skipped**.
- Đồng bộ các assertion head hiện hành sang `f0a2c4e6b8d1`; giữ `c6e8a0b2d4f9` như revision lịch sử trong ca nâng/hạ có chủ đích. `uv run alembic heads` xác nhận sole head `f0a2c4e6b8d1`.
- [x] Update docs with commands, exact results, migration head, evidence source checked 20/11/2026, limits, and remaining non-QA rights/sample gate.
- [x] Re-run the full unit suite after the reviewer-requested NOT_RUN vector-schema guard and version-aware CLI parser; **6.098 passed/466.64s/0 skipped**. Update memory with the final result.

### Rerun theo lựa chọn v3 đã xác nhận — 22/11/2026

- Lần kiểm Compose ngày 20/11 dừng trước migration/probe vì PostgreSQL unhealthy sau 180 giây; không tính là pass. Rerun hiện hành đạt: `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_test_opportunity_proof.py` → **1 passed/90.15s/0 skipped**, gồm proof v3, validator PostgreSQL, ACL/migration và cleanup Compose.
- `uv run pytest -q -rs --tb=short tests/unit/test_learning_scorer_test_opportunity_proofs.py tests/unit/test_wp06_scorer_test_proof_v3_migration.py tests/unit/test_learning_evaluation_cli.py` → **41 passed/2.92s/0 skipped**.
- `uv run pytest -q -rs --tb=short tests/e2e/test_learning_scorer_admin_ui.py` → **2 passed/11.33s/0 skipped**; desktop/mobile semantic text và no-overflow assertions đạt. Screenshot hiện hành được tạo lại và xem trực quan.
- Rerun này xác nhận lựa chọn v3 migration đã được thực thi trên checkout hiện tại; không tạo rights receipt hay dữ liệu mẫu giả. CRY-313 vẫn In Progress cho tới commit gate và policy source-rights/sample-support của WP06; `SHADOW`/`PAPER_GATE` khóa.

## Status

- Task 1–5 implementation, tests, docs, and visual inspection are complete on `codex/wp06-followthrough`; CRY-313 and WP06 remain open for their external evidence/commit gates.
- Full unit suite: **6.098 passed/466.64s/0 skipped**, 4 deprecation warnings. Focused scorer/parser/migration suite: **130 passed/2.38s/0 skipped** (2 existing deprecation warnings); current proof/domain/parser subset **41 passed/2.92s/0 skipped**. PostgreSQL proof migration/ACL E2E: earlier **1 passed/31.86s**, rerun **1 passed/90.15s/0 skipped**. Source-linked Compose after vector-schema guard: **1 passed/145.35s** (3 other cases deselected). Playwright desktop/mobile earlier **2 passed/5.32s**, rerun **2 passed/11.33s/0 skipped**. Fresh QA-only Drift membership-race regression: **1 passed/22.82s/0 skipped**.
- `uv run ruff check src tests migrations scripts` passed; format-check confirmed **1,410 files already formatted**; `uv run mypy src/trading_bot` passed for **544 source files**. Alembic sole head `f0a2c4e6b8d1`, Compose Alembic check, and `git diff --check` passed. Evidence sources checked 2026-11-20: [PostgreSQL ALTER TABLE](https://www.postgresql.org/docs/18/sql-altertable.html), [GRANT](https://www.postgresql.org/docs/18/sql-grant.html), and [CREATE TRIGGER](https://www.postgresql.org/docs/18/sql-createtrigger.html).
- CRY-313 stays In Progress until the project commit gate is met. No new database ACL or non-QA entitlement was added; sample support remains below policy; `SHADOW`/`PAPER_GATE` remain locked.

### Recheck on integrated migration head d3 — 23/11/2026

- `tests/e2e/test_wp06_scorer_test_opportunity_proof.py` → **1 passed/23.56s/0 skipped** on head `f0a2c4e6b8d3`; `tests/unit/test_wp06_scorer_test_proof_v3_migration.py tests/unit/test_learning_scorer_test_opportunity_proofs.py` → **29 passed/0.78s/0 skipped**.
- `tests/e2e/test_learning_scorer_admin_ui.py` → **2 passed/4.96s/0 skipped**, desktop/mobile responsive and proof-v3 count semantics. V3 migration remains compatible with legacy proof v2; no entitlement or TEST sample count is fabricated.
