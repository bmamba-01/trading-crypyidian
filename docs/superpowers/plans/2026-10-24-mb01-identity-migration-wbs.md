# MB-01 WBS — Bot identity, account binding, grants và migration

**Trạng thái:** đã triển khai và kiểm chứng ngày 2026-10-24 (unit 534, E2E 14/14, review độc lập đã xử lý); chờ người dùng duyệt commit trên `dev`.  
**Ngày:** 2026-10-24  
**Gói cha:** [WBS Multi-Bot](./2026-10-24-multibot-implementation-plan.md), Task 2.  
**Contract:** [Hợp đồng kiến trúc Multi-Bot](../../architecture/decisions/2026-10-24-multibot-contracts.md).  
**Phụ thuộc:** MB-00 đã hoàn tất tại commit `8158d9e` trên `dev`.

## Mục tiêu và ngoài phạm vi

Tạo identity một-bot/một-account, quyền bot-scoped, composite lineage và migration an toàn từ schema WP-02; bảo đảm các luồng PAPER hiện có tiếp tục ghi đúng bot và RLS không cho phép truy cập chéo bot. Thay đổi runtime ở đây chỉ để truyền identity/scope cần cho persistence và RLS; scheduler, lease/fencing, multi-bot pause/recovery và activity reconciliation thuộc MB-05.

Không thêm Binance private execution, bot config versions, shared evidence, workspace risk aggregate, reports, web/Telegram hoặc model/knowledge tables trong WP này.

## Rulings từ code discovery

1. **Nguồn account→bot:** lưu mapping duy nhất ở `BotInstance`; không thêm `bot_id` ngược vào `TradingAccount` để tránh FK vòng. `BotInstance` có `UNIQUE(workspace_id, trading_account_id)` cùng unique target `(workspace_id, id, trading_account_id)` cho FK composite.
2. **Trạng thái backfill:** với account mode `PAPER`, bot mặc định kế thừa `TradingAccount.status` (`ENABLED` hoặc `DISABLED`) để không đổi hành vi runtime hiện có; account mode ngoài `PAPER` làm migration dừng fail-closed. `BotInstance` không nhân đôi `mode`; `TradingAccount.mode` là nguồn dữ liệu canonical.
3. **Ràng buộc lineage:** FK `(workspace_id, bot_id, account_id)` phải tới cùng một mapping `(workspace_id, BotInstance.id, BotInstance.trading_account_id)`. Không dùng hai FK riêng, vì chúng cho phép bot A gắn nhầm account B trong cùng workspace.
4. **Payload lịch sử:** `bot_id` là cột quan hệ mới. Không chèn bot ID vào historical canonical JSON, decision/plan hash, intent lineage, checkpoint payload hoặc snapshot hash trong WP này.
5. **RLS:** không thêm policy permissive thứ hai bên cạnh `workspace_isolation`, vì nhiều permissive policies ghép bằng OR. Thay policy hiện hữu bằng một predicate workspace AND bot-scope hoặc cấu hình restrictive policy có test chứng minh tương đương.
6. **Grant DB:** `crypidian_app` được kế thừa bởi nhiều process role; không cấp DML rộng cho BotInstance/BotGrant qua group đó. Cấp quyền trực tiếp tối thiểu theo DB role, kiểm tra `has_table_privilege` bằng process roles thật.
7. **Account bootstrap:** unique chỉ cấm duplicate bot/account, không bảo đảm account mới có bot. `PaperAccountSeedRepository` phải tạo/resolve account và BotInstance trong cùng transaction rồi mới ghi seed.
8. **Legacy rows:** `config_version` vẫn giữ làm lineage lịch sử, được gắn bot qua account mapping; `approval_request.trading_account_id` nullable nên `bot_id` cũng nullable và hai cột phải cùng null/cùng khác null. Backtest/research rows không có account ID thì giữ workspace-only.
9. **Trigger:** migration phải tạm disable đúng các trigger bảo vệ immutable/state-transition trên account-scoped tables, chỉ cập nhật `bot_id`, validate dữ liệu, rồi enable lại trước khi transaction kết thúc. Không dùng `session_replication_role` hoặc tắt trigger toàn schema.
10. **Role scope:** enum role hiện có `OWNER`, `OPERATOR`, `STRATEGY_ADMIN`, `RISK_ADMIN`, `ANALYST_REVIEWER`, lưu DB bằng lowercase `owner`, `operator`, `strategy_admin`, `risk_admin`, `analyst_reviewer`; không có literal role `ADMIN`. `OWNER`, `STRATEGY_ADMIN`, `RISK_ADMIN` có workspace-wide bot row visibility; các quyền hành động vẫn phải được kiểm theo application permission. `OPERATOR`/`ANALYST_REVIEWER` cần BotGrant theo bot và permission.

