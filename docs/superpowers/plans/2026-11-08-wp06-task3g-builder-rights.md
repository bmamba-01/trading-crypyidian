# WP06 Task 3G — Builder TRAINING rights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Cho phép candidate builder chuẩn hóa source-dependent manifest chỉ khi mọi typed reference còn quyền TRAINING hiện hành và giữ được khóa quyền đến khi receipt cùng normalized payload commit.

**Architecture:** Reuse `DatasetSourceEligibilityGate`, `DatasetManifestEligibilityRepository`, và `SourceItemRepository` trên cùng transaction. Gate khóa theo `research-source:{workspace_id}:{source_id}` (cùng key policy/item writers), kiểm pending rights events, invalidation và access envelope; repository kiểm tra lại sau flush rồi ghi receipt metadata append-only cùng normalized rows. Nếu policy, item, role, tenant, hoặc transaction không thể xác minh thì dừng trước khi trả thành công.

**Tech Stack:** Python 3.14, SQLAlchemy async, PostgreSQL 18, Alembic, pytest/Compose.

**Spec:** `docs/superpowers/specs/2026-11-06-wp06-completion-design.md`, mục Quyết định dữ liệu 5–6 và Ranh giới DB cho holdout.

## Global Constraints

- Chỉ dữ liệu có typed lineage `dataset-manifest-v3` và purpose `TRAINING` mới được gate.
- Candidate builder là capability writer riêng; không cấp quyền source payload, trainer, app hay admin.
- Quyền phải khớp source/item/revision/policy sequence/policy ID/rights hash; receipt ghi thời điểm DB, không phải quyền dùng lại về sau.
- Giữ advisory lock chung với WP05 writers qua toàn bộ flush và commit; source locks lấy theo thứ tự ổn định.
- RLS tenant-scoped, bảng receipt append-only và downgrade bị chặn khi có dữ liệu.
- Comment/docstring/tài liệu tiếng Việt; identifier tiếng Anh; không stage/commit/push/reset/clean worktree.

## Review Focus

- Policy writer commit trong lúc builder chờ source lock: builder phải thấy policy mới dưới READ COMMITTED và từ chối stale reference.
- Pending/dead rights event hoặc invalidation: từ chối trước khi normalized payload được append.
- Policy hết hạn giữa lần check đầu và flush: kiểm lại trước khi transaction có thể commit.
- Retry sau khi rights bị revoke: không bỏ qua gate chỉ vì normalized dataset đã tồn tại.
- Source references lặp nhiều sample: khóa source một lần theo thứ tự ổn định, receipt không duplicate.

## Task 1: Gate quyền nguồn và receipt persist cùng normalized dataset

**Files:**
- Modify: `src/trading_bot/application/dataset_source_eligibility.py`
- Modify: `src/trading_bot/adapters/database/repositories/learning_fold_samples.py`
- Create: `src/trading_bot/adapters/database/models/learning_source_training_receipt_entities.py`
- Modify: `src/trading_bot/adapters/database/models/__init__.py`
- Create: `migrations/versions/<revision>_wp06_source_training_receipt.py`
- Modify: `tests/unit/test_dataset_source_eligibility.py`
- Modify: `tests/unit/test_learning_fold_samples.py`
- Modify: `tests/e2e/probes/wp06_normalized_fold_samples.py`
- Modify: `tests/e2e/test_wp06_normalized_fold_samples.py`

**Interfaces:**
- Consumes: `DatasetSourceReference`, `DatasetManifest`, `NormalizedLearningDataset`, `SourceItemRepository.select_access_envelope`, and `DatasetManifestEligibilityRepository`.
- Produces: immutable `DatasetSourceRightsEvidence` for each manifest/sample/reference; `LearningFoldSampleRepository.append_normalized_dataset` writes matching `learning_source_training_receipt` rows under the same Unit of Work and returns metadata only.

- [x] **Step 1: Add failing unit tests** for evidence returned on permitted exact rights, source lock uniqueness/ordering, rights hash mismatch, pending event, invalidation, and gate exception; Compose verifies the repository rejects stale source refs.
- [x] **Step 2: Run focused unit tests and confirm the new evidence/receipt assertions fail.**
- [x] **Step 3: Implement the source gate and repository integration.** Require active READ COMMITTED transaction with workspace binding; lock sorted source IDs using the shared WP05 advisory key; verify every source reference; recheck eligibility after normalized rows flush; persist canonical hash/DB check time receipt rows atomically, including on first insert only. Same-content retry rechecks current rights and verifies stored receipts before returning.
- [x] **Step 4: Add migration grants and E2E tests.** Builder receives only the metadata columns/tables needed for source policies, item metadata, invalidation events, outbox rights-event status and receipts. Trainer/app/admin receive no receipt INSERT/UPDATE/DELETE. E2E covers allowed, revoked/invalidation, policy-writer contention, workspace isolation, immutable receipt and populated downgrade guard; unit tests cover pending events and gate failures.
- [x] **Step 5: Run verification.** Focused source eligibility/fold-sample/database-model units; sequential WP06 Compose normalized-fold E2E; WP05 rights event E2E regression; Ruff/format/mypy; Alembic one-head/check and `git diff --check`.
- [x] **Step 6: Update and reread** implementation memory, SDD progress, WP06 completion/WBS/module/project-status. Keep Task3 open because evaluator/trainer current-rights access, pre-replay quarantine and API/UI remain.

**Completion evidence:** all focused commands pass without skips; Compose proves source rights remain serialized through receipt/payload persistence and no source content or holdout is exposed. Do not commit because the shared dirty worktree contains unrelated WP03/WP05/WP07 changes.
