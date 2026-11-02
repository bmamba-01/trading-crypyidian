# Kế hoạch đề xuất: Admin Web thích ứng điện thoại và tiếng Việt

> **Trạng thái:** Bản rà soát và kế hoạch để xem xét; chưa được phê duyệt và chưa triển khai. Tài liệu này không thay đổi WBS, trạng thái dự án hay thiết kế MB-07b hiện hành.
>
> **Ngày rà soát:** 2026-11-02.

## Mục đích

Làm cho Admin Web dễ truy cập trên điện thoại và máy tính bảng, đồng thời bảo đảm toàn bộ nội dung người vận hành nhìn thấy được viết bằng tiếng Việt rõ ràng, có dấu đầy đủ. Giữ các luồng vận hành, phân quyền, CSRF, MFA, nhật ký kiểm toán và giới hạn PAPER hiện có. Kế hoạch này chỉ thay đổi cách trình bày; không thêm chỉ số giao dịch, luồng vận hành hay hành vi sản phẩm mới.

## Hiện trạng đã kiểm tra

- `src/trading_bot/templates/admin/base.html:2,5` đã khai báo `lang="vi"` và kích thước hiển thị phù hợp thiết bị di động. Điều hướng có các mục tiếng Việt nhưng còn “Flatten workspace” tại dòng 18.
- Hầu hết nhãn và hướng dẫn trong `src/trading_bot/templates/admin/*.html` đã là tiếng Việt. Tuy nhiên, nhiều chữ hiển thị vẫn là tiếng Anh hoặc mã trạng thái thô: “Pause bot”, “Resume bot”, “Disable bot”, “Readiness”, “entry”, “Draft”, “revision”, “Publish”, “Operation”, “Quantity”, “Mark”, cùng các giá trị `READY`, `ACTIVE`, `PAUSED`, `COMPLETED`, `UNKNOWN`, `PAPER`, `DISABLED`. Một số tên riêng như Crypidian và Telegram, địa chỉ email, mã/ID, mã băm và JSON là định danh hoặc dữ liệu; chúng cần được phân biệt với nhãn và hướng dẫn cho người dùng.
- `src/trading_bot/admin/web_router.py:247-260` chuyển tiếp trạng thái từ runtime vào mô hình trình bày dưới dạng chuỗi thô. Các template chi tiết bot, danh sách bot, bảng điều khiển, phần xem trước đóng toàn bộ và trạng thái thao tác hiển thị trực tiếp các chuỗi đó. Các lỗi của biểu mẫu phần lớn trả về `HTTPException`/JSON thay vì trang lỗi tiếng Việt có hướng khắc phục.
- `src/trading_bot/templates/admin/bot_detail.html:6-8,45-61` có tên trường và nội dung còn pha tiếng Anh; `reports.html:15-21` nhận thời gian dạng ISO-8601 và hiển thị báo cáo thành JSON thô. `flatten_preview.html:5,8-16` trình bày thời gian chưa định dạng địa phương và bảng bảy cột; `telegram_bindings.html:21` cũng hiển thị thời gian thô.
- `src/trading_bot/static/admin.css:3` chỉ định `system-ui, sans-serif`. Font thực tế vì thế tùy hệ điều hành/trình duyệt; repo chưa khóa hoặc tự lưu một font và chưa kiểm tra riêng ký tự/dấu tiếng Việt. CSS hiện có thuộc tính `flex-wrap`, kiểu biểu mẫu đăng nhập và chiều rộng bảng cơ bản; không có điểm ngắt bố cục theo màn hình, chỉ có truy vấn media giảm chuyển động tại dòng 103.
- Chưa tìm thấy Bootstrap hoặc Tailwind trong thư viện phụ thuộc, mẫu HTML hay CSS hiện hành. Bảng đóng toàn bộ có bảy cột trong bảng `width: 100%`; báo cáo dùng `<pre>` JSON. Trên màn hình hẹp, hai thành phần này có nguy cơ cuộn/tràn ngang và khó đọc.
- `tests/unit/test_admin_accessibility.py` có một kiểm tra nền về liên kết bỏ qua điều hướng, vùng nội dung chính, vùng đang được chọn và giảm chuyển động. `tests/e2e/test_admin_web_workflows.py` có một luồng trình duyệt rộng, bao gồm đăng nhập, điều hướng, bàn phím, bot, đóng toàn bộ, cấu hình và Telegram; hiện không đặt kích thước khung nhìn điện thoại hay kiểm tra tràn ngang. Kế hoạch MB-07b ghi Task 7 còn mở vì danh sách kiểm tra trợ năng thủ công chưa hoàn tất.
- Tài liệu `docs/implementation/04-admin-telegram.md` đã yêu cầu giao diện tiếng Việt, nhãn trợ năng và thông báo lỗi tiếng Việt; thiết kế MB-07b đặt mục tiêu WCAG 2.2 AA. Các tài liệu này chưa định nghĩa cách hiển thị thích ứng trên điện thoại hoặc quy tắc dịch trạng thái.

