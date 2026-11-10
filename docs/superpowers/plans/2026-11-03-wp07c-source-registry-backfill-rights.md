# WP-07C Source Registry Backfill Rights — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nối quyền nạp bù Binance USD-M REST với policy registry đã quan sát trong đúng workspace, và tách quyền nghiên cứu backtest khỏi quyền PAPER demo.

**Architecture:** Thêm hai mục đích có tên riêng vào domain + PostgreSQL checks. `SourceRegistryBackfillRightsGate` đọc chuỗi policy/observer receipt và DB clock; chỉ phát rights evidence cho purpose được duyệt, policy còn hiệu lực và evidence HTTPS. Evidence có lease tối đa 5 phút, còn fingerprint giữ ổn định theo policy để checkpoint có thể resume. Không có policy hoặc receipt mặc định cho phép.

**Tech Stack:** Python 3.14, SQLAlchemy/PostgreSQL, Alembic, pytest; không thêm dependency.

**Spec:** [WP-07 trader workbench](./2026-11-03-wp07-trader-workbench-wbs.md), WP-07C mục 1 và 8; [backtest manifest](./2026-11-03-wp07c-backtest-dataset-manifest.md).

## Global Constraints

- `BACKTEST_RESEARCH` và `PAPER_DEMO` là hai quyền độc lập; không cấp mặc định.
- Chỉ policy hiện hành đã verified, đã có observer receipt và được phép đúng mục đích mới cấp evidence.
- Thời điểm kiểm tra quyền lấy từ PostgreSQL; gate giữ source lock trong transaction chứa trang và cursor để policy không đổi giữa authorization/persist.
- Connector vẫn offline/fixture; không tải lịch sử thật hoặc tạo lệnh.
- Comment/docstring/tài liệu tiếng Việt; identifier tiếng Anh; thuật ngữ mới phải vào glossary.

## Review Focus

- Nguồn thiếu policy/receipt, policy pending/revoked/expired hoặc purpose không khớp phải từ chối.
- Policy valid_until gần hạn không được cấp evidence sống lâu hơn policy; policy không có hạn vẫn chỉ được cấp lease ngắn.
- Clock của caller không được dùng thay DB clock khi ra quyết định quyền.
- Migration downgrade từ chối nếu purpose mới đã được lưu.
- `warmup_bars` lớn hơn phạm vi phải trả lỗi domain trước khi nhân timedelta.

### Task 1: Add backfill rights purpose and current registry adapter

**Files:** domain `source_policy.py`, domain `source_registry.py`, database models/repository, a new Alembic migration, application `binance_rest_backfill_rights.py`, unit/migration/E2E tests, glossary, WP-07C WBS/status/memory.

**Interface:** `SourceRegistryBackfillRightsGate(workspace_id, repository, source_id="BINANCE_USDM_REST")` implements `BackfillRightsGate.authorize(request, now)`. It returns `BackfillRightsEvidence` bound to the current revision `source_rights_hash`, HTTPS `evidence_ref`, the requested purpose, and a lease ending at the earlier of policy expiry or database time + 5 minutes.

- [x] Write failing unit tests for allow, exact-purpose denial, missing receipt, stale/revoked/expired policy, DB-clock authority, terms URL rejection, and short evidence lease.
- [x] Write failing tests for new database purpose checks and downgrade preservation guard.
- [x] Bound `warmup_bars` by request.expected_bars before interval arithmetic.
- [x] Add repository DB-clock read and application rights gate using the domain observed-policy contract.
- [x] Add migration to expand policy/quota-purpose checks; downgrade blocks if new purpose is persisted.
- [x] Extend Compose persistence E2E to use registry-backed gate with fixture provider, verify DB-observed policy fingerprint and downgrade guard; no network request.
- [x] Run focused tests, full unit suite, targeted Compose E2E, Ruff, format, mypy, Alembic check and `git diff --check`.
- [x] Update WBS/status/glossary/memory and read the new memory entry back.

**Deferred:** production deployment wiring, real data download, CLI/scheduler, and complete backtest runner. Manifest persistence and replay-time rights revalidation are tracked in [the follow-up plan](./2026-11-03-wp07c-manifest-persistence-revalidation.md).
