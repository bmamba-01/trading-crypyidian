# WP06.4 Diagnostic Evaluation Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (inline execution is authorized for this continuation). Steps use checkbox syntax for tracking. Do not commit unless explicitly requested.

**Goal:** Nối WP06.4 fold evaluator với PostgreSQL snapshot và lưu báo cáo chẩn đoán bất biến, idempotent, theo workspace.

**Architecture:** `LearningEvaluationReportService` đọc manifest và fold outcomes qua một `UnitOfWork(snapshot_read_only=True)`, dùng `DatabaseFoldOutcomeVerifier`, rồi ghi report qua UoW thứ hai. Bảng WP06 riêng giữ canonical JSON/hash và chỉ role trainer được SELECT/INSERT; báo cáo luôn đánh dấu completeness chưa xác minh.

**Tech Stack:** Python 3.14, SQLAlchemy 2.1, Alembic, PostgreSQL 18, pytest/Compose hiện có. Không thêm dependency.

**Spec:** [WP06.4 diagnostic evaluation persistence design](../specs/2026-11-06-wp06d-diagnostic-evaluation-persistence-design.md).

## Global Constraints

- Nội dung hướng dẫn, comment, docstring viết bằng tiếng Việt; identifier/API dùng tiếng Anh.
- Không thêm ML dependency, không gọi API/provider, không tạo order, không bật LIVE.
- Chỉ dùng manifest WP-02 đã persist; từ chối manifest có research source.
- Mọi đọc manifest/outcome diễn ra trong một transaction `REPEATABLE READ READ ONLY` và cùng workspace scope.
- Fold evaluator chỉ đọc outcome UUID mà fold yêu cầu; sealed holdout không được đưa vào report.
- Mọi report giữ `candidate_completeness_verified=false`, `evaluation_mode=diagnostic` và caveat WP-02.
- Report append-only; idempotency theo `(workspace_id, idempotency_key)`; cùng key/payload là retry, cùng key/payload khác là conflict.
- Không stage/commit/push/reset/clean; không sửa unrelated dirty baseline.

## Review Focus

1. Caller đưa workspace hoặc manifest hash khác — request phải fail closed trước khi lưu; kiểm bằng UoW/RLS và Compose.
2. Fold có chứa hoặc truy vấn sealed holdout — không tạo report, không truy vấn payload holdout; kiểm bằng fixture có holdout.
3. Caller gửi source-dependent manifest không có rights gate an toàn — từ chối trước verifier và persist.
4. Cùng idempotency key nhưng report khác — conflict; retry cùng payload trả lại record hiện có.
5. Sửa report JSON/canonical bytes/hash hoặc UPDATE/DELETE trực tiếp — readback từ chối mismatch, DB trigger/privileges chặn mutation.

---

### Task 1: Canonical payload rõ chế độ chẩn đoán

**Files:**
- Modify: `src/trading_bot/application/learning_fold_evaluation.py`
- Test: `tests/unit/test_learning_fold_evaluation.py`

**Interfaces:**
- `WalkForwardEvaluationReport.canonical_json: str` trả canonical payload đã dùng tạo `report_hash`.
- `evaluation_mode` trong canonical payload cố định `diagnostic`.

- [x] Viết test xác nhận canonical JSON lặp lại ổn định, SHA-256 khớp `report_hash`, evaluation mode là diagnostic, completeness false và sealed holdout không xuất hiện.
- [x] Chạy test vừa viết và xác nhận RED do canonical serialization/mode chưa lộ trên report: `AttributeError` do chưa có `canonical_json`.
- [x] Refactor evaluator để một hàm canonical duy nhất tạo payload/hash; giữ metric, caveat và membership hiện có.
- [x] Chạy `PYTHONPATH=.:tests/unit:tests/e2e .venv/bin/python -m pytest --import-mode=importlib tests/unit/test_learning_fold_evaluation.py tests/unit/test_learning_fold_plan.py -q --tb=short` → **50 passed / 0.10s**.

### Task 2: Kho report bất biến và RLS

**Files:**
- Create: `src/trading_bot/adapters/database/models/learning_evaluation_entities.py`
- Modify: `src/trading_bot/adapters/database/models/__init__.py`
- Create: `src/trading_bot/adapters/database/repositories/learning_evaluation_reports.py`
- Create: Alembic migration sau head hiện hành
- Test: `tests/unit/test_learning_evaluation_report_repository.py`

