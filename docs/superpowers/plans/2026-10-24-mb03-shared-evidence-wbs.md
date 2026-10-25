# MB-03 WBS — Shared signal evidence và fan-out strategy theo bot

**Trạng thái:** WBS viết trước code ngày 2026-10-24; đã triển khai và kiểm chứng trên nhánh worktree riêng (song song MB-02), chờ controller tích hợp.\
**Ngày:** 2026-10-24\
**Gói cha:** [WBS Multi-Bot](./2026-10-24-multibot-implementation-plan.md), Task 4.\
**Contract:** [Hợp đồng kiến trúc Multi-Bot](../../architecture/decisions/2026-10-24-multibot-contracts.md) §2, §4, §6–§8.\
**Phụ thuộc:** MB-01 (`4871661`, head Alembic `c7a90d3f8e21`). MB-02 chạy song song ở worktree khác, code chưa có ở đây.

## Mục tiêu và ngoài phạm vi

Lưu một lần evidence thị trường/nghiên cứu dùng chung (trung lập, point-in-time, bất biến, scope workspace), rồi fan-out idempotent thành một job quyết định cho mỗi bot đủ điều kiện. Mỗi bot tự chạy strategy release/tham số riêng qua `StrategyRegistry` và ghi một `BotDecision` bot-scoped (CANDIDATE/ABSTAIN/VETO) có đủ lineage evidence/config/strategy/model. Evidence không bao giờ tạo plan, reservation, intent hay lệnh venue.

Ngoài phạm vi: risk/planner/reservation theo bot và aggregate workspace (MB-04); scheduler/lease/pause runtime và wiring vào `app.py` (MB-05); bảng `bot_config_version`, resolver và Admin API (MB-02); bảng model release/knowledge (MB-08); dựng `DecisionSnapshot` từ bảng market thật (MB-05 runtime wiring). Không gọi Binance, không đọc key sàn.

## Rulings từ code discovery

