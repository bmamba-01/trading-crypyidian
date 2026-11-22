# Kế hoạch triển khai heartbeat worker CK-9

> **Dành cho người thực hiện:** dùng quy trình `superpowers:executing-plans`; đánh dấu các bước bằng cú pháp ô chọn.

**Mục tiêu:** Lưu trạng thái và hoạt động của ingestion-worker theo workspace, cung cấp cho Admin và hiển thị số liệu stream cùng tick cuối trên thanh trạng thái chart.

**Kiến trúc:** Tạo bảng heartbeat mới nhất theo workspace, component và instance của worker. Ingestion-worker định kỳ ghi các bộ đếm được giới hạn qua repository; admin-web đọc hàng mới nhất trong phạm vi workspace và đánh dấu heartbeat quá hạn là `not_ready`. Các worker thị trường công khai vẫn chỉ đọc API sàn; lỗi ghi heartbeat không được làm dừng việc thu thập dữ liệu. Bổ sung bộ đếm reconnect và tick thành công cho worker hiện có. Dock chart đọc endpoint readiness và hiển thị trạng thái, bộ đếm, số lần reconnect và thời điểm tick. Giữ bộ lọc kỳ lịch sử hiện có, đồng thời thêm phân trang offset cho nhật ký quyết định, 20 dòng mỗi lượt.

**Công nghệ:** Python 3.13, SQLAlchemy async, Alembic, FastAPI, PostgreSQL RLS, pytest, Playwright.

## Các tệp liên quan

- `src/trading_bot/adapters/database/models/worker_heartbeat_entities.py`: ORM cho trạng thái worker mới nhất.
- `src/trading_bot/adapters/database/models/__init__.py`: đăng ký ORM với metadata của Alembic.
- `migrations/versions/0095..._ck9_worker_heartbeat.py`: tạo bảng, chính sách RLS theo workspace, quyền ghi ingestion-worker và quyền đọc admin-web.
- `src/trading_bot/adapters/database/repositories/worker_heartbeats.py`: upsert theo workspace và truy vấn hàng mới nhất.
- `src/trading_bot/application/ingestion_worker_heartbeat.py`: DTO an toàn cùng dịch vụ ghi/đọc định kỳ.
- `src/trading_bot/workers/live_market_ingestion_worker.py`: cung cấp tick thành công cuối và bộ đếm reconnect.
- `src/trading_bot/app.py`: chỉ khởi chạy publisher ở ingestion-worker; cung cấp reader có phạm vi cho admin-web.
- `src/trading_bot/admin/web_router.py`: gộp heartbeat đã lưu vào phản hồi readiness.
- `src/trading_bot/templates/admin/market_chart.html`, `src/trading_bot/static/chart-dock.js`: hiển thị trạng thái stream và số liệu.
- `tests/unit/test_ingestion_worker_heartbeat.py`, các test liên quan đến migration/router, `tests/e2e/probes/live_kline_ingestion.py` và `tests/e2e/test_live_kline_ingestion_persistence.py`: kiểm tra snapshot, trạng thái quá hạn, quyền role, cách ly RLS, luồng ghi/đọc và status chart trên trình duyệt.
- `docs/superpowers/plans/2026-11-15-ck-chart-cockpit-reference-apps-wbs.md`, `docs/implementation/project-status.md`, `docs/implementation/implementation-memory.md`: cập nhật phạm vi CK-9 và ghi kết quả đã xác minh.

## Công việc

- [x] Thêm unit test cho snapshot heartbeat, phân loại trạng thái quá hạn/còn hiệu lực, bộ đếm theo component và cách gộp readiness.
- [x] Mở rộng probe Compose để kiểm tra upsert heartbeat, quyền chỉ đọc của Admin và RLS giữa các workspace; chưa chạy được do Docker Desktop lỗi.
- [x] Thêm kiểm tra Playwright cho trạng thái stream và phân trang nhật ký quyết định; E2E tập trung đã đạt.
- [x] Thêm ORM model, migration, repository và DTO/service có giới hạn dữ liệu.
- [x] Bổ sung bộ đếm reconnect và tick cuối; nối publisher vào vòng đời ingestion-worker, cô lập lỗi và dừng gọn.
- [x] Đọc heartbeat bằng dịch vụ admin-web có phạm vi workspace và thêm vào phản hồi readiness.
- [x] Hiển thị trạng thái stream trên chart và phân trang nhật ký 20 mục; giữ bộ lọc kỳ lịch sử hiện có.
- [x] Chạy E2E Compose/PostgreSQL để xác minh RLS và quyền role; `tests/e2e/test_live_kline_ingestion_persistence.py` đạt sau khi Docker Desktop được restart bằng CLI.
- [x] Cập nhật WBS CK-9, project status và implementation memory; đọc lại memory mới ghi và đối chiếu diff/kết quả kiểm tra.