**Interfaces:**
- `LearningEvaluationReportRepository.append(*, workspace_id, idempotency_key, report) -> LearningEvaluationReportWriteResult`.
- `LearningEvaluationReportRepository.get_by_idempotency_key(*, workspace_id, idempotency_key)`.
- Retry so canonical bytes, JSONB, report hash và denormalized lineage columns; lệch bất kỳ trường nào thì conflict.

- [x] Viết unit tests cho same-key/same-payload, same-key/changed-payload, wrong workspace, canonical tamper và fixed diagnostic/completeness state.
- [x] Chạy tests mới xác nhận RED vì model/repository chưa có.
- [x] Tạo table `learning_evaluation_report` với workspace-scoped idempotency, manifest FK, canonical payload/hash, evaluator/fold lineage, `evaluation_mode='diagnostic'`, completeness `false`, FORCE RLS, immutable trigger; admin-web chỉ SELECT và trainer-worker SELECT/INSERT.
- [x] Implement append/get repository với advisory transaction lock và kiểm toàn bộ canonical readback.
- [x] Chạy unit repository/model tests; PostgreSQL Compose `upgrade head`, `check`, `heads` xác nhận một head `f3a5c7d9e1b2`.

### Task 3: Service composition dùng snapshot read-only

**Files:**
- Create: `src/trading_bot/application/learning_evaluation.py`
- Test: `tests/unit/test_learning_evaluation_service.py`

**Interfaces:**
- `LearningEvaluationRequest(workspace_id, manifest_hash, folds, evaluation_cutoff, seed, idempotency_key)`.
- `LearningEvaluationReportService.evaluate(request) -> LearningEvaluationResult`.

- [x] Viết unit tests cho workspace/request validation, missing manifest, source-dependent rejection, same report result và failure trước persist khi evaluator lỗi.
- [x] Chạy tests vừa viết và xác nhận RED do service module chưa tồn tại.
- [x] Implement service: đọc manifest đã lưu trong UoW snapshot, dựng lại fold plan bằng manifest/split policy lưu, từ chối source-dependent, gọi `DatabaseFoldOutcomeVerifier` trên cùng session, thoát read transaction rồi append report qua UoW ghi riêng.
- [x] Không nhận manifest hoặc outcome payload từ caller; service chỉ nhận hash/fold parameters. Không để service truy vấn sealed holdout.
- [x] Chạy service/fold/repository/model unit regression: **76 passed**; full `tests/unit`: **2726 passed**.

### Task 4: PostgreSQL Compose E2E, traceability và handoff

**Files:**
- Modify: `tests/e2e/probes/wp06_fold_outcome_verifier.py`
- Modify: `tests/e2e/test_learning_fold_outcome_verifier.py` or create focused persistence E2E wrapper
- Modify: `docs/implementation/06-ai-learning.md`
- Modify: `docs/implementation/project-status.md`
- Modify: `docs/research/glossary-vi.md`
- Modify: `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md`
- Modify: `docs/implementation/implementation-memory.md`

- [x] Extend Compose probe to call real application service and check report persistence/readback/hash, retry, changed-payload conflict, holdout exclusion, trainer SELECT/INSERT boundary, admin SELECT-only, workspace isolation, immutable trigger and downgrade guard.
- [x] Run focused unit: evaluator, report repository, service (**75 passed**).
- [x] Run PostgreSQL Compose E2E: **1 passed / 27.53s**, including `alembic upgrade head`, `alembic check`, one head, trainer/admin privileges, workspace RLS, immutable mutation denial and rollback guard.
- [x] Run relevant Ruff/format/mypy and `git diff --check`; full unit regression **2726 passed / 14.93s**, no failure/skip.
- [x] Add EN→VI glossary entry `Diagnostic evaluation → Đánh giá chẩn đoán`, link to the spec/WBS and state it is not completeness, training or promotion evidence.
- [x] Update WP06 WBS/module/status/memory and read back the new memory entry against actual command outputs.
- [x] Review scoped diff for secrets and accidental edits; independent reviewer approved the refreshed diff with no Critical/Important findings. All unrelated dirty files remain untouched; no commit/push.
