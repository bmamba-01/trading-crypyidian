# WP06 License Evidence Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nhận bằng chứng license theo luồng, chỉ lưu SHA-256 và scope có kiểu, ghim vào policy/receipt và giữ mọi quyền WP06 fail-closed khi scope thiếu hoặc không khớp.

**Architecture:** Thêm value object canonical cho scope, evidence ledger append-only theo workspace/source, upload raw body từ một policy `rights_pending`, rồi gắn đúng một evidence ID vào revision VERIFIED. Đồng bộ hash contract trong Python/PostgreSQL, tích hợp kiểm tra scope vào source-dependent WP06 rights paths và thêm trang admin responsive.

**Tech Stack:** Python 3.14, FastAPI 0.156.4 `Request.stream()`, Pydantic, SQLAlchemy 2.1, PostgreSQL/Alembic, Jinja2, pytest/Playwright. Không thêm dependency.

**Spec:** `docs/superpowers/specs/2026-11-13-wp06-license-evidence-digest-design.md`

## Global Constraints

- Chỉ lưu external `evidence_ref`, SHA-256, byte count, allowlisted media type, canonical typed scope và provenance metadata; không lưu bytes hoặc tên file. Evidence immutable có thể được dùng lại giữa revisions cùng workspace/source khi ref/scope/digest giữ nguyên.
- Upload là raw body theo `Request.stream()`, tối đa 10 MiB; đọc số bytes thực, không gọi `.body()`.
- Scope v1, purpose/output/derived-use allowlist, nửa mở `[start,end)`, duplicate set rejection, canonical JSON và scope hash phải khớp spec.
- Exact consumer scope được dựng từ manifest/snapshot/bar lineage đã lưu và operation server; missing dimensions fail closed, không suy đoán từ tên/version/hash.
- Policy VERIFIED cũ tiếp tục đọc được nhưng không đạt WP06 TRAINING/PAPER rights gate; không backfill digest.
- Bảng mới RLS FORCE, FK ghép workspace/source, append-only, role grant tối thiểu; source rights hash/receipt gồm digest và scope hash.
- Nội dung API/UI/docs/comment bằng tiếng Việt; identifier bằng tiếng Anh; thuật ngữ mới được thêm vào glossary.
- Mọi thay đổi hành vi dùng TDD RED→GREEN; trước commit chạy unit và Compose E2E liên quan, cập nhật/đọc lại memory.

## Review Focus

- Stream có `Content-Length` sai/thiếu hoặc chunk cuối vượt 10 MiB → không tạo evidence.
- Retry cùng idempotency key cùng/different digest → bản ghi cũ hoặc conflict, không tạo duplicate.
- Source/policy/workspace mismatch, policy không còn pending, purpose/output vượt scope → từ chối.
- Policy legacy hoặc consumer thiếu một chiều scope → không được cấp WP06 eligibility.
- Hash/canonical parity SQL–Python và RLS/role grants không làm lộ evidence hoặc mở quyền đọc file.
- Runtime scorer/SHADOW/DRIFT phải kiểm quyền cho từng candidate/decision/cohort member; không dùng receipt nguồn hoặc boolean chung cho batch.

---

### Task 1: Typed License Scope và canonical digest

**Files:**
- Create: `src/trading_bot/domain/research/license_scope.py`
- Modify: `src/trading_bot/domain/research/__init__.py`
- Test: `tests/unit/test_source_license_scope.py`
- Modify: `docs/research/glossary-vi.md`

**Interfaces:**
- Produces `SourceLicenseScope`, `SourceUseScope`, `canonical_scope_json(scope) -> str`, `scope_sha256(scope) -> str`, and `scope.covers(use) -> bool`.
- Consumes `SourceUsePurpose` enum and scope fields fixed by the spec.

- [ ] **Step 1: Viết test RED** cho normalization, duplicate rejection, token/symbol/timeframe validation, `[start,end)` boundary, purpose/output/derived-use subset, max retention và canonical SHA-256.
- [ ] **Step 2: Chạy test** `PYTHONPATH=. uv run pytest tests/unit/test_source_license_scope.py -q`; xác nhận fail vì symbols chưa tồn tại.
- [ ] **Step 3: Implement value object** từ chối member trùng rồi sort tập hợp, timestamp UTC cố định, no wildcard và canonical contract v1.
- [ ] **Step 4: Chạy test GREEN** cùng Ruff/mypy mục tiêu; chỉ sửa behavior trong phạm vi test.
- [ ] **Step 5: Thêm glossary EN→VI** cho license evidence digest, typed rights scope và source-use coverage, dẫn nguồn FastAPI/reference phù hợp.

