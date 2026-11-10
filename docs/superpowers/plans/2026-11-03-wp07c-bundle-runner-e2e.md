# WP-07C Bundle → PAPER Runtime → Runner Compose E2E

**Trạng thái:** Hoàn thành kiểm chứng ngày 03/11/2026.

## Mục tiêu

Bổ sung bằng chứng Compose/PostgreSQL cho bundle replay đã kiểm hash chạy qua observation builder, context provider tuần tự, PAPER runtime evaluator và `BacktestRunner`, sau đó lưu scenario/report/trade outcomes qua repository hiện có. Test dùng dữ liệu tổng hợp offline; quyền dữ liệu thật và rights revalidation vẫn do E2E backfill/manifest hiện có kiểm tra riêng.

## Phạm vi

- Thêm fixture tích hợp thuần Python tạo bundle đa timeframe, manifest replay và context seed PAPER rõ workspace/version.
- Mở rộng Compose E2E outcome persistence để chạy bundle fixture qua `run_backtest_dataset_runtime_replay`, lưu kết quả PostgreSQL và xác nhận retry idempotent.
- Giữ nguyên các test runtime fixture đang xác nhận trade outcomes có đóng lệnh; test mới xác nhận tuyến bundle mới và report persistence.
- Cập nhật WP-07C WBS, project status và implementation memory sau khi E2E đạt.

## Chấp nhận

1. Bundle có các manifest con READY, cùng workspace/symbol/evaluation window/rights fingerprint và hash tái lập.
2. Runtime comparison hoàn tất qua runner với số observation khớp bundle; không phát request mạng hoặc lệnh sàn.
3. Report/scenario được lưu trên PostgreSQL theo workspace; retry cùng kết quả trả cùng row/hash, không nhân bản.
4. Test runtime outcome cũ tiếp tục chứng minh fill/close/PnL trade outcomes được lưu và đọc lại.

## Kiểm chứng

- Unit liên quan đến bundle, observation/context replay và outcome repository.
- Compose `tests/e2e/test_backtest_outcomes_persistence.py` qua PostgreSQL.
- Ruff/format cho file Python thay đổi và `git diff --check`.

## Giới hạn

Lát này chưa nối UI/CLI, worker queue, tiến độ/hủy, mark-to-market hay macro calendar; fixture dùng dữ liệu tổng hợp và rights fixture, không đại diện giấy phép dùng market data thật.

## Kết quả

- Fixture đa timeframe tạo manifest `1h`/`4h` có hash và rights lineage nhất quán, rồi chạy đủ 4 observation qua `run_backtest_dataset_runtime_replay()`.
- Compose E2E lưu report/scenario của bundle lên PostgreSQL, retry idempotent và đọc lại hash outcome rỗng; cùng test tiếp tục lưu/đọc lại trade outcome đóng từ runtime PAPER fixture hiện có.
- Kết quả cuối: context unit **6 passed**; toàn bộ unit **2526 passed**; Compose `tests/e2e/test_backtest_outcomes_persistence.py` **1 passed**; Ruff check/format và `git diff --check` đạt.
- E2E backfill riêng vẫn xác minh replay loader kiểm policy hiện hành và chặn sau revoke. Fixture mới không gọi mạng, không kết nối sàn và không đại diện dữ liệu/giấy phép thật.
