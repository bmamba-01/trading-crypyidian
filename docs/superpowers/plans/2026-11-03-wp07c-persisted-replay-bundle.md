# WP-07C Persisted Replay Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist reusable multi-timeframe replay bundle references and immutable per-run rights evidence, while revalidating current rights and bars whenever a bundle is replayed.

**Architecture:** Add a canonical, workspace-scoped bundle reference and immutable PostgreSQL bundle repository. A loader resolves each child manifest through the existing rights-and-bars replay loader. Backtest outcome persistence accepts the replayed bundle and appends one canonical rights receipt per child manifest atomically with the outcome; old runs remain without retroactive evidence.

**Tech Stack:** Python 3.12, SQLAlchemy async, PostgreSQL JSONB/RLS, Alembic, pytest, Ruff, mypy, Docker Compose E2E.

**Spec:** [WP-07C persisted replay bundle design](../specs/2026-11-03-wp07c-persisted-replay-bundle-design.md)

## Global Constraints

- Bundle chỉ giữ tham chiếu/hash các manifest con; không sao chép OHLCV.
- Khi nạp để replay, hệ thống phải dùng `BacktestDatasetReplayLoader` hiện có để kiểm quyền hiện hành và kiểm lại bars.
- Bằng chứng lịch sử chỉ phục vụ audit, không thay thế kiểm quyền hiện hành và không tự cấp quyền replay.
- Không lưu bars/raw payload; bundle giữ policy fingerprint ổn định.
- Không tạo evidence hồi tố cho run cũ.
- Chưa cấp quyền `crypidian_trainer_worker` đọc dataset manifest/bundle hoặc Source Registry.
- Không tải market data thật, không gửi Telegram, không phát lệnh sàn và không tuyên bố profitability.
- Giải thích, hướng dẫn, comment và docstring viết bằng tiếng Việt; identifier và API viết bằng tiếng Anh.
- Thuật ngữ trading/AI mới phải có định nghĩa EN→VI và nguồn trong `docs/research/glossary-vi.md`.

## Review Focus

- Cùng hash nhưng canonical payload khác hoặc JSONB/cột chỉ mục bị sửa phải fail-closed — kiểm trong bundle record/repository tests.
- Workspace khác không được đọc/ghi bundle hoặc rights receipt — kiểm repository tests và Compose RLS test.
- Policy đổi, revoke hoặc lease hết hạn sau bundle persist phải chặn replay — kiểm loader unit và Compose rights E2E.
- Child manifest khác symbol/window/policy, thiếu bars, sai bars hash hoặc timeframe trùng phải chặn load — kiểm bundle-loader unit tests.
- Retry run mới phải giữ nguyên receipt đầu tiên; legacy run thiếu receipt không được bổ sung hồi tố — kiểm outcome repository unit/E2E.

---

## File Structure

- Modify `src/trading_bot/application/backtest_dataset_replay_bundle.py`: canonical bundle reference/component, hash, parse and validation contract.
- Modify `src/trading_bot/adapters/database/models/backtest_entities.py` and `src/trading_bot/adapters/database/models/__init__.py`: immutable bundle ORM record.
- Modify `src/trading_bot/adapters/database/models/backtest_outcome_entities.py`: immutable rights receipt ORM record.
- Create `migrations/versions/e7c9a1b3d5f2_wp07c_persisted_replay_bundle.py`: both append-only tables, constraints, RLS, grants and guarded downgrade.
- Create `src/trading_bot/adapters/database/repositories/backtest_dataset_replay_bundles.py`: append/read bundle reference with canonical and workspace validation.
- Create `src/trading_bot/application/backtest_dataset_replay_bundle_loader.py`: resolve a stored bundle through the current child replay loader.
- Modify `src/trading_bot/adapters/database/repositories/backtest_outcomes.py`: persist and verify per-run child rights receipts in the outcome transaction.
- Create `tests/unit/test_backtest_dataset_replay_bundle_persistence.py`: reference, bundle record/repository, loader unit coverage.
- Modify `tests/unit/test_backtest_outcomes_repository.py`: receipt canonicalization, idempotency and legacy behavior.
- Modify `tests/e2e/test_backtest_outcomes_persistence.py`: actual worker-role receipt persistence, retry immutability, and workspace isolation.
- Modify `tests/e2e/test_wp07_rest_backfill_persistence.py` and `tests/e2e/probes/wp07_backfill_persistence.py`: stored bundle load, RLS/immutability and rights revoke coverage.
- Update `docs/superpowers/plans/2026-11-03-wp07-trader-workbench-wbs.md`, `docs/implementation/project-status.md`, `docs/research/glossary-vi.md`, and `docs/implementation/implementation-memory.md` with the verified result and remaining orchestration limits.

