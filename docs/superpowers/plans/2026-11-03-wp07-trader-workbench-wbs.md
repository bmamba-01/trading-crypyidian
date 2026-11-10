# WBS WP-07 — Trader Workbench, tín hiệu Telegram và dữ liệu lịch sử

**Ngày:** 2026-11-03 · **Trạng thái:** đang triển khai theo yêu cầu người dùng · **Chế độ:** PAPER/đọc dữ liệu; không route order. <br>
**Thiết kế:** [Trader Workbench design](../specs/2026-11-03-trader-workbench-design.md). <br>
**Nền:** WP-02 PAPER/replay, MB-06 reports, MB-07a Telegram binding, WP-05 rights registry, WP-06 manifest/fold contracts.

## Mục tiêu sản phẩm

Một trader có thể xem từng quyết định/vị thế trên chart responsive, hiểu chính xác vì sao candidate được phát và vì sao vị thế đóng; theo dõi tín hiệu Telegram mà không chạy PAPER hoặc gửi lệnh; chuẩn bị bộ dữ liệu lịch sử có lineage/chất lượng để backtest và chỉ dùng làm dữ liệu demo khi source grant cho phép mục đích đó. Mọi màn hình admin/report dùng tiếng Việt và có bố cục mobile.

## WBS

### WP-07A — Trade chart và giải thích quyết định

1. Tự lưu TradingView Lightweight Charts kèm giấy phép/attribution; chỉ dùng internal OHLCV.
2. Bổ sung close event chứa exit reason, exit fill key, bar key/time và rule/decision provenance; giữ append-only/idempotent.
3. Report query dựng timeline từ entry/exit fills, decision candidate/evidence, stop update và cost ledger; mọi record được scope theo workspace/bot/account.
4. Chart hiển thị side, entry/average fill, exit/partial fill, stop/target nếu có, net/gross P&L, fee/funding/slippage, giờ vào/đóng, strategy release, trigger reason/evidence, exit reason và data-quality cảnh báo.
5. Chart chọn được trade, symbol, timeframe và khoảng thời gian; trade detail dùng marker + panel/tables, không phụ thuộc hover; JSON/ledger detail vẫn truy cập được.
6. Responsive ở 360–1440px, keyboard/focus/touch, text tiếng Việt, chart lỗi/thiếu nến có fallback rõ ràng.
7. Dashboard marker có mô tả ngắn; rê chuột/chạm marker mở giải thích đủ thời điểm, P&L, chiến thuật, trigger, lý do đóng. Có danh sách keyboard/screen-reader dùng được cho 50 trade gần nhất; tránh nhồi câu dài vào marker.

**Acceptance:** giá/giờ marker khớp ledger; chart không đoán trường thiếu; exit reason khớp event idempotent; authorization giữ nguyên; dashboard/report dùng cùng chart library, có chú giải tương tác và danh sách thay thế mobile. Mốc entry/exit và P&L ngắn nằm trên chart; nguyên nhân đầy đủ hiển thị bằng panel/list liên kết cùng mốc, không phụ thuộc hover.

**Kiểm chứng trình duyệt (2026-11-03):** Playwright mở report của trade PAPER đã đóng, chọn chart 5m, rê chuột trực tiếp qua vùng marker và xác nhận panel hiện cả giải thích entry/exit; chart được kiểm reflow tại 360, 390, 768, 1024 và 1280px, zoom 200% và focus bàn phím. Lượt Chromium desktop: **1 passed**.

**Dashboard snapshot theo bot (04/11/2026):** trang tổng quan cho chọn bot đang bật và từng giao dịch PAPER còn mở, hiển thị runtime, metric theo quote asset và chi tiết ledger. JSON có xác thực, giới hạn một bot, `no-store`; trình duyệt tự làm mới mỗi 5 giây khi tab hiện, giữ snapshot cũ khi lỗi và dừng poll khi quyền bị thu hồi/tab ẩn. “Phiên” ở lát này là trade PAPER đang mở; nguồn là dữ liệu đã lưu, không phải heartbeat, giá Binance hoặc stream market. Playwright trên 390×844 và 1280×800 tạo trade mở qua runtime PAPER, xác nhận ID/chi tiết trên UI và chụp `tests/e2e/evidence/wp07-live-dashboard-{mobile,desktop}.png`; **2 passed**. Đã mở xem cả hai ảnh.

