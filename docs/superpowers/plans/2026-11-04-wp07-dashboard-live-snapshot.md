# WP-07 Dashboard live snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dashboard cho chọn từng bot đang bật và từng phiên PAPER để theo dõi snapshot runtime/ledger được cập nhật định kỳ.

**Architecture:** Thêm endpoint JSON admin có xác thực cho snapshot của đúng bot; endpoint tái dùng operation/report services để quyền và tenant scope giữ ở backend. Template hiển thị danh sách chọn bot và vùng chi tiết; JavaScript polling bot đang chọn mỗi 5 giây, dừng khi document ẩn hoặc trang rời.

**Tech Stack:** FastAPI, BotOperationsService, BotReportingService, Jinja2, JavaScript/CSS hiện có, pytest/Playwright.

**Spec:** [wp07 dashboard live snapshot design](../specs/2026-11-04-wp07-dashboard-live-snapshot-design.md)

## Global Constraints

- Chỉ hiển thị dữ liệu đã lưu trong runtime/PAPER ledger; không gọi Binance hoặc giả định vị thế sàn.
- Không nhận workspace ID từ browser; report qua read-only UoW, runtime qua `PrincipalBotOperations` tenant-bound. `bot_status` hiện chỉ SELECT nhưng UoW chưa khai báo `snapshot_read_only`; không sửa service ngoài task.
- “Phiên” hiện tại là giao dịch/vị thế PAPER đang mở; không thêm khái niệm lưu trữ phiên mới.
- Nội dung người vận hành bằng tiếng Việt; không thêm dependency, migration, ghi DB hoặc lệnh giao dịch.
- UI phải responsive; các thao tác chọn và thông báo cập nhật phải truy cập được bằng bàn phím/aria.

## Review Focus

1. Bot ngoài quyền hoặc quyền report bị thu hồi trong lúc dashboard mở → endpoint trả 404/403 an toàn, client ngừng polling bot đó.
2. Chuyển bot khi request trước còn chạy → response cũ không được ghi đè bot đang chọn.
3. Poll lỗi sau khi đã có dữ liệu → giữ snapshot cũ và hiện thời điểm/cảnh báo stale, không xóa panel.
4. Trang chuyển nền hoặc rời trang → polling được dừng; request lỗi không tạo unhandled rejection.
5. Bot chưa có trade/position hoặc nhiều quote asset → empty state rõ ràng, chỉ đọc đúng bot và không cộng gộp tài sản.

---

### Task 1: API snapshot theo bot

**Files:**
- Modify: `src/trading_bot/admin/web_router.py`
- Test: `tests/unit/test_admin_web_views.py` hoặc `tests/unit/test_admin_mobile_ui.py`

**Produces:** endpoint GET dưới `/admin/dashboard/bots/{bot_id}/snapshot`, xác thực `current_identity`, `Cache-Control: no-store`, runtime từ `_operations(...).bot_status(bot_id)`, báo cáo riêng từ `_reporting_service(...).get_bot_report(...)`, query `scope=bot` cho 30 ngày UTC gần nhất. JSON contract: root `observed_at`, `source="PAPER_LEDGER"`, `bot={id,name,status}`, `runtime={entry_state,control_revision,readiness,readiness_reason_code,workspace_kill_state,workspace_control_revision,entry_allowed}`, `window={start,end}`, `totals` theo quote asset với metric allowlist từ `METRICS`, `open_trades`, `recent_closed_trades` (10 mới nhất). Mỗi open trade chỉ có `{trade_id,symbol,side,quote_asset,opened_at}`; mỗi closed trade thêm `{closed_at,gross_pnl,fee_total,funding_total,slippage_total,total_cost,net_pnl}`. `observed_at` là thời điểm server đọc, không phải event heartbeat.

- [x] Viết test route cho snapshot đúng bot, timestamp và open trades.
- [x] Viết test fail-closed khi bot không được quyền đọc hoặc reporting service chưa sẵn sàng.
- [x] Chạy test mới, xác nhận RED do endpoint chưa có.
- [x] Thêm endpoint chỉ dùng service read-only có sẵn, map lỗi thành status an toàn; không serialize secret, owner ID, ORM hoặc raw exception.
- [x] Chạy focused unit và static checks. Endpoint qua app thật được kiểm trong Task 2 Playwright flow.

### Task 2: Dashboard chọn bot và polling accessible

**Files:**
- Modify: `src/trading_bot/templates/admin/dashboard.html`
- Create: `src/trading_bot/static/dashboard-live.js`
- Modify: `src/trading_bot/static/admin.css`
- E2E: `tests/e2e/test_admin_web_workflows.py`

**Consumes:** endpoint Task 1. <br>
**Produces:** danh sách bot ENABLED/ACTIVE dạng button, panel chi tiết một bot và open PAPER trade dạng selector phiên; polling 5 giây cho bot đang chọn. Chọn phiên mở hiển thị đúng các trường ledger có sẵn. `recent_closed_trades` render PnL theo asset và timestamp. Có thời điểm cập nhật, stale/error state; bỏ phản hồi cũ sau đổi bot; ngừng poll khi tab ẩn và dọn timer khi rời trang. Screenshot: `tests/e2e/evidence/wp07-live-dashboard-{desktop,mobile}.png`.

- [x] Viết E2E kiểm chọn bot, body snapshot, chỉ bot chọn được poll và chuyển lựa chọn không nhận response cũ.
- [x] Viết E2E kiểm stale state, mobile/desktop reflow và keyboard selection.
- [x] Chạy E2E mới, xác nhận RED do UI/poll chưa có.
- [x] Thêm HTML tiếng Việt, JS same-origin/no-store và CSS responsive, không dùng `innerHTML` với dữ liệu server; escape bằng DOM text nodes.
- [x] Chụp screenshot dashboard tại 390×844 và 1280×800; cả hai ảnh thể hiện trade PAPER đang mở và chi tiết đã chọn.
- [x] Chạy Playwright E2E trên hai viewport và mở ảnh để kiểm tra trực quan.

### Task 3: Regression, docs và tích hợp

**Files:**
- Update: `docs/superpowers/plans/2026-11-03-wp07-trader-workbench-wbs.md`
- Update: `docs/implementation/project-status.md`
- Update: `docs/implementation/implementation-memory.md`

- [x] Chạy focused unit, dashboard Playwright, Ruff/format và `git diff --check`; toàn bộ unit suite đạt tại snapshot trước các thay đổi backend fence tiếp theo. Mypy không thay đổi trong Task 2.
- [x] Ghi rõ dữ liệu mới nhất là snapshot PAPER/runtime đã lưu, không phải Binance live feed; không tuyên bố WP-07 hoàn tất.
- [x] Đọc lại memory và status sau khi cập nhật.
