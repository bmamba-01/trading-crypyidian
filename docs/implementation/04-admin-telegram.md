# Plan 04 — Admin web và Telegram

**Mục tiêu:** cung cấp giao diện quản trị tiếng Việt, quản lý cấu hình theo version, quyền tối thiểu và điều khiển Telegram có xác thực server-side. \
**Phụ thuộc:** [Plan 01](./01-foundation-database.md); order/trade queries ổn định từ [Plan 02](./02-trading-core-paper.md) để hoàn thiện dashboard. \
**Stories:** US-01…07, US-21, US-22, US-24, US-25, US-28. \
**Tài liệu:** [UC admin](../product/use-cases.md#4-chi-tiết-use-case), [sequence SD-01/02/12](../product/sequence-diagrams.md), [glossary](../research/glossary-vi.md), [language policy](../standards/language-policy.md).

## WP-04A — API, session và authorization layer

1. Xây `FastAPI` routers cho auth, workspaces, users, accounts, config, risk, watchlists, sources, strategies, trades, incidents, reports và experiments.
2. Với mỗi endpoint ghi rõ principal, workspace scope, function permission, object permission, input schema, idempotency và audit side effect.
3. Áp dụng session cookie secure/HttpOnly/SameSite, CSRF cho mutation, rate limit login/webhook và request correlation.
4. Dùng optimistic version/ETag cho config; xung đột trả thông báo tiếng Việt và không ghi đè.
5. Sinh OpenAPI mô tả bằng tiếng Việt; `operationId`, field name và error code máy đọc dùng tiếng Anh.

**Nghiệm thu:** mọi endpoint có permission test; gọi API trực tiếp cũng bị chặn theo role; mutation có audit; dữ liệu workspace khác không được lộ qua ID đoán được.

## WP-04B — Admin UI server-rendered

1. Tạo Jinja2 + HTMX layout tiếng Việt, ưu tiên vận hành đơn giản; JS chỉ dùng cho chart/interaction cần thiết.
2. Screens: setup, mode badge, credential ref, member/role, config draft/publish, risk limits, watchlist, strategy release, source registry, trade timeline, incident, report, experiment/promotion.
3. Mọi thuật ngữ kỹ thuật có tooltip/link glossary; hiển thị tên strategy/model tiếng Việt cùng ID/version tiếng Anh.
4. Secrets chỉ nhập mới/rotate, không render lại giá trị. Các màn hình live luôn hiển thị environment và trạng thái readiness.
5. Thêm filter, pagination, UTC/local display policy, empty/error/loading states và accessible form labels.

**Nghiệm thu:** người vận hành hiểu mode/account trước khi thao tác; UI không lộ key; thao tác không được phép không xuất hiện và API vẫn chặn khi gọi thủ công; error message có cách xử lý tiếng Việt.

## WP-04C — Configuration approval, audit và dashboards

1. Xây config lifecycle draft → validated → proposed → approved → active/retired, bất biến sau publish.
2. Bắt buộc reason cho credential/risk/strategy/live-sensitive actions; hỗ trợ dual approval khi policy yêu cầu.
3. Render audit trail có actor, thời điểm, target, before/after hash, reason và correlation ID; quyền xem audit riêng.
4. Dashboard lọc mode/strategy/symbol/time; phân tách gross/net PnL, fees, funding, slippage, drawdown, exposure, trade count và coverage.
5. Báo cáo phải nêu dữ liệu chưa đủ và khác biệt PAPER/DEMO/LIVE; export giữ phạm vi/filter/version.

**Nghiệm thu:** config version conflict không mất dữ liệu; dashboard không gộp mode; audit có thể truy từ lệnh về config và người duyệt.

## WP-04D — Telegram webhook, bind và command policy

1. Tạo one-time binding code từ web; lưu `telegram_user_id`, `chat_id`, workspace/user và thời gian bind/revoke.
2. Xác minh webhook secret header constant-time; ghi `update_id` unique để deduplicate; reject payload sai schema.
3. Allowlist command: `/bind`, `/status`, `/pause_entries`, `/resume_entries` (nếu policy cho phép), `/help`; không nhận câu lệnh tự do làm hành động trading.
4. Map command sang user đã bind, áp RBAC phía server, yêu cầu confirm cho hành động nhạy cảm và log lý do.
5. Notification đi qua outbox; retry/backoff có giới hạn; khi Telegram lỗi, alert vẫn hiện trên web.
6. Tin nhắn phải tiếng Việt, nêu mode/workspace/reason/action link, không bao giờ có secret hoặc dữ liệu riêng tư thừa.

**Nghiệm thu:** update giả/header sai bị từ chối; update lặp không lặp pause/resume; viewer không resume; Telegram outage không làm pause/resume state bị đoán sai.

## WP-04E — Hướng dẫn vận hành và kiểm tra giao diện

Viết `docs/operations/admin-guide.md` tiếng Việt: khởi tạo owner, bật MFA, bind Telegram, thêm account demo, cấu hình risk/watchlist, publish config, đọc trade timeline, xử lý incident và thu hồi quyền. Có ảnh/chú giải nếu cần; thuật ngữ dẫn tới glossary.

**Kiểm tra khi triển khai:** integration tests cho RBAC/config version/webhook dedup; browser smoke tests cho Owner, Operator, Analyst và trường hợp bị từ chối; tất cả hướng dẫn phải kiểm tra với đúng phiên bản UI.
