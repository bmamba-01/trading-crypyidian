# MB-07b WBS — Admin Web, flatten-all và liên kết Telegram

**Trạng thái:** MB-07b đang triển khai trên `dev`; Task 1–6 đã có implementation trong working tree, còn E2E/accessibility/review cuối.
**Ngày:** 2026-10-29
**Phạm vi gốc:** Task 8 / MB-07 trong [kế hoạch Multi-Bot](./2026-10-24-multibot-implementation-plan.md); tiếp nối [WBS MB-07a](./2026-10-27-mb07a-bot-control-api-telegram-wbs.md).
**Liên quan:** [RBAC và cấu hình bot](./2026-10-24-mb02-bot-config-wbs.md), [runtime/fencing](./2026-10-26-mb05-runtime-registry-wbs.md), [báo cáo bot](./2026-10-27-mb06-bot-reports-wbs.md), [nghiên cứu bảo mật](../../research/security-admin-bot-2026.md), [glossary EN→VI](../../research/glossary-vi.md).

## Mục tiêu và ranh giới

Hoàn thiện giao diện web quản trị nhiều bot, bổ sung luồng flatten-all có preview và xác nhận gắn với nội dung thao tác, cho phép owner bật lại bot đã disable, và quản lý liên kết Telegram bằng mã một lần. Mọi thao tác dùng chung application services hiện có; template/HTTP handler không tự ghi database hay gọi trực tiếp sàn. Phạm vi giao dịch chỉ PAPER; không bật Binance private execution/LIVE.

**Ngoài phạm vi:** chỉnh rule chiến lược, tạo strategy mới, thu thập nguồn WP-05, huấn luyện WP-06, sửa báo cáo MB-06, lệnh flatten qua Telegram, hoặc thay đổi risk cap. Flatten chỉ đóng các vị thế PAPER đã biết và hủy entry chưa khớp; không nhận/adopt hoạt động lạ trên venue.

## Quyết định và bất biến dự kiến

1. Giao diện server-rendered dùng FastAPI/Jinja2 và component HTML hiện có; trước khi khóa dependency phải kiểm tra lockfile/runtime và tài liệu chính thức hiện hành. Không thêm frontend framework nếu server-side đủ.
2. Mọi route web áp dụng session, CSRF cho mutation, RBAC/grant theo bot, 404 không phân biệt bot ngoài scope và thiếu grant, audit theo cùng service.
3. Flatten-all workspace là hành động riêng chỉ owner được phép, cần MFA gần đây và lý do. Preview hiển thị số bot/vị thế/lệnh, giá trị mark và thời điểm dữ liệu; xác nhận buộc khớp `preview_hash`, thời hạn và idempotency key. Nếu snapshot, revision control hoặc danh sách vị thế thay đổi thì từ chối và yêu cầu preview lại. Không có endpoint flatten-all Telegram.
4. Hành động flatten tạo một operation bền vững có trạng thái, request hash và kết quả từng bot/position; retry cùng idempotency key trả lại operation cũ. Mỗi đóng lệnh vẫn đi qua L1/fencing/control/risk coordinator, không bỏ qua pause/kill hoặc protective monitoring. Nếu không thể đóng một phần tử, operation báo partial/blocked và giữ entry bị chặn đến reconcile.
5. Enable bot chỉ owner + MFA gần đây + lý do; chỉ áp dụng sau khi kiểm workspace không killed, account hoạt động, incident đã resolve, runtime đã reconcile, và cấu hình PUBLISHED hợp lệ. Không tự resume control hoặc xóa incident.
6. Mã liên kết Telegram sinh ngẫu nhiên, lưu hash, TTL tối đa 10 phút, dùng một lần, buộc workspace deployment và membership đã xác thực; không đưa mã vào log/audit/analytics. Xác nhận liên kết bằng callback nonce có hạn, step-up/MFA ở Admin Web; thu hồi yêu cầu step-up. Chỉ private chat; không hỗ trợ group.
7. Sau revoke, cùng Telegram user có thể bind lại nhưng không được có hai binding ACTIVE cho cùng identity trong workspace. Khi membership/user bị khóa, binding mất hiệu lực ngay theo kiểm tra runtime; thao tác thu hồi hàng loạt có audit.
8. Báo cáo trang bot/workspace chỉ gọi `BotReportingService`; bot scope là mặc định, workspace aggregate phải được chọn rõ và có quyền phù hợp.
9. Trang và trạng thái quan trọng đáp ứng accessibility WCAG 2.2 AA ở các luồng keyboard/focus/label/contrast có thể kiểm tự động; review thủ công bổ sung.

