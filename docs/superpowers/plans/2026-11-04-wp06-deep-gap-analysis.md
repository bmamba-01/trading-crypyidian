# Phân tích sâu khoảng trống WP-06

**Ngày đối chiếu:** 04/11/2026 <br>
**Phạm vi:** đọc Plan 06, WBS, implementation memory, project status, source và test đang có; không sửa code, chạy test, gọi model/provider hoặc dữ liệu thị trường. <br>
**Nguồn ngoài:** không tra cứu thêm trong vòng này vì không chọn framework, API hoặc phiên bản mới. Các nguồn phương pháp hiện có được lưu tại Plan 06 và WBS với ngày kiểm tra 01–02/11/2026.

## Kết luận

WP-06 đã có nền hợp đồng offline khá sâu: scorer fail-closed, DSL giới hạn, nhãn từ outcome đã đóng, manifest bất biến, lineage nguồn typed, rights invalidation, temporal split/purge, evaluator report contract và PostgreSQL verifier outcome đã được kiểm thử. **WP-06 chưa là chu trình huấn luyện end-to-end:** chưa có verifier DB chứng minh tập candidate đầy đủ, split/holdout approval và access gate, composition evaluator, persistence báo cáo learning, trial-family/fold accounting đầy đủ, trainer, model registry, promotion, shadow/PAPER hoặc drift lifecycle.

Điểm cần giữ rõ khi cập nhật docs:

1. `DatasetEvidenceVerifier` trong `DatasetManifestBuilder` và `DatabaseFoldOutcomeVerifier` là hai port khác nhau. Port thứ nhất chưa có adapter authoritative chứng minh membership/completeness của feature, label, split và rights. Port thứ hai đã có `DatabaseFoldOutcomeVerifier` đọc outcome/trade được chỉ định trong snapshot PostgreSQL chỉ đọc và **đã có unit regression cùng Compose E2E**.
2. Evaluator có application function dựng report in-memory từ fold outcome đã xác minh. Phần còn thiếu là composition root/UoW, lưu báo cáo learning và vòng đời trial; không nên ghi “chưa có evaluator code” hoặc “adapter chưa có test”.
3. Không nên viết rằng toàn repo “chưa có mark-to-market”. Domain `mark_to_market_equity` và repository/read model account equity đã có; khoảng trống còn lại cụ thể là replay evaluator và `paper_account_state` dùng realized-only equity, cùng semantics day/rolling loss chưa được đóng.
4. Rights-event consumer và expiry scanner WP05/WP06G.2 hiện đã nối vào ingestion worker theo workspace. Điều này không đồng nghĩa có source connector được duyệt hay source data đủ quyền để train.

## Bằng chứng theo gói

| Gói | Đã có và có evidence trong repo | Khoảng trống thực tế |
| --- | --- | --- |
| WP06.1 scorer/abstention | `domain/learning/scoring.py`, `application/learning_scoring.py`, preflight rights/as-of/schema, deadline và nhánh lỗi `ABSTAIN`; unit tests có trong memory. | Chưa có model adapter/runtime composition. Transport thực phải tự dọn tài nguyên nếu không hợp tác cancellation. Scorer không có quyền gửi lệnh. |
| WP06.2 DSL/experiment | Parser/type checker/compiler bounded, allowlist feature, canonical AST/hash và giới hạn cấu trúc; tests `test_learning_dsl.py`. | Chưa có execution/evaluator tích hợp. Không đưa DSL thành mã Python tùy ý hoặc thêm framework qua phần contract. |
| WP06.3 label/manifest | Label chỉ từ outcome hoàn tất; `DatasetManifestBuilder` xác minh candidate qua verifier port; manifest canonical, repository immutable/RLS workspace và trainer-worker SELECT-only; v3 typed source refs/reverse index. | Chưa có adapter dữ liệu authoritative hoàn chỉnh hoặc proof rằng candidate query đã bao phủ toàn universe. Caller vẫn chọn candidate list. Chưa có biểu diễn ổn định cho sample 0 trade hoặc nhiều trade/sample; split approval và holdout access control còn thiếu. |
| WP06G rights | `DatasetRightsChangeConsumer`, eligibility ledger, `DatasetSourceEligibilityGate`, WP05 expiry scan và rights-event consumer trong worker. Source-dependent evaluation yêu cầu live gate; gate fail-closed với backlog/revoke/revision/hash/access mismatch. | Không tạo dữ liệu có quyền: WP05 vẫn thiếu source connector/provider được duyệt và propagation tới blob/artifact. Không backfill rights event lịch sử trước consumer. |
| WP06.4 temporal/evaluator | `select_walk_forward_samples`, fold-plan v1/v2, overlap purge; `build_walk_forward_evaluation_report`; `DatabaseFoldOutcomeVerifier` rehydrate manifest/fold, kiểm tenant/read-only snapshot, exact sample/role/outcome/trial/run lineage, interval và canonical trade row hash. | WBS/memory ghi evidence **74 unit passed + 1 PostgreSQL Compose E2E passed** (02/11/2026); giới hạn còn lại là composition, report persistence, đầy đủ trial-family/fold accounting và các nhánh DB riêng. Report verifier chỉ kiểm liên kết report; không hydrate/verify aggregate metrics/hash để tránh đọc dữ liệu holdout ngoài fold. Chỉ 1 trade row/sample được hỗ trợ; 0/multi-trade bị từ chối. |
| WP06.5–.8 | Chưa thấy trainer, artifact registry, promotion workflow, shadow/PAPER model challenger hoặc drift/retrain pipeline trong module ownership hiện tại. | Cần hoàn tất data/holdout gates trước trainer; sau đó mới artifact, approval, shadow/PAPER rollback, drift. Không được tự activate LIVE. |

