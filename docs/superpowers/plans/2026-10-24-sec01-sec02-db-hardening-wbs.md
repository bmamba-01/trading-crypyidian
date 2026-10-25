# SEC-01/SEC-02 WBS — Quyền DB tối thiểu theo process và PostgreSQL 18.6

**Trạng thái:** đã triển khai trên nhánh worktree (2026-10-24/25); chờ controller tích hợp.\
**Gói cha:** [Nghiên cứu bảo mật Admin/bot](../../research/security-admin-bot-2026.md) mục 3.1 và 4.0 (SEC-01, SEC-02, P0).\
**Liên quan:** [MB-01 WBS, rulings 6, 14, 16](./2026-10-24-mb01-identity-migration-wbs.md), [hợp đồng Multi-Bot](../../architecture/decisions/2026-10-24-multibot-contracts.md).\
**Revision Alembic:** `f4d6b8c0e2a5` (down `c7a90d3f8e21`; controller nối lại chuỗi khi tích hợp với MB-02/MB-03).

## 1. Mục tiêu và ngoài phạm vi

- **SEC-01:** process role không thừa hưởng quyền ghi bảng identity/auth/config qua group `crypidian_app`. Quyền trên 16 bảng identity/config được cấp trực tiếp theo đường code thật của từng process; policy `workspace_isolation` (mặc định `PUBLIC`) trên các bảng đó được giới hạn `TO <role>` dự định, giữ nguyên biểu thức nên hành vi của role dự định không đổi.
- **SEC-02:** ghim `postgres:18.6-bookworm@sha256:<index digest>` trong `compose.yaml`; test tĩnh và E2E kiểm phiên bản ≥ 18.6.
- **Ngoài phạm vi:** quyền của `admin_web`/`trading_runtime` trên bảng vận hành (outbox, market, feature, ledger, backtest) vẫn đi qua `crypidian_app` (xem ruling R2, việc còn mở SEC-01b); role migrator superuser (SEC-04); TLS DB (SEC-05); không đụng container dev `crypidian-postgres-1`.

## 2. Hiện trạng đã xác minh trong code (trước SEC-01)

- `48a80bae456d…:478-489`: `crypidian_app` có `SELECT, INSERT, UPDATE, DELETE` trên 13 bảng tenant (user_membership, role_binding, session, mfa_factor, telegram_binding, trading_account, secret_reference, config_version, risk_profile_version, approval_request, watchlist_version, watchlist_item, strategy_release) và `SELECT` trên `workspace`; cả 5 process role là thành viên `crypidian_app`.
- `48a80bae456d…:490-499`: policy `workspace_isolation` không có `TO` (áp cho mọi role) và tin GUC `app.workspace_id` do chính session đặt.
- `d0b7e9a2416c…:115-119`: `crypidian_app` có `SELECT, INSERT, UPDATE` trên `workspace_settings_version`; trigger cho phép DRAFT→PUBLISHED, nên worker có thể tự chèn draft rồi publish cấu hình gốc.
- MB-01 (`c7a90d3f8e21`) đã dùng grant trực tiếp + policy `TO` cho `bot_instance`/`bot_grant` và policy theo role cho 13 bảng bot-scoped; quyền bảng của admin/runtime trên các bảng bot-scoped (trừ bảng đã có grant trực tiếp) vẫn đến từ `crypidian_app`.
- Gateway/ingestion/trainer đã có grant trực tiếp cho đúng phần việc của mình: ingestion `SELECT, INSERT, UPDATE` 7 bảng market (`e18f35a7c902…:448`), trainer `SELECT` 4 bảng market + `SELECT, INSERT` 5 bảng backtest (`9a7c2d5e1b40…:173`, `8c4f2a6d9e31…:121`), cả 5 role `INSERT audit_event` (`a63d41e7b205…:140-147`).

## 3. Đường code theo process (bằng chứng cho ma trận)