## Cách tiếp cận đang cân nhắc

1. **Tự lưu Bootstrap 5.3 CSS trong static, trên FastAPI/Jinja hiện có — phương án đề xuất.** Dùng lưới, khoảng cách, tiện ích biểu mẫu và bảng có điểm ngắt theo độ rộng; chỉ nạp CSS từ cùng máy chủ để tương thích chính sách CSP `default-src 'self'`. Không thêm JavaScript Bootstrap nếu các màn hình không cần thành phần tương tác của framework. Giữ CSS riêng cho bố cục/điều khiển đặc thù. Cách này phù hợp mong muốn trước đây của người dùng là dùng một bộ khung CSS phổ biến và giảm lượng kiểu dáng phải tự xây. Bootstrap mô tả cách tiếp cận ưu tiên điện thoại và sáu điểm ngắt mặc định trong tài liệu chính thức.
2. **Tiếp tục CSS tự viết, bổ sung thiết kế ưu tiên điện thoại.** Không thêm thư viện phụ thuộc hay CSS bên thứ ba; có thể giữ gói nhỏ. Đổi lại, nhóm phải tự xây và duy trì lưới, biểu mẫu, bảng, điểm ngắt và trạng thái nhất quán cho mọi trang.
3. **Ứng dụng một trang (SPA) riêng.** Có thể tăng khả năng tương tác, nhưng tạo bộ công cụ giao diện và môi trường chạy mới, đồng thời phải kiểm tra lại session, CSRF, CSP, RBAC, truy cập dữ liệu và các luồng trình duyệt. Không phù hợp với phạm vi chỉ làm mới trải nghiệm các trang Jinja hiện có.

**Đề xuất để người dùng xem xét:** phương án 1, dùng Bootstrap CSS tự lưu; giữ Jinja và kết xuất HTML phía máy chủ, hạn chế JavaScript. Đây mới là đề xuất, chưa phải quyết định được phê duyệt. Nếu chọn phương án khác, cập nhật kế hoạch trước khi viết giao diện.

## Định hướng thiết kế đề xuất

- Thiết kế ưu tiên điện thoại, dùng nội dung và độ rộng thực tế để quyết định bố cục. Điều hướng hiển thị rõ, gọn trên điện thoại; không ẩn thao tác chính sau menu không thể dùng bằng bàn phím hoặc trình đọc màn hình.
- Trang tổng quan giữ đúng dữ liệu đang có và xếp các số liệu/bộ điều khiển thành khối dễ đọc một cột trên điện thoại, nhiều cột trên màn hình rộng. Không thêm PnL, biểu đồ hoặc chỉ số ngoài phạm vi hiện tại.
- Danh sách bot hiển thị mỗi bot thành khối có tên, trạng thái đã dịch và liên kết chi tiết; biểu mẫu và thao tác xếp dọc, trường nhập chiếm chiều rộng phù hợp màn hình.
- Bảng xem trước flatten chuyển thành danh sách/khối có nhãn cho từng giá trị ở màn hình hẹp; giữ bảng có caption và tiêu đề cột ở màn hình lớn. Nếu có trường hợp cần cuộn ngang, vùng cuộn phải nhận biết được và không làm cuộn toàn trang.
- Báo cáo trình bày nhãn và giá trị đã định dạng trong giao diện thay vì chỉ đưa JSON thô. JSON chỉ giữ lại như dữ liệu kỹ thuật có chủ đích nếu người dùng cần xem, với tên và cách truy cập riêng.
- Tập trung hóa nhãn tiếng Việt cho chế độ, trạng thái bot/tài khoản/không gian làm việc, mức sẵn sàng, trạng thái mở vị thế, trạng thái đóng toàn bộ và lỗi thao tác. Giữ giá trị enum/mã tiếng Anh ở tầng máy; không đưa mã thô thành nhãn duy nhất trên giao diện. Mọi lỗi hiển thị phải nêu được vấn đề và hành động khắc phục bằng tiếng Việt, đồng thời không làm lộ chi tiết nội bộ.
- Dùng font tự lưu có xác nhận bộ ký tự tiếng Việt, đề xuất Be Vietnam Pro, sau đó khai báo danh sách font dự phòng `"Be Vietnam Pro", system-ui, sans-serif`. Chỉ lấy font từ kho mã nguồn chính thức Google Fonts; trước khi đóng gói cần xác định chính xác tệp woff2, tập ký tự và độ đậm, giữ giấy phép SIL OFL và ghi thông tin tác giả/giấy phép trong repo. Không gọi font qua mạng phân phối nội dung bên ngoài vì triển khai hiện dùng CSP cùng máy chủ và cần hoạt động nhất quán khi không truy cập dịch vụ ngoài.
- Định dạng ngày giờ theo quy ước giao diện tiếng Việt đã được dự án chọn; vẫn lưu và gửi giá trị ISO/UTC theo hợp đồng máy chủ. Trước khi triển khai phải xác định rõ giờ hiển thị là UTC hay Asia/Ho_Chi_Minh và ghi nhãn múi giờ để không nhập nhằng.