## WBS, thứ tự và điều kiện nghiệm thu

| ID | Đầu việc | Kết quả chính | Điều kiện nghiệm thu |
|---|---|---|---|
| MB07b.0 | Khóa contract và inventory | Kiểm tra service/router/schema hiện có của MB-02/05/06/07a; rà dependency/lock; xác định migration cần thiết và quyền từng route. | Không trùng service hoặc thay đổi ngoài scope; ghi ADR/rulings, API/view model và threat cases trước code; nguồn tài liệu/API có URL + ngày kiểm tra trong memory. |
| MB07b.1 | Server-rendered Admin Web | Dashboard trạng thái workspace; bot list/detail; create/bind account; strategy overlay draft/validate/publish; pause/resume; report bot/workspace; runtime readiness và incident. | Mọi view đọc đúng service/query; permission matrix owner/admin/risk_admin/strategy_admin/operator/grant/analyst; không lộ bot ngoài scope; tất cả mutation có CSRF và audit. |
| MB07b.2 | Enable bot đã disable | Application command/service, schema/API và UI; owner + MFA + reason; transaction kiểm account/bot mirror, root config, runtime state, workspace control và incident. | Resume chỉ chuyển account ENABLED khi toàn bộ precondition đạt; không tự resume runtime pause/kill; conflict/failure không đổi state; test concurrency với pause/disable và audit deny/noop. |
| MB07b.3 | Flatten-all preview/confirm | Read-only preview, preview hash/expiry, confirmation step-up gắn nội dung, idempotency ledger/operation state và xử lý từng bot/position. | Chỉ owner+MFA; stale preview/revision bị từ chối; replay cùng key không nhân đôi lệnh; flatten Telegram không tồn tại; partial failure hiển thị chính xác và không mở entry mới; protective monitoring còn hoạt động. |
| MB07b.4 | Telegram link/revoke | Một lần code, hash-at-rest, TTL, verify qua `/start <code>` private-chat, callback nonce, revoke, rebind và xử lý membership locked. | Code hết hạn/phát lại/sai workspace không bind; code và secret không lộ; rebind sau revoke được; active duplicate bị chặn trong DB; user locked không thể điều khiển ngay cả trước cleanup nền. |
| MB07b.5 | UI accessibility/security polish | Navigation keyboard, focus/error summary, labels/semantic HTML, CSP-safe templates, output encoding, no inline secret, CSRF/session expiry behavior. | Automated accessibility checks đạt cho luồng chính; manual checklist có kết quả; kiểm tra HTML/log/API không chứa credential, MFA secret, webhook secret hoặc link token. |
| MB07b.6 | E2E, regression và review | Playwright end-to-end trên Compose image dùng chung; PostgreSQL/RLS thật; operation idempotency, concurrency, restart recovery; independent review. | Unit + API + browser E2E không skip; full unit/E2E liên quan pass; Ruff/format/mypy/Alembic/Compose/diff pass; review không còn finding mức cao; memory cập nhật và đọc lại trước commit. |

**Ruling 2026-11-01 (MB07b.3):** admin-web không dùng runtime gateway trong RAM vì process/DB role riêng. Confirm ghi operation ledger bền vững; `BotRuntimeSupervisor` thực thi từng bot dưới lease/fence. Mark PAPER là `MarketBar` đã đóng theo MarkPolicy workspace, còn mới theo max-mark-age; mark thiếu/cũ chặn item và giữ bot PAUSED. Không tự resume khi operation partial/blocked.

## Kế hoạch test-first

