# WP-07C Replay DecisionSnapshot Builder Implementation Plan

> **For agentic workers:** Dùng test-first cho từng task; triển khai trực tiếp theo yêu cầu tiếp tục của người dùng.

**Goal:** Tạo `ReplayObservation` point-in-time từ replay bundle đã xác minh để strategy dùng đúng bars/features có sẵn tại mỗi decision time.

**Architecture:** Tách cấu hình timeframe, feature requirement và feature version dùng chung giữa signal producer và replay. Builder lấy decision bars theo entry timeframe của release; tại mỗi mốc khả dụng chỉ lọc bars đã đóng và đã available, tính current/previous features, chất lượng chuỗi và quote volumes, rồi tạo snapshot/event ID xác định. Không đọc DB, sửa bundle, chạy evaluator hoặc tự dựng trạng thái vị thế/macro.

**Tech Stack:** Python 3.14, `FeatureCalculator`, `DecisionSnapshot`, `ReplayObservation`, WP-07C replay bundle.

**Spec:** [WP-07 trader workbench WBS](./2026-11-03-wp07-trader-workbench-wbs.md), WP-07C mục 6.

## Global Constraints

- Bars/features chưa `available_at` tại decision time không được có trong snapshot.
- Chỉ nến đóng và bundle `READY` đã được loader kiểm rights/hash mới được dùng.
- Không resample/nội suy; quote volume thiếu giữ `None` để H03 abstain.
- Thiếu position-state hoặc macro evidence thì giữ `None`; không mặc định flat/no-blackout.
- Không thêm dependency, migration, lệnh/trade, network I/O hoặc ghi DB.

## Review Focus

- Nến timeframe cao chưa đóng tại mốc entry không được lọt vào snapshot — kiểm tra bằng H01 1h/4h.
- Event đến trễ không được giả vờ có sẵn tại event time — decision time lấy từ `available_at` và quá biên range thì từ chối.
- Gap/stale/warm-up phải giữ quality fail-closed — kiểm tra metadata series/features.
- Thiếu quote volume không được ước lượng — kiểm tra `MarketSeries.quote_volumes is None`.
- Position/macro không có provider không được giả định an toàn — kiểm tra trường snapshot vẫn `None`.

## Files

- Create: `src/trading_bot/application/strategy_market_requirements.py` — cấu hình feature, entry timeframe và settings theo release dùng chung.
- Modify: `src/trading_bot/application/market_signal_composition.py`, `market_signal_scanner.py` — dùng chung cấu hình, không đổi hành vi live.
- Create: `src/trading_bot/application/backtest_dataset_replay_observations.py` — chuyển bundle thành tuple observation xác định.
- Test: `tests/unit/test_backtest_dataset_replay_observations.py` — bundle fixture H01 đa khung và case no-lookahead/quality/missing context.

## Interfaces

```python
def build_backtest_dataset_replay_observations(
    bundle: BacktestDatasetReplayBundle,
    *,
    strategy_release_id: str,
    position_is_flat_at: Callable[[datetime], bool | None] | None = None,
) -> tuple[ReplayObservation, ...]: ...
```

Mốc quyết định lấy theo `available_at` của nến entry đã đóng; observation `event_at` giữ `close_time`. Feature requirement và entry timeframe chỉ lấy từ cấu hình release đã biết; release chưa hỗ trợ bị từ chối.

## Tasks

- [x] Viết RED cho H01 đa khung, lược bỏ nến H4 chưa khả dụng, event trễ, release không hỗ trợ, quote volume thiếu và position/macro chưa biết.
- [x] Tách cấu hình timeframe/feature/version để signal producer và replay builder dùng cùng một nguồn.
- [x] Thêm builder xác định feature current/previous, quality, event/snapshot ID, decision time và quote volume từ bundle bars đã qua loader.
- [x] Chạy focused/full unit, signal producer Compose E2E, Ruff/format, mypy, Alembic head và `git diff --check`.
- [x] Cập nhật WBS/status/glossary/memory, đọc lại memory và xác nhận phần evaluator/context còn mở.

## Chưa gồm

Production context provider cho position/account/risk, macro calendar, `RuntimeReplayEvaluator`/`BacktestRunner` composition, fee/funding/fill assumptions, persistence/report, CLI/scheduler và raw payload retention.