### Hai loại verifier cần phân biệt

- `DatasetEvidenceVerifier` là cổng mà builder gọi cho **từng candidate**. Hợp đồng yêu cầu adapter dựng sample từ DB/source/split policy đáng tin cậy, nhưng chưa có implementation authoritative đủ để chứng minh query completeness, zero/multi outcome representation hoặc phê duyệt split.
- `DatabaseFoldOutcomeVerifier` là adapter **đã có** để xác minh những UUID outcome/trade cụ thể mà fold plan yêu cầu. E2E không chứng minh candidate universe đầy đủ, và việc verifier đạt không thay thế verifier của manifest builder.

Không gom hai mục này thành trạng thái “verifier còn thiếu” hoặc “WP06.4 chưa được kiểm thử”.

## Thứ tự triển khai đề xuất

### P0 — Freeze candidate, split và outcome representation

Trước schema mới, xác định query universe/cutoff nào là nguồn authoritative; cách tạo sample 0 trade, multi-trade và trade được sửa/đóng muộn; ai duyệt split; cách niêm phong holdout, ghi nhận truy cập và cấm chọn/tune lại theo holdout. Giữ candidate exclusions cùng reason/version. Fixture WP-02 offline có thể dùng để chốt contract.

Không cho `caller-supplied samples` được diễn giải là đầy đủ. Nếu completeness chưa chứng minh, report phải tiếp tục gắn `candidate_completeness_verified=False`.

### P1 — WP06.3 authoritative sample verifier + completeness proof

Tạo adapter read-only tenant-scoped từ feature snapshot, trade/outcome/label và split policy đã duyệt. Kiểm point-in-time, sample identity, completeness digest/count/exclusion, label completion/correction, schema/version, source refs/revision/rights. Source-dependent sample chỉ bật sau WP05 rights/access contract hiện hành. Lưu manifest trong repository immutable hiện có, không sửa canonical legacy hash.

### P2 — WP06.4 composition và evaluator report persistence

Nối `build_walk_forward_evaluation_report` qua một UoW snapshot `REPEATABLE READ|SERIALIZABLE READ ONLY` bao toàn bộ fold; gắn `DatasetSourceEligibilityGate` cùng workspace/transaction. Sau khi đọc snapshot, lưu learning evaluation report qua append-only storage riêng, có canonical payload/hash, evaluator/manifest/fold/seed/metrics lineage và idempotency key. Khi persist phải xử lý rights đổi giữa read snapshot và write transaction; không dùng report đã lưu như bằng chứng rằng candidate list đầy đủ. Giữ holdout ngoài payload report và ngoài query outcome của fold.

Thêm Compose kiểm quyền thật, workspace/RLS, snapshot isolation, rollback, retry idempotency, exact fold selection, rights revoke/backlog và report readback/hash. Bổ sung unit/E2E cho trial mismatch, missing/malformed row, interval boundaries, rights failure và report write conflict. Không biến test verifier hiện có thành bằng chứng cho composition/report store mới.

### P3 — Trial-family/fold accounting và protocol đánh giá

Tạo ledger cho mọi trial, kể cả failed/invalid/rejected; ghim family hash, index/count, evaluator/code/dependency, dataset/fold/hash, seed, cost/fill assumptions và trạng thái. Tách threshold/selection khỏi sealed holdout và quyết định số lượt thử/tolerance/uncertainty trước model search.

### P4–P6 — Trainer → registry/approval → shadow/PAPER → drift