1. Viết test permission/view model và mã lỗi trước template/route.
2. Viết RED cho `enable_bot` precondition/transaction/audit và race disable/pause.
3. Viết RED cho flatten preview hash, expiration, MFA, idempotency, stale revision, partial result và replay trước service/repository.
4. Viết RED cho mã Telegram hết hạn, consume một lần, scope, active uniqueness/rebind, revoke và membership lock trước migration/handler.
5. Dùng Playwright qua giao diện thật; chỉ stub Telegram outbound bằng port, không gọi mạng Telegram. Mọi lệnh giao dịch trong test chỉ PAPER.
6. Chạy unit/API/E2E nhắm mục tiêu sau từng gói, sau đó full suites và security review. Test chưa chạy/skip/flaky chưa điều tra không tính pass.

## Phụ thuộc và trình tự

`MB-05b + MB-07a E2E/review` → `MB07b.0` → `MB07b.1` và `MB07b.2` (service/UI cơ bản) → `MB07b.3` flatten operation → `MB07b.4` Telegram link lifecycle → `MB07b.5` accessibility/security → `MB07b.6` full regression/review.

Có thể tách worktree cho WBS/implementation sau khi dependency MB-07a được tích hợp; không sửa hoặc cherry-pick thay đổi đang ở worktree khác khi chưa xác nhận commit/base SHA.

## Thuật ngữ EN→VI đã bổ sung vào glossary

- **Server-rendered UI** → *giao diện kết xuất phía máy chủ*: server dựng HTML từ template thay vì SPA riêng.
- **Step-up authentication** → *xác thực tăng cường*: yêu cầu xác thực/MFA gần thời điểm thao tác rủi ro cao.
- **WYSIWYS confirmation** → *xác nhận đúng nội dung sẽ thực thi*: người dùng xác nhận đúng danh sách/vị thế và số liệu đang được hiển thị.
- **Idempotency key** → *khóa chống thực thi lặp*: định danh một thao tác để retry không tạo thêm lệnh.
- **One-time link code** → *mã liên kết dùng một lần*: bí mật ngắn hạn, chỉ được tiêu thụ đúng một lần để gắn Telegram identity.
- **Partial completion** → *hoàn tất một phần*: operation thực hiện được một số mục nhưng còn mục thất bại/chờ reconcile.
- **Accessibility** → *khả năng tiếp cận*: khả năng dùng giao diện bằng công nghệ hỗ trợ và điều hướng bàn phím.

Các mục trên đã được thêm vào [glossary EN→VI](../../research/glossary-vi.md) cùng nguồn giải thích.

## Nguồn thiết kế đã kiểm tra (2026-10-29)

