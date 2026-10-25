# Cơ chế bảo mật cho Admin và bot — nghiên cứu ngày 24/10/2026

- **Trạng thái:** tài liệu nghiên cứu, chưa thay đổi code/schema. Đầu vào cho MB-02, MB-05, MB-07, MB-09 và WP-03.
- **Ngày kiểm tra nguồn và đối chiếu code:** 2026-10-24, trên cây làm việc `trading-crypyidian` (Alembic head `c7a90d3f8e21`, sau MB-01).
- **Liên quan:** [hợp đồng Multi-Bot](../architecture/decisions/2026-10-24-multibot-contracts.md), [MB-01 WBS và rulings 1–17](../superpowers/plans/2026-10-24-mb01-identity-migration-wbs.md), [kế hoạch Multi-Bot](../superpowers/plans/2026-10-24-multibot-implementation-plan.md), [thiết kế kỹ thuật §8](../architecture/technical-design.md#security-operations), [kết nối Binance và ingestion](./execution-and-ingestion-2026.md). Tài liệu này không lặp lại nội dung execution/ingestion; chỉ bổ sung phần bảo mật.
- **Cách đọc:** mục 3 là bảng yêu cầu `SEC-xx` có bằng chứng `file:line` (đường dẫn tính từ gốc repo). Trạng thái "Đã có" chỉ được ghi khi đã đọc code xác nhận; "Một phần" nghĩa là có cơ chế nhưng chưa đủ theo nguồn; "Thiếu" là chưa có code.

## Tóm tắt

Nền tảng hiện có nhiều điểm tốt đã được xác minh trong code: mật khẩu Argon2id với tham số mặc định vượt mức tối thiểu OWASP, TOTP có chống phát lại bằng counter, token phiên và CSRF 256 bit chỉ lưu SHA-256, cookie HttpOnly/SameSite=Strict, role đọc lại từ DB mỗi request, RLS `FORCE` theo workspace/bot, hàm `SECURITY DEFINER` ghim `search_path`, audit chặn UPDATE/DELETE, container non-root/read-only/cap_drop và image ghim digest.

Các khoảng trống lớn nhất:

1. **P0 — Quyền DB của process quá rộng:** mọi process role (runtime, gateway, ingestion, trainer) thừa hưởng `crypidian_app` có DML trên `role_binding`, `session`, `mfa_factor`, `telegram_binding`, `user_membership`, `trading_account`, `risk_profile_version`, `workspace_settings_version`… và tự đặt được `app.workspace_id`. Một worker bị chiếm (ingestion-worker xử lý nội dung Internet) có thể tự cấp role owner hoặc publish cấu hình gốc mà không qua Admin API, MFA hay audit (`migrations/versions/48a80bae456d_foundation_workspace_schema.py:478-499`, `migrations/versions/d0b7e9a2416c_workspace_settings_versions.py:115`).
2. **P0 — PostgreSQL 18.1 đã cũ:** bản hiện hành là 18.6 (13/08/2026); từ 18.2 đến 18.6 có nhiều CVE CVSS 8.8 cho phép user DB đã đăng nhập thực thi mã trên server, cùng CVE-2026-14666 về cache plan RLS (`compose.yaml:5`).
3. **P0 cho WP-03 — Chế độ key Binance:** chưa có kiểm tra quyền key khi khởi động. Binance ghi HMAC là deprecated, khuyến nghị Ed25519; withdrawal chỉ bật được khi có IP restriction. Gateway cần tự kiểm `GET /sapi/v1/account/apiRestrictions` và dừng (fail-closed) nếu key có quyền rút/chuyển tiền hoặc không bị giới hạn IP.
4. **P1 — Hành động rủi ro cao:** step-up hiện chỉ là "MFA trong 15 phút" theo phiên và chỉ áp khi đổi `risk.profile_version_id`. Chưa có maker-checker (người publish tự duyệt chính mình), chưa có quyền/luồng kill switch và flatten-all, chưa có idempotency key cho lệnh điều khiển.
5. **P1 — Audit và chống brute-force:** sự kiện xác thực, tạo risk profile và cấp/thu hồi grant chưa được audit. Audit chưa có chuỗi băm, actor/thời gian do process tự khai. Rate limit đăng nhập chỉ nằm trong RAM theo IP+email, còn `/auth/reauthenticate` không bị giới hạn.
6. **P1 — Telegram chưa có code:** cần xây theo mục 4.3 (secret_token, binding một lần, chỉ chat riêng, chống lệnh mơ hồ giữa nhiều bot, cấm flatten).

Thống kê: 47 yêu cầu, gồm P0: 3, P1: 25, P2: 16, P3: 3.

## 1. Phạm vi, tài sản cần bảo vệ và giả định

### 1.1 Phạm vi

| Thành phần | Trong phạm vi | Ghi chú |
| --- | --- | --- |
| Admin API/Web (FastAPI, server-rendered dự kiến ở MB-07) | Xác thực, phiên, CSRF, MFA, RBAC + BotGrant, hành động rủi ro, header, audit | Hiện có `auth/` và `admin/settings_*`; trang web chưa có. |
| Telegram | Webhook, binding danh tính, lệnh bot, thông báo | Chưa có code (`src/trading_bot/telegram/__init__.py:1`). |
| Trading runtime | Quyền DB, fail-closed khi control plane lỗi, kill/pause, fencing | PAPER. |
| Execution gateway (WP-03) | Key Binance, ký request, cô lập secret/egress | Chưa có code venue; chỉ định hướng. |
| PostgreSQL 18 | Role/privilege, RLS, SECURITY DEFINER, TLS, vá lỗi, audit | Compose local. |
| CI/chuỗi cung ứng | Lock/hash, ghim digest, audit lỗ hổng, SBOM, secret scanning | Repo chưa có cấu hình CI. |

Ngoài phạm vi: pentest thực tế, topology cloud/production, OIDC/SSO, ứng dụng mobile, bảo mật thiết bị của người vận hành.

### 1.2 Tài sản

| Mã | Tài sản | Hậu quả nếu mất |
| --- | --- | --- |
| A1 | Quyền điều khiển bot: kill/pause/resume/flatten, publish config/risk | Lệnh ngoài ý muốn, vượt risk, đóng vị thế sai thời điểm. |
| A2 | Root `WorkspaceSettings`, risk profile, bot overlay, strategy release | Nới cap, đổi chiến lược ngầm. |
| A3 | Bí mật: key Binance (WP-03), mật khẩu DB theo process, khóa mã hóa MFA, seed TOTP, bootstrap token, token phiên/CSRF, token bot Telegram và webhook secret | Chiếm tài khoản sàn, giả danh admin, giải mã MFA. |
| A4 | Tính toàn vẹn ledger/trade/decision và audit | Không tái dựng hoặc truy trách nhiệm được. |
| A5 | Danh tính: membership, role, BotGrant, Telegram binding | Leo thang đặc quyền. |
| A6 | Tính sẵn sàng của protective exit và kill switch | Không dừng được khi sự cố. |
| A7 | Báo cáo/PnL/vị thế (bảo mật thông tin) | Lộ chiến lược, vị thế. |
| A8 | Image, dependency, migration | Mã độc chạy với quyền DB/secret. |

### 1.3 Giả định

- Chỉ PAPER; không gọi Binance private API trước WP-03. Admin-web hiện chỉ publish cổng loopback (`compose.yaml:74-75`).
- Production chưa chốt: cần reverse proxy TLS trước admin-web; Telegram webhook bắt buộc HTTPS public.
- Nhóm vận hành nhỏ (1–5 người). Maker-checker cần chính sách rõ cho workspace chỉ có một admin (mục 4.2).
- Mô hình kẻ tấn công: attacker Internet; admin bị phishing (TOTP không chống phishing theo NIST SP 800-63B-4); member nội bộ ác ý hoặc bị khóa; process worker bị chiếm (ingestion-worker đọc nội dung Internet); dependency bị chèn mã; tài khoản Telegram/SIM bị chiếm.
- **RLS dựa trên GUC không phải ranh giới chống role DB bị chiếm:** mọi role đều có thể `set_config('app.workspace_id', …)`. RLS chỉ chặn lỗi ứng dụng (truy vấn thiếu filter); ranh giới thật giữa các process là **grant theo role và policy `TO <role>`**.

## 2. Threat model rút gọn (STRIDE)

S = Spoofing (giả mạo), T = Tampering (sửa đổi), R = Repudiation (chối bỏ), I = Information disclosure (lộ thông tin), D = Denial of service (từ chối dịch vụ), E = Elevation of privilege (leo thang đặc quyền). Phương pháp theo [OWASP Threat Modeling Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html).

### 2.1 Admin Web/API

| STRIDE | Mối đe dọa | Biện pháp |
| --- | --- | --- |
| S | Credential stuffing, đánh cắp cookie, phishing mã TOTP theo thời gian thực | SEC-08, SEC-09, SEC-13, SEC-14, SEC-20 |
| T | CSRF đổi config; sửa payload giữa lúc duyệt và lúc publish (TOCTOU) | SEC-28, SEC-20 (gắn hash payload), SEC-21, SEC-24 |
| R | Admin chối đã publish/flatten; thiếu audit sự kiện xác thực | SEC-32, SEC-33 |
| I | Secret lọt qua API/HTML/log; `/openapi.json`; dò user qua thời gian phản hồi | SEC-26, SEC-35, SEC-12, SEC-27 |
| D | Đăng nhập dồn dập buộc Argon2 tốn 64 MiB/lần; flood API | SEC-08, SEC-30 |
| E | BFLA/BOLA giữa bot/workspace; tự duyệt thay đổi risk | SEC-18, SEC-19, SEC-21 |

### 2.2 Telegram

| STRIDE | Mối đe dọa | Biện pháp |
| --- | --- | --- |
| S | Request webhook giả; tin nhắn forward, gửi thay mặt chat (`sender_chat`), bot khác ra lệnh (Bot-to-Bot), tài khoản Telegram bị chiếm | SEC-36, SEC-37 |
| T | Phát lại `update_id`/callback; sửa lệnh qua `edited_message` | SEC-36, SEC-38 |
| R | Lệnh không gắn actor/membership | SEC-32 |
| I | Gửi vị thế/PnL vào group; lộ token bot trong URL/log | SEC-39, SEC-35 |
| D | Flood update; bị Telegram trả 429 | SEC-36, SEC-39 |
| E | Lệnh mơ hồ áp nhầm bot; một user Telegram bind nhiều workspace; resume/flatten qua chat | SEC-37, SEC-38, SEC-22, SEC-23 |

### 2.3 Trading runtime

| STRIDE | Mối đe dọa | Biện pháp |
| --- | --- | --- |
| S | Worker cũ (lease hết hạn) tiếp tục ghi state | SEC-47 |
| T | Role runtime sửa risk profile/settings qua quyền `crypidian_app` | SEC-01 |
| R | Lệnh nội bộ không có audit | SEC-32 |
| I | Runtime đọc được bảng `session`/`mfa_factor` | SEC-01 |
| D | Control plane sập làm mất kill/protective exit | SEC-47, SEC-22 |
| E | Runtime tự INSERT bản nháp rồi UPDATE sang PUBLISHED (trigger cho phép DRAFT→PUBLISHED) | SEC-01 |

### 2.4 Execution gateway (WP-03)

| STRIDE | Mối đe dọa | Biện pháp |
| --- | --- | --- |
| S | Key dùng từ IP khác; request nội bộ giả tới gateway | SEC-40, SEC-41 |
| T | Intent bị sửa sau khi risk duyệt; phát lại order | SEC-41, SEC-24 |
| R | Lệnh venue không truy vết về intent/actor | SEC-32, SEC-41 |
| I | Lộ API secret/listenKey qua log, env, core dump | SEC-40, SEC-42, SEC-44, SEC-35 |
| D | Bị Binance ban IP (418), vượt rate limit | SEC-42 |
| E | Key có quyền withdrawal/transfer; gateway bị chiếm gây ảnh hưởng mọi account | SEC-40 (một key/account, chỉ trade) |

### 2.5 PostgreSQL

| STRIDE | Mối đe dọa | Biện pháp |
| --- | --- | --- |
| S | Mật khẩu role không TLS, dùng chung | SEC-05, SEC-44 |
| T | Process sửa bảng identity/config; owner TRUNCATE audit; migrator là superuser | SEC-01, SEC-33, SEC-04 |
| R | Không log thay đổi role/DDL | SEC-06 |
| I | Bypass RLS do CVE (CVE-2026-14666), covert channel qua FK/unique | SEC-02, SEC-03 |
| D | Query/lock kéo dài | SEC-06 |
| E | CVE RCE yêu cầu đăng nhập DB (CVSS 8.8); SECURITY DEFINER sai `search_path` | SEC-02, SEC-07 |

### 2.6 CI và chuỗi cung ứng

| STRIDE | Mối đe dọa | Biện pháp |
| --- | --- | --- |
| S | Typosquatting/dependency confusion | SEC-45 |
| T | Tag image bị thay; package bị chèn mã | SEC-45 (digest đã ghim; thêm SBOM/provenance) |
| R | Không biết build nào đang chạy | SEC-45 |
| I | Secret commit vào repo | SEC-45 (secret scanning; `.secrets/` đã ignore) |
| D | Package bị yank/registry lỗi | lock + cache (đã có) |
| E | Mã độc trong dependency chạy cùng quyền DB/secret của admin-web | SEC-45, SEC-01 |

## 3. Bảng yêu cầu bảo mật

**Thang ưu tiên:**

- **P0:** blocker. Xử lý trước khi nghiệm thu WP kế tiếp có liên quan và trước mọi môi trường ngoài máy dev (với SEC-40 là điều kiện bật venue ở WP-03).
- **P1:** bắt buộc trong WP đích, là điều kiện nghiệm thu.
- **P2:** bắt buộc trước production/live (gate MB-09/WP-03).
- **P3:** hardening.

**Ký hiệu dữ liệu test:** xem mục 5.2 (W1/W2, `owner1`, `risk_admin1`…).

### 3.1 Nền tảng và PostgreSQL

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-01 | Tách quyền DB theo process. Thu hồi DML mà `crypidian_app` có trên bảng identity/config (`session`, `role_binding`, `user_membership`, `mfa_factor`, `telegram_binding`, `secret_reference`, `trading_account`, `risk_profile_version`, `workspace_settings_version`, `strategy_release`, `watchlist_*`) khỏi runtime/gateway/ingestion/trainer. Chỉ grant trực tiếp tối thiểu (ví dụ runtime chỉ `SELECT` những bảng cần). Đổi `workspace_isolation` thành policy `TO <role>`. | [ASVS 5.0 V13.2.2](https://github.com/OWASP/ASVS/blob/v5.0.0_release/5.0/en/0x22-V13-Configuration.md), [V8.4.1](https://github.com/OWASP/ASVS/blob/v5.0.0_release/5.0/en/0x17-V8-Authorization.md); [NIST SP 800-53 AC-6](https://github.com/usnistgov/oscal-content); [PG18 RLS](https://www.postgresql.org/docs/18/ddl-rowsecurity.html) | P0 | **Thiếu.** `48a80bae456d_foundation_workspace_schema.py:478-480` (DML cho `crypidian_app`), `:482-489` (cấp `crypidian_app` cho cả 5 process role), `:493-499` (policy không có `TO role`); `d0b7e9a2416c_workspace_settings_versions.py:115` (INSERT/UPDATE settings cho `crypidian_app`); trigger cho phép DRAFT→PUBLISHED `d0b7e9a2416c…:82-105`. E2E hiện chỉ kiểm `user_account`/`audit_event` (`tests/e2e/test_compose_boundary.py:568-573`). | Migration riêng trước MB-02; gate MB-09 | E2E: ma trận `has_table_privilege` cho 5 role × bảng identity/config; probe dưới `crypidian_ingestion_worker` thử `INSERT role_binding`, `UPDATE workspace_settings_version` → `permission denied`; regression runtime PAPER | 5 process role, W1/W2, `owner1` |
| SEC-02 | Nâng PostgreSQL lên 18.6 (ghim digest), theo dõi bản vá minor (quy trình hằng quý), kiểm phiên bản trong E2E. | [PG versioning](https://www.postgresql.org/support/versioning/), [PG security](https://www.postgresql.org/support/security/), [18.6 release notes](https://www.postgresql.org/docs/release/18.6/) | P0 | **Thiếu.** `compose.yaml:5` dùng `postgres:18.1`. Từ 18.2 đến 18.6 có CVE-2026-2006, -16239, -14680, -14669… (CVSS 8.8, cần đăng nhập DB) và CVE-2026-14666 (cache plan bỏ qua thay đổi role đối với RLS). | Platform, trước MB-02 | E2E `SHOW server_version` ≥ 18.6; chạy lại toàn bộ E2E | — |
| SEC-03 | Mỗi bảng tenant/bot mới: `ENABLE` + `FORCE` RLS, policy theo từng role (`TO`), không thêm permissive policy làm OR rộng; ghi rõ RLS GUC không phải ranh giới chống role bị chiếm; tránh covert channel qua FK/unique. | [PG18 RLS](https://www.postgresql.org/docs/18/ddl-rowsecurity.html) (FK/unique bỏ qua RLS); MB-01 rulings 5, 6, 16 | P1 | **Một phần.** Bảng bot-scoped đã có policy `TO` (`c7a90d3f8e21_bot_identity_scope.py:199-212`); bảng identity nền thì chưa (`48a80bae456d…:493-499`). | MB-02, MB-09 | E2E: role không có policy thấy 0 hàng; role đặt `app.workspace_id` của W2 không đọc được bảng identity nếu không có grant | W1/W2, 5 role |
| SEC-04 | Migrator dùng role owner không phải superuser; superuser chỉ dùng cho bootstrap/provision. | ASVS V13.2.2; [PG18 privileges](https://www.postgresql.org/docs/18/ddl-priv.html) | P2 | **Thiếu.** `compose.yaml:8` (`POSTGRES_USER` là superuser), `:113-116` (migrator dùng chính role này). | MB-09 | E2E: `rolsuper=false` cho role migrator; `alembic upgrade/downgrade` đạt | role owner mới |
| SEC-05 | TLS DB ở môi trường ngoài local: `sslmode=verify-full` + `sslrootcert`, `hostssl … scram-sha-256` trong `pg_hba`. | [libpq SSL](https://www.postgresql.org/docs/18/libpq-ssl.html), [pg_hba](https://www.postgresql.org/docs/18/auth-pg-hba-conf.html), ASVS V12.3.1/12.3.3 | P2 | **Thiếu.** `src/trading_bot/adapters/database/engine.py:19-25` không có tham số SSL. | MB-09, WP-03 | Kiểm tra tay trên staging: kết nối không TLS bị từ chối | chứng chỉ test |
| SEC-06 | Đặt `statement_timeout`/`lock_timeout` theo role; ghi log thay đổi role/DDL (tùy chọn pgAudit 18.x hoặc `log_statement=ddl`). | [pgAudit](https://github.com/pgaudit/pgaudit), ASVS V16.3.3 | P3 | **Thiếu.** | MB-09 | E2E kiểm `pg_db_role_setting` | — |
| SEC-07 | Giữ (regression) hàm SECURITY DEFINER ghim `search_path = pg_catalog, pg_temp`, `REVOKE EXECUTE … FROM PUBLIC`, guard role provisioner, trigger bất biến identity/grant. | [PG18 CREATE FUNCTION – SECURITY DEFINER](https://www.postgresql.org/docs/18/sql-createfunction.html) | P1 | **Đã có.** `c7a90d3f8e21…:271-283` (guard role), `:303-321` (definer, `search_path`, revoke PUBLIC), `:327-352` (trigger bất biến bot/grant). Có E2E trong `tests/e2e/probes/multibot_migration.py`. | MB-09 (regression) | Giữ E2E hiện có | — |

### 3.2 Xác thực và phiên

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-08 | Throttle/lockout bền vững (DB hoặc kho dùng chung): đếm lỗi liên tiếp theo tài khoản + factor (≤100 theo NIST; khuyến nghị delay tăng dần từ 5 lần lỗi), thêm giới hạn theo IP để chặn password spraying; áp cho login, bootstrap verify, **reauthenticate**, step-up, Telegram link code; giới hạn số Argon2 chạy đồng thời. | [NIST SP 800-63B-4 §3.2.2](https://pages.nist.gov/800-63-4/sp800-63b.html); [OWASP Authentication – Account Lockout](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html); ASVS V6.1.1, V6.3.1 | P1 | **Một phần.** `src/trading_bot/auth/security.py:74-115` giới hạn trong RAM theo khóa `IP:email`; `src/trading_bot/app.py:167` khởi tạo theo process (mất khi restart, không chia sẻ giữa replica); `src/trading_bot/auth/router.py:197-212` reauthenticate không gọi limiter; đổi email là né được giới hạn theo IP. | MB-07, MB-09 | Unit: bộ đếm/delay; E2E: restart admin-web không reset bộ đếm; reauth sai 6 lần → 429; spray 20 email/1 IP → 429 | `owner1`, `operator_granted`, 20 email giả |
| SEC-09 | Idle timeout cho phiên admin (khuyến nghị 30 phút; NIST AAL2 ≤1 giờ, AAL3 ≤15 phút) cùng absolute 12 giờ; lưu `last_seen_at`. | NIST SP 800-63B-4 §2.2.3/§2.3.3; [OWASP Session](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html); ASVS V7.3.1 | P1 | **Một phần.** Absolute 12 giờ `src/trading_bot/auth/service.py:25`, `:268`; không có idle timeout (`src/trading_bot/adapters/database/models/entities.py:79-99` không có `last_seen_at`). | MB-07 | Unit với thời gian giả: 31 phút không hoạt động → 401 | `analyst_reviewer1` |
| SEC-10 | Phát token phiên mới và thu hồi token cũ khi reauthenticate/step-up và khi role thay đổi. | ASVS V7.2.4 (L1); OWASP Session "Renew the Session ID After Any Privilege Level Change" | P2 | **Thiếu.** `src/trading_bot/auth/service.py:446` chỉ cập nhật `mfa_verified_at` trên cùng phiên. | MB-07 | E2E: cookie cũ sau reauth → 401 | `owner1` |
| SEC-11 | Quản lý phiên: xem/thu hồi phiên của mình; owner thu hồi phiên của member; thu hồi tất cả khi đổi factor/khóa member. Ràng buộc enum `user_membership.status`. | ASVS V7.4.2, V7.4.3, V7.4.5, V7.5.2 | P2 | **Một phần.** Member/user không ACTIVE bị chặn ở mỗi request (`src/trading_bot/auth/service.py:305-306`) nhưng phiên không bị revoke; chưa có API; `status` là chuỗi tự do (`48a80bae456d…:189`). | MB-07 | E2E: khóa `member_suspended` → mọi phiên 401, audit ghi nhận | `member_suspended` |
| SEC-12 | Đăng nhập không lộ user qua thời gian phản hồi: luôn verify Argon2 với hash giả khi user/workspace không tồn tại; mã lỗi đồng nhất. | [OWASP Authentication – error messages/timing](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html); ASVS V6.3.8 | P2 | **Thiếu.** `src/trading_bot/auth/service.py:212-217` bỏ qua Argon2 khi `user is None`; `:258-259` trả 403 riêng (chỉ sau khi đã xác thực đủ — rủi ro thấp). | MB-07 | Unit: hàm verify luôn được gọi; đo thời gian thủ công | email không tồn tại |
| SEC-13 | Cookie phiên `__Host-` (Secure, không Domain, `Path=/`), HttpOnly, SameSite=Strict; Secure bật ở mọi môi trường trừ test HTTP cục bộ. | ASVS V3.3.1, V3.3.3, V3.3.4; [MDN Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie); OWASP Session | P1 | **Một phần.** `src/trading_bot/auth/router.py:136-144`: tên `crypidian_session` không có prefix, `path="/api/v1"` (trang Admin Web MB-07 sẽ không nhận cookie), `secure` chỉ bật khi `app_env == "production"`. | MB-07 | Unit kiểm header `Set-Cookie`; Playwright E2E | `owner1` |
| SEC-14 | Vòng đời MFA: giữ chống replay TOTP; tài liệu hóa cửa sổ lệch giờ; recovery code dùng một lần (băm); reset factor có xác minh danh tính và owner không biết secret; thêm factor mới cần reauth; lộ trình WebAuthn/passkey (TOTP không chống phishing). | NIST SP 800-63B-4 §3.1.4, §3.2.5; ASVS V6.4.3, V6.4.4, V6.5.1, V6.5.5; [OWASP MFA](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html) | P2 | **Một phần.** Replay counter `src/trading_bot/auth/service.py:243-248`, `:440-445`; `valid_window=1` (`:186`, `:243`, `:440`) chấp nhận ±1 chu kỳ trong khi ASVS 6.5.5 nêu TOTP tối đa 30 giây; chưa có recovery (`docs/architecture/technical-design.md:340`). | MB-07 | Unit: mã đã dùng bị từ chối; mã chu kỳ ±2 bị từ chối; recovery code dùng 2 lần bị từ chối | seed TOTP test |
| SEC-15 | Đổi mật khẩu (yêu cầu mật khẩu hiện tại, thu hồi phiên khác); so với blocklist mật khẩu phổ biến/bị lộ; không áp luật thành phần ký tự. | NIST SP 800-63B-4 §3.1.1.2; ASVS V6.2.2–6.2.4, V6.2.12 | P2 | **Một phần.** Độ dài ≥14 (`src/trading_bot/auth/security.py:20`, `src/trading_bot/auth/schemas.py:15`); chưa có đổi mật khẩu hay blocklist. | MB-07 | Unit blocklist; E2E đổi mật khẩu → phiên khác 401 | `strategy_admin1` |
| SEC-16 | Bootstrap token: vô hiệu sau khi bootstrap xong (xóa file/không mount), chỉ dùng một lần. | ASVS V6.4.1 | P3 | **Một phần.** Chỉ hợp lệ khi DB trống (`src/trading_bot/auth/service.py:85-89`), so sánh hằng thời gian (`src/trading_bot/auth/security.py:118-125`); vẫn mount vĩnh viễn (`compose.yaml:85`). | MB-09 | E2E: bootstrap lần 2 → 409 (đã có), không còn secret mount | — |
| SEC-17 | Giữ (regression) baseline: Argon2id (argon2-cffi mặc định t=3, m=64 MiB, p=4 — vượt mức tối thiểu OWASP m=19 MiB, t=2, p=1); token phiên/CSRF 256 bit, DB chỉ lưu SHA-256; role đọc lại từ DB mỗi request; seed TOTP mã hóa AES-GCM có AAD theo workspace/reference. | [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html); ASVS V7.2.3 | P1 | **Đã có.** `src/trading_bot/auth/security.py:13-28`; `src/trading_bot/auth/service.py:260-271`, `:315-325`, `:505-506`; `src/trading_bot/adapters/secrets/local_mfa.py:33-35`, `:96-98`. Tham số argon2-cffi xác minh trong venv ngày 2026-10-24. | MB-09 (regression) | Giữ `tests/unit/test_auth_security.py` | — |

### 3.3 Phân quyền và hành động rủi ro cao

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-18 | Deny-by-default tập trung: mọi route khai báo permission (và bot scope nếu có) qua dependency chung. Test tự động duyệt `app.routes` và fail nếu route mutation chưa có policy. | [OWASP Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html); [API5:2023 BFLA](https://owasp.org/API-Security/editions/2023/en/0x11-t10/); ASVS V8.2.1, V8.3.1 | P1 | **Một phần.** Kiểm tra rải rác: `src/trading_bot/admin/settings_router.py:32-39`, `src/trading_bot/auth/router.py:239-240`. Chưa có registry route→permission. | MB-02, MB-07 | Unit: quét route; ma trận role×endpoint | 5 role + `member_suspended` |
| SEC-19 | Ủy quyền cấp đối tượng (BOLA): `bot_id` từ URL luôn qua `BotGrantRepository.require` + RLS; trả 404 như nhau khi không có quyền/không tồn tại; workspace chỉ lấy từ phiên. | [API1:2023 BOLA](https://owasp.org/API-Security/editions/2023/en/0x11-t10/); ASVS V8.2.2, V8.4.1; hợp đồng Multi-Bot §7 | P1 | **Một phần.** `src/trading_bot/adapters/database/repositories/bot_grants.py:24-41` (role ceiling + grant, không mượn quyền workspace); workspace lấy từ cookie (`src/trading_bot/auth/service.py:287-290`); chưa có API bot. | MB-02, MB-06, MB-07 | API test: operator không có grant gọi bot B → 404; bot của W2 → 404 | `operator_granted`, `operator_ungranted`, W2 bot X |
| SEC-20 | Step-up theo hành động (transaction authorization): challenge một lần, TTL ≤5 phút, gắn hash payload hiển thị cho người dùng (WYSIWYS), gắn phiên, tiêu thụ nguyên tử, lỗi 5 lần thì bắt đầu lại. Áp cho: publish/rollback root/bot config ảnh hưởng risk/strategy/universe; tạo/kích hoạt risk profile; resume sau kill; flatten-all; grant/revoke; đổi member/role; binding Telegram; thao tác secret/key. | [OWASP Transaction Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html) (1.5, 2.4, 2.9, 2.10); ASVS V7.5.1, V7.5.3; NIST SP 800-63B-4 (OTP chỉ dùng một lần) | P1 | **Một phần.** Chỉ có "MFA trong 15 phút" theo phiên (`src/trading_bot/auth/service.py:26`); kiểm khi đổi `risk.profile_version_id` (`src/trading_bot/admin/settings_service.py:412-428`) và khi tạo risk profile (`src/trading_bot/auth/router.py:241-242`). Đổi strategy params/symbols/`paper_execution` không cần step-up. | MB-02, MB-05, MB-07 | Unit: challenge hết hạn/đã dùng/payload khác → 403; E2E publish có/không step-up | `risk_admin1`, `strategy_admin1`, seed TOTP |
| SEC-21 | Maker-checker: thay đổi làm tăng rủi ro (nới cap, đổi risk profile, đổi strategy release/universe ở LIVE, grant quyền cao, resume workspace sau kill, flatten LIVE) cần người duyệt khác người lập. DB `CHECK (decided_by <> requested_by)`, hash payload, hạn duyệt, state machine; chế độ một-admin chỉ cho PAPER với lý do + cooling-off + cảnh báo. | ASVS V2.3.5 (multi-user approval); [NIST SP 800-53 AC-5](https://github.com/usnistgov/oscal-content) (phân tách nhiệm vụ) | P1 | **Thiếu.** `src/trading_bot/admin/settings_service.py:260` và `:319` đặt `approved_by = principal.membership_id` (tự duyệt, kể cả rollback). Bảng `approval_request` đã có `requested_by`/`decided_by` nhưng chưa được dùng và chưa có CHECK/hash/expiry (`src/trading_bot/adapters/database/models/entities.py:330-371`). | MB-02 (thiết kế + DB), MB-07 (UI); bắt buộc trước WP-03 | Unit state machine; E2E: `owner1` lập, `owner1` duyệt → 403; `owner2` duyệt → publish; payload sửa sau khi duyệt → 409 | `owner1`, `owner2`, `risk_admin1` |
| SEC-22 | Kill switch/pause: quyền riêng (`workspace:kill`, `bot:runtime:pause`); ma sát thấp cho hành động **giảm** rủi ro (đã xác thực + CSRF + audit, không chờ duyệt); resume là hành động **tăng** rủi ro nên cần step-up + lý do; runtime đọc trạng thái kill từ DB và fail-closed; có đường break-glass ngoài control plane. | [EU RTS 6 (2017/589) Art. 12](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32017R0589); kế hoạch Multi-Bot (Global Constraints, Task 6/8) | P1 | **Thiếu.** Có `RUNTIME_PAUSE/RESUME` (`src/trading_bot/auth/policy.py:24-25`) và `BotPermission.RUNTIME_PAUSE/RESUME` (`src/trading_bot/auth/bot_policy.py:12-13`), nhưng chưa có quyền kill/flatten, endpoint hay trường trạng thái (`src/trading_bot/config/workspace_settings.py:129-138`). | MB-05, MB-07 | E2E 2 bot: kill workspace → chặn entry mới, hủy lệnh chưa khớp, exit bảo vệ vẫn chạy; resume không step-up → 403 | `risk_admin1`, `operator_granted`, bot A/B |
| SEC-23 | Flatten-all: lệnh riêng; chỉ owner (hoặc risk_admin + người duyệt thứ hai ở LIVE); step-up theo hành động có preview số vị thế/notional (WYSIWYS); gõ xác nhận; idempotency key; cấm qua Telegram; giới hạn tần suất; audit + cảnh báo. | OWASP Transaction Authorization; ASVS V2.3.5, V7.5.3; kế hoạch Multi-Bot Task 8 Step 4 | P1 | **Thiếu.** | MB-07 (PAPER), WP-03 (LIVE) | E2E: thiếu step-up → 403; gọi trùng key → một lệnh; Telegram `/flatten` → từ chối | `owner1`, `risk_admin1`, `owner2` |
| SEC-24 | Idempotency và chống phát lại cho mutation điều khiển: header `Idempotency-Key` scope `(workspace, actor, operation, key)` + fingerprint payload (cùng key khác payload → 422/409), giữ cùng optimistic revision. | [IETF draft-ietf-httpapi-idempotency-key-header-08](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/); hợp đồng Multi-Bot §6 | P1 | **Một phần.** Settings dùng `expected_revision`/parent (`src/trading_bot/admin/settings_schemas.py:20`, `:27`; `src/trading_bot/admin/settings_service.py:121-122`, `:228-231`); `X-Request-ID` chỉ là correlation (`src/trading_bot/admin/settings_router.py:184-191`). | MB-02, MB-05, MB-07 | Unit: retry trả cùng kết quả; payload khác → 422 | `operator_granted` |
| SEC-25 | BotGrant có `expires_at` và lý do; audit grant/revoke; báo cáo rà soát quyền định kỳ. | ASVS V8.3.2; NIST AC-2 | P2 | **Một phần.** Grant/revoke chỉ owner, giữ lịch sử (`src/trading_bot/adapters/database/repositories/bot_grants.py:43-107`, `c7a90d3f8e21…:339-352`), nhưng chưa audit, chưa có hạn. | MB-07 | Unit: grant hết hạn không cấp quyền; audit có bản ghi | `owner1`, `operator_granted` |
| SEC-26 | Chỉ mount router admin ở process `admin-web`; tắt `openapi_url` ở production; không công khai docs/monitoring. | ASVS V13.4.5, V15.2.3; [FastAPI metadata](https://fastapi.tiangolo.com/tutorial/metadata/) | P2 | **Một phần.** `docs_url`/`redoc_url` đã tắt nhưng `openapi_url` vẫn mặc định (`src/trading_bot/app.py:160-165`); router auth/settings mount cho mọi role (`src/trading_bot/app.py:176-177`). | MB-07 | Unit: `GET /openapi.json` → 404; app runtime không có route `/api/v1/auth/*` | — |

### 3.4 Web/API hardening

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-27 | Header bảo mật cho mọi response: CSP (`default-src 'self'; frame-ancestors 'none'; form-action 'self'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, COOP `same-origin`, HSTS ≥1 năm ở HTTPS, `Cache-Control: no-store` cho trang/API có dữ liệu nhạy cảm. | ASVS V3.4.1, V3.4.3–3.4.6, V3.4.8; [OWASP HTTP Headers](https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html) | P1 | **Một phần.** Chưa có middleware header (`src/trading_bot/app.py:160-177`); chỉ một số route đặt `no-store` (`src/trading_bot/auth/router.py:78`, `:108`, `:145`, `:193`; `src/trading_bot/admin/settings_router.py:54`, `:69`…), còn `/auth/me` thì không (`src/trading_bot/auth/router.py:174-178`). | MB-07 | Unit snapshot header mọi route; Playwright kiểm CSP không vỡ trang | — |
| SEC-28 | CSRF cho Admin Web server-rendered: synchronizer token trong form (trường ẩn) + kiểm `Origin`/`Sec-Fetch-Site`; giữ `X-CSRF-Token` cho API; không đổi state qua GET. | [OWASP CSRF](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html); ASVS V3.5.1, V3.5.3 | P1 | **Một phần.** API đã có token theo phiên, lưu hash, so sánh hằng thời gian (`src/trading_bot/auth/router.py:159-171`, `src/trading_bot/auth/service.py:337-356`); `GET /auth/csrf` xoay token (thay đổi state qua GET) (`src/trading_bot/auth/router.py:181-194`). | MB-07 | Playwright: form thiếu token → 403; Origin lạ → 403 | `owner1` |
| SEC-29 | Reverse proxy TLS + `TrustedHostMiddleware` + uvicorn `--forwarded-allow-ips` đúng IP proxy để IP client (dùng cho rate limit/audit) không bị giả. | [Starlette middleware](https://github.com/Kludex/starlette/blob/main/docs/middleware.md); [Uvicorn settings](https://github.com/Kludex/uvicorn/blob/main/docs/settings.md); ASVS V12.2.1, V15.3.4, V4.1.3 | P2 | **Thiếu.** `src/trading_bot/auth/router.py:39-40` dùng `request.client.host`; `Dockerfile:32` chạy uvicorn không cấu hình proxy (mặc định chỉ tin 127.0.0.1); không có tài liệu TLS. | MB-07, MB-09 | E2E: Host lạ → 400; giả `X-Forwarded-For` từ IP không tin cậy bị bỏ qua | — |
| SEC-30 | Rate limit chung theo phiên/IP cho mutation và đọc nặng (429), giới hạn kích thước body, `--limit-concurrency`. | [API4:2023](https://owasp.org/API-Security/editions/2023/en/0x11-t10/); ASVS V2.4.1, V15.2.2; [OWASP REST](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html) | P2 | **Thiếu.** | MB-07, MB-09 | E2E tải nhẹ: vượt ngưỡng → 429 | `analyst_reviewer1` |
| SEC-31 | Jinja autoescape bật; cấm bộ lọc `safe` cho dữ liệu người dùng/Telegram/research (dữ liệu không tin cậy). | ASVS V3.2.2, V1 | P2 | **Thiếu** (chưa có template). | MB-07 | Unit render payload `<script>` → được escape | chuỗi XSS mẫu |

### 3.5 Audit và giám sát

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-32 | Phủ audit đầy đủ: đăng nhập thành công/thất bại, reauth/step-up, lỗi CSRF, bị từ chối quyền, tạo risk profile, grant/revoke, kill/pause/resume/flatten, binding/lệnh Telegram, xoay secret. Trường gồm actor, hash phiên, IP/UA (đã che), request_id, lý do, kết quả. | [OWASP Logging](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html); ASVS V16.3.1–16.3.3, V16.2.1 | P1 | **Một phần.** Settings ghi audit + outbox (`src/trading_bot/admin/settings_service.py:430-444`, `:324-331`; `src/trading_bot/application/outbox.py:141-164`). `AuthService` không ghi audit; `create_risk_profile` không có audit (`src/trading_bot/auth/service.py:448-476`); grant/revoke không audit (`src/trading_bot/adapters/database/repositories/bot_grants.py:43-107`). | MB-07, MB-09 | Unit/E2E: mỗi hành động sinh đúng một audit có các trường bắt buộc; không chứa secret | 5 role, `member_suspended` |
| SEC-33 | Audit bất biến và chống giả mạo: giữ trigger chặn UPDATE/DELETE; thêm chặn TRUNCATE; trigger DB tự đặt `db_role = current_user`, `recorded_at = clock_timestamp()`, `seq`, `prev_hash`/`row_hash` (chuỗi băm theo workspace); job xác minh chuỗi; xuất định kỳ ra kho tách biệt (object lock/WORM). | ASVS V16.4.2, V16.4.3; [NIST SP 800-53 AU-9, AU-10](https://github.com/usnistgov/oscal-content) | P1 | **Một phần.** Trigger append-only `a63d41e7b205_transactional_outbox_and_audit.py:151-164`; mọi process role có INSERT (`:140-147`) với `actor_membership_id` tùy ý; `occurred_at` lấy giờ ứng dụng (`src/trading_bot/application/outbox.py:162`); chưa có hash chain/TRUNCATE guard/export. | MB-09 | E2E: process role chèn audit giả actor → `db_role` thật vẫn được ghi; sửa một hàng (superuser) → verify báo đứt chuỗi | 5 process role |
| SEC-34 | Cảnh báo bất thường: nhiều lần đăng nhập/step-up thất bại; kill/flatten/resume; publish risk; grant owner; Binance `-2015`/418; foreign activity; lệch đồng hồ; đứt chuỗi audit. | OWASP Logging; ASVS V16.4.3; RTS 6 Art. 13 (giám sát tự động) | P2 | **Thiếu.** | MB-09, WP-03 | E2E fake notifier nhận alert | — |
| SEC-35 | Không log/serialize secret, cookie, CSRF token, listenKey, token bot Telegram (URL `api.telegram.org/bot<token>`), Binance signature. | OWASP Logging "Data to exclude"; ASVS V16.2.5; technical design §8 | P2 | **Một phần.** `redact()` cho settings (`src/trading_bot/admin/settings_service.py:469-495`); `SecretLease.value` có `repr=False` (`src/trading_bot/ports/secrets.py:15`); validation error không lặp lại input (`src/trading_bot/app.py:179-192`). | MB-07, WP-03 | Unit: bắt log khi gửi token giả → không xuất hiện | token giả |

### 3.6 Telegram

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-36 | Xác thực webhook: `setWebhook` với `secret_token` ngẫu nhiên (1–256 ký tự `A-Za-z0-9_-`, khuyến nghị ≥32), so sánh hằng thời gian header `X-Telegram-Bot-Api-Secret-Token`; chỉ HTTPS; allowlist IP Telegram tại proxy (`149.154.160.0/20`, `91.108.4.0/22`); `allowed_updates=["message","callback_query"]`; `max_connections` thấp; dedup `update_id` bền vững (unique theo workspace) trước khi xử lý; trả 200 nhanh; giới hạn body; `drop_pending_updates` khi xoay secret. | [Telegram Bot API – setWebhook/Update](https://core.telegram.org/bots/api); [Webhooks guide](https://core.telegram.org/bots/webhooks); technical design §8 | P1 | **Thiếu** (`src/trading_bot/telegram/__init__.py:1`, `src/trading_bot/adapters/telegram/__init__.py:1` chỉ có docstring; chưa có bảng dedup update). | MB-07 | Unit: thiếu/sai header → 401 và không chạm DB; `update_id` trùng → no-op; E2E qua fake Telegram | secret test, update JSON mẫu |
| SEC-37 | Binding và danh tính: liên kết qua mã một lần sinh từ Admin Web sau step-up (deep link `start`, ≤64 ký tự, TTL ≤10 phút, dùng một lần, bị throttle); chỉ nhận lệnh trong private chat với `chat.id == from.id`; từ chối `from.is_bot`, `sender_chat`, `via_bot`, tin forward (`forward_origin`), group/channel; `telegram_user_id` unique toàn hệ thống (hoặc bắt chọn workspace tường minh); mỗi lệnh kiểm lại membership ACTIVE + RBAC + BotPermission; thu hồi binding khi khóa member. | Telegram Bot API (`is_bot`, `sender_chat`, `forward_origin`); [Bot features – deep linking, Bot-to-Bot](https://core.telegram.org/bots/features); [Mini Apps initData](https://core.telegram.org/bots/webapps) nếu dùng | P1 | **Thiếu.** Schema `telegram_binding` có `verified_at`/`revoked_at` nhưng unique chỉ theo workspace (`48a80bae456d…:404-426`, `:425`), chưa có `chat_id` hay bảng mã liên kết. | MB-07 | Unit từng loại update giả mạo → từ chối + audit; user bind ở W2 không điều khiển W1 | `tg_owner1`, `tg_unbound`, `tg_w2`, update group/anonymous admin/bot/forward |
| SEC-38 | Chống mơ hồ giữa nhiều bot: lệnh thay đổi trạng thái phải chỉ rõ bot (ID/alias duy nhất); nhiều bot mà thiếu target → từ chối kèm danh sách; ID lạ không fallback; xác nhận 2 bước bằng callback nonce gắn `(user, bot, action, expiry ≤60s)`, dùng một lần; bỏ qua `edited_message`; Telegram không resume workspace và không flatten. | Kế hoạch Multi-Bot Task 8 Step 3–4; OWASP Transaction Authorization 1.5/2.5 | P1 | **Thiếu.** | MB-07 | Unit: `/bot pause` khi có 2 bot → từ chối; callback dùng lại → từ chối; `/flatten` → từ chối | bot A/B, `tg_operator` |
| SEC-39 | Outbound và token: tuân thủ giới hạn Telegram (≈1 tin/giây/chat, 20 tin/phút/group, ≈30 tin/giây broadcast), xử lý 429 `retry_after`; không gửi chi tiết vị thế vào group; token bot lấy qua SecretProvider, xoay bằng BotFather `/token`. | [Bots FAQ – limits](https://core.telegram.org/bots/faq); [Bot features](https://core.telegram.org/bots/features) | P2 | **Thiếu.** | MB-07 | Unit với fake API trả 429 | token giả |

### 3.7 Execution gateway và Binance (WP-03)

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-40 | Chế độ key: Ed25519 (HMAC đã deprecated; WebSocket API `session.logon` chỉ hỗ trợ Ed25519; trang REST USDⓈ-M hiện chỉ minh họa HMAC/RSA nên WP-03 phải xác nhận ký REST bằng Ed25519, nếu không được thì dùng RSA, không dùng HMAC); chỉ bật Futures + Reading; tắt withdrawal, internal transfer, universal transfer, spot/margin, options, portfolio margin; bắt buộc IP restriction; một key cho mỗi account/bot. Khi khởi động, gateway gọi `GET /sapi/v1/account/apiRestrictions` và dừng (fail-closed) nếu `ipRestrict=false` hoặc bất kỳ quyền chuyển/rút tiền nào bật. | [Binance API Key Types](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/api_key_types.md); [Get API Key Permission](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account#get-api-key-permission); [Binance FAQ tạo API key](https://www.binance.com/en/support/faq/detail/360002502072); [USDⓈ-M WebSocket API](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-api-general-info) | P0 (điều kiện bật venue) | **Thiếu.** `src/trading_bot/adapters/binance_usdm/` chỉ có market data; compose mount file key cho gateway ngay ở profile `paper` (`compose.yaml:197-201`). | WP-03 | Unit với fixture `apiRestrictions`: withdraw bật / `ipRestrict=false` / transfer bật → gateway không khởi động; Demo thủ công | fixture JSON (không dùng key thật) |
| SEC-41 | Cô lập gateway: chỉ gateway có secret và egress tới sàn; secret lấy qua SecretProvider lease ngắn; chỉ gửi lệnh từ intent DB đã được risk duyệt, kiểm lại scope/fencing/kill state ngay trước khi gửi; `newClientOrderId` xác định từ intent; policy DB tường minh cho role gateway. | Technical design §6 (SecretProvider), §9; MB-01 ruling 16; ASVS V13.3.1, V13.3.2 | P1 | **Một phần.** Network `exchange_egress` chỉ gắn gateway (`compose.yaml:204-206`); adapter MFA từ chối cấp secret giao dịch (`src/trading_bot/adapters/secrets/local_mfa.py:78-81`); gateway không có policy trên bảng bot-scoped (default-deny, ruling 16). | WP-03 | E2E fake venue: intent không có reservation/kill bật/fencing cũ → không gửi | bot A/B, fake venue |
| SEC-42 | Ký request: đồng bộ NTP + đo lệch so với `serverTime`; `recvWindow` ≤5000 ms; không retry mù khi trạng thái UNKNOWN; backoff khi 429, dừng khi 418 (ban IP 2 phút–3 ngày); `-2015` → pause bot + alert; coi listenKey là secret (không log), keepalive trước 60 phút, kết nối hết hạn sau 24 giờ. | [USDⓈ-M General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info); [User Data Streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams) | P1 | **Thiếu.** | WP-03 | Unit: lệch đồng hồ > ngưỡng → chặn gửi; fake 429/418/-2015 | fake venue |
| SEC-43 | Chưa mount secret Binance trước WP-03; gateway từ chối khởi động nếu mode PAPER mà có key, hoặc mode DEMO/LIVE mà thiếu cổng bật tường minh. | Technical design §8; kế hoạch Multi-Bot (Global Constraints) | P2 | **Một phần.** File placeholder rỗng (`scripts/prepare_local_secrets.py:33-36`) nhưng vẫn được mount (`compose.yaml:197-201`); `AppSettings` chỉ cho PAPER/DEMO (`src/trading_bot/config/settings.py:8`). | WP-03 | Unit settings; E2E compose profile | — |

### 3.8 Secret, chuỗi cung ứng và container

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-44 | Kiểm kê secret + lịch xoay (DB role, khóa mã hóa MFA, token bot Telegram, webhook secret, key Binance, bootstrap token); file secret quyền 0600; khóa MFA có key id/phiên bản trong ciphertext để xoay mà không mất dữ liệu; vault/KMS ngoài môi trường local. | [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html); ASVS V13.1.4, V13.3.1, V13.3.4; [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/) | P1 | **Một phần.** Mật khẩu role tách theo process và script hỗ trợ đổi (`scripts/provision_database_roles.py:31-38`); file secret ghi theo umask mặc định (`scripts/prepare_local_secrets.py:29-32`); ciphertext MFA không có key id (`src/trading_bot/adapters/secrets/local_mfa.py:36-42`). | MB-09, WP-03 | Runbook + E2E xoay mật khẩu role và khóa MFA (re-encrypt) | khóa test |
| SEC-45 | Gate CI cho chuỗi cung ứng: `uv lock --check`; `uv audit` hoặc `pip-audit` (OSV) trên lock; SBOM (BuildKit `--sbom=true`/CycloneDX); quét image base; secret scanning; cooldown dependency (`exclude-newer`); kiểm provenance/attestation khi có. | ASVS V15.1.1, V15.1.2, V15.2.1, V15.2.4; [uv CLI](https://docs.astral.sh/uv/reference/cli/); [pip-audit](https://github.com/pypa/pip-audit); [Docker SBOM](https://docs.docker.com/build/metadata/attestations/sbom/) | P1 | **Một phần.** Image ghim digest (`Dockerfile:1-3`, `compose.yaml:5`, `:25`); `uv sync --frozen` (`Dockerfile:17`, `:25`); lock có hash. Repo không có cấu hình CI (không có `.github/`). Truy vấn OSV ngày 2026-10-24: 0 lỗ hổng trên 47 package trong lock. `httpx2` 2.15.1 không có PyPI provenance attestation. | MB-09 | CI job; thử thêm package có CVE → job fail | — |
| SEC-46 | Container: giới hạn mem/cpu/pids; `:ro` cho bind mount migrations; tắt header `server` của uvicorn; giữ seccomp mặc định. | [OWASP Docker](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) Rule #7; ASVS V13.4.6 | P3 | **Một phần.** Đã có non-root/read-only/cap_drop/no-new-privileges (`Dockerfile:26-29`; `compose.yaml:76-81`, `:128-131`, `:176-181`); thiếu limits; `compose.yaml:127` mount không `:ro`. | MB-09 | `docker compose config` lint | — |

### 3.9 Runtime

| ID | Yêu cầu | Nguồn | Ưu tiên | Trạng thái + bằng chứng | WP đích | Kiểm thử | Dữ liệu test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-47 | Runtime fail-closed khi control plane lỗi: chỉ dùng config đã publish; đọc trạng thái kill/pause từ DB ở mỗi chu kỳ quyết định; mọi lệnh thay đổi state mang fencing token hiện hành; worker cũ bị từ chối; vẫn giữ protective exit. | Hợp đồng Multi-Bot §7; technical design §8; ASVS V16.5.3 | P1 | **Một phần.** Runtime ghim settings version/hash và chỉ cho PAPER (`src/trading_bot/config/settings.py:40-54`); chưa có lease/fencing/kill (thuộc MB-05). | MB-05 | E2E: admin-web dừng → runtime vẫn chạy exit; kill bật trong DB → entry bị chặn trong ≤1 chu kỳ | bot A/B |

## 4. Khuyến nghị theo thứ tự triển khai

### 4.0 Quick win (ngay, trước MB-02)

1. **SEC-02:** đổi `postgres:18.1` sang `postgres:18.6-bookworm@sha256:<digest>`, chạy toàn bộ E2E. PostgreSQL khuyến nghị luôn chạy minor hiện hành.
2. **SEC-01:** migration `revoke_process_identity_dml`:
   - `REVOKE ALL ON <bảng identity/config> FROM crypidian_app`.
   - Cấp lại trực tiếp cho `crypidian_admin_web` đúng quyền đang dùng. `crypidian_trading_runtime` chỉ `SELECT` các bảng runtime đọc (`trading_account`, `workspace_settings_version`, `risk_profile_version`, `strategy_release`…). Gateway/ingestion/trainer không có quyền.
   - Thay `workspace_isolation` bằng policy `TO crypidian_admin_web` / `TO crypidian_trading_runtime`.
   - Thêm E2E ma trận quyền (mẫu `tests/e2e/test_compose_boundary.py:568-573`).
   - Sau đó cân nhắc ngừng dùng group `crypidian_app` (ruling 6 đã đi hướng này cho bảng mới).
3. **SEC-26:** `FastAPI(openapi_url=None)` khi `app_env != "local"`; chỉ `include_router` admin khi `process_role == "admin-web"`.
4. **SEC-08 (phần nhỏ):** áp limiter cho `/auth/reauthenticate`; thêm khóa theo IP độc lập email.
5. **SEC-32 (phần nhỏ):** ghi audit `risk_profile.created` trong `create_risk_profile`; audit grant/revoke trong repository/service gọi nó.
6. **SEC-43:** bỏ mount `binance_api_key/secret` khỏi profile `paper` cho tới WP-03.

### 4.1 Step-up MFA cho hành động rủi ro (MB-02 xây khung, MB-05/07 dùng)

- **Mô hình:** bảng `step_up_challenge(id, workspace_id, membership_id, session_token_hash, action, payload_hash, created_at, expires_at, consumed_at, failed_attempts)`; RLS + grant chỉ cho admin-web.
- **Luồng:**
  1. Client gửi `POST /api/v1/step-up` với `action` và payload chuẩn hóa. Server trả `challenge_id` cùng bản tóm tắt cần hiển thị (WYSIWYS: bot, trường thay đổi trước → sau, số vị thế/notional với flatten).
  2. Người dùng nhập TOTP (tương lai: WebAuthn). Server kiểm counter TOTP chưa dùng (tái sử dụng `last_used_counter`), lỗi quá 5 lần thì challenge hỏng và phải tạo lại.
  3. Mutation gửi kèm `X-Step-Up-Challenge`. Service tính lại `payload_hash` từ request thật và tiêu thụ nguyên tử bằng `UPDATE … SET consumed_at = now() WHERE id = :id AND consumed_at IS NULL AND expires_at > now() AND payload_hash = :h AND session_token_hash = :s RETURNING id` **trong cùng transaction** với mutation và audit.
- **TTL:** 5 phút. Giữ "MFA trong 15 phút" theo phiên chỉ cho thay đổi rủi ro thấp (ví dụ đổi tên bot). Hành động mức cao (flatten, resume workspace, grant owner, bật live) luôn yêu cầu mã mới cho đúng challenge đó.
- **Phân loại thay đổi config theo chiều rủi ro.** Tính `risk_direction` khi validate draft:
  - *Tăng:* nới cap, thêm symbol, đổi strategy release, tăng `starting_equity` PAPER…
  - *Giảm:* siết cap, bớt symbol.
  - *Trung tính.*
  - Step-up áp cho mọi publish/rollback làm thay đổi `risk`, `strategy`, `market_data.symbols` và `paper_execution` (mở rộng `_require_risk_change_access` ở `src/trading_bot/admin/settings_service.py:412-428`, hiện chỉ xét `profile_version_id`).
- Sau step-up thành công, phát token phiên mới (SEC-10).

### 4.2 Maker-checker cho publish risk/config

- **Mở rộng `approval_request`** (đã có `requested_by`/`decided_by`, `entities.py:330-371`):
  - Thêm `action`, `payload_hash`, `subject_version_id`, `expires_at`, `required_approvals` (mặc định 1 người duyệt khác người lập).
  - DB `CHECK (decided_by IS NULL OR decided_by <> requested_by)`; trigger state machine `PENDING → APPROVED|REJECTED|EXPIRED|CANCELLED`, bất biến sau khi kết thúc.
- **Publish:** `publish_draft` yêu cầu một `approval_request` APPROVED, chưa hết hạn, `payload_hash == settings_hash` của draft, `decided_by` có quyền tương ứng và đã step-up khi duyệt. `approved_by` lấy từ approval, không lấy từ người gọi publish (hiện `settings_service.py:260`, `:319`).
- **Phạm vi bắt buộc:** thay đổi có `risk_direction = tăng`; grant `bot:config:publish`/owner; resume workspace sau kill; flatten ở LIVE; bật DEMO/LIVE cho account. **Không** áp cho pause/kill và thay đổi giảm rủi ro (fast path, vẫn step-up + audit).
- **Workspace chỉ có một admin (chỉ PAPER):** setting `approval_policy = SINGLE_ADMIN_PAPER` cho phép tự duyệt khi có lý do bắt buộc, cooling-off (ví dụ 10 phút giữa lập và duyệt), cảnh báo và audit `self_approved=true`. WP-03 phải chặn chính sách này ở DEMO/LIVE.
- **Rollback:** là một publish nên đi cùng quy trình. Rollback về phiên bản *giảm* rủi ro được dùng fast path.
- **Kiểm thử:** xem SECT-20 đến SECT-24.

### 4.3 Bảo vệ Telegram (MB-07)

1. **Ingress:**
   - Endpoint `/api/v1/telegram/webhook/{workspace_slug}`: kiểm header secret bằng `hmac.compare_digest` trước khi parse JSON; body ≤ 1 MiB.
   - Proxy chỉ nhận dải IP Telegram.
   - `setWebhook(allowed_updates=["message","callback_query"], max_connections≤10, secret_token=<32–64 ký tự>)`.
   - Lưu `telegram_update(workspace_id, update_id)` UNIQUE và ghi trong cùng transaction với side effect; trùng thì no-op 200.
2. **Binding:**
   - Admin Web (sau step-up) sinh mã liên kết một lần (≥128 bit, base64url ≤64 ký tự, băm trong DB, TTL 10 phút).
   - Người dùng mở `https://t.me/<bot>?start=<code>` trong **chat riêng**. Server xác nhận `chat.type == "private"`, `chat.id == from.id`, `from.is_bot == false`, rồi ghi `telegram_user_id` + `chat_id`, đặt `verified_at` và audit.
   - Unique `telegram_user_id` toàn hệ thống để một tài khoản Telegram không điều khiển nhiều workspace một cách mơ hồ.
   - Khóa member → revoke binding.
3. **Lọc lệnh:** bỏ qua `edited_message`, tin có `sender_chat`, `via_bot`, `forward_origin`, chat không phải private, và mọi `from.is_bot` (Telegram đã thêm Bot-to-Bot Communication cho phép bot gửi `/command@OtherBot` trong group).
4. **Chống mơ hồ:**
   - Cú pháp bắt buộc `/bot pause <bot_id|alias>`.
   - Workspace có >1 bot mà thiếu target → trả danh sách bot, không thực thi. ID không khớp → "không tìm thấy", không đoán gần đúng.
   - Lệnh thay đổi trạng thái dùng nút xác nhận `callback_data` chứa nonce ngẫu nhiên tham chiếu bản ghi `(user, bot, action, expires_at = now + 60s)`, dùng một lần.
5. **Quyền:** pause bot và kill workspace được phép qua Telegram (giảm rủi ro). Resume chỉ trên Admin Web (cần step-up). Flatten và publish config bị cấm qua Telegram. Mọi lệnh gọi cùng application service với Admin API (`handle_bot_command(principal, …)`), kiểm membership ACTIVE + RBAC + BotPermission mỗi lần.
6. **Outbound:** hàng đợi gửi có rate limit (1 tin/giây/chat), xử lý `retry_after`. Không gửi PnL chi tiết vào group. Token bot là secret, không log URL Bot API.

### 4.4 Kill switch và flatten-all (MB-05/MB-07)

Ma trận đề xuất. Quyền mới cần thêm vào `Permission`/`BotPermission`: `workspace:kill`, `workspace:resume`, `workspace:flatten`, `bot:flatten`.

| Hành động | Quyền | Role mặc định | Xác thực | Maker-checker | Telegram |
| --- | --- | --- | --- | --- | --- |
| Pause bot | `bot:runtime:pause` | owner, risk_admin, operator có grant | phiên + CSRF | Không | Có (xác nhận nonce) |
| Kill workspace (chặn entry mới mọi bot, hủy lệnh chưa khớp, giữ exit bảo vệ) | `workspace:kill` | owner, risk_admin | phiên + CSRF (không step-up để không cản trở khẩn cấp) | Không | Có (xác nhận nonce) |
| Resume bot | `bot:runtime:resume` | owner, risk_admin, operator có grant | step-up + lý do | Không (trừ khi bot đang bị kill cấp workspace) | Không |
| Resume workspace sau kill | `workspace:resume` | owner | step-up + lý do | Có (trừ chế độ một-admin PAPER) | Không |
| Flatten-all / flatten bot | `workspace:flatten` / `bot:flatten` | owner | step-up theo hành động + preview + gõ xác nhận | Có ở LIVE; tùy chọn ở PAPER | Cấm |

Yêu cầu runtime:

- Trạng thái kill là hàng DB có version, đọc ở mỗi chu kỳ quyết định và trước mỗi lệnh gateway. Mất kết nối control plane không được hủy trạng thái kill (fail-closed).
- **Break-glass:** script CLI chạy với role DB riêng chỉ có quyền đặt kill. Ở LIVE, phương án cuối là xóa/khóa API key trên Binance. Có runbook.
- Kill/flatten luôn được audit + cảnh báo, idempotent theo `Idempotency-Key`. Flatten có tần suất tối đa 1 lần/phút/workspace.
- Tham chiếu ngành: EU RTS 6 Art. 12 yêu cầu khả năng hủy ngay mọi lệnh chưa khớp và biết lệnh nào thuộc thuật toán nào; SEC 15c3-5 yêu cầu kiểm soát pre-trade nằm dưới quyền kiểm soát trực tiếp. Dự án không bị ràng buộc pháp lý bởi hai quy định này nhưng dùng làm chuẩn tham chiếu.

### 4.5 Audit bất biến (MB-09)

- Trigger `BEFORE INSERT` trên `audit_event` (SECURITY DEFINER, `search_path` ghim):
  - Đặt `db_role = current_user`, `recorded_at = clock_timestamp()`.
  - Lấy `seq` tiếp theo theo workspace (advisory xact lock workspace).
  - Tính `row_hash = sha256(prev_hash || canonical_json(row))` bằng `pgcrypto` hoặc `sha256()` built-in.
  - Không cho client tự ghi các cột này.
- Thêm trigger `BEFORE TRUNCATE` báo lỗi. Không cấp TRUNCATE/UPDATE/DELETE cho role nào ngoài owner. Owner chỉ dùng khi migrate.
- Job hằng ngày xác minh chuỗi và xuất batch đã ký ra object store có object lock/retention (Garage local chỉ dùng thử nghiệm). Đứt chuỗi → cảnh báo P1.
- Bổ sung loại sự kiện theo SEC-32. Ghi hash phiên (không ghi token), IP đã chuẩn hóa từ proxy tin cậy, user agent rút gọn.

### 4.6 Rate limiting và lockout (MB-07)

- Bảng `auth_throttle(subject_type, subject_key_hash, factor, consecutive_failures, locked_until, updated_at)` dùng cho tài khoản và IP.
- **Chính sách đề xuất** (thỏa NIST ≤100 lần liên tiếp):
  - Lỗi thứ 5 trở đi delay 30 giây, tăng gấp đôi tới tối đa 15 phút.
  - 50 lần liên tiếp thì khóa factor tới khi owner mở hoặc qua recovery.
  - Thành công thì reset.
  - Giới hạn theo IP độc lập email (ví dụ 30 lần thất bại/10 phút) để chặn spraying.
- Áp cho login, bootstrap verify, reauthenticate, step-up, mã liên kết Telegram.
- Semaphore giới hạn số Argon2 chạy đồng thời (mỗi lần 64 MiB) để tránh cạn RAM.
- Dùng IP từ proxy tin cậy (SEC-29). Trả 429 thống nhất, không tiết lộ tài khoản có tồn tại hay không.

### 4.7 Quản lý và xoay vòng secret (MB-09/WP-03)

| Secret | Nơi dùng | Lưu trữ đề xuất | Chu kỳ xoay gợi ý | Cách xoay |
| --- | --- | --- | --- | --- |
| Mật khẩu role DB theo process | mọi process | Compose secret (local) / vault | 90 ngày hoặc khi nghi lộ | `provision_database_roles.py` ALTER ROLE rồi restart process |
| Khóa mã hóa MFA | admin-web | file 0600 / KMS | 1 năm | Thêm key id vào ciphertext, re-encrypt dần, giữ key cũ chỉ để giải mã |
| Token bot Telegram | telegram handler | vault | khi nghi lộ / 1 năm | BotFather `/token`, cập nhật secret, `setWebhook` |
| Webhook `secret_token` | telegram handler | vault | 90 ngày | `setWebhook` với secret mới + `drop_pending_updates` |
| Key Binance (Ed25519) | chỉ gateway | vault/KMS, lease ngắn | 90 ngày hoặc khi đổi IP | Tạo key mới (IP restriction), self-check `apiRestrictions`, chuyển, xóa key cũ |
| Bootstrap token | admin-web (một lần) | file | sau khi bootstrap | Xóa file và bỏ mount |
| Token phiên/CSRF | DB (hash) | — | theo phiên | Đã xoay khi login; thêm xoay khi reauth (SEC-10) |

- File trong `.secrets/` cần `chmod 600` trên POSIX. Trên Windows, ACL do host quản lý (technical design §9).
- Không log hoặc đưa secret vào prompt/artifact AI (technical design §8).

### 4.8 Key Binance và tách quyền gateway (WP-03)

1. Mỗi `TradingAccount` LIVE/DEMO có **một key riêng**, kiểu **Ed25519** (RSA nếu WP-03 xác nhận REST USDⓈ-M chưa nhận Ed25519; không dùng HMAC), IP restriction tới IP egress cố định của gateway. Chỉ bật Futures + Reading.
2. Khi khởi động và định kỳ (ví dụ mỗi giờ), gateway gọi `GET /sapi/v1/account/apiRestrictions`. Chặn nếu `ipRestrict != true`, hoặc `enableWithdrawals`, `enableInternalTransfer`, `permitsUniversalTransfer`, `enableSpotAndMarginTrading`, `enableMargin`, `enableVanillaOptions` có giá trị `true`, hoặc `enableFutures != true`. Gặp lỗi thì pause bot và cảnh báo.
3. Có thể tách key USER_DATA (đọc/reconcile) và key TRADE như Binance gợi ý ("one API-key could be used for TRADE only").
4. Ký request với `recvWindow` ≤5000 ms. Theo dõi `X-MBX-USED-WEIGHT-*` và `X-MBX-ORDER-COUNT-*`; backoff khi 429, dừng gửi khi 418.
5. **Ranh giới process:** chỉ `execution-gateway` có secret và network `exchange_egress`. Runtime không bao giờ đọc key. Gateway chỉ nhận intent qua DB/outbox (payload chỉ chứa ID) và đọc lại trạng thái chuẩn (reservation, kill, fencing) trước khi gửi.

## 5. Test case bảo mật đề xuất và dữ liệu test

### 5.1 Danh sách test case (đưa vào WBS MB-07/MB-09)

| ID | Mô tả | SEC | Loại | WBS |
| --- | --- | --- | --- | --- |
| SECT-01 | Ma trận `has_table_privilege` cho 5 process role trên toàn bộ bảng identity/config/bot; ingestion/trainer/gateway không có DML identity/config | SEC-01, SEC-03 | E2E | MB-09 (sớm hơn nếu làm quick win) |
| SECT-02 | Dưới `crypidian_ingestion_worker`: `set_config('app.workspace_id', W1)` rồi `INSERT role_binding(owner)` / `UPDATE workspace_settings_version SET status='PUBLISHED'` → permission denied | SEC-01 | E2E | MB-09 |
| SECT-03 | `SHOW server_version` ≥ 18.6 | SEC-02 | E2E | MB-09 |
| SECT-04 | Đăng nhập sai 5 lần → delay/429; restart admin-web không reset bộ đếm | SEC-08 | E2E | MB-07 |
| SECT-05 | Reauthenticate sai lặp lại → 429, audit `auth.reauth_failed` | SEC-08, SEC-32 | Unit + E2E | MB-07 |
| SECT-06 | Password spraying: 20 email/1 IP → 429 theo IP | SEC-08 | Unit | MB-07 |
| SECT-07 | Phiên không hoạt động 31 phút → 401; absolute 12 giờ → 401 | SEC-09 | Unit (giờ giả lập) | MB-07 |
| SECT-08 | Sau reauthenticate/step-up, cookie cũ → 401, cookie mới hoạt động | SEC-10 | E2E | MB-07 |
| SECT-09 | Khóa `member_suspended` → mọi phiên + binding Telegram bị thu hồi, audit | SEC-11, SEC-37 | E2E | MB-07 |
| SECT-10 | Owner thu hồi phiên của `operator_granted` → 401 ngay | SEC-11 | E2E | MB-07 |
| SECT-11 | Login email không tồn tại vẫn gọi Argon2 (spy); mã lỗi đồng nhất | SEC-12 | Unit | MB-07 |
| SECT-12 | `Set-Cookie` có `__Host-`, `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/` | SEC-13 | Unit | MB-07 |
| SECT-13 | TOTP: mã đã dùng, mã chu kỳ ±2, mã sau khi reset factor → từ chối | SEC-14 | Unit | MB-07 |
| SECT-14 | Recovery code dùng 2 lần → lần 2 bị từ chối | SEC-14 | Unit | MB-07 |
| SECT-15 | Quét `app.routes`: mọi mutation có permission dependency + CSRF | SEC-18, SEC-28 | Unit | MB-07 |
| SECT-16 | Ma trận role×endpoint (owner/strategy_admin/risk_admin/operator/analyst_reviewer/suspended) cho bot API | SEC-18 | Unit/API | MB-07 |
| SECT-17 | `operator_ungranted` đọc/pause bot A → 404; bot của W2 → 404 (không phân biệt với không tồn tại) | SEC-19 | API + E2E | MB-07, MB-09 |
| SECT-18 | Step-up: thiếu challenge, challenge hết hạn, đã dùng, payload khác, phiên khác → 403 | SEC-20 | Unit | MB-07 |
| SECT-19 | Publish thay đổi `strategy.parameters`/`market_data.symbols` không step-up → 403 | SEC-20 | API | MB-07 |
| SECT-20 | Maker-checker: `owner1` lập + tự duyệt → 403; `owner2` duyệt → publish OK; `approved_by == owner2` | SEC-21 | E2E | MB-07 |
| SECT-21 | Draft bị sửa sau khi duyệt (hash khác) → publish 409 | SEC-21 | Unit | MB-07 |
| SECT-22 | Approval hết hạn → publish bị từ chối | SEC-21 | Unit | MB-07 |
| SECT-23 | DB `CHECK decided_by <> requested_by` chặn ghi trực tiếp | SEC-21 | E2E | MB-09 |
| SECT-24 | Chế độ `SINGLE_ADMIN_PAPER`: tự duyệt cần lý do + cooling-off, audit `self_approved`; cấm ở DEMO/LIVE | SEC-21 | Unit | MB-07 |
| SECT-25 | Kill workspace bởi `risk_admin1`: entry mới bị chặn ở cả 2 bot, lệnh chưa khớp bị hủy, exit bảo vệ vẫn chạy | SEC-22, SEC-47 | E2E | MB-09 |
| SECT-26 | Resume không step-up → 403; `operator_granted` resume workspace → 403 | SEC-22 | API | MB-07 |
| SECT-27 | Admin-web dừng; runtime vẫn đọc kill DB và giữ exit | SEC-47 | E2E | MB-09 |
| SECT-28 | Flatten: thiếu step-up → 403; gửi trùng `Idempotency-Key` → một lệnh; preview khớp vị thế | SEC-23, SEC-24 | E2E | MB-07 |
| SECT-29 | `Idempotency-Key` dùng lại với payload khác → 422 | SEC-24 | Unit | MB-07 |
| SECT-30 | Grant hết hạn không cấp quyền; grant/revoke có audit | SEC-25, SEC-32 | Unit | MB-07 |
| SECT-31 | `GET /openapi.json` → 404 (non-local); app runtime không có route `/api/v1/auth/*` | SEC-26 | Unit | MB-07 |
| SECT-32 | Header CSP/nosniff/Referrer-Policy/COOP/`no-store` trên mọi trang/API nhạy cảm | SEC-27 | Unit + Playwright | MB-07 |
| SECT-33 | Form Admin Web thiếu token CSRF hoặc `Origin` lạ → 403; GET không đổi state | SEC-28 | Playwright | MB-07 |
| SECT-34 | Host header lạ → 400; `X-Forwarded-For` từ IP không tin cậy bị bỏ qua | SEC-29 | E2E | MB-09 |
| SECT-35 | Template escape payload `<script>` từ tên bot/research | SEC-31 | Unit | MB-07 |
| SECT-36 | Mọi hành động ở SEC-32 sinh audit đủ trường; không chứa token/secret | SEC-32, SEC-35 | Unit + E2E | MB-07, MB-09 |
| SECT-37 | Process role chèn audit giả actor → `db_role` thật được ghi; sửa hàng (superuser) → verify báo đứt chuỗi; TRUNCATE bị chặn | SEC-33 | E2E | MB-09 |
| SECT-38 | Webhook thiếu/sai `X-Telegram-Bot-Api-Secret-Token` → 401, không chạm DB | SEC-36 | Unit | MB-07 |
| SECT-39 | `update_id` trùng → no-op; `edited_message` bị bỏ qua | SEC-36, SEC-38 | Unit | MB-07 |
| SECT-40 | Update từ group / `sender_chat` / `from.is_bot` / `via_bot` / forward → từ chối + audit | SEC-37 | Unit | MB-07 |
| SECT-41 | Mã liên kết: hết hạn, dùng lại, sai 10 lần → từ chối; `tg_w2` không điều khiển W1 | SEC-37, SEC-08 | Unit + E2E | MB-07 |
| SECT-42 | `/bot pause` thiếu target khi có 2 bot → từ chối kèm danh sách; ID lạ không fallback; callback nonce dùng lại/hết hạn → từ chối | SEC-38 | Unit | MB-07 |
| SECT-43 | `/flatten`, `/resume workspace`, `/publish` qua Telegram → từ chối | SEC-38, SEC-23 | Unit | MB-07 |
| SECT-44 | Fake Telegram 429 `retry_after` → hàng đợi chờ đúng | SEC-39 | Unit | MB-07 |
| SECT-45 | Gateway với fixture `apiRestrictions` sai (withdraw/transfer bật, `ipRestrict=false`, futures tắt) → không khởi động | SEC-40 | Unit | WP-03 |
| SECT-46 | Fake venue 429/418/`-2015`; lệch đồng hồ > ngưỡng → chặn gửi, pause bot | SEC-42 | Unit | WP-03 |
| SECT-47 | Rendered HTML/API/log/audit không chứa token phiên, CSRF, secret Telegram, key | SEC-35 | E2E | MB-07, MB-09 |
| SECT-48 | CI: `uv lock --check`, `uv audit`/pip-audit, SBOM tạo thành công; package có CVE làm job fail | SEC-45 | CI | MB-09 |

### 5.2 Yêu cầu dữ liệu và tài khoản test (phối hợp bộ QA dataset)

Mọi credential, seed TOTP và secret dưới đây là **giá trị test do fixture sinh** (hoặc lưu trong file seed/fixture của dự án). Không dùng mật khẩu thật, key sàn thật hay token Telegram thật.

| Nhóm | Mã | Mô tả | Dùng cho |
| --- | --- | --- | --- |
| Workspace | W1 `qa-alpha` | Workspace chính, ACTIVE, PAPER; 3 bot: A (account A, ENABLED), B (account B, ENABLED), C (DISABLED) | đa số test |
| Workspace | W2 `qa-beta` | Workspace khác, 1 bot X | cross-tenant |
| Tài khoản W1 | `owner1`, `owner2` | Owner (hai người để test maker-checker) | SEC-20…23 |
| Tài khoản W1 | `strategy_admin1` | Có `settings:write`, `bot:config:*` | SEC-18, 20, 21 |
| Tài khoản W1 | `risk_admin1` | Có `risk_profile:write`, pause/resume, đề xuất `workspace:kill` | SEC-20, 22 |
| Tài khoản W1 | `operator_granted` | Operator có grant `bot:read`, `bot:runtime:pause`, `bot:runtime:resume` trên bot A | SEC-19, 22, 26 |
| Tài khoản W1 | `operator_ungranted` | Operator không có grant | SEC-19 |
| Tài khoản W1 | `analyst_reviewer1` | Chỉ đọc/review | SEC-18 |
| Tài khoản W1 | `multi_role1` | operator + analyst_reviewer (không được mượn quyền workspace) | SEC-19 |
| Tài khoản W1 | `member_suspended` | Membership không ACTIVE (cần enum `SUSPENDED`) | SEC-11 |
| Tài khoản W1 | `user_disabled` | `user_account.status` không ACTIVE | SEC-11 |
| Tài khoản W1 | `pending_mfa1` | Chưa xác minh TOTP | SEC-14 |
| Tài khoản W2 | `owner_w2`, `operator_w2` | Owner/operator của W2 | SEC-19 |
| Tài khoản chéo | `cross_member` | Thành viên cả W1 và W2 (khác role) | phạm vi phiên theo workspace |
| Telegram | `tg_owner1`, `tg_operator` | Đã bind, chat riêng | SEC-37, 38 |
| Telegram | `tg_unbound` | Chưa bind | SEC-37 |
| Telegram | `tg_w2` | Bind ở W2 | SEC-37 |
| Telegram | Update mẫu | Group chat, anonymous admin (`sender_chat`), `from.is_bot=true`, `via_bot`, forward, `edited_message`, `update_id` trùng, callback nonce cũ | SEC-36…38 |
| Secret test | — | Khóa MFA test, seed TOTP theo user, webhook secret test, token bot giả, fixture `apiRestrictions` (đúng chuẩn và 4 biến thể sai) | SEC-14, 36, 40 |
| DB role | — | 5 process role + `crypidian_bot_provisioner` + role migrator non-superuser (sau SEC-04) | SEC-01, 03, 04 |
| Thời gian | — | Đồng hồ inject (tham số `at`/clock port) để test TTL/idle/TOTP mà không sleep | SEC-09, 14, 20, 21 |
| Venue | — | Fake venue/adapter cho 429/418/-2015/UNKNOWN (không gọi Binance thật) | SEC-41, 42 |

## 6. Nguồn tham khảo

Ngày kiểm tra cho mọi nguồn: **2026-10-24**. Cheat sheet OWASP là tài liệu sống nên ghi ngày commit gần nhất trên GitHub.

| Nguồn | URL | Phiên bản / ngày phát hành |
| --- | --- | --- |
| OWASP ASVS 5.0.0 (V2, V3, V4, V6, V7, V8, V12, V13, V15, V16) | https://github.com/OWASP/ASVS/releases/tag/v5.0.0_release ; https://owasp.org/www-project-application-security-verification-standard/ | 5.0.0 stable, 2025-05-30 (nhánh "latest" bleeding edge 2026-10-15 chưa phải bản phát hành) |
| OWASP API Security Top 10 | https://owasp.org/API-Security/editions/2023/en/0x11-t10/ | Bản 2023 |
| OWASP Authentication Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html | commit 2026-10-04 |
| OWASP Session Management Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html | commit 2026-10-04 |
| OWASP CSRF Prevention Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html | commit 2026-10-22 |
| OWASP Password Storage Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html | commit 2026-10-04 |
| OWASP Multifactor Authentication Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html | commit 2026-10-04 |
| OWASP Authorization Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html | commit 2026-10-09 |
| OWASP Logging Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html | commit 2026-10-22 |
| OWASP Secrets Management Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html | commit 2026-10-04 |
| OWASP REST Security Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html | commit 2026-10-14 |
| OWASP Transaction Authorization Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html | commit 2026-10-18 |
| OWASP HTTP Headers Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html | commit 2026-10-04 |
| OWASP Docker Security Cheat Sheet | https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html | commit 2026-10-14 |
| OWASP Threat Modeling Cheat Sheet (STRIDE) | https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html | trang hiện hành |
| NIST SP 800-63B-4 | https://pages.nist.gov/800-63-4/sp800-63b.html ; https://csrc.nist.gov/pubs/sp/800/63/b/4/final | Final, CSRC ghi 2025-07-31 |
| NIST SP 800-53 Rev. 5.2.0 (AC-2, AC-5, AC-6, AU-9, AU-10), bản OSCAL | https://github.com/usnistgov/oscal-content | 5.2.0; metadata last-modified 2026-10-22 |
| Telegram Bot API (setWebhook, Update, Message) | https://core.telegram.org/bots/api ; changelog https://core.telegram.org/bots/api-changelog | Bot API 10.3, 2026-08-24 |
| Telegram Webhooks guide | https://core.telegram.org/bots/webhooks | trang hiện hành (IP `149.154.160.0/20`, `91.108.4.0/22`) |
| Telegram Bots FAQ (giới hạn gửi) | https://core.telegram.org/bots/faq | trang hiện hành |
| Telegram Bot features (deep linking, privacy mode, Bot-to-Bot, `/token`) | https://core.telegram.org/bots/features | trang hiện hành |
| Binance USDⓈ-M General Info (SIGNED, `recvWindow`, rate limit, 418) | https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info | docs mới; changelog mới nhất 2026-10-23 |
| Binance USDⓈ-M WebSocket API General Info (`session.logon` chỉ Ed25519, `-2015`) | https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-api-general-info | như trên |
| Binance USDⓈ-M User Data Streams (listenKey 60 phút, kết nối 24 giờ) | https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams | như trên |
| Binance USDⓈ-M Change Log | https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log | mục mới nhất 2026-10-23 |
| Binance Wallet API — Get API Key Permission | https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account#get-api-key-permission | trang hiện hành |
| Binance API Key Types (Ed25519 khuyến nghị, HMAC deprecated) | https://github.com/binance/binance-spot-api-docs/blob/master/faqs/api_key_types.md | commit 2026-10-15 (repo docs Spot; áp dụng cho loại key tài khoản) |
| Binance Support — How to Create API Keys | https://www.binance.com/en/support/faq/detail/360002502072 | cập nhật 2025-03-20 |
| PostgreSQL 18 Row Security Policies | https://www.postgresql.org/docs/18/ddl-rowsecurity.html | 18 |
| PostgreSQL 18 CREATE FUNCTION (SECURITY DEFINER) | https://www.postgresql.org/docs/18/sql-createfunction.html | 18 |
| PostgreSQL 18 libpq SSL / SSL server / pg_hba / privileges | https://www.postgresql.org/docs/18/libpq-ssl.html ; https://www.postgresql.org/docs/18/ssl-tcp.html ; https://www.postgresql.org/docs/18/auth-pg-hba-conf.html ; https://www.postgresql.org/docs/18/ddl-priv.html | 18 |
| PostgreSQL versioning policy | https://www.postgresql.org/support/versioning/ | 18.6 là minor hiện hành |
| PostgreSQL security information | https://www.postgresql.org/support/security/ | danh sách CVE 2025–2026 |
| PostgreSQL 18.6 release notes | https://www.postgresql.org/docs/release/18.6/ | 2026-08-13 (18.5 không phát hành) |
| pgAudit | https://github.com/pgaudit/pgaudit | 18.0, 2025-09-24 |
| Starlette middleware (TrustedHost, HTTPSRedirect, CORS) | https://github.com/Kludex/starlette/blob/main/docs/middleware.md | nhánh main (starlette.io không truy cập được); lock dùng starlette 1.9.0 |
| Uvicorn settings (proxy headers, forwarded-allow-ips, server header) | https://github.com/Kludex/uvicorn/blob/main/docs/settings.md | nhánh main (uvicorn.org không truy cập được); lock dùng 0.54.0 |
| FastAPI Metadata and Docs URLs (`openapi_url`) | https://fastapi.tiangolo.com/tutorial/metadata/ | trang hiện hành; lock dùng FastAPI 0.156.4 |
| MDN Set-Cookie (`__Host-`, `__Secure-`) | https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie | trang hiện hành |
| Docker Compose secrets | https://docs.docker.com/compose/how-tos/use-secrets/ | trang hiện hành |
| Docker SBOM attestations | https://docs.docker.com/build/metadata/attestations/sbom/ | trang hiện hành |
| pip-audit | https://github.com/pypa/pip-audit | trang hiện hành |
| uv CLI (`uv audit`) và settings (`exclude-newer`) | https://docs.astral.sh/uv/reference/cli/ ; https://docs.astral.sh/uv/reference/settings/ | uv mới nhất 0.13.5 (2026-10-22); repo yêu cầu ≥0.13.4 |
| OSV API (truy vấn lock) | https://osv.dev/ | querybatch 2026-10-24: 0 lỗ hổng / 47 package |
| HTTPX2 trên PyPI | https://pypi.org/project/httpx2/ | mới nhất 2.16.0; wheel 2.15.1 không có provenance |
| IETF Idempotency-Key HTTP Header | https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/ | draft-08, 10/2026 (hết hạn 2027-04-25; chưa là RFC) |
| EU Delegated Regulation 2017/589 (RTS 6) Art. 12, 13, 15, 18 | https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32017R0589 | OJ L 87, 31/3/2017; dùng làm tham chiếu ngành |
| SEC Rule 15c3-5 (17 CFR 240.15c3-5) | https://www.ecfr.gov/current/title-17/section-240.15c3-5 | eCFR hiện hành; dùng làm tham chiếu ngành |
