# Plan 06 — AI learning có kiểm soát

**Mục tiêu:** xây vòng tạo giả thuyết → gán nhãn → train/evaluate → paper promotion → theo dõi drift/rollback; AI không có quyền đặt lệnh hoặc tự promote live.
**Phụ thuộc:** [Plan 01](./01-foundation-database.md), ledger/recipe nhất quán từ [Plan 02](./02-trading-core-paper.md), context từ [Plan 05](./05-research-ingestion.md) nếu feature đó được dùng.
**Stories:** US-15, US-23, US-26…29.
**Tài liệu:** [AI use cases](../product/use-cases.md#4-chi-tiết-use-case), [training sequence](../product/sequence-diagrams.md#sd-10--trade-review-outcome-label-và-attribution), [glossary AI](../research/glossary-vi.md#e-dữ-liệu-và-chiến-lược-huấn-luyện-ai), [strategy research](../research/strategies-2026.md).

**Foundation offline hiện có:** scorer/abstention domain contract và application service có preflight rights/as-of/schema, deadline/cancel; bounded DSL/hypothesis, sample lineage + interval purge, completed-outcome labels và canonical dataset manifest. `DatasetManifestBuilder` xác minh sample qua verifier port; manifest được lưu immutable theo workspace với trainer-worker SELECT-only. `build_manifest_fold_plan` gắn split/cutoff/sample selection với manifest hash. Chưa có verifier adapter/repository đầy đủ, trial ledger, trainer, evaluator/report integration, model registry hay runtime/promotion; caller-supplied candidate list chưa chứng minh completeness.

## Ranh giới phụ thuộc và thứ tự khởi động

- Có thể bắt đầu offline trước WP-05: đóng băng scorer/abstention contract; DSL chỉ nhận feature/recipe đã có trong WP-02; tăng cường sample-level temporal validation, replay, trial accounting và test leakage trên dữ liệu PAPER đã persist. Các bước này không được đọc hoặc giả định có research feature.
- Có thể thiết kế schema label/manifest trước WP-05, nhưng dataset chỉ nhận outcome WP-02 đã đóng đủ fill/fee/funding và feature point-in-time hiện có. Không đưa provisional outcome vào train. Mọi record dựa trên research source phải chờ WP-05 cung cấp source identity, rights, revision và `available_at`/`as_of` semantics.
- Trainer, registry/promotion và drift dựa trên research feature chỉ bắt đầu sau khi manifest, temporal evaluator và rights contract đạt. WP-03 Binance Demo không phải dependency của offline/PAPER learning; không dùng WP-06 để mở venue execution hoặc LIVE.
- Baseline replay của WP-02 có thể được tái sử dụng, nhưng không giả định rằng fixed time-gap đã loại mọi leakage. Với từng sample, kiểm tra feature/source đã khả dụng tại decision/as-of cutoff và lưu khoảng outcome/label được quan sát; purge sample train khi label/outcome interval của nó chồng lấn vùng label validation/test hoặc khi feature/source cần thiết vượt cutoff. Cửa sổ feature lịch sử có thể dùng chung nếu toàn bộ dữ liệu đã khả dụng trước decision; không purge máy móc chỉ vì hai lookback lịch sử cùng đọc một nến. `purge`/`embargo` theo thời lượng là hàng rào bổ sung, không thay thế các kiểm tra này.
- Thứ tự chi tiết, module boundaries và nguồn được ghi tại [WBS WP-06 learning](../superpowers/plans/2026-11-01-wp06-learning-wbs.md).

## WP-06A — Hợp đồng scorer và abstention

1. Định nghĩa `ScoreRequest` có candidate, feature snapshot IDs, `as_of`, strategy/version, context đã lọc và schema version.
2. Định nghĩa `ScoreResult` chặt gồm approve/veto/abstain, score/probability đã hiệu chuẩn nếu có, regime/confidence, evidence refs, uncertainty, model version và latency.
3. Cấu hình timeout, max input/token, schema validation, source allowlist, OOD/missing-feature gate và redaction.
4. Khi timeout, invalid output, stale data/OOD hoặc thiếu coverage thì trả abstain; fallback deterministic phải được cho phép rõ, có version.
5. LLM giải thích bằng evidence đã lưu, không cung cấp chain-of-thought nội bộ; không có tool/function để gửi lệnh.
6. Mọi output chỉ là feature/ranking cho Planner; RiskEngine tiếp tục quyền chặn cuối.

**Nghiệm thu:** mọi nhánh lỗi trả abstain; không có API key exchange/portfolio tool trong model context; output không hợp schema không thể tạo `OrderIntent`.

## WP-06B — DSL giới hạn và experiment specification

1. Tạo grammar allowlist cho toán tử, feature, timeframe, window và điều kiện kết hợp; không `eval`, import Python, filesystem, network hoặc vòng lặp tùy ý.
2. Giới hạn độ sâu AST, số node, memory, runtime và tập symbol; canonicalize biểu thức thành hash.
3. Mỗi hypothesis gồm giả thuyết tiếng Việt, tên ID tiếng Anh, mô hình chiến lược, universe, horizon, entry/exit recipe, baseline so sánh và mục tiêu.
4. Validate feature/source rights và thời điểm khả dụng trước khi compile/chạy.
5. Lưu DSL version, compiler version, hash và diagnostics tiếng Việt theo vị trí ký tự.

**Nghiệm thu:** DSL độc hại/ngoài allowlist bị chặn trước runtime; cùng biểu thức canonical cho hash ổn định; hypothesis hiển thị đủ assumptions để reviewer hiểu.

## WP-06C — Outcome labels và dataset manifest

**Foundation hiện có:** `application/learning_dataset.py::DatasetManifestBuilder` nhận sample chuẩn từ verifier port theo workspace/as-of; trước temporal gate so label/value/hash, toàn bộ timestamps, feature/outcome IDs/hash, `source_dependent` và split với candidate. Rights hashes/revision refs được lấy nguyên trạng từ sample authoritative; caller không thể ghi đè. Verifier attests feature/schema/split-policy versions và holdout seal; `split_policy_version` được pin vào canonical manifest. Sample research chỉ đủ điều kiện khi quyền `TRAINING` được xác minh. Thiếu, stale, revoked, timeout hoặc exception bị fail-closed. Mỗi verify chờ tối đa 5 giây; build tối đa 120 giây, tối đa 10.000 sample+exclusion và 32 verifier task chậm còn sống. Tracker dùng module-scope shared trong giả định một event loop mỗi process; test xác nhận budget dùng chung giữa nhiều builder. Timeout hủy task nhưng không chờ adapter chống cancellation; adapter vẫn phải tự giới hạn tài nguyên/transport. Chưa có verifier adapter kết nối database/source registry, split assigner được duyệt, phê duyệt, persistence hoặc trainer consumer.

1. Chỉ gán label sau khi trade/replay window đủ dữ liệu, fees/funding/fills hoàn tất; provisional label không vào tập train.
2. Replay đúng exit recipe đã gắn vào trade (partial, stop/target, trailing, time/opposing exit), không mặc định đổi sang triple-barrier label khác.
3. Ghi label source/version/reviewer/correction, input event IDs, feature time boundary và source-right eligibility.
4. Dataset manifest pin query/symbol/time range, as-of policy, feature/label version, rights, exclusions, balance class, hash và retention.
5. Tách dataset build khỏi trainer execution; trainer chỉ đọc dataset manifest được duyệt.

**Nghiệm thu:** label sửa tạo version mới; recipe mismatch làm row ineligible; mọi row train truy về dữ liệu có thời điểm khả dụng hợp lệ.

## WP-06D — Trainer và temporal evaluation

1. Bắt đầu bằng baseline CPU có giám sát, deterministic seed và time-series split; benchmark với rule đơn giản/naive baseline.
2. Dùng expanding/rolling walk-forward; với mỗi sample lưu feature/source availability và label/outcome interval. Purge sample train nếu label/outcome interval giao với validation/test hoặc nếu feature/source vượt cutoff; feature lookback lịch sử được dùng chung khi vẫn point-in-time hợp lệ. Embargo sau cửa sổ kiểm định là hàng rào bổ sung. Khóa holdout dùng một lần; không tái dùng holdout để sửa DSL, threshold hay model.
3. Điều chỉnh phí, funding, slippage, fill rejection và latency; tính net metrics, drawdown, expectancy, calibration và confidence interval.
4. Lưu toàn bộ trial/parameter search để tính selection bias; không tối ưu threshold trên test cuối.
5. Lưu manifest bất biến gồm query/range/as-of, sample IDs hoặc digest, feature availability/label intervals, exclusions, source rights, feature/label/schema/compiler versions, code/config/library/seed hashes, trial ledger, artifact checksum, evaluation report và sample counts. Có thể tái dựng chính xác tập sample đã cấp cho trainer.
6. Nêu rõ giới hạn CPCV/PBO/DSR và loại bằng chứng; không dùng một metric để kết luận chắc chắn có lợi nhuận.

**Nghiệm thu:** train có thể tái lập theo manifest trong tolerance; leakage fixture làm evaluation fail; label interval overlap bị purge dù event timestamps nằm ở hai fold khác nhau; future/revised-after-cutoff feature/source bị từ chối; historical feature lookback dùng chung chỉ được chấp nhận khi availability trước decision đã được xác minh; report có so sánh baseline, chi phí, coverage, toàn bộ trial budget và holdout integrity.

## WP-06E — Model registry, paper promotion và rollback

1. Model registry lưu artifact bất biến; state lifecycle `draft → evaluated → rejected/eligible → paper_active → retired`.
2. Promotion policy kiểm tra sample minimum, temporal folds, costs, drawdown/risk, calibration, OOD/coverage, rights và reviewer approvals.
3. Auto promotion nếu có chỉ tới PAPER và phải có policy version; LIVE promotion không bao giờ tự động.
4. Canary/shadow chạy bản challenger không có quyền order hoặc có quota paper riêng; compare quyết định cùng thời điểm.
5. Rollback giữ artifact/history, chuyển active pointer về bản trước, audit actor/reason và khóa entries nếu nguyên nhân là safety incident.

**Nghiệm thu:** candidate không đạt gate bị từ chối có reason; chỉ principal đủ quyền publish paper/live readiness; không tồn tại đường trainer gọi gateway hoặc set LIVE.

## WP-06F — Drift, feedback và retraining schedule

1. Theo dõi feature distribution, predicted score/calibration, realized outcome, source coverage, abstention và risk/performance theo version.
2. Drift event phân biệt `detected`, `inconclusive` và `degraded`; dữ liệu ít không bị xem như không drift.
3. Trigger có thể tạo experiment/trainer run, nhưng không sửa active strategy/model.
4. Thực hiện phân tích false positive, false negative, skipped signal, action latency và operator overrides; không chỉ trade win/loss.
5. Viết tài liệu tiếng Việt cho model card, metric glossary, promotion rationale và rollback rationale.

**Nghiệm thu:** cảnh báo drift truy được version/dữ liệu; retrain tạo challenger riêng; không tự thay bản đang active; mọi hành vi bot lưu được để reviewer đối chiếu.
