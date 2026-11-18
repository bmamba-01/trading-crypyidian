# Hướng dẫn Vận hành Admin Web — Crypidian

**Phạm vi:** Tài liệu hướng dẫn thao tác và kiểm tra giao diện Admin Web dành cho các vai trò `Owner`, `Operator`, `Risk Admin`, `Strategy Admin` và `Analyst Reviewer`.

---

## 1. Đăng nhập, MFA và Bảo mật Phiên

1. Truy cập `/admin/login`.
2. Nhập **Workspace slug**, **Email**, **Mật khẩu** và mã **MFA (TOTP 6 số)** từ ứng dụng xác thực.
3. Mọi trang `/admin/*` đều trả về header `Cache-Control: no-store` để trình duyệt không lưu cache dữ liệu nhạy cảm.
4. Mọi biểu mẫu thay đổi trạng thái (`POST`) đều yêu cầu **CSRF token** hợp lệ; khi hết hạn phiên, hệ thống chuyển hướng về `/admin/login`.

---

## 2. Phân quyền theo Vai trò (RBAC)

| Vai trò | Quyền chính trên Admin Web | Hành vi bị từ chối (HTTP 403) |
| --- | --- | --- |
| **Owner** | Toàn quyền: cấu hình bot, workspace kill/release, flatten, liên kết Telegram, duyệt mô hình. | — |
| **Operator** | Xem dashboard/báo cáo, tạm dừng (`pause`) hoặc tiếp tục (`resume`) từng bot. | Bị từ chối khi kích hoạt `workspace kill/release`, đổi cấu hình chiến lược hoặc liên kết Telegram. |
| **Risk Admin** | Quản trị rủi ro, `pause`/`resume` bot, kích hoạt `workspace kill/release`, quản lý sự cố học máy. | Bị từ chối khi sửa cấu hình chiến lược hoặc duyệt mô hình. |
| **Strategy Admin** | Đọc/ghi cấu hình nháp, validate/publish cấu hình bot và watchlist. | Bị từ chối khi kích hoạt `workspace kill` hoặc thao tác giao dịch khẩn cấp. |
| **Analyst Reviewer** | Xem báo cáo, rà soát giao dịch, gắn nhãn dữ liệu, duyệt nguồn nghiên cứu và split học máy. | Bị từ chối khi `pause`/`resume` bot hoặc `workspace kill`. |

---

## 3. Các Màn hình Vận hành Chính

### 3.1. Bảng điều khiển (`/admin`) & Live Monitor (`/admin/live`)
- Theo dõi trạng thái kill-switch của workspace (`ACTIVE` / `KILLED`), danh sách bot, PnL, drawdown, số vị thế mở và trạng thái circuit breaker.
- Mỗi nhãn chỉ số có nút chú giải `.metric-help`: bấm, rê chuột hoặc nhấn `Tab` + `Enter` để mở tooltip (định nghĩa, cách tính, cách đọc, giới hạn); nhấn `Escape` để đóng.

### 3.2. Quản lý Bot & Cấu hình (`/admin/bots`, `/admin/bots/new`, `/admin/bots/{bot_id}`)
- Tạo bot mới ở chế độ `PAPER` (mặc định an toàn).
- Chỉnh sửa cấu hình theo quy trình 3 bước: **Tạo bản nháp (Draft)** → **Kiểm tra hợp lệ (Validate)** → **Phát hành (Publish)**, có thể **Rollback** về phiên bản trước.
- Điều khiển runtime trực tiếp trên trang chi tiết bot: `Pause`, `Resume`, `Disable`, `Enable`.

### 3.3. Ngắt khẩn cấp & Flatten Toàn bộ (`/admin/flatten`)
- **Workspace Kill (`/admin/workspace/kill`):** khóa cổng vào lệnh mới ngay lập tức trên toàn workspace.
- **Flatten khẩn cấp (`/admin/flatten`):**
  1. Bấm **Xem trước (Preview)** để kiểm tra số bot, vị thế mở và lệnh đang chờ sẽ bị đóng/hủy.
  2. Xác nhận **Confirm Flatten**; theo dõi tiến độ qua status live region trên trang chi tiết thao tác (`/admin/flatten/operations/{operation_id}`).

### 3.4. Liên kết Telegram (`/admin/telegram`)
- Tạo liên kết một lần (có thời điểm hết hạn rõ ràng) để ghép tài khoản Telegram với tư cách thành viên trong workspace.
- Cho phép **Thu hồi (Revoke)** liên kết và cấu hình **Topic** nhận cảnh báo.
- Giao diện tuyệt đối không hiển thị Telegram user ID thô.

### 3.5. Báo cáo, Biểu đồ & Nghiên cứu
- **Báo cáo & Xuất CSV (`/admin/reports`, `/admin/reports/export.csv`):** lọc theo bot, thời gian, loại giao dịch; gắn nhãn mẫu âm tính cho AI learning.
- **Biểu đồ (`/admin/chart`):** biểu đồ nến OHLC theo múi giờ `Asia/Ho_Chi_Minh`, chú giải bằng chữ không chỉ dựa vào màu sắc.
- **Strategy Cards & Microscope (`/admin/strategy-cards`, `/admin/strategy-cards/{card_id}/microscope`):** kiểm tra từng nến quyết định và lý do vào/bỏ qua lệnh.
- **AI Analyses & Signal Outcomes (`/admin/ai-analyses`, `/admin/signal-outcomes`):** xem báo cáo phân tích và kết quả tín hiệu.
- **Lịch sự kiện & Scanner (`/admin/calendar`, `/admin/scanner`):** quản lý sự kiện kinh tế và quét cặp giao dịch.

---

## 4. Hướng dẫn Trợ năng & Điều hướng Bàn phím

Admin Web hỗ trợ thao tác hoàn toàn bằng bàn phím và chế độ phóng đại **200%**:
- **Bỏ qua điều hướng:** Nhấn `Tab` ngay khi tải trang để chọn liên kết *Bỏ qua điều hướng* (`#main-content`), nhấn `Enter` để nhảy thẳng vào nội dung chính.
- **Di chuyển:** `Tab` / `Shift+Tab` di chuyển qua các liên kết, nút bấm, công tắc kiểu iOS và ô nhập liệu theo đúng thứ tự đọc; viền focus (`:focus-visible`) luôn hiển thị rõ.
- **Kích hoạt & Đóng:** `Enter` hoặc `Space` để bật/tắt công tắc, mở chú giải `.metric-help`, hoặc gửi biểu mẫu; `Escape` để đóng tooltip hoặc danh sách thả xuống.
- **Giảm chuyển động:** Tôn trọng cài đặt hệ thống `prefers-reduced-motion`.
