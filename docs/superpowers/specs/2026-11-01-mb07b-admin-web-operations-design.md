# Thiết kế MB-07b — Admin Web và thao tác vận hành

## Ý định

Hoàn thiện giao diện quản trị nhiều bot và các thao tác vận hành còn thiếu trong WBS MB-07b. Owner và operator được xem trạng thái, cấu hình, báo cáo và điều khiển bot qua giao diện có session/RBAC/CSRF hiện hữu. Các thao tác rủi ro cao được ràng buộc vào dữ liệu đã xem, MFA gần đây, audit và idempotency.

Phạm vi giao dịch là PAPER. Không gọi Binance private API, không mở LIVE và không nhận hoạt động lạ trên venue. Mục tiêu là có luồng quản trị có thể kiểm tra qua browser E2E và database Compose thật.

## Mục tiêu

- Dashboard workspace; danh sách và trang chi tiết bot; tài khoản, cấu hình chiến lược, readiness, incident và báo cáo.
- Cho phép owner bật bot đã disable sau khi kiểm tra điều kiện an toàn; không tự gỡ pause/kill hay incident.
- Cho phép owner xem trước và xác nhận flatten-all theo đúng snapshot, yêu cầu step-up/MFA, lưu operation bền vững và chống lặp.
- Cho phép thành viên đã xác thực liên kết hoặc thu hồi Telegram bằng mã một lần; chỉ private chat, hỗ trợ revoke rồi bind lại.
- Bảo vệ mọi trang và mutation theo quyền workspace/bot, session, CSRF, audit, giới hạn lỗi và cách ly secret.
- Đáp ứng accessibility mục tiêu WCAG 2.2 AA cho luồng chính; có kiểm tra tự động và checklist thủ công.

## Ngoài phạm vi

- Thay đổi chiến lược, risk cap, báo cáo MB-06, ingestion WP-05 hoặc AI WP-06.
- Gửi lệnh Telegram để flatten; Telegram chỉ phục vụ các lệnh operator đã có.
- Binance Demo/Live order execution hoặc thay đổi cấu hình chuyển mode.
- SPA/frontend framework riêng nếu template server-rendered đáp ứng luồng này.

## Ràng buộc và bất biến

- Giữ FastAPI làm HTTP server. Handler/template chỉ gọi service/query hiện hữu hoặc application command mới; không ghi DB trực tiếp và không gọi venue.
- Session và role/grant được kiểm phía server cho từng request. Bot ngoài scope và bot thiếu grant trả cùng kết quả không tiết lộ tồn tại.
- Mọi mutation web cần CSRF và audit. Mọi thao tác nhạy cảm cần MFA còn hiệu lực theo policy hiện hành.
- `enable_bot` chỉ đổi trạng thái account sau khi kiểm trong cùng transaction/lock domain: bot/account mirror, workspace kill hiện hành, account status, incident, runtime activity/readiness và bot-control revision đã đối soát, config PUBLISHED. Không tự đổi pause/kill state. Không yêu cầu runtime đã áp workspace revision mới nhất khi bot DISABLED vì runtime roster loại bot này; L1 + kiểm workspace kill hiện hành giữ điều kiện an toàn và runtime sẽ dùng control hiện hành khi bot quay lại roster.
- Flatten-all cần preview băm theo snapshot/lista đối tượng và thời hạn; confirm phải khớp preview hash, revision và idempotency key. Operation lưu request hash, trạng thái và kết quả từng bot/vị thế. Partial/blocked giữ entry bị chặn cho tới reconcile.
- Mã Telegram lưu dạng hash, TTL tối đa 10 phút, tiêu thụ một lần, không ghi mã vào log/audit. Membership bị khóa làm binding mất hiệu lực ngay khi runtime kiểm tra.
- Workspace aggregate chỉ được mở rõ ràng và qua quyền phù hợp; mặc định trang báo cáo là bot scope qua `BotReportingService`.
- Không log credential, token, MFA seed hoặc raw one-time code.

## Các cách đã cân nhắc

1. **FastAPI + Jinja2, triển khai theo lát cắt WBS.** Khớp kiến trúc hiện có, tái dùng cookie/CSRF/service/API, giảm JavaScript và tránh hai bộ kiểm tra quyền. Cần dependency template và browser E2E. Đây là lựa chọn đề xuất và giả định cho bản thiết kế này.
2. **SPA riêng gọi Admin API.** Có thể hỗ trợ tương tác phong phú hơn, nhưng thêm toolchain/frontend runtime và phải xử lý đồng bộ auth/CSRF/RBAC ở client. Không phù hợp mục tiêu hoàn thiện nhanh trên backend hiện có.
3. **Chỉ làm dashboard/report/pause-resume trước.** Giảm phạm vi vòng đầu, nhưng để flatten-all và Telegram về sau dù các contract đang nằm cùng WBS. Có thể chọn nếu muốn tách milestone trước khi code.

## Phân rã triển khai

