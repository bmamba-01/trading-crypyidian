# WP06F Model Card and PAPER Action Latency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a Vietnamese, responsive model card tied to one exact scorer artifact/TEST report, with rights-gated aggregate PAPER action-latency telemetry sourced from existing ledgers.

**Architecture:** The report projection remains behind its TEST rights recheck. A separate PostgreSQL `SECURITY DEFINER` aggregate gateway freezes exact PAPER_GATE decision lineage, rechecks current rights for every member, then returns only counts and percentiles. The application combines these read-only projections for a model-card page; no latency ledger, raw decision/intent view, scorer state change, or trading action is added.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL 17, Alembic, Jinja2, CSS, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-20-wp06f-model-card-action-latency-design.md`

## Global Constraints

- Use existing `SharedSignalEvidence → BotDecision → OrderIntent` timestamps and exact `LearningScoreEvent` lineage; do not add a latency-event table or write path.
- Cohort is UTC half-open `[as_of - window, as_of)` by `SharedSignalEvidence.available_at`; `as_of` is database `transaction_timestamp()`.
- PAPER latency is aggregate-only; deny the entire result if any cohort member, including one without an intent, fails the current source-rights check.
- Each stage displays p50/p95 only with at least 100 valid samples; use PostgreSQL `percentile_cont`, round to the nearest integer millisecond, and show counts below the threshold.
- `INCONCLUSIVE` hides model-quality metrics; independently authorized latency telemetry may still show with clear operational-only wording.
- Candidate without an exact intent is an inventory count, not a missed signal, loss, veto, or failure reason. Future or negative timestamp anomalies are separately counted and excluded from percentiles.
- Active scorer modes remain locked; no promotion, rollback, order, RiskEngine, or bot-state behavior changes.
- User-facing copy, comments, docstrings, and documentation are Vietnamese; identifiers and APIs are English.
- Update WBS, module notes, glossary, project status, and implementation memory; read the new memory entry back before ending the work cycle.

## Review Focus

1. An exact artifact/report mismatch could combine unrelated PAPER decisions; cover with report-ID/hash/artifact/base/policy lineage tests.
2. A source-rights failure in a no-intent member could be silently dropped from the cohort; cover with whole-request denial tests.
3. A wait for source locks could make a stale membership set appear valid; compare frozen membership after deterministic source locks and fail closed on change.
4. A timestamp at a window boundary or after `as_of` could skew counts; cover half-open boundaries, future intents, negative durations, and absent intent separately.
5. A small cohort could produce unstable percentile claims or expose raw rows; cover 99/100 support, aggregate-only schema, and direct-role ACL tests.

---

### Task 1: Exact model-card and latency projection contracts

**Files:**
- Create: `src/trading_bot/domain/learning/scorer_model_card.py`
- Modify: `src/trading_bot/adapters/database/repositories/learning_scorer_report_read_model.py`
- Modify: `src/trading_bot/application/learning_scorer_report_read_model.py`
- Create: `src/trading_bot/adapters/database/repositories/learning_scorer_model_card.py`
- Create: `tests/unit/test_learning_scorer_model_card.py`
- Modify: `tests/unit/test_learning_scorer_report_read_model.py`

**Interfaces:**
- `LearningScorerReportReadRepository.get_by_id(*, workspace_id: UUID, report_id: UUID) -> ScorerReportSummary | None` performs the same TEST current-rights recheck and strict allowlist projection as `get_latest`.
- `ScorerLatencyStageSummary` holds `stage`, `valid_sample_count`, `invalid_timestamp_count`, `p50_ms`, and `p95_ms`.
- `ScorerActionLatencySummary` holds `bot_id`, window code, `as_of`, `window_start`, candidate count, no-intent count, future-intent count, and exactly the three stage summaries.
- The repository parser accepts exactly three known stage rows and rejects missing/duplicate stages, unknown keys/status, impossible counts, non-integer percentiles, or percentiles below the 100-sample floor.

- [x] **Step 1: Write failing domain and report-by-id tests** for exact UUID validation, report rights before projection, 3 required latency stages, 99 samples hiding p50/p95, 100 samples allowing them, malformed/duplicate rows, negative timestamp counts, no-intent and future-intent separation.
- [x] **Step 2: Run tests to verify they fail** before adding the DTO/parser and exact report read method.
- [x] **Step 3: Implement the immutable DTOs, strict parser, and exact-report read method** using allowlisted fields only; preserve existing `get_latest` behavior.
- [x] **Step 4: Run focused tests**; 49 model-card/report/application/migration tests pass with no skips.
- [x] **Step 5: Run Ruff on changed files**; focused check/format pass, and full source/test/migration check is part of Task 4.

### Task 2: Aggregate-only PostgreSQL gateway and least-privilege ACL

**Files:**
- Create: `migrations/versions/f0a2c4e6b8d2_wp06_scorer_action_latency.py` (down revision `f0a2c4e6b8d1`)
- Create: `tests/e2e/test_wp06_scorer_action_latency_gateway.py`
- Create: `tests/e2e/probes/wp06_scorer_action_latency_gateway.py`
- Create: `tests/unit/test_wp06_scorer_action_latency_migration.py`
- Modify: `tests/e2e/test_learning_scorer_admin_ui.py` only if shared E2E data setup requires it

**Interfaces:**
- Create `public.wp06_aggregate_scorer_paper_action_latency(uuid,uuid,uuid,text,text,text,text,text)` for `(workspace, bot, report, artifact_hash, report_hash, base_artifact_hash, policy_hash, window_code)`; it returns exactly three rows of stage/count/percentile metadata.
- The gateway accepts only `24h`, `7d`, or `30d`; gets `as_of` from `transaction_timestamp()`; validates workspace/session/bot/report lineage; matches `PAPER_GATE + APPROVE + CANDIDATE`, exact report and hashes, and exact BotDecision hash/candidate/evidence links.
- It freezes every matching BotDecision (with or without intent), locks all current source IDs in deterministic order, compares the exact member set after the fence, invokes `wp06_authorize_paper_performance_decision_source_scope` for every member, then aggregates only the frozen set. Any denial or membership change raises and returns no partial result.
- It uses the existing no-login, no-superuser, no-BYPASSRLS evaluator role, narrowly grants evidence timestamp columns and the helper execute rights, and does not grant direct intent/decision/evidence reads to `crypidian_scorer_history` or `crypidian_admin_web`.
- Count `candidate_no_intent_count` only where no exact PAPER intent row exists. An exact intent timestamp `>= as_of` is `future_intent_count`, not a no-intent decision. Each stage counts negative/missing duration separately. Percentiles use `percentile_cont(0.5/0.95)` and integer millisecond rounding only at `valid_sample_count >= 100`.

- [x] **Step 1: Write PostgreSQL probes** for aggregate-only grant, evidence-column allowlist, scorer-history raw-source denial, evaluator role isolation, and missing-bot fail-closed behavior. Exact-cohort/source-rights denial and positive non-QA rights still need a legitimate entitled source fixture; they are not simulated here.
- [x] **Step 2: Run Compose E2E**. The first run exposed a faulty probe boolean/ACL substring check; corrected it after inspecting `proacl`, then the PostgreSQL migration/gateway E2E passed.
- [x] **Step 3: Implement the migration and function** with closed `search_path`, explicit caller/workspace/isolation checks, evaluator ownership, deterministic source locks, whole-cohort authorization, exact frozen membership, metadata-only return, revoke-by-default, and explicit grants.
- [x] **Step 4: Run migration unit tests and the available PostgreSQL ACL/fail-closed E2E**; focused unit suite and Compose E2E pass with zero skips. This does not prove current-rights denial for a seeded full cohort or positive source entitlement.
- [x] **Step 5: Check Alembic graph and SQL formatting**; sole head is `f0a2c4e6b8d2`, Ruff and diff-check pass.

### Task 3: Application read model and responsive Vietnamese model card

**Files:**
- Create: `src/trading_bot/application/learning_scorer_model_card.py`
- Create: `src/trading_bot/admin/learning_scorer_model_card_router.py`
- Create: `src/trading_bot/templates/admin/learning_scorer_model_card.html`
- Modify: `src/trading_bot/app.py`
- Modify: `src/trading_bot/admin/learning_scoring_page_router.py`
- Modify: `src/trading_bot/templates/admin/learning_scoring.html`
- Modify: `src/trading_bot/static/pages/learning.css`
- Modify: `tests/unit/test_learning_scorer_admin_page.py`
- Create: `tests/unit/test_learning_scorer_model_card_page.py`
- Modify: `tests/e2e/test_learning_scorer_admin_ui.py`
- Update screenshots: `tests/e2e/evidence/wp06-scoring-desktop.png`, `tests/e2e/evidence/wp06-scoring-mobile.png`

**Interfaces:**
- `ScorerModelCardReadModel.load_action_latency(principal, bot_id, report, window_code) -> ScorerActionLatencySummary` checks the bot's current `REPORT_READ` grant inside a workspace/bot/membership-scoped unit of work before invoking the aggregate gateway.
- Route `GET /admin/learning/model-card?report_id=<uuid>&bot_id=<uuid>&window=24h|7d|30d` rechecks TEST report rights, lists PAPER bots through the existing admin bot service, and asks the latency read model for the selected bot; no body/query field can select a workspace or hash.
- The scoring page links the exact displayed report ID to the card. The card independently reads current deployment mode/revision/pointer, labels it separately from exact historical report lineage, and never implies that a non-active report is running.

- [x] **Step 1: Write failing route/template tests and Playwright assertions** for exact report link/hash, INCONCLUSIVE hiding quality KPIs, rights failure with no partial latency, Vietnamese labels, 24h/7d/30d selection and desktop/mobile no-horizontal-overflow. A focused application test verifies that missing bot `REPORT_READ` blocks the aggregate call.
- [x] **Step 2: Run tests to verify they fail**: the first Playwright run failed because the model-card route did not exist; the deployment-state assertion also failed before that required section was added.
- [x] **Step 3: Implement the application facade, route, template and responsive CSS**; the application checks current bot `REPORT_READ`, and the page separates live deployment state from the historical report.
- [x] **Step 4: Run focused tests** with the listed unit commands and Playwright model-card/scoring pages; 49 unit tests and 3 Playwright tests pass, with screenshots regenerated.
- [x] **Step 5: Inspect both screenshots** at 1440×1000 and 375×812 after the final Vietnamese-copy update; report lineage is legible, denied latency is omitted, and neither viewport overflows horizontally.

### Task 4: WP06 records, glossary, and final verification

**Files:**
- Modify: `docs/research/glossary-vi.md`
- Modify: `docs/implementation/06-ai-learning.md`
- Modify: `docs/implementation/project-status.md`
- Modify: `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md`
- Modify: `docs/implementation/implementation-memory.md`
- Modify: `docs/superpowers/specs/2026-11-20-wp06f-model-card-action-latency-design.md`

- [x] **Step 1: Add EN→VI glossary entry** for continuous percentile with NIST and PostgreSQL sources, checked 2026-11-21.
- [x] **Step 2: Update WBS/module/project status** with exact functionality, current test evidence, remaining non-QA-rights/sample gates, and active-mode lock; WP06 and CRY-323 remain open.
- [x] **Step 3: Run focused and broad verification**: full `uv run pytest -q --tb=short tests/unit` → **6,136 passed**, 4 deprecation warnings; Playwright desktop/mobile → **3 passed**; PostgreSQL gateway → **1 passed/18.26s/0 skipped**; Ruff/format → **1,427 files**, mypy → **548 source files**, sole Alembic head `f0a2c4e6b8d2`, and `git diff --check` pass. The first full run found two stale unit assertions for the previous head; after checking the graph, unit/E2E current-head guards were updated and the full unit suite passed.
- [x] **Step 4: Update implementation memory with actual commands/results and blockers**, then reread that entry to verify it records the test failure cause, migration decision, reviewer scope, and remaining rights/sample work accurately.
- [x] **Step 5: Run independent review** of exact lineage, RLS/function ACL, rights-lock ordering, output whitelist, Vietnamese responsive UI, and active-mode invariants. No Critical/Important findings; reviewer confirmed the database structure statically. Positive non-QA aggregate rights and whole-cohort denial remain unverified because no legitimate entitled fixture is available; the available PostgreSQL E2E only proves ACL shape and missing-bot fail-closed behavior.

## Self-review

- **Coverage:** report identity and TEST-rights recheck are Task 1; PAPER aggregate and current rights are Task 2; route/page and mobile/desktop UI are Task 3; glossary, WBS, project status, memory, and integrated verification are Task 4.
- **Failure modes:** all five Review Focus entries have assigned tests in Tasks 1–3. Positive non-QA rights remain dependent on external rights material and are not replaced by QA fixtures.
- **Type consistency:** gateway returns a fixed three-stage row projection; repository parses it into `ScorerActionLatencySummary`; application takes workspace/bot/exact `ScorerReportSummary`/window; route renders that summary.
- **Proportion:** the plan follows the existing repository boundaries and does not redesign scoring, rights policy, or execution.

### Integrated head d3 rerun — 23/11/2026

`uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_action_latency_gateway.py` → **1 passed/16.15s/0 skipped**. This rechecks the existing-ledger aggregate gateway after migration head d3; it does not add a latency ledger or prove non-QA source entitlement/positive eligible cohort.

### Fail-closed projection parser follow-up — 24/11/2026

The repository now normalizes strict projection rows inside the same fail-closed boundary as aggregate parsing. Unsupported database-returned values map to `LearningScorerActionLatencyUnavailable` instead of leaking a raw `ValueError`.

- **RED:** `uv run pytest -q -rs --tb=short tests/unit/test_learning_scorer_model_card_read_model.py::test_repository_maps_unsupported_projection_values_without_leaking_type` failed as expected before the fix, exposing the raw `ValueError`.
- **GREEN:** `uv run pytest -q -rs --tb=short tests/unit/test_learning_scorer_model_card.py tests/unit/test_learning_scorer_model_card_application.py tests/unit/test_learning_scorer_model_card_read_model.py` → **18 passed/0 skipped**.
- Ruff check and format-check passed for the repository and regression test. This adapter-only change does not alter SQL, authorization, the responsive page, or trading behavior; earlier gateway/Playwright evidence remains as recorded above.
- This closes the CRY-323 projection robustness follow-up only. Non-QA data rights, positive PAPER entitlement and policy sample support remain WP06 gates; `SHADOW`/`PAPER_GATE` stay locked.
