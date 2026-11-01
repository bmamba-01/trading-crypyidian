# Kế hoạch MB-07b — Admin Web, enable, flatten-all và Telegram binding

> **Cho agent:** Dùng skill `superpowers:executing-plans` nếu bàn giao thực thi. Thực thi từng task theo thứ tự, viết test trước implementation và không bỏ qua RED/GREEN.

**Mục tiêu:** Hoàn thiện lát cắt quản trị Admin Web server-rendered và các thao tác enable, flatten-all, Telegram binding trong phạm vi PAPER.

**Kiến trúc:** FastAPI + Jinja2, HTML form/server-rendered, router mỏng gọi application services; cookie session, CSRF, RBAC, MFA và audit tiếp tục dựa trên auth hiện có. Operation rủi ro cao có ledger bền vững, idempotency và transaction DB. Browser E2E dùng Playwright trên Compose.

**Stack:** Python 3.14, FastAPI hiện tại, Jinja2 3.1.6, Playwright 1.64.1, pytest-playwright 0.10.0, PostgreSQL Compose.

**Thiết kế:** `docs/superpowers/specs/2026-11-01-mb07b-admin-web-operations-design.md` (được duyệt có điều kiện sau khi MB-05b/06/07a đạt).

## Ràng buộc chung

- Áp dụng `.agents/skills/futures-bot-implementation/SKILL.md`; trước code đọc implementation-memory và tài liệu module liên quan. Mỗi vòng implement cập nhật rồi đọc lại memory.
- Giải thích, comment, docstring tiếng Việt; identifier/API tiếng Anh. Thuật ngữ EN→VI mới phải thêm glossary kèm nguồn.
- Không gọi Binance private API, không mở LIVE, không nhận/adopt venue activity lạ. Không gửi lệnh flatten qua Telegram.
- Handler/template không ghi DB trực tiếp; mọi mutation có CSRF/RBAC/audit. Session/role/scope xác minh phía server. Không lộ bot ngoài scope.
- Không log password, credential, MFA seed, Telegram token, one-time code hoặc session cookie.
- Mỗi task chạy targeted unit/API test liên quan và kiểm lint/format; test chưa chạy/skip/flaky chưa điều tra không tính đạt. Trước commit (nếu có) phải chạy unit + E2E liên quan; chưa commit cho tới full regression/review.
- Tài liệu API/dependency kiểm từ nguồn chính thức; lưu URL và ngày 2026-11-01 trong memory.

## Review Focus

- Transaction/lock ordering so với MB-04/05, bất biến L1/fencing và khả năng pause/kill đồng thời.
- MFA/CSRF/session renewal, authz theo workspace/bot, concealment, audit deny/no-op.
- Hash/expiry/consume-once/idempotency/restart recovery và partial operation state.
- RLS/migration rollback, output escaping/CSP, accessibility bàn phím/focus/error summary.
- Browser E2E không tắt assertion, không skip; Compose test độc lập và cleanup chắc chắn.

## Task 1 — Khóa inventory, dependency và test/browser harness

**Files:** `pyproject.toml`, `uv.lock`, `tests/conftest.py` hoặc fixture mới `tests/e2e/browser_fixtures.py`, `tests/e2e/test_admin_web_smoke.py`, implementation memory.

1. Tạo test smoke RED cho route `/admin` trả login redirect/401 khi thiếu session, route có session trả HTML no-store; dùng TestClient/ASGI fixture hiện có. Kiểm tra title, semantic navigation và không cache.
2. Thêm Jinja2 runtime pin `jinja2==3.1.6`; thêm Playwright + pytest-playwright ở dev pins `playwright==1.64.1`, `pytest-playwright==0.10.0`. Chạy `uv lock --check`/`uv sync --frozen`.
3. Cài browser Chromium Playwright tương ứng; thêm cấu hình fixture/headless, base URL và artifact path, không tải browser trong test runtime.
4. Xây `Jinja2Templates`/template directory và dependency `get_templates`; mount static assets tối thiểu nếu dùng. Không thêm JS framework.
5. GREEN: test xác thực/HTML smoke pass; Ruff check/format, mypy app wiring và `uv lock --check` pass.
6. Ghi package version, nguồn PyPI, docs FastAPI/Jinja/Playwright + checked date trong implementation memory.

