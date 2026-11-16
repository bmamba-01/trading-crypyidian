# Admin UI kit (v1.1, 2026-11-15)

Chuẩn giao diện chung cho mọi trang Admin Web sau đợt revamp. Trang mẫu đã làm theo chuẩn này: **Tổng quan** (`templates/admin/dashboard.html`, `static/dashboard-live.js`). Quyết định thiết kế gốc: [`design-system/crypidian-admin/pages/admin-shell-glass.md`](../../design-system/crypidian-admin/pages/admin-shell-glass.md).

## 1. Ràng buộc bắt buộc

- **CSP** `default-src 'self'`: không `<script>` nội tuyến, không thuộc tính `on*=`, không `style="…"`, không `data:` URI, không tài nguyên ngoài (font, ảnh, CDN). Đặt style qua CSSOM (`el.style.setProperty`) thì được. Test chặn: `tests/unit/test_admin_templates_csp_compliance.py`.
- **Vùng chạm**: mọi `button`, `input` (trừ checkbox/radio), `select`, `textarea` hiển thị phải cao ≥ 44px (E2E `assert_responsive` kiểm ở 360/390/768/1024/1280px). Nút nhỏ về thị giác thì dùng `min-height: 44px` + `margin-block` âm.
- **Focus** nhìn thấy bằng `outline` thật (không chỉ `box-shadow`).
- **Không tràn ngang** ở mọi viewport; bảng rộng đặt trong khung `overflow-x: auto`.
- **Tiếng Việt** cho chữ hiển thị, comment, docstring; định danh tiếng Anh. Thuật ngữ mới → `docs/research/glossary-vi.md`.
- **Giữ nguyên định danh mà test dựa vào** (id, `aria-label`, `name`, nhãn form, tên nút, `data-*`). Đổi chữ hiển thị chỉ khi cập nhật test tương ứng vì thay đổi giao diện hợp lệ.
- **Không bịa dữ liệu**: chỉ hiển thị dữ liệu có nguồn; thiếu thì ghi "Chưa có dữ liệu".

## 2. Khung trang (đã có, không sửa nếu không cần)

- `templates/admin/base.html`: sidebar (`nav.main-nav`, "Điều hướng chính"), topbar (huy hiệu PAPER, email, đổi giao diện), `<main id="main-content">` có `<h1>` từ `{% block heading %}`.
- Thêm CSS riêng của trang qua `{% block head_extra %}<link rel="stylesheet" href="/static/pages/<trang>.css">{% endblock %}`; JS riêng qua `{% block scripts %}`.
- Script chung đã nạp ở mọi trang: `app-shell.js`, `theme-toggle.js`, `toast.js` (`window.crypidianToast.show({tone,title,message})`), `tabs.js`, `metric-tooltip.js`.

## 3. Token

- Chữ: `--fs-2xs .68rem` · `--fs-xs .74rem` (nhãn) · `--fs-sm .82rem` (giá trị trong hàng) · `--fs-md .9rem` · `--fs-lg 1.1rem` (số trong ô) · `--fs-xl 1.45rem` · `--fs-2xl 1.85rem` (KPI).
- Bề mặt kính: `--glass-bg`, `--glass-bg-strong`, `--glass-border`, `--glass-shadow`, `--glass-blur`. Màu lãi/lỗ: `--market-up`, `--market-down`, `--pnl-positive-text`, `--pnl-negative-text`.
- Bo góc: thẻ `0.8rem`, ô số `0.7rem`, chip `999px`. Khoảng cách: 4/8/12/16/24px.

## 4. Thành phần

