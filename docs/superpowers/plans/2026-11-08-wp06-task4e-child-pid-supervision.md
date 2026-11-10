# WP06 Task 4E — Xác minh dừng child process và restart worker

## Hợp đồng

- Khi nhận cancel hoặc vượt wall deadline, trainer parent terminate child, chờ child thoát và escalates sang kill nếu cần.
- Lỗi đã làm sạch có thể mang PID child để phục vụ đối soát vận hành; PID không chứa payload hoặc thông tin bí mật.
- Attempt bị hủy không lưu artifact; receipt đã commit vẫn giữ nguyên và attempt/job nhận terminal event theo ledger hiện có.
- Worker supervisor phải hoạt động lại sau khi container `trainer-worker` được restart; restart không làm mất job đã xếp hàng.

## TDD và kiểm chứng

- RED Compose probe: cancellation xảy ra nhưng `LearningTrainingProcessFailure` chưa có PID để khẳng định child đã được reap.
- GREEN: runner trả PID sau `terminate()`/`join()`; probe chạy trong Linux Compose container xác nhận PID khác parent và `os.kill(pid, 0)` báo tiến trình không còn.
- `tests/e2e/test_wp06_training_process_supervision.py`: **1 passed/3.61s**.
- `tests/e2e/test_wp06_normalized_fold_samples.py`: **1 passed/175.07s**, gồm restart `trainer-worker` khi idle, enqueue sau restart, supervisor xử lý job đến artifact/event/job `succeeded`, lease recovery, queued/running cancellation, cgroup và ACL/RLS.
- Focused process/trainer unit: **13 passed**; Ruff, format, mypy source và `git diff --check` đạt.

## Giới hạn

Compose E2E chạy restart service lúc idle rồi xác nhận job sau restart; lease recovery trước/sau receipt được test riêng. Chưa mô phỏng SIGKILL container giữa đúng đoạn child đang fit, nhưng production child PID cancellation đã được kiểm trực tiếp trong Linux container và worker cancel lifecycle vẫn được kiểm bằng PostgreSQL Compose.