### WP-07B — Chế độ chỉ phát tín hiệu qua Telegram

1. Subscription tường minh theo private chat và bot; allowlist symbol, timeframe, hướng, strategy ID và setup/reason code; danh sách rỗng nghĩa là mọi giá trị. `/start` liên kết chat, `/stop` hủy ngay; cài quiet hours/timezone, cooldown và giới hạn số tín hiệu/ngày.
2. Producer rà soát market stream dùng nến đã đóng, dữ liệu point-in-time và feature quality gate để phát `SharedSignalEvidence`; fan-out chạy strategy đã publish theo từng bot.
3. Candidate chỉ được phát sau BotDecision pipeline, không ghi `OrderIntent`, `FillRecord`, `TradeRecord` hoặc reservation. Market-signal producer và Telegram sender tách process/DB role khỏi PAPER runtime; producer chỉ đọc nến đã lưu và ghi evidence/decision/outbox, sender chỉ đọc candidate/subscription và ghi delivery.
4. Event/outbox có decision ID, setup identity, freshness/expiry/invalidation và dedupe; worker rate-limited, retry/dead-letter và receipt/audit.
5. Telegram Bot API sender dùng token từ secret file/provider; redaction, HTTPS, message length/escaping và xử lý 429 `retry_after`.
6. Worker deployment role có DB grants đúng bảng market/evidence/decision/subscription/delivery; không có exchange credential, execution client hoặc ghi quyền order.
7. Dashboard hiển thị trạng thái liên kết, đăng ký, lần gửi cuối, lỗi, dedupe/expired/invalidated và lịch sử; cho phép test message không tạo signal/order.
8. Tùy chọn loại thông báo candidate, invalidation, hết hạn và đóng PAPER trade; có giới hạn tần suất theo chat/bot và nội dung có thể đọc nhanh trên mobile.

**Acceptance:** từ nến đóng offline fixture → evidence → BotDecision candidate → tối đa một delivery receipt; `/stop` ngăn gửi tiếp; candidate hết hạn bị bỏ; lỗi Telegram được retry theo policy; DB role producer/sender không thể chạm order/trade/position; không có lệnh hay PAPER trade từ alert. Đường producer → evidence → candidate → fake Telegram receipt đã được nối. Subscription hỗ trợ GET/PUT, giao diện HTML tiếng Việt và năm allowlist; PostgreSQL E2E chứng minh preference đổi khi receipt đang chờ làm receipt thành `FILTERED`, không tăng attempts/không gửi, trong khi subscription khớp vẫn nhận đúng một tin giả. WP-07B còn thiếu market-feed ingestion tự động, dashboard lịch sử/trạng thái delivery, quiet-hours/cooldown/rate-cap đầy đủ và retry/dead-letter matrix mọi lỗi Bot API.

### WP-07C — Backfill dữ liệu lịch sử một năm

