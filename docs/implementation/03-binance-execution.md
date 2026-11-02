# Plan 03 — Binance USDⓈ-M execution

**Mục tiêu:** tích hợp tài khoản Binance USDⓈ-M Demo qua adapter cô lập, có idempotency, bảo vệ vị thế và reconcile; LIVE vẫn bị khóa. \
**Phụ thuộc:** [Plan 01](./01-foundation-database.md), vertical slice PAPER của [Plan 02](./02-trading-core-paper.md), hợp đồng PAPER/risk gate ổn định. WP-05/06 không phải phụ thuộc kỹ thuật trực tiếp. \
**Stories:** US-02, US-16…20. \
**WBS chi tiết:** [WP-03 execution WBS](../superpowers/plans/2026-11-01-wp03-execution-wbs.md). \
**Tài liệu:** [sequence giao dịch](../product/sequence-diagrams.md#sd-06--tính-entryexitsize-risk-gate-và-gửi-entry-idempotent), [technical design](../architecture/technical-design.md#service-contracts), [glossary futures](../research/glossary-vi.md#c-futures-dòng-lệnh-và-quản-trị-lệnh).

**Foundation offline hiện có:** domain order lifecycle/reservation, HTTP outcome classifier, `BinanceDemoTarget`, signed MARKET/LIMIT REST adapter qua transport fake-only, schema/repository durable intent + append-only transition audit (`a7c9e2f4b6d8`), và `VenueOrderReconciliationService` qua verifier/ledger ports. DB trigger khóa state graph/fill monotonicity; venue rows có bot/workspace/account scope, runtime RLS và reservation được giữ `UNKNOWN` cùng transaction khi vào `SUBMITTING`. Compose E2E đạt các regression isolation/idempotency/retry/event-time/reservation và PAPER compatibility. Reconciliation application kiểm scope, freshness, fill monotonicity/status; chỉ append trạng thái venue đã xác minh, còn absent/malformed/stale/conflict giữ risk. Service chưa nối repository/composition và chưa có transaction bridge để ghi open-risk rồi transfer/release an toàn. Process-local HMAC proof chưa hỗ trợ rehydrate domain intent sau restart. Chưa có production transport, secret provider, user stream/algo protection hoặc Demo run; contract offline không chứng minh private Demo endpoints hỗ trợ.

## Giới hạn và điều kiện mở cổng

- WP-03 chỉ triển khai và xác minh USDⓈ-M Demo; không dùng tiền thật, không có đường tự chuyển sang production/LIVE.
- Có thể bắt đầu ngay việc kiểm kê API, chốt contract, adapter offline/fake, fixture và fault-injection không kết nối sàn.
- Không gửi lệnh Demo cho tới khi PAPER lifecycle và risk gate được xác nhận ổn định; định nghĩa/triển khai đầy đủ giới hạn lỗ ngày/rolling hoặc chốt policy Demo rõ ràng mà RiskEngine cưỡng chế; host/key isolation và entry-gate/reconcile đã qua test.
- Tài liệu USDⓈ-M gọi môi trường `https://demo-fapi.binance.com` và `wss://demo-fstream.binance.com` là Testnet. Trong sản phẩm, mode và credential phải biểu thị rõ Demo/testnet, độc lập với production; cặp host/key phải được cấu hình tường minh, kiểm tra trước readiness và không có fallback tự động.
- `UNKNOWN` giữ nguyên reservation/risk cho đến khi có bằng chứng đối soát đủ thẩm quyền. Không suy diễn timeout, mất kết nối hoặc response lỗi chung thành lệnh bị từ chối.

## WP-03A — Capability audit và Binance adapter

1. Lập capability matrix REST/WebSocket USDⓈ-M hiện hành: `exchangeInfo`, account/balance, positions, regular order submit/cancel/query, fills/trades, mark/funding, user stream và conditional/algo orders. Ghi endpoint, quyền API key, request/response fields, rate limit, testnet support và giới hạn lịch sử.
2. Lưu SDK/API version và nguồn chính thức đã kiểm tra. Chọn SDK chính thức nếu hỗ trợ đầy đủ semantics; chỉ dùng HTTP client giới hạn trong adapter cho phần SDK thiếu và có contract test.
3. Tạo port `ExchangeGateway`; domain không import SDK, HTTPX hay model Binance.
4. Ánh xạ symbol/filter/precision, order state/type, `clientOrderId`, `reduceOnly`, `positionSide`, trigger source, execution/fill/fee, algo ID/status, response/error code.
5. Tách mode, account ID, credential reference, REST host, WebSocket host và quyền key. Chỉ cho phép cặp host Demo đã duyệt; production host bị từ chối khi mode Demo.
6. Thực hiện clock synchronization, chữ ký trong adapter, `timestamp`/`recvWindow`, readiness theo quyền/IP và redaction.

**Lưu ý API hiện hành:** USDⓈ-M chuyển các conditional order `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `STOP`, `TAKE_PROFIT`, `TRAILING_STOP_MARKET` sang Algo Service từ **2025-12-09**. Tạo/hủy/truy vấn dùng họ endpoint `/fapi/v1/algoOrder`, `/fapi/v1/openAlgoOrders`, `/fapi/v1/allAlgoOrders`; order update dùng `ALGO_UPDATE`. Không đặt conditional order qua endpoint order thường. [Binance USDⓈ-M Change Log](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log), kiểm tra **2026-11-01**.

**Nghiệm thu:** contract fixtures bao phủ accepted/rejected/partial/unknown; request và parser theo API version đã pin; test chứng minh Demo key/mode không tạo production URL; SDK không làm mất trường/trạng thái cần thiết; capability/giới hạn Demo được ghi rõ.

## WP-03B — State machine, idempotency và UNKNOWN

1. Chốt state machine order thường `CREATED → SUBMITTING → ACKNOWLEDGED/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/UNKNOWN`; tách vòng đời conditional algo (`NEW/TRIGGERING/TRIGGERED/FINISHED/CANCELED/REJECTED/EXPIRED`) và order thường sau trigger.
2. Sinh client ID xác định từ intent; unique trong scope account và có bảng ánh xạ bền vững nội bộ. Không dựa duy nhất vào giới hạn unique của Binance để bảo đảm idempotency xuyên retry.
3. Ghi intent, risk reservation và outbox trước network request. Khóa payload fingerprint để cùng idempotency key nhưng nội dung khác bị conflict.
4. Deduplicate REST acknowledgement, user-stream event và recent trades bằng khóa venue có sẵn; xử lý event đến trễ/đảo thứ tự mà không làm state lùi.
5. Với client timeout, mất kết nối sau submit, HTTP 408/5xx hoặc response không phân loại được, giữ `UNKNOWN` nếu chưa có bằng chứng chắc chắn. Riêng HTTP 503 “Unknown error…” có thể là request đã được nhận nhưng response timeout; trước khi retry phải query stream/order history theo client ID và reconcile.
6. Phân biệt rõ 503 không xác định với 503 “Service Unavailable”/lỗi được Binance mô tả là thất bại; không retry ồ ạt. Tôn trọng 429/418, `Retry-After` và backoff.
7. Chỉ release reservation khi chứng minh order chưa được gửi hoặc reconcile xác nhận order absent; khi order/fill thành exposure, ghi exposure đồng thời với transfer reservation để không có khoảng hở ngân sách.

**Nghiệm thu:** fault injection timeout sau khi Binance nhận lệnh không tạo lệnh thứ hai; UNKNOWN không bị gán REJECTED tùy tiện; unknown reservation không hết hạn tự giải phóng; state transition có actor/source/time; đối chiếu API 503 handling với [Binance General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info), kiểm tra **2026-11-01**.

## WP-03C — User stream, partial fill và bảo vệ

1. Tạo/renew/đóng listen key; triển khai reconnect, connection rotation trước giới hạn 24 giờ, listen-key expiry, event dedupe và REST recovery khi mất stream.
2. Đọc `ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, `ALGO_UPDATE`; chuẩn hóa transaction/event time, execution type, trade ID, last/cumulative fill, average price, commission/asset và position side.
3. Với mỗi partial fill đã xác nhận, cập nhật exposure thực và đặt/điều chỉnh protective algo order cho đúng lượng đang mở; lưu liên kết client/exchange ID cùng trigger source/price/quantity.
4. Không xem ACK create algo là đã khớp hoặc đã được bảo vệ nếu trạng thái chưa xác nhận. `REJECTED`, `EXPIRED`, trigger rejection, mất stop hoặc mismatch phải tạo incident, đóng entry gate và chạy recovery policy đã duyệt.
5. Xử lý race giữa fill mới, cancel/replace, stop trigger, exit fill và position quantity thay đổi; sau mỗi thao tác reconcile protection so với vị thế venue.
6. Protective management và reconcile chạy độc lập khỏi web, Telegram và AI.

**Nghiệm thu:** partial fill có stop đúng quantity; stop/algo reject đóng gate và báo incident; position zero không giữ stop dư; trigger/replace race không làm tăng exposure ngoài ý muốn; mất control plane không ngăn runtime tiếp tục giám sát và khôi phục bảo vệ. Nguồn stream: [Binance USDⓈ-M User Data Streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams), kiểm tra **2026-11-01**.

## WP-03D — Startup/reconnect reconciliation và readiness

1. Khi process khởi động, mất stream, đổi listen key hoặc phát hiện event gap, đóng entry gate trước khi phát lệnh mới.
2. Lấy snapshot balances/positions/open normal orders/open algo orders/recent trades; kiểm tra freshness/scope; đối chiếu intent, reservation, fills, positions và protective orders nội bộ.
3. Phân loại orphan order/position, foreign activity, missing protection, quantity/fee mismatch, UNKNOWN, stale/incomplete snapshot và duplicate event thành incident có reason code.
4. Chỉ mở entry khi state đã reconcile, risk/account snapshot hợp lệ, protection được xác nhận, feed/account healthy và policy cho phép. Không tự adopt/hủy/flatten foreign activity.
5. Giữ protective exit/reconcile khả dụng khi entry pause, UI/Telegram lỗi hoặc cap bị siết; cap breach không tự flatten vị thế.
6. Viết runbook tiếng Việt cho restart, key rotate/revoke, API outage, 429/418, từng loại 503, stream expiry/reconnect, stop rejection và reconciliation conflict.

**Nghiệm thu:** restart khi có order/position không duplicate; mọi discrepancy giữ entry đóng tới khi xử lý hoặc được người có thẩm quyền phê duyệt; snapshot chưa xác minh không được coi là zero exposure.

## WP-03E — Binance Demo contract run

1. Chỉ chạy sau khi các gate trong “Giới hạn và điều kiện mở cổng” được chứng minh; dùng account/key Demo chuyên dụng, quyền tối thiểu và endpoint allowlist.
2. Kiểm tra symbol filter/precision, account/position snapshot, market/limit order, cancel/query, conditional algo stop/TP, fill/fee và exposure reconciliation. Không giả định Demo hỗ trợ partial fill, cancel/replace hoặc fault scenario nếu chưa quan sát và lưu bằng chứng.
3. Thực hiện fault drill có kiểm soát cho timeout-after-accept, unknown 503, duplicate/out-of-order event, stream expiry/reconnect, 429/418 và algo rejection. Không tạo hoạt động làm ảnh hưởng account ngoài kịch bản đã định.
4. Lưu evidence package đã khử secret: SDK/API schema version, mode/host, timestamp, sanitized request/response, event sequence, state/reservation transitions, reconcile result và giới hạn simulator.

**Nghiệm thu:** Demo contract suite đạt trên endpoint Demo; từng order/position/protective order được đối soát; mọi lỗi không rõ giữ gate/reservation; không có code path hoặc config tự chuyển mode/host/key sang LIVE.

## Nguồn API chính thức đã xác minh

- [USDⓈ-M General Info — production và testnet REST/WebSocket host; HTTP 503/429/418](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info) — kiểm tra **2026-11-01**.
- [USDⓈ-M User Data Streams — listen key, 24 giờ kết nối, ordering và event `ALGO_UPDATE`](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams) — kiểm tra **2026-11-01**.
- [USDⓈ-M Change Log — Algo Service migration từ 2025-12-09, `/fapi/v1/algoOrder`, conditional rejection qua `ALGO_UPDATE`](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log) — kiểm tra **2026-11-01**.
- [USDⓈ-M REST API catalog](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api) và [WebSocket API/Streams catalog](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams) — dùng để đóng băng endpoint/schema cụ thể trong WP-03A; kiểm tra **2026-11-01**.
