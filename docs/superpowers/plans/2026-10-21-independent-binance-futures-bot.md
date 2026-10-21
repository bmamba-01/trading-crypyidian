# Kế hoạch tổng thể: Bot Binance Futures độc lập

**Mục tiêu:** xây bot futures Binance USDⓈ-M độc lập, ưu tiên Python, đi từ PAPER có thể tái lập → Demo → chỉ xem xét LIVE sau một lần phê duyệt readiness riêng.

**Kiến trúc:** ứng dụng Python module hóa; FastAPI/Jinja2 admin; domain contract cho strategy/risk/execution; PostgreSQL transaction ledger/outbox; Binance Execution Gateway tách biệt; workers thu thập dữ liệu và trainer không có secret sàn. Chi tiết tại [thiết kế kỹ thuật](../../architecture/technical-design.md).

**Stack:** CPython 3.14.x, FastAPI/Pydantic, Jinja2/HTMX, SQLAlchemy async/psycopg 3/Alembic, PostgreSQL 18, `asyncio`, Binance SDK adapter + HTTPX giới hạn, NumPy/pandas/scikit-learn, PyArrow/Parquet/DuckDB, object store tương thích S3, Docker Compose. Đây là phiên bản tham khảo tại 21/10/2026; phải kiểm tra compatibility và khóa dependency khi bắt đầu code.

**Đặc tả:** [thiết kế tổng thể](../specs/2026-10-07-independent-binance-futures-bot-design.md), [use cases](../../product/use-cases.md), [sequence diagrams](../../product/sequence-diagrams.md), [user stories](../../product/user-stories.md), [glossary](../../research/glossary-vi.md).

**Ràng buộc chung:** không fork Freqtrade thành runtime sản phẩm; PAPER là mặc định, Demo/Live tách account/config. LLM/trainer không gửi lệnh, không vượt RiskEngine và không tự promote LIVE. Ghi intent trước side effect; reconcile UNKNOWN trước retry. Giữ quyền nguồn và dữ liệu point-in-time. Secret không vào DB/log/prompt. Pause entries không dừng protective orders hoặc reconciliation.

**Trọng tâm review:** endpoint/sai khác Binance SDK; state machine order, partial-fill protection và reconcile; concurrency của risk reservation; dữ liệu point-in-time; tenant/function authorization; cách ly secret; data rights/retention; leakage/promotion gate; khả năng khôi phục và vận hành.

## Quy tắc thực hiện

- Thực hiện theo thứ tự plan 00–06 trong [bản đồ implementation](../../implementation/README.md); các module chỉ chạy song song sau khi interface/schema liên quan ổn định.
- Identifier trong code bằng tiếng Anh. Comment/docstring, README, runbook, hướng dẫn, thiết kế và mô tả tiêu chí nghiệm thu viết bằng tiếng Việt theo [language policy](../../standards/language-policy.md).
- Mỗi thuật ngữ chiến lược/AI mới phải có bản dịch, định nghĩa, nguồn/link và cách dùng trong hệ thống ở [glossary](../../research/glossary-vi.md).
- Mỗi task trong plan module có file dự kiến, phụ thuộc, đầu ra, acceptance và bước verification để chạy **khi triển khai task**. Tài liệu hiện tại chỉ là kế hoạch; chưa có implementation/test nào được tuyên bố hoàn thành.
- LIVE không được bật theo lịch hoặc do AI tự quyết. Chỉ cân nhắc sau Demo prospective, khôi phục, security review, quyền nguồn và phê duyệt owner/risk độc lập.

## Giai đoạn 0 — Nền tảng và database

**Plan chi tiết:** [01-foundation-database.md](../../implementation/01-foundation-database.md)

- Chuẩn hóa Python package, lock dependency, quy ước ngôn ngữ, Docker Compose và process roles.
- Tạo PostgreSQL/Alembic schema, tenant scope, identity/RBAC/MFA, secret provider, audit và transactional outbox.
- Cổng nghiệm thu: DB dựng mới/nâng cấp được; endpoint enforce tenant/function permissions; secrets được che; queue retry không lặp side effect.

## Giai đoạn 1 — Trading core và PAPER

**Plan chi tiết:** [02-trading-core-paper.md](../../implementation/02-trading-core-paper.md)

- Thu thập/chuẩn hóa market data có `event_at` và `available_at`; feature snapshot bất biến.
- Implement strategy/chart candidate, planner, hard RiskEngine, paper fill/position monitor và ledger.
- Cổng nghiệm thu: vertical slice PAPER có replay tái lập, chi phí, exit recipe, audit và protection logic; không cần credential sàn.

## Giai đoạn 2 — Binance Demo execution

**Plan chi tiết:** [03-binance-execution.md](../../implementation/03-binance-execution.md)

- Kiểm kê SDK/API capability; viết gateway adapter, intent/idempotency, UNKNOWN reconcile, user stream và stop protection.
- Cổng nghiệm thu: Demo contract run xử lý accepted/rejected/partial/ambiguous order, restart/reconnect không duplicate; không chuyển endpoint/credential sang LIVE tự động.

## Giai đoạn 3 — Admin web và Telegram

**Plan chi tiết:** [04-admin-telegram.md](../../implementation/04-admin-telegram.md)

- Xây API/UI tiếng Việt, config version/approval, roles, audit, dashboard, bind Telegram, webhook, command allowlist và notifier.
- Cổng nghiệm thu: mọi endpoint kiểm quyền phía server; duplicate update không lặp action; secrets không hiển thị; hướng dẫn vận hành bằng tiếng Việt.

## Giai đoạn 4 — Nguồn và phân tích thông tin

**Plan chi tiết:** [05-research-ingestion.md](../../implementation/05-research-ingestion.md)

- Source registry/quyền/quota; market/macro news, event extraction, trending/attention và trader opinions có provenance.
- Cổng nghiệm thu: dữ liệu có timestamp point-in-time, revision/dedup/source rights; thiếu coverage được đánh dấu degraded; opinion chưa audit không được gắn nhãn performance.

## Giai đoạn 5 — AI learning có kiểm soát

**Plan chi tiết:** [06-ai-learning.md](../../implementation/06-ai-learning.md)

- Bounded scorer/abstain; DSL giới hạn; label theo exit recipe; dataset manifest; trainer temporal; registry, paper promotion, drift và rollback.
- Cổng nghiệm thu: run tái lập theo manifest; holdout được khóa; leakage/promotion failure có lý do; model không có quyền gửi lệnh hoặc tự promote LIVE.

## Giai đoạn 6 — Vận hành, phục hồi và readiness

**Plan chi tiết:** [07-operations-release.md](../../implementation/07-operations-release.md)

- Logs/metrics/tracing, breaker/incident drills, backup/restore, security/supply-chain và Demo readiness dossier.
- Cổng nghiệm thu: restore drill có số liệu RPO/RTO; mọi incident trọng yếu có runbook đã diễn tập; LIVE tiếp tục khóa cho tới khi phê duyệt riêng.

## Định nghĩa hoàn tất chương trình

- R0 PAPER chạy end-to-end, replay được và qua safety gates.
- Demo reconcile chính xác order/fill/position trong tình huống thường và lỗi; không có entry trùng.
- Admin/Telegram enforce role và lưu audit; source ingestion point-in-time và có quyền sử dụng.
- Challenger được kiểm định theo thời gian, có costs, lineage và rollback; không tự promote LIVE.
- Backup khôi phục được, incident có runbook đã diễn tập, secret được cách ly, rủi ro còn mở được ghi rõ.