1. Chọn provider/terms/purpose và gắn source rights policy trước ingest; connector mặc định disabled khi thiếu quyền hợp lệ. Tách quyền research cá nhân với quyền sản phẩm/demo/thương mại; không dùng Binance Vision cho demo/sản phẩm nếu chưa có giấy phép enterprise bằng văn bản.
2. Manifest kế hoạch gồm UTC start/end (365 ngày đóng + warm-up riêng), symbol, interval, loại giá (contract/mark/index), funding, parser/version, archive/API request lineage, retrieval/acceptance time và terms.
3. Downloader có giới hạn concurrency/rate, retry/backoff, resume cursor, checksum publisher + SHA-256 local, raw archive retention và an toàn zip/path/row/size limits.
4. Parser Decimal/UTC giữ cả base volume và quote asset volume theo source contract; kiểm tra schema, duplicate/order, OHLC invariant, interval grid, contract lifetime và coverage; trường nguồn thiếu giữ NULL, không ước lượng/nội suy.
5. Append bars/funding qua MarketDataRepository giữ source/revision/as-of; conflict/daily↔monthly revision không ghi đè lịch sử và được đưa vào review.
6. Backtest runner nhận bundle các manifest con đã persist và revalidate riêng cho từng timeframe cần bởi strategy; bundle ghim symbol, workspace, evaluation window, rights lineage và hash tổng hợp ổn định. Khoảng warm-up có thể khác theo timeframe; không tự resample/nội suy bar. Sau đó dựng DecisionSnapshot point-in-time và chạy RuntimeReplayEvaluator cùng BacktestRunner/PAPER semantics.
7. Thêm dashboard/CLI để tạo, xem tiến độ, resume, hủy và export quality report; mặc định phạm vi giới hạn (BTCUSDT/ETHUSDT và interval do người vận hành chọn).
8. Có provider Binance USDⓈ-M REST `/fapi/v1/klines` với chia trang theo `limit` hiện hành, giới hạn request/concurrency, bắt 429/418 theo Retry-After và tiếp tục từ cursor; trước khi bật phải xác nhận quyền sử dụng đúng mục đích.

