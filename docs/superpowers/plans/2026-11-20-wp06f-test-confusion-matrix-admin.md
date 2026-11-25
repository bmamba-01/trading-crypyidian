# WP06F — Chiếu ma trận nhầm lẫn TEST lên admin

## Mục tiêu và giới hạn

Đưa ma trận nhầm lẫn aggregate đã lưu trong TEST report qua read model scorer và hiển thị trên trang admin tiếng Việt. Giữ nguyên current-rights recheck trước khi đọc metadata; không đọc toàn bộ report/canonical payload, sample, nhãn hoặc trade từng dòng. Không thêm migration/schema/API/dependency và không mở `SHADOW`/`PAPER_GATE`.

Ma trận có đúng 3 hàng nhãn thực tế `-1, 0, 1` và 4 cột nhãn dự đoán `-1, 0, 1, ABSTAIN`. Mỗi ô là số nguyên không âm; tổng hàng khớp class support theo thứ tự, tổng toàn ma trận khớp số mẫu TEST, tổng cột `ABSTAIN` khớp `abstain_count`. `ABSTAIN` nghĩa là scorer không đưa ra phân lớp; ma trận này không biểu diễn hành động entry PAPER vì dự đoán argmax có thể khác quyết định VETO/ABSTAIN ở tầng policy.

## Tệp và trách nhiệm

- `src/trading_bot/domain/learning/scorer_report_summary.py`: thêm ma trận, số đếm false-veto và phép suy ra FP/FN aggregate-only.
- `src/trading_bot/adapters/database/repositories/learning_scorer_report_read_model.py`: allowlist JSON metadata (`confusion_matrix`, `false_veto_count`) và xác thực chiều, kiểu, bất biến count sau khi rights recheck.
- `src/trading_bot/admin/learning_scoring_page_router.py` và `templates/admin/learning_scoring.html`: truyền nhãn tiếng Việt, render bảng accessible trên desktop và ba thẻ hàng dễ đọc trên mobile.
- `src/trading_bot/static/pages/learning.css`: style riêng cho bảng 3×4.
- `tests/unit/test_learning_scorer_report_read_model.py`, `test_learning_scorer_admin_page.py`: kiểm projection, rights gate, metadata minimization và lỗi schema/count.
- `tests/e2e/test_learning_scorer_admin_ui.py`: kiểm tiếng Việt, dữ liệu aggregate, không lộ payload và layout desktop/mobile; tạo screenshots cập nhật.
- Cập nhật WP06 WBS/module/status, glossary và implementation memory sau khi kiểm chứng.

## Trình tự TDD

1. [x] Cập nhật fixture và viết unit assertions cho projection matrix, 3×4 dimensions, bool/string/negative cell, row support, TEST total và ABSTAIN total; RED xác nhận thiếu metadata allowlist/projection.
2. [x] Thêm strict parser/DTO và JSON metadata key; giữ nguyên thứ tự latest-report lookup → current-rights recheck → metadata projection.
3. [x] Render bảng semantic trên desktop và thẻ từng hàng trên mobile; giải thích giới hạn mẫu/quyền và phân biệt lớp dự đoán với hành động PAPER.
4. [x] Unit **24 passed**; Playwright scorer **2 passed/4.52s/0 skipped**, ảnh desktop/mobile được cập nhật và xem; không tràn ngang trang tại 390px.
5. [x] Ruff check, format (**6 files**), mypy (**544 source files**) và `git diff --check` đạt; đã cập nhật glossary/WBS/module/status/memory và kiểm đọc lại.

## Kết quả logic đã xác minh

- Current-rights recheck vẫn diễn ra trước truy vấn JSON aggregate. Chỉ thêm hai khóa aggregate `confusion_matrix` và `false_veto_count` vào allowlist; không chọn cột full `report`/`canonical_report`.
- Parser từ chối shape khác 3×4, bool/âm/không nguyên, class order khác `-1, 0, 1`, row support sai, tổng sample sai và tổng ABSTAIN sai.
- Số đếm FP/FN lớp PnL dương được suy ra từ ma trận: FP là nhãn thực `-1`/`0` bị dự đoán `+1`; FN là nhãn thực `+1` bị dự đoán `-1`, `0` hoặc `ABSTAIN`. False-veto tách riêng theo quyết định APPROVE, hiển thị tử số `false_veto_count` trên mẫu số support lớp `+1` đã lưu.
- Khi report là `INCONCLUSIVE`, chỉ hiển thị số đếm aggregate mô tả; ẩn tỷ lệ và điểm hiệu năng. Không đổi các ngưỡng hoặc trạng thái report.
- Báo cáo mẫu vẫn `INCONCLUSIVE`; fixture UI 9 mẫu (3/2/4 theo lớp) chỉ kiểm rendering, không xác nhận quyền hoặc độ tin cậy FP/FN.

## Điều kiện chưa hoàn tất

- Không có rights non-QA hoặc TEST support dưới policy: báo cáo tiếp tục `INCONCLUSIVE`, không công bố tỷ lệ FP/FN đáng tin cậy và không bật active modes.
- QA/fake fixture không được dùng làm bằng chứng quyền production.

## Follow-up CRY-323 — Số đếm FP/FN aggregate (20/11/2026)

