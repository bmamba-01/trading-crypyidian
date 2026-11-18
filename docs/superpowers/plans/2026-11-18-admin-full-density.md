# CRY-443 — Rà soát mật độ toàn Admin

**Goal:** Chuẩn hóa nút gọn/icon và khả năng thu gọn nội dung phụ cho toàn bộ page Admin.

**Architecture:** Nạp CSS mật độ dùng chung sau `admin.css`; SVG icon nội tuyến trong các control lặp lại; disclosure `<details>/<summary>` cho các bảng/lịch sử/chẩn đoán phụ. Giữ nguyên tab, form vận hành và markup nghiệp vụ hiện hành.

**Spec:** `docs/superpowers/specs/2026-11-18-admin-full-density-design.md`

## Global constraints

- Desktop dùng nút nhỏ gọn; con trỏ cảm ứng vẫn giữ vùng bấm 44px.
- Icon-only cần `aria-label`, `title`, focus thấy được; thao tác rủi ro giữ nhãn chữ.
- Không đổi endpoint, method/action/name/value, quyền, state, logic trading hoặc dữ liệu.
- Thay đổi tương tác theo RED→GREEN; không thêm dependency.

## Task 1 — Hệ thống control gọn và bộ icon

**Files:** `base.html`, CSS mới `pages/admin-density.css`, `filters.css`, chart template/CSS, filter templates/tests.

- [x] Viết regression cho CSS nạp toàn cục, nút gọn, vùng cảm ứng ≥44px, icon filter có tên truy cập.
- [x] CSS global: desktop button 34–38px, form controls 38–40px; media coarse 44px. Không ép `min-width:44px` lên nút văn bản.
- [x] Icon hóa filter submit/search, zoom/home/reset/Live, lớp chart và công cụ vẽ; tooltip + accessible name đủ mô tả.
- [x] Giữ chữ cho pause/resume/disable/delete/publish/run/write-log, form lưu và các hành động có hệ quả.
- [x] Kiểm E2E chart/scanner/live/risk, labels, form contract, desktop/mobile.

## Task 2 — Thu gọn khu dữ liệu phụ và audit từng page

**Files:** `market_scanner.html`, `signal_outcomes.html`, `ai_analysis_detail.html`, `run_report.html`, `live.html`, `risk_appetite.html`, CSS page tương ứng, tests/E2E.

- [x] Scanner: giữ kết quả chính; gập watchlist, mã loại và lịch sử quét.
- [x] Signal outcomes: giữ filter, KPI, phân phối; gập các breakdown và tín hiệu gần đây.
- [x] AI detail/Run Report: giữ summary, nhận định, đề xuất; gập backtest/context và lượt report liên quan.
- [x] Live: giữ sức khỏe, vốn, bot, vị thế; feed sự kiện dài mở riêng. Risk: chỉ gập lịch sử phiên bản.
- [x] Kiểm toàn bộ 32 page templates; ghi từng trang trong ma trận thiết kế.
- [x] E2E desktop/mobile chart/scanner/live/risk; scanner xác nhận disclosure đóng mặc định và mở để đọc.

## Task 3 — Ghi nhận và hoàn tất

- [x] Unit liên quan 208 passed; E2E design-system/chart/scanner/live/risk 9 passed; `git diff --check` đạt. Ruff/staged diff là bước cuối.
- [x] Chụp và xem ảnh chart/scanner desktop; E2E xác nhận responsive mobile; rà form contract và labels.
- [x] Cập nhật project status, WBS, implementation memory; đọc lại memory.
- [ ] Commit/push `dev` và restart admin-web theo yêu cầu đã có; kiểm health và UI live.
- [ ] Ghi kết quả lên tracker CRY-443.

## Review focus

- Nút desktop giảm bề ngang nhưng không làm nút hành động quan trọng khó hiểu.
- Màn hình cảm ứng giữ target 44×44px.
- Disclosure mặc định chỉ giấu phần thứ cấp; mở bằng bàn phím và summary có tên/đếm rõ.
- Không thay đổi submit behavior khi form có nhiều nút như Scanner.
- Các state tab/ARIA cũ không xung đột với `<details>`.