## Các giai đoạn triển khai sau khi thiết kế được duyệt

### Giai đoạn 1 — Chốt quy tắc giao diện

- Chốt phương án CSS, cách điều hướng trên điện thoại, font/cách đóng gói, định dạng ngày giờ, quy ước Việt hóa trạng thái và danh sách tên riêng/mã kỹ thuật được giữ nguyên.
- Lập inventory chuỗi theo tất cả trang, lỗi, thông báo trạng thái và tên accessible; đối chiếu với glossary hiện tại, chỉ thêm thuật ngữ giao dịch/AI nếu thật sự phát sinh.

### Giai đoạn 2 — Nền bố cục và kiểu chữ

- Thay đổi `src/trading_bot/templates/admin/base.html` và `src/trading_bot/static/admin.css`; thêm tệp CSS Bootstrap có khóa phiên bản và tệp font/giấy phép nếu phương án được duyệt.
- Giữ `lang="vi"`, khung nhìn, liên kết bỏ qua điều hướng, vùng nội dung, thứ tự đề mục, vùng đang được chọn và chế độ giảm chuyển động; bố cục phải sử dụng được ở chiều rộng nhỏ trước khi mở rộng.

### Giai đoạn 3 — Việt hóa và cập nhật màn hình

- Thêm lớp trình bày/mô hình hiển thị cho nhãn đã dịch; không sửa enum lưu DB, mã API hoặc quyết định quyền.
- Rà và cập nhật các trang: `login.html`, `dashboard.html`, `bots.html`, `bot_create.html`, `bot_detail.html`, `reports.html`, `flatten.html`, `flatten_preview.html`, `flatten_operation.html`, `telegram_bindings.html`, `telegram_link_created.html`.
- Trình bày bảng đóng toàn bộ và báo cáo phù hợp màn hình hẹp; nhóm biểu mẫu và thao tác quan trọng theo trang; thêm trang/thành phần lỗi tiếng Việt cho các luồng biểu mẫu nếu hợp đồng hiện tại cho phép, giữ nguyên mã trạng thái HTTP và hành vi bảo mật.

### Giai đoạn 4 — Kiểm thử giao diện và nghiệm thu trợ năng

- Bổ sung kiểm tra nội dung tiếng Việt/siêu dữ liệu ngôn ngữ và ánh xạ trạng thái cho mọi mẫu HTML; không cho phép mã trạng thái thô xuất hiện đơn độc ở vị trí nhãn.
- Bổ sung kiểm thử E2E trên trình duyệt ở các kích thước 360×800, 390×844, 768×1024, 1024×768 và 1280×800 cho đăng nhập, bảng điều khiển, danh sách/chi tiết bot, báo cáo, phần xem trước/trạng thái đóng toàn bộ và Telegram.
- Kiểm tra không tràn ngang toàn trang; các bảng/khối dài có cách đọc phù hợp; biểu mẫu, nút, liên kết có thể thao tác bằng cảm ứng và bàn phím; lỗi/trạng thái có ngữ nghĩa trợ năng; không che vùng đang được chọn.
- Kiểm tra font thực sự tải trong trình duyệt và xem trực quan dấu tiếng Việt ở cỡ giao diện nhỏ, đặc biệt chuỗi mẫu “Tiếng Việt: ă â ê ô ơ ư đ ắ ằ ấ ầ ễ ệ”; kiểm tra font dự phòng và không cắt dấu ở các trình duyệt mục tiêu.
- Hoàn tất danh sách kiểm tra `docs/implementation/admin-web-accessibility-checklist-vi.md` trên trang/khung nhìn đại diện. Kiểm tra tự động không thay cho kiểm tra thủ công.

## Tệp dự kiến thay đổi khi có phê duyệt

