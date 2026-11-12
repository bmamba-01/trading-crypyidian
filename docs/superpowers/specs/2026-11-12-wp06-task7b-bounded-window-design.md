# Thiết kế WP06 Task7B: cửa sổ quan sát có giới hạn

**Ngày:** 2026-11-12
**Phạm vi:** chỉ cải thiện assessment chất lượng dữ liệu của learning observer; không thay đổi đánh giá hiệu năng scorer hoặc quyền bật mode.

## Vấn đề

Task7B hiện gom tất cả observation của cùng workspace, registry và revision. Observation cũ có thể che lấp việc coverage/chất lượng gần đây suy giảm. Bản ghi assessment đã có `window_start`/`window_end`, nhưng hiện chúng chỉ phản ánh thời điểm sớm nhất/muộn nhất trong toàn bộ lịch sử được gom.

## Cách tiếp cận

1. **Giữ toàn bộ lịch sử:** không giới hạn thời gian; đơn giản nhưng có thể pha loãng thay đổi gần đây.
2. **Lấy N observation gần nhất:** giới hạn kích thước cohort nhưng độ dài thời gian thay đổi theo tần suất bot.
3. **Cửa sổ thời gian 30 ngày:** giới hạn theo thời điểm quan sát, độc lập với tốc độ phát observation; mẫu ít vẫn được báo `INCONCLUSIVE`.

Chọn cách 3 làm chính sách vận hành khởi điểm, phiên bản `coverage-v2-30d`, với thời lượng 30 ngày được lưu cùng assessment. Đây không phải horizon phổ quát hoặc kết luận từ nghiên cứu; sẽ giữ hạn chế này rõ trong tài liệu. Nghiên cứu cửa sổ thích nghi cho thấy độ dài cửa sổ phụ thuộc thay đổi dữ liệu; lát cắt hiện tại chỉ dùng metadata chất lượng và không có label/outcome để hiệu chỉnh một bộ phát hiện thích nghi.

## Hợp đồng

- Server định nghĩa `observation_window_days=30`, `minimum_observations=3`, `minimum_snapshot_coverage=0.8`, `minimum_quality_rate=0.9`; client không truyền ngưỡng hoặc ngày bắt đầu/kết thúc.
- Sau source-rights lock và cohort lock, đọc `transaction_timestamp()` từ PostgreSQL. Truy vấn chỉ lấy observation thỏa `as_of - 30 ngày <= observed_at <= as_of` trong đúng workspace/registry/revision.
- Đưa thời điểm DB làm `window_end` và điểm sớm nhất trong các member đã chọn làm `window_start`. Membership digest ghim chính xác các member; khi quan sát cũ rời khỏi cửa sổ, membership và idempotency scope đổi.
- Lưu 30 ngày trong policy metadata của assessment, ràng buộc `coverage-v2-30d` với chính xác 30 ngày, và thêm index theo workspace/registry/revision/`observed_at`.
- Giữ `minimum_observations=3`. Dưới mức này, chất lượng dữ liệu là `INCONCLUSIVE`.
- Nếu mọi challenger đều `ABSTAIN`, **performance** vẫn `INCONCLUSIVE`. Assessment data-quality vẫn có thể là `NO_DRIFT` hoặc `DETECTED` dựa trên coverage/chất lượng metadata; không diễn giải thành đánh giá model.
- Không đọc label, outcome, PnL hay holdout; không tạo training job, không đổi registry pointer, scorer mode hoặc entry gate. Proposal tiếp tục chỉ là metadata chờ người duyệt.
- Admin hiển thị phiên bản/chính sách 30 ngày cùng phạm vi thời gian thực tế của member; không thêm payload mẫu.

## Kiểm chứng

- Unit: hợp đồng policy v2, truy vấn có hai cận thời gian, DB time được lấy trước cohort read, cutoff bao gồm/loại chính xác biên, membership/idempotency, low-N và ABSTAIN semantics.
- PostgreSQL Compose: một observation cũ hơn 30 ngày không lọt vào assessment; observation trong cửa sổ được giữ theo thứ tự ổn định; DB lưu chính sách 30 ngày; RLS, current-rights lock, immutability, retry và populated downgrade guard vẫn đúng.
- Playwright desktop/mobile: hiển thị nhãn chính sách 30 ngày, thời gian và trạng thái inconclusive; không có dữ liệu mẫu hoặc tràn ngang. Lưu screenshot mới nếu giao diện đổi.
- Ruff, format, mypy, Alembic single-head/check, full unit và các E2E bị tác động phải đạt.

## Nguồn và giới hạn

Bifet và Gavaldà mô tả cửa sổ thích nghi ADWIN và nêu rằng kích thước cố định có thể cần chọn theo tốc độ thay đổi. Nguồn này hỗ trợ việc coi 30 ngày là quyết định vận hành tạm thời, không phải ngưỡng tối ưu: [Learning from Time-Changing Data with Adaptive Windowing (2007)](https://doi.org/10.1137/1.9781611972771.42). Kiểm tra nguồn: 2026-11-12.