### Task 2: Evidence ledger, source policy revision và migration

**Files:**
- Create: `src/trading_bot/adapters/database/models/source_license_evidence_entities.py`
- Modify: `src/trading_bot/adapters/database/models/research_entities.py`
- Modify: `src/trading_bot/adapters/database/models/__init__.py`
- Modify: `src/trading_bot/domain/research/source_registry.py`
- Modify: `src/trading_bot/adapters/database/repositories/source_registry.py`
- Create: `migrations/versions/a4c6e8f0b2d5_wp06_license_evidence.py`
- Test: `tests/unit/test_source_revision_access.py`, `tests/unit/test_source_registry_repository.py`, `tests/unit/test_wp05_source_registry_migration.py`

**Interfaces:**
- `SourcePolicyRevision` carries nullable `provider/product/venue/contract` identity fields and optional `license_scope`, `license_evidence_id`, `license_digest`, and `license_scope_hash`; nulls preserve legacy rows. Identity becomes immutable once set.
- `SourceLicenseEvidenceRecord` contains only digest/scope/reference/provenance metadata.
- `source_rights_hash()` conditionally includes evidence identity/digest/scope hash, preserving old fingerprints for legacy revisions.

- [ ] **Step 1: Viết unit/migration tests RED** cho verified revision thiếu evidence, scope/evidence mismatch, immutable evidence, legacy-null round-trip, fingerprint binding/parity contract, workspace FK, append-only and RLS/grants.
- [ ] **Step 2: Chạy các tests mục tiêu** và xác nhận thiếu model/migration behavior.
- [ ] **Step 3: Thêm model và migration nối từ Alembic head `f6b8d0e2a4c7`**; evidence FK ghép workspace/source, canonical text/JSONB consistency, DB SHA-256 check, verified revision trigger và downgrade populated guard.
- [ ] **Step 4: Map ORM/repository/domain**; verified legacy database rows đọc được nhưng eligibility yêu cầu evidence id/digest/scope hash hợp lệ.
- [ ] **Step 5: Chạy tests GREEN**, `uv run alembic heads`, migration schema unit, Ruff/mypy mục tiêu; mở rộng và chạy `tests/e2e/test_wp05_source_registry.py` để kiểm tra ledger, RLS, append-only và role grants trên PostgreSQL Compose.

### Task 3: Pending policy và upload raw stream

**Files:**
- Modify: `src/trading_bot/application/source_policy_admin.py`
- Modify: `src/trading_bot/adapters/database/repositories/source_registry.py`
- Modify: `src/trading_bot/admin/research_source_schemas.py`
- Modify: `src/trading_bot/admin/research_source_router.py`
- Test: `tests/unit/test_source_policy_admin.py`, `tests/unit/test_research_source_admin_api.py`
- Test: `tests/e2e/test_wp05_source_policy_admin.py`

**Interfaces:**
- Pending revision request contains `SourceLicenseScope` plus external archive `evidence_ref`.
- Pending revision captures required provider/product/venue/contract, which must match scope and remain stable on future revisions.
- `POST /api/v1/research-sources/{source_id}/revisions/{policy_id}/license-evidence` reads raw body using `Request.stream()` and returns metadata-only evidence response.
- Upload limit is 10 MiB; allowlist and idempotency are exactly as defined in spec.

- [ ] **Step 1: Viết tests RED** cho in-limit chunk stream, over-limit/chunked body, bad `Content-Length`, empty body, unsupported media type, disconnect, source/policy/MFA/CSRF, idempotent retry and changed digest.
- [ ] **Step 2: Chạy tests mục tiêu** và xác nhận endpoint/append operation chưa tồn tại.
- [ ] **Step 3: Implement stream hashing** incremental, không giữ bytes hoặc filename; tạo evidence row sau khi stream hoàn tất trong transaction và trả response no-store.
- [ ] **Step 4: Chạy tests GREEN** cho API/service/repository, test workspace scope và transaction rollback.

