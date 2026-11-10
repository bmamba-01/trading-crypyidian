# WP-07C Replay bars pinned integrity

> **For agentic workers:** Dùng `superpowers:executing-plans` và TDD cho từng task.

**Goal:** Khi mở manifest cho replay, trả đúng tập bar REST backfill đã tạo snapshot đó, đọc theo `prepared_at`, kiểm coverage và tính lại content hash; sai/thiếu dữ liệu thì fail closed.

**Architecture:** Replay loader nhận `MarketDataRepository` cùng repository manifest và rights gate. Sau current-rights check, nó tải source `BINANCE_USDM_REST_BACKFILL` trong range nửa mở tại `manifest.prepared_at`; so bar count/hash với manifest trước khi trả một `BacktestDatasetReplayInput` gồm manifest, bars và rights lease hiện hành. Không chạy evaluator trong lát cắt này.

**Tài liệu:** [WBS WP-07](./2026-11-03-wp07-trader-workbench-wbs.md), [manifest persistence/revalidation](./2026-11-03-wp07c-manifest-persistence-revalidation.md).

## Tasks

- [x] Viết unit RED cho cùng dữ liệu, thiếu bar, bar bị sửa, source/timeframe khác, và request `as_of` đúng `prepared_at`.
- [x] Xuất helper hash bar chuẩn để builder và replay loader dùng chung.
- [x] Thêm `BacktestDatasetReplayInput` và buộc replay loader đọc bars/so hash sau rights gate.
- [x] Thêm filter source tường minh cho `MarketDataRepository.list_bars`.
- [x] Mở rộng Compose E2E kiểm bar readback tại snapshot time và trả đúng replay input.
- [x] Chạy focused/full unit, E2E, Ruff/format, mypy và `git diff --check`.
- [x] Cập nhật WBS/status/memory và đọc lại memory.

**Chưa gồm:** evaluator, fee/funding/fill assumptions, CLI/scheduler, raw payload retention và tải dữ liệu thật.
