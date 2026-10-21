# Plan 01 — Nền tảng và database

**Mục tiêu:** tạo nền Python, môi trường chạy và lớp dữ liệu tenant-safe để các module giao dịch, admin, ingest và AI dùng chung hợp đồng có version. \
**Phụ thuộc:** không. \
**Stories:** US-01…07, US-24. \
**Tài liệu:** [thiết kế kỹ thuật](../architecture/technical-design.md), [use case](../product/use-cases.md), [glossary](../research/glossary-vi.md), [quy ước ngôn ngữ](../standards/language-policy.md).

## WP-01A — Quy ước repo, ngôn ngữ và dependency

**Đầu ra:** package Python, dependency lock có hash, chuẩn format/type, quy ước docstring/comment và cấu trúc process.

1. Kiểm tra repository root, `AGENTS.md`, file hiện hữu và worktree trước khi tạo file; không ghi đè dữ liệu người dùng.
2. Tạo `pyproject.toml`, lock file đã chọn, `.python-version`, `src/trading_bot/`, cấu hình formatter/linter/type checker và `.env.example` chỉ có placeholder.
3. Tạo module theo `technical-design.md`: `domain`, `application`, `ports`, `adapters`, `workers`, `admin`, `telegram`, `observability`.
4. Tên class/function/field/module bằng tiếng Anh; docstring/comment giải thích bằng tiếng Việt có dấu. Thêm lint rule hoặc review checklist để phát hiện comment mới bằng ngôn ngữ sai.
5. Mọi thuật ngữ domain mới phải được thêm vào glossary với bản dịch, định nghĩa, link nội bộ và nguồn ngoài trước khi tài liệu/module được nghiệm thu.
6. Pin Python patch và thư viện sau khi kiểm tra wheel/runtime tương thích; tạo SBOM/license inventory cho dependency trực tiếp.

**Nghiệm thu:** cài được môi trường sạch từ lock; import package không gọi mạng; template/comment mẫu tuân thủ quy ước; không có credential trong `.env.example`; dependency có version/hash.

**Kiểm tra khi triển khai:** `python --version`, `uv sync --frozen`, `uv run ruff check src`, `uv run mypy src`, import smoke command.

## WP-01B — Docker Compose và process boundary

**Đầu ra:** `compose.yaml`, Dockerfile khóa base image/tool theo digest, Garage TOML local, health/readiness probes và role process riêng. Đây là khung bootstrap; worker chưa có vòng nghiệp vụ cho tới các plan tương ứng.

1. Tạo service `admin-web`, `trading-runtime`, `execution-gateway`, `ingestion-worker`, `trainer-worker`, PostgreSQL và object store.
2. Tách network/process: chỉ execution gateway có thể lấy secret Binance; trainer và admin web không mount secret đó. `backend` nội bộ; admin web thêm `web_ingress` để publish cổng loopback host; gateway có `exchange_egress`, ingestion có `source_egress`. Docker bridge không thay thế firewall/egress allowlist. DB login riêng cho từng process được hoàn tất trong WP-01C cùng schema/repository.
3. Chọn profile `local`/`paper`; mọi cấu hình khởi đầu là PAPER. Demo/live endpoint và account không dùng chung config.
4. Thêm health/readiness để phân biệt process chạy với DB/feed/exchange readiness; secret không xuất ra health endpoint.
5. Viết hướng dẫn tiếng Việt tại `README.md`: cài runtime, chạy migration, khởi động paper, dừng, xem log và backup local.

**Nghiệm thu:** `docker compose config` hợp lệ; Compose khởi động dependency local và healthcheck đạt; dừng/khởi động lại không mất named volume; trainer/web không có biến hoặc mount secret trading; chỉ gateway có network egress dành cho Binance. Không chuyển thành LIVE từ cấu hình local.

<a id="wp-01c-schema"></a>
## WP-01C — Schema nền, migration và tách biệt workspace

**File dự kiến:** `migrations/`, `src/trading_bot/adapters/database/models/`, `repositories/`, `unit_of_work.py`.

