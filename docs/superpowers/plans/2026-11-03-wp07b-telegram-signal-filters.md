# Kế hoạch — bộ lọc subscription Telegram WP-07B

## Quyết định thiết kế

- Lưu năm allowlist (`symbols`, `timeframes`, `directions`, `strategy_ids`, `setup_codes`) dạng JSONB trên từng subscription.
- Mảng rỗng nghĩa là không giới hạn tiêu chí đó; PUT cũ chỉ có `enabled` giữ nguyên filter hiện tại. PUT mới dùng object `filters` đầy đủ để thay bộ allowlist nguyên tử; nhóm bỏ khỏi object mặc định rỗng, nghĩa là xóa giới hạn của nhóm đó.
- Chuẩn hóa và kiểm tra ở domain/application; DB giới hạn kiểu JSON array/số phần tử và worker chỉ được đọc các cột cần thiết.
- Lọc trước khi tạo delivery và xác minh lại khi claim. Delivery đang chờ nhưng không còn khớp được đóng `FILTERED`, có receipt nhưng không gửi.
- Thêm GET để UI đọc trạng thái và mở rộng PUT có CSRF. Trang chi tiết bot có form tiếng Việt, phân tách danh sách bằng dấu phẩy/dòng mới và nêu rõ semantics của danh sách rỗng. Giữ `enabled`, binding, workspace và quyền bot hiện hành.

## Tasks

1. Thêm `TelegramSignalFilters` bất biến với chuẩn hóa uppercase, uniqueness/order ổn định, giới hạn 100 phần tử mỗi nhóm, kiểm symbol/timeframe/direction và `matches(candidate)`. Test default wildcard, match/mismatch từng chiều, input sai và canonicalization.
2. Thêm migration kế tiếp sau `d4f6a8c0e2b1`: năm JSONB array columns/default/check constraints; trạng thái delivery `FILTERED`; admin update và signal-worker SELECT grants chỉ cho các filter columns; downgrade fail khi có delivery rows không thể export an toàn.
3. Nối repository, subscription state/service và GET/PUT API. Payload legacy chỉ `enabled` giữ nguyên filters; object `filters` thay nguyên tử cả năm allowlist, `[]` chủ ý xóa giới hạn nhóm. Audit record chứa cấu hình filter, không chứa chat ID/token.
4. Nối `TelegramSignalDeliveryWorker`: chỉ enqueue khi target filter khớp; lúc claim dùng filter hiện tại, chuyển pending item không khớp thành `FILTERED` và tiếp tục vòng claim. Không tạo delivery cho candidate bị lọc trước enqueue.
5. Bổ sung unit/API/admin/migration/worker tests và Compose E2E: filter long BTCUSDT/1h/H01/setup; khẳng định candidate khác không được gửi, đổi filter khi delivery đang chờ làm receipt thành `FILTERED`, GET/PUT giữ scope và DB worker vẫn không có execution privilege. Đã có E2E PostgreSQL cho candidate khớp và candidate đổi sang không khớp khi pending.

## Kiểm chứng

- Chạy focused unit trước/sau cho domain, API, repository, worker và migration.
- Chạy Compose E2E Telegram signal hiện hành cùng test mới; E2E phải chạy `alembic upgrade head` và `alembic check`.
- Chạy full `tests/unit`, Ruff, mypy files liên quan, Compose config, Alembic heads/check và `git diff --check`.

## Giới hạn

- Bộ lọc chỉ chọn candidate đã qua BotDecision; không kích hoạt strategy, không tạo order và không xác nhận độ chính xác/tính sinh lời.
- Đã triển khai API và HTML form chỉnh allowlist trên bot detail. Quiet hours, cooldown, per-chat daily cap, dashboard lịch sử/trạng thái delivery, live feed ingestion và retry/dead-letter matrix toàn Bot API vẫn là các task kế tiếp.