| Process (`compose.yaml`) | Đường code dùng DB | Bảng identity/config chạm tới |
| --- | --- | --- |
| `admin-web` (`crypidian_admin_web`) | `auth/service.py` bootstrap/confirm/login/authenticate/verify_csrf/logout/issue_csrf/reauthenticate/create_risk_profile; `admin/settings_service.py` + `repositories/workspace_settings.py`; `repositories/bot_grants.py` (MB-01, khóa `user_membership FOR UPDATE`); `repositories/bot_instances.py` (khóa `trading_account FOR UPDATE`); E2E `WorkspaceRepository.list_trading_accounts` | workspace (S, I, U, `LOCK EXCLUSIVE`, `FOR UPDATE`), user_account (S, I, U), user_membership (S, I, `FOR UPDATE`), role_binding (S, I), session (S, I, U `revoked_at/csrf_token_hash/mfa_verified_at`), mfa_factor (S, I, `FOR UPDATE`, U `verified_at/last_used_counter`), secret_reference (I + RETURNING), risk_profile_version (S, I), workspace_settings_version (S, I, U), trading_account (S; MB-02 `e53331b` `BotInstanceRepository.create_paper_account` INSERT account PAPER, `create_for_account` khóa `FOR UPDATE`) |
| `trading-runtime` (`crypidian_trading_runtime`) | `paper_account_initialization.py` → `PaperAccountSeedRepository.ensure_seed` (INSERT `trading_account`, `SELECT … FOR UPDATE`); `paper_ledger.py:86`, `risk_reservations.py:589` (`trading_account FOR UPDATE`); `paper_runtime_application.py:596,669`, `paper_ledger.py:185` (strategy_release); `runtime_risk_profile.py:130` (risk_profile_version); `workspace_settings.py` đọc bản PUBLISHED; MB-01 probe đọc `approval_request` workspace-only (ruling 15) | trading_account (S, I, `FOR UPDATE`), strategy_release (S), risk_profile_version (S), workspace_settings_version (S), config_version/approval_request (S), workspace (S) |
| `execution-gateway` | Chưa có code venue (WP-03); lifespan không truy vấn DB | không |
| `ingestion-worker` | `repositories/market_data.py` (7 bảng market) | không |
| `trainer-worker` | `repositories/backtests.py`, `backtest_outcomes.py` | không |

