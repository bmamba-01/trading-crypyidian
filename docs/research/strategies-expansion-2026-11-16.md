# Khảo sát & Đặc tả Mở rộng Chiến lược Định lượng Crypto Perpetual Futures (16/11/2026)

**Ngày kiểm tra nguồn và chốt đặc tả:** 16/11/2026 (`Asia/Ho_Chi_Minh`).  
**Phạm vi tài sản:** Hợp đồng tương lai vĩnh cửu ký quỹ USDT (Binance USDⓈ-M Perpetual Futures).  
**Nguyên tắc học thuật:** Mọi chiến lược mới được triển khai với trạng thái ban đầu `EXPERIMENTAL_UNVALIDATED`. Tài liệu nguồn giải thích **cơ chế kinh tế, cấu trúc vi mô và ranh giới rủi ro**, không mặc định hiệu suất quá khứ trên một tập tài sản sẽ lặp lại sau chi phí thực thi thực tế của dự án.

---

## 1. Bối cảnh vấn đề trong hệ thống hiện tại

Trước đợt mở rộng này, giao diện quản trị `/admin/strategy-cards` và `strategy_registry()` chỉ phản ánh một tập chiến lược rất hẹp:
- Trong `build_default_demo_cards()`, mới có đúng **2 Strategy Card** (`SC-BINANCE-USDM-VOL-BREAKOUT-001` và `SC-BINANCE-USDM-MEAN-REVERT-002`), thậm chí thẻ #2 còn tồn tại nút mồ côi (`l1_body`) và bị loại khỏi `PRODUCT_CARD_IDS` do động cơ `L0 → L6` chưa thực thi thật các chỉ báo `EMA`, `Z-Score`, `RSI`, `VWAP`, `MACD`, `Bollinger BandWidth / Keltner Squeeze` hay `Taker Flow Imbalance`.
- Trong `StrategyRegistry` / `strategy_registry()`, mới có 3 nhóm rule cổ điển `H01`, `H03`, `H06` và 3 rule scalping M5 `H18–H20`; các chiến lược đa chế độ quan trọng đã nêu lý thuyết ở [strategies-2026.md](./strategies-2026.md) như **H02** (Time-Series Momentum), **H04** (Volatility Squeeze Breakout), **H05** (EMA Trend Pullback), **H07** (Session VWAP Reversion), cùng các chiến lược **động lượng lọc nhiễu MACD+ADX** và **vi cấu trúc dòng lệnh chủ động (Taker Order-Flow Imbalance)** chưa có mã thực thi hay Strategy Card L0–L6 để đưa vào kiểm thử Backtest Replay (`/admin/backtests`).

---

## 2. Nguồn nghiên cứu & học thuật đã kiểm chứng (16/11/2026)

