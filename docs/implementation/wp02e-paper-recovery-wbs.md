# WP-02E — Khôi phục trạng thái PAPER sau khi khởi động lại

## Mục đích và trạng thái

Đây là WBS và báo cáo triển khai recovery của PAPER trong Task 8/10 WP-02. Phần có thể nghiệm thu trong WP-02 là phục hồi PAPER deterministic và an toàn; đối soát Binance live thuộc WP-03 và nằm ngoài phạm vi. Kết luận BLOCKED ban đầu đã được thay thế: hiện coordinator/UoW, runtime bootstrap/hydrate theo một workspace deployment và Compose E2E đã có. PP2/PP6 target và final full suite đạt; WP-02 được nghiệm thu trong phạm vi PAPER/replay MVP.

**Kết luận điều tra:** schema hiện tại **chưa đủ để khôi phục an toàn, đúng nguyên trạng** một `PaperExecutionGateway` sau process restart. Schema lưu ý định, plan, fill/cost và một phần position transition, nhưng gateway giữ một số state cần thiết chỉ trong bộ nhớ. Không được tự suy diễn thiếu dữ liệu hoặc âm thầm dựng một vị thế khác.

## Phạm vi và ranh giới cấu hình

- Hệ thống tiếp tục dùng `WorkspaceSettings` làm root settings chung. Runtime chỉ nạp bản `PUBLISHED` đã đến `effective_at`, xác minh `settings_hash`, rồi sử dụng `settings_version_id`/hash làm cấu hình nguồn cho vòng mới.
- Mỗi lệnh/vị thế đang tồn tại phải được phục hồi theo cấu hình đã pin ở `OrderIntentRecord.settings_version_id`, `TradePlanRecord.payload`/`plan_hash`, lineage của ledger và `paper_execution_profile_hash`. Không được áp cấu hình đang effective mới nhất để viết lại lệnh cũ.
- Nếu policy/catalog/profile gắn với một order đang mở không còn được resolve đúng version/hash, recovery phải fail-closed: giữ entry tạm dừng, ghi lỗi có lineage, không tự chọn profile mới.
- Admin API chỉ sửa/publish cấu hình root theo quyền và workflow hiện hữu. Thêm cờ/chế độ vận hành recovery vào cùng `WorkspaceSettings` nếu cần, không tạo một trang cấu hình rời. Cài đặt giao dịch hiện hữu tiếp tục ở `runtime`, `market_data`, `features`, `strategy`, `risk`, `paper_execution`.
- Cấu hình tối thiểu đề xuất cho root: `paper_execution.recovery_policy` có giá trị versioned như `STRICT_FAIL_CLOSED`; mặc định không cho resume tự động khi phát hiện checkpoint thiếu, hash sai, replay lệch hoặc có event chưa rõ. Không cho admin tắt các kiểm tra bất biến/idempotency.

## Khảo sát cấu trúc hiện tại