- [x] RED: unit read-model mới thất bại vì chưa chiếu `false_veto_count` và DTO chưa có FP/FN; GREEN giữ current-rights recheck trước projection, bổ sung strict count validation và kiểm `false_veto_count / class_support(+1)` khớp tỷ lệ đã lưu.
- [x] Tính FP/FN từ ma trận TEST đã lưu; FN gồm `ABSTAIN`. Tách rõ lỗi phân lớp khỏi `false-veto` theo hành động APPROVE. Không đọc mẫu, nhãn hay trade từng dòng, không thêm migration hoặc quyền DB.
- [x] Report `INCONCLUSIVE` vẫn hiển thị count mô tả nhưng ẩn rate/score KPI. Desktop dùng bảng semantic, mobile dùng card; Playwright kiểm viewport 1280×800 và 390×844, ảnh được tạo mới và xem.
- [x] Regression hợp nhất scorer read-model/admin, runtime source-rights/committer/target, migration gate, Playwright và QA source-lock E2E → **68 passed/32.21s/0 skipped**; Ruff, format-check (8 files), mypy (544 source files), `git diff --check` đạt.
- **Còn mở sau lát FP/FN:** chưa chiếu `no_candidate_count`/`input_excluded_count`, chưa có model card độc lập tiếng Việt hoặc action-latency end-to-end; dữ liệu/quyền hiện tại không đủ để kết luận FP/FN hay hiệu quả mô hình. Projection inventory đã được hoàn thiện trong follow-up bên dưới. Không mở `SHADOW`/`PAPER_GATE`.

## Follow-up CRY-323 — Count trạng thái inventory cơ hội TEST (20/11/2026)

- Read model chiếu `no_candidate_count` và `input_excluded_count` từ đúng proof row đã được report ghim, sau current-rights recheck. Chỉ lấy hai integer columns metadata; không đọc `canonical_proof`, entry, sample, label, outcome hoặc trade. Không migration, schema/API/dependency hay quyền DB mới.
- `no_candidate_count` giữ nghĩa chính xác ở proof v2/v3: opportunity status `NO_CANDIDATE`, không phải số tín hiệu bị bỏ lỡ, lệnh/trade hoặc PnL. `input_excluded_count` chỉ được gọi riêng khi proof v3 tách `NOT_RUN` thành count non-NULL. Proof v2 có `not_run_count IS NULL`; count cũ gộp `EVALUATED + NOT_RUN` với `INPUT_EXCLUDED`, nên admin hiển thị một dòng legacy tổng hợp và không trình bày nó như input-excluded riêng.
- Tất cả count được giới hạn là int không âm; kiểm thử projection xác nhận thứ tự report ID → current-rights recheck → aggregate SELECT và cấm projection `canonical_proof`. Admin ghi rõ các trạng thái inventory không phải trade, execution hay outcome/PnL. `INCONCLUSIVE` và khóa `SHADOW`/`PAPER_GATE` giữ nguyên.
- TDD: RED unit xác nhận DTO/parser thiếu hai count; RED legacy page xác nhận UI chưa gộp đúng proof v2. Test canonical proof xác nhận v2 giữ `input_excluded_count`, không có `not_run_count`; read-model/UI giữ v2 count dưới nhãn gộp. Regression ba unit files `test_learning_scorer_report_read_model.py`, `test_learning_scorer_admin_page.py`, `test_learning_scorer_test_opportunity_proofs.py`: **66 passed/1.90s/0 skipped**. Playwright `tests/e2e/test_learning_scorer_admin_ui.py`: **2 passed/5.14s/0 skipped**, viewport 1280×900 và 390×844; ảnh desktop/mobile tạo mới và xem, không tràn ngang.
- `uv run ruff check` đạt; `ruff format --check` xác nhận **6 files already formatted**; `uv run mypy src/trading_bot` → **544 source files, no issues**; `git diff --check` đạt.
- Còn mở: model card độc lập tiếng Việt, signal→PAPER-intent latency end-to-end, quyền non-QA và support TEST policy ≥1.000 tổng/≥100 mỗi lớp. Hiện 9 mẫu vẫn `INCONCLUSIVE`; không công bố rate/hiệu năng hay mở active modes.

## Follow-up CRY-323 — Hiển thị KPI responsive (20/11/2026)

- Playwright screenshot review phát hiện metadata deployment bị cắt trên desktop bởi `white-space: nowrap`/ellipsis; nhãn tình trạng health trên mobile dùng cỡ chữ KPI quá lớn. CSS nay cho metadata xuống dòng đầy đủ, co giãn theo bề rộng và giảm cỡ nhãn trạng thái trên màn hình nhỏ.
- Regression assertion kiểm desktop 1280×900 yêu cầu `white-space: normal` và không cắt metadata KPI; mobile 390×844 giới hạn cỡ chữ nhãn KPI text không quá 17px. E2E tiếp tục kiểm toàn trang không tràn ngang và tạo lại ảnh desktop/mobile.
- TDD: RED tái hiện `nowrap`; GREEN `uv run pytest -q -rs --tb=short tests/e2e/test_learning_scorer_admin_ui.py` → **2 passed/7.59s/0 skipped**. Ảnh mới được xem trực quan; metadata không còn ellipsis, nhãn mobile dễ đọc hơn.
- Đây là chỉnh responsive của phần admin aggregate-only đã có; không thêm thuật ngữ, schema, quyền hay mode. E2E chạy standalone, không cần Docker. PostgreSQL Compose E2E của các lát khác trong vòng này chưa xác minh vì Docker/PostgreSQL healthcheck lỗi; xem implementation memory.
