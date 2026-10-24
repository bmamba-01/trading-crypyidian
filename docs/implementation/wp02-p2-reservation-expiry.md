# WP-02 P2 — Reservation expiry and PAPER revalidation

## WBS trước khi implement

1. **Mô hình domain:** gắn `expires_at` bất biến vào `RiskReservation`; hết hạn chỉ tự giải phóng trạng thái `RESERVED` chưa có intent. `UNKNOWN` và reservation đã liên kết intent luôn tiếp tục giữ hạn mức cho tới khi reconcile.
2. **Persistence/risk repository:** thêm cột `expires_at` có timezone, lấy giá trị từ `RiskDecision.candidate_expires_at`; giữ idempotency và account row lock; thêm thao tác expire các reservation đủ điều kiện trong transaction.
3. **PAPER ledger:** khi tạo intent, khóa reservation và kiểm tra thời điểm hiện tại còn trước `expires_at`; nếu hết hạn hoặc timestamp/expiry thiếu/naive thì từ chối tạo intent.
4. **Migration:** tạo revision nối head hiện tại; backfill expiry từ payload của risk decision; không cho phép reservation thiếu expiry; mở rộng trigger chỉ cho phép chuyển `RESERVED → RELEASED/EXPIRED_BEFORE_SUBMIT`.
5. **Kiểm thử:** unit cho biên đúng hạn/hết hạn, idempotent retry, UNKNOWN không tự hết hạn, intent fail-closed và SQL/RLS scoping; Compose E2E dùng PostgreSQL thật để xác nhận expiry persist, transition, intent bị chặn, rồi migration downgrade/upgrade.

## Điều kiện nghiệm thu

- Thời điểm hết hạn gắn với reservation được lưu bền vững và không thể sửa sau khi tạo.
- `now >= expires_at` không thể reserve mới hoặc tạo PAPER intent; so sánh chính xác tại biên.
- Chỉ `RESERVED` chưa có OrderIntent mới được tự release khi hết hạn; `UNKNOWN` hoặc đã có intent không được giải phóng chỉ vì đồng hồ chạy qua hạn.
- Reservation hết hạn không còn chiếm risk/notional/margin sau thao tác expiry đã commit; lần retry cùng key không thể làm sống lại reservation đã terminal.
- Timestamp thiếu, naive, dữ liệu migration không thể backfill hoặc lỗi truy vấn phải đóng an toàn (fail-closed).
- Unit/E2E liên quan, kiểm tra Alembic và migration round-trip chạy đạt; không tạo lệnh live.

