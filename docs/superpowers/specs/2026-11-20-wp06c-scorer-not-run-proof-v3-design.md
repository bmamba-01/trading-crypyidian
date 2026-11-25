# Thiết kế proof TEST phân biệt `NOT_RUN` cho scorer

## Ý định

Sửa projection scorer TEST để giữ nguyên khác biệt mà ledger replay đã ghi: `EVALUATED + NOT_RUN` là đầu vào hợp lệ có vector nhưng pipeline chưa gọi strategy; `INPUT_EXCLUDED + NOT_RUN` là dữ liệu đầu vào bị loại trước đánh giá. Proof hiện tại gộp cả hai thành `input_excluded`, làm sai count và bỏ reason code.

## Mục tiêu

- Proof mới có schema v3, status `not_run`, count riêng `not_run_count`, và reason code có nguồn từ opportunity row đã persist.
- `input_excluded` tiếp tục là trạng thái riêng và giữ reason code.
- Completeness không đạt khi có `not_run` hoặc `input_excluded`; report downstream tiếp tục `INCONCLUSIVE`.
- Giữ nguyên canonical bytes, hash và khả năng đọc của proof v2 đã lưu. Không tái diễn giải hay cập nhật row cũ.
- Dùng quyền SELECT metadata `backtest_decision_opportunity` hiện có của candidate builder; không cấp thêm quyền sửa hoặc đọc payload market.
- Admin có thể thấy count `not_run` sau current-rights recheck. Với proof v2, hiển thị chưa xác định thay vì coi count là 0.

## Không mục tiêu

- Không đổi class label, candidate retention, ngưỡng scorer, quyết định scorer, baseline, hoặc logic quyền hiện hành.
- Không biến `NOT_RUN`, `NO_CANDIDATE` hay input bị loại thành trade, nhãn PnL hoặc mẫu lớp 0.
- Không dùng QA làm bằng chứng quyền non-QA; không tạo entitlement giả; không mở `SHADOW` hoặc `PAPER_GATE`.
- Không sửa migration lịch sử hoặc viết lại proof/report v2 đã persist.
- Không đọc raw sample, feature, label, trade, payload report đầy đủ để dựng count giao diện.

## Cách hiện hành và ranh giới bị lỗi

`ReplayDecisionOpportunity` v3 đã persist `input_state`, `strategy_state`, vector lineage và `reason_code`. Test proof reader chỉ chiếu `input_state`/`strategy_state`, rồi biến cả `INPUT_EXCLUDED` lẫn mọi `NOT_RUN` thành `input_excluded`. Domain, ORM và trigger PostgreSQL hiện chỉ nhận ba status v2. Candidate builder đã có SELECT metadata trên bảng opportunity nên không cần grant mới. Sửa Python đơn lẻ vẫn bị trigger từ chối hoặc cho ra count sai.

## Các hướng đã cân nhắc

1. **Giữ proof v2 và từ chối build khi gặp `NOT_RUN`.** An toàn hơn việc phân loại sai và không cần migration, nhưng không tạo được proof đầy đủ cho run có pipeline stop; reason code vẫn không được lưu trong proof.
2. **Thêm proof v3 tương thích ngược — chọn hướng này.** V2 giữ nguyên bytes/behavior và chỉ đọc; proof mới tách `not_run`, lưu reason cho cả `not_run` và `input_excluded`, thêm count nullable cho row v2, cập nhật trigger và projection admin. Quyền metadata opportunity và quyền admin đọc proof đã tồn tại nên migration không mở rộng ACL. Cần migration cùng E2E PostgreSQL nhưng giữ được inventory có thể kiểm toán.

## Hợp đồng v3

- Mỗi entry vẫn chỉ chứa stable ID đã băm và lineage/outcome hashes. `reason_code` chỉ là mã chẩn đoán nội bộ, không chứa dữ liệu mẫu; v3 entry luôn có key `reason_code`, giá trị `null` cho `candidate`/`no_candidate`, chuỗi không rỗng cho `input_excluded`/`not_run`.
- `EVALUATED + NOT_RUN` ánh xạ sang status `not_run`; chỉ lấy `reason_code` từ `backtest_decision_opportunity` authoritative row và giữ nguyên `vector_hash` đã chụp. Entry này không có candidate/outcome/sample linkage. `INPUT_EXCLUDED + NOT_RUN` ánh xạ sang `input_excluded`, giữ reason riêng và không có vector lineage.
- Proof v3 thêm `not_run_count`; tổng entry hợp lệ bằng candidate + no-candidate + input-excluded + not-run. `completeness_verified` chỉ true khi không có excluded, not-run, gap, mismatch hay candidate thiếu nhãn.
- Proof v2 canonical key set, entry key set, hash và status semantics giữ nguyên. Row v2 có `not_run_count IS NULL`; row v3 bắt buộc có giá trị, kể cả 0.
- Lần build mới tạo v3. V2 row đang có không được nâng cấp hoặc sửa. Retry trên scope đã có v2 mà không thể chứng minh phép chiếu v3 tương đương phải fail closed bằng lỗi conflict có mã rõ; không trả v2 như thể đã giữ reason/state mới.
- Evaluation report không đổi canonical shape trong lát này: report đã bind exact proof hash và `candidate_completeness_verified=false`; admin lấy riêng `not_run_count` từ proof row sau khi đã recheck quyền report. Với row v2, count là `null`/chưa được lưu.

## Ràng buộc và kiểm chứng

- TDD unit cho mapping cả hai trạng thái, reason, vector lineage, count, completeness, và golden canonical v2.
- PostgreSQL Compose E2E kiểm v2 bytes còn hợp lệ; validator chạy dưới candidate-builder chấp nhận payload v3 gồm hai trạng thái/reason/vector trước khi FK attempt chưa seed từ chối; payload sai state/reason/count bị trigger từ chối. Source-linked Compose tiếp tục kiểm writer/repository thật trên lineage đầy đủ; ACL không đổi, downgrade v3 populated bị chặn.
- Repository E2E phải đi qua reader/writer thật, không chỉ gọi helper Python.
- Admin Playwright desktop/mobile xác nhận nhãn Việt ngữ, count v3, trạng thái chưa xác định cho v2 và không tràn ngang.
- Chạy regression scorer proof/access/report và kiểm sole Alembic head. `SHADOW`/`PAPER_GATE` vẫn khóa.

## Nguồn và ngày kiểm tra

- Kế hoạch Task7 đã duyệt: `docs/superpowers/plans/2026-11-08-wp06-task7-opportunity-universe-plan.md`, hợp đồng opportunity có `EVALUATED + NOT_RUN`, vector exact và reason code.
- PostgreSQL 18, kiểm ngày 20/11/2026: [ALTER TABLE](https://www.postgresql.org/docs/18/sql-altertable.html), [GRANT](https://www.postgresql.org/docs/18/sql-grant.html), [CREATE TRIGGER](https://www.postgresql.org/docs/18/sql-createtrigger.html). Migration chỉ thêm cột nullable, grant SELECT theo cột và thay validator insert; không phụ thuộc API/dependency mới.