## Task 2 — Layout và dashboard/list/detail read-only

**Files:** `src/trading_bot/admin/web_router.py`, `src/trading_bot/admin/web_views.py`, `src/trading_bot/templates/admin/{base,dashboard,bots,bot_detail}.html`, `src/trading_bot/app.py`, `tests/unit/test_admin_web_views.py`, `tests/unit/test_admin_web_router.py`.

1. Test RED cho unauthenticated redirect/login, authenticated HTML/no-store, bot list scoping và concealment 404, template escaping, workspace dashboard counts/status.
2. Xác định view model typed riêng, không serialize ORM. Đọc dữ liệu qua query/service hiện có: `BotConfigAdminService`, `BotOperationsService`, `BotReportingService`; nếu thiếu read query, tạo application query/repository có RLS, không query từ template/router.
3. Tạo `/admin`, `/admin/bots`, `/admin/bots/{bot_id}` và trang reports bot/workspace. Bot scope mặc định; workspace aggregate yêu cầu quyền rõ ràng.
4. Đăng ký router/lifecycle template trong `app.py`; CSP-safe inline assets (external/static only), autoescape mặc định, no-store với dữ liệu cá nhân.
5. GREEN: unit/router tests cho role matrix owner/admin/risk_admin/strategy_admin/operator/grant/analyst và concealment; Ruff/format/mypy.

## Task 3 — Web mutation cho bot/config/control hiện hữu

**Files:** `src/trading_bot/admin/web_router.py`, templates `bot_create.html`, `bot_config.html`, `bot_detail.html`, router/service reuse tests.

1. RED test POST thiếu CSRF → 403; bot ngoài quyền → cùng 404; role không đủ → 403; field/redirect không chứa secret.
2. Tạo HTML form dùng PRG (Post/Redirect/Get) cho create/bind account và draft/validate/publish strategy overlay; gọi `BotConfigAdminService`, không duplicate business rules.
3. Tạo pause/resume/disable form gọi `BotOperationsService`; giữ expected revision và reason hiện hành, hiện lỗi conflict thân thiện; workspace kill/release chỉ theo permission/service policy.
4. Test audit/service invocation, invalid form/error summary, CSRF rotation/session expiry, escaping user input. GREEN + lint/typecheck.

## Task 4 — Enable bot đã disable

**Files:** `src/trading_bot/application/bot_enable.py`, repository/model files theo schema hiện hữu, admin API/router/schema, web template, Alembic migration chỉ nếu inventory chứng minh cần, unit/API tests.

1. Viết RED cho permission owner, recent MFA, reason, bot/account mirror, active account, workspace not killed, resolved incidents, runtime reconciled, config PUBLISHED; deny/no-op audit; state unchanged trên conflict.
2. Chốt transaction/lock order bằng đọc `BotOperationsService`, `BotRuntimeControlService`, `BotRuntimeRegistry` và runtime lifecycle repositories. Ghi contract trước khi implement; không tự resume pause/kill hoặc clear incident.
3. Thêm application command `BotEnableService.enable_bot(bot_id, reason)`; thực hiện precondition và transition trong một transaction lock phù hợp; CAS revision nếu schema hỗ trợ.
4. Thêm endpoint CSRF/API tương thích router hiện tại và HTML form owner+MFA; map lỗi ổn định.
5. RED concurrency tests cho enable đối đầu disable/pause; GREEN unit/API/E2E trên PostgreSQL thật; audit kiểm bằng query.

## Task 5 — Flatten-all preview và operation ledger

**Ruling 2026-11-01:** admin-web không được dùng runtime gateway trong RAM vì chạy process/DB role riêng. Confirm tạo operation ledger bền vững; `BotRuntimeSupervisor` thực thi từng bot dưới lease/fence. Mark PAPER phải là `MarketBar` đóng, đúng MarkPolicy workspace và còn mới theo max-mark-age; thiếu/stale mark chặn item và giữ bot PAUSED. Không resume tự động khi operation partial/blocked.