1. **Khóa contract/inventory:** đọc service/router/schema MB-02/05/06/07a, session/MFA/CSRF/RBAC hiện tại; xác định route, view model, migration, transaction/lock order, audit event và threat cases. Kiểm tài liệu chính thức FastAPI/Jinja2/Playwright và khóa dependency tương thích trong vòng này.
2. **Admin Web cơ bản:** template base/navigation, dashboard workspace, bot list/detail, account/config workflow, readiness/incidents, bot/workspace reports. Quyền truy cập qua service hiện hữu; template không chứa business logic.
3. **Enable bot:** command/service/API + UI; kiểm precondition trong transaction với cùng thứ tự khóa đang được MB-05/MB-04 dùng; audit cả allow/deny/no-op; thêm test race với pause/disable.
4. **Flatten-all:** preview read-only, operation ledger bền vững, step-up MFA gắn với nội dung, idempotent confirm, per-position outcome và restart/reconcile. Không tạo route Telegram tương ứng.
5. **Telegram binding:** issue/consume one-time code, private `/start <code>`, callback nonce, revoke, rebind và membership lock. Tận dụng webhook/binding hiện có; migration/unique constraint chỉ thêm khi inventory chứng minh cần.
6. **Accessibility/security:** keyboard/focus/error summary/labels/semantic HTML, CSP-safe templates, output encoding, session expiry và không lộ secret.
7. **Regression/review:** browser E2E trên Compose, DB/RLS thật, unit/API tests, concurrency/recovery cases, static quality, migration checks và independent review.

## Tiêu chí hoàn tất

- AC của MB07b.0–.6 trong [WBS](../plans/2026-10-29-mb07b-admin-web-flatten-telegram-wbs.md) đạt; không có test skip hoặc E2E chưa chạy.
- Owner/admin/risk_admin/strategy_admin/operator/grant/analyst không vượt role, grant hoặc bot scope.
- Stale preview/revision, expired/reused Telegram code, unauthorized step-up và replay không gây side effect.
- Retry operation giữ nguyên kết quả theo idempotency key; restart/reconcile không gửi trùng PAPER close.
- Template, HTML response, logs và audit không chứa secret; các lệnh sản phẩm chỉ tác động PAPER.
- Cập nhật implementation memory, project status/WBS và đọc lại memory sau vòng implement; không commit cho tới khi unit + E2E + review đạt.

## Quyết định

Ngày 2026-11-01, người dùng yêu cầu bắt đầu MB-07 sau khi rà soát và xác nhận các milestone trước đã hoàn tất. Rà soát MB-05b, MB-06 và MB-07a đạt; theo điều kiện đó, triển khai phương án 1 (FastAPI + Jinja2, server-rendered) được xem là được duyệt. Chi tiết route/schema được khóa theo inventory MB07b.0; dependency Jinja2/Playwright được ghi ở kế hoạch và implementation memory.

### Task 5 inventory/ruling

- `admin-web` và `trading-runtime` là process/service tách credential; API web không được gọi gateway RAM của runtime để tự đóng vị thế. `BotRuntimeSupervisor` là worker giữ lease/fencing hiện hành, nên confirm cần ghi operation bền vững để runtime xử lý sau.
- Giá PAPER dùng để đóng phải có provenance. Preview/worker đọc nến `MarketBar` đã đóng theo `MarkPolicy` trong workspace settings, chỉ dùng nguồn/timeframe cấu hình, và từ chối mark cũ hơn `risk.max_mark_age_seconds`. Mỗi preview item giữ ID/revision/checksum/close_time của mark; confirm re-read và từ chối nếu snapshot đã thay đổi.
- Confirm owner + MFA gần đây + reason + idempotency tạo operation ledger, rồi yêu cầu pause từng bot qua `BotOperationsService` hiện hữu. Runtime chỉ xử lý phần bot đã pause và đang READY/đang giữ lease. Vị thế đóng qua `PaperLifecycleCoordinator` với close key ổn định, event/checkpoint được commit cùng ledger item; operation hoàn thành một phần không tự resume bot. Reconcile và resume là hành động riêng.
- Open order UNKNOWN và reservation không xác định không được xóa/giải phóng bằng suy đoán. Operation giữ bot pause, ghi BLOCKED cho mục chưa thể xác nhận và đòi reconcile. Không dùng `entry_reference_price` làm giá thoát giả.

## Nguồn chính thức đã kiểm tra ngày 2026-11-01

- [FastAPI — Templates](https://fastapi.tiangolo.com/advanced/templates/): tích hợp `Jinja2Templates` qua FastAPI/Starlette.
- [Jinja — Installation](https://jinja.palletsprojects.com/en/stable/intro/) và [changelog](https://jinja.palletsprojects.com/en/stable/changes/): Jinja hỗ trợ autoescape cho HTML; PyPI hiện công bố 3.1.6.
- [Playwright Python — Installation](https://playwright.dev/python/docs/intro), [browsers](https://playwright.dev/python/docs/browsers) và [CI](https://playwright.dev/python/docs/ci): Python hỗ trợ, phiên bản package gắn với browser binaries; PyPI hiện công bố Playwright 1.64.1 và pytest-playwright 0.10.0.
- [Telegram Bot Features — Deep Linking](https://core.telegram.org/bots/features#deep-linking): contract `/start` private-chat và giới hạn tham số.
- [OWASP — Transaction Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html): step-up phải ràng buộc thao tác đã xác nhận.
- [W3C — WCAG 2.2](https://www.w3.org/TR/WCAG22/): accessibility target AA.
- [PostgreSQL 18 — Partial Indexes](https://www.postgresql.org/docs/18/indexes-partial.html) và [Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html): khả năng unique constraint cho binding ACTIVE.
