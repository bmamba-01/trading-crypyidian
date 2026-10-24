# ADR — Khôi phục PAPER state sau restart

- **Trạng thái:** Đã chốt cho WP-02E recovery slice
- **Ngày:** 2026-10-23
- **Bối cảnh:** `PaperExecutionGateway` là state machine RAM. `PaperLifecycleCoordinator` hiện mutate gateway trước khi `UnitOfWork` commit. Ledger hiện có đủ lineage quyết định nhưng thiếu checkpoint runtime (reference price, monitor state, market event watermark) để dựng lại gateway.

## Quyết định

1. **Phạm vi khôi phục là toàn bộ một PAPER account trong một workspace**, vì gateway có thể giữ nhiều intent/position và account pause là trạng thái chung. Mọi truy vấn/khóa/checkpoint mang `workspace_id + account_id`; intent IDs đơn lẻ không tạo ranh giới tenant.
2. **Lưu snapshot append-only sau mỗi command/state transition và mỗi market event** (kể cả event không tạo fill hoặc stop update). Mỗi hàng có revision tăng đơn điệu, event key, event payload hash, snapshot schema version, canonical JSON snapshot, content hash và cấu hình/profile lineage. Giữ checkpoint gần nhất để hydrate; các revision trước là bằng chứng khôi phục/điều tra. Không cắt lịch sử dedupe trong WP-02.
3. **PostgreSQL account advisory transaction lock** tuần tự hóa mọi thay đổi account và phân bổ revision. Thứ tự market event là tăng dần theo `bar.opened_at` riêng từng symbol; retry đúng event key/hash là idempotent, cùng key khác hash là conflict; event cũ/out-of-order không được áp dụng và đưa account vào trạng thái pause/recovery-required.
4. **Domain snapshot typed và serialization canonical, có schema version.** DTO chứa toàn bộ orders, idempotency keys, plans (`TradePlan` đã canonical và decoder được kiểm tra), positions, close receipts/idempotency, policy version/hash, account pause và watermark/dedupe. Decimal lưu chuỗi chuẩn; datetime ISO-8601 có timezone; không `default=str` cho dữ liệu checkpoint. Sai trường, Decimal/enum/timestamp, version, checksum hoặc lineage phải từ chối restore.
5. **Candidate-state transaction:** lifecycle clone state gateway sang candidate, áp transition trên candidate, ghi ledger/outbox/checkpoint trong cùng UoW, và chỉ sau commit mới `replace_state` của gateway đang sống. `UnitOfWork` có callback sau commit; rollback không publish candidate. Nếu commit thành công nhưng callback/process chết, startup nạp checkpoint đã commit. Nếu transaction commit lỗi/không rõ kết quả, không tiếp tục dùng candidate; reload từ DB trước khi nhận event kế tiếp.
6. **Root `WorkspaceSettings` vẫn là nguồn config duy nhất.** Runtime/admin dùng bản PUBLISHED/hash-verified hiện hành cho command mới. Snapshot/order đang mở phải khớp setting/profile/catalog IDs và hashes đã pin trong plan/intent; current effective settings không được thay đổi cách phục hồi lệnh cũ. Recovery policy fail-closed là `paper_execution.recovery_policy=STRICT_FAIL_CLOSED`; không có nút tắt invariant/dedupe. Nếu thêm trường schema, publish qua Admin API hiện hữu và audit/version; không tạo setting store mới.
7. **Checkpoint là phần bổ sung cho ledger, không thay ledger.** Mỗi lần restore kiểm hash snapshot, scope, settings/profile lineage, order↔plan, fills ENTRY/EXIT, costs, position state và trade OPEN/CLOSED; sai khác giữ entries paused và không tự sửa PnL, stop hoặc fill.

## Phương án bị loại

- **Tái dựng chỉ từ `FillRecord`/`PositionEventRecord`:** thiếu reference price và market event không có transition nên không thể chứng minh giá entry/PnL, bars-held hoặc duplicate suppression.
- **Một hàng checkpoint mutable, không revision:** đơn giản hơn nhưng không giữ được bằng chứng snapshot trước crash/commit; khó xác định crash window và đối chiếu ledger.
- **Dùng current published settings để hydrate:** có thể đổi fill, fee, recipe hoặc stop của một position đã mở.
- **Mutate gateway rồi gọi UoW:** transaction rollback để RAM tiến lên; retry không còn deterministic.
- **Watermark-only dedupe:** không chứng minh payload của duplicate đã từng thấy, đặc biệt với nhiều symbols/retry; WP-02 giữ event-key+hash trong append-only checkpoint log.

## Hệ quả và giới hạn

- Checkpoint được ghi theo mỗi market event nên tăng write volume theo tần suất bar/số symbol. Đây là đánh đổi cho correctness đầu tiên; compaction/retention cần design riêng, chỉ làm khi có consumer cursor/dedupe proof và WBS riêng.
- Mọi đường thay gateway state (submit, market event, cancel/unknown, pause/resume, monitor, funding, reduce-only) phải đi qua lifecycle persistence boundary. Direct mutation chỉ dành domain unit/replay, không được composition root vận hành dùng.
- Snapshot/checkpoint có thể tái lập deterministic PAPER, không phải bằng chứng khớp lệnh Binance và không khôi phục credential/exchange state. Reconciliation live thuộc WP-03.
- Nếu plan decoder, policy catalog hoặc settings/profile version cũ không còn khả dụng, hệ thống giữ account paused; không “nâng” phiên bản trong lúc restore.

## Kế hoạch kiểm chứng

- Unit: canonical serialization/hash, roundtrip mọi order/position lifecycle state, unsupported schema/corrupt hash/profile mismatch, event retry/conflict/order, transaction callback commit/rollback.
- Compose/PostgreSQL: RLS/FORCE RLS và role grants; revision serialization; commit → dispose gateway/process → hydrate → cùng input tạo cùng kết quả; rollback → retry không double fill/cost/trade; crash sau commit trước callback → restart phục hồi; workspace khác nhìn 0 checkpoint; migration `alembic check`, downgrade/upgrade.
- Không gọi Binance, không credentials, không external order request.