| Thành phần | Bằng chứng lưu bền | State chưa được lưu/không khôi phục chính xác |
|---|---|---|
| `PaperExecutionGateway` | Không tự lưu; `OrderIntentRecord` có `idempotency_key`, plan hash, symbol/side/quantity/status; `TradePlanRecord.payload` lưu plan canonical; `FillRecord` lưu role, key, bar, policy version, quantity, execution price, phí/slippage, thời điểm. | Gateway lưu `_orders`, `_keys`, `_positions`, `_plans`, `_close_keys`, `_entries_paused` trong dict/bool bộ nhớ. Không có constructor hydrate/restore hay snapshot contract.
| Lệnh entry | `OrderIntentRecord` lưu quantity đã yêu cầu/đã fill, status; plan payload có source time, expiry, stop/target, exit recipe và exchange rules theo canonical plan. `RiskReservationRecord` có trạng thái/expiry. | `PaperOrderIntent.eligible_at`, `seen_bar_keys`, tuple fills và một số state máy trạng thái không phải toàn bộ cột riêng của intent. Cần adapter deserialize plan/version chính thức, không được đoán từ mã JSON.
| Fill/vị thế | ENTRY/EXIT `FillRecord`; `TradeCostRecord`; `PositionEventRecord` (`OPENED`, `INCREASED`, `REDUCED`, `CLOSED`, `STOP_UPDATED`); `TradeRecord` OPEN/CLOSED; account equity read-model từ seed + trade closed. | `FillRecord` không lưu `reference_price`; entry position dùng giá tham chiếu trung bình để tính gross PnL. Stop update chỉ mang stop mới trong payload. Không có snapshot đầy đủ recipe, quantity bảo vệ, bars held, funding event ID set, exit status, `seen_bar_keys` hoặc market event watermark. Funding đang in-memory trong gateway; ledger cost có thể lưu nhưng không có reader tái dựng toàn bộ gateway.
| Lifecycle/transaction | `PaperLifecycleCoordinator` gọi gateway rồi append fill/event/trade vào caller `UnitOfWork`; ledger API có khóa idempotency và transaction DB. | Gateway bị mutate **trước khi DB commit**. Nếu UoW rollback nhưng process vẫn sống, state RAM đã tiến lên trong khi DB quay lui. Nếu DB commit xong rồi process chết, RAM mất; hiện không có đường phục hồi.
| Settings/Admin | `WorkspaceSettingsVersion` bất biến theo version/status/effective time; `WorkspaceSettingsRepository.load_effective_config()` xác minh hash. Intent/Trade ghi IDs/hash lineage. | Không có cơ chế phiên bản hóa state gateway/checkpoint hoặc quy tắc phục hồi gắn với root setting. Đọc settings effective mới rồi áp vào lệnh đang mở có thể làm đổi fee/fill/exit behavior lịch sử.
| Tenant/security | Các bảng ledger có `workspace_id`, `account_id`, RLS/FORCE RLS, FK ghép workspace/account và runtime grants. | Bất kỳ checkpoint/state repo mới nào phải lặp lại ranh giới này; truy vấn theo intent ID đơn lẻ là không đủ.

### Khả năng tái dựng từ dữ liệu hiện có

`TradePlanRecord.payload` cung cấp nguồn có thể giải mã để khôi phục `TradePlan`, nhưng đây mới là một phần. ENTRY fill chỉ lưu giá thực thi và một số tổng chi phí; không lưu đầy đủ reference price và thứ tự state của từng lệnh. Nhiều chuỗi market event không tạo transition nào nên không có record để tái dựng `bars_held`/`seen_bar_keys`. Các event funding/close idempotency cũng có state RAM riêng. Do vậy, dựng gateway chỉ từ schema hiện hữu có thể tạo sai PnL, trailing stop, event dedupe hoặc double-process sau restart. Đọc `TradeRecord` OPEN/CLOSED không khắc phục thiếu hụt đó.

## Seam phục hồi an toàn tối thiểu được đề xuất

Tạo một **PAPER state repository/checkpoint seam** tại application/adapter boundary; không để domain import SQLAlchemy hoặc database. Persist một snapshot versioned, canonical và hash-bound cho từng intent/account, cùng marker cho market event đã xử lý. Bản ghi tối thiểu cần mang đủ `PaperOrderIntent`, `PaperPosition` (nếu có), plan/version references, policy/profile hash, entry-fill references, pause flag/account execution state, snapshot schema version, monotonic revision, last processed event identity/time và checksum. Lưu Decimal/datetime/enum theo serialization canonical có schema và validation nghiêm ngặt.

Để giảm độ phức tạp ở vòng đầu, có thể dùng một bảng checkpoint versioned theo `(workspace_id, account_id, intent_id, revision)` và unique event identity; mỗi input market event được áp đúng thứ tự. Snapshot và ledger fill/cost/position/trade/outbox phải ghi **trong cùng transaction**. Chỉ publish state mới vào gateway sống sau khi commit thành công (copy-on-write/transition candidate), hoặc hủy bỏ instance và reload từ checkpoint khi rollback. Chốt contract lựa chọn này trước migration; không được dựa vào mutation RAM rồi “hy vọng” commit.

Recovery flow đề xuất:

