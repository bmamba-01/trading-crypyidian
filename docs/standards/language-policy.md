# Quy ước ngôn ngữ và chú giải

**Ngày áp dụng:** 21/10/2026 · **Phạm vi:** source code, schema, API, log, giao diện, tài liệu, comment và plan.

## 1. Quy tắc ngôn ngữ

| Thành phần | Ngôn ngữ | Quy định |
| --- | --- | --- |
| Tên repository, package, module, file code, class, function, biến, enum, API field, event type, DB column | Tiếng Anh | Dùng tên ngắn, nhất quán với thuật ngữ ngành và style của Python/HTTP/SQL. Không viết identifier bằng tiếng Việt không dấu. |
| Comment trong code, docstring, TODO/FIXME giải thích lý do | Tiếng Việt | Nêu lý do nghiệp vụ, bất biến, rủi ro hoặc hành vi khó suy ra. Không dịch lại dòng code hiển nhiên. |
| README, hướng dẫn setup, runbook, API/how-to, thiết kế, kế hoạch, ADR và acceptance criteria | Tiếng Việt | Giữ nguyên tên API, class, field, package, lệnh CLI và thuật ngữ gốc trong backtick; giải thích tiếng Việt ngay lần xuất hiện đầu. |
| UI, Telegram message, lỗi hướng người vận hành | Tiếng Việt | Dùng từ dễ hiểu; có thể thêm mã lỗi hoặc thuật ngữ tiếng Anh trong ngoặc nếu giúp tra cứu. |
| Tên metric/log/event và payload máy đọc | Tiếng Anh | Dùng tên ổn định, có thể truy vấn; phần mô tả trong tài liệu và dashboard là tiếng Việt. |
| Tên strategy, model, feature, experiment | Tiếng Anh hoặc mã trung tính | Phần cấu hình máy giữ nguyên; dashboard và registry hiển thị tên tiếng Việt/mô tả, công thức, giả thuyết và link tài liệu. |

## 2. Quy tắc viết comment/docstring

- Comment và docstring phải viết tiếng Việt có dấu. Chỉ giữ nguyên tiếng Anh cho API/identifier, câu lệnh, tên phương pháp và trích dẫn.
- Viết ngắn gọn, tập trung vào **vì sao**, giả định, giới hạn, đơn vị, timezone hoặc hành vi lỗi; để code thể hiện **làm gì**.
- Với công thức hoặc logic trading, ghi rõ đơn vị, thời điểm dữ liệu, dữ liệu đã đóng hay chưa, trường hợp biên và link tới định nghĩa/glossary khi không hiển nhiên.
- Không ghi bí mật, dữ liệu tài khoản, token hoặc nội dung private vào comment, docstring, ví dụ cấu hình, log hay tài liệu.
- Ví dụ:

```python
def calculate_position_size(account_equity: Decimal, stop_distance: Decimal) -> Decimal:
    """Tính khối lượng theo ngân sách rủi ro và khoảng cách đến điểm dừng lỗ."""
    # Từ chối dữ liệu không hợp lệ để tránh tạo khối lượng vô hạn khi stop_distance bằng 0.
    if stop_distance <= 0:
        raise ValueError("Khoảng cách dừng lỗ phải lớn hơn 0")
```

## 3. Chú giải thuật ngữ trong tài liệu/UI

1. Ghi lần đầu theo mẫu **Bản dịch tiếng Việt (English term, `identifier`)**; ví dụ “trượt giá (slippage)” hoặc “kiểm định cuốn chiếu theo thời gian (walk-forward validation)”.
2. Kèm một câu định nghĩa tiếng Việt. Nếu có ảnh hưởng tới giao dịch, ghi thêm cách dùng trong hệ thống và điều thuật ngữ **không** khẳng định.
3. Hyperlink thuật ngữ đến mục glossary có anchor; từ glossary dẫn tới tài liệu dự án liên quan và nguồn bên ngoài đáng tin cậy.
4. Với thuật ngữ có nhiều cách dịch, glossary ghi bản dịch ưu tiên, biến thể được chấp nhận và lý do chọn; không âm thầm đổi từ giữa các tài liệu.
5. Không dịch tên phương pháp/hàm chuẩn nếu dịch sẽ làm mất khả năng tìm tài liệu, nhưng luôn giải thích ngay bằng tiếng Việt.

## 4. Chọn nguồn thuật ngữ

- Dùng tài liệu sàn cho hợp đồng, phí, funding, giá đánh dấu, margin và trạng thái lệnh; tài liệu Freqtrade chỉ dùng để đối chiếu khái niệm engine/strategy, không ngụ ý bot độc lập dùng runtime đó.
- Dùng tài liệu framework chính thức cho cách đánh giá mô hình; dùng bài báo gốc hoặc kho học thuật cho phương pháp nghiên cứu. Ghi rõ preprint/working paper khi chưa phản biện.
- Các thuật ngữ khuyến nghị cho sản phẩm nằm trong [glossary song ngữ](../research/glossary-vi.md); định nghĩa cụ thể của domain/runtime nằm trong [thiết kế kỹ thuật](../architecture/technical-design.md).