**Đã triển khai một phần:** `build_trailing_365_day_rest_request` dựng request một năm theo biên interval UTC gần nhất đã đóng (1w là 364 ngày, 3d là 363 ngày); lưới 1w dùng thứ Hai 00:00 UTC, được kiểm tra bằng fixture và một phản hồi REST quan sát ngày 03/11/2026. `BinanceRestBackfillRunner` phân trang nửa mở, bắt buộc rights evidence cho từng trang, giữ checksum/receipt, serialize checkpoint canonical và kiểm request/policy fingerprint, bar đóng, thứ tự, interval grid, coverage. `BinanceRestBackfillIngestionService` append bar và checkpoint trong cùng Unit of Work; cursor `ingestion_cursor` tách theo request fingerprint. `SourceRegistryBackfillRightsGate` đọc policy hiện hành cùng toàn bộ observer receipt, dùng PostgreSQL clock và cấp lease tối đa 5 phút; `BACKTEST_RESEARCH` tách biệt `PAPER_DEMO`, không có quyền mặc định. Migration mở rộng purpose checks và chặn downgrade khi dữ liệu mới đã được lưu. Compose E2E với provider fixture/PostgreSQL chứng minh rollback không để lại bar/cursor, commit lưu 2 bar + cursor và resume không tải/trùng thêm dữ liệu; E2E cũng xác minh fingerprint evidence khớp policy đọc lại từ DB, không có request mạng và downgrade bị chặn khi policy đã lưu purpose mới. `build_backtest_dataset_manifest` tạo snapshot hash gắn request, quyền, terms, thời điểm kiểm quyền, warm-up, mốc nến và coverage; khoảng evaluation tách riêng, nến thiếu được liệt kê chính xác và `require_ready()` từ chối replay khi chưa đủ coverage. Manifest v2 đã gắn workspace của rights evidence vào canonical hash; manifest v3 hiện là mặc định và đưa quote asset volume vào bar hash, trong khi reader vẫn giữ nguyên schema v2. Migration `c9d7e5f3a1b2` bổ sung `MarketBar.quote_volume` nullable, giữ dữ liệu cũ là NULL, không ước lượng; replay schema v2 bị chặn khi DB có quote volume mới chưa ghim. Binance REST parser, paginator, Vision CSV parser, DB repository và signal snapshot truyền cùng Decimal từ nguồn; H03 chỉ nhận chuỗi quote volume khi mọi bar có dữ liệu. Migration downgrade bị chặn nếu cột đã có giá trị. `BacktestDatasetManifestRepository` lưu canonical JSON + JSONB, hash và cột chỉ mục theo workspace; migration `b1d3f5a7c9e2` áp dụng RLS, unique workspace/hash, quyền admin-web SELECT/INSERT, immutable trigger và downgrade guard. Repository append idempotent, đọc lại kiểm hash/canonical bytes/cột chỉ mục; `BacktestDatasetReplayLoader` yêu cầu coverage READY, đọc đúng workspace rồi revalidate policy hiện hành bằng Source Registry trong cùng transaction, từ chối revoke/expiry hoặc fingerprint đổi; có API revalidate evidence lần nữa trên bundle đã nạp mà không đọc lại bars trước persist; sau đó đọc lại bar source `BINANCE_USDM_REST_BACKFILL` trong range nửa mở tại `prepared_at`, so số lượng và `bars_content_hash`, rồi mới trả replay input gồm manifest, bars và rights evidence hiện hành. `build_backtest_dataset_replay_bundle` ghép một hoặc nhiều input đã xác minh, yêu cầu timeframe không trùng, chung workspace/symbol/evaluation window/rights fingerprint, giữ warm-up riêng từng timeframe và hash tổng hợp canonical ổn định. PostgreSQL E2E xác nhận rollback/commit/resume, quote volume Decimal được đọc lại, manifest READY có 1 warm-up + 1 evaluation bar, insert/readback/retry, RLS giữa workspace, immutable trigger, replay được phép khi policy còn nguyên và bị chặn sau revoke. `build_backtest_dataset_replay_observations` tạo ReplayObservation/DecisionSnapshot theo nến entry; timeframe/feature contract dùng chung với signal producer, snapshot chỉ chứa bar đóng và available tại decision time, current/previous feature có ID xác định, quote volume thiếu giữ None; decision bar available từ range_end trở đi bị từ chối. DecisionSnapshot builder để position/macro unknown nếu chưa có source. Context provider tuần tự, `RuntimeReplayEvaluator` và composition qua `BacktestRunner` đã được thêm; còn thiếu macro calendar evidence, raw response/market raw reference, run persistence cho pipeline mới, CLI/scheduler và E2E riêng từ bundle tới runner. Bundle không resample/nội suy và chưa chạy dữ liệu thị trường thật. Binance Vision vẫn bị chặn cho demo/sản phẩm nếu chưa có grant bằng văn bản; REST API technical docs cũng không thay thế quyền lưu/hiển thị/phân phối theo hợp đồng áp dụng.

**Lát vừa triển khai:** [plan replay context tuần tự](./2026-11-03-wp07c-sequential-runtime-context.md) bổ sung `BacktestDatasetReplayContextProvider` và `run_backtest_dataset_runtime_replay()`: bundle/manifest/window/workspace phải khớp; seed Account/RiskState phải known và flat; nến execution mới được phát tuần tự theo `available_at`; ATR/opposing signal gắn theo từng bar; thiếu evidence cần cho exit thì từ chối trial. `RuntimeReplayEvaluator` tính `position_is_flat` từ exposure/order/position sau event và ID snapshot có hash portfolio state; stop/target/time exit vẫn xử lý được khi ATR/opposing signal không cần cho nhánh exit đó. Mỗi sensitivity trial có gateway/provider riêng, reservation trong replay là in-memory. Unit và evaluator persistence E2E hiện đạt; Compose E2E riêng bundle → provider → runner được ghi nhận tại cập nhật tiếp theo.

**Cập nhật 03/11/2026:** [Compose E2E bundle → runner](./2026-11-03-wp07c-bundle-runner-e2e.md) chạy bundle synthetic hash-verified đa timeframe qua observation builder, context provider, PAPER evaluator và `BacktestRunner`; đủ 4 observation được lưu thành report/scenario trên PostgreSQL và retry idempotent. E2E outcome hiện có xác nhận trade/PnL được persist. Đây là bằng chứng fixture offline, không chứng minh quyền sử dụng dữ liệu thị trường thật.

