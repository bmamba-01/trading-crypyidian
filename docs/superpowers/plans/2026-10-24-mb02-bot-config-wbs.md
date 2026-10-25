# MB-02 WBS — Bot config versioning, root+bot resolver và Admin API

**Trạng thái:** đã triển khai và kiểm chứng ngày 2026-10-24 trên nhánh worktree (unit 670, E2E MB-02 tập trung đạt); chờ review độc lập và tích hợp của controller.\
**Ngày:** 2026-10-24\
**Gói cha:** [WBS Multi-Bot](./2026-10-24-multibot-implementation-plan.md), Task 3.\
**Contract:** [Hợp đồng kiến trúc Multi-Bot](../../architecture/decisions/2026-10-24-multibot-contracts.md) mục 3 (root config/overlay), mục 6 (serialization/idempotency), mục 7 (RBAC/RLS).\
**Phụ thuộc:** MB-01 (`4871661`, `98da8d9` trên `dev`); Alembic head nền `c7a90d3f8e21`.\
**Chạy song song:** MB-03 (shared evidence/fan-out) dùng `EffectiveBotConfig` để dựng `BotDecisionContext`.

## Mục tiêu và ngoài phạm vi

Tạo phiên bản cấu hình bot (bot overlay) bất biến, có hash, có cha/revision, author/approver và thời điểm hiệu lực; resolver hợp nhất root `WorkspaceSettings` đã publish + overlay bot đã publish + strategy release đã duyệt thành `EffectiveBotConfig` xác định (deterministic) và kiểm chứng được bằng hash; Admin API quản lý bot và vòng đời draft/validate/publish/rollback với kiểm tra workspace + `BotPermission`, CSRF, MFA gần đây cho thay đổi risk, audit/outbox cùng transaction và idempotent retry. PAPER runtime dùng overlay cho entry mới, vị thế đang mở giữ lineage đã ghim.

Ngoài phạm vi: `workspace_max_open_risk_fraction`/`bot_max_open_risk_fraction` và aggregate workspace risk (MB-04); chạy strategy release khác root theo từng bot / fan-out evidence (MB-03); lifecycle bot độc lập (pause/kill, enable), lease/fencing, scheduler (MB-05); model release (MB-08); Admin Web/Telegram (MB-07); tùy chọn thông báo trong overlay (chưa có consumer). Không gọi Binance, không đọc/ghi key sàn, chỉ PAPER.

## Rulings từ code discovery