1. Tạo bảng `workspace`, `user_account`, `user_membership`, `role_binding`, `session`, `mfa_factor`, `telegram_binding`.
2. Tạo bảng `trading_account`, `secret_reference`, `config_version`, `risk_profile_version`, `approval_request`, `watchlist_version`, `watchlist_item`, `strategy_release`.
3. Dùng UUID, `timestamptz` UTC, `Decimal`/numeric cho tiền và giá; thêm workspace-aware FK/unique constraints.
4. Cấu hình Alembic upgrade/downgrade có kiểm soát; seed owner/workspace PAPER chỉ ở local setup, không seed key thật.
5. Repository/query bắt buộc nhận `workspace_id`; thêm kiểm thử ngăn truy vấn vượt tenant hoặc IDOR.
6. Thiết kế migration tương thích rollout: expand → deploy → backfill → contract; không xóa/đổi cột đang được worker cũ dùng trong cùng bước.
7. Dùng role PostgreSQL riêng cho từng process, secret password file local; bật Row-Level Security (RLS, chính sách lọc hàng theo workspace) bắt buộc trên bảng tenant. Query repository vẫn phải nhận workspace ID vì role/RLS là lớp phòng vệ thứ hai.
8. Chạy `alembic upgrade head` trong migrator trước khi bật admin; migration phải có `downgrade` và E2E kiểm tra nâng/hạ/nâng lại trên PostgreSQL riêng.

**Nghiệm thu:** schema mới có thể tạo từ DB rỗng và nâng cấp từ bản trước; truy vấn mọi bảng tenant có workspace scope; tiền không dùng float; migration được review và có rollback strategy.

**Kiểm tra khi triển khai:** Alembic upgrade/downgrade trong DB dùng riêng; unit/integration kiểm tra tenant scope, role không có `BYPASSRLS`, dữ liệu không vượt workspace và precision `Decimal`.

## WP-01D — Authentication, RBAC, MFA và secret boundary

**Đầu ra:** session auth, MFA, policy matrix, interface `SecretProvider`.

1. Xác định role permission matrix cho Owner, Operator, Strategy Admin, Risk Admin, Analyst/Reviewer và service identity.
2. Enforce permission tại service/API theo function + object + workspace; UI chỉ ẩn nút không được xem là kiểm soát bảo mật.
3. Thêm session bảo mật, CSRF, throttling, Argon2id, TOTP cho thao tác nhạy cảm, revoke session và bootstrap owner một lần.
4. Tạo `SecretProvider` interface; DB chỉ giữ `secret_ref`, trạng thái/fingerprint; secret lease ngắn hạn chỉ gateway được resolve.
5. Redact token/key/cookie ở exception, trace, audit, Telegram, model prompt và fixture.
6. Viết hướng dẫn tiếng Việt về tạo user, khôi phục MFA, rotate/revoke secret và kiểm tra quyền.

**Nghiệm thu:** kiểm thử trực tiếp endpoint chứng minh viewer/reviewer không đổi risk, secret hoặc live mode; secret fixture không xuất hiện trong DB/log; quyền bị revoke vô hiệu session theo policy.

## WP-01E — Unit of Work, audit và outbox

1. Tạo `UnitOfWork` đảm bảo cập nhật domain, audit và outbox nằm trong một transaction.
2. Thêm outbox claim bằng lease/`SKIP LOCKED`, retry với backoff, max attempts, dead-letter và receipt duy nhất theo consumer/event.
3. Chuẩn hóa event envelope có `event_id`, `event_type`, `workspace_id`, `aggregate_id`, `occurred_at`, `correlation_id`, `idempotency_key`, `schema_version`.
4. Không đưa secret, raw source body lớn, nội dung Telegram private hay mutable config vào payload; sự kiện tham chiếu ID/version.
5. Ghi audit append-only với actor, action, target, reason, before/after hash và request correlation.

**Nghiệm thu:** rollback DB không phát event; retry event không lặp side effect; audit không bị update/delete qua API ứng dụng; dead-letter có thông tin phục hồi tiếng Việt.

## Thứ tự bàn giao

Hoàn tất WP-01A trước; WP-01B có thể triển khai cùng WP-01C sau khi schema/process contract thống nhất; WP-01D và WP-01E phụ thuộc nền DB. Bàn giao ERD/migration, quyền matrix, event schema và hướng dẫn local cho các plan tiếp theo.
