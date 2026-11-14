# WP06 PAPER Outcome Attribution and Performance Diagnostic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exact typed PAPER outcome attribution and immutable, aggregate-only Brier/log-loss diagnostics against the frozen TEST report, without changing bot state or claiming profitability.

**Architecture:** Add a nullable scoped FK from PAPER intent to `BotDecision`; compute observed score metrics inside a narrow database evaluator boundary, persist one immutable aggregate report per exact scorer target fingerprint, and expose a metadata-only Vietnamese admin view. One report never combines deployment lineages.

**Tech Stack:** Python 3.14, SQLAlchemy 2.1, PostgreSQL/Alembic, FastAPI/Jinja2, pytest/Playwright. No ML dependency.

**Spec:** `docs/superpowers/specs/2026-11-13-wp06-paper-outcome-attribution-performance-design.md`

## Global Constraints

- Join exact `learning_score_event → bot_decision → order_intent → trade` with workspace/bot/UUID/hash/candidate/scorer lineage checks.
- Legacy intent `bot_decision_id` remains NULL; JSON is never a join key; no-trade is not label 0; zero-PnL closed PAPER trade is class 0.
- Per target report uses two adjacent matured decision cohorts `[as_of−90d,as_of−60d)` and `[as_of−60d,as_of−30d)`.
- Every cohort individually requires ≥1,000 covered outcomes, ≥100 per class, and ≥80% mature coverage.
- `DEGRADED` only when both ΔBrier and Δlog-loss exceed 0.01 in both windows; all lineage/data uncertainty fails closed.
- Reports are diagnostic, selection-biased and PAPER-only. They cannot create an entry hold, bot state transition, rollback, retrain, promotion or live order.
- Admin reads report summaries only; do not grant direct broad SELECT to trade, order_intent, score-event probability arrays or raw decision payloads.
- Keep UI fully Vietnamese and responsive. Before commit run unit plus relevant Compose and Playwright; update/read back memory.

## Review Focus

- Decision without typed intent, JSON-only legacy attribution, duplicate intent/trade or workspace/bot mismatch → excluded/inconclusive.
- No trade, open trade, late close, exact zero PnL, and closed zero-PnL label → distinguished exactly.
- Cohort start/end inclusivity, maturation cutoff and time zone → deterministic boundary tests.
- Different registry/artifact/policy/report/rights/schema lineage → separate target report, never combined.
- Threshold one-sided regression, metric borderline, low class support, coverage below 0.8 or rights change → not `DEGRADED`.

---

### Task 1: Typed immutable PAPER decision attribution

**Files:**
- Modify: `src/trading_bot/adapters/database/models/paper_entities.py`
- Modify: `src/trading_bot/application/paper_lifecycle.py`
- Modify: `src/trading_bot/application/paper_runtime_application.py`
- Modify: `src/trading_bot/adapters/database/repositories/paper_ledger.py`
- Modify: `src/trading_bot/adapters/database/repositories/bot_decisions.py`
- Create: `migrations/versions/b5d7f9a1c3e6_wp06_paper_decision_attribution.py`
- Test: `tests/unit/test_trade_ledger.py`, `tests/unit/test_paper_runtime_application.py`
- Test: `tests/e2e/probes/multibot_runtime.py`, `tests/e2e/test_compose_boundary.py`

**Interfaces:**
- `OrderIntentRecord.bot_decision_id: UUID | None` with FK `(workspace_id,bot_id,bot_decision_id)` to BotDecision and unique partial constraint per non-NULL decision.
- `PaperLifecycleCoordinator.submit_approved(..., bot_decision_id: UUID | None)` passes trusted command lineage into `PaperLedgerRepository.create_intent(..., bot_decision_id=...)`.
- Retry compares typed decision ID as well as current idempotency payload; legacy flow persists NULL.

