# WP06 Task 3I — Cách ly ý định học trước replay

**Phạm vi:** giữ kết quả replay dùng làm dữ liệu nền huấn luyện tách khỏi replay nghiên cứu thông thường, từ lúc xếp job tới mọi nơi đọc job/result.

## Hợp đồng

- `RESEARCH` là mặc định tương thích ngược; manifest giữ nguyên canonical hash lịch sử.
- `TRAINING_BASE` là ý định bất biến trong payload/canonical request của job và trong manifest; manifest hash gắn với UUID job.
- Candidate builder chỉ chấp nhận run từ job `SUCCEEDED`, request canonical khớp chính xác, job được tạo trước run, result chứa run và bundle/strategy/seed khớp manifest. Kiểm tra này chạy trước quyền TRAINING và trước khi đọc report/outcome.
- Đọc danh sách kết quả từ service/CLI/admin không trả run ID cho intent training hoặc intent không nhận diện. UI hiển thị trạng thái cách ly; đây không phải quyền duyệt split hay quyền disclosure holdout.
- Replay hiện chỉ mô phỏng PAPER trong DB; không gửi lệnh giao dịch.

## TDD và kiểm chứng

- RED: constructor chưa nhận learning intent; candidate proof không từ chối run replay thường; reader kết quả còn lộ run ID của job training.
- GREEN: migration `c2d4e6f8a0b3` cấp builder đúng quyền SELECT metadata job dưới tenant RLS; request/manifest/job binding; fail-closed ở candidate proof; result ID quarantine ở service, CLI và admin.
- Unit liên quan: 98 passed; Playwright admin replay desktop/mobile: 1 passed; Compose candidate proof: 1 passed. Ruff, mypy và `git diff --check` đạt.
- Ảnh Playwright: `tests/e2e/evidence/wp07c-backtests-desktop.png` và `tests/e2e/evidence/wp07c-backtests-mobile.png`.

## Còn mở

Quarantine không thay thế split approval. Task3 vẫn cần admin API/UI có MFA/CSRF cho approval trước seal; Task4 restart/PID proof, Task7 shadow/PAPER/drift, Task8 registry UI và Task9 full journey/security review cũng còn mở. Không bật scorer; `candidate_completeness_verified=false`; không có order/LIVE capability.
