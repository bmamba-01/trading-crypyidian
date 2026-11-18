# Sắp xếp filter và Bảng Entry trên chart — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Đặt filter ngay dưới chỉ số trực tiếp và thu gọn Bảng Entry theo bot, vẫn giữ chi tiết mở được.

**Architecture:** Di chuyển thứ tự DOM của toolbar sau live metrics; gom công cụ vẽ trong disclosure HTML, giữ zoom/Live và bộ đếm hình vẽ luôn thấy. Đổi mỗi thẻ Entry thành disclosure `<details>/<summary>`; giữ nguyên dữ liệu sẵn có trong vùng mở rộng.

**Tech Stack:** Python 3.14, Jinja2, HTML, CSS, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-18-admin-chart-controls-entry-design.md`

## Global Constraints

- Comment, hướng dẫn, tài liệu bằng tiếng Việt; identifier bằng tiếng Anh.
- Không thay API, logic giao dịch, URL/query, giá trị biểu mẫu hoặc quyền.
- Không thêm dependency; dùng disclosure HTML bàn phím tự nhiên.
- Giữ trạng thái Entry có nhãn văn bản, tương phản và responsive.

## Review Focus

- Trang không có chart payload vẫn hiện bộ chọn và trạng thái rỗng đúng.
- Mở/đóng Entry bằng bàn phím hoạt động nhờ `<summary>` native.
- Nội dung chẩn đoán không còn trong hàng gọn nhưng vẫn đầy đủ khi mở.
- Ở màn hẹp không có tràn ngang toàn trang.
- Trigger panel lớp vẫn hoạt động sau khi được chuyển khỏi toolbar cha cũ.

## File Map

- `src/trading_bot/templates/admin/market_chart.html`: đổi thứ tự metric/control, tóm tắt và disclosure Entry.
- `src/trading_bot/static/pages/chart.css`: dải metrics gọn, thanh điều khiển sau metrics, disclosure công cụ vẽ và hàng Entry responsive.
- `src/trading_bot/static/chart-drawings.js`: cập nhật bộ đếm vẽ nằm ngoài toolbar gập.
- `tests/unit/test_admin_market_chart.py`: đảm bảo thứ tự metric/control, markup summary/details và các trường quan trọng.
- `tests/e2e/test_admin_market_chart_playwright.py`: kiểm thứ tự trực quan, panel Lớp và thao tác mở/đóng chi tiết.
- `docs/implementation/implementation-memory.md`, `docs/implementation/project-status.md`: record vòng và kết quả kiểm.

## Tasks

### Task 1: Reorder chart controls and collapse Entry details

**Files:** template, chart CSS, chart unit/E2E tests, implementation memory, project status.

- [x] Thêm unit regression bảo đảm thứ tự `mc-live-strip` → `mc-toolbar` và Bảng Entry có summary gọn, chi tiết condition/monitor/links trong disclosure.
- [x] Thêm E2E xác nhận nhóm vẽ mặc định đóng, mở được bằng phím, và bộ đếm cạnh summary vẫn cập nhật.
- [x] Chạy unit regression và xác nhận đỏ vì toolbar và disclosure chưa tồn tại.
- [x] Sửa template: giữ query/options hiện tại; đưa toolbar sau metrics; mỗi bot là `<details>` đóng mặc định với các trường tóm tắt đã duyệt, dữ liệu hiện hành trong body.
- [x] Cập nhật CSS desktop/mobile: cột summary rõ, chi tiết mở dùng grid, không tràn ngang.
- [x] Cập nhật E2E: kiểm thứ tự vị trí, disclosure ban đầu đóng, mở bằng keyboard, nội dung đầy đủ, đóng lại; giữ kiểm trigger panel/filter.
- [x] Chạy unit chart, E2E chart và regression live/risk liên quan; lưu screenshot desktop và mobile.
- [x] Cập nhật project-status và implementation-memory, đọc lại memory và đối chiếu với diff/test.
- [ ] Chạy `git diff --check`, rà form names/query và commit sau khi unit/E2E đạt.

## Verification

- `uv run pytest tests/unit/test_admin_market_chart.py -q`
- `uv run pytest tests/e2e/test_admin_market_chart_playwright.py tests/e2e/test_admin_live_monitor_playwright.py tests/e2e/test_admin_risk_appetite_playwright.py -q`
- `uv run ruff check tests/e2e/test_admin_market_chart_playwright.py`
- `git diff --check`