**Cập nhật 03/11/2026 — persisted replay bundle:** migration `e7c9a1b3d5f2` thêm kho bundle dùng lại theo `(workspace_id, bundle_hash)` và `backtest_run_dataset_rights` lưu snapshot quyền bất biến theo run/manifest con. Bundle chỉ giữ manifest hash/canonical scope, không sao chép bar; loader đọc từng manifest từ PostgreSQL rồi recheck rights và hash bar hiện hành trước khi trả bundle. Compose E2E xác nhận 1h/4h fixture bars được đọc lại và ghép thành bundle, insert/readback/retry idempotent, RLS workspace, immutable trigger và replay bị từ chối sau revoke. Outcome E2E lưu receipt canonical cho mỗi child qua role `crypidian_trainer_worker`, retry giữ nguyên canonical/hash; worker không có SELECT trên bundle. Run cũ không được gán receipt hồi tố. Downgrade bị chặn khi bundle/receipt còn dữ liệu. Service flow đã được nối ở cập nhật kế tiếp; đây vẫn là fixture offline, chưa chứng minh quyền dùng dữ liệu thị trường thật.

**Cập nhật 03/11/2026 — service orchestration:** `BacktestDatasetReplayExecutionService` nối persisted bundle → runtime PAPER → outcomes/rights receipts. Service dùng UoW load riêng cho `crypidian_admin_web`, chạy runner ngoài transaction bằng worker thread, rồi dùng UoW persist của `crypidian_trainer_worker` để revalidate Source Registry hiện hành và append mọi sensitivity child atomically; không cấp worker quyền đọc bundle/manifests. Compose E2E trên PostgreSQL thực chạy H01 từ bundle đã đọc lại, kiểm hai scenario/report, trade-row count so với runtime, receipt cho từng child, rollback sau fault injection, retry sau đổi clock giữ nguyên receipt đầu và revoke chặn lần chạy kế tiếp trước executor. Scheduler riêng và xác nhận run với dataset được cấp quyền thật còn mở; CLI/job lifecycle, progress/cancel, worker Compose và UI `/admin/backtests` đã có bằng chứng ở cập nhật 04/11/2026.

**Cập nhật 04/11/2026 — replay jobs/CLI/admin:** queue PostgreSQL, owner-only job service, worker Compose, CLI MFA owner và `/admin/backtests` đã được kiểm bằng Compose/Playwright như implementation memory ghi. Task 1 thêm capability persist hẹp cho role trainer; Task 2 nối `begin → quyền hiện hành → mọi outcome/receipt → complete` trong cùng UoW. Task 3 hiện đang chuyển worker khỏi lease callback/session riêng sang row lock cùng transaction; chỉ sau Compose race/rollback E2E mới xác nhận end-to-end fencing hoàn tất. Trainer được thu hồi SELECT market cũ; quyền đọc market nằm ở replay worker riêng. Scheduler, macro/raw evidence và dữ liệu có grant thật vẫn mở.

**Acceptance:** fixture offline cho một năm tạo manifest xác định; tải dở có thể tiếp tục; checksum/schema/gap bị phát hiện; quyền không hợp lệ chặn ingest/training; backtest từ manifest nêu rõ coverage và mọi giả định fill/chi phí. Request/paginator/cursor, Source Registry rights gate, manifest coverage/hash/persistence, workspace RLS, quyền recheck, kiểm toàn vẹn bars và service-level flow bundle → runtime PAPER → `BacktestRunner` → PostgreSQL reports/scenarios/trade outcomes/rights receipts đều có unit/Compose evidence trên fixture offline. Scheduler riêng và xác nhận run với dataset được cấp quyền thật còn mở; CLI, job lifecycle, worker và UI `/admin/backtests` đã triển khai, nhưng ownership fence xuyên transaction outcome đang được hoàn thiện ở WP07C Task 3. Luồng dữ liệu dùng cho demo/sản phẩm chỉ chạy với source grant phù hợp; dữ liệu bị giới hạn quyền chỉ được gắn với purpose được phép.

