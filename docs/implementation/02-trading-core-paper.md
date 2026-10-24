# Plan 02 — Trading core và paper trading

**Mục tiêu:** tạo vòng quyết định có thể replay từ dữ liệu point-in-time đến candidate, risk decision, fill mô phỏng và kết quả trade; chưa gửi lệnh ra Binance. \
**Phụ thuộc:** [Plan 01](./01-foundation-database.md). \
**Stories:** US-08, US-10, US-14…20, US-22, US-24. \
**Tài liệu:** [sequence 03/05/06/08/10](../product/sequence-diagrams.md), [technical design](../architecture/technical-design.md), [glossary](../research/glossary-vi.md), [strategy hypotheses](../research/strategies-2026.md).

**Thiết kế đã duyệt:** [WP-02 trading core/config design](../superpowers/specs/2026-10-22-wp02-trading-core-design.md).

**WBS chi tiết:** [WP-02 implementation plan](../superpowers/plans/2026-10-22-wp02-implementation-plan.md) — người dùng đã duyệt WBS ngày 2026-10-22. Bổ sung [WBS pinning/replay progression](./wp02e-profile-pinning-and-replay-progression-wbs.md) xử lý PP2/PP5/RP4/PP6. WP-02 đã nghiệm thu trong phạm vi PAPER/replay MVP; final full suite và independent review đạt.

## WP-02A — Hợp đồng dữ liệu thị trường và adapter đọc

**Trạng thái:** Hoàn thành phần nền (MVP): market contracts/quality, persistence/repository và Binance USDⓈ-M public adapter đã có unit/offline Compose coverage. Runtime collector/backfill coverage cần tiếp tục harden.

1. Định nghĩa domain objects cho instrument, OHLCV bar, trade, funding, open interest, market snapshot, ingestion cursor và data quality.
2. Tạo adapter Binance market-data chỉ đọc qua port; chuẩn hóa symbol USDT-M, tick size, quantity step, min notional và contract status.
3. Mỗi event có `event_at`, `received_at`, `available_at`, source cursor và checksum/raw reference; không ghi đè lịch sử đã được quyết định sử dụng.
4. Thêm dedup key, sequence gap, clock skew, stale thresholds và backfill cursor. Đánh dấu range incomplete cho tới khi backfill xác nhận.
5. Hỗ trợ offline fixture/replay không cần mạng và không chứa secret.

**Nghiệm thu:** cùng input không sinh bản ghi market trùng; gap/stale được nhận diện; truy vấn `as_of` không trả dữ liệu đến sau thời điểm đó; backfill không xóa lineage cũ.

## WP-02B — Feature snapshot và regime features

**Trạng thái:** Hoàn thành phần nền (MVP): công thức và snapshot bất biến có lineage/hash/as-of, repository PostgreSQL/RLS đã có kiểm tra offline và Compose. Chưa xem regime/rule là tín hiệu giao dịch độc lập.

1. Implement các phép tính đã được thống nhất trong `strategies-2026.md`: SMA/EMA, ATR, RSI, ADX/DMI, Bollinger, volatility, VWAP và z-score.
2. Pin seed, smoothing, `ddof`, warm-up, timezone/session reset, missing-data semantics và rounding.
3. Lưu snapshot bất biến có feature schema/hash, source event IDs, formula version, `as_of`, quality/freshness flags.
4. Thêm regime labels như trend/range/high-volatility dưới dạng feature có confidence/coverage; không coi nhãn regime là hướng lệnh.
5. Mỗi feature/công thức mới phải có mục glossary hoặc liên kết tới mục sẵn có.

**Nghiệm thu:** formula fixtures đối chiếu tính toán tham chiếu; nến chưa đóng/stale/missing trả quality flag; replay snapshot cũ cho đúng version gốc.

## WP-02C — Strategy engine, chart geometry và DSL runtime

**Trạng thái:** Hoàn thành phần nền (MVP): H01/H03/H06, registry/recipes và chart confirmation contracts có unit coverage. H03 đã được gọi qua staged `PaperDecisionCycle` trên fixture Compose; các release vẫn là giả thuyết chưa kiểm chứng lợi thế.