1. **Hard cap gốc nằm ở risk profile:** `WorkspaceSettings.risk` chỉ chứa `profile_version_id`; hard caps nằm trong `RiskProfileVersion` (đọc qua `risk_limits_from_profile`/`RuntimeRiskProfile.limits`). Resolver nhận đúng 4 tham số vị trí của brief (`workspace_settings, bot_version, account, as_of`) và thêm tham số keyword bắt buộc `root_risk_profile: RuntimeRiskProfile` cùng `strategy_releases: Sequence[StrategyRelease]`; hàm async `load_effective_bot_config(session, ...)` nạp các input này rồi gọi resolver thuần.
2. **Allowlist overlay schema v1 (extra="forbid"):** `strategy {release_id, parameters}`, `universe {symbols?, timeframes?}`, `risk {per_trade_risk_fraction?, max_aggregate_open_risk?, max_notional?, max_leverage?, max_positions?, max_symbol_notional?, max_concentration_fraction?, max_daily_loss?, max_rolling_loss?}`. Trường không có trong allowlist (venue, mode, workspace/account/bot ID, risk profile ID, credential...) bị reject, không bỏ qua. Khối `risk` là model riêng để MB-04 thêm `bot_max_open_risk_fraction` mà không đổi resolver.
3. **Khối strategy thay nguyên khối:** overlay `strategy` thay release + parameters của root như một đơn vị (không merge từng tham số); thiếu khối thì kế thừa root. Entry/confirmation timeframe vẫn theo root và phải nằm trong `universe.timeframes` nếu bot thu hẹp timeframe. Parameters kiểm như `StrategySelection` (chỉ `h03-v1` có tham số, khóa/miền hỗ trợ).
4. **Release phải đã duyệt cùng workspace:** release có trong `StrategyRegistry` (known) và có đúng một hàng `strategy_release` cùng workspace, `status='PUBLISHED'`, `content_hash` bằng hash registry (active) — cùng quy tắc với `_resolve_strategy_release_id` của runtime. Hàng của workspace khác, hàng không PUBLISHED hoặc hash lệch đều bị từ chối. Model release để MB-08.
5. **Chỉ siết, cho phép bằng:** mỗi giá trị risk overlay ≤ giá trị root tương ứng (bằng là hợp lệ); `max_notional` còn phải ≤ `TradingAccount.max_notional`. Giá trị Decimal chỉ nhận chuỗi/`Decimal`/int (float bị từ chối), hữu hạn, dương, fraction trong (0, 1]; chuẩn hóa dạng chuỗi để hash ổn định. Effective `max_notional = min(root, account, overlay)`; các trường khác = overlay nếu có, ngược lại root.
6. **Venue/mode:** không có trong allowlist; resolver còn kiểm `account.mode == root.runtime.mode == 'PAPER'` và venue account khớp root (`binance_usdm` ↔ `BINANCE_USDM`).
7. **Trường giống bí mật:** khóa khớp `secret|token|password|credential|api_key|cookie` ở bất kỳ cấp nào (kể cả `strategy.parameters`) bị reject trước persist; thông báo lỗi chỉ nêu đường dẫn, không lặp giá trị (exception handler 422 hiện có đã bỏ `input`).
8. **Root-version pin và mismatch:** version bot ghi `base_settings_version_id/hash` (root đã dùng để validate lúc tạo/sửa/publish). Lúc resolve, overlay được kiểm lại với root đang hiệu lực tại `as_of`; nếu root đổi khiến overlay vượt cap/không còn là tập con → lỗi resolve, runtime không nhận entry mới (spec §11 “root/bot config mismatch”), không tự kẹp (clamp) âm thầm.
9. **Thời điểm hiệu lực:** version bot `effective_at = published_at`; resolver yêu cầu root và bot đều `effective_at <= as_of`, `as_of` có timezone. `EffectiveBotConfig.effective_at = max(root.effective_at, bot.effective_at)`, `resolved_at = as_of`. Hash effective loại trừ `resolved_at` để cùng cặp version luôn ra cùng hash.
10. **Draft không có hiệu lực runtime:** resolver từ chối version không PUBLISHED; policy RLS của `crypidian_trading_runtime` chỉ cho thấy hàng `status='PUBLISHED'` của đúng bot.
11. **Serialization publish:** publish/rollback bot lấy cùng advisory xact lock workspace của root publish và runtime decision (`WorkspaceSettingsRepository.lock_workspace`), rồi kiểm stale revision/parent. Bot publish không ghi `workspace_settings_version`.
12. **Đường rollback:** brief ghi `POST .../rollback`; rollback trên draft vô nghĩa nên dùng `POST /api/v1/bots/{bot_id}/config-versions/{version}/rollback`, body `{reason, expected_current_version_id}` (optimistic), tạo version PUBLISHED mới chép overlay đích, ghi `source_version_id`.
13. **`POST /api/v1/bots`:** MB-01 tạo bot bằng trigger khi INSERT PAPER `trading_account` và bot mirror name/status. Tạo bot = tạo PAPER account (`venue=binance_usdm`, `environment=local`, `status=DISABLED`, `max_notional` > 0) trong UoW admin → trigger tạo bot → trả bot. Chỉ `owner` + MFA gần đây (tạo ranh giới vốn). Không tạo config version/seed (bật bot thuộc MB-05). Idempotent theo khóa tự nhiên: cùng tên + cùng thuộc tính → 200 trả bot hiện có, khác thuộc tính → 409.
14. **Idempotency-Key:** header tùy chọn theo IETF draft-08 cho mutation config; lưu `bot_config_request(workspace_id, bot_id, account_id, operation, idempotency_key)` + fingerprint. Cùng key + cùng fingerprint → trả trạng thái hiện tại của version kết quả, không ghi audit/outbox lần hai; khác fingerprint → 422; cùng bot được tuần tự hóa bằng lock nên không có trạng thái “đang xử lý”. Không key thì vẫn an toàn nhờ stale revision/parent (409).
15. **Quyền:** list/detail → `BotPermission.READ` (list lọc theo quyền); tạo/sửa/validate draft → `CONFIG_WRITE`; publish/rollback → `CONFIG_PUBLISH`; mọi thay đổi khối risk so với version cha/hiện hành cần MFA ≤ `RECENT_MFA_LIFETIME` (MFA tương lai bị từ chối). Bot/draft khác workspace hoặc khác bot → 404 không lộ tồn tại.
16. **Runtime (Step 7):** `PaperRuntimeApplicationService` resolve `EffectiveBotConfig` cho mỗi entry decision. Bot chưa có version publish → đường root như MB-01 (overlay rỗng ngầm). Resolve lỗi → `PAPER_BOT_CONFIG_NOT_EFFECTIVE` (fail-closed). Áp risk subset, universe và parameters; release khác root → `PAPER_BOT_STRATEGY_FANOUT_REQUIRED` cho tới MB-03. Lineage bot (version ID/hash, effective hash) vào evidence intent và idempotency key; monitor ghim parameters theo version bot trong intent.
17. **Grant/RLS:** `bot_config_version` ENABLE+FORCE RLS; runtime chỉ SELECT hàng PUBLISHED của `app.bot_id`; admin_web SELECT/INSERT/UPDATE theo `_member_policy` MB-01; `bot_config_request` chỉ admin_web SELECT/INSERT; REVOKE khỏi `PUBLIC`/`crypidian_app` (gateway/ingestion/trainer default-deny).
18. **Bất biến:** trigger chặn DELETE, mọi UPDATE hàng PUBLISHED, đổi identity (`id, workspace, bot, account, version, parent, source, created_by, created_at`) và đổi nội dung khi DRAFT→PUBLISHED; `bot_config_request` append-only.
19. **Outbox key:** key gồm revision (`bot.config.draft_updated:{id}:{revision}`) để nhiều lần sửa cùng draft không đụng unique `(workspace_id, event_type, idempotency_key)`.

