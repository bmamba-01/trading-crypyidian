# Kế hoạch E2E từ chối QA cho writer Shadow của WP06

> **Hướng dẫn triển khai:** Thực hiện lần lượt từng mục và viết test trước. Đây chỉ là lát kiểm chứng từ chối; không cấp quyền dữ liệu hoặc bật chế độ scorer. Phép thử QA riêng cho race membership của Drift đã nằm ở Task 7 trong kế hoạch consumer và được chạy trong cùng Compose E2E; kế hoạch này không tạo test trùng lặp.

**Mục tiêu:** Xác nhận writer `LearningShadowApplication.observe_pending` thật ghi observation gắn nguồn QA với lý do `SOURCE_RIGHTS_INELIGIBLE` và chỉ lưu metadata, retry idempotent, không gây side effect giao dịch hoặc scorer.

**Kiến trúc:** Mở rộng probe source-scope PostgreSQL Compose dùng một lần bằng decision QA first-party đã persist và fixture challenger đã đăng ký. Gọi application bằng tài khoản DB observer; dùng kết nối có quyền kiểm thử riêng để đọc observation cùng các ledger liên quan.

**Công nghệ:** Python 3.14, SQLAlchemy async, PostgreSQL/Alembic, pytest, Docker Compose.

**Đặc tả:** `docs/superpowers/specs/2026-11-13-wp06-source-use-scope-consumers-design.md`.

## Ràng buộc chung

- Lineage QA chỉ chứng minh hành vi từ chối; tuyệt đối không coi là license hoặc deployment đủ điều kiện.
- Giữ deployment scorer ở `DISABLED`; không ghi score event, intent, order hoặc position.
- Giữ fixture operator WP06F riêng; không tái sử dụng.
- Chỉ persist metadata cần thiết theo contract observation Shadow hiện có; không tiết lộ feature, probability hoặc score payload.
- Không đổi hành vi production hoặc schema trừ khi E2E chỉ ra lỗi đúng đắn cụ thể.
- Sau kiểm chứng, cập nhật implementation memory và trạng thái WBS/module; đọc lại phần memory vừa cập nhật.

## Điểm cần rà soát

- Có challenger đang chờ: test phải chứng minh application xử lý decision gắn QA thật, không chỉ trả batch rỗng.
- Chỉ seed các hàng writer Shadow cần; không tạo PAPER trade không liên quan trong fixture này.
- QA bị từ chối: row lưu phải có reason không đủ quyền rõ ràng, rights/source-scope fingerprint là NULL.
- Retry: lần gọi application thứ hai không tạo observation mới.
- Side effect: ledger `BotDecision`, `OrderIntent`, order, position, score-event và deployment pointer không đổi.
- Tối thiểu hóa dữ liệu: observation lưu và dữ liệu trả ra API không chứa probability, feature hoặc model score payload.

## Task 1: Chứng minh writer application chỉ từ chối QA

**Tệp:**

- Sửa `tests/e2e/probes/wp06_shadow_drift_source_scope.py`.
- Sửa `tests/e2e/test_wp06_shadow_drift_source_scope.py`.
- Sau kiểm chứng, cập nhật `docs/superpowers/plans/2026-11-14-wp06-exact-source-use-consumers.md`, trạng thái WBS/module và implementation memory.

- [x] Thêm kết quả E2E kỳ vọng; RED ban đầu xác nhận thiếu ba kết quả của writer.
- [x] Dùng setup kiểm thử dùng một lần để seed decision/lineage source-bar QA first-party và challenger đã đăng ký; application và kiểm tra exact-scope đều dùng observer login.
- [x] Gọi `LearningShadowApplication.observe_pending`; xác nhận có đúng một row mới với `SOURCE_RIGHTS_INELIGIBLE`, rights/source-scope fingerprint NULL, allowlist metadata chính xác, không có PAPER trade không liên quan và ledger decision/order/score/deployment không đổi.
- [x] Kiểm tra decision chỉ có đúng nguồn QA và exact-scope gate hiện hành trả `None` trước lẫn sau khi gọi writer.
- [x] Retry cùng lời gọi application; xác nhận idempotent, không thêm row.
- [x] Chạy Compose E2E Shadow/PAPER và unit Shadow liên quan với 0 skip; chạy Ruff, kiểm tra format và `git diff --check`.
- [x] Ghi kết quả chính xác vào implementation memory, đọc lại phần vừa ghi và cập nhật liên kết plan/WBS.
- [x] Re-review độc lập xác nhận tuân thủ thiết kế và chất lượng mã, không còn finding.

**Chưa kiểm:** entitlement bên ngoài đủ điều kiện, `SHADOW`/`PAPER_GATE` active, scorer commit thành công chạy đua với thu hồi quyền, hoặc hành vi operator entry-hold.