| Thành phần | Lớp / cách dùng | Ghi chú |
| --- | --- | --- |
| Thẻ kính | `.glass-card` (hoặc lớp đã liệt kê trong khối "Bề mặt kính" của `admin.css`) | Nội dung trong thẻ: padding `0.85rem`. |
| Tab | `<div data-tabs>` + `.tabs-bar[role=tablist]` + `button[role=tab][aria-controls]` + `div[role=tabpanel]` | `tabs.js` tự xử lý bàn phím, hash, nhớ tab. Không đặt `hidden` sẵn trên panel. |
| KPI ngang | `.kpi-card.kpi-card--h` gồm `.kpi-icon.kpi-icon--{blue,green,amber,violet}` + `.kpi-body` (`.kpi-value` + `.kpi-label`) | Số lớn và nhãn trên cùng hàng; mô tả phụ đưa vào tooltip chỉ số. |
| Ô số | `.stat-strip` > `.stat` (`svg.ico` + `.stat__body` > `.stat__value` + `.stat__label`) | Giá trị một dòng; dùng cho 3–6 chỉ số chính. |
| Hàng nhãn–giá trị | `dl.kv-grid` > `div.kv` > `dt` (biểu tượng + nhãn) + `dd` (giá trị) | Nhãn trái được xuống dòng, giá trị phải không ngắt. Không dùng kiểu nhãn trên/giá trị dưới cho dữ liệu dạng bảng thông số. |
| Lãi/lỗ | `span.pnl-value.pnl-value--{positive,negative,flat}` | Trong hàng/ô/bảng: chữ màu + mũi tên, không nền. |
| Tiền | số làm tròn 2 chữ số + biểu tượng USDT `.asset-usdt` (₮) | Phía máy chủ: bộ lọc `number2_vi` (hiển thị) — `number_vi` giữ độ chính xác cho trang cần đối soát. |
| Thời gian | `local_minute` (máy chủ, "14/11 13:13" giờ VN) hoặc dạng UTC rút gọn ở JS | Bản đầy đủ đặt ở `title`. Mốc "cập nhật trực tiếp" giữ giây. |
| ID | viết tắt 8 ký tự + nút sao chép (`.id-chip`, `.copy-btn[data-copy]`) | Giá trị đầy đủ vẫn có trong văn bản (ẩn thị giác) cho tìm kiếm/test. |
| Mã coin | `window.crypidianCoins.chip("BTCUSDT")` hoặc `<span data-coin="BTCUSDT">` (cần nạp `/static/coins.js`) | Icon thật cho mã đã xác định, còn lại chữ cái. |
| Hướng lệnh | `.side-badge.side-badge--{long,short}` | Có biểu tượng mũi tên. |
| Bảng | `table.trade-table` trong khung cuộn | Cột số căn phải; ≤ 767px chuyển thành hàng 2 dòng. |
| Popup | `<dialog class="glass-dialog">` + `showModal()`; nút đóng là `form[method=dialog]` | Dùng cho giải thích chi tiết, không cho thao tác nguy hiểm. |
| Nút biểu tượng | `button.icon-btn` | Có chữ hoặc `aria-label`. |
| Khối thu gọn | `<details class="glass-card">` + `<summary>` | Cho thông tin phụ (lineage, JSON, giải thích dài). |
| Công tắc bật/tắt (v1.1) | `input[type=checkbox].ios-switch[role=switch]` trong `label.switch-row` (nhãn trái, công tắc phải), hoặc `button.switch-control[role=switch][aria-checked]` chứa `span.ios-switch` | Kiểu iOS: rãnh 51×31px, ô bấm input cao 44px. Dùng cho mọi lựa chọn hai trạng thái có hiệu lực ngay. Thao tác có lý do/kiểm toán (tạm dừng, tắt bot, dừng khẩn cấp) giữ dạng form + nút. JS chỉ đổi `aria-checked`. |
| Ô chọn thay ô gõ (v1.1) | `<select>` với mốc từ `admin/form_choices.py` | Trường cấu hình chọn từ danh sách; nhãn hiển thị %/số có ngăn nghìn, giá trị gửi là số thập phân. Option rỗng "Kế thừa cấu hình gốc" khi trường là tùy chọn. Máy chủ vẫn kiểm giới hạn. |
| Multi-select (v1.1) | `<select multiple data-multiselect>`; danh sách coin `data-multiselect="coins"` với `data-name`/`data-icon` trên option | Tom Select 2.6.2 tự host (`static/form-controls.js`). Form gửi trường lặp; route khai báo `multi_value_fields` khi gọi `_parse_form`. Giá trị đã lưu ngoài danh mục vẫn hiện, đánh dấu "(ngoài danh mục)". `select[data-searchable]` cho chọn một có tìm kiếm. |