1. Tại startup, chặn tạo entry mới cho account PAPER cần recovery.
2. Nạp trạng thái theo `workspace_id + account_id`, có tenant scope/RLS; lấy checkpoint/market-event watermark và ledger rows cần kiểm chứng.
3. Xác minh schema version, checksum/hash, parent/plan/profile/settings lineage, thứ tự revision, trạng thái intent/trade/reservation, fill ENTRY–EXIT, position event/cost totals và quantity còn mở. Chỉ resolve version từ bản cấu hình đã pin; không dùng current effective settings để sửa state cũ.
4. Khôi phục checkpoint thành một gateway mới; kiểm tra deterministic invariants và không có event đã xử lý được áp lại. Event cũ/đến sai thứ tự phải bị từ chối hoặc đưa vào hàng chờ điều tra, không âm thầm bỏ qua.
5. Khi dữ liệu thiếu/lệch, ghi audit/outbox với nguyên nhân và giữ entry paused; không tự đóng vị thế hoặc tự ghi PnL bù trừ. Cho phép admin có quyền xem lỗi và thực hiện hành động quản trị theo use case đã xác định; mọi override cần lý do, actor, timestamp và audit trail.
6. Chỉ mở lại entry sau khi toàn bộ account state đạt kiểm tra; vị thế đã mở vẫn được monitor theo stop/protective state đã phục hồi.

## WBS chi tiết trước implementation

| ID | Công việc | Kết quả bàn giao | Điều kiện hoàn tất |
|---|---|---|---|
| R1 | Chốt contract serialization `PaperGatewayState`/`PaperExecutionCheckpoint` và canonical hash; xác định state fields, schema version, migration compatibility, transition/event ordering. | ADR ngắn + typed domain DTO/schema + glossary nếu phát sinh thuật ngữ giao dịch. | Có mapping từng field từ RAM sang nguồn persisted; serialize→deserialize roundtrip giữ nguyên Decimal, datetime, enum và hash; dữ liệu version không hỗ trợ bị từ chối.
| R2 | Thiết kế DB checkpoint/event idempotency, RLS, FK composite, grants, trigger/immutability và retention. | Migration design, model/repository contract, privilege/RLS test cases. | Mỗi row luôn scope workspace/account/intent; không thể đọc chéo tenant; duplicate identity + payload giống nhau idempotent, payload khác conflict; multi-worker cùng account được serialize bằng lock/optimistic revision đã chọn.
| R3 | Tách bước tính transition khỏi việc cập nhật gateway đang chạy; thêm snapshot export/restore strict validation. | Domain gateway recovery contract không phụ thuộc DB + unit tests. | Restore giữ nguyên order/position/equity-related state, dedupe keys, event watermark và pause; thiếu field, policy/profile lệch hoặc checksum sai fail-closed; chạy tiếp cùng input tạo output/hash giống run không restart.
| R4 | Nối `PaperLifecycleCoordinator` với checkpoint repository và transaction. | Application orchestration đảm bảo checkpoint, ledger, cost, trade, reservation/outbox cùng commit. | Commit cập nhật bền vững một lần; rollback không làm live state tiến; commit trước crash/restart reload được đúng; crash sau DB commit/trước memory publish cũng load đúng checkpoint.
| R5 | Nối root settings/Admin API. | Recovery policy ở root `WorkspaceSettings`; admin đọc/cập nhật/publish đúng permission, audit và version. | Settings mới tác dụng cho vòng/lệnh mới; lệnh mở phục hồi đúng settings/profile hash cũ. Setting invalid/unsupported không publish; thiếu lineage giữ account paused.
| R6 | Tạo Compose E2E recovery trên PostgreSQL runtime role. | Một probe nhiều pha, có thể tái sử dụng image dùng chung; không tạo image riêng cho từng test. | Tạo lệnh/partial fill/open position, commit, dispose process/gateway, tạo gateway mới, restore; tiếp tục event/exit ra đúng fill/trade/PnL/account read-model; không Binance/network live.
| R7 | Kiểm thử rollback, crash window, duplicate/out-of-order event, tenant và corruption. | Unit + Compose E2E negative coverage và recovery diagnostics. | Không duplicate fill/cost/trade; rollback vẫn replay được event; event committed không double-apply; sai workspace không thấy state; hash/schema/order mismatch fail-closed và pause; race hai worker không double process.
| R8 | Run regression/static, cập nhật docs/status/memory và review. | WBS/implementation plan, glossary, memory; status rõ từng gate. | Unit và E2E liên quan chạy đủ, không skip/flaky chưa điều tra; Ruff/mypy/Alembic/diff check đạt; memory được cập nhật rồi đọc lại; reviewer xác nhận không làm sống lại lệnh/position ngoài ý muốn.