11. **Account creation invariant:** trigger AFTER INSERT tạo identity cùng transaction bằng role NOLOGIN chỉ có INSERT bot_instance; SECURITY DEFINER pin search_path=pg_catalog, pg_temp (pg_temp cuối theo khuyến nghị PostgreSQL 18), qualify public tables, revoke EXECUTE từ PUBLIC. Runtime repository resolve mapping do trigger tạo, không race INSERT.
12. **Default theo transaction:** bot_id NOT NULL có server default lấy app.bot_id, thiếu context vẫn NULL và bị NOT NULL/RLS chặn; app repositories vẫn truyền identity tường minh. ApprovalRequest không có default để workspace-only approval giữ cặp NULL.

13. **Bot mirror account (MB-01):** trigger `provision_account_bot` đồng bộ `name`/`status` từ `TradingAccount` sang bot khi account đổi, để account tạo DISABLED rồi bật vẫn chạy được và downgrade guard không kẹt. Lifecycle bot độc lập (pause/kill) thuộc MB-05 và phải xem lại quy tắc này.
14. **Role cấp cluster:** `crypidian_bot_provisioner` tạo có điều kiện; nếu đã tồn tại mà có LOGIN/SUPERUSER/BYPASSRLS/REPLICATION/CREATEROLE/CREATEDB hoặc có thành viên thì migration dừng `MB01_PROVISIONER_ROLE_UNSAFE` (policy của role là `USING (true)`). Downgrade chỉ xóa role khi không còn phụ thuộc ở database khác. Hàm SECURITY DEFINER pin `search_path = pg_catalog, pg_temp`.
15. **Approval workspace-only:** `approval_request` có `bot_id IS NULL` chỉ được ĐỌC qua policy `FOR SELECT` riêng (runtime cùng workspace; admin có membership ACTIVE). Ghi vẫn bot-strict: runtime/operator không sửa hàng NULL và không chuyển hàng của bot thành NULL; chỉ role workspace-wide ghi được hàng NULL.
16. **Tiến trình khác:** `execution_gateway`, `ingestion_worker`, `trainer_worker` không có policy trên 13 bảng bot-scoped (default-deny). WP-03/MB-08 phải thêm policy tường minh khi cần.
17. **Bộ lọc theo account tương đương bot:** truy vấn runtime/read model lọc `account_id` + RLS bot; vì account↔bot 1:1 có unique và FK ghép, không thêm `bot_id == uow.bot_id` (khi UoW chưa bind sẽ thành `IS NULL` và âm thầm rỗng).

## Bảng dữ liệu và lineage