## 5. Căn chỉnh

- Biểu tượng và chữ trong cùng hàng: `display:flex; align-items:center; gap`.
- Số đứng cạnh nhau: `font-variant-numeric: tabular-nums`, căn phải trong cột/hàng.
- Không để giá trị và nhãn khác chiều cao trong cùng lưới (không chip nền cho giá trị số).
- Kiểm bằng ảnh ở 1440, 1280, 1024, 768, 390, 360px, cả sáng và tối.

## 6. Kiểm thử cho mỗi trang

- Unit: `PYTHONPATH=. uv run pytest tests/unit -q` (đặc biệt `test_admin_*`).
- Playwright in-process (CSP thật): các file `tests/e2e/test_admin_*_playwright.py` liên quan.
- Compose E2E (`tests/e2e/test_admin_web_workflows.py`) do người tích hợp chạy cuối, vì dùng chung image Docker.
- `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy src/trading_bot`, `node --check` cho JS sửa.

## 5. Component dùng chung bổ sung (UI Kit v1.2 — 2026-11-16)

1. **Chủ đề Sáng/Tối đồng bộ hệ điều hành (`static/theme-init.js` + `static/theme-toggle.js`)**:
   - Lắng nghe thay đổi `prefers-color-scheme: dark` khi chưa khóa `localStorage.crypidian_theme`, đồng bộ `data-theme`, `aria-checked` và phát sự kiện `themechange` cho biểu đồ.
   - Nút chính và nút nguy hiểm (`.btn-primary`, `.btn-danger`) dùng `--primary-contrast` / `--danger-contrast` màu trắng `#ffffff` đạt tương phản >= 4.5:1 ở cả Light và Dark Mode; thêm `.btn-danger-outline`.
   - Thanh `.app-topbar` và `.tabs-bar` dùng nền bán đục `--glass-bg-strong` / `color-mix(in srgb, var(--bg-base) 88%, transparent)` tránh chữ xuyên qua khi cuộn trang.
2. **Popup / Hộp thoại dùng chung (`static/dialog.js` + `.glass-dialog`)**:
   - Hỗ trợ `[data-dialog-open="<id>"]`, `[data-dialog-close]`, đóng khi bấm vào nền mờ (`::backdrop`), và khôi phục focus về nút mở sau khi đóng.
   - Cung cấp `window.crypidianDialog.openDetail({ title, lead, rows, rawText, trigger })` cho hộp thoại chi tiết/JSON dùng chung (không `innerHTML`, tuân thủ CSP).
3. **Thông báo nổi Toast (`static/toast.js` + `.app-toast`)**:
   - Đẩy xuống dưới `.app-topbar` khi trang có App Shell, dùng kính mờ `--glass-bg-strong`, nút đóng chạm >= 44px.
   - Hỗ trợ `window.crypidianToast.flash(options)` (qua `sessionStorage`) cho thao tác fetch + `reload()`, và `[data-toast-message][data-toast-mirrored]` cho banner kết quả từ server (`bot_create`, `bot_detail`, `backtest_replays`, `flatten_operation`, `login`).
4. **Cảnh báo & Biểu mẫu thống nhất (`.ui-alert`, `.error-summary`, `.field-error`, `select`, `input[type="file"]`)**:
   - Chuẩn `.ui-alert` với 4 biến thể `--info`, `--success`, `--warning`, `--danger` và `.error-summary` dùng biến CSS tương thích Light + Dark.
   - Tự động gắn mũi tên `/static/icons/chevron-down.svg` cho mọi `select:not([multiple]):not([size])`, tạo dáng `input[type="file"]::file-selector-button`, trạng thái `[aria-invalid="true"]`, `[aria-busy="true"]`, giữ focus outline rõ ràng theo WCAG.
