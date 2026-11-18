# Thiết kế: tối ưu bộ lọc và không gian biểu đồ Admin

**Ngày:** 18/11/2026
**Mục tiêu:** Đưa biểu đồ vào vùng nhìn ban đầu trên desktop bằng cách rút gọn điều khiển, đồng thời chuẩn hóa hình thức bộ lọc trên các trang Admin có nhiều tiêu chí.

## Phạm vi và tiêu chí đạt

- Trên `/admin/chart`, hàng điều khiển mặc định giữ bot, mã, khung thời gian và một nút mở cài đặt. Mười chín lớp hiển thị và bộ preset chuyển vào panel trượt bên phải, đóng mặc định.
- Panel cho biết số lớp đang bật, giữ đầy đủ nhãn/tooltip và vùng tương tác bàn phím, đồng thời dùng lại lựa chọn lớp hiện tại trong trình duyệt.
- Các bộ lọc của `/admin/reports`, `/admin/scanner`, `/admin/ai-analyses`, `/admin/signal-outcomes`, `/admin/live`, `/admin/risk`, lịch sử chart và bộ lọc tín hiệu Telegram trong trang bot dùng chung nhịp khoảng cách, chiều cao trường, nút và bố cục responsive.
- Giữ nguyên tên trường, phương thức GET/POST, giá trị mặc định, quyền truy cập, thứ tự submit và logic truy vấn. Các form cấu hình có tác động vận hành không bị thu gọn như filter tìm kiếm.
- Icon luôn có nhãn truy cập được; tooltip bổ sung nội dung nhưng không thay nhãn. Không ép chip hoặc trường bị cắt khi thu nhỏ.

## Phương án đã cân nhắc và quyết định

1. Thanh công cụ gọn + panel cài đặt trượt bên phải: giữ mã/bot/khung thời gian trong tầm tay và giải phóng chiều cao cho chart. **Chọn phương án này** cho chart; mô hình panel phù hợp với cách cấu hình chart trên TradingView và Binance Futures.
2. Rail bên trái cố định: dễ truy cập nhưng chiếm bề ngang liên tục và không cần thiết với tuỳ chọn ít dùng.
3. Mọi bộ lọc luôn nằm trên đầu trang dưới dạng chip/icon: nhìn thấy trạng thái ngay nhưng vẫn cao khi có nhiều trường, đồng thời buộc người dùng nhớ nghĩa biểu tượng.

Các bộ lọc truy vấn ở trang dữ liệu không cần một drawer riêng; chúng theo một hàng tiêu chí chính và phần nâng cao có thể gập trên màn hình hẹp, giữ trạng thái lọc hiện thời hiển thị bằng giá trị trường/chip có tên.

## Ràng buộc và ngoài phạm vi

- Dùng Jinja2, HTML, CSS và JavaScript hiện có; không thêm thư viện.
- Không thay màu/kiểu chữ toàn hệ thống, hợp đồng API, chiến lược, hoặc hành vi trading.
- Không suy diễn chữ/nhãn bên thứ ba trong ảnh người dùng thành yêu cầu dự án; ảnh chỉ tham khảo bố cục.
- Không dùng icon trơ không tên trợ năng hoặc chỉ hiển thị trạng thái bằng màu.

## Nguồn và quyết định giao diện

- [TradingView — Configure your Supercharts](https://www.tradingview.com/support/solutions/43000748166-how-to-configure-your-supercharts/) — cài đặt chart mở qua nút bánh răng; kiểm tra 18/11/2026.
- [Binance — Customize the Futures trading interface](https://www.binance.com/en-ZA/support/faq/detail/a784518335b0492a9ebfa4a72e1ca092) — tuỳ chỉnh mở side panel; kiểm tra 18/11/2026.
- UI/UX Pro Max, tìm kiếm `compact filter toolbar icon tooltip accessibility` và `icon button accessible label tooltip`: dùng button ngữ nghĩa, tên truy cập được, trạng thái `aria-expanded`/`aria-pressed`, focus nhìn thấy, nội dung xếp dòng hoặc có nút mở rộng.
- Kế thừa token, nền tối và vùng chạm 44px của [UI kit Admin](../../design/admin-ui-kit.md); dùng SVG nét cùng phong cách Lucide đang có, không thêm thư viện icon.

## File dự kiến và giới hạn rủi ro

- Chart: template `market_chart.html`, CSS `chart.css`, JS điều khiển chart và lưu lớp; panel native `<dialog>` đóng bằng nút/Escape, giữ focus, lớp được chuyển nguyên cấu trúc vào dialog.
- Bộ lọc dùng chung: stylesheet riêng `static/pages/filters.css` được nạp sau `admin.css`; các trang mục tiêu bổ sung class chung, stylesheet cục bộ chỉ giữ layout riêng.
- Test hiện có liên quan: `tests/unit/test_admin_market_chart.py`, `tests/e2e/test_admin_market_chart_playwright.py` và các bài Playwright cho scanner/reports/AI.
- Rủi ro chính: dialog có thể che nội dung trên viewport hẹp hoặc mất focus sau đóng; kiểm bằng keyboard, Escape, viewport desktop/mobile và giảm chuyển động. CSS dùng `prefers-reduced-motion`.
