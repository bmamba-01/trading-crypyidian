# WP-07C Backtest Jobs, CLI và Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement independent tasks. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cho phép owner tạo replay job từ bundle đã lưu, theo dõi/hủy job trong trang admin, và chạy job bền vững qua worker/CLI riêng.

**Architecture:** Admin xác thực và ghi yêu cầu vào bảng job theo workspace. Worker chuyên biệt claim bằng lease, nạp bundle và kiểm quyền hiện hành, chạy PAPER ngoài transaction, rồi ghi outcome/receipt nguyên tử qua orchestration service; job state được cập nhật bằng repository riêng. CLI dùng cùng application services để chạy worker hoặc vận hành job. UI chỉ hiển thị trạng thái do DB xác nhận.

**Tech Stack:** Python 3.14, FastAPI/Jinja, SQLAlchemy async, PostgreSQL RLS, Docker Compose, Typer/argparse hiện có hoặc `python -m` CLI, pytest, Playwright.

**Spec:** [Trader Workbench design](../specs/2026-11-03-trader-workbench-design.md), [WP-07 WBS](./2026-11-03-wp07-trader-workbench-wbs.md), [persisted replay bundle design](../specs/2026-11-03-wp07c-persisted-replay-bundle-design.md).

## Global Constraints

- Replay chỉ chạy PAPER runtime; không cấu hình Binance order adapter hoặc tạo lệnh thật.
- Bundle load và quyền hiện hành phải được kiểm trước replay và ngay trước persist; rights receipt của lần trước không thay thế quyền hiện hành.
- `crypidian_trainer_worker` không được SELECT manifest, bundle hoặc OHLCV; role replay mới chỉ nhận quyền cụ thể cần để nạp/ghi job.
- Job dùng lease/claim nguyên tử, kiểm tra cancel giữa các giai đoạn, trạng thái thất bại không để lại outcome một phần.
- UI, CLI help/error và comment/docstring hướng người vận hành viết bằng tiếng Việt; identifier/API viết tiếng Anh.
- Không stage/commit/push/reset/clean trong worktree đang có nhiều thay đổi của phiên khác.
- E2E browser phải chụp screenshot trang admin ở desktop và mobile; Compose E2E kiểm persistence/worker/rights boundary.

## Review Focus

- Hai worker cùng poll không thể thực thi một job đồng thời: test claim concurrency và lease reclaim.
- Hủy job không xóa audit/outcome, không thể hủy transaction persist đang commit: test cancel ở QUEUED/RUNNING và race với completion.
- Workspace khác không thể xem/hủy/claim job: test RLS dưới role admin/replay worker.
- Quyền source bị revoke sau khi replay chặn ghi outcome: giữ regression orchestration đã có và test worker integration.
- Job lỗi/cancel/retry không để lại outcome một phần hoặc chạy lệnh sàn: kiểm trạng thái transaction, executor PAPER-only và privilege role.

## Interfaces

- `BacktestReplayJobSubmission`: `bundle_hash`, `strategy_release_id`, `execution_interval`, `starting_equity`, `per_trade_risk_fraction`, `entry_fee_rate`, `exit_fee_rate`, `slippage_rate`, `seed`.
- `BacktestReplayJobService.submit(principal, request)`, `.list(principal)`, `.cancel(principal, job_id)`.
- Job view: `id`, `status`, `stage`, `progress`, bundle/strategy/timeframe, create/update timestamps, `error_code`, `result_run_ids`, `cancel_requested`.
- Admin routes: `GET /admin/backtests`, `POST /admin/backtests/submit`, `POST /admin/backtests/{job_id}/cancel`.
- Worker claims one job at a time, reports stable stage/progress, checks cancellation before replay and before persistence, and persists terminal state with result IDs/error code.

## Files and Tasks

### Task 1: Durable job persistence and application service

**Files:** new Alembic migration, SQLAlchemy model, repository, application service, unit tests; role provisioning/settings/Compose files only where needed for a dedicated worker identity.

