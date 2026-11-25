# Thiết kế WP06F: Model card scorer và độ trễ signal→PAPER intent

Ngày: 2026-11-20, cập nhật 2026-11-21. Trạng thái: đã chốt phương án dữ liệu; đang triển khai theo plan.

## Mục tiêu

Mỗi scorer artifact/report có một trang model card tiếng Việt, gắn chặt với hash artifact và report đã đánh giá. Trang giúp owner/risk-admin hiểu model dùng cho việc gì, được đánh giá bằng dữ liệu/policy nào, giới hạn nào còn hiệu lực, và model có trạng thái triển khai/rollback ra sao.

Trang cũng trình bày thống kê vận hành PAPER về thời gian từ signal tới intent. Chỉ đưa số liệu aggregate, không cấp quyền đọc trực tiếp hàng `order_intent`, không suy diễn candidate không có intent thành tín hiệu bị bỏ lỡ hay khoản lỗ, và không coi latency là chất lượng hoặc lợi nhuận model.

## Bối cảnh hiện tại

- `SharedSignalEvidenceRecord` lưu `observed_at`, `available_at`, `created_at`; `BotDecisionRecord` lưu `evidence_id`, `model_release_id`, `created_at`; `OrderIntentRecord` có `bot_decision_id`, `mode`, `created_at`. Các liên kết định danh đang có nên có thể đo latency mà không thêm bản sao timestamp.
- Scorer TEST report lưu artifact hash, policy/version, manifest/fold lineage, proof/report hash, support và evaluation status; read model hiện tại gọi current-rights gate rồi mới chiếu whitelist aggregate.
- `crypidian_scorer_history` bị giới hạn cột và RLS cho lịch sử score. Không được cấp quyền bảng `order_intent` trực tiếp cho role này.
- Checkout hiện có 9 mẫu/3 mỗi lớp, dưới policy 1.000 tổng/100 mỗi lớp; report phải giữ `INCONCLUSIVE`, không công bố rate/score như bằng chứng hiệu quả, và active modes đang khóa.

## Mục tiêu, ngoài phạm vi và điều cấm

**Mục tiêu**

- Tạo trang riêng, responsive desktop/mobile, toàn bộ nhãn/hướng dẫn bằng tiếng Việt, gắn với đúng model artifact và TEST report.
- Chiếu lineage/hash/policy/status/support/giới hạn đã lưu; chỉ hiển thị metric khi trạng thái và policy cho phép. Với `INCONCLUSIVE`, hiển thị count mô tả và giải thích thiếu dữ liệu/quyền, ẩn KPI hiệu năng.
- Tính ba aggregate thời gian: signal available→PAPER intent (`available_at`→`OrderIntent.created_at`), evidence persisted→intent (`SharedSignalEvidence.created_at`→intent), và BotDecision→intent (`BotDecision.created_at`→intent). Kèm số mẫu hợp lệ và p50/p95; timestamp âm/không hợp lệ được đếm riêng và không tham gia percentile.
- Hiển thị riêng số `CANDIDATE` không có PAPER intent trong cửa sổ truy vấn, không gắn nhãn “missed signal”, không giả định nguyên nhân.
- Recheck current source rights cho exact decision/bar lineage dưới operation aggregate report trước khi đọc/trả latency; nếu quyền thiếu/thu hẹp/thu hồi, từ chối toàn bộ aggregate, không trả một phần.
- Dùng aggregate-only `SECURITY DEFINER` projection với search_path đóng, owner không đăng nhập/không bypass RLS, workspace/bot/window được kiểm từ context server. Không cấp SELECT vào `order_intent`, `bot_decision`, hay raw signal evidence cho scorer-history reader.

**Ngoài phạm vi**

- Mua dữ liệu, tạo license/evidence/receipt, thay ngưỡng policy, mở `SHADOW`/`PAPER_GATE`, đặt lệnh LIVE/PAPER, hoặc tự động promote/rollback.
- Lưu thêm latency event/row, canonical proof, sample/label/trade/evidence payload, PnL hoặc thông tin định danh intent trên model card.
- Gọi candidate không có intent là missed opportunity; phân tích nguyên nhân chặn entry; hoặc đưa ra cam kết lợi nhuận.

**Điều cấm**

- Không đọc/đưa vào HTML JSON canonical report/proof, sample IDs, labels, probabilities, order/trade IDs, license bytes hoặc member-level data.
- Không biến no-intent, timestamp lỗi, thiếu rights hay thiếu support thành latency 0, VETO, thua lỗ hoặc metric thuận lợi.
- Không để model card làm thay đổi scorer state, BotDecision, RiskEngine, order intent hay bot state.

## Các phương án