1. **Tách khỏi MB-02 (ruling controller):** `application/bot_dispatcher.py` định nghĩa `BotDecisionContext` frozen gồm `workspace_id`, `bot_id`, `account_id`, `bot_config_version_id`, `bot_config_hash`, `root_settings_version_id`, `root_settings_hash`, `strategy_release_id`, `strategy_parameters`, `symbols` (universe hiệu lực) và `effective_at`. Dispatcher nhận `Sequence[BotDecisionContext]` thay vì `BotInstance`; consumer outbox nhận Protocol `BotDecisionContextProvider.contexts_for(workspace_id, as_of)`. Khi tích hợp, adapter ánh xạ `EffectiveBotConfig` của MB-02 sang context. Các trường `bot_config_hash`, `symbols`, `effective_at` đều có trong `EffectiveBotConfig` theo contract §3, cần cho lineage, kiểm universe và chặn config chưa hiệu lực.
2. **`bot_config_version_id`:** cột UUID NOT NULL trên `bot_decision_job` và `bot_decision`, **chưa có FK** vì bảng `bot_config_version` chưa tồn tại ở worktree này; controller thêm FK `(workspace_id, bot_id, bot_config_version_id)` sau khi merge MB-02.
3. **Migration:** `revision = "e3c5a7b9d2f4"`, `down_revision = "c7a90d3f8e21"`; controller re-chain lên head MB-02 khi tích hợp.
4. **Evidence trung lập:** bảng `shared_signal_evidence` chỉ scope workspace, không có `bot_id`/`account_id`/quantity/notional/side/order. RLS như bảng shared `feature_snapshot`: ENABLE + FORCE, một policy `workspace_isolation`. Trigger từ chối UPDATE/DELETE.
5. **Định danh evidence (sửa ở fix round 1):** `evidence_hash = SHA-256(canonical JSON)` của schema version, workspace, symbol, setup type, ba mốc thời gian (UTC), feature snapshot ID, source refs đã sắp xếp và model release ID — dùng xác minh toàn vẹn hàng. `content_hash` = cùng payload nhưng **bỏ** `available_at`/`validity_end` (mốc caller có thể lấy từ wall-clock); `evidence_id = uuid5(namespace cố định, "<workspace_id>:<content_hash>")`. DB unique `(workspace_id, content_hash)` (cùng `(workspace_id, id)`, `(workspace_id, evidence_hash)`): detector chạy lại chỉ khác thời gian là retry, nhận lại hàng đầu tiên, không tạo hàng/outbox/job/quyết định mới; revision thật đổi nội dung nên là hàng mới. Chọn unique DB thay vì suy `available_at` trong factory vì ràng buộc áp mọi đường ghi và bảng bất biến không dọn được trùng.
6. **Provenance:** FK `(workspace_id, feature_snapshot_id)` → `feature_snapshot(workspace_id, snapshot_id)` chặn tham chiếu snapshot không tồn tại hoặc khác workspace. `source_refs` bắt buộc không rỗng, không trùng. `model_release_id` chưa có FK (bảng release model thuộc WP-06/MB-08).
7. **Mô hình thời gian:** factory `build_shared_signal_evidence` bắt buộc `feature.as_of <= observed_at`, `feature.computed_at <= available_at`, mọi source và model release có `available_at <= evidence.available_at`, và feature snapshot usable. Vì vậy `evidence.available_at <= evaluated_at` kéo theo mọi input khả dụng tại `evaluated_at`. Dispatcher bắt buộc `available_at <= evaluated_at < validity_end` trước khi ghi job; `DecisionSnapshot` (nến close/event/available, feature, macro, `decision_time`) có mốc sau `evaluated_at` thì bot ghi VETO `FUTURE_INPUT_REJECTED` mà không gọi strategy.
8. **Revision:** bản sửa của một quan sát là evidence bất biến mới (ID khác, `available_at` muộn hơn); không UPDATE bản cũ. Đánh giá lịch sử tại `evaluated_at` chỉ thấy revision có `available_at <= evaluated_at` (`get_as_of`); dispatch revision muộn cho mốc sớm hơn bị từ chối `EVIDENCE_NOT_YET_AVAILABLE`.
9. **Fan-out và transaction:** một transaction workspace ghi evidence (dedupe) và outbox event `signal.evidence.recorded.v1` (idempotency key = evidence ID) chỉ khi evidence mới. Mỗi job bot ghi trong transaction riêng có scope bot (quy tắc một transaction một bot scope của MB-01; không dùng `allow_switch`). Job key duy nhất `(workspace_id, bot_id, evidence_id, bot_config_version_id)`; job ghim `evaluated_at` và context canonical/hash. Cùng key + cùng payload trả job cũ; khác payload là conflict (contract §6). Crash giữa chừng an toàn vì retry/consumer dedupe từng job.
10. **Outbox consumer:** `OutboxRepository.claim_ready` thêm tham số tùy chọn `event_types` để consumer fan-out không claim event của consumer khác (mặc định giữ nguyên hành vi cũ). Consumer claim → load evidence (kiểm hash payload) → provider trả context hiệu lực tại `evaluated_at` đã ghim → fan-out → acknowledge (consumer receipt); lỗi → `record_failure`/backoff.
11. **Đánh giá theo bot:** dispatcher chỉ dùng `StrategyRegistry` với `StrategySelection` riêng của bot (`settings_version_id` = root `workspace_settings_version` theo ruling controller fix round 1, vì planner/FK `order_intent.settings_version_id`/ledger đọc như root; bot config version chỉ ở `bot_config_version_id`), dựng qua helper `strategy_selection_for_release` tách ra từ `paper_decision_cycle.py` để giữ cùng quy tắc tham số với root settings. Dispatcher không giữ `PaperExecutionGateway`, planner hay risk engine nên không thể tạo intent/lệnh; plan/risk theo bot thuộc MB-04.
12. **Trạng thái quyết định:** `CANDIDATE` khi strategy trả `StrategyCandidate`; `ABSTAIN` khi strategy trả `NoTrade` hoặc symbol ngoài universe của bot; `VETO` khi cổng bot chặn trước strategy (config chưa hiệu lực, evidence ngoài cửa sổ, input tương lai, release không tồn tại, snapshot/candidate sai scope). Luôn có `reason_code` và giải thích tiếng Việt.
13. **Định danh quyết định:** `idempotency_key = SHA-256("bot-decision-v1|workspace|bot|account|evidence|config_version")`; `decision_id`/`job_id` là `uuid5` từ key nên replay tái lập. `decision_hash` phủ canonical payload, không chứa đồng hồ wall-clock; `completed_at` của job lấy từ clock tiêm vào và không vào hash.
14. **RLS/grant bảng bot-scoped (mẫu MB-01):** FK ghép `(workspace_id, bot_id, account_id)` → `bot_instance(workspace_id, id, trading_account_id)`; ENABLE + FORCE RLS; policy `workspace_isolation` cho `crypidian_trading_runtime` (workspace + `bot_id = app.bot_id`) và `bot_member_isolation` cho `crypidian_admin_web` (membership ACTIVE có role owner/strategy_admin/risk_admin hoặc `bot_grant` còn hiệu lực); index `ix_<table>_bot_scope`. Grant trực tiếp: runtime `SELECT, INSERT` (+ `UPDATE (status, completed_at)` trên job để chuyển trạng thái và `SELECT ... FOR UPDATE`), admin_web `SELECT`; revoke khỏi `PUBLIC`, `crypidian_app`. Evidence: runtime `SELECT, INSERT`, admin_web `SELECT`. Gateway/ingestion/trainer default-deny (WP-05/MB-08 thêm khi cần).
15. **Bất biến DB:** trigger evidence và decision từ chối UPDATE/DELETE; trigger job chỉ cho phép `PENDING → COMPLETED` (đặt `completed_at`), từ chối sửa cột khác và DELETE.
16. **Downgrade:** từ chối `MB03_DOWNGRADE_REQUIRES_EXPORT_OF_DECISIONS` nếu còn evidence/job/decision (contract §8.5: rollback không drop dữ liệu); bảng rỗng mới drop.
17. **Ranh giới file:** contract `BotDecisionContext`, `BotDecisionJob`, `BotDecision` và Protocol store/provider nằm trong `application/bot_dispatcher.py`; ORM ở `models/signal_entities.py`; repository + store PostgreSQL ở `repositories/signal_evidence.py` và `repositories/bot_decisions.py` (repository import application contract như tiền lệ `backtests.py`).
18. **Nguồn `DecisionSnapshot`:** Protocol `DecisionSnapshotProvider.snapshot_for(evidence, context, evaluated_at)`; unit/E2E dùng fixture xác định. Lắp từ bảng market thật thuộc MB-05.