### Task 4: TRAINING manifest và normalized-data exact scope

**Files:**
- Create: `migrations/versions/b7d9f1a3c5e8_wp06_training_license_scope.py`
- Modify: `src/trading_bot/domain/learning/dataset.py`, `src/trading_bot/application/learning_dataset.py`
- Modify: `src/trading_bot/application/learning_split_candidate.py`, `src/trading_bot/application/learning_fold_samples.py`
- Modify: `src/trading_bot/adapters/database/repositories/backtest_outcomes.py`
- Modify: `src/trading_bot/domain/research/source_revision_access.py`, `src/trading_bot/adapters/database/repositories/source_items.py`
- Modify: `src/trading_bot/application/dataset_source_eligibility.py`, `src/trading_bot/application/learning_fold_evaluation.py`
- Modify: `src/trading_bot/adapters/database/repositories/learning_fold_samples.py`
- Modify: `migrations/versions/f1b3d5f7a9c2_wp06_consumer_source_rights.py` behavior via chained `wp06_assert_training_source_rights` and `wp06_claim_learning_training_batch` replacements only; historical file remains immutable.
- Modify: `src/trading_bot/adapters/database/models/learning_source_training_receipt_entities.py`
- Create: `src/trading_bot/adapters/database/models/learning_replay_training_receipt_entities.py`
- Test: `tests/unit/test_dataset_source_eligibility.py`, `tests/unit/test_learning_fold_samples.py`, `tests/unit/test_learning_fold_evaluation.py`, `tests/unit/test_learning_split_candidate.py`, `tests/unit/test_learning_training_batch_access.py`, `tests/unit/test_source_revision_access.py`
- Test: `tests/e2e/test_wp06_source_license_scope.py` (create), `tests/e2e/test_wp06_normalized_fold_samples.py`, `tests/e2e/test_wp06_rights_invalidation.py`

**Interfaces:**
- `SourceAccessEnvelope` exposes only current-policy typed identity, scope and evidence metadata; it never exposes bytes or raw ledger rows.
- The verifier persists a typed per-sample `SourceUseLineage` bound into the canonical manifest: exact source item revision, MarketBar UUIDs (where source is OHLCV), and symbol/timeframe/range derived by querying those rows. The DB reopens and verifies the saved IDs; it never joins `DatasetSample.feature_snapshot_hash` to `FeatureSnapshotRecord` because for backtest outcomes that field is the entry-feature-values hash.
- `BacktestTradeOutcomeRecord` has `run_id/trial_record_id`, not `bundle_hash`. Repository must join outcome→run→all `BacktestRunDatasetRightsRecord` child rows, verify exact child set/single bundle/provider/policy/run-manifest consistency, and attach only that authoritative bundle hash to `LearningCandidateOutcome`. `learning_split_candidate.py` then stores it as typed `source_replay_bundle_hash` in a new manifest version and marks the sample source-dependent. SQL independently cross-checks sample→outcome→trial/run/right receipts/bundle; no empty `source_references`/false flag may bypass rights. Test missing child receipt, extra/wrong child, cross-run, bundle/provider/policy mismatch. Legacy manifests remain readable but ineligible for source-backed training.
- `FeatureSnapshotRecord.input_bar_ids` are content hashes of open-time/symbol/timeframe, not MarketBar UUIDs or source revisions; they must not be used as rights lineage. Missing source-use lineage is an explicit denial, not permission to fall back to manifest-global symbols/date or feature hash.
- Training output/derived-use and aggregate-evaluation output/derived-use are explicit server-side operations. Indefinite current retention is `None`, so a finite license is denied.
- The stored source training receipt remains immutable and matches a successful exact use-scope check under the existing source lock order.

