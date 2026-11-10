# WP-07C Sequential Runtime Context Implementation Plan

> **For agentic workers:** Dùng test-first cho từng thay đổi; không commit khi chưa có unit/E2E evidence đạt.

**Goal:** Nối replay bundle đa timeframe với `RuntimeReplayEvaluator` bằng context được phát tuần tự theo thời gian, state đầu vào khai báo rõ, và execution event/ATR đúng mốc khả dụng.

**Architecture:** Provider mới nhận bundle đã kiểm rights/hash, observations đã tạo và một context seed được caller xác nhận. Provider chỉ phát execution bars mới có `available_at <= decision_time`; evaluator tiến PAPER reducer trước mỗi quyết định, tự tính trạng thái flat theo exposure/order/position sau event và tạo snapshot ID mới theo trạng thái runtime. ATR và opposing-signal được gắn theo từng execution bar; thiếu dữ liệu thoát mà recipe yêu cầu thì replay bị từ chối thay vì coi là `False`.

**Tech Stack:** Python 3.14, `BacktestDatasetReplayBundle`, `ReplayObservation`, `PaperBar`, `RuntimeReplayEvaluator`, `PaperExecutionGateway`.

**Spec:** [WP-07 Trader Workbench WBS](./2026-11-03-wp07-trader-workbench-wbs.md), WP-07C mục 6.

## Global Constraints

- Không tự dựng account, equity, margin, exposure, trạng thái flat hay macro evidence; caller phải truyền state gốc đã biết.
- Context provider/evaluator phải xử lý observations theo thứ tự khả dụng và giữ nguyên mọi biên thời gian nửa mở.
- Mỗi execution bar chỉ được áp dụng một lần, sau khi đóng và đã available; không nội suy, resample hay dùng ATR/tín hiệu phát sinh sau bar.
- Context thiếu opposing signal/ATR mà exit recipe cần phải từ chối trial, không biến `unknown` thành `False`.
- Không phát Binance order; replay dùng gateway PAPER cô lập từng scenario và không ghi DB từ provider.

## Files

- Create: `src/trading_bot/application/backtest_dataset_replay_context.py` — chuyển execution component thành context provider stateful, tuần tự và point-in-time.
- Modify: `src/trading_bot/application/runtime_replay_evaluator.py` — nhận metadata theo event, dựng position state và ID snapshot runtime xác định.
- Create: `tests/unit/test_backtest_dataset_replay_context.py` — thứ tự/cursor, no-lookahead, explicit seed, ATR và opposing signal theo bar.
- Modify: `tests/unit/test_runtime_replay_evaluator.py` — position-flat sau event và fail-closed khi thiếu context thoát.
- Reuse: `tests/e2e/test_backtest_outcomes_persistence.py` — PostgreSQL persistence E2E qua runtime replay fixture; no schema change trong lát này.
- Update: WP-07C WBS, project status, implementation memory và glossary.

## Tasks

- [x] Bổ sung regression cho event availability/cursor, provider order, seed không phẳng, ATR/opposing theo bar, runtime flat và trial gateway/provider isolation. Lưu ý: implementation bắt đầu trước khi plan riêng này xuất hiện trong continuation trước đó, nên bộ test này là regression coverage, không ghi nhận RED-first cho cả lát.
- [x] Tạo provider từ execution interval được chọn; chuyển `OHLCVBar` thành `PaperBar`, phát event mới theo `available_at`, cập nhật `evaluated_at`/`RiskState` mà giữ nguyên account capture time.
- [x] Tính ATR gần nhất đã khả dụng cho từng execution bar từ `ReplayObservation`; callback opposing-signal chỉ nhận observation không sau event và không có fallback `False`.
- [x] Sửa evaluator để áp metadata theo event, kiểm tra recipe requirements, tính `position_is_flat` từ exposure/open order/open position và hash lại runtime snapshot ID.
- [x] Nối bundle → observation builder → context provider → evaluator → `BacktestRunner`; mỗi scenario có gateway/provider riêng, manifest/window/workspace phải khớp, H06 vẫn unknown khi thiếu macro.
- [x] Chạy focused/full unit, runtime evaluator persistence Compose E2E, Ruff/format/mypy và `git diff --check`; không đổi schema nên không chạy Alembic. Cập nhật docs/glossary/memory và đọc lại.

## Review Focus

- Nến 1m có `available_at` sau quyết định không được fill/monitor vị thế.
- ATR 1h chỉ được dùng từ thời điểm feature đã available; không lan ngược vào các nến 1m trước đó.
- Tín hiệu opposing dùng ở event phải có thời điểm không sau event; thiếu giá trị không tương đương không có tín hiệu.
- Snapshot ID không được giữ nguyên khi account/exposure/flat state runtime khác nhau.
- Initial exposures không có `PaperPosition` thực thi phải được từ chối hoặc có nguồn khôi phục tương ứng.

## Chưa gồm

Macro calendar/release ingestion, opposing-signal producer đầy đủ, initial position/order restoration, equity mark-to-market theo từng nến, orchestration persistence cho run dataset mới, API/CLI và dashboard progress/cancel. Compose E2E đã chạy runtime evaluator/persistence fixture có sẵn; chưa có E2E riêng cho bundle → provider → runner.