| Chủ đề / Chiến lược | Nguồn học thuật & tài liệu kỹ thuật | Tóm tắt cơ chế & giới hạn áp dụng |
|---|---|---|
| **Time-Series Momentum & Volatility Scaling (H02 / Card 003)** | - Moskowitz, Ooi & Pedersen (2012), *"Time Series Momentum"*, *Journal of Financial Economics* 104(2):228–250 — [DOI 10.1016/j.jfineco.2011.11.003](https://doi.org/10.1016/j.jfineco.2011.11.003)<br>- Liu & Tsyvinski (2018 / 2021), *"Risks and Returns of Cryptocurrency"*, NBER WP 24877 / *Review of Financial Studies* 34(6) — [NBER w24877](https://www.nber.org/papers/w24877)<br>- Barroso & Santa-Clara (2015), *"Momentum has its moments"*, *JFE* 116(1):111–120 — [DOI 10.1016/j.jfineco.2014.11.010](https://doi.org/10.1016/j.jfineco.2014.11.010) | Lợi suất quá khứ trên chính tài sản có khả năng dự báo hướng đi trung hạn; riêng crypto có hiệu ứng momentum mạnh nhưng chịu *tail crash* khi đảo chiều gắt. Phải có hàng rào biến động thực tế (`realized_volatility_20` có cả sàn dưới chống chuỗi phẳng và trần trên chống sốc hỗn loạn). |
| **Bollinger–Keltner Volatility Squeeze Breakout (H04 / Card 004)** | - John Bollinger, BandWidth & Compression — [TradingView BBW Support](https://www.tradingview.com/support/solutions/43000501972-bollinger-bands-width-bbw/)<br>- Keltner Channels (EMA20 ± m·ATR) — [TradingView KC Support](https://www.tradingview.com/support/solutions/43000502266-keltner-channels-kc/) | Khi độ rộng Bollinger `(upper - lower)/mid` nén xuống dưới phân vị thấp lịch sử hoặc dải Bollinger thu gọn vào bên trong Keltner Channel (`EMA20 ± 1.5·ATR14`), biến động đang ở pha tích tụ. Bắt buộc đợi nến đóng phá đỉnh/đáy `20` nến kèm mở rộng lại `BandWidth` và đột biến khối lượng để tránh phá vỡ giả trong squeeze kéo dài. |
| **EMA Trend Pullback + RSI Regime Continuation (H05 / Card 005)** | - Wilder (1978), RSI/ATR/ADX trong hệ thống xu hướng; MACD/EMA/RSI thực hành chuẩn hóa theo [TradingView Support](https://www.tradingview.com/support/solutions/43000502344-macd-moving-average-convergence-divergence/)<br>- Đa khung trend confirmation đồng nhất hợp đồng `H01` của dự án ([strategies-2026.md](./strategies-2026.md)) | Khắc phục vào đuổi (late entry) của giao cắt EMA truyền thống: chỉ giao dịch khi xu hướng khung `4h` (`EMA200`) và khung `1h` (`EMA20 > EMA80` cùng độ dốc EMA80) đồng thuận, đợi giá hồi chạm `EMA20` nhưng đóng nến giữ vững cấu trúc xu hướng với `RSI14 ∈ [40, 60]`. |
| **Adaptive Wick & Z-Score / Intraday VWAP Reversion (Card 002 & H07 / Card 006)** | - Leung & Li (2015), *"Optimal Mean Reversion Trading with Transaction Costs and Stop-Loss Exit"*, *IJTAF* 18(3) — [arXiv:1411.5062](https://arxiv.org/abs/1411.5062)<br>- Ackerer, Hugonnier & Jermann (2024/2025), *"Perpetual Futures Pricing"* — [arXiv:2310.11771](https://arxiv.org/abs/2310.11771)<br>- Session VWAP reset 00:00 UTC — [TradingView VWAP](https://www.tradingview.com/support/solutions/43000502018-volume-weighted-average-price-vwap/) | Hồi quy thống kê chỉ hợp lý khi thị trường ở trạng thái cân bằng (`ADX14 < 20`, `ER40 < 0.30`), có điểm dừng lỗ chặn đuôi rủi ro và khoảng kỳ vọng về trung tâm (`VWAP` hoặc `EMA20`) phải **vượt ít nhất 3× chi phí khứ hồi** để tránh kèo nhỏ bị phí + trượt giá bào mòn (Leung & Li 2015). |
| **MACD Histogram + ADX Regime Confirmation (H21 / Card 007)** | - Appel MACD (12, 26, 9) — [TradingView MACD Support](https://www.tradingview.com/support/solutions/43000502344-macd-moving-average-convergence-divergence/)<br>- Kết hợp chỉ số sức mạnh xu hướng `ADX14` / `+DI14` / `-DI14` và `4h EMA200` chống whipsaw | Chuyển pha `MACD histogram` (đổi dấu qua `0`) thu được sớm điểm tăng tốc động lượng nhưng dễ thua lỗ nặng khi đi ngang; chỉ mở tín hiệu khi `ADX14 ≥ 22`, `±DI` xác nhận hướng và đồng thuận `EMA200 (4h)`. |
| **Taker Order-Flow Imbalance & Liquidity Breakout (H22 / Card 008)** | - Kim & Hansen (2026), *"The Quarter-Hour Effect: Periodic Algorithmic Trading and Return Predictability in Cryptocurrency Futures"* — [arXiv:2607.09426](https://arxiv.org/abs/2607.09426)<br>- Bieganowski & Ślepaczuk (2026), *"Explainable Patterns in Cryptocurrency Microstructure"* — [arXiv:2602.00776](https://arxiv.org/abs/2602.00776)<br>- Cont, Kukanov & Stoikov (2014), *"The Price Impact of Order Book Events"*, *JFEC* 12(1) — [arXiv:1011.6402](https://arxiv.org/abs/1011.6402)<br>- Trường `takerBuyBaseAssetVolume` — [Binance USD-M Klines API](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data) | Trên hợp đồng vĩnh cửu crypto, các cụm thanh khoản và dòng lệnh thuật toán bùng phát theo chu kỳ 15m (`Kim & Hansen 2026`); áp lực lệnh khớp chủ động có dấu (`TI = (2·taker_buy - volume)/volume`) có tính ổn định cao qua nhiều đồng coin (`Bieganowski & Ślepaczuk 2026`) giúp phân biệt cú phá vỡ thật có dòng tiền chủ động với bẫy quét thanh khoản. |

---

## 3. Ánh xạ chuẩn xác vào kiến trúc Crypidian (Point-in-Time Contract)

1. **Bảo toàn bất biến module cũ:**
   - Không sửa `rules.py`, `rules_v2.py` (`BASE_RULES_SOURCE_SHA256`), `scalping_rules.py`, hay `_BUILT_INS` của `StrategyRegistry()` không tham số.
   - Mọi rule chiến lược mới đặt tại `src/trading_bot/domain/strategy/expanded_rules.py` và chỉ nạp qua `application/strategy_catalog.py:strategy_registry()`.
2. **Bảo toàn `FeatureSchema` (`crypidian-feature-snapshot-v1`):**
   - Toàn bộ chiến lược mới dùng trong `FEATURES_BY_RELEASE` và `ENTRY_TIMEFRAME_BY_RELEASE` **chỉ tham chiếu tới 18 feature chuẩn** đã được `FeatureCalculator` tính point-in-time và được `_unit_for_feature()` trong `learning_feature_vector.py` ánh xạ đơn vị.
   - Các chỉ số dẫn xuất khác (`BandWidth`, `Keltner Channel`, `MACD Histogram`, `HH_n(t-1)/LL_n(t-1)`, `TSMOM log-returns`, `Taker Flow Imbalance`) được tính thuần túy **point-in-time từ các nến đã đóng và khả dụng trong `snapshot.series`**, tuyệt đối loại bỏ nến `t` khỏi biên cực trị quá khứ `HH/LL`, median khối lượng, hay đường phân vị nén.
3. **Fail-Closed & Ngắn mạch (L0–L6):**
   - Nếu `DecisionSnapshot` thiếu lịch sử nến warmup hoặc thiếu `taker_buy_base_volume` / `quote_volumes` / `macro_blackout` (nếu yêu cầu), rule trả `NoTrade` với mã lý do cụ thể (`WARMUP_INCOMPLETE`, `TAKER_FLOW_UNAVAILABLE`, `QUOTE_VOLUME_UNAVAILABLE`, `NOT_FLAT`, `MACRO_BLACKOUT`, ...); `StrategyCardStrategy` kiểm tra tương tự với `STRATEGY_CARD_INDICATOR_WARMUP`.
   - Tại tầng L4 của `StrategyPipelineEngine`, nếu bất kỳ cổng bối cảnh nào trả `BLOCK`, tầng L5 và L6 tuyệt đối bị ngắt mạch (`trigger_result is None` / `SKIPPED`).
   - Tại `StrategyCardAdminService`, kiểm định đồ thị chạy thực sự cả `DeadNodeDetector` (không có nút mồ côi/chu trình/ngõ cụt) lẫn `AntiConsensusAnalyzer` trên chuỗi dữ liệu chuẩn trực giao (không còn gán cứng `has_consensus_warning = False`).

---

## 4. Ma trận 8 Strategy Cards & 6 Expanded Quantitative Rules

| Layer / Role | Rule Release (`expanded_rules.py`) | Strategy Card L0–L6 (`demo_cards.py`) | Entry TF (+HTF) | Features khai báo (`FEATURES_BY_RELEASE`) | Exit Recipe (`SL / TP / Trail / Max Hold`) |
|---|---|---|---|---|---|
| Cổ điển Breakout *(giữ nguyên)* | — (`h03-v1`/`v2` đã có) | `SC-BINANCE-USDM-VOL-BREAKOUT-001` (`v1.0.0`) | `1h` | `atr_14` | SL `2.0 ATR`, TP `4.0 ATR` (`R:R 2.0`), max `12` |
| Thống kê Z-Score + Wick *(sửa dead node & bật)* | — (`h06-v1`/`v2` đã có) | `SC-BINANCE-USDM-MEAN-REVERT-002` (`v1.2.1`) | `15m` | `atr_14`, `ema_20`, `zscore_20` | SL `1.5 ATR`, TP `3.0 ATR` (`R:R 2.0`), max `12` |
| Cổ điển Quant TSMOM | `h02-v1` | `SC-BINANCE-USDM-TSMOM-003` (`v1.0.0`) | `4h` | `atr_14`, `realized_volatility_20` | SL `3.0 ATR`, Trail `4.0 ATR`, opposite exit, max `252` (Card: SL `3.0`, R:R `2.0`) |
| Nén Biến Động BB–KC Squeeze | `h04-v1` | `SC-BINANCE-USDM-BBKC-SQUEEZE-004` (`v1.0.0`) | `15m` | `atr_14`, `ema_20`, `sma_20`, `bollinger_upper_20`, `bollinger_lower_20`, `realized_volatility_20` | SL `1.5 ATR`, TP `3.0 ATR`, Trail `2.0 ATR`, max `32` |
| Nhịp Hồi Theo Xu Hướng EMA | `h05-v1` | `SC-BINANCE-USDM-TREND-PULLBACK-005` (`v1.0.0`) | `1h` + `4h` | `1h`: `atr_14`, `ema_20`, `ema_80`, `rsi_14`, `adx_14`; `4h`: `ema_200` | SL `2.0 ATR`, TP `3.0 ATR`, opposite EMA80 cross, max `48` |
| Hồi Quy Session VWAP UTC | `h07-v1` (`5m`) | `SC-BINANCE-USDM-VWAP-REVERT-006` (`v1.0.0`, `15m`) | `5m` / `15m` | `atr_14`, `adx_14`, `efficiency_ratio_40`, `session_vwap` | SL `1.5 ATR`, TP `2.25 ATR` (`R:R 1.5`), max `12` |
| Động Lượng Lọc Nhiễu MACD+ADX | `h21-v1` | `SC-BINANCE-USDM-MACD-ADX-007` (`v1.0.0`) | `1h` + `4h` | `1h`: `atr_14`, `adx_14`, `plus_di_14`, `minus_di_14`; `4h`: `ema_200` | SL `2.0 ATR`, Trail `2.5 ATR`, opposite exit, max `96` |
| Dòng Lệnh Chủ Động Taker-Flow | `h22-v1` | `SC-BINANCE-USDM-TAKER-FLOW-008` (`v1.0.0`) | `15m` + `1h` | `15m`: `atr_14`, `adx_14`; `1h`: `ema_80`, `atr_14` | SL `2.0 ATR`, Trail `2.5 ATR`, opposite exit, max `32` |