- [ ] **Step 1: Viết tests RED** for producer preservation of outcome bundle hashes, authoritative per-sample source-use lineage, valid exact coverage, every identity/dimension/purpose/output/derived-use/retention denial, legacy or missing lineage, forged/cross-workspace MarketBar UUID, event backlog/revocation, proof→revoke/contract-scope-change→trainer claim denial before payload/receipt, and retry receipt mismatch.
- [ ] **Step 2: Chạy scoped unit và Compose** để chứng minh gate hiện chưa đối chiếu license scope với persisted snapshot/bar inputs.
- [ ] **Step 3: Implement typed envelope/use-scope derivation** from verifier-produced manifest lineage, replay candidate `bundle_hash` and exact persisted MarketBar rows; preserve READ COMMITTED and source lock order; use operation-specific purpose/output/derived-use.
- [ ] **Step 4: Persist append-only `learning_replay_training_receipt` per bundle child, normalized dataset/access run and exact scope hash. Add chained SECURITY DEFINER/helper migration replacing `wp06_assert_training_source_rights` and `wp06_claim_learning_training_batch` so current scope is rechecked at normalization and every training claim before payload/receipt; preserve grants, RLS, locks, receipt immutability and legacy denial. No direct evidence-table grant.
- [ ] **Step 5: Run focused unit + fresh Compose scope, normalization and rights-invalidation E2E**, including allowed exact fixture and every mismatch/no-write case; no skips.

### Task 5: Replay, scorer TEST và deployment source scope

**Files:**
- Create: `migrations/versions/c1d3e5f7a9b2_wp06_scorer_license_scope.py`
- Modify: `src/trading_bot/application/binance_rest_backfill_rights.py`, `src/trading_bot/application/backtest_dataset_replay.py`
- Modify: `src/trading_bot/adapters/database/replay_training_rights.py`
- Modify: `src/trading_bot/workers/backtest_replay_worker.py` only as required to preserve the current reauthorization boundary before replay execution/persistence
- Modify: `src/trading_bot/adapters/database/repositories/candidate_universe_proofs.py`, `learning_scorer_test_opportunity_proofs.py`, `learning_scorer_test_access.py`
- Modify: `src/trading_bot/adapters/database/repositories/learning_scorer_current_rights.py`, `src/trading_bot/adapters/database/repositories/learning_scoring_deployments.py`
- Modify or replace SQL functions: `wp06_assert_training_source_rights` TEST/evaluation callers, `wp06_recheck_scorer_current_rights`, `wp06_verify_scorer_deployment_rights`.
- Test: `tests/unit/test_learning_scorer_current_rights.py`, `tests/unit/test_learning_scoring_deployments.py`
- Test: replay rights-gate unit coverage and `tests/unit/test_candidate_universe_proof.py`, `test_learning_scorer_test_opportunity_proofs.py`, `test_learning_scorer_test_access_repository.py`
- Test: `tests/unit/test_binance_rest_backfill_rights.py`, `tests/unit/test_backtest_dataset_replay.py`, `tests/unit/test_historical_backfill.py`
- Test: `tests/e2e/test_wp06_scorer_test_opportunity_proof.py`, `tests/e2e/test_wp06_candidate_universe_proof.py`, `tests/e2e/test_wp06_scorer_deployment.py`, `tests/e2e/test_wp06_source_pinned_replay_persistence.py`
- Test: `tests/e2e/test_wp07_rest_backfill.py`, `tests/e2e/test_wp07_rest_backfill_persistence.py` for deny-before-fetch/write and replay revalidation after scope change.

**Interfaces:**
- `DatabaseReplayTrainingRightsGate.authorize` takes an explicit server enum operation. Its callers cannot omit the operation or reuse a receipt for a different purpose.
- `SourceRegistryBackfillRightsGate.authorize` is the actual BACKTEST_RESEARCH/PAPER_DEMO fetch and replay gate; it rebuilds scope from the exact REST request or persisted replay manifest and checks current evidence before page fetch and revalidation. BACKTEST_RESEARCH maps to `aggregate_report`/`aggregate_reporting`; PAPER_DEMO maps to `paper_inference`/`paper_inference`. It is separate from the downstream TRAINING gate.
- Bundle generation/replay separately checks `BACKTEST_RESEARCH` + aggregate report/reporting; TRAINING fit/reuse checks `TRAINING` + derived features/model outputs; TEST/evaluation SQL `access_kind='evaluation'` checks `TRAINING` + aggregate report/reporting. These checks are independent and never inferred from `rights_purpose` stored in the bundle.
- Rights use is reconstructed from each persisted bundle child and verified `MarketBar` source/revision identities; each interval uses `[warmup_start, range_end)` with `range_end` already exclusive (do not inspect `entry_features_hash` as a snapshot key or add microseconds to manifest `range_end`).
- Current deployment rights recheck comes from persisted report/proof/bundle lineage; caller cannot choose manifest, scope or policy dimensions.
- Only metadata/fingerprint/eligibility crosses the deployment rights gateway; no raw score, probability array or trade rows.

