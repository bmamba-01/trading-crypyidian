# WP-07C Backtest Dataset Manifest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tạo manifest bất biến kiểm tra quyền, warm-up, dữ liệu nến và độ phủ trước khi bộ dữ liệu lịch sử được phép đưa vào backtest.

**Architecture:** Dùng `RestBackfillRequest` hiện có làm phạm vi tải, `BackfillRightsEvidence` làm bằng chứng đã được rights gate duyệt, và `OHLCVBar` đã lưu làm đầu vào. Manifest là hợp đồng thuần, băm dữ liệu chuẩn hóa, ghi rõ gap; chỉ trạng thái đủ độ phủ mới cho phép caller tiếp tục sang replay. Vòng này chưa thêm database persistence, network fetch hoặc UI.

**Tech Stack:** Python 3.14, dataclasses, `hashlib`, `pytest`; không thêm dependency.

**Spec:** [WP-07 trader workbench](./2026-11-03-wp07-trader-workbench-wbs.md), mục WP-07C.

## Global Constraints

- Quyền backtest phải đến từ bằng chứng được cấp rõ mục đích `BACKTEST_RESEARCH`, còn hiệu lực tại thời điểm tạo manifest; thiếu/sai/expired thì từ chối.
- Khoảng thời gian UTC là nửa mở; warm-up nằm trước evaluation range và không được tính nhầm vào hiệu suất.
- Chỉ nhận nến đã đóng, đúng symbol/interval/source, đã available tại `as_of`; không nội suy gap.
- Identifiers tiếng Anh; tài liệu/comment/docstring tiếng Việt; ghi thuật ngữ mới EN→VI vào glossary.
- Không tải data thật, gửi lệnh, gọi Telegram hoặc thay đổi database trong task này.

## Review Focus

- Rights evidence hết hạn, sai provider/purpose hoặc chưa hiệu lực phải bị từ chối.
- Nến chưa đóng, chưa available, khác source/symbol/timeframe hoặc ngoài range phải bị từ chối.
- Nến thiếu phải tạo manifest `INCOMPLETE` với timestamp gap; manifest đó không thể chuyển sang replay.
- Hash phải đổi khi OHLCV, range, warm-up hoặc policy fingerprint đổi.
- Bar trùng hoặc không theo lưới thời gian phải bị từ chối, không bị âm thầm gộp.

### Task 1: Build and validate historical backtest dataset manifests

**Files:**
- Create: `src/trading_bot/application/backtest_dataset_manifest.py`
- Modify: `src/trading_bot/application/binance_rest_backfill.py` only if a public interval accessor is needed.
- Test: `tests/unit/test_backtest_dataset_manifest.py`
- Modify: `docs/superpowers/plans/2026-11-03-wp07-trader-workbench-wbs.md`, `docs/implementation/project-status.md`, `docs/research/glossary-vi.md`, `docs/implementation/implementation-memory.md`.

**Interface:** `build_backtest_dataset_manifest(request, *, evaluation_start, warmup_bars, bars, rights_evidence, prepared_at) -> BacktestDatasetManifest`; the result contains dataset hash, request/policy fingerprints, coverage counts, exact gap timestamps and `require_ready()`.

- [x] Write failing tests for a complete warm-up + evaluation window, exact gap reporting, evidence denial/expiry, wrong bar identity/range/freshness/closed state, duplicate/out-of-grid bars, manifest hash sensitivity and `require_ready()` denial.
- [x] Run only the new tests and verify failure comes from missing behavior.
- [x] Implement the immutable manifest and canonical SHA-256 using the existing request interval and rights evidence contracts.
- [x] Re-run the focused tests, then all unit tests, targeted WP-07 REST persistence E2E, Ruff, mypy and `git diff --check`.
- [x] Update WBS/status/glossary/memory with exact evidence and read the new memory entry back.

**Review:** Đọc lại độc lập không tìm thấy blocker trong coverage/hash/range/source validation. Reviewer chỉ ra hai follow-up: current rights recheck tại replay và giới hạn `warmup_bars` trước phép nhân `timedelta`; cả hai đã được xử lý trong [rights gate](./2026-11-03-wp07c-source-registry-backfill-rights.md) và [persistence/revalidation](./2026-11-03-wp07c-manifest-persistence-revalidation.md).

**Deferred:** complete backtest evaluator/composition, CLI/scheduler, funding and execution assumptions, raw response retention, and real provider execution remain separate WP-07C tasks. Manifest persistence and current-rights revalidation were added in [the follow-up plan](./2026-11-03-wp07c-manifest-persistence-revalidation.md); the current Source Registry adapter was added in [the rights-gate plan](./2026-11-03-wp07c-source-registry-backfill-rights.md).
