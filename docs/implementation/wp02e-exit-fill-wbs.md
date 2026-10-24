# WP-02E — PAPER exit fill shared ledger

## WBS trước khi implement

1. **Schema:** thêm `fill_role` (`ENTRY`/`EXIT`) vào `fill`, backfill fill cũ thành `ENTRY`, rồi áp ràng buộc enum; migration nối sau `5b8c3f4a1d92`.
2. **Repository:** giữ `append_fill` là ENTRY; thêm `append_exit_fill` khóa intent/trade, kiểm tra lượng vị thế còn mở, ghi exit fill idempotent và không đổi `OrderIntent.filled_quantity` hay entry order.
3. **Chi phí và trạng thái:** ghi phí/slippage exit vào cùng trade bằng khóa idempotent; ghi position event giảm/đóng để ledger dùng chung có thể tái dựng vị thế.
4. **Kiểm thử:** RED→GREEN cho partial/full exit, retry giống payload, retry xung đột, vượt lượng mở, thiếu trade/closed trade và không double-count entry; Compose E2E chạy trên PostgreSQL thật, kiểm tra migration rollback/re-upgrade.

## Điều kiện nghiệm thu

- Mọi fill mới lưu rõ vai trò `ENTRY` hoặc `EXIT`; dữ liệu cũ sau migration là `ENTRY`.
- Exit fill chỉ gắn với trade PAPER đang mở và không vượt lượng còn mở.
- Retry cùng fill key/payload không tạo thêm fill, trade cost hay position event; payload khác bị từ chối.
- Exit fill không tăng `OrderIntent.filled_quantity`, không thay đổi entry exchange order và không tạo trade thứ hai.
- Fee/slippage của exit được ghi đúng quote asset một lần; fill/position/cost cùng được ghi trong transaction caller.
- Unit và Compose E2E liên quan cùng migration check/downgrade/upgrade đạt; không có lệnh live.
