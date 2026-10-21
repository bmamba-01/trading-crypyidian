# Bản đồ kế hoạch triển khai

Thư mục này là điểm vào ở gốc `docs/` cho kế hoạch xây dựng từng phần. [Kế hoạch tổng thể](../superpowers/plans/2026-10-21-independent-binance-futures-bot.md) giữ thứ tự phụ thuộc giữa các giai đoạn; các tài liệu dưới đây phân rã đầu việc theo module để giao việc và theo dõi nghiệm thu.

Dependency trực tiếp, phiên bản khóa và giấy phép được theo dõi trong [dependency inventory](./dependency-inventory.md).

## Thứ tự và phụ thuộc

| Thứ tự | Plan | Phạm vi | Phụ thuộc | Stories chính |
| --- | --- | --- | --- | --- |
| 0 | [Nền tảng và database](./01-foundation-database.md) | Quy ước, package skeleton, Docker, DB schema/migrations, tenant isolation, auth/RBAC, secrets, audit/outbox | Không | US-01…07, US-24 |
| 1 | [Trading core và paper](./02-trading-core-paper.md) | Market data point-in-time, features, strategy/chart rules, RiskEngine, paper execution, ledger | Plan 0 | US-08, US-10, US-14…20, US-22, US-24 |
| 2 | [Binance execution](./03-binance-execution.md) | USDⓈ-M adapter, order lifecycle, idempotency, protective orders, reconcile, Demo | Plan 0 và vertical slice paper | US-02, US-16…20 |
| 3 | [Admin web và Telegram](./04-admin-telegram.md) | Admin UI/API, role, config approval, Telegram bind/webhook/commands/notifications | Plan 0 | US-01…07, US-21, US-22, US-24, US-25, US-28 |
| 4 | [Research ingestion](./05-research-ingestion.md) | Source rights, official data, news/macro, attention/trending, trader opinion, review | Plan 0; market/core contract ổn định để nối feature | US-09…13, US-23 |
| 5 | [AI learning](./06-ai-learning.md) | DSL, labels/datasets, scorer, trainer, evaluation, registry, drift, promotion/rollback | Plan 0, paper ledger và data lineage từ Plan 1; optional research features từ Plan 4 | US-15, US-23, US-26…29 |
| 6 | [Operations và release](./07-operations-release.md) | Observability, bảo mật, backup/restore, incident, Demo readiness, Live gate | Các plan 0–5 | US-24, US-28, US-29 |

## Quy tắc thực hiện

- Triển khai code theo tiếng Anh; comment/docstring, README, runbook, hướng dẫn và thông báo cho người vận hành viết tiếng Việt theo [language policy](../standards/language-policy.md).
- Tên chiến lược/feature/model có thể giữ English identifier, nhưng registry/UI phải có tên tiếng Việt, công thức, đơn vị, giả định, điểm vô hiệu và link đến [glossary](../research/glossary-vi.md).
- Mỗi plan nêu rõ file/module dự kiến, đầu việc, phụ thuộc, đầu ra, tiêu chí nghiệm thu và bước kiểm tra cần chạy **khi thực thi plan**. Chưa có code thì không coi các bước kiểm tra tương lai là đã chạy.
- Không bắt đầu Binance LIVE theo lịch tự động. Chỉ xem xét sau Demo có bằng chứng prospective, kiểm tra khôi phục, security review, nguồn/giấy phép và phê duyệt owner/risk riêng.

## Tạo thay đổi mới

Mỗi thay đổi cần cập nhật tối thiểu những tài liệu bị ảnh hưởng trong chuỗi: use case → story/AC → sequence → technical contract/schema → module plan → glossary nếu xuất hiện thuật ngữ mới. Liên kết gốc và traceability được kiểm tra trước khi xem thiết kế hoàn tất.

Trước mỗi vòng, đọc [ghi nhớ triển khai](./implementation-memory.md); sau mỗi vòng, cập nhật và đọc kiểm chứng lại. Yêu cầu về nghiên cứu nguồn hiện hành, lựa chọn tool, unit/E2E test và điều kiện commit được quy định trong [skill dự án](../../.agents/skills/futures-bot-implementation/SKILL.md).