| Nhóm | Bảng | Cột account hiện tại | Hành động MB-01 |
| --- | --- | --- | --- |
| Bot identity | `bot_instance` | `trading_account_id` | Tạo `BotInstance`; unique account binding, unique composite target, FK account/workspace. |
| Bot grants | `bot_grant` | — | Tạo grant theo membership+bot+permission; lưu grantor/revocation; composite FK cùng workspace. |
| PAPER capital/config lịch sử | `paper_account_seed`, `config_version` | `trading_account_id` | Thêm `bot_id NOT NULL`, backfill theo `(workspace_id,trading_account_id)`, composite FK đến mapping bot/account. |
| Account approval | `approval_request` | `trading_account_id NULL` | Thêm `bot_id NULL`; backfill khi account có giá trị; check hai scope cùng null/cùng non-null và FK composite. |
| Risk | `trade_plan`, `risk_decision`, `risk_reservation` | `account_id` | Thêm `bot_id NOT NULL`; composite bot/account FK; child FK giữ bot/account từ parent. |
| PAPER ledger | `order_intent`, `exchange_order`, `fill`, `position_event`, `trade`, `trade_cost` | `account_id` | Thêm `bot_id NOT NULL`; composite bot/account FK xuyên chuỗi; sửa FK order intent→reservation hiện chỉ có workspace/reservation ID. |
| PAPER recovery | `paper_execution_checkpoint` | `account_id` | Thêm `bot_id NOT NULL`, composite mapping FK và RLS bot scope. |

Shared workspace-only, không gắn bot trong WP này: `workspace_settings_version`, `risk_profile_version`, `strategy_release`, market tables, `feature_snapshot`, outbox/consumer receipt/audit, watchlists, knowledge/research, backtest runs/trials/reports/outcomes.

## Work breakdown và điều kiện nghiệm thu

### MB01.1 — Domain model, permissions và repositories

- [x] Tạo `BotInstance` với workspace/account/name/status/timestamps; `mode` được lấy từ `TradingAccount`, không copy vào bot.
- [x] Tạo `BotGrant` với workspace/bot/membership/permission/granted_by/revoked_at và unique active grant.
- [x] Tạo enum `BotPermission` tối thiểu: `bot:read`, `bot:config:write`, `bot:config:publish`, `bot:runtime:pause`, `bot:runtime:resume`, `bot:report:read`.
- [x] Tạo `BotInstanceRepository.create_for_account/get_for_account/list_enabled_for_runtime` và `BotGrantRepository.grant/revoke/require`; luôn truyền workspace/account/bot rõ và reject mismatch.
- [x] Tạo unit tests trước source: duplicate account binding; cross-workspace FK; bot A + account B mismatch; active/revoked/missing grant; grant reissue; owner/admin-role bypass theo permission map.

**Nghiệm thu:** mỗi account có tối đa một bot, repository không lookup chỉ bằng unscoped ID, grant revoked/missing không cấp quyền, permission string lạ bị từ chối.

### MB01.2 — Expand/backfill/contract migration

- [x] Tạo bảng bot identity/grant, unique/FK/check/index và role grants tối thiểu.
- [x] Trước backfill, assert mọi `TradingAccount.mode = 'PAPER'`; unsupported mode phải rollback migration, không đổi mode ngầm.
- [x] Tạo một bot cho mỗi account, status mirror `TradingAccount.status`, tên mặc định account name; account ID/workspace ID và mọi payload hiện hữu giữ nguyên.
- [x] Thêm nullable `bot_id`; backfill theo account mapping. Đối với approval workspace-scoped giữ `bot_id = NULL`.
- [x] Disable đúng trigger names trên bảng immutable/transition-guarded trong cùng migration transaction; chỉ cập nhật bot column; kiểm orphan/duplicate; restore trigger trước khi thêm NOT NULL/FK.
- [x] Tạo composite unique/FK đầy đủ cho bot-account và child-parent. Làm mạnh FK order intent→risk reservation thêm bot/account scope.
- [x] Enforce NOT NULL cho mọi account-scoped row đã xác định; approval request dùng CHECK/FK có điều kiện.
- [x] Thay workspace RLS policy bằng workspace+bot scope (không thêm permissive policy làm OR rộng); context không có bot/member phải default-deny trên bot-scoped rows.
- [x] Dùng grants trực tiếp cho `crypidian_admin_web` và `crypidian_trading_runtime` theo nhu cầu; revoke khỏi `crypidian_app` trên bảng bot mới trước khi cấp direct least privilege. Xác minh quyền thực tế của inherited process roles.
- [x] Downgrade guard từ chối nếu có BotGrant hoặc bot state tùy chỉnh không thể tái dựng; trên migration-only default mapping, downgrade bỏ bot columns/tables mà không đổi account, business payload, PnL hay canonical hashes.

