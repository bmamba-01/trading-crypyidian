# MB-07a WBS — Admin API điều khiển bot và lệnh Telegram (PAPER)

**Trạng thái:** đã triển khai trên nhánh worktree (2026-10-27): unit + quality gates đạt; **E2E Compose `tests/e2e/test_bot_control_api_telegram.py` còn chờ** (Docker daemon treo, `docker info` quá 30 s); chờ review độc lập và lượt E2E tuần tự của controller.\
**Phạm vi gốc:** Task 8 / MB-07 của [kế hoạch Multi-Bot](./2026-10-24-multibot-implementation-plan.md), phần điều khiển bot qua Admin API + Telegram (controller chia MB-07 thành MB-07a và MB-07b ngày 2026-10-27).\
**Liên kết:** [MB-05 WBS](./2026-10-26-mb05-runtime-registry-wbs.md) (ruling 20, 25–30), [ADR addendum runtime control](../../architecture/decisions/2026-10-26-runtime-control-addendum.md), [nghiên cứu bảo mật](../../research/security-admin-bot-2026.md) (SEC-19/20/22/23/24/35/36/37/38), [bộ dữ liệu QA](../../testing/qa-test-data.md), [glossary EN→VI](../../research/glossary-vi.md).

## Mục tiêu

Một control plane dùng chung cho mọi bot: Admin API và Telegram gọi **cùng** application service (`BotRuntimeControlService` của MB-05 và service vận hành bot mới), không có đường ghi DB riêng cho từng kênh. Mọi thao tác thay đổi trạng thái được kiểm quyền ở backend, có audit (kể cả noop và bị từ chối) và lỗi không làm lộ bot của workspace/scope khác.

## Chia phạm vi

### (a) MB-07a — trong vòng này

1. **Admin API điều khiển bot** (`admin/bot_control_router.py`):
   - `GET /api/v1/workspace/runtime`: trạng thái kill workspace + danh sách trạng thái runtime của các bot principal được đọc (`bot:read`).
   - `GET /api/v1/bots/{bot_id}/runtime`: trạng thái một bot (enablement, `entry_state`, revision control, readiness runtime, kill workspace, `entry_allowed`).
   - `POST /api/v1/bots/{bot_id}/runtime/pause` (`reason`), `POST /api/v1/bots/{bot_id}/runtime/resume` (`reason`, `expected_revision`).
   - `POST /api/v1/workspace/runtime/kill` (`reason`), `POST /api/v1/workspace/runtime/release` (`reason`, `expected_revision`).
   - `POST /api/v1/bots/{bot_id}/disable` (`reason`) với guard `BOT_NOT_FLAT` trong cùng transaction.
   - Mọi mutation cần phiên + CSRF; lỗi trả `{"detail": {"code", "message"}}` với mã ổn định.
2. **Lệnh Telegram** (`telegram/bot_commands.py`): `handle_bot_command(principal, command, bot_id | None) -> BotCommandResult` cho `/bots`, `/bot status <id>`, `/bot pause <id>`, `/bot resume <id>`; parser từ chối mọi lệnh khác (kể cả `/flatten`, `/kill`, `/resume workspace`, `/publish`, `/bot disable`).
3. **Webhook Telegram** (`telegram/webhook.py`): kiểm `X-Telegram-Bot-Api-Secret-Token` hằng thời gian trước mọi truy cập DB, giới hạn body, dedup `update_id` bền vững, chỉ nhận `message` trong private chat (`chat.id == from.id`), resolve binding Telegram → membership ACTIVE → principal, throttle theo user, audit mỗi update đã xử lý, trả lời qua **port gửi tin giả** (không gọi mạng Telegram).
4. **Timeout phiên DB admin-web** (việc mang theo MB-07): `lock_timeout`, `idle_in_transaction_session_timeout` (và `statement_timeout`) đặt qua connection options khi `process_role = admin-web`, vì đường disable/control giữ L1 trong admin-web.
5. **Không lộ credential/token** (Step 5): test phản hồi API, log và audit không chứa cookie phiên, mã CSRF, secret webhook.
6. **Migration duy nhất** `f7b9d1e3a5c8` (sau `e1f3a5c7b9d2`): grant cột `UPDATE (status)` trên `trading_account` cho `crypidian_admin_web` (ruling 29, SEC R10) và bảng `telegram_update_receipt` (dedup `update_id`, SEC-36).

