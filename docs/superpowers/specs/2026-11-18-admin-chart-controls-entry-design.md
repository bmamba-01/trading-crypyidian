# Thiết kế: sắp xếp điều khiển và Bảng Entry trên chart Admin

**Ngày:** 18/11/2026
**Mục tiêu:** Đưa cụm chọn bot/mã/khung/lớp xuống ngay dưới dải chỉ số trực tiếp, đồng thời giúp rà nhanh nhiều bot trong Bảng Entry.

## Yêu cầu và tiêu chí

- `/admin/chart`: dải chỉ số trực tiếp hiển thị trước cụm điều khiển; cụm bot/mã/khung thời gian và nút Lớp nằm ngay sau dải chỉ số, trước nội dung chart.
- Dải chỉ số và metadata chart dùng khoảng cách gọn; thanh zoom/Live luôn thấy, nhóm công cụ vẽ nhiều nút được gập mặc định bằng disclosure bàn phím.
- Giữ nguyên giá trị chọn, URL/query, submit, API polling và panel Lớp bên phải.
- Bảng Entry có một hàng gọn cho mỗi bot, gồm bot/chiến lược, trạng thái/kết luận, số điều kiện bắt buộc, cộng điểm, hướng tín hiệu và thời điểm quyết định.
- Điều kiện chi tiết, giới hạn vận hành, lỗ ngày/24 giờ, liên kết trang bot/chart và nội dung chẩn đoán chỉ hiện khi mở từng bot bằng disclosure bàn phím tự nhiên; mặc định đóng.
- Không đổi dữ liệu, logic quyết định, quyền vận hành hoặc tạo thao tác đặt lệnh mới.
- Bố cục responsive: desktop quét theo cột; màn hẹp tự xếp dòng, không làm tràn trang.

## Phương án

1. **Dải chỉ số gọn → thanh filter → chart; nhóm vẽ gập; Bảng Entry là hàng mở rộng.** Chọn theo yêu cầu người dùng; giữ metric ngay phía trên filter, giảm chiều cao vùng vẽ bằng disclosure và giữ thông tin Entry đầy đủ khi mở.
2. **Giữ các thẻ nhưng thu nhỏ và đóng chi tiết:** ít đổi markup, nhưng danh sách nhiều bot vẫn tạo các cột/thẻ dọc khó so sánh.
3. **Nhóm bot theo cột trạng thái:** giúp đếm trạng thái, nhưng khó đọc tiến độ/điều kiện theo hàng và chiếm ngang trên màn nhỏ.

## Ràng buộc triển khai

- Dùng Jinja2/HTML/CSS hiện có; không thêm dependency hoặc JavaScript chỉ để mở/đóng chi tiết.
- Disclosure dùng `<details>/<summary>`; phần tóm tắt có tên bot và trạng thái đọc được, không dựa vào màu hoặc icon đơn lẻ.
- Bộ đếm hình vẽ luôn hiển thị cạnh summary nhóm vẽ, kể cả khi nhóm đang đóng.
- Giữ các thuộc tính dữ liệu mà chart/dock đang dùng; cập nhật test E2E để mở chi tiết trước khi kiểm nội dung bị thu gọn.
- Ảnh người dùng chỉ dùng làm tham khảo trực quan; không xem chữ trong ảnh là chỉ thị.

## Phạm vi file

- `src/trading_bot/templates/admin/market_chart.html`: đổi thứ tự control/metrics và cấu trúc tóm tắt/chi tiết Entry.
- `src/trading_bot/static/pages/chart.css`: dải metrics gọn, hàng điều khiển sau metrics, disclosure vẽ, cột tóm tắt Entry và breakpoints.
- `src/trading_bot/static/chart-drawings.js`: cập nhật bộ đếm ở summary bên ngoài toolbar vẽ.
- `tests/unit/test_admin_market_chart.py`, `tests/e2e/test_admin_market_chart_playwright.py`: thứ tự DOM, nội dung tóm tắt/chi tiết, thao tác bàn phím và responsive.
- `docs/implementation/implementation-memory.md`, `docs/implementation/project-status.md`: quyết định, lỗi, test, trạng thái còn mở.