**Nghiệm thu migration:** seeded WP-02 account PAPER được map đúng một bot; số dòng/PnL/canonical payload trước và sau bằng nhau; không sót/orphan; trigger được khôi phục và tiếp tục chặn sửa append-only; mismatch bot/account bị DB từ chối; upgrade→downgrade→upgrade trên DB cô lập đạt; payload mới không bị mất khi downgrade bị guard.

### MB01.3 — Transaction context và quyền truy cập

- [x] Mở rộng `UnitOfWork` với optional `bot_id`, đặt transaction-local `app.workspace_id`, `app.bot_id`, `app.membership_id`; từ chối scope mismatch.
- [x] RLS bot-scoped data cho phép đúng một bot trong runtime; membership role owner/workspace-admin được truy cập workspace theo policy; operator/reviewer chỉ thấy bot có grant. Action permission vẫn được kiểm trong application.
- [x] Cập nhật AdminSettings UoW để role được ủy quyền đọc đầy đủ exposure sibling bot khi validate root risk configuration.
- [x] `BotPermission` gate dùng role literal hiện hành; không giả định có role tên `ADMIN`.
- [x] Unit tests cho UoW set/reset local context, thiếu scope fail-closed, wrong workspace/bot reject, owner/strategy-admin/risk-admin visibility, operator grant and revoke.

**Nghiệm thu:** bot ID từ request không tự cấp quyền; transaction pooling không làm rò context; missing scope không thấy bot rows; process DB privileges và RLS đều pass.

### MB01.4 — Chuyển các đường ghi/đọc PAPER hiện hữu

- [x] Account seed/bootstrap tạo account + bot + seed atomically; account retry resolve cùng binding và không tạo bot thứ hai.
- [x] `RiskReservationRepository` populate/validate bot ID cho plan/decision/reservation và các mutation release/expire/transfer/reconcile.
- [x] `PaperLedgerRepository` populate/validate bot ID ở intent; child order/fill/position/trade/cost lấy bot từ parent và query mutation có bot filter.
- [x] `PaperCheckpointRepository` đọc/ghi/verify theo workspace+bot+account; không thay payload/snapshot/event hash.
- [x] Read models account state/bootstrap dùng mapping bot rõ ràng.
- [x] `PaperRuntimeAccount` giữ bot ID; bootstrap đặt bot scope trước hydrate từng account; runtime command UoW dùng bot ID. Giữ mô hình hiện có per-account; chưa thêm lease/fencing/pause behavior của MB-05.
- [x] `config_version` gắn bot lineage nhưng `BotConfigVersion` table mới thuộc MB-02.
- [x] Không thêm bot ID vào `_intent_lineage()` hoặc canonical payload cũ.

**Nghiệm thu:** full unit/runtime regression giữ H03 behavior; mismatch scope reject; retry/recovery historical lineage vẫn khớp; sau migration mọi insert account-scoped đều có bot ID.

### MB01.5 — PostgreSQL Compose E2E và quality gates