- [x] Add RLS-scoped durable job table with constrained status/progress, lease/claim fields, cancellation request, stable payload and result/error fields.
- [x] Grant admin-web only the job operations needed to enqueue/list/cancel; grant the dedicated replay worker claim/read/update access, preserving trainer-worker restrictions.
- [x] Add atomic claim with `FOR UPDATE SKIP LOCKED`, lease expiry/reclaim, workspace binding, cancellation, progress, success/failure transitions, and tests for concurrent claims, tenant isolation, cancellation, invalid transition, retry and terminal state.
- [x] Define/validate the immutable submission and job view DTOs in `application/backtest_replay_jobs.py`; owner-only create/cancel and workspace-scoped list.
- [x] Run focused unit tests and migration/role E2E before integrating worker.

### Task 2: Worker runtime and CLI

**Files:** new worker and composition modules, CLI module, app lifecycle/settings/process role, Compose worker service, DB provisioner, focused unit/Compose tests.

- [x] Build replay executor from the pinned strategy release and explicit job profile; create deterministic `ReplayManifest` and known PAPER account/risk/cost seed; reject unsupported or unavailable release/context before running.
- [x] Compose loader and outcome repository through role-specific UoWs; verify the role cannot access order/fill/trade execution APIs and trainer-worker still cannot read bundles/manifests.
- [x] Add worker poll/claim/lease/heartbeat/progress/cancel/failure behavior; no long-lived DB transaction during replay.
- [x] Add CLI commands to process jobs and inspect/cancel by workspace/job id; validate arguments and print Vietnamese status without secrets.
- [x] Add a Compose E2E that enqueues via admin service, runs the actual worker against a persisted bundle under PostgreSQL roles, checks outcomes/receipts, cancel/failure and role denials.

### Task 3: Admin UI

**Files:** new replay router/template and focused unit tests; modify only admin navigation/CSS and app router/service wiring.

- [x] Add Vietnamese owner-only form to choose bundle hash, published strategy release, execution timeframe, starting equity, risk, fees/slippage and seed.
- [x] Show jobs with progress, stage, status, timestamps, results and safe Vietnamese error codes; allow cancellation only before terminal states.
- [x] Keep forms CSRF-protected, workspace scoped, no-store, autoescaped, responsive and keyboard/touch usable; reject requests when service is absent.
- [x] Run route/view unit tests and responsive Playwright workflow.

### Task 4: Browser screenshots, regression and memory

**Files:** new Playwright test and evidence screenshots, WP-07C WBS, project status, implementation memory.

- [x] Playwright submit → queued/running/success and a second cancellation path; assert no horizontal overflow at 1280×900 and 390×844, controls ≥44px, Vietnamese labels and save full-page desktop/mobile screenshots.
- [x] Run targeted unit and Compose E2E, migrations/head/check, Ruff/format/type checks for changed files; investigate every failure.
- [x] Update/read back implementation memory and WP-07C status with exact command output and remaining limitations.

## Verification results (2026-11-04)

- CLI, replay executor, execution service, admin routes and adjacent replay regressions: **141 passed**.
- `tests/e2e/test_wp07_rest_backfill_persistence.py`: **1 passed**; includes Alembic upgrade/check, real PostgreSQL role boundaries, actual service enqueue, worker persistence/rights receipts, cancel-before-persist, concurrent claim, lease reclaim/stale-worker fencing, workspace RLS, queued cancellation and retry exhaustion.
- `tests/e2e/test_backtest_admin_ui.py`: **1 passed**; Playwright observes queued → running (65%) → success, then a second job cancellation, with screenshots at 1280×900 and 390×844 and controls at least 44px.
- Ruff, format, mypy on eight production modules, Python compilation, `docker compose config --quiet`, CLI help and `git diff --check` passed for this slice.
- Remaining system limits: this slice adds the worker loop and `--once` CLI, not a scheduler; PostgreSQL RLS is workspace GUC-scoped and assumes trusted application code sets the configured workspace. A worker paused after the last lease check during a long persist transaction is retried idempotently, but the lease is not a hard fencing token held for the full DB transaction.