- `src/trading_bot/templates/admin/base.html` và toàn bộ `src/trading_bot/templates/admin/*.html`.
- `src/trading_bot/static/admin.css`, CSS Bootstrap tự lưu ở static, font woff2 và tệp giấy phép/ghi công tương ứng.
- Một mô-đun trình bày/mô hình hiển thị trong `src/trading_bot/admin/` để ánh xạ enum và lý do lỗi sang nhãn/hướng dẫn tiếng Việt; tên mô-đun sẽ chốt khi kiểm kê mã trước khi triển khai.
- `tests/unit/test_admin_accessibility.py`, `tests/unit/test_admin_web_views.py` hoặc mô-đun unit mới chuyên kiểm tra bản địa hóa; `tests/e2e/test_admin_web_workflows.py` hoặc E2E mới cho khung nhìn thích ứng.
- `docs/implementation/admin-web-accessibility-checklist-vi.md`; sau vòng triển khai mới cập nhật `docs/implementation/implementation-memory.md` theo quy trình dự án. Chưa sửa WBS, project status hoặc memory trong kế hoạch này.

## Tiêu chí nghiệm thu đề xuất

1. Mọi trang Admin khai báo `lang="vi"`; mọi nội dung người dùng đọc được, gồm điều hướng, tiêu đề, nút, nhãn, lỗi, trạng thái, ngày giờ và tên trợ năng, là tiếng Việt tự nhiên và có dấu. Tên riêng/sản phẩm, email, URL, ID/mã băm, mã giao thức hoặc nội dung JSON có thể giữ nguyên khi cần; không được dùng enum/mã kỹ thuật tiếng Anh làm nhãn duy nhất.
2. Be Vietnam Pro được tự lưu, giấy phép và ghi công rõ ràng, trình duyệt xác nhận font đã tải; nếu font không tải thì font dự phòng vẫn hiển thị đúng dấu. Không phụ thuộc mạng phân phối nội dung bên ngoài.
3. Mọi màn hình/luồng chính hoạt động trên các kích thước khung nhìn nêu ở Giai đoạn 4, không có tràn ngang toàn trang ở chế độ dọc; nội dung dài có giải pháp đọc rõ ràng. Bố cục dùng được ở mức phóng đại 200% và chỉ bằng bàn phím.
4. Nhãn, lỗi, trạng thái, vùng đang chọn, độ tương phản, vùng nội dung, đề mục và bảng đạt danh sách kiểm tra WCAG 2.2 AA hiện có; hoàn tất cả kiểm thử tự động trên trình duyệt lẫn danh sách kiểm tra thủ công, không có mục bị bỏ qua.
5. Không thay đổi đường dẫn, mã API, vai trò/phạm vi quyền, CSRF, MFA, nhật ký kiểm toán, no-store, CSP hoặc chế độ PAPER; kiểm thử hồi quy chứng minh các bất biến này giữ nguyên.

## Quyết định còn chờ xem xét

- Chọn Bootstrap 5.3 CSS tự lưu (đề xuất) hay tiếp tục CSS tự viết.
- Chấp thuận Be Vietnam Pro tự lưu làm font chính; xác nhận phương án đóng gói tập ký tự/độ đậm và ghi công sau khi kiểm tra tệp nguồn cụ thể.
- Chốt múi giờ hiển thị mặc định cho người vận hành (UTC hay Asia/Ho_Chi_Minh).
- Chốt quy ước tiếng Việt cho trạng thái và thuật ngữ kỹ thuật; các mã máy vẫn dùng tiếng Anh phía server.
- Xác nhận phạm vi giao diện chỉ trình bày dữ liệu hiện hữu, không thêm phân tích dữ liệu hay biểu đồ trong vòng này.

## Nguồn đã kiểm tra ngày 2026-11-02

- [Bootstrap 5.3 — Breakpoints](https://getbootstrap.com/docs/5.3/layout/breakpoints/): tài liệu chính thức mô tả cách thiết kế ưu tiên điện thoại và sáu điểm ngắt mặc định (`576`, `768`, `992`, `1200`, `1400` px; mặc định nhỏ hơn `576` px).
- [Bootstrap 5.3 — Tables](https://getbootstrap.com/docs/5.3/content/tables/): tài liệu chính thức về bảng thích ứng theo độ rộng; bảng cuộn cần được áp dụng có chủ đích để không làm mất khả năng đọc trên điện thoại.
- [Bootstrap 5.3 — Accessibility](https://getbootstrap.com/docs/5.3/getting-started/accessibility/): Bootstrap không tự bảo đảm khả năng trợ năng; cấu trúc HTML, ARIA, độ tương phản và kiểm tra phải do ứng dụng xác nhận.
- [Google Fonts — Thông tin Be Vietnam Pro trong kho mã nguồn chính](https://github.com/google/fonts/blob/main/ofl/bevietnampro/METADATA.pb): thông tin trong kho chính thức `google/fonts` liệt kê tập ký tự `vietnamese` và giấy phép OFL. Đây là bằng chứng để đánh giá lựa chọn font, không phải quyết định đã chọn tệp nhị phân hay cách đóng gói.
