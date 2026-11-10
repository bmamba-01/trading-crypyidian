# WP-07C Persisted Replay Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nối bundle WP-07C đã lưu thành use-case replay có kiểm quyền ở lúc load và trước persist, chạy PAPER runtime ngoài transaction dài, rồi lưu toàn bộ run/outcome/rights receipt nguyên tử.

**Architecture:** Application service dùng hai Unit of Work factory: admin-web nạp bundle/manifests/bars và kiểm quyền; PAPER runtime chạy ngoài DB transaction bằng worker thread; trainer-worker mở transaction mới để revalidate policy hiện hành rồi append mọi child outcome/receipt nguyên tử. Worker không đọc bundle/manifest. Receipt đầu tiên bất biến; retry cùng trial giữ receipt cũ khi evidence policy-bound không đổi dù lease timestamp mới.

**Tech Stack:** Python 3.14, SQLAlchemy async, PostgreSQL, `BacktestDatasetReplayBundleLoader`, `RuntimePaperReplayComparison`, `BacktestOutcomeRepository`, pytest, Docker Compose.

**Spec:** [Trader Workbench WBS](./2026-11-03-wp07-trader-workbench-wbs.md), [thiết kế persisted replay bundle](../specs/2026-11-03-wp07c-persisted-replay-bundle-design.md), WP-07C mục 6.

## Global Constraints

- Mọi load/replay mới phải qua `BacktestDatasetReplayLoader` và rights gate hiện hành; không dùng receipt lịch sử thay quyền hiện tại.
- DB transaction không được giữ trong lúc runner xử lý dữ liệu; mọi sensitivity child được persist trong một transaction.
- Caller cung cấp runtime manifest/settings/context rõ ràng; không tự dựng account/risk/positions, không gửi Binance order.
- Dữ liệu E2E phải là fixture offline; không gọi Binance/Telegram hoặc dùng Binance Vision thiếu grant.
- Tài liệu và thông báo lỗi tiếng Việt; identifier tiếng Anh; thuật ngữ trading/AI mới phải có glossary EN→VI kèm nguồn.
- Không commit/push, sửa stage, reset hay dọn các thay đổi không thuộc lát này trong shared worktree.

## Review Focus

- Policy bị revoke trong lúc replay: pha persist phải từ chối và rollback mọi child outcome.
- Retry cùng deterministic trial sau thời điểm DB clock đổi: quyền được revalidate, receipt đầu giữ nguyên, không conflict vì lease timestamps mới.
- Cùng trial nhưng report/scenario/trade/policy-bound receipt khác: phải conflict, không ghi đè.
- Bundle hash, workspace hoặc evaluation window lệch manifest: executor không được gọi.
- Lỗi khi append child sau: rollback report, scenario, trade outcomes và receipt của toàn lượt.

## Files

- Create: `src/trading_bot/application/backtest_dataset_replay_execution.py` — service, Unit of Work protocol và kết quả orchestration.
- Modify: `src/trading_bot/application/backtest_dataset_replay.py` — tái kiểm quyền từ input đã nạp mà không đọc lại OHLCV.
- Modify: `src/trading_bot/application/backtest_dataset_replay_bundle_loader.py` — tái kiểm quyền toàn bundle và giữ nguyên hash.
- Modify: `src/trading_bot/adapters/database/repositories/backtest_outcomes.py` — giữ receipt gốc khi retry đúng cùng run, đồng thời xác minh canonical/hash và policy-bound scope.
- Modify: `tests/unit/test_backtest_dataset_replay_bundle_persistence.py`, `tests/unit/test_backtest_outcomes_repository.py`; create `tests/unit/test_backtest_dataset_replay_execution.py`.
- Modify: `tests/fixtures/runtime_replay.py` — cho phép chạy fixture H01 trên bundle PostgreSQL đã nạp.
- Modify: `tests/e2e/probes/wp07_backfill_persistence.py`, `tests/e2e/test_wp07_rest_backfill_persistence.py` — Compose path persisted bundle → runtime → outcome/receipt, retry, revoke/fail-closed.
- Update: persisted bundle design, WP-07 WBS, project status, implementation memory.

## Tasks

### Task 1: Rights revalidation và receipt retry semantics

- [x] Viết RED cho revalidate trên bundle đã load, từ chối fingerprint đổi/revoke và receipt retry có `checked_at`/`valid_until` khác nhưng giữ nguyên receipt cũ.
- [x] Refactor quyền child thành API public; bundle loader tạo bundle mới từ evidence mới và yêu cầu dataset hash không đổi.
- [x] Với deterministic run duplicate, xác minh integrity/hash/canonical và policy-bound fields của receipt cũ; bỏ qua thời gian lease mới nhưng không update row. Run mới và receipt legacy vẫn theo semantics cũ.
- [x] Chạy unit test rights loader/outcome repository; kiểm tra failing assertion trước implementation và kết quả GREEN.

### Task 2: Service load → replay → persist

- [x] Viết RED cho thứ tự load → replay → persist, executor chạy ngoài transaction, manifest mismatch chặn executor, quyền bị revoke sau replay chặn append, và mọi run persist cùng UoW.
- [x] Tạo `BacktestDatasetReplayExecutionService.execute(...)` nhận workspace, bundle hash, ReplayManifest và executor; tách UoW load và persist để giữ boundary quyền admin-web/trainer-worker.
- [x] Persist từng `RuntimePaperReplayRun` qua `BacktestOutcomeRepository` port với bundle evidence vừa revalidate; trả run/report/scenario identifiers.
- [x] Unit service **4 passed**; nhóm loader/outcome/runtime fixture **83 passed**; Ruff/format và mypy file sản xuất đạt.

### Task 3: PostgreSQL Compose E2E

- [x] Seed policy/manifests/bars/bundle; chạy service với loader/rights gate/outcome repository PostgreSQL thật; load dưới `crypidian_admin_web`, persist dưới `crypidian_trainer_worker`.
- [x] Chạy H01 runtime trên bundle được load lại từ DB; kiểm report/scenario, số trade rows khớp trade outcomes runtime và đủ receipts cho mỗi child. Retry sau đổi clock giữ counts, IDs và canonical/hash/timestamp receipt gốc.
- [x] Fault injection sau lần append thứ hai chứng minh rollback thật; revoke sau persist chặn load tiếp theo, không gọi executor và không đổi outcome counts.
- [x] Compose E2E `tests/e2e/test_wp07_rest_backfill_persistence.py` **1 passed / 14.12s**, có `alembic check`; regression `tests/e2e/test_backtest_outcomes_persistence.py` **1 passed / 27.18s**; full unit **2607 passed / 13.71s**.

### Task 4: Tài liệu và hoàn tất lát

- [x] Cập nhật design decision retry, WBS/status và implementation memory; ghi đúng commands/output/lỗi/còn mở.
- [x] Đọc lại memory và docs vừa sửa, đối chiếu source/test output, `git diff --check`; không claim CLI, scheduler, progress/cancel hoặc dashboard đã có.