## Interfaces

- `BacktestDatasetReplayBundleReference.from_bundle(bundle: BacktestDatasetReplayBundle) -> BacktestDatasetReplayBundleReference`
- `BacktestDatasetReplayBundleReference.canonical_json -> str`; `.bundle_hash -> str`
- `BacktestDatasetReplayBundleReference.from_canonical_json(canonical_json: str, *, expected_bundle_hash: str) -> BacktestDatasetReplayBundleReference`
- `BacktestDatasetReplayBundleReference` fields: `schema_version: int`, `workspace_id: UUID`, `symbol: str`, `evaluation_start: datetime`, `range_end: datetime`, `rights_provider: str`, `rights_purpose: str`, `policy_fingerprint: str`, `components: tuple[BacktestDatasetReplayBundleComponentReference, ...]`.
- `BacktestDatasetReplayBundleComponentReference` fields: `interval: str`, `dataset_hash: str`, `bars_content_hash: str`.
- `BacktestDatasetReplayBundleRepository.append(*, workspace_id: UUID, bundle: BacktestDatasetReplayBundle) -> BacktestDatasetReplayBundleWriteResult`, where result fields are `record: BacktestDatasetReplayBundleRecord` and `is_duplicate: bool`.
- `BacktestDatasetReplayBundleRepository.get_by_hash(*, workspace_id: UUID, bundle_hash: str) -> BacktestDatasetReplayBundleReference | None`
- `BacktestDatasetReplayBundleLoader.load_for_replay(*, workspace_id: UUID, bundle_hash: str, now: datetime) -> BacktestDatasetReplayBundle`
- Extend `BacktestOutcomeRepository.append_sensitivity_run(..., dataset_bundle: BacktestDatasetReplayBundle | None = None)`; when supplied, validate `sensitivity_run.manifest.dataset_hash == dataset_bundle.dataset_hash` and append one `BacktestRunDatasetRightsRecord` per child, using the child’s existing `current_rights` evidence.
- `BacktestRunDatasetRightsRecord.from_bundle_component(*, workspace_id: UUID, run_id: UUID, bundle_hash: str, component: BacktestDatasetReplayInput) -> BacktestRunDatasetRightsRecord`; `.canonical_evidence: str` and `.evidence_hash: str` are deterministic. It is unique by `(workspace_id, run_id, child_dataset_hash)` and has a composite workspace/run FK. It does not grant replay permission.

## Tasks

### Task 1: Canonical persisted bundle reference

**Files:**
- Modify: `src/trading_bot/application/backtest_dataset_replay_bundle.py`
- Modify: `tests/unit/test_backtest_dataset_replay_bundle.py`
- Test: `tests/unit/test_backtest_dataset_replay_bundle_persistence.py`

**Interfaces:**
- Consumes: `BacktestDatasetReplayBundle` and its already validated components.
- Produces: `BacktestDatasetReplayBundleComponentReference` and `BacktestDatasetReplayBundleReference` as defined in Interfaces above. Each component binds `interval`, `dataset_hash`, and `bars_content_hash`; canonical payload uses schema version `1` and stable interval ordering. Bundle hash must equal the existing `_bundle_hash` output.

- [x] **Step 1: Write failing tests** for `from_bundle` canonical bytes/hash stability under input ordering, round-trip via `from_canonical_json`, and rejection of wrong expected hash, unknown/missing fields, duplicate/unsorted interval, malformed child hash, and scope tampering.
- [x] **Step 2: Run the focused tests** with `PYTHONPATH=.:tests/unit:tests/e2e .venv/bin/python -m pytest --import-mode=importlib tests/unit/test_backtest_dataset_replay_bundle.py tests/unit/test_backtest_dataset_replay_bundle_persistence.py -q`; confirm the new reference cases fail before implementation.
- [x] **Step 3: Implement** the immutable reference/component contract in `backtest_dataset_replay_bundle.py`; derive its canonical JSON/hash from the exact existing bundle hash payload, excluding volatile rights lease timestamps.
- [x] **Step 4: Run the focused tests** again; all reference and existing bundle tests must pass.