## Điều kiện nghiệm thu tổng thể

1. **Roundtrip deterministic:** snapshot của order ACCEPTED/PARTIALLY_FILLED/FILLED/CANCELLED/EXPIRED/UNKNOWN và position OPEN/CLOSED lưu–nạp giữ nguyên canonical hash và mọi thuộc tính ảnh hưởng quyết định/khớp/giám sát.
2. **Nguyên tử:** checkpoint, event dedupe, fill/cost, position transition, trade close, reservation transition và outbox thuộc cùng transaction nghiệp vụ; rollback không để RAM/DB lệch sau khi request trả về.
3. **Idempotency:** event retry cùng payload trả kết quả cũ; cùng event key khác payload báo conflict; crash ở trước commit, sau commit và trước state publish không tạo fill/fee/trade lặp.
4. **Fail-closed:** checkpoint thiếu/hỏng, schema không hỗ trợ, policy/hash mismatch, ledger invariant sai, order/trade/reservation không khớp hoặc input out-of-order làm account pause và phát diagnostic; không tự suy diễn giá tham chiếu hay PnL.
5. **Tenant scope:** mọi read/write/checkpoint/event đều bị ràng buộc workspace+account+intent ở repository và DB RLS/FORCE RLS; Compose E2E bằng runtime role xác nhận workspace khác không thể nhìn thấy.
6. **Root settings:** dùng chung `WorkspaceSettings`/Admin API/audit/version. Không xuất hiện config repository thứ hai; thay đổi current root không ghi đè settings/profile hash lineage của state đã mở.
7. **Coverage:** Unit kiểm serializer, DTO validation, restore states, deterministic replay, conflict, rollback boundary và hash mismatch. Compose E2E dùng PostgreSQL thật để kiểm migration, RLS/grants, idempotency, commit/restart/resume/rollback, cross-workspace denial, `alembic check`, downgrade/upgrade.
8. **Paper-only:** toàn bộ luồng chạy bằng PAPER gateway, không credentials/exchange client/network order call; có test guard xác nhận boundary.

## Use cases và ca lỗi bắt buộc

- **Khởi động account PAPER bình thường:** root runtime settings được publish, load seed/checkpoint, validate hash, hydrate gateway, sau đó mới cho phép decision cycle.
- **Khởi động có order chưa fill:** phục hồi plan/status/eligible time/expiry/seen event position; order terminal hoặc reservation hết hạn được reconcile theo rule rõ ràng, không được tự khớp event đã qua.
- **Khởi động có partial fill và vị thế mở:** khôi phục lượng còn lại, giá tham chiếu/trung bình và execution, fees/slippage/funding, recipe/stop/target/trailing, bars held và event watermark.
- **Rollback sau transition:** bar vẫn có thể gửi lại và tạo đúng một transition sau khi transaction mới commit; instance bị mutate trước đó không được tiếp tục làm nguồn sự thật.
- **Crash ngay sau commit:** sau restart, checkpoint reflects ledger đầy đủ; không ghi lại ENTRY/EXIT/cost/trade/outbox.
- **Crash trước commit:** không có checkpoint/ledger transition mới; input có thể retry chính xác một lần.
- **Duplicate, event cũ hoặc out-of-order:** duplicate canonical idempotent; payload conflict/out-of-order reject, pause khi không thể chứng minh state sequence.
- **Tenant mismatch:** checkpoint của account/workspace khác không thể hydrate qua UUID đoán được.
- **Profile/config drift:** current root settings khác profile cũ; state mở vẫn gắn profile cũ; thiếu catalog cũ thì fail closed.
- **State corruption/migration:** bad checksum, unsupported snapshot schema hay incomplete checkpoint không tự chữa bằng fallback equity/default config.

## Quyết định cần giữ mở trước khi code

- Chọn mô hình lưu snapshot append-only có revision hay bản checkpoint hiện hành có optimistic concurrency. Dù chọn kiểu nào, checkpoint và ledger transition phải cùng UoW, có lịch sử đủ điều tra.
- Chốt semantics thứ tự event (monotonic event time + sequence/provider cursor) và chính sách event đến muộn.
- Chốt việc khởi động phục hồi cả account như một đơn vị hay độc lập theo từng intent; account-level pause phải thắng pause trạng thái riêng của một order.
- Kiểm tra payload `TradePlanRecord` có schema version/decoder tương thích hay cần bổ sung phiên bản; hiện đang lưu dataclass canonical nhưng chưa có decoder gateway được xác nhận.
- Chốt recovery state có chứa mọi bar key hay watermark + dedupe journal. Không cắt lịch sử dedupe nếu không có retention/consumer cursor proof.