### WP-07D — Trader analytics và report mở rộng

**Đã triển khai một phần:** report v2 có win rate, profit factor, expectancy, trung bình thắng/thua, streak, thời gian giữ và lịch P&L thực hiện theo ngày `Asia/Ho_Chi_Minh`; bot/workspace tách theo quote asset, report v1 giữ schema cũ. Journal/tag/mistake attribution, filters/CSV, so sánh khoảng, MFE/MAE và market context vẫn là các task riêng còn mở.

**Lát highlight lãi/lỗ (04/11/2026):** dashboard và report dùng xanh cho giá tăng/PnL dương, đỏ cho giá giảm/PnL âm; nến chart dùng màu Binance-style `#0ECB81`/`#F6465D`, marker thoát theo PnL, các giá trị report có dấu và nền highlight. Chữ xanh/đỏ dùng sắc tối để đủ tương phản trên nền sáng; hòa vốn/trạng thái thiếu dùng màu trung tính. Dấu `+`/`−` và ký hiệu hướng giữ ý nghĩa khi người xem không phân biệt màu. Tài liệu Binance xác nhận xanh tăng/đỏ giảm là mặc định và bảng màu có thể tùy chỉnh; đây là quy ước mặc định của Crypidian, chưa có tuỳ chọn đổi palette trong sản phẩm.

1. Cumulative realized equity, net/gross P&L, fees/funding/slippage, drawdown và trade count theo thời gian.
2. Phân rã strategy/release/symbol/side/timeframe, calendar/session và trade drilldown; không gộp quote currency khác nhau.
3. Filter/CSV export, report hash/lineage và range comparison; timezone hiển thị rõ Asia/Ho_Chi_Minh nhưng lưu UTC.
4. MFE/MAE, mark-to-market equity, benchmark và replay chỉ được hiển thị sau khi WP-02/WP-07C có nguồn và semantics đạt.
5. Journal note, checklist, tag setup và screenshot attach cần audit/retention/privacy design riêng; không đưa vào ledger bất biến.
6. Mở rộng trader analytics phổ biến: win/loss rate, expectancy, profit factor, average win/loss, streak, thời lượng nắm giữ và lịch hiệu suất; mỗi metric có định nghĩa, mẫu số, cửa sổ, quote asset và nguồn dữ liệu rõ ràng.
7. So sánh strategy/release, symbol, long/short, timeframe, phiên/ngày trong tuần và export CSV có report hash; filter không được vượt quyền truy cập.
8. Tạo lịch P&L theo ngày/tháng, so sánh ngày lời/lỗ, và drill-down theo setup/tag/lý do sai lầm; ghi chú thủ công và tag phải có audit, quyền sửa, retention và không làm thay đổi ledger.
9. Bản report v2 công bố metric mới có semantics/version riêng; v1 tiếp tục đọc được nguyên schema. PnL dùng net theo quote asset, hòa vốn không tính là thắng/thua và ngắt streak; profit factor không xác định khi không có tổng lỗ; metric chia làm tròn tối đa 8 chữ số thập phân theo HALF_UP.
10. Phân tích funding/OI/liquidations/heatmap là lớp bối cảnh tùy chọn có provenance, coverage, timestamp và nguồn/terms riêng; không dùng nguồn tổng hợp hoặc heatmap làm giá fill, sự thật của sàn hay tín hiệu dự báo nếu chưa qua validation.

**Acceptance:** số liệu reconcile với ledger, filters không đổi scope quyền, drawdown/window semantics được kiểm tra, mobile không tràn và không có chỉ số không nguồn. UI E2E kiểm PnL dương/âm theo tone và dấu, chart tăng/giảm, màu chữ tương phản, hòa vốn trung tính, cùng layout mobile/desktop.