Ghi chú kỹ thuật: `SELECT … FOR UPDATE` cần quyền `UPDATE` trên ít nhất một cột ([PG18 Privileges](https://www.postgresql.org/docs/18/ddl-priv.html)); ORM SQLAlchemy 2.x lấy server default bằng `INSERT … RETURNING` nên role INSERT cũng cần `SELECT` và policy SELECT.

## 4. Ma trận quyền 16 bảng identity/config

Ký hiệu: S=SELECT, I=INSERT, U=UPDATE (U(cột) = chỉ cột liệt kê), D=DELETE, — = không có.

| Bảng | Trước (mọi role qua `crypidian_app`) | admin_web sau | runtime sau | gateway/ingestion/trainer sau | Policy `workspace_isolation` sau |
| --- | --- | --- | --- | --- | --- |
| workspace (không RLS) | S (+admin I,U trực tiếp) | S, I, U | S | — | (không RLS) |
| user_account (không RLS) | admin S,I,U trực tiếp | S, I, U (giữ nguyên) | — | — | (không RLS) |
| user_membership | S,I,U,D | S, I, U(created_at) lock-only | — | — | TO admin_web |
| role_binding | S,I,U,D | S, I | — | — | TO admin_web |
| session | S,I,U,D | S, I, U(revoked_at, csrf_token_hash, mfa_verified_at) | — | — | TO admin_web |
| mfa_factor | S,I,U,D | S, I, U(verified_at, last_used_counter) | — | — | TO admin_web |
| telegram_binding | S,I,U,D | S | — | — | TO admin_web |
| secret_reference | S,I,U,D | S, I | — | — | TO admin_web |
| trading_account | S,I,U,D | S, I, U(updated_at) lock-only | S, I, U(updated_at) lock-only | — | TO admin_web, runtime |
| config_version (MB-01 bot-scoped) | S,I,U,D | S | S | — | policy MB-01 giữ nguyên |
| risk_profile_version | S,I,U,D | S, I | S | — | TO admin_web, runtime |
| approval_request (MB-01 bot-scoped) | S,I,U,D | S | S | — | policy MB-01 giữ nguyên |
| watchlist_version | S,I,U,D | S | — | — | TO admin_web |
| watchlist_item | S,I,U,D | S | — | — | TO admin_web |
| strategy_release | S,I,U,D | S | S | — | TO admin_web, runtime |
| workspace_settings_version | S,I,U (+admin S,I,U; runtime S trực tiếp) | S, I, U (giữ nguyên) | S (giữ nguyên) | — | TO admin_web, runtime |

`crypidian_app` sau migration: **không còn quyền nào** trên 16 bảng trên; vẫn giữ quyền bảng vận hành; thành viên chỉ còn `admin_web`, `trading_runtime`.

## 5. Rulings

- **R1 — Bỏ membership `crypidian_app` của gateway/ingestion/trainer.** Ba role này đã có grant trực tiếp cho đúng đường code (mục 2); membership chỉ mở thêm DML outbox/ledger/market/backtest và identity. Cấp thêm `USAGE ON SCHEMA public` trực tiếp để không phụ thuộc `PUBLIC`. Ingestion-worker (xử lý nội dung Internet) là mối đe dọa chính trong threat model.
- **R2 — Giữ tạm membership cho admin_web/runtime nhưng `crypidian_app` không còn quyền identity/config.** Hai role này vẫn dùng group cho bảng vận hành (admin đọc ledger/reservation để tính exposure; E2E compose chạy probe market/outbox trong container admin-web; MB-01 probe `SET LOCAL ROLE crypidian_app` của runtime). Rút hẳn group là SEC-01b (cần chuyển probe market/outbox sang đúng process và cấp trực tiếp tối thiểu).
- **R3 — Quyền theo đường code thật; admin_web giữ quyền ĐỌC (đã chịu RLS) trên bảng control-plane, chỉ GHI ở bảng có code ghi; không role nào có DELETE trên bảng identity/config.** Cột cho phép UPDATE được thu hẹp về cột code thật sự sửa: admin không đổi được `session.expires_at/token_hash`, `mfa_factor.secret_reference`, bất kỳ cột nghiệp vụ nào của `trading_account` (kể cả cap `max_notional`, `status`) hay `user_membership` (kể cả `status`, thuộc predicate RLS MB-01).
- **R4 — Runtime chỉ `UPDATE(updated_at)` trên `trading_account`** để giữ khóa hàng `SELECT … FOR UPDATE` (PG yêu cầu UPDATE trên ít nhất một cột); runtime không còn sửa được `max_notional`/`status`/`mode`.
- **R10 — (fix round 1, review) admin_web chỉ có cột lock-only trên `trading_account` (`updated_at`) và `user_membership` (`created_at`).** Đã đối chiếu code `src/` và MB-02 (`e53331b`: `admin/bot_service.py`, `repositories/bot_instances.py`, probe `multibot_config.py`): admin chỉ INSERT account PAPER (giữ theo ruling controller, H03 seed cũng dùng) và khóa hàng `FOR UPDATE` (`bot_instances.py`, `bot_grants.py`); không có UPDATE cột account/membership. Bật/tắt account, đổi cap hay tạm ngưng thành viên (MB-05/MB-07) phải thêm grant cột trực tiếp cùng code đó.
- **R5 — Runtime/admin chỉ ĐỌC `config_version`/`approval_request`.** Không có đường code ghi; policy MB-01 (ruling 8, 15) giữ nguyên. Probe MB-01 đổi kỳ vọng UPDATE hàng workspace-only của runtime từ `rowcount 0` sang `42501` (mạnh hơn: không có quyền bảng). Luồng maker-checker (MB-07) phải cấp grant trực tiếp khi có code.
- **R6 — Policy cũ `workspace_isolation` chuyển `TO <role>` bằng `ALTER POLICY … TO`, giữ nguyên USING/WITH CHECK;** role không có policy thì default-deny (giống ruling 16 MB-01). Không thêm policy permissive mới (ruling 5).
- **R7 — Downgrade khôi phục đúng trạng thái trước** (grant bảng/cột, membership, schema usage, policy `TO PUBLIC`); E2E so sánh snapshot ACL (`aclexplode` bảng/cột/schema, `pg_policy`, `pg_auth_members`) trước upgrade và sau downgrade.
- **R8 — Membership role là đối tượng cấp cluster;** migration thu hồi membership ảnh hưởng mọi database trong cluster. Trên PostgreSQL < 18.6, CVE-2026-14666 khiến plan cache có thể giữ trạng thái RLS cũ sau thay đổi membership → nâng 18.6 (SEC-02) và khởi động lại process sau migration.
- **R9 — SEC-02 ghim digest của multi-arch OCI index** (`docker buildx imagetools inspect`), không ghim manifest một kiến trúc; minor upgrade 18.x không cần dump/restore hay `pg_upgrade` (release notes 18.6).

## 6. Work breakdown và nghiệm thu

1. [x] WBS này (ma trận, rulings, nguồn).
2. [x] RED: unit/static `tests/unit/test_compose_postgres_image.py` (image `postgres:18.N-bookworm@sha256:<64 hex>`, N ≥ 6) và `tests/unit/test_db_role_least_privilege_migration.py` (revision/down_revision, ma trận khai báo khớp WBS). E2E `tests/e2e/test_db_role_least_privilege.py` + probe stdin `tests/e2e/probes/db_role_least_privilege.py`: phiên bản server ≥ 18.6; ma trận `has_table_privilege`/`has_column_privilege` cho 5 role × 16 bảng × S/I/U/D; `crypidian_app` không có quyền; membership; policy roles; thử thật bằng login của từng process (worker/runtime bị `42501` khi INSERT/UPDATE role_binding, user_membership, session, mfa_factor, secret_reference, workspace_settings_version, đọc mfa_factor/session/secret_reference; admin vẫn ghi đúng cột, bị từ chối cột ngoài danh sách và DELETE; RLS chéo workspace vẫn 0 hàng); downgrade → so ACL snapshot; re-upgrade → ma trận lại đạt.
3. [x] GREEN: migration `f4d6b8c0e2a5_process_role_least_privilege.py`; `compose.yaml` → 18.6 digest; sửa kỳ vọng probe MB-01 (R5).
   - Ngoài phạm vi nhưng chặn gate unit: `tests/unit/test_risk_reservations.py` hard-code hạn candidate 2026-10-24 13:05 UTC với đồng hồ thật nên hỏng sau mốc đó; tiêm đồng hồ cố định 13:00 UTC (chỉ sửa test).
4. [x] Gates: unit đầy đủ, ruff check/format, mypy, `git diff --check`, `alembic heads` = `f4d6b8c0e2a5`; E2E mới; E2E hiện có chạy tuần tự: `test_compose_boundary.py`, `test_paper_runtime_bootstrap.py`, `test_h03_tenant_isolation.py`, `test_exit_fill_ledger.py`, `test_reservation_expiry.py`, `test_multibot_migration.py`, `test_backtest_persistence.py`, `test_backtest_outcomes_persistence.py`, `test_market_ingestion_offline.py`.
5. [x] Commit trên nhánh worktree; report cho controller.

**Nghiệm thu:** không process worker nào có quyền trên bảng identity/config; runtime chỉ đọc config và không sửa được cap/account/settings; admin flows (bootstrap, MFA, login, risk profile, settings publish/rollback, logout) đạt trong compose E2E; downgrade khôi phục ACL chính xác; server 18.6.

## 7. Nguồn (kiểm tra 2026-10-24)

- PostgreSQL Versioning Policy — 18.6 là minor hiện hành của 18: <https://www.postgresql.org/support/versioning/>
- PostgreSQL 18.6 Release Notes (phát hành 2026-08-13; 18.5 không phát hành; không cần dump/restore): <https://www.postgresql.org/docs/release/18.6/> — CVE-2026-14666 (plan cache không phản ánh thay đổi role membership/attribute với RLS); CVE-2026-14664 (tràn buffer hàm regexp match/split, không phải RLS).
- PostgreSQL Security Information: <https://www.postgresql.org/support/security/>
- PG18 Privileges (`FOR UPDATE` cần UPDATE trên ≥ 1 cột): <https://www.postgresql.org/docs/18/ddl-priv.html>
- PG18 CREATE POLICY (`TO` mặc định `PUBLIC`): <https://www.postgresql.org/docs/18/sql-createpolicy.html>; ALTER POLICY (đổi danh sách role): <https://www.postgresql.org/docs/18/sql-alterpolicy.html>
- PG18 REVOKE (revoke bảng thu hồi luôn quyền cột; quyền hiệu lực có thể đến từ group/PUBLIC): <https://www.postgresql.org/docs/18/sql-revoke.html>
- Docker Official Image `postgres:18.6-bookworm`, index digest lấy bằng `docker buildx imagetools inspect` ngày 2026-10-24: <https://hub.docker.com/_/postgres>
- OWASP ASVS 5.0 V8.4.1/V13.2.2 và NIST SP 800-53 AC-6 (least privilege) theo tài liệu nghiên cứu SEC-01.

## 8. Thuật ngữ mới (đề xuất cho glossary, controller ghi)

- **Least privilege — đặc quyền tối thiểu:** mỗi process chỉ có quyền DB đúng với đường code của nó (NIST SP 800-53 AC-6).
- **Group role — role nhóm:** role PostgreSQL không đăng nhập, quyền của nó được thành viên thừa hưởng (PG18 Role Membership).
- **Column-level privilege — quyền cấp cột:** GRANT UPDATE/SELECT trên cột cụ thể thay vì toàn bảng (PG18 Privileges).
- **Image index digest — digest chỉ mục image đa kiến trúc:** SHA-256 của OCI image index trỏ tới manifest từng kiến trúc; ghim digest này để mọi nền tảng dùng cùng bản phát hành.