### Task 2: PostgreSQL bundle and per-run rights schema

**Files:**
- Modify: `src/trading_bot/adapters/database/models/backtest_entities.py`
- Modify: `src/trading_bot/adapters/database/models/backtest_outcome_entities.py`
- Modify: `src/trading_bot/adapters/database/models/__init__.py`
- Create: `migrations/versions/e7c9a1b3d5f2_wp07c_persisted_replay_bundle.py`
- Modify: `tests/unit/test_database_models.py`: add both new tables to the foundation schema allowlist.
- Test: `tests/unit/test_backtest_dataset_replay_bundle_persistence.py`
- Modify: `docs/research/glossary-vi.md`: define the new EN→VI run-level rights receipt term with WP-07C sources.

**Interfaces:**
- Consumes: canonical bundle reference from Task 1 and existing `BacktestRunRecord` composite workspace key.
- Produces: `BacktestDatasetReplayBundleRecord.from_reference(...)` / `.to_reference()` and `BacktestRunDatasetRightsRecord.from_bundle_component(*, workspace_id: UUID, run_id: UUID, bundle_hash: str, component: BacktestDatasetReplayInput)` as defined in Interfaces above.
- Bundle table: unique `(workspace_id, bundle_hash)`, JSONB + canonical text, denormalized scope fields, workspace RLS/FORCE RLS, append-only trigger, `crypidian_admin_web` SELECT/INSERT only.
- Rights table: unique `(workspace_id, run_id, child_dataset_hash)`, FK `(workspace_id, run_id)` to `backtest_run`, canonical evidence/hash, RLS/FORCE RLS; `crypidian_trainer_worker` SELECT/INSERT and `crypidian_admin_web` SELECT only. Both tables reject UPDATE/DELETE. Downgrade refuses if either table is non-empty.

- [x] **Step 1: Write failing metadata tests** for table names, workspace unique keys, composite FK, canonical/hash columns, and model round trips/tamper detection.
- [x] **Step 2: Run** `PYTHONPATH=.:tests/unit:tests/e2e .venv/bin/python -m pytest --import-mode=importlib tests/unit/test_backtest_dataset_replay_bundle_persistence.py -q`; verify missing records/contracts fail.
- [x] **Step 3: Implement** ORM records and Alembic revision `e7c9a1b3d5f2` chained from `c9d7e5f3a1b2`, with workspace RLS/FORCE RLS, least-privilege grants, immutable triggers, and non-empty downgrade guards.
- [x] **Step 4: Run** the focused unit tests and migration static checks; confirm one Alembic head and metadata matches migration.

### Task 3: Bundle repository and replay-time loader

**Files:**
- Create: `src/trading_bot/adapters/database/repositories/backtest_dataset_replay_bundles.py`
- Create: `src/trading_bot/application/backtest_dataset_replay_bundle_loader.py`
- Test: `tests/unit/test_backtest_dataset_replay_bundle_persistence.py`

**Interfaces:**
- Consumes: Task 1 reference, Task 2 records, `BacktestDatasetReplayLoader`, and the caller’s `AsyncSession`.
- Produces: repository and loader APIs defined in Interfaces above. Repository queries must bind `app.workspace_id` and include explicit workspace predicates. Loader must resolve every child hash through `BacktestDatasetReplayLoader`, rebuild the bundle, and compare all stored scope/hash values before returning.

- [x] **Step 1: Write failing repository tests** for append/readback, identical retry, same-hash payload conflict, corrupt canonical/JSONB/index fields, invalid hash, and workspace mismatch.
- [x] **Step 2: Write failing loader tests** for successful two-timeframe load and rejection after child missing/not READY, current rights changed/revoked/expired, bars missing/changed, or bundle reference tampering.
- [x] **Step 3: Run** `PYTHONPATH=.:tests/unit:tests/e2e .venv/bin/python -m pytest --import-mode=importlib tests/unit/test_backtest_dataset_replay_bundle_persistence.py -q` and confirm each new behavior fails for the expected missing implementation.
- [x] **Step 4: Implement** advisory-lock idempotent append/read repository and application loader; a current-rights failure must propagate without returning partial bundle data.
- [x] **Step 5: Run** the focused bundle/reference/loader tests to green.