### WP-07E — Readiness, vận hành và chất lượng dữ liệu

1. Readiness riêng cho signal producer, Telegram sender và backfill worker; thiếu source, terms, secret, quyền DB hoặc tick mới nhất thì báo degraded/not-ready có mã nguyên nhân.
2. Theo dõi bar freshness/coverage/gaps, scanner lag, candidate→delivery latency, retry/dead-letter, archive checksum và backfill progress.
3. Cảnh báo khi market data stale, chiến thuật chưa publish, Telegram binding bị revoke, subscription không hợp lệ hoặc quyền dataset hết hạn.
4. Runbook resume/cancel, rate-limit handling, data correction/revision, incident review và retention; không log token Telegram, chat PII hoặc raw secret.

**Acceptance:** worker health gắn với tick thành công gần nhất và quyền nguồn hiện hành; lỗi/gap hiển thị tiếng Việt, có thể truy vết tới run/manifest/evidence; dashboard hoạt động trên mobile.

## Thứ tự triển khai

1. WP-07A chart/report slice từ fills + decision + close reason.
2. WP-07C provider contract, license gate, archive validation và offline import slice; không bật archive nguồn khi policy chưa xác nhận.
3. WP-07B opt-in, durable signal path, sender và worker capability boundary.
4. WP-07D metrics/filters sau khi data fields/gaps được chốt.
5. WP-07E readiness/observability/runbook sau khi producer, sender và backfill có deployment contract.

WP-07A/B/C độc lập phần lớn và có thể thực hiện song song; migration chain và implementation-memory được tích hợp tuần tự để giữ một Alembic head và ghi evidence chính xác.

## Tham khảo và điều kiện quyền dữ liệu

Xem đầy đủ URL, nguồn/phiên bản và ngày kiểm tra tại [thiết kế WP-07](../specs/2026-11-03-trader-workbench-design.md). Đặc biệt: Binance Vision Dataset Terms áp CC BY-NC-SA 4.0, cho phép phạm vi backtest nghiên cứu thuần túy cá nhân phi sản xuất nhưng loại trừ use case thương mại/sản phẩm nếu không có enterprise license bằng văn bản. Vì vậy WBS yêu cầu rights gate và không được coi `README` code license là quyền sử dụng dữ liệu.

Khảo sát tính năng sản phẩm (03/11/2026) dùng tài liệu do chính nhà cung cấp công bố: TradingView alerts/watchlists/strategy report, Tradervue calendar và tag reports, TraderSync Mistakes report, CoinGlass derivatives API. Đây là nguồn tham khảo phạm vi chức năng, không xác nhận lợi nhuận, độ chính xác của provider, giấy phép dữ liệu hay lựa chọn nhà cung cấp cho Crypidian. Mapping đề xuất: watchlist + trạng thái/expiry vào WP-07B; calendar/tag/mistake drill-down vào WP-07D; nguồn phái sinh có provenance/rights gate vào WP-07C/E.

## Rủi ro và giới hạn

- Chart library không tự cung cấp dữ liệu giao dịch hay semantics của lệnh; Crypidian phải cung cấp các record/annotations và giữ attribution TradingView.
- Telegram không bảo đảm exactly-once khi gửi gặp timeout mơ hồ; giao diện phải nêu delivery state và alert ID ổn định.
- USD-M 1m một năm cho nhiều symbol tạo lượng dữ liệu đáng kể; người vận hành phải chọn interval/universe, còn worker có giới hạn dung lượng/thời gian.
- OHLCV không tái tạo spread, hàng đợi, intrabar order sequence hoặc liquidation chính xác; kết quả backtest phải kèm sensitivity/quality caveat.
- Dữ liệu Binance Vision không được dùng vượt quyền; terms và policy phải được xác nhận lại trước mỗi connector run.
