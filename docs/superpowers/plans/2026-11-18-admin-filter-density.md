# Tối ưu bộ lọc Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Đưa biểu đồ thị trường vào viewport ban đầu bằng panel cài đặt gọn, đồng bộ các bộ lọc dữ liệu Admin trong cùng hệ thống thị giác.

**Architecture:** Panel `<dialog>` bên phải giữ nguyên các lựa chọn lớp/preset và nút mở trong toolbar. Bộ lọc trang dữ liệu dùng một stylesheet chung đặt sau `admin.css`, còn CSS riêng giữ bố cục mỗi trang. Không thay logic query hoặc API.

**Tech Stack:** Python 3.14, Jinja2, HTML, CSS, JavaScript vanilla, pytest/Playwright đã có trong repo.

**Spec:** `docs/superpowers/specs/2026-11-18-admin-filter-density-design.md`

## Global Constraints

- Hướng dẫn, comment và docstring bằng tiếng Việt; identifier bằng tiếng Anh.
- Không thêm dependency; không đổi hành vi dữ liệu/trading.
- Giữ kích thước vùng chạm tối thiểu 44px, focus bàn phím rõ, nhãn icon có thể truy cập, giảm animation khi `prefers-reduced-motion`.
- Giữ đủ nhãn trường, form method/action/name/value và quyền hiện hành.

## Review Focus

- Khi đóng dialog bằng Escape hoặc nút đóng, focus phải trở về nút Cài đặt.
- Khi mở dialog trên mobile, nội dung cuộn được và không tràn viewport.
- Đổi/bật lớp vẫn lưu và vẽ giống trước; preset vẫn cập nhật số lớp bật.
- GET/POST của Scanner và bộ lọc Telegram giữ nguyên action, method và trường.
- Bộ lọc màn hình hẹp không cắt nhãn hay cuộn ngang trang.

## File Map

- `src/trading_bot/templates/admin/market_chart.html`: toolbar và dialog chứa lớp.
- `src/trading_bot/static/pages/chart.css`: toolbar thấp, dialog cạnh phải, trạng thái mở/đóng và responsive.
- `src/trading_bot/static/market-chart.js`: mở/đóng dialog, focus trả về và bộ đếm lớp.
- `src/trading_bot/static/pages/filters.css`: lớp chung cho form lọc, chip trạng thái và co giãn responsive.
- `src/trading_bot/templates/admin/{reports,market_scanner,ai_analyses,signal_outcomes,bot_detail,live,risk_appetite,market_chart}.html`: gắn class chung, không đổi tham số biểu mẫu.
- `src/trading_bot/templates/admin/base.html`: không đổi giao diện toàn cục; stylesheet dùng trang mục tiêu tự nạp.
- `docs/superpowers/plans/2026-11-15-ck-chart-cockpit-reference-apps-wbs.md`, `docs/implementation/project-status.md`, `docs/implementation/implementation-memory.md`: traceability và bằng chứng vòng làm.

## Tasks

### Task 1: Panel cài đặt chart

**Files:** template, `chart.css`, `market-chart.js`, test chart unit/E2E hiện có.

- Giữ bot, mã, nút xem, khung thời gian và nút `Lớp & cài đặt` trong một toolbar thấp.
- Chuyển preset và 19 switch vào `<dialog>` phải; bảo toàn `data-layer`, `data-default`, nhóm, aria label và key lưu `localStorage` hiện hành.
- Thêm tên trợ năng, `aria-expanded`, Escape/đóng, focus ban đầu, focus trả lại và `prefers-reduced-motion`.
- Làm panel cuộn độc lập, full-width trên màn hình hẹp; trạng thái mở không làm đổi layout chart.
- Kiểm hiện hành: `uv run pytest tests/unit/test_admin_market_chart.py -q` và `uv run pytest tests/e2e/test_admin_market_chart_playwright.py -q`.

### Task 2: Hệ thống filter trang dữ liệu

**Files:** tạo `filters.css`, sửa các template mục tiêu và liên kết CSS tương ứng.

- Thêm lớp chung `admin-filter`, `admin-filter__fields`, `admin-filter__actions`, `admin-filter__advanced` và `admin-filter__active` theo cấu trúc phù hợp; nhãn trường vẫn hiện.
- Dùng cùng chiều cao, khoảng cách, focus, nút clear/submit và responsive wrap; không đổi input names hoặc route.
- Áp dụng cho Báo cáo, Scanner, Phân tích AI, Kết quả tín hiệu, Theo dõi trực tiếp, Khẩu vị rủi ro, lịch sử chart và bộ lọc subscription tín hiệu trong trang bot.
- Không đưa form rủi ro, publish, hoặc form cấu hình vận hành vào accordion.
- Kiểm hiện hành: các Playwright admin phù hợp và `tests/unit/test_admin_market_chart.py` để xác nhận HTML/template.

### Task 3: Traceability và hoàn tất vòng

**Files:** WBS CK, `project-status.md`, `implementation-memory.md`, glossary chỉ nếu xuất hiện thuật ngữ giao dịch mới.

- Thêm mục `UI-Admin/filters` vào plan và project status.
- Cập nhật memory bằng nguồn giao diện đã kiểm, quyết định, file, sự cố/nguyên nhân nếu có, các kiểm tra đã chạy/chưa chạy, giới hạn còn mở.
- Đọc lại memory và so khớp với diff/evidence. Chỉ chuyển ticket Done khi unit và E2E liên quan chạy đạt theo AGENTS.md.

## Verification

- `uv run pytest tests/unit/test_admin_market_chart.py -q`
- `uv run pytest tests/e2e/test_admin_market_chart_playwright.py -q`
- Chạy các Playwright hiện có cho Scanner, Báo cáo, AI và kiểm bộ tương ứng qua mobile/desktop.
- Rà `git diff --check`; kiểm không thay form method/action/field/API ngoài dự kiến.