- [ ] **Step 1: RED tests** for exact backfill/replay-vs-training-vs-TEST operation scopes, deny-before-fetch/write/run, reauthorization after scope/policy change, every child warmup interval, mixed interval/source/revision, missing lineage, policy purpose mismatch, revoked evidence, SQL evaluation output mapping and unchanged proof/receipt/deployment pointer/event on denial.
- [ ] **Step 2: Implement current-rights SQL/repository recheck** using the canonical evidence-aware fingerprint/scope helper and narrowly granted function execution.
- [ ] **Step 3: Run unit and fresh Compose proof/deployment/replay E2E**, including scope changes between report and activation; no skips.

### Task 6: Candidate-bound PAPER runtime, SHADOW and DRIFT scope

**Files:**
- Create: `migrations/versions/c3e5f7a9b1d4_wp06_paper_runtime_license_scope.py`
- Create: `src/trading_bot/adapters/database/models/bot_decision_source_bar_entities.py`
- Modify: `src/trading_bot/adapters/database/repositories/learning_scorer_runtime_targets.py`, `learning_scorer_runtime_committer.py`, `learning_scorer_current_rights.py`
- Modify: `src/trading_bot/adapters/database/repositories/learning_shadow.py`, `learning_drift.py`
- Modify: `src/trading_bot/adapters/database/models/__init__.py`, BotDecision repository and score-event guard for immutable per-timeframe MarketBar references
- Modify: `src/trading_bot/application/learning_shadow.py`, `learning_drift.py`
- Modify or replace SQL functions: runtime target resolver, score-event guard, PAPER rights receipt/hash, shadow decision rights check, and drift cohort/member guard.
- Test: `tests/unit/test_learning_scorer_runtime_targets.py`, `tests/unit/test_learning_scorer_runtime_committer.py`, `tests/unit/test_learning_shadow_repository.py`, `tests/unit/test_learning_shadow_application.py`, `tests/unit/test_learning_drift_application.py`, `tests/unit/test_bot_dispatcher.py`, `tests/unit/test_bot_decision_contexts.py`
- Test: `tests/e2e/test_wp06_scorer_runtime_target_resolver.py`, `tests/e2e/test_wp06_scorer_deployment.py`, `tests/e2e/test_wp06_scorer_paper_gateway.py`, `tests/e2e/test_wp06_shadow_drift_persistence.py`, `tests/e2e/test_wp06_task9_integrated_learning_journey.py`

**Interfaces:**
- Runtime scope is candidate-bound and reconstructed/verified from `DecisionSnapshotTimeframeLineage.source_bar_ids` exact MarketBar UUIDs, source event IDs, symbol and timeframe; `FeatureSnapshotRecord.input_bar_ids` content hashes are not substituted.
- Persist all snapshot input bars in `bot_decision_source_bar` in the same transaction as BotDecision, including baseline decisions without scorer events; use append-only tenant-scoped composite FKs and verify timeframe/snapshot/event/revision. Runtime score event, Shadow and Drift re-open this exact set; a digest alone is insufficient lineage.
- Shadow rights are checked per persisted decision before append; one batch cannot share a generic source-wide `true` receipt.
- Drift rechecks current scope for every exact cohort member. Any missing/uncovered member yields `INCONCLUSIVE`; reports never change bot/deployment/hold state.
- PAPER operation maps to `paper_demo` / `paper_inference`; drift report maps to `paper_demo` / `aggregate_report`; both require indefinite retention under the current product lifecycle.

- [ ] **Step 1: RED tests** for persisted source-bar identity/revision/checksum and exact candidate coverage, policy change between preflight and atomic commit, wrong symbol/timeframe/range/source denial before score commit, no decision/event/pointer mutation, missing decision-lineage denial, positive baseline and scorer-backed Shadow observation, per-decision shadow denial, cohort member mismatch to INCONCLUSIVE, and QA/legacy evidence denial.
- [ ] **Step 2: Implement candidate-bound DB gateway and exact lineage reconstruction**; repeat rights check atomically before score/shadow/drift persistence, retain source lock order and no raw evidence grants.
- [ ] **Step 3: Run unit plus fresh Compose runtime, PAPER gateway, shadow/drift and integrated Task9 E2E**, assert 0 skips and unchanged state for denied cases.

