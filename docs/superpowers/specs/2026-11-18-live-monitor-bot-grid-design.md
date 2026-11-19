# Thiết kế lưới bot cho trang theo dõi trực tiếp

**Ngày:** 2026-11-18
**Trạng thái:** Đã được người dùng duyệt

## Trạng thái triển khai

- Card Live đã được triển khai và E2E xác nhận đủ 4/2/1 cột tại 1280/800/390 px; ảnh desktop/mobile được lưu ở `tests/e2e/evidence/`. Docker Desktop đã restart bằng lệnh quản lý chính thức để khôi phục engine.
- Phần số liệu báo cáo và tooltip đã qua 119 unit test liên quan và E2E trang báo cáo/tooltip (1 test). Toàn bộ thay đổi đã qua kiểm tra nhưng đang chờ commit/push.

## Bổ sung: số liệu báo cáo và tooltip

Người dùng yêu cầu trên trang `/admin/reports` các giá trị thập phân được rút gọn còn tối đa 2 chữ số và tooltip khi rê chuột có bề mặt riêng, không để nền phía sau lẫn vào.

- Làm tròn số tiền, tỷ lệ, phần trăm và bps để đọc nhanh; giữ giá/khối lượng đầy đủ vì coin giá nhỏ có thể bị sai nếu làm tròn.
- Giữ số ledger đầy đủ trong `title` để đối soát.
- Tooltip lấy nền đặc từ token `--bg-base`, không dùng hiệu ứng blur. `--bg-surface-subtle` ở giao diện tối là màu trong suốt 55%, nên không phù hợp làm tooltip.

## Mục tiêu

Giúp người vận hành đọc nhanh danh sách nhiều bot trên trang `/admin/live`, bỏ cuộn ngang và làm các chỉ số rõ trên nền tối.

## Phạm vi

- Thay bảng bot bằng card; desktop hiển thị đúng 4 cột, màn hình vừa 2 cột, điện thoại 1 cột.
- Mỗi card giữ tên bot và toàn bộ dữ liệu hiện có: trạng thái vào lệnh, số vị thế, vốn, drawdown, PnL hôm nay và nhịp runtime.
- Dùng màu/trạng thái và kiểu chữ có độ tương phản rõ; giữ liên kết từ tên bot sang trang chi tiết.
- Không đổi API, dữ liệu, phạm vi quyền, cập nhật SSE/poll hoặc thao tác điều khiển.

## Phương án

1. Giữ bảng và chỉ sửa độ tương phản: ít thay đổi nhất, nhưng vẫn rộng và có cuộn ngang trong cột hiện tại.
2. Chuyển sang card 4 cột có đủ chỉ số (đề xuất): giảm chiều ngang, các trạng thái dễ quét hơn; trên màn hình hẹp số cột tự giảm để nội dung không bị ép.

Chọn phương án 2 theo yêu cầu card 4 cột của người dùng.

## Tiêu chí hoàn tất

- Mỗi bot là một card độc lập, vẫn thể hiện đủ sáu chỉ số và tên bot có thể mở trang chi tiết.
- Desktop có 4 cột; tablet có 2; mobile có 1.
- Không xuất hiện thanh cuộn ngang trong danh sách bot; màu chữ và trạng thái đọc rõ trên nền tối.
- Luồng dữ liệu và hành vi cập nhật trực tiếp hiện tại giữ nguyên.