1. **A — Aggregate từ ledger hiện có (đã chọn ngày 2026-11-21):** dùng FK `SharedSignalEvidence→BotDecision→OrderIntent` và timestamp DB/event hiện có; trả count + percentile qua function aggregate giới hạn quyền, sau current-rights gate. Không tạo latency ledger trùng; có thể thêm migration cho function/quyền tối thiểu. Đổi lại, chỉ đo được chặng có persisted intent; không đo queue-stage timestamp chưa được lưu.
2. **B — Ledger latency bất biến:** thêm bảng/sự kiện ghi checkpoint ở từng chặng, migration và write path runtime mới. Tách stage rõ hơn nhưng nhân bản timestamp, mở rộng transaction/race/ACL surface và cần backfill không thể suy ra cho lịch sử.

**Ruling:** dùng A theo lựa chọn của người dùng. Thêm aggregate-only SQL gateway và grant tối thiểu; không thêm bảng latency hoặc write path.

## Contract dữ liệu

- Card neo vào exact `scorer_artifact_hash` và gồm base artifact hash + exact TEST report hash/proof hash/policy hash; registry state chỉ nối khi artifact hash khớp chính xác. Nếu thiếu lineage thì fail closed hoặc hiển thị “Chưa có report gắn với artifact này”, không ghép report mới nhất bất kỳ.
- Các trường card tối thiểu: danh tính/version/hash artifact; mục đích và chế độ được phép; manifest/fold/split policy; sample count và class support; report status; metric theo gate; current rights recheck status; phê duyệt/active pointer/rollback event; giới hạn, giả định và ngày đánh giá.
- Latency report dùng cửa sổ UTC 24 giờ, 7 ngày hoặc 30 ngày; mặc định 7 ngày. `as_of` là `transaction_timestamp()` của PostgreSQL và cohort dùng nửa-mở `[as_of - window, as_of)`, neo theo `SharedSignalEvidence.available_at` (signal thuộc kỳ khi khả dụng trong kỳ); latency kết thúc tại `OrderIntent.created_at`. Candidate không có intent cũng được đếm trong cùng cohort theo BotDecision/evidence. Filter workspace/bot/model do server lấy từ identity/request đã kiểm, không tin JSON caller.
- Tập tính latency chỉ gồm PAPER `OrderIntent` có exact `bot_decision_id`, BotDecision cùng workspace/bot, evidence ID khớp và scorer artifact hash khớp release. Không dùng JSON `OrderIntent.evidence` làm nguồn lineage.
- Ba chặng được đặt tên: signal available→intent (`available_at`→`OrderIntent.created_at`), evidence persisted→intent (`SharedSignalEvidence.created_at`→intent), và decision persisted→intent (`BotDecision.created_at`→intent). Chặng cuối đo thời gian từ lúc quyết định được lưu tới lúc intent được lưu; không phải thời gian evaluator/model tính chiến lược (`model_latency_ms`).
- Với từng chặng, chỉ duration `>= 0` và có đủ timestamp hợp lệ tham gia percentile. Tính `percentile_cont` tuyến tính; làm tròn về mili-giây nguyên gần nhất (nửa mili-giây làm tròn lên). Trả `valid_sample_count`, `invalid_timestamp_count`, `p50_ms`, `p95_ms`. Chỉ công bố p50/p95 khi chặng đó có ít nhất 100 mẫu hợp lệ; dưới ngưỡng thì count vẫn hiện và percentile là null/“Chưa đủ mẫu”.
- `candidate_no_intent_count` chỉ đếm quyết định không có hàng OrderIntent khớp chính xác. Intent có `created_at >= as_of` là bất thường timestamp tương lai, đếm riêng và loại khỏi latency; không gọi trường hợp này là chưa có intent. Mỗi chặng có `invalid_timestamp_count` riêng cho duration âm/không hợp lệ; mẫu lỗi không tham gia percentile.
- Current-rights gate phải xác minh toàn bộ member decision/bar trong cửa sổ cho operation `paper_demo + aggregate_report + aggregate_reporting` trước aggregate SELECT. Gateway đóng băng tập member exact-lineage, khóa các source ID theo thứ tự toàn cục xác định, tái kiểm membership sau khóa và gọi current-scope authorizer cho cả member có intent lẫn member không có intent. Một member thiếu scope làm cả request bị từ chối; không rớt member khỏi mẫu số và không persist/cache kết quả quyền cũ.
- Card TEST vẫn recheck quyền bằng `wp06_recheck_scorer_test_report_source_rights` trước whitelist projection. Card không lộ evidence_ref, digest giấy tờ hoặc license bytes; chỉ hiển thị quyền hiện tại đã qua/chưa qua.
- Khi TEST report là `INCONCLUSIVE`, ẩn metric đánh giá model nhưng vẫn có thể hiển thị latency như telemetry vận hành riêng nếu current PAPER aggregate-rights gate qua; luôn kèm sample count/cửa sổ và không gọi đây là metric chất lượng.

## UI và ngôn ngữ

