# Checklist trợ năng Admin Web

Dùng với Chromium ở chế độ phóng đại 200% và chỉ bàn phím. Đây là checklist kiểm tra thủ công; không thay cho đánh giá WCAG đầy đủ.

- [ ] Có thể dùng liên kết bỏ qua điều hướng để chuyển tới nội dung chính.
- [ ] Thứ tự Tab theo thứ tự đọc; mọi liên kết/nút có focus nhìn thấy rõ.
- [ ] Có thể thao tác mọi form chỉ bằng bàn phím; nút submit không tự kích hoạt khi chưa chủ ý.
- [ ] Mỗi trường nhập có nhãn; hướng dẫn, lỗi và trạng thái được trình đọc màn hình liên kết/nhận biết.
- [ ] Mỗi trang có một tiêu đề cấp 1 mô tả nội dung; landmark điều hướng và nội dung chính có tên/cấu trúc rõ.
- [ ] Bảng có caption và tiêu đề cột; nội dung vẫn đọc được ở màn hình hẹp.
- [ ] Danh sách bot có tên dễ nhận biết; trang chi tiết nhóm điều khiển bot và cấu hình bằng tiêu đề mô tả.
- [ ] Trang preview flatten đọc được số bot/vị thế/lệnh và caption bảng trước khi xác nhận; trang operation công bố trạng thái bằng status live region.
- [ ] Trang Telegram hiển thị thao tác link/revoke rõ ràng nhưng không render Telegram user ID; link tạo một lần có tên accessible và có thời điểm hết hạn.
- [ ] Focus không bị che; trạng thái không chỉ phân biệt bằng màu sắc.
- [ ] Chuyển động tùy chọn được giảm khi bật `prefers-reduced-motion`.
- [ ] Nội dung nhạy cảm không bị cache; dữ liệu không đáng tin được escape; token/session không xuất hiện trong văn bản trang hoặc log.
- [ ] Thao tác thay đổi dữ liệu được bảo vệ CSRF và lỗi có thông báo hữu ích, giữ được ngữ cảnh để sửa.
- [ ] Chú giải chỉ số (UI-1): mỗi nhãn chỉ số là một nút `.metric-help` nhận focus bằng Tab, có `aria-describedby` trỏ tới `role="tooltip"` và `aria-expanded` đúng trạng thái; mở được bằng rê chuột, focus và chạm; Escape, bấm ra ngoài hoặc rời focus đều đóng; tooltip nằm trọn trong khung nhìn ở 360px và không làm trang cuộn ngang.
- [ ] Nội dung chú giải khớp từ điển `src/trading_bot/admin/metric_help.py` (định nghĩa, cách tính, cách đọc, giới hạn) và đọc được ở cả giao diện sáng lẫn tối; ở màn hình hẹp, bảng ẩn tiêu đề cột phải có hàng chú giải thay thế.
- [ ] Biểu đồ: chú thích tĩnh (nến, mốc vào/đóng, đường giá, khung thời gian, nguồn nến) hiển thị bằng chữ; chú giải theo con trỏ hiện Mở / Cao / Thấp / Đóng, % thay đổi và thời gian theo `Asia/Ho_Chi_Minh`; thông tin không chỉ truyền đạt bằng màu.

Kết quả kiểm tra và người thực hiện cần được ghi vào implementation memory trước khi nghiệm thu Task 7.