### (b) MB-07b — để sau

- Trang Admin Web render phía server + template (cần `jinja2` → bị chặn do `uv` cục bộ không cập nhật được lock), Playwright E2E giao diện, kiểm accessibility.
- Flatten-all (SEC-23) có preview WYSIWYS, step-up theo hành động, Idempotency-Key và cấm qua Telegram (Telegram đã từ chối `/flatten` ở MB-07a).
- Trang báo cáo bot (MB-06 cung cấp service).
- Liên kết Telegram bằng mã một lần sau step-up (deep link, TTL ≤10 phút), thu hồi binding khi khóa member, xác nhận 2 bước bằng callback nonce (SEC-37/38), sửa unique `telegram_binding` (xem ruling 37).
- Enable lại bot đã disable (tăng rủi ro → cần step-up theo hành động).

## Rulings MB-07a

31. **Một control plane, hai kênh.** Router HTTP và handler Telegram chỉ gọi `BotOperationsService` (`application/bot_operations.py`); service này ủy quyền pause/resume/kill/release cho `BotRuntimeControlService` của MB-05 (giữ nguyên quyền ruling 20/30, MFA, lý do, `expected_revision`, audit và L1). Không route/handler nào mở session DB để ghi trực tiếp.
32. **Quyền và lỗi không rò rỉ.** Đọc trạng thái bot cần `bot:read` qua `BotGrantRepository.require`; bot không thấy dưới RLS, khác workspace hoặc thiếu grant đều trả `BOT_NOT_FOUND` (404) như nhau (SEC-19, OWASP API1:2023). Kill/release: `CONTROL_PERMISSION_DENIED` (403) khi không có `workspace:kill`. Ánh xạ HTTP: `BOT_NOT_FOUND` 404; `CONTROL_PERMISSION_DENIED`, `RECENT_MFA_REQUIRED` 403; `CONTROL_REASON_REQUIRED` 422; `CONTROL_REVISION_CONFLICT`, `CONTROL_REVISION_REQUIRED`, `BOT_NOT_FLAT` 409; `RUNTIME_CONTROL_UNAVAILABLE` 503. Body chỉ có mã + thông điệp tiếng Việt; `BOT_NOT_FLAT` kèm số đếm của **chính** bot đó (ruling 29).
33. **Idempotency control theo trạng thái đích (ruling 21), không thêm ledger `Idempotency-Key` HTTP.** Pause/kill lặp là noop có audit `.noop`; resume/release bắt buộc `expected_revision` nên gửi lại sau khi đã đổi trả `CONTROL_REVISION_CONFLICT` thay vì làm lại. Ledger Idempotency-Key chỉ cần cho flatten-all (MB-07b, SEC-23/24).
34. **Disable bot = disable account PAPER (MB-01 ruling 13).** Chỉ `owner` + MFA gần đây + lý do (cùng mức với tạo bot của MB-02). Thứ tự trong **một** transaction: kiểm role/MFA/lý do (không chạm DB) → bot thấy được → `BotLifecycleGuard.ensure_flat_for_disable` (L1 rồi đếm) → khóa `trading_account` `FOR UPDATE` → `UPDATE status = 'DISABLED'` (trigger MB-01 mirror sang `bot_instance`) → audit `bot.disabled`. Đã DISABLED → noop `bot.disabled.noop`. Không có chế độ disable chỉ-thoát; còn vị thế/lệnh/reservation → `BOT_NOT_FLAT` + audit `bot.lifecycle.disable_denied`. Enable lại để MB-07b.
35. **Telegram không có MFA.** Principal Telegram có `mfa_verified_at` = thời điểm nhỏ nhất (không bao giờ "gần đây"), nên `/bot resume` đi qua đúng service và bị từ chối `RECENT_MFA_REQUIRED` với hướng dẫn dùng Admin Web (SEC-22: resume là hành động tăng rủi ro cần step-up). Telegram không có kill/release workspace, flatten, publish, disable (SEC-23, SEC-38).
36. **Chống mơ hồ (SEC-38).** Lệnh đổi trạng thái (`pause`/`resume`) luôn cần ID bot đầy đủ (UUID); thiếu ID → `BOT_TARGET_AMBIGUOUS` (≥2 bot thấy được) hoặc `BOT_TARGET_REQUIRED` (≤1), kèm danh sách ID bot principal được đọc. `status` thiếu ID chỉ tự chọn khi principal thấy đúng một bot. ID sai định dạng hoặc không thấy được → `BOT_NOT_FOUND`, **không bao giờ** fallback sang bot khác.
37. **Binding Telegram ↔ membership không cần migration ở MB-07a.** Webhook gắn với **một** workspace deployment (`TRADING_BOT_TELEGRAM_WORKSPACE_ID`, như `runtime_workspace_id` của runtime) và một secret riêng; lookup binding chỉ trong workspace đó, nên user bind ở W2 không điều khiển W1 dù unique hiện tại chỉ theo `(workspace_id, telegram_user_id)`. Ràng buộc user + chat: chỉ nhận private chat với `chat.id == from.id` nên không cần cột `chat_id`. Binding hợp lệ: `verified_at` có giá trị, `revoked_at` rỗng, membership ACTIVE, user ACTIVE, có role. Khoảng trống còn lại của SEC-37/38 (unique toàn hệ thống hoặc chọn workspace tường minh khi nhiều workspace dùng chung một bot Telegram; không re-bind được sau revoke vì unique tính cả hàng đã revoke; mã liên kết một lần) để MB-07b cùng luồng tạo binding.
38. **Dedup `update_id` bền vững, at-most-once.** Sau khi qua secret và parse, webhook `INSERT … ON CONFLICT DO NOTHING` vào `telegram_update_receipt (workspace_id, update_id)` trong transaction riêng **trước** khi xử lý; trùng → 200 `duplicate`, không xử lý lại. Update đã claim mà xử lý lỗi không được thử lại (lệnh pause idempotent theo trạng thái; resume cần MFA nên không chạy qua Telegram). `edited_message` và loại update khác được claim rồi bỏ qua.
39. **Thứ tự kiểm webhook (SEC-36/37).** (1) webhook chưa cấu hình → 404, không chạm DB; (2) thiếu/sai secret (so sánh `hmac.compare_digest` trên bytes) → 401, không chạm DB; (3) body > 64 KiB → 413; (4) JSON/`update_id` sai → 400; (5) claim `update_id`; (6) chỉ `message` có `text` bắt đầu `/`; từ chối group/channel, `chat.id != from.id`, `from.is_bot`, `sender_chat`, `via_bot`, `forward_origin`, `guest_bot_caller_user` → audit `telegram.update.rejected`; (7) binding → không có thì `TELEGRAM_NOT_BOUND` (cùng mã cho user lạ, user bị khóa, binding bị thu hồi); (8) throttle 20 lệnh/60 s/user; (9) handler; (10) audit `telegram.command.accepted|rejected` với actor membership, mã lệnh, bot đích, `request_id` = UUIDv5 của update. Phản hồi HTTP cho Telegram chỉ có `{"ok": true, "status": …}`; nội dung trả lời đi qua port `TelegramMessageSender` (bản giả ghi bộ nhớ; không có adapter mạng ở MB-07a).
40. **Timeout admin-web.** `lock_timeout = 5 s`, `idle_in_transaction_session_timeout = 15 s`, `statement_timeout = 30 s` (theo khuyến nghị PostgreSQL: `lock_timeout` < `statement_timeout`; đặt theo phiên qua `options`, không đặt trong `postgresql.conf`). Admin-web treo giữa transaction đang giữ L1 bị cắt sau 15 s nên runtime không bị chặn lâu; chờ L1 sau entry dài quá 5 s trả `55P03` → `RUNTIME_CONTROL_UNAVAILABLE` (503) thay vì treo request.
41. **Không lộ bí mật (SEC-35).** Secret webhook chỉ đọc từ file (`TRADING_BOT_TELEGRAM_WEBHOOK_SECRET_FILE`), kiểm 32–256 ký tự `[A-Za-z0-9_-]`, không log, không đưa vào response/audit. Audit chỉ chứa mã lý do/ID, không chứa text tin nhắn Telegram, cookie hay CSRF.

