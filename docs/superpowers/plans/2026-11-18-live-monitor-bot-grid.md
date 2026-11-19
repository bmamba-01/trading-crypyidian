# Tối ưu Live và số liệu báo cáo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Hiển thị danh sách bot ở trang Live thành lưới card bốn cột trên desktop; sửa số liệu thập phân và độ tách nền tooltip trên trang báo cáo.

**Architecture:** Giữ API/SSE và hợp đồng snapshot hiện có. Template cung cấp vùng danh sách; `live-monitor.js` dựng từng card từ payload đã có; `admin.css` tạo lưới 4/2/1 cột cùng kiểu chữ, màu trạng thái tương phản trên nền tối. Trang báo cáo dùng `number2_vi` cho tỷ lệ/tiền/bps và bề mặt token đặc cho tooltip; giá/khối lượng vẫn giữ độ chính xác ledger.

**Tech Stack:** FastAPI, Jinja2, vanilla JavaScript, CSS hiện có.

**Spec:** [2026-11-18-live-monitor-bot-grid-design](../specs/2026-11-18-live-monitor-bot-grid-design.md)

## Global Constraints

- Không đổi API, dữ liệu, phạm vi quyền, luồng SSE/poll hoặc thao tác điều khiển.
- Mỗi card giữ tên bot, trạng thái vào lệnh, số vị thế, vốn, drawdown, PnL hôm nay và nhịp runtime.
- Desktop 4 cột, màn hình vừa 2 cột, điện thoại 1 cột.
- Giữ selector `data-bot-row` để hành vi kiểm tra giao diện hiện hữu không mất hiệu lực.
- Không chỉnh sửa hoặc stage các thay đổi khác đang có trong workspace.

## Review Focus

1. Payload không có bot: vùng lưới vẫn có trạng thái trống bằng tiếng Việt.
2. Trạng thái vào lệnh bị chặn: nguyên nhân và readiness hiện đầy đủ, không chỉ đổi màu.
3. Runtime chưa có heartbeat: hiển thị “Chưa có” bằng trạng thái trung tính.
4. PnL âm/dương: màu trạng thái giữ đúng quy ước hiện có.
5. Độ rộng 1280 px: card đủ rộng để đọc; tablet/mobile giảm cột mà không tràn ngang.

## Task 1: Chuyển danh sách bot thành card responsive

**Files:**
- Modify: `src/trading_bot/templates/admin/live.html`
- Modify: `src/trading_bot/static/live-monitor.js`
- Modify: `src/trading_bot/static/admin.css`
- Modify: `tests/e2e/test_admin_live_monitor_playwright.py`
- Update: `docs/implementation/implementation-memory.md`

**Interfaces:**
- Consumes: `state.bots` hiện có (`id`, `name`, `entry_allowed`, `entry_state`, `readiness`, `open_positions`, `equity.equity`, `equity.drawdown_pct`, `net_today`, `heartbeat_age_seconds`, `heartbeat_stale`).
- Produces: `div[data-live-bots][role=list]` chứa `article[data-bot-row][role=listitem]` cho mỗi bot.

- [x] Bổ sung assertion vào E2E hiện có cho card/đủ chỉ số và 4/2/1 cột tại 1280/800/390 px, vẫn không tràn ngang.
- [ ] Chạy E2E trước sửa để xác nhận đỏ vì markup card/lưới chưa tồn tại.
- [x] Thay bảng bot trong template bằng vùng list semantic và trạng thái tải ban đầu.
- [x] Render article card bằng DOM APIs, giữ link tên bot và đủ sáu chỉ số hiện có; giữ `data-bot-row` theo ID.
- [x] Đặt lưới 4 cột từ 1200 px, 2 cột từ 640 px, 1 cột bên dưới; cho panel bot chiếm toàn chiều rộng desktop để card không bị ép.
- [x] Chạy lại E2E liên quan; lưu ảnh desktop/mobile mới làm bằng chứng.
- [x] Cập nhật trạng thái spec và memory; đọc lại mục memory để đối chiếu diff.
- [x] Rà diff và kiểm tra whitespace.

## Task 2: Làm gọn số liệu báo cáo và nền tooltip

**Files:**
- Modify: `src/trading_bot/templates/admin/reports.html`
- Modify: `src/trading_bot/templates/admin/metric_cards.html`
- Modify: `src/trading_bot/static/admin.css`
- Modify: `tests/unit/test_admin_reports_revamp.py`
- Modify: `tests/e2e/test_admin_trading_revamp_playwright.py`
- Update: `docs/implementation/implementation-memory.md`

**Quyết định:**
- Chỉ số tiền, phần trăm, tỷ lệ và bps hiển thị tối đa 2 chữ số thập phân; tooltip/title vẫn giữ giá trị ledger đầy đủ.
- Giá và khối lượng giao dịch giữ nguyên độ chính xác để tránh làm sai mã giá nhỏ.
- Tooltip dùng nền token `--bg-base` đặc, không dùng backdrop blur.

- [x] Bổ sung assertion đỏ cho số chi tiết có nhiều chữ số, R:R/rủi ro/bps theo định dạng vi-VN và màu nền tooltip đặc.
- [x] Chuyển phần chỉ số chi tiết trên trang báo cáo sang chế độ làm tròn, giữ số gốc trong `title`.
- [x] Chuẩn hóa các tỷ lệ/bps/PnL đối soát còn định dạng thô.
- [x] Đặt nền tooltip khác nền card, tắt backdrop blur.
- [x] Chạy kiểm tra unit/E2E mục tiêu và xem lại ảnh báo cáo/tooltip.
- [x] Cập nhật rồi đọc lại memory.
- [x] Rà diff và whitespace sau cập nhật tài liệu.
