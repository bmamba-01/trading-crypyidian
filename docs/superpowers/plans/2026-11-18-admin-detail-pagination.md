# CRY-444 — Popup chi tiết và phân trang danh sách dài

**Spec:** `docs/superpowers/specs/2026-11-18-admin-detail-pagination-design.md`

## Các bước

1. [x] Viết kiểm thử đỏ cho hợp đồng popup Entry/bot/giao dịch, bộ phân trang chung và E2E thao tác trên biểu đồ.
2. [x] Tạo `admin-pagination.js` và CSS điều khiển gọn; phân trang tự động `<tbody>`, `<ul>/<ol>` dài và vùng thẻ được đánh dấu.
3. [x] Đổi Entry trên chart thành hàng tóm tắt + dialog cho từng bot.
4. [x] Thêm popup xem nhanh cho danh sách bot; đưa hậu kiểm từng giao dịch bot vào dialog.
5. [x] Gắn phân trang cho vùng thẻ dài đã được audit; cập nhật kiểm thử template/E2E.
6. [x] Chạy unit, E2E liên quan, Ruff và `git diff --check`; xử lý mọi lỗi/flaky trước commit.
7. [x] Cập nhật `implementation-memory.md`, `project-status.md`, WBS và tracker; đọc lại memory.
8. [x] Commit, push fast-forward lên `dev`, restart admin-web và xác minh health/UI; archive worktree.

## Lệnh kiểm tra dự kiến

- Unit: `uv run pytest tests/unit/test_admin_list_pagination.py tests/unit/test_admin_market_chart.py tests/unit/test_admin_bot_pages_ui_kit.py tests/unit/test_bot_detail_trading_revamp.py tests/unit/test_cry430_signal_outcomes_page.py tests/unit/test_admin_reports_revamp.py tests/unit/test_admin_reports_page_layout.py -q`
- E2E: `uv run pytest tests/e2e/test_admin_market_chart_playwright.py tests/e2e/test_admin_dashboard_playwright.py -q`
- Chất lượng: `uv run ruff check src/ tests/` và `git diff --check`.
