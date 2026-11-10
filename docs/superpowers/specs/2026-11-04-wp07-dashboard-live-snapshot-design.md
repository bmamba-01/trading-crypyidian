# Thiết kế ảnh chụp cập nhật dashboard theo bot

**Ngày:** 2026-11-04 <br>
**Trạng thái:** được triển khai theo yêu cầu người dùng; cách cập nhật là quyết định triển khai có thể thay thế sau này.

## Mục tiêu

Cho người vận hành chọn một bot đang bật từ dashboard và xem trạng thái runtime cùng các phiên PAPER (giao dịch đang mở) của bot đó; có thể chọn từng phiên để xem các trường ledger sẵn có. Dữ liệu tự làm mới khi trang đang mở. Trên mobile, danh sách bot và chi tiết xếp thành một cột; trên desktop, danh sách và chi tiết có thể nằm cạnh nhau.

## Hiện trạng và giới hạn dữ liệu

Dashboard hiện render số liệu khi mở trang, dùng `BotOperationsService` và `BotReportingService`. Báo cáo đã đọc ledger theo từng bot, gồm `open_trades`, closed trades và metric. Trạng thái runtime hiện cung cấp readiness/control nhưng chưa đưa heartbeat vào `BotRuntimeStatus`. Repo chưa có entity phiên chạy bot riêng. `market_dispatch` và Binance execution vẫn tắt; vì vậy không được mô tả dữ liệu ledger PAPER là vị thế Binance hoặc giá thị trường trực tiếp.

Trong lát đầu, “phiên” trên dashboard nghĩa là giao dịch/vị thế PAPER đang mở được đọc từ ledger của bot. Snapshot ghi `observed_at`; trường sự kiện gần nhất chỉ hiện khi source hiện hữu cung cấp timestamp. Giá chưa lưu, unrealized PnL và heartbeat không được ước lượng.

## Các phương án

1. **Polling JSON có xác thực, một bot được chọn mỗi lần (đề xuất):** dùng route admin hiện có, quyền `current_identity`, `BotOperationsService` và `BotReportingService`; trang gọi lại mỗi 5 giây, bỏ polling khi tab ẩn và báo thời điểm snapshot. Đây là thay đổi nhỏ, sử dụng dữ liệu có thẩm quyền đang lưu và tránh fan-out cho mọi bot.
2. **SSE/WebSocket:** chỉ phù hợp khi runtime có event stream/transport và giao thức nối lại; hiện chưa có producer hoặc event bus cho dashboard. Thêm riêng UI push sẽ không tạo dữ liệu mới và làm tăng vận hành.
3. **Chỉ nạp lại trang:** an toàn nhưng không đáp ứng yêu cầu cập nhật trực tiếp khi đang theo dõi.

## Quyết định và phạm vi

Chọn polling mỗi 5 giây trên một bot được chọn. Backend kiểm tra bot/runtime và quyền report theo từng request, không nhận workspace từ browser. Report đi qua read-only UoW; `PrincipalBotOperations.bot_status` là truy vấn SELECT-only hiện có nhưng UoW của nó chưa khai báo `snapshot_read_only`, nên lát này không sửa rộng service đó. Response chỉ chứa allowlist dữ liệu trình bày an toàn, timestamp UTC và `observed_at` (thời gian server đọc, không phải heartbeat nguồn). UI nêu rõ đây là dữ liệu PAPER/runtime đã ghi nhận. Lỗi poll giữ snapshot cuối cùng và báo đã mất cập nhật; đổi bot hủy request/poll cũ. Danh sách và panel dùng ngữ nghĩa button/region, có focus/aria status và responsive.

Không thêm bảng phiên, socket, market feed, unrealized PnL hoặc thao tác ghi/lệnh. Việc thêm session entity hoặc stream dữ liệu runtime sẽ cần thiết kế WP riêng khi có producer và semantics được chốt.

## Acceptance

- Người dùng có quyền chỉ chọn được bot ENABLED/ACTIVE họ được phép xem; bot ngoài phạm vi trả cùng dạng 404/không lộ sự tồn tại.
- Chọn bot tải runtime, report summary và các PAPER trade đang mở; người dùng chọn từng open trade để xem chi tiết ledger sẵn có; không trộn bot hay quote asset.
- Snapshot hiển thị thời điểm quan sát và nguồn dữ liệu; lỗi/timeout không xóa dữ liệu cuối và được thông báo.
- Polling chỉ chạy cho bot đang chọn, dừng khi tab ẩn/trang rời, không tạo write hoặc exchange request.
- Playwright kiểm thao tác chọn bot, cập nhật snapshot, lỗi polling, responsive mobile/desktop và lưu ảnh bằng chứng.

## Ràng buộc chung

- Nội dung UI, comment/docstring và tài liệu bằng tiếng Việt; identifier/API bằng tiếng Anh.
- Không thêm dependency hoặc API sàn; phải dùng quyền và unit-of-work hiện hữu.
- Trước commit cần unit và E2E liên quan đạt; lượt này không commit/push.