## Bảng dữ liệu

| Bảng | Scope | Cột chính | Ràng buộc |
| --- | --- | --- | --- |
| `shared_signal_evidence` | workspace | `id` (= evidence ID), symbol, setup type, `observed_at`/`available_at`/`validity_end`, `feature_snapshot_id`, `source_refs`, `model_release_id`, `evidence_hash`, schema version, canonical JSON | Unique `(workspace_id,id)`, `(workspace_id,evidence_hash)`; FK feature snapshot; check thứ tự thời gian, hash 64, source refs mảng không rỗng; index as-of; trigger immutable |
| `bot_decision_job` | bot | bot/account, evidence ID/hash, `bot_config_version_id`, `evaluated_at`, context payload/hash, status, `completed_at` | Unique key job; FK bot/account, FK evidence; check status↔completed_at; index bot scope + pending; trigger chỉ cho PENDING→COMPLETED |
| `bot_decision` | bot | job, evidence ID/hash, config/root/strategy/model lineage, `evaluated_at`, status, reason, candidate ID, idempotency key, decision hash, payload | Unique `(workspace,bot,idempotency_key)`, `(workspace,bot,job_id)`; FK bot/account, job, evidence; check status↔candidate; index bot scope + thời gian; trigger immutable |

## Work breakdown và điều kiện nghiệm thu

### MB03.1 — Domain evidence (Step 1, 5)

