# Popup chi tiết và phân trang danh sách dài trên Admin

## Mục tiêu

Giữ trang chính ở trạng thái dễ quét nhanh: hiển thị các trường tóm tắt, còn nội dung diễn giải sâu mở trong hộp thoại khi người dùng chọn **Chi tiết**. Danh sách dữ liệu dài hiển thị tối đa 20 mục mỗi trang, bắt đầu ở trang đầu.

## Phạm vi

- Trang biểu đồ: mỗi bot ở Bảng Entry là một hàng/card tóm tắt; checklist, trạng thái vận hành, giới hạn rủi ro và lý do còn thiếu nằm trong popup riêng.
- Trang danh sách bot: thêm popup xem nhanh trạng thái, mức sẵn sàng, trạng thái vào lệnh và mã cặp nếu có; popup dẫn tới trang bot đầy đủ.
- Trang chi tiết bot: hàng giao dịch giữ các chỉ số cần đọc nhanh; lý do vào/thoát, sai lệch, phí, funding, trượt giá và kế hoạch được mở trong popup giao dịch.
- Toàn Admin: bộ phân trang dùng chung tự áp dụng cho mỗi `<tbody>` có trên 20 dòng, `<ul>/<ol>` có trên 20 mục, và vùng thẻ dữ liệu được đánh dấu rõ trong template. Mỗi vùng có bộ đếm, nút trước/sau, nhãn truy cập và kích thước mặc định 20.

## Tương tác và khả năng truy cập

- Dùng native `<dialog>` cùng `dialog.js` hiện có; Escape/nút đóng trả focus về nút mở, nội dung popup có thể cuộn độc lập.
- Nút chỉ có icon phải có tên truy cập và tooltip tiếng Việt; điều khiển phân trang có tên “Trang trước”/“Trang sau”, trạng thái disabled ở biên, và dòng trạng thái đọc được bởi trình đọc màn hình.
- Phân trang chỉ ẩn các mục ngoài trang đang chọn. Không đổi quyền, endpoint, bộ lọc, truy vấn, thứ tự, nội dung hay thao tác biểu mẫu.
- Danh sách ngắn không xuất hiện điều khiển phân trang. Khi JavaScript lỗi, nội dung HTML vẫn đọc được.

## Ma trận rà soát

| Khu vực | Trang | Cách hiển thị |
| --- | --- | --- |
| Danh sách bot | `/admin/bots` và roster trên dashboard | Hàng tóm tắt, popup xem nhanh; bảng phân trang tự động |
| Vị thế, giao dịch, hậu kiểm | `/admin/bots/{id}` và `/admin/reports` | Chi tiết giao dịch trong popup; bảng/thẻ dài phân trang |
| Entry và nhật ký trên biểu đồ | `/admin/chart` | Hàng bot gọn, popup checklist; danh sách dài phân trang |
| Tín hiệu đã chấm | `/admin/signal-outcomes` | Bảng tín hiệu phân trang; bộ lọc và KPI giữ nguyên |
| AI, replay, scanner, lịch, nguồn và học máy | Các trang admin tương ứng | Tự phân trang bảng/danh sách dài; vùng lưới thẻ được đánh dấu theo template |

## Không thuộc phạm vi

- Không thay đổi logic giao dịch, quyền dữ liệu, API, form submit hay cấu hình backend.
- Không thêm thư viện/dependency mới.
- Không chuyển toàn bộ trang bot thành popup; popup là lối xem nhanh/đọc sâu từ trang chính, còn trang bot đầy đủ vẫn là đường dẫn riêng.

## Tiêu chí nghiệm thu

- Entry và giao dịch có nút Chi tiết mở đúng popup; modal đóng bằng Escape và khôi phục focus.
- Danh sách dài bắt đầu với 20 mục; điều hướng tới trang tiếp theo/đầu tiên cập nhật đúng phạm vi mục đang thấy, không mất mục.
- Danh sách ngắn không có phân trang; nút phân trang không tràn ngang trên mobile.
- Unit và E2E liên quan chạy đạt; Ruff và `git diff --check` sạch.