### Hiệu chỉnh sau review (fix round 1, 2026-10-27)

42. **Lỗi DB tạm thời trên control plane (Important #1).** Với `lock_timeout = 5 s`, `pg_advisory_xact_lock` (L1) ném `DBAPIError` 55P03 khi runtime giữ L1 lâu; control service MB-05 chỉ dịch lỗi domain. `PrincipalBotOperations` dịch lỗi tạm thời (`TRANSIENT_SQLSTATES` = 55P03, 57014) thành `RUNTIME_CONTROL_UNAVAILABLE` (HTTP 503) cho mọi mutation (kèm audit `bot.runtime.control_denied`/`bot.lifecycle.disable_denied` ở transaction riêng; lỗi ghi audit không che mã lỗi) và cho đường đọc (không audit); ném `from None` để không giữ câu SQL/params. Lỗi DB không tạm thời vẫn nổi lên (không che bug). Webhook bắt **mọi** lỗi sau khi claim update: audit `telegram.command.rejected` `RUNTIME_CONTROL_UNAVAILABLE`, trả lời người dùng "thử lại", HTTP 200 `unavailable`; log chỉ tên lớp lỗi.
43. **Trigger DB cho chuyển trạng thái account (minor 1).** `trading_account_status_guard` (BEFORE UPDATE OF status, SECURITY INVOKER, `search_path` pin) chỉ cho `crypidian_admin_web` ENABLED → DISABLED; không đếm lại vị thế trong trigger vì RLS của invoker có thể làm đếm ra 0 (an toàn giả) — guard phẳng vẫn ở application cùng transaction.
44. **Throttle trước DB và gộp từ chối chưa liên kết (minor 2).** Throttle theo Telegram sender ID (20 update/60 s) chạy trước claim/binding/audit; từ chối `TELEGRAM_NOT_BOUND` chỉ audit + trả lời tối đa 1 lần/10 phút/sender.
45. **Retention receipt (minor 3).** Claim xóa receipt quá 7 ngày trong cùng transaction (index `(workspace_id, created_at)`); policy `receipt_purge` FOR DELETE chỉ cho hàng quá 7 ngày, policy đọc/ghi tách riêng (`workspace_isolation` FOR SELECT, `receipt_insert` FOR INSERT). Lý do 7 ngày: Telegram chọn `update_id` ngẫu nhiên sau ≥1 tuần không có update, nên receipt cũ hơn không còn giá trị chống replay mà có thể chặn nhầm update mới.
47. **Throttle không bao giờ làm mất lệnh lặng lẽ (fix round 2).** Update bị throttle (chưa claim, HTTP 200 nên Telegram không giao lại) được trả lời "lệnh CHƯA được thực hiện" và audit `TELEGRAM_THROTTLED` gộp mẫu 1 lần/phút/sender, không lookup binding. `/bot pause` có ngân sách riêng (10/60 s/sender): pause là hành động giảm rủi ro và idempotent theo trạng thái (SEC-22), nên lệnh khác không được làm cạn ngân sách của nó; vẫn có trần để chống flood.
48. **Bộ đếm throttle có giới hạn bộ nhớ (fix round 2).** Khóa sắp theo lần truy cập; mỗi lần gọi dọn khóa hết cửa sổ ở đầu hàng đợi, vượt `max_keys` (10 000) thì loại LRU (chỉ reset bộ đếm của khóa đó). Áp cho throttle chung, pause, sampler chưa liên kết và sampler thông báo throttle.
49. **Catch-all phân loại lỗi (fix round 2).** Log ERROR kèm SQLSTATE và `file:dòng` của frame ném lỗi (không stack, không giá trị). Lỗi tạm thời → `RUNTIME_CONTROL_UNAVAILABLE` (`unavailable`, bảo thử lại); lỗi khác → `TELEGRAM_COMMAND_FAILED` (`failed`). Nếu lệnh đã chạy (ví dụ pause đã áp) và chỉ audit `accepted` hoặc gửi trả lời lỗi thì chỉ bù phần thiếu với kết quả thật, không audit `rejected` và không bảo thử lại. Lỗi khi lookup binding chỉ trả lời qua sampler chưa liên kết.
46. **Đầu vào webhook (minor 4–6).** `TelegramWebhookService` từ chối secret rỗng/không hợp lệ ngay khi khởi tạo; body đọc theo luồng có trần 64 KiB (chunked không có Content-Length); `update_id` phải trong `1..2^63-1` (BIGINT); 503 "service chưa sẵn sàng" của Admin API dùng body `{"code": "SERVICE_UNAVAILABLE", "message"}`.

## Ánh xạ yêu cầu bảo mật

| SEC | Yêu cầu | Hiện thực MB-07a | Test |
| --- | --- | --- | --- |
| SEC-19 | BOLA: không có quyền = không tồn tại | Ruling 32; `BOT_NOT_FOUND` cho bot ngoài scope/khác workspace/thiếu grant | Unit API ma trận; Telegram ID lạ; E2E operator → bot B |
| SEC-20 | Step-up | Resume/release cần MFA gần đây (ruling 30, 35); disable owner + MFA (ruling 34); step-up theo hành động (challenge gắn payload) để MB-07b | Unit API MFA cũ → 403 |
| SEC-22 | Kill/pause quyền riêng, giảm rủi ro ma sát thấp, resume step-up | Ruling 31, 35; kill/release chỉ owner/risk_admin | Unit API ma trận; E2E kill/release |
| SEC-23 | Flatten-all riêng, cấm qua Telegram | Telegram từ chối `/flatten`; flatten Admin Web → MB-07b | Unit parser |
| SEC-24 | Idempotency mutation | Ruling 33 | Unit noop + revision conflict |
| SEC-28 | CSRF | `require_csrf` trên mọi mutation | Unit CSRF thiếu → 403, service không được gọi |
| SEC-32/33 | Audit mọi thao tác, kể cả từ chối | Control service MB-05 + `bot.disabled*`, `bot.lifecycle.disable_denied`, `telegram.*` | Unit + E2E đọc `audit_event` |
| SEC-35 | Không lộ token/secret | Ruling 41 | Unit + E2E quét payload/log/audit |
| SEC-36 | Webhook secret, dedup, giới hạn body | Ruling 38, 39 | Unit SECT-38/39; E2E replay |
| SEC-37 | Binding, private chat, chặn update giả mạo | Ruling 37, 39 | Unit SECT-40; E2E user W2/unbound |
| SEC-38 | Chống mơ hồ, không resume/flatten qua Telegram | Ruling 35, 36 | Unit SECT-42/43 |
| SEC-47 | Control plane không giữ khóa lâu | Ruling 40 | Unit engine options |

## Work breakdown và nghiệm thu

### MB07a.1 — Service vận hành bot dùng chung
- [x] RED `tests/unit/test_bot_operations_service.py`: trạng thái bot cần `bot:read`, bot ngoài scope/thiếu grant → `BOT_NOT_FOUND`; danh sách lọc theo grant; disable: không phải owner → `CONTROL_PERMISSION_DENIED`, MFA cũ → `RECENT_MFA_REQUIRED`, thiếu lý do → `CONTROL_REASON_REQUIRED`, còn vị thế → `BOT_NOT_FLAT` + audit từ chối, phẳng → guard chạy trước khi ghi account + audit `bot.disabled`, đã disable → noop.
- [x] GREEN `application/bot_operations.py`, `adapters/database/repositories/bot_operations.py` (`BotOperationsRepository.disable_account`).

### MB07a.2 — Admin API
- [x] RED `tests/unit/test_bot_control_api.py`: CSRF cho mọi mutation; ma trận RBAC (owner, strategy_admin, risk_admin, operator có/không grant, analyst_reviewer, member bị khóa → 401, principal workspace khác → 404) cho pause/resume/kill/release/disable/status; MFA gần đây cho resume/release/disable; mã lỗi không rò rỉ; audit; không có token trong payload.
- [x] GREEN `admin/bot_control_router.py` (schema `ControlReasonRequest`/`ControlResumeRequest` đặt ngay trong router, không tách file), đăng ký router tối thiểu trong `app.py`.

### MB07a.3 — Lệnh Telegram và webhook
- [x] RED `tests/unit/test_telegram_bot_commands.py`: parser + handler (SECT-42/43, ruling 35/36).
- [x] RED `tests/unit/test_telegram_webhook.py`: SECT-38/39/40, binding, throttle, giới hạn body, không lộ secret.
- [x] GREEN `telegram/bot_commands.py`, `telegram/webhook.py`, `adapters/database/repositories/telegram.py`, `adapters/telegram/recording_sender.py`, `ports/telegram.py`, settings `telegram_workspace_id` + `telegram_webhook_secret_file`.

### MB07a.4 — Migration, timeout admin-web
- [x] RED `tests/unit/test_mb07a_migration.py` + cập nhật ghim head/model; `tests/unit/test_admin_web_db_timeouts.py` cho timeout admin-web.
- [x] GREEN migration `f7b9d1e3a5c8`, model `TelegramUpdateReceipt`, `admin_web_session_timeouts()`.

### MB07a.5 — Compose E2E và cổng chất lượng
- [ ] (viết xong, **chờ chạy** — Docker treo) `tests/e2e/test_bot_control_api_telegram.py` (probe qua stdin, tái dùng probe MB-05): Admin API thật (cookie phiên + CSRF, role `crypidian_admin_web`) pause A → runtime chặn entry, giữ stop của vị thế mở; ma trận kill/release; disable bị `BOT_NOT_FLAT`; Telegram handler thật pause/status, ID lạ, mơ hồ, replay, user W2; audit; không secret trong response/audit.
- [x] Full unit, ruff/format, mypy, `git diff --check`, một alembic head; cập nhật ghim E2E head (`test_multibot_config.py`) và ma trận grant (`probes/db_role_least_privilege.py`).

## Nguồn đã kiểm tra (2026-10-27)

- Telegram, [Bot API](https://core.telegram.org/bots/api) (Bot API 10.3, 2026-08-24): `setWebhook.secret_token` 1–256 ký tự `A-Za-z0-9_-`, header `X-Telegram-Bot-Api-Secret-Token`; `Update.update_id` tăng tuần tự, dùng để bỏ qua update lặp; `Message.sender_chat`, `forward_origin`, `via_bot`, `guest_bot_caller_user`, `User.is_bot`, `Chat.type` (`private`/`group`/`supergroup`/`channel`); `edited_message` là update riêng.
- Telegram, [Marvin's Marvellous Guide to All Things Webhook](https://core.telegram.org/bots/webhooks): webhook đến từ `149.154.160.0/20` và `91.108.4.0/22` (allowlist ở proxy, ngoài phạm vi app).
- OWASP, [Transaction Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html): step-up cho hành động tăng rủi ro; xác nhận gắn nội dung giao dịch (MB-07b).
- OWASP, [API1:2023 Broken Object Level Authorization](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/): kiểm quyền theo đối tượng ở mọi endpoint nhận ID.
- PostgreSQL 18, [Client Connection Defaults](https://www.postgresql.org/docs/current/runtime-config-client.html): `lock_timeout` áp cho từng lần chờ khóa (kể cả khóa tường minh), nên nhỏ hơn `statement_timeout`; `idle_in_transaction_session_timeout` cắt phiên rảnh trong transaction để giải phóng khóa; không nên đặt toàn cục trong `postgresql.conf`.
- Python, [`hmac.compare_digest`](https://docs.python.org/3/library/hmac.html): so sánh hằng thời gian thay cho `==` để giảm rủi ro timing attack.

## Thuật ngữ EN→VI (đề xuất bổ sung glossary)

- **Webhook secret token** → *mã bí mật webhook*: chuỗi do bot đặt khi `setWebhook`, Telegram gửi lại trong header mỗi request để server xác minh nguồn ([Telegram Bot API](https://core.telegram.org/bots/api#setwebhook)).
- **Update deduplication / replay protection** → *khử trùng lặp update / chống phát lại*: ghi bền vững `update_id` đã nhận và bỏ qua lần giao lặp ([Telegram Bot API – Update](https://core.telegram.org/bots/api#update)).
- **At-most-once processing** → *xử lý tối đa một lần*: claim trước khi xử lý; lỗi sau claim không thử lại.
- **Constant-time comparison** → *so sánh hằng thời gian*: thời gian so sánh không phụ thuộc vị trí ký tự khác nhau ([Python hmac](https://docs.python.org/3/library/hmac.html#hmac.compare_digest)).
- **Ambiguous target rejection** → *từ chối đích mơ hồ*: lệnh đổi trạng thái thiếu ID bot rõ ràng bị từ chối kèm danh sách ID, không đoán (SEC-38).