- [x] E2E dùng một PostgreSQL Compose project cô lập, upgrade từ head WP-02 `b2e7a9d4c631`; seed historical rows bằng raw SQL trước upgrade.
- [x] Snapshot row counts, trade PnL/fees/status, canonical JSON/hash và checkpoint payload trước migration; so sau migration.
- [x] Chạy dưới role `crypidian_admin_web`, `crypidian_trading_runtime` và scoped membership thật: workspace mismatch, bot A→bot B SELECT/INSERT/UPDATE, no bot context, owner/admin workspace visibility, operator grant/revoke, account/bot mismatch, duplicate binding, runtime privileges.
- [x] Kiểm trigger enablement và append-only/state-transition rejection sau migration.
- [x] Kiểm downgrade guard, downgrade/re-upgrade DB cô lập; giữ nguyên image E2E dùng chung và chỉ dọn tài nguyên theo Compose project label.
- [x] Chạy unit đầy đủ, E2E đầy đủ, Alembic heads/check/downgrade/upgrade, Ruff check/format, mypy và diff checks.
- [x] Independent task review; mỗi finding được sửa qua test-first và test lại.
- [x] Cập nhật/read-back implementation memory, project status và main WBS trước commit MB-01 trên `dev`.

## Kết quả triển khai 2026-10-24

- **Index:** mỗi bảng bot-scoped có `ix_<table>_bot_scope (workspace_id, bot_id, account)` cho predicate RLS và FK ghép (PostgreSQL không tự index phía con).
- **Review độc lập:** không P0. Đã sửa P2: user nhiều role mượn bypass workspace (`BotGrantRepository.require`); bot không đồng bộ account (ruling 13); `CREATE ROLE` không guard (ruling 14); approval workspace-only bị RLS che rồi lộ quyền ghi ở bản sửa đầu (ruling 15, review lại); guard đổi PAPER execution profile phụ thuộc RLS (nay 403 khi principal không có role workspace-wide). P3 đã sửa: cache scope luôn đặt lại GUC, `create_for_account` chỉ resolve mapping do trigger tạo, bỏ UPDATE `bot_instance` của admin_web, assert phép thay chuỗi trong policy, role provisioner có sẵn nhưng không an toàn (ruling 14, review lại). P3 chấp nhận: ruling 16, 17.
- **E2E cũ được sửa fixture:** expiry probe chạy bằng `trading-runtime` (reservation thuộc runtime, ruling 6); fixture exit-fill seed chuỗi plan→decision→reservation thật để FK ghép hợp lệ khi re-upgrade; H03 seed và compose probe đặt bot scope, account race dùng UoW riêng (một transaction một bot scope).
- **Windows:** E2E multibot truyền probe qua stdin vì giới hạn dòng lệnh 32.767 ký tự.
- **Kiểm chứng:** unit 534 passed; E2E 14 passed / 452.08s; Ruff, format, mypy, `alembic heads` (`c7a90d3f8e21`), `alembic check`, `git diff --check` đạt.

## Gate tổng WP

MB-01 chỉ nghiệm thu khi toàn bộ account có một binding bot đúng; migrations preserve immutable history; bot/account/workspace constraints không cho gắn chéo; policy RLS default-deny khi thiếu scope và không có permissive-policy OR bypass; app grants/action permission đúng; tất cả write/read path PAPER tương thích; unit+Compose E2E+quality gates pass; independent review không còn P0/P1/P2 nghiêm trọng; memory và status đồng bộ.

## Nguồn và thuật ngữ

- PostgreSQL 18 official docs, [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) và [REVOKE/role privileges](https://www.postgresql.org/docs/current/sql-revoke.html), kiểm tra 2026-10-24. Permissive policies ghép OR, restrictive policies ghép AND; privilege hiệu lực có thể đến từ direct grant, group role hoặc PUBLIC nên cần kiểm `has_table_privilege`.
- **Bot instance — thực thể bot:** identity riêng nối account, trạng thái, grant và lịch sử; v1 một account/bot.
- **Composite foreign key — khóa ngoại ghép:** ràng buộc nhiều cột để cùng xác nhận workspace, bot và account thuộc đúng mapping.
- **Row-level security (RLS) — bảo mật cấp hàng:** PostgreSQL giới hạn hàng đọc/ghi theo policy và scope transaction; không thay thế authorization hành động ở application.