20. **Draft chưa hợp lệ vẫn lưu được:** như root settings, tạo/sửa draft chỉ kiểm schema/allowlist/secret; ràng buộc so với root (nới cap, release, universe) do `validate` báo lỗi theo trường và `publish`/`rollback` từ chối 422. Draft không có hiệu lực runtime nên không nới được cap.
21. **Test phụ thuộc giờ hệ thống (phát hiện khi chạy):** `tests/unit/test_risk_reservations.py` cố định `candidate_expires_at = 2026-10-24 13:05 UTC` nhưng `RiskReservationBook` dùng đồng hồ thật, nên 9 test fail sau 13:05 UTC ngày 24/10/2026 (không liên quan MB-02). Sửa bằng clock cố định trong test, không đổi source.

## Kết quả triển khai 2026-10-24

- **Unit:** 670 passed (baseline 534; +136 test mới/điều chỉnh). Ruff check/format, mypy (105 files), `git diff --check`, `alembic heads` = `d2b4f6a8c1e3` đạt.
- **E2E:** `tests/e2e/test_multibot_config.py` (Compose PostgreSQL thật): downgrade/upgrade trống, `alembic check`; owner/MFA/root; bot create + replay + 409; CSRF 403; cross-scope 404; secret/venue 422; validate lỗi theo trường; publish 422 khi nới cap hoặc release RETIRED; stale revision/parent 409; Idempotency-Key replay/422; entry v1 risk budget 50 (0,5% của 10.000); publish v2 khi vị thế mở, root không đổi; entry mới `BOT_MARKET_SCOPE_MISMATCH`; vị thế cũ thoát `STOP_LOSS` với pin v1/root; rollback lineage + replay; audit/outbox; RLS admin theo role/grant, runtime chỉ thấy PUBLISHED, UPDATE 42501; trigger bất biến; quyền bảng; downgrade guard.

## Bảng dữ liệu

| Bảng | Cột chính | Ràng buộc |
| --- | --- | --- |
| `bot_config_version` | workspace/bot/account, version, parent/source version, schema_version, `overlay_json`/`overlay_hash`, `base_settings_version_id/hash`, status, revision, created_by/updated_by/approved_by, published_at/effective_at | FK ghép `(workspace_id, bot_id, account_id)` → `bot_instance(workspace_id, id, trading_account_id)`; FK cha/source cùng bot; FK root version cùng workspace; FK membership; unique `(workspace_id, bot_id, version)`; CHECK trạng thái/hash SHA-256; index `(workspace_id, bot_id, status, effective_at)` và `ix_bot_config_version_bot_scope`; trigger bất biến. |
| `bot_config_request` | workspace/bot/account, operation, idempotency_key, request_hash, bot_config_version_id, created_by | unique `(workspace_id, bot_id, operation, idempotency_key)`; FK scope bot/account và version cùng bot; CHECK operation/key/hash; append-only. |

Migration `d2b4f6a8c1e3` (down `c7a90d3f8e21`); downgrade bị chặn `MB02_DOWNGRADE_REQUIRES_EXPORT_OF_BOT_CONFIG` nếu còn version.

## Work breakdown và nghiệm thu

### MB02.1 — Overlay schema và resolver (unit, RED trước)