**Files:** `src/trading_bot/application/flatten_all.py`, `src/trading_bot/domain/execution/flatten_all.py`, repositories/models, migration, API schemas/router, templates, tests.

1. RED tests cho preview read-only (bot/position/open-order/reservation snapshot, mark/time), hash + expiry; stale revision/list/hash bị từ chối; owner+MFA+reason; invalid/reused idempotency; partial outcome; retry cùng key trả operation cũ.
2. Thêm model ledger operation và per-item result theo RLS workspace, migration + policy/constraint + migration tests. Hash canonical payload và snapshot IDs/revisions; key scoped workspace/user.
3. Thêm `FlattenAllService.create_preview(principal, reason)` và `confirm(principal, preview_id, preview_hash, idempotency_key, reason)`; re-read snapshot trong transaction, buộc nội dung confirm đúng preview và chỉ PAPER positions đã biết.
4. Từng mục đóng/hủy qua coordinator/L1/fencing/risk/protective monitoring hiện hữu. Lỗi từng item thành BLOCKED/PARTIAL, giữ entry blocked tới reconcile; restart worker tiếp tục không gửi trùng.
5. Thêm API preview/confirm/status có CSRF và HTML review/confirm/result. Không route Telegram flatten.
6. GREEN unit/API, Postgres E2E cho stale/replay/partial/restart/concurrency; kiểm DB ledger và audit.

## Task 6 — Telegram link/revoke/rebind

**Files:** `src/trading_bot/application/telegram_binding.py`, Telegram models/repositories/entities, Alembic migration, `telegram/webhook.py`, `telegram/bot_commands.py`, admin API/web templates, tests.

1. RED tests: code entropy/hash-at-rest/TTL ≤10m/private-only/consume-once, sai workspace/expired/replay, callback nonce expiry/replay, revoke, rebind, active duplicate, locked membership immediate denial; raw code không có trong log/audit.
2. Inventory unique constraint hiện tại `uq_telegram_binding_user` trên `(workspace_id, telegram_user_id)` cản rebind sau revoke. Migration thay bằng partial unique active index/constraint phù hợp trạng thái schema; giữ lịch sử revoke và RLS. Test upgrade/downgrade.
3. Application service phát mã plaintext một lần cho UI và chỉ lưu digest, bind chờ callback từ private `/start <code>` sau khi người dùng xác nhận từ Admin Web, consume nguyên tử và audit metadata không bí mật. Enforce Telegram deep-link length/format theo docs.
4. Tạo trang link/revoke với CSRF/MFA hiện có; webhook không bind từ group, kiểm membership/user active mỗi invocation; nonce gắn identity/workspace/deployment.
5. GREEN service/repository/webhook/API/Compose E2E cho rebind + lock/revoke; Ruff/typecheck.

## Task 7 — Accessibility/security pass và browser E2E

**Files:** templates/static CSS, `tests/e2e/test_admin_web_workflows.py`, `tests/e2e/browser_fixtures.py`, checklist trong `docs/implementation/`.

1. Playwright RED cho login, dashboard/list/detail/config flow, pause/resume, enable deny/allow, flatten preview stale/confirm/retry, Telegram link/revoke/rebind; dùng PostgreSQL Compose và session thật, Telegram outbound fake.
2. Thêm labels, landmarks, heading hierarchy, error summary/focus, keyboard navigation, visible focus, accessible status; automated axe-core only if package/toolchain approved and pinned, otherwise run Playwright accessibility snapshot/assertions supported by installed version plus explicit manual checklist.
3. Assert no secret in HTML, logs/audit, network responses; assert CSP/CSRF/cache headers; output escaping; test unauthorized bot concealment.
4. GREEN targeted E2E sequentially and diagnose flakes (no skips); run MB-05b/06/07a regression E2E affected and full related unit suite; Ruff, format, mypy, migration checks, `git diff --check`.
5. Request independent code review after all checks; resolve findings. Update project-status, MB07b WBS, glossary if new terminology, implementation-memory; re-read memory and report exact verification output. Commit only if user later asks or repository workflow requires; before any commit run related unit + E2E.

## Tiến độ