- [ ] **Step 1: Viết tests RED** for typed attribution on PaperBotDecisionCommand, NULL legacy flow, idempotency mismatch, cross-bot/workspace FK denial, one-intent-per-decision, and unchanged runtime UPDATE grants/immutable trigger.
- [ ] **Step 2: Chạy scoped unit tests** và Compose boundary case, xác nhận missing column/FK/assertion.
- [ ] **Step 3: Add nullable field, constraints, unique index, migration and lifecycle propagation**; update candidate-dispatch query to use typed field while retaining JSON only for legacy audit.
- [ ] **Step 4: Run unit tests and fresh Compose boundary/multibot E2E**; check no skipped case and active order state unchanged.

### Task 2: PAPER performance diagnostic policy and pure domain math

**Files:**
- Create: `src/trading_bot/domain/learning/paper_performance.py`
- Modify: `src/trading_bot/domain/learning/__init__.py`
- Create: `tests/unit/test_learning_paper_performance.py`
- Modify: `docs/research/glossary-vi.md`

**Interfaces:**
- `PaperPerformancePolicy` pins version/hash, 30-day cohort/maturity windows, min 1,000 outcomes, 100/class, 0.80 coverage and 0.01 metric regressions.
- `PaperPerformanceCohort` stores bounds, count/support/coverage, observed and baseline scores, membership digest, status/reasons.
- `score_multiclass_outcomes(probabilities, labels) -> (brier, log_loss)` uses class order `(-1,0,+1)`, validated normalized probabilities and deterministic epsilon clipping.
- `assess_paper_performance(cohorts, policy) -> PaperPerformanceStatus` returns `DEGRADED` only if both metrics regress in both mature cohorts; deficient input is `INCONCLUSIVE`.

- [ ] **Step 1: Write unit tests RED** for exact labels/probability validation, stable score math, class order, sample/support/coverage requirements, both-vs-one metric regression, two-cohort requirement and UTC boundary.
- [ ] **Step 2: Run test** and confirm domain contract is missing.
- [ ] **Step 3: Implement immutable policy/result DTOs** and deterministic Decimal/finite arithmetic without new dependency.
- [ ] **Step 4: Run tests GREEN**, Ruff and mypy; add EN→VI glossary for mature outcome coverage and proper scoring rule with references.

### Task 3: Aggregate-only DB evaluator, report ledger and idempotent service

**Files:**
- Create: `src/trading_bot/adapters/database/models/learning_paper_performance_entities.py`
- Modify: `src/trading_bot/adapters/database/models/__init__.py`
- Create: `src/trading_bot/adapters/database/repositories/learning_paper_performance.py`
- Create: `src/trading_bot/application/learning_paper_performance.py`
- Create: `migrations/versions/c7e9a1b3d5f8_wp06_paper_performance.py`
- Test: `tests/unit/test_learning_paper_performance_repository.py`
- Test: `tests/e2e/probes/wp06_paper_performance.py`
- Test: `tests/e2e/test_wp06_paper_performance.py`

**Interfaces:**
- SECURITY DEFINER/limited evaluator boundary receives workspace, target, and DB `as_of`; it returns only aggregate sums/counts/support/coverage and a digest over exact ordered member IDs, never row probabilities, labels or trade payloads.
- `LearningPaperPerformanceService.assess(workspace_id, target_id, idempotency_key)` resolves frozen TEST baseline/current rights and persists a canonical append-only `LearningPaperPerformanceReport`.
- Report unique identity pins `as_of`, policy hash, target fingerprint and both cohort membership digests; rights or active target change before persistence yields ineligible/inconclusive.

- [ ] **Step 1: Write tests RED** for exact joins, no/open/late/multi trade, zero PnL, mismatch/legacy exclusion, two target groups, two 30-day matured cohorts, min n/class/coverage, rights race, immutable retry/conflict and no outcome leakage from gateway.
- [ ] **Step 2: Run focused unit/Compose tests** and confirm missing evaluator/report storage.
- [ ] **Step 3: Implement limited DB aggregation and report table** with workspace RLS FORCE, append-only triggers, exact frozen report FK/hash, policy hash, membership digest and downgrade populated guard. Do not grant admin direct SELECT to source ledgers.
- [ ] **Step 4: Implement service** to resolve target fingerprint, DB clock, frozen baseline, exact cohort aggregate, pure policy assessment, idempotency and late CAS/rights verification.
- [ ] **Step 5: Run focused unit and fresh Compose report E2E**; assert admin cannot select raw score probabilities/trades and another workspace cannot read reports.