- Trainer CPU đầu tiên cần chạy tách process/role, đọc manifest đã được duyệt và chỉ ghi metadata/artifact; không exchange secret/order capability. Chọn framework sau khi kiểm Python runtime compatibility, license, reproducibility, serialization security, CPU/resource envelope và artifact retention; ghi official docs/date lúc có quyết định.
- Registry chỉ nhận artifact bất biến kèm manifest/evaluation hashes; promotion cần actor, policy/version/reason và human approval tới shadow/PAPER. Rollback giữ lịch sử và có thể block entry theo policy; không route AI sang LIVE.
- Drift/retrain thấp mẫu phải là `inconclusive`/proposal, không tự promote. Không tạo source coverage metric nếu WP05 chưa có contract nguồn/freshness.

## Ràng buộc và phụ thuộc

- WP-03 Binance Demo không phải phụ thuộc cho offline WP-06. Không dùng model hoặc learning service để tạo order.
- WP-05 quyền/lineage chặn source-derived sample, nhưng WP-06.3/.4 có thể tiến trên WP-02 outcomes đã persist và fixture không phụ thuộc source.
- Không thêm ML dependency trong P0–P3. Không nạp holdout aggregate hoặc report metrics từ toàn trial để đơn giản hóa verifier.
- Mark-to-market: nền account equity đã có tại `domain/risk/equity.py::mark_to_market_equity` và `adapters/database/repositories/account_equity.py`. Tuy vậy `RuntimeReplayEvaluator` còn cập nhật `initial_equity + realized_pnl`, `paper_account_state` mô tả realized-only, `realized_loss_today`/`realized_loss_rolling` hiện là accumulator không có time-window reset/zone semantics. WBS WP-02 phải mô tả đúng phần replay/risk window này; WP-06 report giữ caveat và không tuyên bố account MTM là evaluator MTM.
- Các quyết định cần chủ sản phẩm khi triển khai đúng bước: authoritative dataset universe/query owner; zero/multi-trade aggregation; split approver/holdout reader; source purpose/provider/retention; model objective, threshold/cost envelope, artifact retention; policy risk window/timezone nếu dùng làm acceptance metric. Không cần hỏi để làm fixture/offline audit trước.

## Tài liệu và tests được đối chiếu

- `docs/implementation/06-ai-learning.md`, `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md`, `docs/implementation/project-status.md`, `docs/implementation/implementation-memory.md` (đặc biệt các mục 02/11/2026 WP06.4 contract, verifier E2E và G1/G2).
- Code: `application/learning_dataset.py`, `learning_fold_plan.py`, `learning_fold_evaluation.py`, `learning_scoring.py`, `learning_temporal_evaluation.py`, `adapters/database/learning_fold_outcome_verifier.py`, `repositories/dataset_manifest.py`, `repositories/dataset_manifest_eligibility.py`, domain `learning/` và `risk/equity.py`.
- Tests: `tests/unit/test_learning_dataset_application.py`, `test_learning_fold_evaluation.py`, `test_learning_fold_plan.py`, `test_learning_scoring_application.py`, `test_learning_temporal_evaluation.py`, `tests/e2e/test_learning_fold_outcome_verifier.py`, `test_wp06_dataset_manifest.py`, `test_wp06_rights_invalidation.py`.
- **Không chạy lại test trong audit này.** 74 unit + 1 PostgreSQL E2E là evidence đã ghi trong memory/WBS cho lát WP06.4 ngày 02/11/2026; không phải kết quả kiểm mới ngày 04/11/2026. Không browse nguồn ngoài vì không phát sinh quyết định API/library/method mới.

## Hạng mục tài liệu cần root đồng bộ

1. `docs/implementation/06-ai-learning.md` phần mở đầu/WP06.4 và WP06G.2: bỏ câu còn nói adapter “chưa có tests”, giữ “composition/report persistence chưa có”.
2. `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md` đầu tài liệu và bảng WP06.4: coi 74 unit + 1 Compose E2E là evidence đã có; “verifier” chưa hoàn tất ở WP06.3 là **sample verifier/completeness**, không phải fold outcome verifier.
3. `docs/implementation/project-status.md` dòng US-27: không ghi evaluator chưa có; ghi evaluation contract/DB verifier đã có nhưng composition/report persistence/trainer chưa có.
4. WP-02/Plan 06 caveat: nói chính xác MTM domain/account snapshot đã có nhưng replay evaluator realized-only và loss window chưa đủ semantics.

**Trạng thái phân tích:** hoàn tất; đây là báo cáo chỉ đọc, chưa sửa WBS/status/memory/code và chưa nghiệm thu WP-06.