- Telegram, [Bot Features — Deep Linking](https://core.telegram.org/bots/features#deep-linking): tham số `start` trong chat riêng được chuyển thành `/start <parameter>`; nguồn này quy định giới hạn định dạng/độ dài tham số nên mã liên kết cần thiết kế nằm trong giới hạn, có entropy phù hợp và không phải credential dài hạn.
- OWASP, [Transaction Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html): step-up và xác nhận nội dung giao dịch phải gắn với hành động cụ thể, khó sửa đổi và chỉ sử dụng một lần.
- W3C, [Web Content Accessibility Guidelines 2.2](https://www.w3.org/TR/WCAG22/): mục tiêu accessibility của giao diện là mức AA; đánh giá conformance theo toàn trang và tiêu chí tương ứng.
- PostgreSQL 18, [Partial Indexes](https://www.postgresql.org/docs/18/indexes-partial.html) và [Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html): unique partial index có thể đảm bảo không trùng các binding ACTIVE trong khi cho phép lưu lịch sử binding đã revoke; migration chỉ quyết định sau khi inventory schema hiện tại.

Tài liệu/version runtime của FastAPI/Jinja2/Playwright và Telegram Bot API cần được kiểm tra lại từ tài liệu chính thức ngay trước khi khóa dependency/API trong `MB07b.0`.

### MB-07b progress (2026-11-01)

- Task 1–3: template runtime/dependencies, read views, CSRF-protected HTML forms hoàn thành ở working tree; browser E2E còn chờ task 7.
- Task 4 enable: implementation và service/API/DB-role integration đạt. Unit regression 134/134; E2E `test_bot_control_api_telegram.py` + `test_db_role_least_privilege.py` 2/2; Ruff, format, mypy và wheel đạt. Root cause SQL NULL ở trigger marker đã được sửa fail-closed. **Còn race E2E enable-vs-disable/pause.**
- Task 5 đang triển khai: preview/confirm/status API và HTML review/result, RLS ledger migration, snapshot provider, runtime worker dưới workspace lock/fence và PAPER reduce-only close đã có trong working tree. Full unit 1615/1615; E2E combined migration/enable/control/DB-role có runtime operation riêng process 4/4; Ruff/format/mypy/lock/build/Compose/diff đạt. E2E operation lifecycle stale/replay/partial/restart/concurrency chưa có.
- Task 6 Telegram link/revoke/rebind có service, API + HTML, one-time digest ledger/callback nonce, partial unique index active, RLS/grants, strict private webhook và membership recheck; Compose E2E kiểm mã hết hạn/replay, callback đúng sender, active duplicate, revoke/rebind, lock/revoke. Đầu ra kiểm thử mới nhất được ghi trong implementation memory.
- Còn race E2E enable-vs-disable/pause; Task 5 stale/replay/partial/restart/concurrency operation E2E; Task 7 Playwright/accessibility/security và review độc lập/full regression. Sender Telegram hiện vẫn là adapter ghi bộ nhớ; deployment cần adapter gửi Bot API để người dùng nhận nút xác nhận thật. Không commit/push.


## Cập nhật tiến độ (2026-11-01)

MB07b.0 đã hoàn tất; dependency Jinja2 3.1.6, Playwright 1.64.1 và pytest-playwright 0.10.0 được khóa, Chromium đã cài. Task 1–4 có code và targeted checks theo progress phía trên. Task 5 đang triển khai; task 6–7 còn mở. Xem [implementation memory](../../implementation/implementation-memory.md) và [kế hoạch thực thi](./2026-11-01-mb07b-admin-web-operations.md).

### MB07b.1 — tiến độ read-only (2026-11-01)

Đã có `/admin`, `/admin/bots`, `/admin/bots/{id}`, `/admin/reports` dùng `BotConfigAdminService`, `BotOperationsService`, `BotReportingService`. Báo cáo workspace cần `scope=workspace` tường minh; trang và responses đặt `Cache-Control: no-store`. Unit/API target đạt 99/99. Form create/config/control đã được thêm ở task 3; browser E2E và accessibility checklist còn mở.

### Cập nhật tích hợp Task 7 (2026-11-01)

- Đã thêm `/admin/login` GET/POST với form workspace/email/password/TOTP; POST dùng cùng `AuthService.login` và `LoginRateLimiter` như API, đặt cookie phiên theo chính sách hiện có rồi PRG về `/admin`.
- Trước khi có session, GET phát token ngẫu nhiên gắn với cookie host-only `HttpOnly`, `SameSite=Strict`, `Path=/`, hết hạn sau 5 phút; production dùng prefix `__Host-` và `Secure`. POST so sánh token cookie/form constant-time; thiếu/sai token trả 403 trước khi gọi auth service. Lỗi credential render thông báo chung và không echo trường nhạy cảm.
- Unit login cuối 4/4; regression auth/web trước vòng review 20/20; browser Compose kiểm bootstrap owner/MFA, login success với mật khẩu/TOTP thật, credential sai/error chung, headers/cache, field labels và không phản chiếu mật khẩu. Review độc lập phát hiện lỗi auth service 503 bị ánh xạ thành 401 và sau đó callback/revoke lock inversion sinh PostgreSQL `40P01`; cả hai đã có regression, sửa và re-review đạt. Manual accessibility checklist vẫn mở.
- Final verification sau các fix/review: full unit **1641 passed / 9.14s**; Compose E2E tích hợp **9 passed / 291.60s**; Ruff, format 329 files, mypy 154 modules, `uv lock --check`, wheel, Compose config, Alembic single head `e2c4a6f8b1d3` và `git diff --check` đạt.
- Xóa tag `crypidian-app:e2e-*` sau mỗi lượt Compose khi xác nhận `docker ps -a` không có container tham chiếu; cuối cùng 0 container/volume, không còn tag E2E, chỉ giữ PostgreSQL + Garage **743.8 MB** và build cache **903.4 MB**.