### Task 4: Admin report page and operator trigger

**Files:**
- Create: `src/trading_bot/admin/learning_paper_performance_router.py`
- Create: `src/trading_bot/admin/learning_paper_performance_schemas.py`
- Modify: `src/trading_bot/admin/web_router.py`
- Create: `src/trading_bot/templates/admin/learning_paper_performance.html`
- Modify: `src/trading_bot/static/admin.css`
- Create: `tests/e2e/test_admin_learning_paper_performance_playwright.py`
- Create: `tests/e2e/evidence/wp06-paper-performance-desktop.png`
- Create: `tests/e2e/evidence/wp06-paper-performance-mobile.png`

**Interfaces:**
- `GET /admin/learning/paper-performance` shows only workspace report summaries.
- `POST /api/v1/learning/paper-performance/assess` requires auth, role, CSRF and idempotency; returns sanitized metadata; never changes bot/deployment/hold state.
- UI explains TEST baseline, decision cohorts/maturity, n/support/coverage, both metrics and that results are not profit claims.

- [ ] **Step 1: Write Playwright/API RED assertions** for Vietnamese UI, report status, warnings, no raw payload, CSRF/idempotency/no bot-state mutation, responsive widths.
- [ ] **Step 2: Run focused Playwright** and verify route/page missing.
- [ ] **Step 3: Implement read-only dashboard and explicit diagnostic action** with no automatic side effects.
- [ ] **Step 4: Run Playwright desktop 1280×800/mobile 390×844**, inspect screenshots and assert no horizontal overflow, no skipped tests.

### Task 5: Integrated WP06 traceability and verification

**Files:**
- Modify: `docs/implementation/06-ai-learning.md`
- Modify: `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md`
- Modify: `docs/superpowers/plans/2026-11-12-wp06-open-gaps.md`
- Modify: `docs/implementation/implementation-memory.md`
- Modify: `docs/research/glossary-vi.md`

- [ ] **Step 1: Run full unit suite** and all WP06 Compose E2E affected by intent schema, score event, rights, report, Task9 and migration; 0 skips.
- [ ] **Step 2: Run desktop/mobile Playwright** and take/inspect screenshots.
- [ ] **Step 3: Run Ruff check/format, mypy, compileall, Compose config, Alembic single-head/check, and diff check.**
- [ ] **Step 4: Update WBS/module/project status** to separate completed code from external eligible data/report/active rollback-restart evidence; keep active modes locked.
- [ ] **Step 5: Record exact URLs/date/test outcomes and remaining blockers in implementation memory; read the new entry back and compare it with diff/output.**

## Trạng thái triển khai 14/11/2026

- Task 1–2: typed PAPER decision attribution, frozen policy/domain scoring và glossary đã implement; full unit regression đạt.
- Task 3: aggregate-only gateway/repository/service/report đã implement. PostgreSQL Compose kiểm ACL, role hẹp, admin summary-only và missing-active-target denial; **chưa** có positive aggregate persistence E2E vì chưa có eligible non-QA target.
- Task 4: admin tiếng Việt responsive đã kiểm Playwright desktop/mobile; screenshot `wp06-paper-performance-{desktop,mobile}.png` được lưu trong `tests/e2e/evidence/`.
- Task 5: full unit **3906 passed/0 skipped**; Playwright performance/license **5 passed**; performance gateway Compose **1 passed**; Ruff/format/mypy/Compose/Alembic/diff checks đạt. Phần integrated WP06, positive eligible performance report, active scorer rights race và rollback/restart vẫn mở; không mở `SHADOW`/`PAPER_GATE`.
