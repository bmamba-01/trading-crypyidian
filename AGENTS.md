# Hướng dẫn AI cho dự án Crypidian

- Khi thực hiện thay đổi trong dự án này, hãy đọc và áp dụng skill [futures-bot-implementation](./.agents/skills/futures-bot-implementation/SKILL.md).
- Nội dung giải thích, hướng dẫn, comment và docstring viết bằng tiếng Việt; mã định danh, tên hàm, lớp, biến và API viết bằng tiếng Anh.
- Mọi thuật ngữ giao dịch/AI mới phải được định nghĩa EN→VI và có nguồn tham khảo trong `docs/research/glossary-vi.md`.
- Trước khi code, đọc `docs/implementation/implementation-memory.md` cùng plan/module liên quan. Sau mỗi vòng implement, cập nhật rồi đọc lại memory để xác nhận lỗi, nguyên nhân, quyết định và việc còn mở đã được ghi.
- Trước mỗi commit, phải có unit test và E2E test liên quan chạy đạt. Không xem test bị bỏ qua, chưa chạy hoặc lỗi/flaky chưa điều tra là đạt.
- Trước quyết định phụ thuộc phiên bản API, thư viện, sàn hoặc công cụ, kiểm tra tài liệu hiện hành từ nguồn có thẩm quyền; lưu URL và ngày kiểm tra trong memory hoặc tài liệu nghiên cứu phù hợp.
- Không đẩy lên repository upstream của Freqtrade. Chỉ đồng bộ GitHub khi remote đích là repo người dùng sở hữu/được ủy quyền và xác thực ghi thành công.