1. Định nghĩa interface `Strategy.evaluate(snapshot) -> Candidate | NoTrade` và recipe đầy đủ cho entry, invalidation, exit, partial/timeout.
2. Implement baseline trend-following, breakout và mean-reversion ở dạng hypothesis có version; không ghi nhãn “đã có edge”.
3. Chart candidate gồm swing points, tolerance, lookback, confirmation time, neckline/threshold, invalidation và geometry version.
4. Cấm pivot dùng nến tương lai; chỉ phát candidate khi rule xác nhận ở thời điểm có thể biết.
5. Trả `NO_TRADE` với mã reason khi thiếu dữ liệu/điều kiện; không tự thay missing thành giá trị mặc định thuận lợi.
6. Ghi tên hiển thị tiếng Việt, thuật ngữ tiếng Anh, công thức, timeframe, chi phí giả định và link glossary vào strategy registry.

**Nghiệm thu:** cùng snapshot + strategy release trả cùng kết quả; pattern chỉ được đánh dấu sau confirmation; tất cả candidate có giải thích/lineage và có thể phát lại.

## WP-02D — Planner và RiskEngine

**Trạng thái:** Hoàn thành phần nền (MVP): planner, hard gates, risk decision/reservation persistence, reservation expiry và PostgreSQL concurrency/margin E2E đã chạy. H03 Compose hiện tạo plan/reservation từ candidate strategy thật. Mốc/cửa sổ ngày và rolling loss chưa có semantics đầy đủ; không coi các trường này là risk gate đã được nghiệm thu hoàn chỉnh.

1. Implement TradePlan gồm entry reference E0, entry/stop/target, exit recipe, size, leverage cap, fees/funding/slippage scenario, exchange precision và expiry.
2. Sizing theo ngân sách lỗ dự kiến và distance tới stop; dùng decimal, cộng adverse costs; reject nếu stop/quantity/min notional không hợp lệ.
3. Implement hard caps: risk/trade, aggregate open risk, pending reservation, max exposure, symbol/concentration, leverage, daily/rolling loss và account state age.
4. Risk reservation atomic/concurrent-safe; UNKNOWN intent vẫn giữ reservation tới khi reconcile.
5. Candidate/model không thể sửa hard cap; AI approve không đồng nghĩa lệnh được phê duyệt.
6. Nếu entry/stop/target/recipe/fee/funding/slippage đổi sau khi chấm điểm, yêu cầu rescore hoặc reject plan.

**Nghiệm thu:** mọi reject có reason/version; concurrency không vượt open-risk ceiling; planned slippage tính đúng một lần; actual PnL chỉ theo exchange-confirmed fills/costs.

## WP-02E — Paper execution, position monitor và ledger

**Trạng thái:** Hoàn thành phần nền (MVP). PAPER gateway/monitor, lifecycle coordinator, append-only ledger, entry/exit `FillRecord`, pinned execution profile và strategy/risk lineage, checkpoint schema v2, account lock, UoW candidate/publish-after-commit và snapshot↔ledger verifier đã có. `trading-runtime` nạp một deployment workspace, hydrate/verify account PAPER enabled trước readiness; `PaperRuntimeApplicationService` được tạo trong app lifespan. PP2 focused unit (17) và PP6 target Compose xác minh active state/provider settings. `market_dispatch=not_configured`, `exchange_execution=disabled`; chưa có worker tự phát command, Binance order hay venue reconcile. Xem [WBS PAPER recovery](./wp02e-paper-recovery-wbs.md), [WBS runtime bootstrap](./wp02e-runtime-bootstrap-wbs.md), [WBS pinning/replay](./wp02e-profile-pinning-and-replay-progression-wbs.md) và [WBS application use case](./wp02-task10-step2-paper-usecase-wbs.md).

1. Tạo paper gateway với fill model theo spread/latency/slippage, fee schedule và funding; mọi giả định lưu cùng bản trade.
2. Ghi order intent → order → fill → position event → trade cost/realized PnL theo cùng state machine khái niệm với sàn.
3. Position monitor thực thi exit recipe đã pin: stop, target, trailing monotonic, time exit và opposing signal.
4. Hỗ trợ partial fill, cancel phần chưa fill, stop coverage và reduce-only semantics trong simulator.
5. Cho phép replay toàn bộ trade từ fixture; nối trade detail tới candidate, model score, risk decision và snapshot.

