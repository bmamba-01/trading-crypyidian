# Thiết kế WP06.4: lưu báo cáo đánh giá chẩn đoán

**Ngày:** 2026-11-06 <br>
**Trạng thái:** phương án được chọn theo chỉ đạo đã có về triển khai WP-06; chỉ tạo báo cáo chẩn đoán, chưa cấp quyền huấn luyện hay promotion.

## Ý định

Nối evaluator walk-forward đã có với manifest và outcome PostgreSQL đã persist, rồi lưu báo cáo bất biến để có thể đọc lại theo workspace và retry an toàn. Người vận hành có thể xem kết quả chẩn đoán trên dữ liệu WP-02 đã lưu mà không nhầm một danh sách candidate do caller cung cấp với một dataset đầy đủ.

## Hiện trạng liên quan

- `build_walk_forward_evaluation_report()` đã xác minh manifest/fold/sample/trade qua verifier port và luôn ghi `candidate_completeness_verified=False`.
- `DatabaseFoldOutcomeVerifier` đã kiểm manifest/fold, workspace, trial/run/report lineage và trade hash trong transaction `REPEATABLE READ` hoặc `SERIALIZABLE`, `READ ONLY`.
- `DatasetManifestRepository` đã lưu manifest bất biến và tái dựng canonical hash.
- Đã có service composition/UoW và kho report WP06 riêng: service đọc manifest/outcome trong snapshot read-only rồi ghi report canonical/idempotent bằng transaction khác. Chưa có verifier authoritative chứng minh toàn bộ universe; zero-trade/multi-trade sample chưa được biểu diễn.

## Mục tiêu

1. Đọc manifest và mọi outcome của fold trong cùng workspace-scoped, read-only snapshot.
2. Tái dựng fold plan từ manifest lưu thật; không nhận payload manifest/outcome tuỳ caller làm nguồn sự thật.
3. Lưu report canonical, hash, evaluator/split lineage và idempotency key trong bảng riêng, append-only.
4. Cùng workspace + key + canonical payload phải trả lại cùng record; cùng key với payload khác phải conflict.
5. Giữ `candidate_completeness_verified=false` và caveat WP-02 trong report lẫn constraint DB.
6. Không truy vấn payload của holdout. Chỉ lấy outcome UUID được fold plan yêu cầu; fold plan hiện tại chỉ chọn sample DEVELOPMENT.

## Ngoài phạm vi

- Source-dependent manifest, cho tới khi quyền nguồn có thể được kiểm lại đồng bộ với lần persist report.
- Chứng minh candidate completeness, zero/multi-trade aggregation, duyệt split/holdout, trial-family accounting, model training, registry, promotion, shadow/PAPER hoặc drift.
- UI, CLI/job lifecycle và lên lịch chạy. Report service/repository này là backend slice; surface khởi chạy/quan sát thuộc lát điều phối WP06 tiếp theo.
- Sửa `backtest_report`, đổi evaluator metrics, thêm ML dependency hoặc gọi provider/venue.

## Các cách đã cân nhắc

1. **Gộp vào `backtest_report`:** tái dùng bảng và một số model. Bỏ vì backtest report là evidence của run/trial cụ thể; report đánh giá WP06 tổng hợp folds và có completeness caveat khác.
2. **Chờ WP06.3 completeness:** tránh mọi khả năng bị hiểu nhầm thành kết quả train. Bỏ vì báo cáo chẩn đoán có giá trị khi được gắn caveat, bị khóa khỏi trainer và không được coi là promotion evidence.
3. **Bảng WP06 riêng, append-only:** giữ phân biệt report, dùng hash/idempotency, workspace RLS và role tối thiểu. **Chọn phương án này** theo WBS/gap analysis; chi phí là migration và phải giữ caveat/DB gate chính xác.

## Thiết kế được chọn

- Application service nhận workspace, manifest hash, fold definitions, cutoff, seed và UUID idempotency key.
- Read UoW dùng process role `crypidian_trainer_worker`; mở transaction `REPEATABLE READ READ ONLY`, bind workspace, nạp manifest, từ chối source-dependent sample, dựng fold plan rồi gọi evaluator với `DatabaseFoldOutcomeVerifier` trên cùng session.
- Fold plan/report chỉ giữ train/validation/test thuộc DEVELOPMENT; sealed holdout không có trong plan hay payload report. Verifier chỉ truy vấn outcome UUID đã yêu cầu.
- Write UoW riêng cùng workspace dùng `crypidian_trainer_worker`; repository khóa idempotency theo workspace/key và append report canonical. Không ghi gì trong read-only snapshot.
- `learning_evaluation_report` giữ idempotency key, report/manifest/fold hashes, split/evaluator version, cutoff, seed, payload JSONB và canonical JSON; `evaluation_mode='diagnostic'`, completeness luôn `false`.
- Bảng bật FORCE RLS; admin-web chỉ SELECT; trainer-worker SELECT/INSERT; không UPDATE/DELETE. Trigger DB ngăn sửa/xoá, unique constraint workspace/key, constraint completeness false.
- Report service không được dùng làm trainer/promotion gate. Source-dependent manifest bị từ chối trước verifier/persist cho tới khi có flow kiểm rights hiện hành và cạnh tranh revoke.

## Kiểm chứng và giới hạn

- Unit: canonical payload/hash, exact fold membership, holdout không rò, completeness false, từ chối source-dependent, idempotent same-key retry và conflict khi đổi payload.
- PostgreSQL Compose ngày 2026-11-06 đã đạt: Alembic upgrade/check/one head; report append/readback/hash, same-key retry, changed-payload conflict, workspace/RLS, trainer SELECT/INSERT, admin SELECT-only, update/delete denial và downgrade guard khi còn report.
- Các test chỉ chứng minh report chẩn đoán trên records đã lưu; không chứng minh completeness, profitability, khả năng tổng quát hoá hoặc quyền dữ liệu ngoài.

## Từ ngữ

`Diagnostic evaluation` được dịch là **đánh giá chẩn đoán**: bản tổng hợp kết quả trên các sample đã chọn, có ích để kiểm tra evaluator và dữ liệu đầu vào nhưng không phải bằng chứng tập candidate đầy đủ, không đủ điều kiện huấn luyện hoặc promotion. Thêm định nghĩa EN→VI và giới hạn vào glossary WP06.