## Trạng thái triển khai sau ADR và kiểm tra an toàn

ADR đã chốt checkpoint append-only toàn account, advisory transaction lock theo workspace/account, event id dạng hash của tuple có lineage, event order theo symbol, snapshot có schema/hash, candidate-state chỉ publish sau commit, root `WorkspaceSettings` là nguồn cấu hình duy nhất và fail-closed khi lineage thiếu/lệch. Xem [ADR phục hồi PAPER](../architecture/decisions/2026-10-23-paper-recovery.md). WBS này được cập nhật theo code/test hiện tại; các ghi chú điều tra ở trên mô tả trạng thái ban đầu, không phải blocker còn nguyên.

### Đã triển khai trong working tree

- `PaperExecutionSnapshot` schema v2 canonical/hash-bound; strict export/restore bao order, key, plan, position, close receipt, pause, bar fingerprints/cursors. Active order/position policy mismatch bị từ chối; order/trade terminal được giữ theo policy đã pin.
- `PaperCheckpointRepository` append/load-latest dùng account advisory lock và `workspace_id + account_id`; bảng checkpoint append-only có RLS/FORCE RLS/grants ở migration `a1f4c9d2e6b8`.
- `PaperLifecycleCoordinator` xử lý submission/market-entry/monitor trên candidate gateway, ghi checkpoint cùng ledger trong một UoW và chỉ publish qua `after_commit`; rollback không làm live gateway tiến. Event market xử lý đúng một intent mỗi lệnh checkpoint; retry trả snapshot mới nhất của account.
- `verify_paper_ledger_consistency()` hydrate fail-closed khi snapshot lệch order/plan/profile/settings lineage, ENTRY/EXIT fill, position event/quantity, cost, trade hoặc reservation. Các truy vấn reconciliation đều giới hạn cả workspace và account.
- Hợp đồng tài chính hiện hành được giữ nguyên: `TradeRecord` OPEN có các rollup fee/funding/slippage/total-cost/gross/net bằng 0; phí, funding và slippage khi OPEN đối soát trực tiếp giữa cost ledger và snapshot vị thế. Khi CLOSED, tổng trade phải khớp cost ledger và công thức gross/net. Không thêm migration/trigger để cập nhật rollup OPEN.
- Root `WorkspaceSettings.paper_execution.recovery_policy` là `STRICT_FAIL_CLOSED`; Admin publish/rollback chặn đổi execution profile khi còn intent entry đang hoạt động, trade/vị thế mở hoặc risk reservation chưa được giải phóng. Sau khi mọi trạng thái đã terminal, profile mới được phép publish; lịch sử terminal giữ policy cũ.
- H03 Compose flow đã gọi service submit có checkpoint, hydrate gateway mới sau entry và đối soát closed trade; probe còn kiểm tra checkpoint hash-valid nhưng lineage sai bị từ chối trước publish và workspace khác không đọc được dữ liệu.

### Kết quả kiểm tra và trạng thái lần chạy

- Recovery-focused unit group: `tests/unit/test_paper_recovery_verifier.py`, `test_paper_lifecycle_checkpoints.py`, `test_paper_execution_recovery.py`, `test_paper_checkpoint_repository.py`, `test_paper_recovery_uow.py`, `test_trade_ledger.py`, `test_admin_settings_service.py` → **49 passed**.
- Mypy trên `paper_recovery.py`, `paper_lifecycle.py`, `paper_checkpoints.py` → **pass**. Ruff check → **pass**; test verifier được format sau khi format-check chỉ ra hai dòng cần chuẩn hóa.
- Compose E2E dùng code verifier mới nhất: `.venv/Scripts/python.exe -m pytest tests/e2e/test_compose_boundary.py::test_paper_stack_starts_and_keeps_database_and_object_store_data -q -s` → **1 passed in 104.14s** (session 56830). Luồng H03 checkpoint/restart/tenant/corruption và close-trade đạt với semantics OPEN rollups bằng zero.
- Lần E2E trước đó (session 75526) thất bại trong một thử nghiệm tạm cập nhật rollup của trade OPEN; PostgreSQL từ chối theo trigger OPEN→CLOSED-only. Thử nghiệm và migration tạm đã được gỡ; không còn file migration tạm và production semantics giữ nguyên. Đây là nguyên nhân chính xác của lần lỗi trước, không phải lỗi verifier hiện tại.
- Teardown sau E2E: chỉ còn `crypidian-postgres-1` healthy; không còn Compose test container.