### Task 7: Admin upload/review UI và Playwright

**Files:**
- Modify: `src/trading_bot/templates/admin/research_sources.html`
- Modify: `src/trading_bot/admin/research_source_router.py`
- Test: `tests/e2e/test_admin_research_sources_playwright.py`
- Test: `tests/e2e/test_research_source_admin_ui.py`
- Create/Update screenshots: `tests/e2e/evidence/wp06-license-evidence-desktop.png`, `tests/e2e/evidence/wp06-license-evidence-mobile.png`

**Interfaces:**
- UI creates pending scope revision first, uploads bytes second, and exposes separate reviewer approval action.
- After reload, a workspace-authorized metadata-only facade reads `evidence_for_pending()` and shows digest/scope; UI never reads evidence ledger/raw row. Reviewer may select an immutable prior `license_evidence_id` only through the existing approval contract.
- Full Vietnamese labels; digest copy; no file bytes, name, or external reference leakage beyond authorized workspace.

- [ ] **Step 1: Viết Playwright assertions RED** for Vietnamese scope form, metadata-only digest after reload, file upload/CSRF/idempotency, separate reviewer approval, desktop/mobile no overflow and pending-vs-verified lifecycle.
- [ ] **Step 2: Chạy Playwright** và xác nhận missing controls.
- [ ] **Step 3: Implement responsive form and scoped metadata-only pending evidence facade** with raw file body, digest recovery after reload, and reviewer evidence-ID selection.
- [ ] **Step 4: Chạy desktop 1280×800/mobile 390×844 Playwright**, inspect both screenshots, assert no horizontal overflow and 0 skipped cases.

### Task 8: Traceability và integrated verification

**Files:**
- Modify: `docs/research/glossary-vi.md`
- Modify: `docs/research/wp06-data-rights-research.md`
- Modify: `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md`
- Modify: `docs/superpowers/plans/2026-11-12-wp06-open-gaps.md`
- Modify: `docs/implementation/06-ai-learning.md`
- Modify: `docs/implementation/implementation-memory.md`

- [ ] **Step 1: Chạy full unit suite**, all affected Compose E2E including source registry/policy, training scope, scorer deployment, candidate runtime, shadow/drift and Task9; no skip.
- [ ] **Step 2: Chạy Ruff check/format, mypy, Compose config, Alembic single-head/check, verify no stale `f6b8d0e2a4c7` head expectations remain in tests/probes, and `git diff --check`.**
- [ ] **Step 3: Cập nhật WBS**: code contract status tách biệt với external license/release evidence; giữ active modes khóa.
- [ ] **Step 4: Ghi memory** chính xác nguồn/URL/ngày/test/result/giới hạn, sau đó đọc lại memory và đối chiếu diff/output.

## Trạng thái triển khai 14/11/2026

- Task 1–3: typed `wp06-license-scope-v1`, append-only evidence ledger, policy approval pin và streamed SHA-256 upload đã implement; bytes không lưu DB/object store. UI evidence tiếng Việt responsive đã kiểm Playwright desktop/mobile.
- Task 4: replay bundle/normalized TRAINING manifest đối chiếu shared child lineage, exact persisted MarketBar identity và current evidence scope; receipt ghi metadata theo access và immutability. Fresh PostgreSQL Compose E2E **1 passed/128.36s/0 skipped** kiểm tamper, receipt, rights revoke trước trainer claim và no-payload/no-training-receipt.
- E2E dùng synthetic first-party QA fixture, không phải license thật. Test riêng evaluator direct-call denial là chủ ý ACL; tampered scope được kiểm qua `candidate-proof-builder` normalization gateway. Repository map `WP06_TRAINING_REPLAY_*` về fail-closed conflict.
- Task 5–6 vẫn mở: exact scope/current-rights wiring cần nối tiếp vào scorer TEST, deployment/runtime PAPER/SHADOW và DRIFT; chưa có positive active target. Task 8 vẫn mở cho integrated regression suite/review cuối. Full unit **3906 passed**, Playwright performance/license **5 passed**, performance gateway Compose **1 passed**, Ruff/format/mypy/Compose/Alembic/diff checks đạt. `SHADOW`/`PAPER_GATE` tiếp tục khóa.
