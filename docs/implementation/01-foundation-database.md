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

1. Tạo service `admin-web`, `trading-runtime`, `execution-gateway`, `ingestion-worker`, `trainer-worker`, PostgreSQL và object store. Các process Python dùng chung một image; chỉ một service khai báo cấu hình build để tránh build/tag lặp cho từng role.
2. Tách network/process: chỉ execution gateway có thể lấy secret Binance; trainer và admin web không mount secret đó. `backend` nội bộ; admin web thêm `web_ingress` để publish cổng loopback host; gateway có `exchange_egress`, ingestion có `source_egress`. Docker bridge không thay thế firewall/egress allowlist. DB login riêng cho từng process được hoàn tất trong WP-01C cùng schema/repository.
3. Chọn profile `local`/`paper`; mọi cấu hình khởi đầu là PAPER. Demo/live endpoint và account không dùng chung config.
4. Thêm health/readiness để phân biệt process chạy với DB/feed/exchange readiness; secret không xuất ra health endpoint.
5. Viết hướng dẫn tiếng Việt tại `README.md`: cài runtime, chạy migration, khởi động paper, dừng, xem log và backup local.

**Nghiệm thu:** `docker compose config` hợp lệ; Compose khởi động dependency local và healthcheck đạt; dừng/khởi động lại không mất named volume; trainer/web không có biến hoặc mount secret trading; chỉ gateway có network egress dành cho Binance. Không chuyển thành LIVE từ cấu hình local.

E2E dùng tag image theo hash các đầu vào thực sự được Dockerfile sao chép. Test chỉ build khi chưa có tag tương ứng; project name ngẫu nhiên vẫn cô lập tài nguyên runtime mà không sinh tag mới ở mỗi lần chạy.

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

1. Khóa matrix quyền tối thiểu cho Owner, Operator, Strategy Admin, Risk Admin, Analyst/Reviewer; mọi endpoint gọi policy phía server.
2. Bootstrap đúng một lần bằng token riêng, tạo workspace trạng thái SETUP, Owner pending MFA, role Owner và tham chiếu MFA trong cùng transaction; xác minh TOTP mới kích hoạt workspace/user.
3. Đăng nhập bằng workspace/email/password/TOTP; băm bằng Argon2id, lưu session token/CSRF chỉ dưới dạng SHA-256, session 12 giờ, MFA gần đây 15 phút, cookie HttpOnly/SameSite Strict/Secure khi production.
4. Chặn phát lại mã TOTP bằng counter lưu trong DB; giới hạn thử đăng nhập theo IP + email trong process đơn; mọi handler sai thông tin trả thông điệp chung.
5. Thêm endpoint đọc identity, cấp token CSRF, re-authenticate, logout và tạo risk profile draft; kiểm quyền Owner/Risk Admin và MFA gần đây ở server.
6. Đặt interface `SecretProvider`; adapter local chỉ cất bí mật TOTP mã hóa AES-GCM ngoài DB. DB giữ tham chiếu; adapter này từ chối resolve credential giao dịch; adapter gateway-only sẽ được gắn khi thực hiện Plan 03.
7. Cấp quyền INSERT/UPDATE workspace cho admin-web chỉ để bootstrap; DB giữ session/MFA/role dưới RLS. Không chuyển bí mật setup URI hoặc token vào log/trace.
8. Thêm unit test cho hash/TOTP/replay/rate limit/policy/mã hóa; E2E đi qua bootstrap → verify → login → CSRF → risk profile → logout trên PostgreSQL/Compose.
9. Hướng dẫn local bootstrap và lưu/backup key MFA. Quản lý invite/member, audit mutation và khôi phục MFA cần phối hợp mail, WP-01E (audit) và Plan 04 (admin UI); không xem role matrix đơn lẻ là giao diện quản lý user hoàn chỉnh.

**Nghiệm thu:** bootstrap token sai bị từ chối và token đúng chỉ tạo một workspace; user chỉ ACTIVE sau khi xác minh TOTP; phiên bị revoke hết hạn ngay; thiếu/sai CSRF bị chặn; reviewer bị 403 khi gọi trực tiếp API thay đổi risk; giá trị TOTP không xuất hiện trong DB/file lưu trữ/log; policy đổi role được nạp lại từ DB trên mỗi request. Kiểm tra E2E phải chạy trên PostgreSQL thật, không skip khi Docker daemon không sẵn sàng.

## WP-01E — Unit of Work, audit và outbox

**Đầu ra:** UnitOfWork gắn transaction với workspace, 3 bảng PostgreSQL có RLS, audit append-only, schema event có version, repository claim/ack/failure/dead-letter và E2E tích hợp trong PAPER Compose.

1. Tạo `UnitOfWork` đảm bảo cập nhật domain, audit và outbox nằm trong một transaction; transaction-local context bắt buộc workspace, lỗi trong context phải rollback toàn bộ.
2. Tạo `outbox_event`, `consumer_receipt`, `audit_event`; bật FORCE RLS và quyền DB tối thiểu. Role ứng dụng không có UPDATE/DELETE trên audit, trigger DB chặn thay đổi audit kể cả khi cấp nhầm quyền ghi.
3. Thêm outbox claim theo workspace bằng lease/`FOR UPDATE SKIP LOCKED`; xử lý side effect sau khi commit claim, retry exponential backoff có trần, max attempts, dead-letter mã lỗi ngắn và receipt unique theo workspace/event/consumer.
4. Chuẩn hóa event envelope có `event_id`, `event_type`, `workspace_id`, `aggregate_type`, `aggregate_id`, `occurred_at`, `correlation_id`, `idempotency_key`, `schema_version`, payload tham chiếu. Payload từ chối các khóa thường chứa secret.
5. Không đưa secret, raw source body lớn, nội dung Telegram private hay mutable config vào payload; sự kiện tham chiếu ID/version. MVP định tuyến event type tới một consumer; fan-out cần bảng delivery riêng.
6. Ghi audit append-only với actor, action, target, reason, before/after SHA-256 và request correlation; chỉ lưu hash trạng thái, không lưu snapshot.
7. E2E PostgreSQL xác nhận rollback domain/audit/outbox, claim loại trừ worker thứ hai, retry/dead-letter, receipt, RLS và audit không thể sửa; migration upgrade/check/downgrade/upgrade lại.

**Nghiệm thu:** rollback DB không giữ lại domain/audit/outbox; lease ngăn hai worker claim đồng thời và event có thể claim lại sau hết lease; lỗi retry đúng backoff/trần/max attempts; receipt không trùng; audit không bị update/delete qua role ứng dụng; payload không có trường secret; dead-letter có mã lỗi và cách phục hồi ghi bằng tiếng Việt. Không tuyên bố exactly-once cho side effect ngoài DB.

## Thứ tự bàn giao

Hoàn tất WP-01A trước; WP-01B có thể triển khai cùng WP-01C sau khi schema/process contract thống nhất; WP-01D và WP-01E phụ thuộc nền DB. Bàn giao ERD/migration, quyền matrix, event schema và hướng dẫn local cho các plan tiếp theo.