- [x] RED: causality (`observed_at <= available_at < validity_end`, datetime aware → UTC), workspace scope trong ID/hash, provenance hash ổn định (source refs không phụ thuộc thứ tự), ID/hash giả bị từ chối, không có trường bot/account/quantity/order/side.
- [x] RED temporal: feature/source/model release khả dụng sau evidence bị từ chối khi dựng; evidence chưa khả dụng hoặc hết hạn bị từ chối khi đánh giá; revision muộn bị từ chối cho đánh giá lịch sử.
- [x] GREEN `domain/market/signal_evidence.py`.

**Nghiệm thu:** evidence bất biến, hash/ID tái lập, không trường thực thi, thời gian point-in-time.

### MB03.2 — Contract dispatcher và đánh giá theo bot (Step 2, 4, 5)

- [x] RED: hai bot cùng workspace, release khác nhau (H01, H03) trên một evidence → một evidence, một outbox marker, hai job, hai decision có bot/account/config/release/idempotency riêng; retry không nhân bản và không gọi lại strategy.
- [x] RED: evidence khác workspace bị từ chối trước khi ghi; evidence stale/chưa khả dụng bị từ chối; input snapshot tương lai, config chưa hiệu lực, release không tồn tại → VETO; symbol ngoài universe/NoTrade → ABSTAIN; job cùng key khác payload → conflict; quyết định tái lập hash.
- [x] RED: dispatcher không có gateway/planner; `BotDecision` không có quantity/order.
- [x] GREEN `application/bot_dispatcher.py` + helper `strategy_selection_for_release`.

**Nghiệm thu:** mỗi bot tự quyết định độc lập, idempotent theo bot, evidence không tạo lệnh.

### MB03.3 — Repository, store PostgreSQL và outbox consumer (Step 3)

- [x] RED repository (fake session): append evidence dedupe/conflict/workspace-scoped, restore kiểm hash, `get_as_of` lọc `available_at <= as_of`; job dedupe/conflict; decision restore kiểm hash; `claim_ready(event_types=...)` lọc loại event.
- [x] RED consumer với store in-memory: claim → fan-out → ack; lỗi → record failure; hash payload sai → failure.
- [x] GREEN ORM `signal_entities.py`, repositories, `PostgresSignalDecisionStore`, `SignalEvidenceFanOutConsumer`, test model (bảng mới, FK ghép, index bot scope, evidence không có cột bot/account).

**Nghiệm thu:** evidence ghi một lần + outbox cùng transaction; job mỗi bot trong transaction bot riêng; retry/consumer không nhân bản.

### MB03.4 — Migration, RLS và grants

- [x] Migration `e3c5a7b9d2f4`: ba bảng, FK/unique/check/index, RLS ENABLE+FORCE, policy, trigger, grant trực tiếp, downgrade guard.
- [x] `alembic heads` = `e3c5a7b9d2f4`; `alembic check` sạch trong E2E.

### MB03.5 — PostgreSQL Compose E2E (Step 6) và quality gates

- [x] `tests/e2e/test_multibot_decision.py` (+ probe qua stdin): hai bot PAPER trong một workspace (bot do trigger tạo khi insert account), một evidence → một hàng evidence, hai job/decision bot-scoped, config/version/release/candidate cô lập; retry và consumer không nhân bản; revision muộn qua consumer + `process_pending`; dispatch lịch sử revision bị từ chối.
- [x] RLS/quyền: runtime bot A không thấy/ghi job bot B; không bot scope → 0; workspace B không thấy evidence A; admin owner thấy cả hai, operator chỉ bot có grant; runtime không UPDATE evidence/decision, job chỉ UPDATE status/completed_at; gateway/ingestion/`crypidian_app` không có quyền; FK chặn job trỏ evidence khác workspace.
- [x] Không có hàng `trade_plan`/`risk_decision`/`risk_reservation`/`order_intent`/`exchange_order`/`fill`; trigger bất biến; downgrade guard, downgrade/upgrade lại, `alembic check`.
- [x] Unit đầy đủ, Ruff check/format, mypy, `git diff --check`, alembic heads; commit trên nhánh worktree.

## Kết quả triển khai 2026-10-24

