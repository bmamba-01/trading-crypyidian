# WP06 Task3H Evaluator/Trainer Rights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Allow evaluator and trainer to consume source-dependent normalized data only after current TRAINING rights are rechecked under the shared WP05 source lock, recording immutable metadata evidence for each run before payload disclosure.

**Architecture:** Add one narrowly privileged PostgreSQL `SECURITY DEFINER` gate shared by the evaluator and trainer gateways. It locks source IDs in stable order, rechecks typed manifest references against builder receipts, current policy/item state, invalidation and rights-event backlog, then appends metadata-only receipts per evaluation run or training attempt. The gate lock remains transaction-scoped through the existing receipt commit; evaluator uses `READ COMMITTED` so post-lock queries see a rights writer that committed while the gate waited. Evaluation data remains immutable after proof seal.

**Tech Stack:** Python 3.14, SQLAlchemy 2, PostgreSQL 18, Alembic, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-06-wp06-completion-design.md`, Task 3 of `docs/superpowers/plans/2026-11-06-wp06-completion.md`.

## Global Constraints

- Source-dependent manifests fail closed unless current purpose `TRAINING` is permitted.
- Reuse WP05 lock key `research-source:{workspace_id}:{source_id}` and hold it through access-receipt commit.
- Preserve payload disclosure ordering: commit the one-shot receipt before returning feature/label/outcome bytes.
- Trainer receives only the approved fold's `TRAIN` and `VALIDATION` samples; evaluator receipt binds the approved manifest and run.
- Rights evidence contains identifiers, hashes, purpose and database check time only; no source body, feature or label.
- Keep RLS, least privilege, append-only receipts and populated-table downgrade protection.
- No exchange capability, LIVE mode or new dependency.
- Vietnamese docs/comments/docstrings; identifiers in English; record official PostgreSQL 18 URLs and check date in implementation memory.

## Review Focus

- Source rights are revoked while a consumer waits for the WP05 source lock; the consumer must see the committed revocation, not a stale snapshot.
- An event is committed but its WP06 invalidation worker has not consumed it; the gate must deny access.
- An old source receipt exists but the live policy, item revision, retention, observation or takedown state no longer matches; the gate must deny access.
- A source-free v2 manifest and a source-free v3 manifest remain usable without source-rights rows.
- Direct callers, wrong workspace, legacy source lineage, duplicate/missing builder receipt or missing database metadata never disclose payload.

## Files and Boundaries

- Create `migrations/versions/f1b3d5f7a9c2_wp06_consumer_source_rights.py`: run-source receipt schema, RLS/grants/immutable trigger, shared definer gate, and evaluator/trainer gateway replacement.
- Create `src/trading_bot/adapters/database/models/learning_run_source_rights_entities.py`: immutable metadata receipt model; export it from `models/__init__.py`.
- Modify `src/trading_bot/application/learning_evaluation.py` and `learning_fold_evaluation.py`: use `READ COMMITTED` for the rights-serialized evaluator UoW and permit this isolation in its verified read adapter.
- Modify `tests/unit/test_learning_evaluation_service.py`, `test_learning_fold_evaluation.py`, `test_learning_training_batch_access.py` and `test_database_models.py` for contract checks.
- Extend `tests/e2e/probes/wp06_normalized_fold_samples.py` and `tests/e2e/test_wp06_normalized_fold_samples.py` for source-dependent evaluator/trainer receipt, lock contention, revocation, role/RLS, immutability and downgrade guard.
- Update WP06 task plan/WBS, module status, project status, glossary if a new term is introduced, and implementation memory.

## Task 1 — Add immutable per-run source rights receipts and shared gate

**Interfaces:** `wp06_assert_training_source_rights(uuid, text, jsonb, uuid, text, text, uuid) -> void`; access kind is `evaluation` or `training`. The function reads the sealed manifest itself and records one receipt for each exact source reference, keyed to evaluation run or training attempt.

- [x] Add failing model/DB-contract tests for typed per-run evidence fields, workspace-scoped uniqueness, append-only trigger expectations, and no payload columns.
- [x] Implement the model and migration with dedicated no-login/no-bypass-RLS reader role, workspace RLS policies and least-privilege grants. Admin may SELECT metadata; app, trainer and evaluator may not SELECT the receipt table directly.
- [x] Implement the definer gate: verify the authenticated gateway principal/access kind/workspace; reject malformed/legacy source lineage; acquire sorted unique WP05 source locks; require exact builder receipt membership; reject pending rights events, recorded invalidation, stale/current policy mismatch, invalid or unobserved policy, missing/unobserved or stale/tombstoned item revision, expiry, retention expiry and takedown; insert immutable per-run evidence using database clock.
- [x] Verify source-free manifests produce no rights rows while source-dependent references produce exactly one row/reference; verify helper is executable only by the two gateway owner roles and cannot be called as an app/admin principal.
- [x] Run focused unit and migration/E2E tests before proceeding.

## Task 2 — Enforce the gate at evaluator and trainer disclosure boundaries

**Consumes:** Task 1 gate and receipt table.

- [x] Add failing tests proving evaluator and trainer gateways accept current source rights, deny stale rights, and retain the lock through transaction commit.
- [x] Replace evaluator's blanket source-dependent denial with the shared gate before its one-shot receipt is inserted; pass the authoritative normalized dataset ID/fold plan hash.
- [x] Replace trainer's blanket source-dependent denial with the shared gate before the trainer-input receipt is inserted; preserve train/validation-only payload selection and one-shot behavior.
- [x] Change evaluator transaction to `READ COMMITTED`; keep outcome verification and report generation in the same transaction. Confirm trial, report and outcome rows are immutable after candidate proof seal, and update verifier isolation assertions accordingly.
- [x] Run focused unit tests and the source-dependent PostgreSQL Compose journey.

## Task 3 — Race, rights-revocation and security verification

- [x] Extend the Compose probe to open evaluator access and claim a trainer batch from a source-dependent v3 manifest while rights are current; commit, then inspect run-specific metadata receipts without exposing them to trainer/app.
- [x] Hold an evaluator/trainer source lock in a test transaction and start a WP05 policy writer; assert the writer waits until consumer receipt commits.
- [x] After policy revoke and WP05 invalidation, assert both evaluator and trainer gateways deny fresh access and append no consumer receipt or payload mutation.
- [x] Test pending rights-event denial, item takedown/retention or revision mismatch, workspace isolation, direct SELECT denial, immutable receipt trigger and populated downgrade guard.
- [x] Run focused unit tests, `tests/e2e/test_wp06_normalized_fold_samples.py`, WP05 rights outbox E2E, Ruff/format/mypy/Alembic checks and `git diff --check`; investigate every failure and rerun the final commands.

## Task 4 — Reconcile WBS and memory

- [x] Update Task3/WP06 completion plan, WBS, module status, project status and glossary if needed; keep Task3 quarantine/API/UI and WP06 Tasks7–9 explicitly open.
- [x] Append exact source URLs/date, decisions, failures/root causes, changed files and final test outputs to implementation memory; reread the new entry against code, migration and test output.
- [x] Confirm no UI changed, no secret entered the diff, and no existing staged/dirty unrelated work was staged or rewritten.
