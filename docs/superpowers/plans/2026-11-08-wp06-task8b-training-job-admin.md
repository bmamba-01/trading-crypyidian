# WP06 Task 8B — Admin enqueue và theo dõi training job

## Phạm vi

- Trang tiếng Việt, responsive để xem các fold đủ điều kiện, enqueue training, theo dõi hàng đợi và yêu cầu hủy.
- API lấy workspace/operator từ session; thay đổi cần CSRF, quyền phù hợp và MFA gần đây.
- Fold lựa chọn chỉ lấy từ profile ngân sách đã lưu và approval khớp exact manifest/fold-plan. Caller không gửi counts, label, feature, holdout, quyền hoặc actor.
- Resource request được server dựng từ profile và policy cố định. Attempt/job được ghi cùng transaction theo idempotency; trial index được tuần tự hóa.
- Hủy chỉ ghi yêu cầu có actor; worker dừng child và chốt trạng thái cuối. UI chỉ nhận metadata/hash/status/error code đã redact.
- Việc enqueue không cấp quyền payload hoặc strategy; worker vẫn kiểm approval/quyền nguồn hiện hành trước disclosure. Scorer tiếp tục `ABSTAIN`; không có order/LIVE.

## TDD và nghiệm thu

1. Unit kiểm permission/MFA, idempotency, resource profile authoritative và request schema cấm identity/count injection.
2. PostgreSQL Compose kiểm exact approval/profile, workspace isolation, server-derived budget, idempotent retry, cancel actor và boundary không đọc payload.
3. Playwright thao tác enqueue/cancel, theo dõi trạng thái, kiểm tiếng Việt/font/responsive desktop-mobile và lưu screenshot.
4. Chạy test WP06/job/API liên quan, Ruff/format/mypy, Alembic check (nếu có migration), `git diff --check`; cập nhật và đọc lại implementation-memory.

## Không nằm trong lát này

- Không cho admin truy cập feature/label/holdout payload hoặc cấp quyền TRAINING.
- Không thêm retry tự động trên attempt đã tiêu thụ receipt; retry nghiệp vụ phải là attempt mới sau current approval/rights gate.
- Không nối model scorer vào bot hoặc kích hoạt order.