- **Unit:** 604 passed (baseline 534 + 70 test mới: evidence 25, dispatcher 25, repository/store 16, migration tĩnh 4; cập nhật tập bảng trong `test_database_models.py`).
- **E2E:** `tests/e2e/test_multibot_decision.py` 1 passed (57.25s): một evidence → một hàng, hai job/quyết định CANDIDATE cô lập (h01/h03, config A/B, candidate khác nhau); retry và consumer không nhân bản; revision qua outbox + `process_pending`, dispatch lịch sử revision bị từ chối; `get_as_of` trả đúng revision; RLS/quyền/FK/trigger; không có hàng plan/risk/reservation/intent/order/fill/position/trade; downgrade guard, downgrade/upgrade lại, `alembic check`.
- **Lỗi đã gặp:** lần E2E đầu fail ở probe `verify` vì assertion tập `bot_id` áp cả cho bot workspace B (0 quyết định → tập rỗng); lỗi của probe, không phải hành vi sản phẩm. Sửa: chỉ kiểm tập khi kỳ vọng có hàng. Unit migration tĩnh fail lần đầu vì tên trigger sinh bằng f-string; đổi sang hằng `_IMMUTABLE_TRIGGERS` tường minh. Tên CHECK được rút gọn để `ck_<table>_<name>` không vượt giới hạn 63 ký tự của PostgreSQL.
- **Tích hợp controller:** thêm FK `bot_config_version_id` sau MB-02; re-chain `down_revision`; adapter `EffectiveBotConfig → BotDecisionContext`; cập nhật tập bảng trong `test_database_models.py` khi gộp với bảng MB-02.

## Gate tổng WP

MB-03 đạt khi evidence chung không nhân bản theo bot; mỗi bot chạy strategy/config riêng và có quyết định riêng idempotent; evidence không thể tạo lệnh venue; replay theo bot tái lập và point-in-time; RLS/grant chặn chéo bot/workspace; unit + E2E tập trung + quality gates đạt. Toàn bộ suite E2E do controller chạy sau tích hợp.

## Nguồn đã kiểm tra (2026-10-24)

- PostgreSQL 18, [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html): kiểm tra toàn vẹn tham chiếu (unique/PK/FK) luôn bỏ qua RLS → FK job/decision → evidence và → bot_instance hoạt động dưới RLS; nhiều permissive policy ghép OR → mỗi role chỉ một policy.
- PostgreSQL 18, [SELECT – The Locking Clause](https://www.postgresql.org/docs/current/sql-select.html): `FOR UPDATE` cần quyền UPDATE trên ít nhất một cột → grant `UPDATE (status, completed_at)` đủ cho khóa job, không cần UPDATE toàn bảng.
- PostgreSQL 18, [GRANT](https://www.postgresql.org/docs/current/sql-grant.html): privilege cấp cột độc lập với cấp bảng → chỉ cấp cột, kiểm `has_column_privilege`.
- Python 3 [`uuid.uuid5`](https://docs.python.org/3/library/uuid.html): UUID dựa trên SHA-1 của namespace + name (RFC 9562 §5.5), tất định → dùng cho evidence/job/decision ID tái lập.
- PostgreSQL 18, [INSERT – ON CONFLICT](https://www.postgresql.org/docs/current/sql-insert.html): đã cân nhắc; giữ mẫu savepoint + `IntegrityError` + đọc lại của `FeatureSnapshotRepository` để thống nhất repository và phân biệt duplicate/conflict.

## Thuật ngữ EN→VI mới (đề xuất cho glossary, controller ghi)

- **Shared signal evidence — bằng chứng tín hiệu dùng chung:** fact/setup trung lập scope workspace, có provenance và mốc khả dụng; không phải lệnh hay quyết định của bot.
- **Fan-out — phân phát theo từng bot:** từ một sự kiện chung tạo đúng một job cho mỗi bot đủ điều kiện, idempotent.
- **Validity window — cửa sổ hiệu lực:** khoảng `[available_at, validity_end)` mà evidence được phép dùng cho quyết định.
- **Data revision — bản sửa đổi dữ liệu:** phiên bản sau của cùng một quan sát; lưu thành bản ghi mới có `available_at` muộn hơn, không ghi đè bản cũ.
- **Decision job — tác vụ quyết định theo bot:** bản ghi bot-scoped ghim evidence, config và `evaluated_at` để bot đánh giá đúng một lần.