- Thêm trang model card riêng từ report scorer, liên kết ở admin scorer; trang có tiêu đề, điều hướng quay lại, vùng lineage, evaluation status, limits và khối PAPER latency.
- Desktop dùng section/card và bảng hỗ trợ screen reader; mobile xếp dọc, không tràn ngang, p50/p95 hiển thị tiếng Việt kèm đơn vị ms. Màu xanh/đỏ không dùng để biểu diễn hiệu năng tốt/xấu khi report còn `INCONCLUSIVE`.
- Khi rights gate từ chối, trang hiển thị thông báo tiếng Việt trung tính, không hiển thị một phần aggregate. Khi chưa có intent hoặc không có sample hợp lệ, hiển thị “Chưa có dữ liệu đủ điều kiện”, không số 0 latency.

## Kiểm thử nghiệm thu

- Unit: exact artifact/report linkage, required card fields, metric visibility matrix, `INCONCLUSIVE` privacy, latency duration boundaries, negative/missing timestamps, no-intent counts không tạo duration.
- PostgreSQL Compose: aggregate function chỉ trả count/percentile; direct SELECT bị từ chối cho scorer-history role; workspace/bot isolation; current-rights denial/revoke/shrink không trả partial metric và không tạo ledger; exact lineage mismatch bị từ chối.
- Playwright desktop/mobile: card tiếng Việt, exact hashes, `INCONCLUSIVE` hide KPI, rights denial không rò metric, latency 24h/7d/30d, responsive không tràn.
- Positive rights/latency aggregate E2E chỉ chạy với nguồn PAPER non-QA có receipt/quyền thật; fixture QA không thay bằng chứng đó. Cho đến khi có quyền, denial path/UI và function ACL phải kiểm được, còn positive gate được ghi Blocked.
- Re-review độc lập phải kiểm role/RLS/SQL function, no leakage, current-rights order, metric timestamp semantics và không thay active mode. Update WBS, module, glossary, project status, memory; unit + Compose + Playwright phải đạt trước commit.

## Nguồn

- Model Cards for Model Reporting, Mitchell et al., FAT* 2019; kiểm tra 2026-11-20: https://research.google/pubs/model-cards-for-model-reporting/ · DOI: https://doi.org/10.1145/3287560.3287596.
- Contract timestamps/lineage đang có: `src/trading_bot/adapters/database/models/signal_entities.py`, `paper_entities.py`, `learning_scorer_test_access_entities.py`; exact report rights gate: `migrations/versions/f8e0a2c4d6bd_wp06_report_source_disclosure_gate.py`.

## Kết quả builder check

Ngày 20/11/2026, reviewer yêu cầu ba quyết định:

1. A hay B: người dùng chọn A ngày 2026-11-21; triển khai tổng hợp từ ledger đã có, không tạo latency ledger.
2. Mốc lọc cửa sổ: chọn `SharedSignalEvidence.available_at`, khoảng nửa-mở `[window_start, as_of)`; `OrderIntent.created_at` chỉ là endpoint đo, không quyết định cohort.
3. Khi TEST `INCONCLUSIVE`: cho phép latency telemetry riêng hiện nếu current PAPER aggregate-rights gate qua; vẫn ẩn mọi metric đánh giá model và ghi rõ sample count/cửa sổ. Đây là chỉ số vận hành, không phải hiệu quả model.
4. Support tối thiểu latency: 100 mẫu hợp lệ riêng cho từng chặng; count nhỏ hơn vẫn hiển thị, percentile không tính/không hiển thị.
5. Intent ghi timestamp từ `as_of` trở đi là timestamp anomaly riêng; không tính như latency và không nhập vào count “không có intent”.

## Rà soát ngữ nghĩa và nguồn triển khai (2026-11-21)

- PostgreSQL xác nhận `percentile_cont` nội suy giữa các mẫu kề nhau; model card dùng đúng continuous percentile và làm tròn thành mili-giây nguyên. `transaction_timestamp()` cố định theo đầu transaction nên tạo một mốc `as_of` nhất quán cho cả lần đọc. Tài liệu hiện hành đã kiểm: https://www.postgresql.org/docs/current/functions-aggregate.html và https://www.postgresql.org/docs/16/functions-datetime.html (2026-11-21).
- Quyền PAPER được kiểm bằng `wp06_authorize_paper_performance_decision_source_scope` theo từng decision, cả member chưa có intent. Gateway không trả phần aggregate nếu bất kỳ member nào bị từ chối. Việc kiểm TEST report tiếp tục qua `wp06_recheck_scorer_test_report_source_rights` trước whitelist projection.
- Hiện chưa có nguồn PAPER non-QA được chứng minh đủ quyền/support trong checkout. Vì vậy QA denial và Playwright không thể được diễn giải thành positive entitlement; active modes vẫn khóa và metric latency chỉ hiện khi current PAPER gate thật sự cho phép.
