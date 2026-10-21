# Plan 02 — Trading core và paper trading

**Mục tiêu:** tạo vòng quyết định có thể replay từ dữ liệu point-in-time đến candidate, risk decision, fill mô phỏng và kết quả trade; chưa gửi lệnh ra Binance. \
**Phụ thuộc:** [Plan 01](./01-foundation-database.md). \
**Stories:** US-08, US-10, US-14…20, US-22, US-24. \
**Tài liệu:** [sequence 03/05/06/08/10](../product/sequence-diagrams.md), [technical design](../architecture/technical-design.md), [glossary](../research/glossary-vi.md), [strategy hypotheses](../research/strategies-2026.md).

## WP-02A — Hợp đồng dữ liệu thị trường và adapter đọc

1. Định nghĩa domain objects cho instrument, OHLCV bar, trade, funding, open interest, market snapshot, ingestion cursor và data quality.
2. Tạo adapter Binance market-data chỉ đọc qua port; chuẩn hóa symbol USDT-M, tick size, quantity step, min notional và contract status.
3. Mỗi event có `event_at`, `received_at`, `available_at`, source cursor và checksum/raw reference; không ghi đè lịch sử đã được quyết định sử dụng.
4. Thêm dedup key, sequence gap, clock skew, stale thresholds và backfill cursor. Đánh dấu range incomplete cho tới khi backfill xác nhận.
5. Hỗ trợ offline fixture/replay không cần mạng và không chứa secret.

**Nghiệm thu:** cùng input không sinh bản ghi market trùng; gap/stale được nhận diện; truy vấn `as_of` không trả dữ liệu đến sau thời điểm đó; backfill không xóa lineage cũ.

## WP-02B — Feature snapshot và regime features

1. Implement các phép tính đã được thống nhất trong `strategies-2026.md`: SMA/EMA, ATR, RSI, ADX/DMI, Bollinger, volatility, VWAP và z-score.
2. Pin seed, smoothing, `ddof`, warm-up, timezone/session reset, missing-data semantics và rounding.
3. Lưu snapshot bất biến có feature schema/hash, source event IDs, formula version, `as_of`, quality/freshness flags.
4. Thêm regime labels như trend/range/high-volatility dưới dạng feature có confidence/coverage; không coi nhãn regime là hướng lệnh.
5. Mỗi feature/công thức mới phải có mục glossary hoặc liên kết tới mục sẵn có.

**Nghiệm thu:** formula fixtures đối chiếu tính toán tham chiếu; nến chưa đóng/stale/missing trả quality flag; replay snapshot cũ cho đúng version gốc.

## WP-02C — Strategy engine, chart geometry và DSL runtime

1. Định nghĩa interface `Strategy.evaluate(snapshot) -> Candidate | NoTrade` và recipe đầy đủ cho entry, invalidation, exit, partial/timeout.
2. Implement baseline trend-following, breakout và mean-reversion ở dạng hypothesis có version; không ghi nhãn “đã có edge”.
3. Chart candidate gồm swing points, tolerance, lookback, confirmation time, neckline/threshold, invalidation và geometry version.
4. Cấm pivot dùng nến tương lai; chỉ phát candidate khi rule xác nhận ở thời điểm có thể biết.
5. Trả `NO_TRADE` với mã reason khi thiếu dữ liệu/điều kiện; không tự thay missing thành giá trị mặc định thuận lợi.
6. Ghi tên hiển thị tiếng Việt, thuật ngữ tiếng Anh, công thức, timeframe, chi phí giả định và link glossary vào strategy registry.

**Nghiệm thu:** cùng snapshot + strategy release trả cùng kết quả; pattern chỉ được đánh dấu sau confirmation; tất cả candidate có giải thích/lineage và có thể phát lại.

## WP-02D — Planner và RiskEngine

1. Implement TradePlan gồm entry reference E0, entry/stop/target, exit recipe, size, leverage cap, fees/funding/slippage scenario, exchange precision và expiry.
2. Sizing theo ngân sách lỗ dự kiến và distance tới stop; dùng decimal, cộng adverse costs; reject nếu stop/quantity/min notional không hợp lệ.
3. Implement hard caps: risk/trade, aggregate open risk, pending reservation, max exposure, symbol/concentration, leverage, daily/rolling loss và account state age.
4. Risk reservation atomic/concurrent-safe; UNKNOWN intent vẫn giữ reservation tới khi reconcile.
5. Candidate/model không thể sửa hard cap; AI approve không đồng nghĩa lệnh được phê duyệt.
6. Nếu entry/stop/target/recipe/fee/funding/slippage đổi sau khi chấm điểm, yêu cầu rescore hoặc reject plan.

**Nghiệm thu:** mọi reject có reason/version; concurrency không vượt open-risk ceiling; planned slippage tính đúng một lần; actual PnL chỉ theo exchange-confirmed fills/costs.

## WP-02E — Paper execution, position monitor và ledger

1. Tạo paper gateway với fill model theo spread/latency/slippage, fee schedule và funding; mọi giả định lưu cùng bản trade.
2. Ghi order intent → order → fill → position event → trade cost/realized PnL theo cùng state machine khái niệm với sàn.
3. Position monitor thực thi exit recipe đã pin: stop, target, trailing monotonic, time exit và opposing signal.
4. Hỗ trợ partial fill, cancel phần chưa fill, stop coverage và reduce-only semantics trong simulator.
5. Cho phép replay toàn bộ trade từ fixture; nối trade detail tới candidate, model score, risk decision và snapshot.

**Nghiệm thu:** vertical slice PAPER hoàn thành không cần credential; pause entries không dừng protective exit; trade đóng có net PnL sau fee/funding; recipe replay khớp ledger.

## WP-02F — Kiểm định strategy và chống thiên lệch

1. Tạo backtest runner event-driven với cùng domain contract và order/exit recipe dùng ở runtime.
2. Mô hình hóa phí, funding, latency, spread, slippage, rejected/unfilled order và ambiguous OHLC path.
3. Thêm look-ahead/data leakage checks, time-ordered split, sensitivity theo chi phí và parameter neighborhood.
4. Báo cáo trade count, coverage, net PnL, drawdown, expectancy, fee/funding share, exposure và confidence intervals; không chỉ win rate.
5. Lưu trial ledger để biết số lần strategy/parameter đã thử; kết quả là hypothesis evidence, không cam kết tương lai.

**Nghiệm thu:** backtest và forward paper dùng cùng rule semantics; feature `available_at` không vượt decision time; báo cáo nêu rõ phạm vi/mode/coverage và giới hạn fill model.

## Kiểm tra khi triển khai

Unit kiểm tra công thức, property tests cho sizing/precision, integration kiểm tra `as_of`, gap/backfill, replay và risk reservation concurrency; chạy tất cả trên dữ liệu fixture/offline trước khi gọi Binance Demo.