### Task 4: Append immutable rights receipts with each new WP-07C outcome

**Files:**
- Modify: `src/trading_bot/adapters/database/repositories/backtest_outcomes.py`
- Modify: `tests/unit/test_backtest_outcomes_repository.py`
- Test: `tests/unit/test_backtest_dataset_replay_bundle_persistence.py`

**Interfaces:**
- Consumes: optional `dataset_bundle` parameter from Interfaces and Task 2 receipt model.
- Produces: one canonical child receipt per component, binding workspace, run ID, bundle hash, child dataset hash/interval, provider, purpose, terms URL, policy fingerprint, `verified_at`, DB `checked_at`, and evidence lease `valid_until`.

- [x] **Step 1: Write failing tests** that new bundle-backed outcomes append one receipt per child in the same transaction, reject wrong workspace or manifest bundle hash, preserve first evidence on identical retry, conflict on changed evidence, and leave a legacy duplicate without receipts unchanged.
- [x] **Step 2: Run** `PYTHONPATH=.:tests/unit:tests/e2e .venv/bin/python -m pytest --import-mode=importlib tests/unit/test_backtest_outcomes_repository.py tests/unit/test_backtest_dataset_replay_bundle_persistence.py -q`; confirm the new receipt assertions fail before code.
- [x] **Step 3: Implement** receipt validation and append after outcome identity is known. For a new logical outcome, insert receipts atomically. For a duplicate outcome, compare existing receipts and never create missing legacy evidence or overwrite the first receipt.
- [x] **Step 4: Run** the focused outcome and bundle tests to green.

### Task 5: Compose E2E, WBS, glossary, and memory readback

**Files:**
- Modify: `tests/e2e/test_backtest_outcomes_persistence.py`
- Modify: `tests/e2e/test_wp07_rest_backfill_persistence.py`
- Modify: `tests/e2e/probes/wp07_backfill_persistence.py`
- Modify: `docs/superpowers/plans/2026-11-03-wp07-trader-workbench-wbs.md`
- Modify: `docs/implementation/project-status.md`
- Modify: `docs/research/glossary-vi.md`
- Modify: `docs/implementation/implementation-memory.md`

**Interfaces:**
- Consumes: Task 3 persisted bundle loader and Task 4 outcome receipt behavior.
- Produces: Compose E2E evidence for role grants/RLS, workspace isolation, immutability, idempotent receipt persistence, current-rights revalidation/revoke rejection, and guarded downgrade.

- [x] **Step 1: Extend the Compose outcome E2E** to save a synthetic bundle-backed run, assert one receipt per child, verify worker retry preserves canonical evidence/hash, verify admin readback and cross-workspace invisibility, and prove UPDATE/DELETE are rejected.
- [x] **Step 2: Extend the WP-07C persistence E2E** to append/read a stored multi-timeframe bundle, load it while rights remain valid, revoke/change the fixture policy and verify loader denial, and assert downgrade is blocked while persisted rows exist.
- [x] **Step 3: Run** `PYTHONPATH=.:tests .venv/bin/python -m pytest --import-mode=importlib tests/e2e/test_backtest_outcomes_persistence.py tests/e2e/test_wp07_rest_backfill_persistence.py -q --tb=short`; both Compose tests must pass without network data or live venue calls.
- [x] **Step 4: Update WBS/status/glossary/memory** with exact implemented scope, migration head, test outputs, role boundary, no-retroactive-evidence rule, and remaining production worker orchestration; verify/retain EN→VI term `Per-run rights receipt → Biên nhận quyền theo lần chạy` with the WP-07C rights gate/design sources.
- [x] **Step 5: Read back** the new implementation-memory entry and verify all claims against source, migration, tests, WBS, and status. Run Ruff check/format on changed Python files, mypy on changed source, Compose `alembic check`, `alembic heads`, and `git diff --check`; record results. Do not commit/push in this plan.

## Handoff

This plan implements only persisted bundle references and per-run rights evidence. It does not add a production job runner/API/CLI/dashboard/queue, progress/cancel support, real market-data download, Telegram delivery, venue orders, or profitability claims. Run-level evidence is an audit snapshot and never a substitute for current authorization.
