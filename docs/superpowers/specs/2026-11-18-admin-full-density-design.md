# Thiết kế: thu gọn nút và nội dung trên toàn Admin

**Ngày:** 18/11/2026
**Mục tiêu:** giảm kích thước thị giác của các nút, ưu tiên icon cho thao tác ngắn/lặp lại, và giúp các khối dữ liệu phụ dài có thể thu gọn trên toàn Admin Web.

## Tiêu chí

- Mọi trang dùng chung kích thước nút gọn trên desktop; vùng tương tác vẫn tối thiểu 44×44px ở màn cảm ứng.
- Các thao tác ngắn/lặp lại như lọc, tìm, phóng to/thu nhỏ, về đầu/Live, vẽ và đóng dùng SVG line icon; `aria-label` cung cấp tên thao tác và `title` bổ sung tooltip.
- Giữ nhãn chữ hiển thị cho lệnh có hệ quả vận hành (dừng bot, công bố cấu hình, xóa, chạy quét ghi log) và hành động cần phân biệt.
- Nội dung phụ dài (lịch sử, nguồn kỹ thuật, chẩn đoán, danh sách loại/breakdown) dùng disclosure native có summary mô tả và số lượng nếu có.
- Không thu gọn mặc định form có thể thay đổi trạng thái, giới hạn rủi ro, quyền, trạng thái hiện hành hoặc thông tin cần thiết để quyết định.
- Giữ API, quyền, `method`, `action`, tên/giá trị trường, query, logic giao dịch và dữ liệu đang chọn.
- Mỗi page Admin được kiểm trong audit matrix; page đã có tab/disclosure phù hợp được giữ nguyên và ghi rõ lý do.

## Phương án

1. **Lớp mật độ dùng chung + disclosure đúng ngữ cảnh:** giảm kích thước nút theo một chuẩn, giữ 44px cho cảm ứng; chỉ icon hóa hành động lặp lại; gập các phần phụ dài. Chọn vì sửa nguyên nhân chung nhưng không giấu thao tác quan trọng.
2. Chỉ thêm nút “thu gọn tất cả”: giảm chiều dài nhưng vẫn giữ nút lớn và khiến trạng thái các phần khó tìm.
3. Thu nhỏ mọi nút xuống mọi viewport và ẩn nhãn thao tác: tiết kiệm chỗ nhưng làm hitbox và ý nghĩa lệnh kém rõ trên điện thoại.

## UI/UX Pro Max

- Tìm `compact icon button accessible tooltip`: icon phải giữ tên truy cập và trạng thái; dùng nét SVG cùng phong cách hiện hành.
- Tìm `dashboard dense content collapsible sections`: cắt/truncate phải có đường mở rộng; không để nội dung tràn hoặc mất.
- Tìm `html css icon button accessible responsive`: icon-only có `aria-label`, có focus ring và vùng chạm nhất quán.
- Lệnh thao tác rủi ro giữ văn bản hiển thị. Không thêm thư viện icon.

## Ma trận rà soát từng trang

Kiểm kê từ 32 template được render trực tiếp trong Admin. Base layout và 6 partial/macro dùng chung không tính là trang.

