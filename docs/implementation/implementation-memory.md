# Ghi nhớ triển khai

Tệp này là bộ nhớ bền vững của các vòng implement. Trước mỗi vòng phải đọc toàn bộ phần liên quan; sau vòng phải ghi nhật ký, rồi đọc lại mục vừa ghi để xác minh. Lưu quyết định, lỗi và bài học có ích cho vòng sau; không ghi secret, credential, dữ liệu cá nhân hoặc thông tin tài khoản sàn.

## Trạng thái ban đầu

- **Ngày lập:** 2026-10-21.
- **Phạm vi:** mới bổ sung quy trình skill, ngôn ngữ và kế hoạch; chưa có vòng implement mã nguồn nào được xác nhận trong bản ghi này.
- **Repo/Git:** workspace gốc `C:\Works\Trading-Crypidian` hiện chưa là Git repository. `freqtrade-develop` là bản Freqtrade upstream, remote `origin` trỏ `https://github.com/freqtrade/freqtrade.git`; không dùng remote này để đẩy sản phẩm độc lập. Thư mục `freqtrade` chỉ có `.git` dang dở, nhánh hiện tại hỏng/chưa có commit và origin cũng trỏ upstream. `gh auth status` báo credential của `konglee2401-hub` không hợp lệ.
- **Hệ quả:** chưa thể an toàn đồng bộ tài liệu lên GitHub `main`, tạo nhánh `dev` từ repo dự án, hoặc commit. Cần khôi phục xác thực và xác nhận/thiết lập repo đích do người dùng sở hữu trước bước push. Không sửa/push upstream công khai.
- **Kiểm tra tài liệu:** đã kiểm tra liên kết Markdown nội bộ và code fence ở vòng trước; chưa xác nhận fragment/anchor tự động.
- **Test:** chưa có mã nguồn sản phẩm; unit/E2E test cho sản phẩm chưa chạy.

## Nhật ký các vòng

### 2026-10-21 — Chuẩn hóa skill nghiên cứu và implement

- **Mục tiêu và phạm vi:** thêm hướng dẫn bền vững để AI tra cứu nguồn/tool cập nhật, dùng đúng ngôn ngữ, ghi memory sau mỗi vòng, và yêu cầu unit + E2E trước commit.
- **Plan/story liên quan:** chuẩn hóa workflow trước khi bắt đầu các plan đánh số; chưa thay đổi mã nguồn sản phẩm.
- **Nguồn/tài liệu hiện hành đã xác minh:** không có quyết định phụ thuộc API/thị trường trong vòng tài liệu này; không cần tra nguồn live. Đã đọc hướng dẫn nội bộ skill creator và quy trình test skill tại `C:\Users\Dell\.codex\plugins\cache\openai-curated-remote\superpowers\6.4.2\skills\writing-skills\testing-skills-with-subagents.md`.
- **Tool/dependency đã kiểm tra:** `git`/remote/status và `gh auth status`. Workspace gốc chưa là repo; `freqtrade-develop` remote trỏ upstream Freqtrade; thư mục `freqtrade` có Git metadata không hoàn chỉnh; GitHub CLI credential không hợp lệ. Không thay đổi remote hoặc nâng dependency.
- **Files/modules thay đổi:** `AGENTS.md`; `.agents/skills/futures-bot-implementation/SKILL.md`; `docs/README.md`; `docs/implementation/README.md`; tệp memory này.
- **Quyết định và giả định:** skill dự án nằm trong `.agents/skills/` và được chỉ dẫn từ `AGENTS.md`. Không push lên upstream Freqtrade. Chỉ đồng bộ `main`/tạo `dev` khi có repo đích do người dùng sở hữu/được ủy quyền và xác thực GitHub hoạt động.
- **Vấn đề phát sinh và nguyên nhân gốc:** không thể fork/push hoặc checkout `dev` an toàn vì repo gốc workspace chưa khởi tạo, repo clone có remote upstream công khai, thư mục `freqtrade` đang ở nhánh hỏng và `gh` token đã hết hiệu lực.
- **Cách xử lý/bài học cho lần sau:** trước triển khai, cần xác thực GitHub lại và làm rõ repo đích cá nhân. Tình huống áp lực baseline cho thấy các cách bỏ qua phổ biến là viện deadline để không tra API, trì hoãn cập nhật memory, hoặc bỏ E2E flaky; skill đã quy định rõ các điểm này. Rà soát sau đó làm rõ E2E phải bao phủ mọi ranh giới quan trọng bị tác động.
- **Unit test:** không áp dụng cho vòng chỉ sửa tài liệu/skill; kiểm tra cấu trúc tài liệu đã đạt: `PASS: 26 Markdown files; local link targets and code fences; skill frontmatter and glossary targets.`
- **E2E test:** không áp dụng cho sản phẩm vì chưa có mã nguồn sản phẩm trong workspace gốc; thay bằng tình huống kiểm chứng skill trước/sau, gồm deadline + API có thể đổi + E2E flaky, E2E runner không khả dụng, và đề xuất tự đưa model vào LIVE. Kết quả: yêu cầu tra nguồn, không commit khi E2E chưa chạy, và cấm tự promotion LIVE đều được nhận diện.
- **Giới hạn kiểm tra hyperlink:** lần thử kiểm tra anchor toàn bộ bằng thuật toán slug tự viết báo nhiều anchor không khớp; quy tắc slug chưa tương thích chắc chắn với GitHub Markdown nên không kết luận đó là liên kết hỏng. Đã xác nhận đường dẫn file cục bộ và ba anchor glossary mới dùng trong skill; cần xác minh anchor các tài liệu cũ bằng renderer/slug chuẩn trước khi đồng bộ tài liệu.
- **Regression/security/data checks:** xác nhận không thêm credential/secrets; chưa có lệnh trade hay dữ liệu mới.
- **Việc còn mở/rủi ro:** xác thực GitHub; thiết lập remote riêng; đồng bộ toàn bộ `docs/` lên `main`; tạo/checkout `dev`; bổ sung kiểm tra anchor Markdown tự động; sau đó mới bắt đầu implementation.
- **Commit/nhánh:** chưa commit; chưa có nhánh dự án hợp lệ.
- **Đã đọc lại mục ghi nhớ sau cập nhật:** có; đối chiếu với kết quả kiểm tra cấu trúc (26 Markdown), ba tình huống kiểm tra skill, trạng thái Git/gh và giới hạn kiểm tra anchor.

## Mẫu ghi cho mỗi vòng

### YYYY-MM-DD — Tên vòng / mã story

- **Mục tiêu và phạm vi:**
- **Plan/story liên quan:**
- **Nguồn/tài liệu hiện hành đã xác minh:** ghi URL, nguồn/tổ chức, ngày xuất bản hoặc phiên bản nếu có, ngày kiểm tra, và nội dung làm thay đổi quyết định.
- **Tool/dependency đã kiểm tra:** runtime, phiên bản và lý do giữ/nâng/thay.
- **Files/modules thay đổi:**
- **Quyết định và giả định:**
- **Vấn đề phát sinh và nguyên nhân gốc:**
- **Cách xử lý/bài học cho lần sau:**
- **Unit test:** command chính xác, số lượng/kết quả, pass/fail/skip.
- **E2E test:** command chính xác, luồng được xác nhận, kết quả và môi trường.
- **Regression/security/data checks:**
- **Việc còn mở/rủi ro:**
- **Commit/nhánh:** chỉ ghi sau khi đã thực hiện; không ghi credential.
- **Đã đọc lại mục ghi nhớ sau cập nhật:** có/không; ghi điểm đối chiếu.