**Nghiệm thu:** Compose/PAPER fixture đạt: không cần credential/venue order; service lấy từ lifespan sau bootstrap/hydrate theo workspace deployment; retry không tạo duplicate; restart khôi phục position; exit có provenance; trade/report khớp ledger; bad lineage, profile drift và callback publish failure fail-closed. WP-02 gate hoàn tất trong phạm vi PAPER/replay MVP sau independent review (không có P0/P1/source P2; documentation-only P2 đã đóng). Market dispatcher và exchange execution vẫn tắt theo thiết kế. Giới hạn còn lại gồm realized-only equity/no mark-to-market, daily/rolling risk window chưa hoàn chỉnh và chưa kiểm chứng profitability.

## WP-02F — Kiểm định strategy và chống thiên lệch

**Trạng thái:** Hoàn thành phần nền (MVP). Deterministic replay, leakage guard, temporal splits, metrics/uncertainty, append-only run/trial/report/scenario/outcome persistence, runtime PAPER evaluator và sensitivity BASE/FEE/SLIPPAGE/PARAMETER đã có. Replay reducer sở hữu realized equity/exposure/reservation progression theo chuỗi PAPER event; context provider cung cấp base account state cùng market/execution facts và không ghi đè state reducer. RP4 Compose target **1 passed / 57.69s**, regression **30 passed**; final full E2E **13 passed / 381.82s**. Deterministic hash được đối chiếu bằng replay lặp không restart worker. Chưa có mark-to-market; semantics giới hạn lỗ ngày/rolling cần được đặc tả hoàn chỉnh. Funding và spread nonzero sensitivity không hỗ trợ; evaluator từ chối fail-closed. Không diễn giải report thành bằng chứng profitability.

1. Tạo backtest runner event-driven với cùng domain contract và order/exit recipe dùng ở runtime.
2. Mô hình hóa phí, funding, latency, spread, slippage, rejected/unfilled order và ambiguous OHLC path.
3. Thêm look-ahead/data leakage checks, time-ordered split, sensitivity theo chi phí và parameter neighborhood.
4. Báo cáo trade count, coverage, net PnL, drawdown, expectancy, fee/funding share, exposure và confidence intervals; không chỉ win rate.
5. Lưu trial ledger để biết số lần strategy/parameter đã thử; kết quả là hypothesis evidence, không cam kết tương lai.

**Nghiệm thu:** evaluator dùng cùng strategy/planner/risk/fill/monitor implementation với PAPER; feature `available_at` được kiểm soát; báo cáo có metrics, cost, coverage/uncertainty, manifest lineage và giới hạn fill model; sensitivity được lưu và phát lại xác định. Funding/spread runtime stress là giới hạn đã nêu trong WBS, chưa được hỗ trợ.

## WP-02G — Root WorkspaceSettings và Admin API

1. Giữ một root `WorkspaceSettings` versioned theo workspace; các cấu hình market, features, strategy selection, risk profile reference và PAPER execution nằm trong schema gốc.
2. Mọi decision/plan/order/fill/trade pin settings version/hash; draft không có hiệu lực, publish/rollback tạo version bất biến.
3. Draft/validate/publish/rollback đi qua Admin API với session, CSRF, RBAC, recent MFA khi đổi risk, audit/outbox và optimistic concurrency.
4. Secret chỉ tham chiếu qua secret provider; không đặt API key, Binance secret, Telegram token trong settings JSON hoặc event.
5. UI quản trị dùng chung API thuộc WP-04; WP-02 không thêm trang cấu hình riêng.

**Trạng thái:** Hoàn thành phần nền (MVP): model/repository/migration, effective-config loader kiểm tra status/hash/scope và Admin API workflow có unit + Compose E2E auth/settings/RLS/publish coverage. Loader đã chạy cùng journey coordinator/risk reservation trong Compose; UI vẫn thuộc WP-04.

**Nghiệm thu:** cross-workspace isolation; hash/version bất biến; draft không active; publish atomic/audited; rollback tạo version mới; secret không lộ; risk changes cần permission/MFA.

## Kiểm tra khi triển khai

Unit kiểm tra công thức, property tests cho sizing/precision, integration kiểm tra `as_of`, gap/backfill, replay và risk reservation concurrency; chạy tất cả trên dữ liệu fixture/offline trước khi gọi Binance Demo.
