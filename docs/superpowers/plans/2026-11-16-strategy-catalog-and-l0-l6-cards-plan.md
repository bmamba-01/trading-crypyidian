# Kế hoạch Triển khai Mở rộng Danh mục Chiến lược & Động cơ Pipeline L0–L6 (2026-11-16)

## 1. Mục tiêu
1. Khắc phục tình trạng hiện chỉ có 2 Strategy Card ở `/admin/strategy-cards` (trong đó thẻ #2 có nút chết và chưa bật vào `PRODUCT_CARD_IDS`) và thiếu các chiến lược định lượng truyền thống/nâng cao trong `strategy_registry()` để đưa vào chạy Backtest (`/admin/backtests`).
2. Triển khai đầy đủ, chuẩn toán học point-in-time (zero look-ahead) các chiến lược từ khảo sát học thuật & thực nghiệm ([strategies-expansion-2026-11-16.md](../../research/strategies-expansion-2026-11-16.md)):
   - **6 Rule-based Quantitative Releases** trong module tách biệt `src/trading_bot/domain/strategy/expanded_rules.py`:
     - `h02-v1` (*Time-Series Momentum đa chân trời + lọc biến động RV*, `4h`)
     - `h04-v1` (*Bollinger–Keltner Volatility Squeeze Breakout*, `15m`)
     - `h05-v1` (*EMA Trend Pullback + RSI Regime Continuation*, `1h` + `4h`)
     - `h07-v1` (*Intraday Session VWAP & ATR Deviation Reversion*, `5m`)
     - `h21-v1` (*MACD Histogram + ADX Regime Confirmation Momentum*, `1h` + `4h`)
     - `h22-v1` (*Taker Order-Flow Imbalance & Liquidity Breakout*, `15m` + `1h`)
   - **Nâng cấp Pipeline Engine L0–L6 & hoàn thiện/mở rộng lên 8 Strategy Cards** trong `build_default_demo_cards()` + `PRODUCT_CARD_IDS`:
     - Giữ nguyên nguyên vẹn `SC-BINANCE-USDM-VOL-BREAKOUT-001` (`v1.0.0`, giữ 100% test & hash hành vi hiện có).
     - Sửa nút mồ côi (`l1_body`), bổ sung `l2_atr`, hoàn thiện L2 EMA / L3 Z-Score / L4 Context / L5 `wick_reversal_trigger`, nâng phiên bản lên `v1.2.1` cho `SC-BINANCE-USDM-MEAN-REVERT-002` và bật vào `PRODUCT_CARD_IDS`.
     - Thêm 6 Strategy Card L0–L6 mới ánh xạ chính xác các mô hình truyền thống & nâng cao trên (`003` TSMOM, `004` BB–KC Squeeze, `005` Trend Pullback, `006` Session VWAP Reversion, `007` MACD+ADX Momentum, `008` Taker-Flow Imbalance Breakout).
3. Kích hoạt kiểm định trực giao thực sự (`AntiConsensusAnalyzer`) trong `StrategyCardAdminService._card_to_view` trên chuỗi chuẩn đa dạng chế độ, không còn gán cứng `has_consensus_warning = False`.
4. Khai báo đầy đủ `FEATURES_BY_RELEASE`, `ENTRY_TIMEFRAME_BY_RELEASE`, `SETUP_RULES` và viết toàn bộ unit tests + integration replay tests + Playwright E2E + kiểm tra visual PNG.

## 2. Ràng buộc bất biến (Invariants)
- **KHÔNG sửa** `src/trading_bot/domain/strategy/rules.py`, `rules_v2.py` (`BASE_RULES_SOURCE_SHA256`), `scalping_rules.py`, hay danh sách `_BUILT_INS` của `StrategyRegistry()`.
- **KHÔNG thay đổi** `_feature_names` hay `feature_schema_hash()` trong `feature_snapshot.py`. Mọi release mới chỉ khai báo từ tập 18 feature chuẩn (`atr_14`, `ema_20`, `ema_80`, `ema_200`, `sma_20`, `rsi_14`, `adx_14`, `plus_di_14`, `minus_di_14`, `bollinger_upper_20`, `bollinger_lower_20`, `realized_volatility_20`, `efficiency_ratio_40`, `zscore_20`, `session_vwap`), tối đa 3 timeframe và 8 feature/timeframe.
- **Bảo toàn hành vi Card #1 (`SC-BINANCE-USDM-VOL-BREAKOUT-001`):** `test_strategy_card_strategy.py`, `test_strategy_pipeline_engine.py`, `test_microscope_replay.py` giữ nguyên hành vi và thông báo trên Card #1.
- **Bảo toàn Playwright contract của Card #2 trên Microscope 1 nến (`test_admin_strategy_backtest_ui_kit_playwright.py`):** Trên Preset #1 (Hammer đơn lẻ, không lịch sử đa nến), `l3_zscore` phải chạy `SUCCESS` ra giá trị hữu hạn (`0.0`) và L5 `wick_reversal_trigger` trả `NO_SIGNAL` vì không đủ cực trị Z-Score.
- **Fail-closed trên đường quyết định thật:** Thiếu lịch sử warmup, thiếu feature, hoặc thiếu `taker_buy_base_volume` / `quote_volumes` bắt buộc phải trả `NoTrade` (`WARMUP_INCOMPLETE`, `STRATEGY_CARD_INDICATOR_WARMUP`, `TAKER_FLOW_UNAVAILABLE`, `QUOTE_VOLUME_UNAVAILABLE`, `MACRO_BLACKOUT`, `NOT_FLAT`).

## 3. Kế hoạch thay đổi theo tệp

### Pha A — Tài liệu & TDD (RED)
1. [x] Tài liệu nghiên cứu: `docs/research/strategies-expansion-2026-11-16.md`.
2. [x] Từ điển EN→VI: `docs/research/glossary-vi.md` (mục `AR`).
3. [x] Plan này: `docs/superpowers/plans/2026-11-16-strategy-catalog-and-l0-l6-cards-plan.md`.
4. [ ] Viết **bộ test RED trước**:
   - `tests/unit/test_expanded_strategies.py`: kiểm 6 rule `H02, H04, H05, H07, H21, H22` (warm-up, point-in-time no-lookahead khi thay đổi bar tương lai hoặc loại trừ bar `t` khỏi Donchian/percentile/median, tín hiệu LONG & SHORT chuẩn xác, điều kiện từ chối `NoTrade` cụ thể, hash bất biến, tương thích `StrategyRegistry`, `FEATURES_BY_RELEASE`, `LEARNING_FEATURE_VECTOR`, `BacktestDatasetReplayObservations` + `PaperDecisionCycle` / `TradePlanner`).
   - `tests/unit/test_expanded_strategy_cards_pipeline.py`: kiểm `StrategyPipelineEngine`, `ContextGateEvaluator`, `TriggerEvaluator`, `MicroscopeReplayService`, `StrategyCardAdminService` (`DeadNodeDetector` = 0, `AntiConsensusAnalyzer` thực tế phát hiện được cặp chỉ báo trùng lặp và xác nhận 8 thẻ sản phẩm trực giao), kích hoạt đủ 8 thẻ trong `PRODUCT_CARD_IDS`, fail-closed warmup, L4 short-circuit L5/L6 cho từng chiến lược mới, và replay through `StrategyCardStrategy`.

### Pha B — Cài đặt Mã nguồn (GREEN)
5. `src/trading_bot/domain/strategy/expanded_rules.py` *(mới)*:
   - Hiện thực `H02TimeSeriesMomentumRule`, `H04VolatilitySqueezeBreakoutRule`, `H05TrendPullbackRule`, `H07SessionVwapReversionRule`, `H21MacdAdxMomentumRule`, `H22TakerFlowBreakoutRule` + `EXPANDED_RULES`.
   - Công thức khớp 100% `strategies-expansion-2026-11-16.md`, chỉ dùng dữ liệu point-in-time đến `decision_time` (không đụng nến chưa đóng hay future bars).
6. `src/trading_bot/application/strategy_pipeline/pipeline_engine.py`, `context_gate.py`, `trigger_evaluator.py`, `microscope.py`, `card_strategy.py`:
   - Mở rộng `_compute_l2_node` hỗ trợ chuẩn xác `average_true_range`, `exponential_moving_average`, `simple_moving_average`, `rsi`, `adx`, `realized_volatility`, `bollinger_bandwidth` / `keltner_squeeze`, `session_vwap`, `macd_histogram`, `taker_flow_imbalance`.
   - Mở rộng `_compute_l3_node` hỗ trợ `zscore_normalizer`, `tsmom_composite`, `squeeze_release_score`, `pullback_alignment_score`, `vwap_atr_deviation`, `macd_momentum_score`, `taker_flow_score` (dùng lịch sử thật hoặc feature thật; ở Microscope 1-bar mode trả proxy hữu hạn an toàn, còn ở DecisionSnapshot runtime gán source `"insufficient_history"` khi thiếu chuỗi/feature).
   - Mở rộng `ContextGateEvaluator` hỗ trợ `rv_regime_gate`, `adx_regime_gate`, `squeeze_regime_gate`, `trend_alignment_gate` (`BULLISH` / `BEARISH` / `BIDIRECTIONAL` / `NEUTRAL`).
   - Mở rộng `TriggerEvaluator` hỗ trợ `wick_reversal_trigger`, `tsmom_threshold_trigger`, `squeeze_breakout_trigger`, `pullback_continuation_trigger`, `vwap_reversion_trigger`, `macd_adx_trigger`, `taker_flow_breakout_trigger`.
   - Cập nhật `PIPELINE_ENGINE_VERSION = "l0-l6-v3"` và thêm fail-closed `insufficient_history` guard trong `StrategyCardStrategy.evaluate`.
   - Trong `MicroscopeReplayService.inspect_bar`, thống nhất logic L2/L3 với engine, truyền `context` và hỗ trợ cho các thẻ mới hiển thị traces L0–L6 mượt mà khi người dùng nhấn Presets hoặc tự nhập nến.
7. `src/trading_bot/application/strategy_pipeline/demo_cards.py`:
   - Sửa hoàn chỉnh `SC-BINANCE-USDM-MEAN-REVERT-002` (`v1.2.1`, nối đủ `l1_body -> l5_revert_trigger`, thêm `l2_atr -> l6_revert_zones` + `l4_trend_gate` chế độ `BIDIRECTIONAL`, 0 nút mồ côi).
   - Bổ sung 6 Strategy Card hoàn chỉnh: `003` (`SC-BINANCE-USDM-TSMOM-003`), `004` (`SC-BINANCE-USDM-BBKC-SQUEEZE-004`), `005` (`SC-BINANCE-USDM-TREND-PULLBACK-005`), `006` (`SC-BINANCE-USDM-VWAP-REVERT-006`), `007` (`SC-BINANCE-USDM-MACD-ADX-007`), `008` (`SC-BINANCE-USDM-TAKER-FLOW-008`).
8. `src/trading_bot/admin/strategy_card_service.py`:
   - Cài đặt kiểm định trực giao thực sự bằng `AntiConsensusAnalyzer` trên chuỗi OHLCV tổng hợp chuẩn đa dạng chế độ cho từng thẻ có >= 2 nút L1/L2/L3 độc lập (đảm bảo phát hiện thật nếu có cặp đa cộng tuyến, và xác nhận 8 thẻ sản phẩm đều đạt `is_verified = True`).
9. `src/trading_bot/application/strategy_catalog.py`, `strategy_market_requirements.py`, `src/trading_bot/domain/strategy/setup_progress.py`:
   - Bật cả 8 thẻ vào `PRODUCT_CARD_IDS`, đăng ký `*expanded_rules()` vào `strategy_registry()`.
   - Khai báo `FEATURES_BY_RELEASE` và `ENTRY_TIMEFRAME_BY_RELEASE` cho tất cả 13 release mới (6 rule + 7 card).
   - Cập nhật `SETUP_RULES` trong `setup_progress.py` cho `H02`, `H04`, `H05`, `H07`, `H21`, `H22`.
10. Cập nhật `tests/unit/test_strategy_card_strategy.py:183` để kiểm tra đủ 8 thẻ trong `PRODUCT_CARD_IDS` (thay cho ràng buộc tạm thời chỉ có 1 thẻ trước đây).

### Pha C — Kiểm thử, Chất lượng & Ghi nhớ (REFACTOR & VERIFY)
11. Chạy toàn bộ unit tests mới (`test_expanded_strategies.py`, `test_expanded_strategy_cards_pipeline.py`) + toàn bộ `tests/unit` (kiểm tra 0 failure, 0 skip).
12. Cập nhật / bổ sung kiểm thử Playwright E2E trên `/admin/strategy-cards`, `/admin/strategy-cards/{card_id}/microscope` (kiểm thẻ mới trên desktop & mobile, light & dark, zero CSP violation, zero horizontal overflow) và `/admin/backtests` (xác nhận hiển thị đủ các chiến lược mới trong `#strategy-release`), soi lại ảnh chụp bằng `read_file`.
13. Chạy `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy src/trading_bot`.
14. Cập nhật và đọc lại `docs/implementation/implementation-memory.md`.
