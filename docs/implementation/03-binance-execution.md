# Plan 03 — Binance USDⓈ-M execution

**Mục tiêu:** tích hợp tài khoản Binance Demo qua adapter cô lập, có idempotency, bảo vệ vị thế và reconcile; live vẫn khóa. \
**Phụ thuộc:** [Plan 01](./01-foundation-database.md), vertical slice PAPER của [Plan 02](./02-trading-core-paper.md). \
**Stories:** US-02, US-16…20. \
**Tài liệu:** [sequence giao dịch](../product/sequence-diagrams.md#sd-06--tính-entryexitsize-risk-gate-và-gửi-entry-idempotent), [technical design](../architecture/technical-design.md#service-contracts), [glossary futures](../research/glossary-vi.md#c-futures-dòng-lệnh-và-quản-trị-lệnh).

## WP-03A — Capability audit và Binance adapter

1. Lập danh sách REST/WebSocket endpoint cần dùng: exchangeInfo, account/balance, positions, order submit/cancel/query, conditional orders, fills/trades, mark/funding và user stream.
2. Ghi bảng capability theo Binance SDK version; dùng SDK chính thức khi đúng semantics; HTTPX fallback giới hạn trong adapter cho endpoint chưa được SDK mô hình hóa đầy đủ.
3. Tạo interface `ExchangeGateway`; domain không import SDK/HTTPX/Binance model.
4. Ánh xạ symbol, precision, status, order type, `clientOrderId`, `reduceOnly`, `positionSide`, trigger source và response/error code.
5. Tách rõ Demo endpoint, credential reference, account ID và mode; không cho fallback tự động sang production.
6. Thêm clock synchronization, signature handling trong gateway, IP/permission readiness và redaction.

**Nghiệm thu:** contract fixtures bao phủ accepted/rejected/partial order; request order ký đúng schema theo version; demo credential không thể gửi production URL; SDK không thiếu semantics trong silence.

## WP-03B — State machine, idempotency và UNKNOWN

1. Chốt state machine `CREATED → SUBMITTING → ACKNOWLEDGED/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/UNKNOWN` và transition hợp lệ.
2. Sinh `clientOrderId` ổn định theo intent; unique theo account; ghi intent/reservation/outbox trước network request.
3. Consume REST ack/user stream/recent trades theo dedup keys; bảo đảm event đến trễ không làm state lùi sai.
4. Khi timeout/503 hoặc kết quả mơ hồ: giữ `UNKNOWN`, không giải phóng risk, query theo client ID/order history và reconcile bằng dữ liệu có thẩm quyền.
5. Chỉ retry sau khi policy xác nhận không tồn tại lệnh và idempotency semantics cho phép.

**Nghiệm thu:** fault injection timeout sau khi exchange đã nhận không tạo lệnh thứ hai; UNKNOWN không bị đánh dấu rejected tùy tiện; mọi transition có actor/source/time.

## WP-03C — User stream, partial fill và bảo vệ

1. Xây lifecycle listen key/user stream: khởi tạo, renew, reconnect, sequence gap, REST recovery.
2. Với fill xác nhận, cập nhật actual quantity/average/commission và bảo vệ chính xác exposure đã fill.
3. Mô hình hóa conditional stop/TP theo endpoint đang hỗ trợ; lưu exchange order ID và protective quantity link.
4. Khi stop không xác nhận, reject hoặc mất: đóng entry gate, incident khẩn, retry/replace hoặc reduce-only flatten theo policy cấu hình; không báo protected trước exchange ack.
5. Xử lý race cancel/replace, stop trigger trong lúc replace và position quantity thay đổi.
6. Protective management/reconcile phải chạy độc lập với web, Telegram và AI.

**Nghiệm thu:** demo scenario partial fill có stop cho đúng lượng; stop rejection tạo alert và block entry; vị thế zero không còn stop dư trái phép; control plane outage không làm mất giám sát bảo vệ.

## WP-03D — Startup/reconnect reconciliation và readiness

1. Khi process khởi động hoặc stream gián đoạn, đặt entry gate đóng.
2. Lấy snapshot balances/positions/open orders/recent trades từ Binance; đối chiếu order intents, fills, positions và stops nội bộ.
3. Phân loại orphan order/position, missing stop, quantity mismatch, UNKNOWN và dữ liệu trễ thành incident riêng.
4. Chỉ mở entry sau khi state khớp, protection xác nhận, feed/account healthy và policy cho phép.
5. Viết runbook tiếng Việt cho restart, key rotate, API outage, 429/418, 5xx unknown và reconciliation conflict.

**Nghiệm thu:** test restart khi có vị thế và order mở không tạo duplicate; mọi discrepancy giữ entries đóng cho đến khi giải quyết/được người có quyền phê duyệt.

## WP-03E — Binance Demo contract run

1. Dùng tài khoản/credential Demo chuyên dụng, giới hạn quyền, không dùng tiền thật.
2. Kiểm tra symbol filter, order precision, market/limit/stop conditional, cancel/replace, partial fill nếu simulator cho phép, funding/account snapshots.
3. Thực hiện fault drill có kiểm soát cho timeout, stream reconnect, duplicate events và rate limits.
4. Lưu evidence package gồm SDK version, endpoint, timestamp, sanitized request/response, reconciliation result và known limitations.

**Nghiệm thu:** Demo contract suite đạt; mọi order state được đối soát; không có đường code chuyển mode hoặc endpoint sang LIVE tự động.