### Trạng thái hiện tại — recovery/bootstrap đạt; WP-02 được nghiệm thu

Các ghi chú ban đầu ở dưới đây mô tả blocker trước khi hoàn tất WBS [runtime bootstrap](./wp02e-runtime-bootstrap-wbs.md), không còn là trạng thái hiện tại. E2E cuối cùng gồm bootstrap/readiness và H03 application lifecycle đã đạt. Giới hạn còn lại là dispatcher tự chạy, xử lý mọi lifecycle command ngoài vertical slice, venue reconcile và multi-workspace scheduling; các phần này thuộc work package tiếp theo.

**Quyết định hiện tại:** runtime bootstrap/hydrate một workspace deployment được nối vào `trading-runtime`; readiness fail-closed khi recovery không đạt. Strategy-only settings publish được tuần tự hóa; vị thế/order active giữ strategy/execution profile đã pin và monitor dùng lineage cũ. Thay đổi execution policy không tương thích vẫn fail-closed. Điều này không bật market dispatcher hay exchange execution. PP2/PP6 targeted E2E và final full E2E **13 passed / 381.82s** đạt. Xem [WBS runtime bootstrap](./wp02e-runtime-bootstrap-wbs.md) và [WBS pinning/replay progression](./wp02e-profile-pinning-and-replay-progression-wbs.md).

### Acceptance matrix hiện tại

| Hạng mục | Trạng thái | Còn thiếu |
|---|---|---|
| Snapshot schema/strict restore, policy drift, deterministic event idempotency | Unit đạt | Giữ test hồi quy khi mở rộng loại lifecycle/event. |
| Repository/migration/RLS, account lock, checkpoint append-only | Unit và H03 Compose runtime-role probe đạt | Chưa có dispatcher nhiều tenant; mọi thao tác production vẫn phải tenant-scoped. |
| UoW commit/rollback và publish sau commit | Unit đạt; H03 E2E dùng coordinator/app lifespan | Dispatcher tự chạy và lifecycle command ngoài slice còn thuộc WBS riêng. |
| Ledger reconciliation | Unit và H03 Compose đạt cho intent/plan/fills/events/cost/trade/reservation, gồm OPEN-zero rollups | Mở rộng lifecycle khác phải giữ cùng coordinator/transaction contract. |
| Root WorkspaceSettings/Admin profile pin | PP2 focused unit 17 pass; PP6 Compose target pass | Strategy-only publish có thể xảy ra khi active state tồn tại; active position giữ profile/release cũ. Execution policy mutation không tương thích bị chặn/fail-closed. Full suite rerun còn chờ. |
| Runtime bootstrap/readiness theo workspace/account | **Đạt cho một workspace deployment** | E2E `test_paper_runtime_bootstrap.py` xác minh liveness/readiness, hydrate và fail-closed; không bao gồm dispatcher nhiều workspace hoặc Binance reconcile. |

WP-02E recovery/pinning technical slice đạt; WP-02 tổng thể **được nghiệm thu trong phạm vi PAPER/replay MVP**. Dispatcher/venue/multi-workspace không thuộc phạm vi đã triển khai.

## Tài liệu liên quan

- [WP-02 plan, Task 8/10](../superpowers/plans/2026-10-22-wp02-implementation-plan.md)
- [WBS exit fill PAPER](./wp02e-exit-fill-wbs.md)
- [Thiết kế lõi giao dịch WP-02](../superpowers/specs/2026-10-22-wp02-trading-core-design.md)
- [Quy trình Binance execution/reconciliation WP-03](./03-binance-execution.md) — chỉ áp dụng khi triển khai exchange adapter; PAPER recovery không truy vấn Binance.
- [Glossary thuật ngữ VI](../research/glossary-vi.md)