- [x] Test kế thừa root khi thiếu khối; allowlist (trường lạ, venue/mode/workspace bị reject); hash canonical ổn định và chuẩn hóa Decimal; root-version pin; effective time (root/bot chưa hiệu lực, as_of naïve); draft bị từ chối; secret ở mọi cấp bị reject.
- [x] Test chặn nới risk cap root/account, đổi venue/mode, release inactive/unknown/workspace khác, scope account/bot/workspace lệch; biên bằng và Decimal (`0.0100` = `0.01`, float bị từ chối).
- [x] `config/bot_settings.py`, `application/bot_config_resolver.py`, `EffectiveBotConfig` (trường typed: workspace/bot/account, root version ID/hash, bot version ID/hash, effective_at/resolved_at, strategy → `StrategySelection`, `RiskLimits`, universe, effective hash + verify).

**Nghiệm thu:** cùng input → cùng hash; overlay không thể nới cap; lỗi luôn fail-closed.

### MB02.2 — Model, migration, repository

- [x] RED: metadata có hai bảng mới, FK ghép, index, unique, CHECK; repository lọc workspace+bot, get_effective theo `as_of`, next version, lock.
- [x] Model `BotConfigVersion`/`BotConfigRequest` trong `bot_entities.py`; migration RLS/grant/trigger/downgrade guard; `BotConfigVersionRepository`.

**Nghiệm thu:** `alembic heads` = `d2b4f6a8c1e3`; `alembic check` sạch trong E2E; bất biến do DB chặn.

### MB02.3 — Admin service/API

- [x] RED API/service: CSRF, RBAC (owner/strategy_admin/risk_admin/operator có-không grant/analyst), MFA risk, stale revision/parent, idempotent retry (cùng/khác fingerprint), audit+outbox cùng UoW, cross-bot/cross-workspace 404, validate trả lỗi field, bot create owner-only + replay.
- [x] `admin/bot_schemas.py`, `bot_service.py`, `bots_router.py`; nối `app.py`.

**Nghiệm thu:** mọi mutation có audit + outbox; publish bất biến, tuần tự; root settings không đổi.

### MB02.4 — Runtime PAPER

- [x] RED: entry dùng risk/universe/parameters overlay; bot không version giữ hành vi cũ; resolve lỗi fail-closed; release khác root bị chặn; idempotency/evidence có lineage bot; monitor ghim parameters theo version trong intent.
- [x] Nối `paper_decision_cycle.py`, `paper_runtime_application.py`, `app.py`.

### MB02.5 — Compose E2E và quality gates

- [x] `tests/e2e/test_multibot_config.py` + probe qua stdin: owner bootstrap/MFA, root settings, Admin API bot create/draft/validate/publish/rollback/idempotency/CSRF/MFA/cross-workspace; runtime mở vị thế PAPER với overlay v1 (risk siết được áp), publish v2 khi vị thế mở, entry mới dùng v2, vị thế cũ giữ lineage v1 và thoát stop; root không đổi; overlay nới cap bị 422; RLS runtime không thấy draft, gateway default-deny; bất biến trigger.
- [x] Unit đầy đủ, Ruff check/format, mypy, `git diff --check`, `alembic heads`, E2E tập trung.

## Nguồn đã kiểm tra (2026-10-24)

- IETF [draft-ietf-httpapi-idempotency-key-header-08](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) (22/10/2026, Active Internet-Draft): fingerprint tùy chọn; tái dùng key với payload khác → 422; request trùng đang xử lý → 409.
- [RFC 9110 §9.2.2 Idempotent Methods](https://www.rfc-editor.org/rfc/rfc9110.html): PUT là idempotent theo ngữ nghĩa — dùng PUT cho sửa draft với optimistic revision.
- PostgreSQL 18 [CREATE POLICY](https://www.postgresql.org/docs/current/sql-createpolicy.html), [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html), [Trigger Functions](https://www.postgresql.org/docs/current/plpgsql-trigger.html): policy theo role đích, FORCE RLS, trigger BEFORE UPDATE/DELETE để bảo vệ lịch sử.
- Thư viện trong venv: Pydantic 2.14.1, FastAPI 0.156.4, SQLAlchemy 2.1.5, Alembic 1.20.0 (không đổi dependency).

## Thuật ngữ

- **Effective configuration — cấu hình hiệu lực:** kết quả hợp nhất root + overlay + release đã duyệt tại một thời điểm, có lineage/hash.
- **Idempotency key — khóa chống lặp:** khóa do client gửi để retry trả cùng kết quả thay vì tạo thay đổi mới.
- **Request fingerprint — dấu vân tay yêu cầu:** hash payload chuẩn hóa để phát hiện tái dùng key với nội dung khác.
- **Tighten-only — chỉ được siết:** giá trị con chỉ có thể nhỏ hơn hoặc bằng giới hạn cha.