- Task 4 implementation: API + HTML enable form, owner/MFA/reason, snapshot preconditions, DB trigger marker/migration, deny/no-op audit. Compose E2E race enable-vs-pause và disable-vs-pause xác nhận request cạnh tranh chờ advisory L1; final suite đạt, xem implementation-memory.

- Task 1: template runtime, `/admin` smoke, dependencies/lock/browser Chromium đã xong ở working tree; unit 1/1, Ruff/format/mypy/lock/wheel đều đạt.

- Task 2: dashboard/list/detail/report read views đã nối service, no-store, autoescape; 99 unit/API regression tests đạt.

- Task 3: create PAPER, config draft/validate/publish, bot pause/resume/disable và workspace kill/release HTML forms đã gọi service hiện hữu, CSRF-guarded; 105 unit/API regression tests đạt. Login HTML được bổ sung ở vòng 2026-11-01 sau; lỗi xác thực giữ thông báo chung, không phản chiếu credential.

- Task 5: migration/ledger với RLS, snapshot mark provenance, service preview/confirm/idempotency/audit, API + HTML review/confirm/result, worker dưới workspace lock/fence, PAPER reduce-only close/checkpoint đã có. Final Compose E2E bao gồm stale-preview denial, confirm replay/key mismatch, worker stop/start recovery, hai worker gọi đồng thời qua UoW riêng, lease/fence, mỗi item xử lý một lần và đúng một exit fill; toàn nhóm **9 passed / 281.95s**. Worker mark retry unit đã sửa: lần đầu buộc match preview; sau partial chỉ tiếp tục bằng mark đóng mới hơn và còn tươi. Gateway PAPER chuẩn luôn khớp đủ quantity checkpoint nên không tạo partial tự nhiên; partial E2E vẫn không thể chứng minh nếu không có execution adapter hỗ trợ partial. Không resume bot tự động.

- Task 6: **implementation và scoped acceptance hoàn tất** — service/API/HTML, digest ledger, callback nonce consume-once, revoke/rebind, active membership/account recheck. Independent review phát hiện quyền UPDATE dư và race lock/revoke; migration đã giới hạn UPDATE theo cột, callback tạo/xác nhận đều khóa membership trước request/challenge, đồng bộ thứ tự với revoke. Compose E2E tái hiện deadlock PostgreSQL `40P01` trước fix và sau đó xác nhận hai race callback/revoke đều tuần tự hóa thành công. Final Compose E2E **9 passed / 291.60s**, DB-role matrix và migration test đạt. Outbound Bot API thật chưa có adapter.
- Task 7: browser E2E Compose đã đạt qua bootstrap owner/MFA, đăng nhập thành công bằng credential/TOTP thật và nhánh credential sai/error chung, dashboard/keyboard/security headers, bot list/detail, pause/disable CSRF forms, flatten preview/confirm/status, config draft/validate/publish, enable bị từ chối trước khi publish và thành công sau đó, Telegram revoke/link/CSRF. Review độc lập tìm lỗi 503 auth service bị đổi thành 401 credential sai; đã thêm regression, sửa và re-review xác nhận. Task 7 vẫn mở vì chưa hoàn tất kiểm tra accessibility thủ công toàn checklist. Full unit cuối **1641 passed**, Compose E2E **9 passed / 291.60s**, Ruff/format/mypy/lock/build/Compose/Alembic/diff đạt. Partial fill chưa thể phát sinh bằng PAPER gateway chuẩn; Telegram sender vẫn là recording adapter, không gửi Bot API thật. Chưa commit.

### Task 4 ruling

Runtime roster loại bot có account DISABLED, vì vậy `applied_workspace_control_revision` có thể cũ sau workspace kill/release và không thể tự cập nhật khi bot còn disabled. Enable vẫn khóa L1, kiểm workspace hiện không KILLED, kiểm readiness/activity đã xác nhận, bot-control revision hiện hành, incident và config; không kiểm equality với workspace-applied revision. Giữ bot pause/kill state nguyên trạng. Chi phí nếu sai: DB precondition không tự chứng minh runtime đã consume mọi thay đổi workspace, nên safety vẫn phụ thuộc L1 + runtime đọc control hiện hành trước entry (đã là contract MB-05).
