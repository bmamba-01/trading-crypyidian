# Tài liệu dự án Crypidian

Tài liệu sản phẩm, nghiên cứu và xây dựng hệ thống được quản lý tập trung tại thư mục gốc `docs/`. Nội dung hướng đến người vận hành và đội phát triển được viết bằng tiếng Việt. Tên file/mã định danh kỹ thuật dùng tiếng Anh theo [quy ước ngôn ngữ](./standards/language-policy.md).

## Cấu trúc thư mục chuẩn

```text
docs/
├── README.md                         # Mục lục và quy ước tìm tài liệu
├── standards/
│   └── language-policy.md            # Quy ước tiếng Anh trong code, tiếng Việt trong giải thích
├── product/
│   ├── use-cases.md                  # Danh mục và luồng use case
│   ├── sequence-diagrams.md          # Sequence diagram
│   └── user-stories.md               # User story và acceptance criteria
├── research/
│   ├── glossary-vi.md                # Thuật ngữ EN→VI, định nghĩa và nguồn
│   ├── strategies-2026.md            # Giả thuyết chiến lược và nghiên cứu 2026
│   ├── macro-market-data.md          # Chỉ số vĩ mô, funding, OI và nguồn dữ liệu
│   ├── execution-and-ingestion-2026.md # Execution, quyền và phương thức thu thập
│   ├── trader-source-directory.md    # Danh mục nguồn/trader được rà soát
│   ├── trader-source-registry.csv    # Bản ghi máy đọc được
│   ├── crypto-traders-notes.md       # Ghi chú nguồn crypto
│   ├── crypto-traders.csv            # Registry crypto
│   ├── global-traders-notes.md       # Ghi chú nguồn thị trường toàn cầu
│   └── global-traders.csv            # Registry toàn cầu
├── architecture/
│   └── technical-design.md           # Stack, service boundary, DB, API, risk và security
├── implementation/
│   ├── README.md                     # Bản đồ kế hoạch theo work package
│   ├── 01-foundation-database.md
│   ├── 02-trading-core-paper.md
│   ├── 03-binance-execution.md
│   ├── 04-admin-telegram.md
│   ├── 05-research-ingestion.md
│   ├── 06-ai-learning.md
│   └── 07-operations-release.md
└── superpowers/
    ├── specs/                        # Đặc tả gốc đã được review
    └── plans/                        # Kế hoạch điều phối tổng thể
```

## Đọc theo thứ tự

1. [Thiết kế tổng thể](./superpowers/specs/2026-10-07-independent-binance-futures-bot-design.md)
2. [Use case](./product/use-cases.md) → [sequence diagram](./product/sequence-diagrams.md) → [user story/tiêu chí nghiệm thu](./product/user-stories.md)
3. [Thuật ngữ song ngữ](./research/glossary-vi.md) và [nghiên cứu strategy 2026](./research/strategies-2026.md)
4. [Thiết kế kỹ thuật](./architecture/technical-design.md)
5. [Bản đồ implementation](./implementation/README.md) → [kế hoạch tổng](./superpowers/plans/2026-10-21-independent-binance-futures-bot.md) → các plan theo module

## Nguồn chuẩn và cập nhật

- Use case/story mô tả sản phẩm cần làm; technical design mô tả ranh giới và hợp đồng; implementation plan phân rã thành đầu việc. Khi khác nhau, ghi quyết định thay đổi vào technical design và cập nhật traceability.
- Nghiên cứu bên ngoài phải có URL, ngày truy cập/công bố nếu xác định được, loại bằng chứng và giới hạn áp dụng. Không biến một bài nghiên cứu hoặc ý kiến trader thành cam kết lợi nhuận.
- Bất kỳ thuật ngữ chiến lược/AI nào xuất hiện trong tài liệu hoặc UI đều phải có bản dịch và định nghĩa trong [glossary](./research/glossary-vi.md), hoặc được bổ sung cùng thay đổi đó.
- Tài liệu kỹ thuật cập nhật phiên bản thư viện cần ghi ngày xác minh; trước khi code phải kiểm tra lại compatibility và pin dependency.
- Quy trình bắt buộc cho AI khi research/implement nằm trong [skill dự án](../.agents/skills/futures-bot-implementation/SKILL.md) và [AGENTS.md ở gốc](../AGENTS.md); skill yêu cầu đọc/cập nhật/đọc lại [implementation memory](./implementation/implementation-memory.md), đồng thời có unit và E2E test đạt trước commit.