| Template trang | Nội dung dài / mật độ | Quyết định |
| --- | --- | --- |
| `dashboard.html` | KPI và nhiều bảng, đã có 4 tab | Giữ tab; nút dùng kích thước gọn chung |
| `bots.html` | Roster và bộ lọc | Giữ bộ lọc và roster; nút gọn chung |
| `bot_create.html` | Form cấu hình bot | Giữ nguyên form, nhãn và hành động rõ |
| `bot_detail.html` | Nhiều khối vận hành, đã chia tab | Giữ tab; lệnh pause/resume/publish giữ chữ |
| `reports.html` | KPI, biểu đồ, lịch sử, đối soát; đã chia tab | Giữ dữ liệu cốt lõi/tab; action lọc đổi sang icon có tên truy cập |
| `market_chart.html` | Toolbar, vẽ, indicator và lớp | Icon hóa công cụ ngắn/lặp lại; giữ aria-label, title, state |
| `market_scanner.html` | Ba bảng kết quả và các vùng phụ | Giữ ba bảng; thu gọn watchlist, mã loại, nhật ký |
| `live.html` | Sức khỏe, vốn, bot, vị thế, feed | Giữ panel hiện trạng; thu gọn feed dài |
| `risk_appetite.html` | Form giới hạn, hiệu lực bot, ví dụ SL/TP, lịch sử | Giữ form và trạng thái rủi ro; thu gọn lịch sử phiên bản |
| `economic_calendar.html` | Form lọc và lịch sự kiện | Giữ form và danh sách; áp dụng nút gọn chung |
| `flatten.html` | Lệnh đóng vị thế khẩn cấp | Giữ chữ, xác nhận và tóm tắt rủi ro |
| `flatten_preview.html` | Bản xem trước lệnh đóng | Giữ checklist và nút xác nhận rõ |
| `flatten_operation.html` | Kết quả thao tác đóng | Giữ kết quả và liên kết tiếp theo |
| `login.html` | Form đăng nhập | Giữ nhãn và vùng bấm đủ lớn |
| `error.html` | Thông báo lỗi / quyền | Giữ nguyên nội dung, áp dụng kích thước theo viewport |
| `ai_analyses.html` | Danh sách lượt AI và bộ lọc | Icon hóa nút lọc; giữ bảng và tóm tắt |
| `ai_analysis_detail.html` | Nhận định, giả thuyết, đề xuất, backtest, context | Giữ nội dung phân tích; thu gọn backtest và context |
| `run_report.html` | Tóm tắt, bot, cảnh báo và lượt AI liên quan | Giữ report/cảnh báo; thu gọn danh sách lượt AI liên quan |
| `signal_outcomes.html` | KPI, phân phối và nhiều bảng breakdown | Giữ filter/KPI/phân phối; thu gọn breakdown và tín hiệu gần nhất |
| `backtest_replays.html` | Form và kết quả replay | Giữ form/kết quả chính; nhận kích thước nút chung |
| `strategy_cards.html` | Danh sách thẻ chiến lược và thao tác | Giữ thao tác có hệ quả ở dạng chữ; nhận nút gọn chung |
| `microscope_replay.html` | Biểu đồ replay và dữ liệu phân tích chuyên sâu | Giữ công cụ chart/kết quả; nhận nút gọn chung |
| `research_sources.html` | Nguồn, phiên bản, quyền và chi tiết | Dùng disclosures hiện có; không gập form/chính sách cần đọc |
| `learning_model_registry.html` | Registry mô hình và artifacts | Giữ disclosures hiện tại; nút gọn chung |
| `learning_observations.html` | Quan sát và nguồn dữ liệu | Giữ disclosure / phân trang hiện tại |
| `learning_paper_performance.html` | KPI và bảng hiệu năng | Giữ báo cáo chính; nhận nút gọn chung |
| `learning_scoring.html` | Cấu hình/chấm điểm và phân tích | Giữ điều khiển chính; giữ cách thu gọn hiện có |
| `learning_splits.html` | Duyệt chia tập dữ liệu | Giữ trạng thái/quyết định duyệt hiển thị |
| `learning_training.html` | Điều phối huấn luyện, artifact, quyền | Giữ thao tác vận hành và chi tiết cần phê duyệt |
| `metric_cards.html` | Chỉ số và giải thích metric | Giữ ngữ cảnh; nút nhận chuẩn mật độ chung |
| `telegram_bindings.html` | Thiết lập kết nối Telegram | Giữ form và hành động kết nối ở dạng chữ |
| `telegram_link_created.html` | Kết quả liên kết Telegram | Giữ hướng dẫn và trạng thái, không gập |

Ngoài các disclosure mới, những thay đổi chung ở nút và input/select áp dụng đồng bộ cho cả 32 trang qua stylesheet của base layout. Không thêm icon-only cho lệnh vận hành/rủi ro.

## Ràng buộc

- Dùng Jinja, SVG inline, CSS và disclosure native; không dependency mới.
- Điểm mở mặc định chỉ áp dụng cho thông tin thứ cấp; summary nêu tên và trạng thái thay vì icon/màu trơ.
- Tương tác bàn phím hoạt động tự nhiên, focus dễ thấy, responsive 360–1280px.
