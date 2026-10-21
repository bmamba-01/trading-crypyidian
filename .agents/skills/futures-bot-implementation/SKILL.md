---
name: futures-bot-implementation
description: Use when researching, designing, implementing, verifying, or maintaining the independent Crypidian Binance Futures bot, its trading strategies, research ingestion, AI learning, admin web, or Telegram operations.
metadata:
  short-description: Quy trình nghiên cứu và implement bot futures
---

# Quy trình xây dựng Crypidian

Áp dụng skill này cho mọi vòng thay đổi sản phẩm hoặc mã nguồn. Mỗi vòng chỉ hoàn tất khi có bằng chứng về nguồn nghiên cứu, test, và ghi nhớ triển khai.

## Trước khi bắt đầu một vòng

1. Đọc `AGENTS.md`, `docs/implementation/implementation-memory.md`, kế hoạch tổng thể trong `docs/superpowers/plans/` và plan/module liên quan. Đối chiếu với code hiện có trước khi sửa.
2. Xác định giả định nào có thể đã đổi theo thời gian: API Binance USDⓈ-M, thư viện Python, database/queue, framework, quy định, dữ liệu thị trường và phương pháp nghiên cứu. Với các điểm đó, tra tài liệu chính thức hoặc nguồn gốc có thẩm quyền đang còn hiệu lực; ghi URL, phiên bản hoặc ngày công bố nếu có, ngày kiểm tra, và ảnh hưởng tới quyết định. Dùng bài nghiên cứu gốc/whitepaper cho phương pháp; dùng nguồn trader như ý kiến cần đối chiếu, không coi là dữ kiện đã kiểm chứng.
3. Kiểm tra công cụ phù hợp và tương thích trong repo (Python runtime, dependency lock, test runner, migration, lint/type checker, browser/E2E runner). Đề xuất cập nhật tool khi có lợi ích đo được về tương thích, độ tin cậy hoặc bảo trì; không nâng phiên bản chỉ vì có bản mới.
4. Nếu phát hiện hướng dẫn, dependency, API contract, giả định chiến lược hoặc nguồn dữ liệu trong docs đã lỗi thời, cập nhật tài liệu liên quan cùng vòng code và thêm thuật ngữ mới vào `docs/research/glossary-vi.md`.

## Trong khi implement

- Theo implementation plan; giữ Python làm ngôn ngữ dịch vụ chính và giữ bot này độc lập với Freqtrade runtime. Chỉ kế thừa ý tưởng/cấu trúc khi có lý do và hợp đồng tương thích được ghi rõ.
- Viết code identifier bằng tiếng Anh. Viết comment, docstring, hướng dẫn và giải thích trong tài liệu bằng tiếng Việt; chú giải thuật ngữ tiếng Anh bằng bản dịch/định nghĩa phù hợp trong glossary.
- Với hành vi mới/sửa đổi, viết test trước khi code theo RED → GREEN → REFACTOR. Bao gồm unit test cho logic đơn vị và E2E test đi qua mọi ranh giới quan trọng bị thay đổi trong hành trình người dùng/dịch vụ. Ưu tiên [PAPER/Demo](../../../docs/research/glossary-vi.md#c-futures-dòng-lệnh-và-quản-trị-lệnh); kiểm tra không gây lệnh live ngoài chủ đích.
- Không để AI tự phát lệnh giao dịch hoặc tự đưa model/strategy vào live. Thay đổi model/strategy cần [nguồn gốc dữ liệu-mô hình](../../../docs/research/glossary-vi.md#h-nguồn-gốc-thời-gian-và-vận-hành), đánh giá [ngoài mẫu](../../../docs/research/glossary-vi.md#e-dữ-liệu-và-chiến-lược-huấn-luyện-ai), ngưỡng rủi ro và cơ chế rollback theo thiết kế.

## Kết thúc mỗi vòng và điều kiện commit

1. Chạy toàn bộ unit test bị ảnh hưởng và E2E test bao phủ các ranh giới quan trọng mà thay đổi tác động; chạy thêm bộ regression thích hợp. Ghi chính xác command và kết quả. Nếu test hỏng/flaky, tìm nguyên nhân, sửa và chạy lại; test chưa chạy hoặc bị skip không được ghi là đạt.
2. Cập nhật `docs/implementation/implementation-memory.md`: ngày/vòng, mục tiêu, file/module đổi, nguồn mới đã kiểm tra, quyết định/giả định, lỗi và nguyên nhân gốc, biện pháp, command/kết quả unit và E2E, rủi ro còn mở, việc kế tiếp. Không ghi credential, secret hay dữ liệu cá nhân.
3. Đọc lại mục memory vừa cập nhật, đối chiếu với diff và output test; sửa nếu còn thiếu, sai hoặc không thể truy vết. Sau đó kiểm tra lại tài liệu/traceability bị ảnh hưởng.
4. Chỉ commit khi unit test và E2E đều đạt, memory đã được cập nhật và đọc kiểm chứng, diff không chứa secret, và thay đổi nằm trên nhánh làm việc được chỉ định. Không commit thay người dùng nếu thiếu bằng chứng hoặc nhánh/đích chưa rõ.

## Ngôn ngữ và nguồn

- Hướng dẫn dự án viết bằng tiếng Việt; source code dùng identifier tiếng Anh.
- Thuật ngữ chiến lược giao dịch, phân tích đồ thị và huấn luyện AI cần có EN→VI sát nghĩa, giải thích ngắn, giả định/giới hạn và hyperlink đến nguồn liên quan trong `docs/research/glossary-vi.md`.
- Ưu tiên nguồn chính thức cho API/tool; nghiên cứu gốc cho phương pháp; nguồn độc lập để đối chiếu. Ghi ngày vì thông tin về sàn, thư viện, quy định và thị trường có thể đổi.
- Không đưa nội dung có bản quyền hoặc dữ liệu không có quyền sử dụng vào bộ huấn luyện; lưu provenance và giấy phép/điều kiện truy cập cho dữ liệu ingest.

## Tài liệu cần đọc

- Ngôn ngữ và thuật ngữ: `docs/standards/language-policy.md`, `docs/research/glossary-vi.md`.
- Kiến trúc, user story và luồng: `docs/architecture/technical-design.md`, `docs/product/`.
- Thứ tự và phạm vi implement: `docs/implementation/README.md` và các plan đánh số.
- Nhật ký quyết định/vấn đề giữa các vòng: `docs/implementation/implementation-memory.md`.
