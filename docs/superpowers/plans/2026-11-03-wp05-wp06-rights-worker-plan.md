# WP05/WP06 Rights Worker Implementation Plan

> **For agentic workers:** Follow this plan task by task. Each task is independently testable. No commit is requested in this work session.

**Goal:** Run source-policy expiry scanning and WP06 rights invalidation continuously in a workspace-scoped ingestion process.

**Architecture:** Keep expiry scanning and rights invalidation in one `ResearchRightsMaintenanceWorker`. Each tick sets the workspace GUC, obtains the database clock, enqueues expired-policy events, processes a bounded rights-event batch, and commits both operations atomically. The worker uses the existing `ingestion-worker` FastAPI lifespan; PostgreSQL grants only the columns, rows, and tables required by this consumer.

**Approaches:** (1) Poll from the existing ingestion process, (2) deploy a separate scheduler plus one-shot worker, or (3) add a database scheduler. Use (1) because Compose already has a supervised `ingestion-worker`, and the current scanner and consumer are caller-transaction services. This avoids a new scheduler dependency and makes expiry enqueue plus consumption restart-safe in one transaction. A separate scheduler would add deployment coordination, while database scheduling would couple application workflow to database extensions.

**Tech Stack:** Python 3.14, FastAPI lifespan, SQLAlchemy async sessions, PostgreSQL 18 RLS/column grants, Docker Compose.

**Spec:** `docs/superpowers/plans/2026-11-01-wp05-research-wbs.md` (WP05G.3/WP06G.2), `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md` (WP06G.2).

## Global Constraints

- One worker deployment receives one required workspace UUID; every tick binds it through transaction-local `app.workspace_id`.
- All batches are bounded; a tick failure rolls back and the next tick retries without logging event payload or secret data.
- The worker may read only rights-change outbox rows in its workspace and may update only delivery/lease/retry columns.
- The worker may append eligibility invalidations and receipts for its fixed consumer name; it cannot mutate source history, manifest payloads, or ledger rows.
- Do not change exchange APIs, provider integrations, dependencies, canonical manifest bytes, or UI behavior.
- Vietnamese comments and documentation; English identifiers.

## Review Focus

1. Missing or malformed workspace identity must stop ingestion-worker startup; other process roles must reject the setting.
2. Outbox rows of another event type or workspace must remain invisible to ingestion-worker under PostgreSQL RLS.
3. Direct identity/payload update, arbitrary receipt insert, eligibility-ledger update/delete, and broad manifest access must fail at the DB boundary.
4. A failure during a tick must roll back scanner enqueue and consumer receipt/ack together, then allow a later tick to retry.
5. Shutdown must signal and await the worker task without disposing its database engine while the task is active.

---

### Task 1: Required ingestion scope settings

**Files:** `src/trading_bot/config/settings.py`; `tests/unit/test_settings.py`.

- [x] Add RED cases for required ingestion workspace, environment loading, role rejection, and tick interval bounds.
- [x] Add `ingestion_workspace_id` and `ingestion_worker_tick_seconds` settings with role-specific validation.
- [x] Run the settings test module.

### Task 2: Transactional maintenance worker

**Files:** create `src/trading_bot/workers/research_rights_worker.py`; create `tests/unit/test_research_rights_worker.py`.

- [x] Add RED coverage for retrying a failed tick and stopping promptly.
- [x] Implement `ResearchRightsMaintenanceWorker.run_once()` and `run(stop_event)`; use DB time and one transaction per tick.
- [x] Run worker and existing source-expiry/rights-consumer unit tests.

### Task 3: Process lifespan and Compose wiring

**Files:** `src/trading_bot/app.py`; `compose.yaml`; `tests/unit/test_readiness.py`.

- [x] Add RED coverage that ingestion-worker lifespan constructs the workspace-scoped worker and stops it during shutdown.
- [x] Start/stop the background task with the existing lifespan and inject a factory for lifecycle testing.
- [x] Set Compose workspace/tick environment with a local zero-UUID default matching the existing runtime deployment pattern.

### Task 4: Least-privilege migration and PostgreSQL E2E

**Files:** create one Alembic migration after `d2f4a6c8e0b1`; update `tests/e2e/probes/wp06_rights_invalidation.py`, `tests/e2e/test_wp06_rights_invalidation.py`, and `tests/e2e/probes/db_role_least_privilege.py`.

- [x] Add focused grant/RLS assertions before migration implementation.
- [x] Grant ingestion SELECT for rights events/current workspace, UPDATE only to delivery columns, INSERT/SELECT required for consumer receipt, and SELECT/INSERT access needed for reverse-index invalidation.
- [x] Exercise a real maintenance tick under `crypidian_ingestion_worker`, including expiry enqueue, invalidation, receipt, retry, tenant isolation, and forbidden mutation.
- [x] Run the WP05 expiry, WP06 invalidation, manifest, and process-role E2E slices.

### Task 5: WBS and implementation memory

**Files:** WP05/WP06 WBS, implementation module docs, project status, implementation memory.

- [x] Record the worker's scope, transaction behavior, DB grants, test evidence, and remaining scheduler/provider/trainer/composition gaps.
- [x] Read back the new memory entry and compare it with the implementation and command output.
