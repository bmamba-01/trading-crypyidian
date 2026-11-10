# WP-07C Bundle replay đa timeframe

> **For agentic workers:** Dùng `superpowers:executing-plans` và test-first cho từng task.

**Goal:** Ghép các replay input đã được load/rights-check riêng thành một dataset bundle bất biến, để strategy đa timeframe không vô tình dùng bars chưa có manifest hoặc quyền riêng.

**Architecture:** `build_backtest_dataset_replay_bundle(inputs)` nhận một hoặc nhiều `BacktestDatasetReplayInput` đã được loader kiểm quyền, kiểm `READY` và current-rights workspace/provider/purpose/fingerprint, yêu cầu symbol/workspace/evaluation window/policy lineage khớp và timeframe không trùng. Mỗi child giữ warm-up/range start riêng; các child phải cùng `evaluation_start` và `range_end`. Bundle sắp xếp components theo interval, giữ tuple bất biến, và tạo SHA-256 canonical trên symbol, shared scope, evaluation range và cặp `(interval, child_dataset_hash)`. Không resample, nội suy, đọc DB hoặc gọi evaluator trong module này.

**Files:** tạo `src/trading_bot/application/backtest_dataset_replay_bundle.py`; tạo `tests/unit/test_backtest_dataset_replay_bundle.py`; cập nhật WBS/status/memory/glossary. Không migration/dependency.

## Tasks

- [x] Viết unit RED cho bundle 1m+1h ổn định dù đảo input, cùng bundle một timeframe; mismatch workspace/symbol/evaluation window/rights lineage; timeframe trùng; input chưa READY.
- [x] Tạo immutable `BacktestDatasetReplayBundle` và builder với canonical composite hash.
- [x] Chạy focused/full unit, Ruff/format, mypy và `git diff --check`.
- [x] Cập nhật WBS/status/memory/glossary, đọc lại memory.

## Chưa gồm

Dựng `DecisionSnapshot` point-in-time, feature lookback đa timeframe, PAPER context/account state, evaluator/runner composition, persistence của bundle run, CLI/scheduler, raw archive/reference và tải dữ liệu thật. Những phần này cần tiếp tục theo từng lát cắt riêng để kiểm soát no-lookahead và fill assumptions.
