# Plan 06 — AI learning có kiểm soát

**Mục tiêu:** xây vòng tạo giả thuyết → gán nhãn → train/evaluate → paper promotion → theo dõi drift/rollback; AI không có quyền đặt lệnh hoặc tự promote live.
**Phụ thuộc:** [Plan 01](./01-foundation-database.md), ledger/recipe nhất quán từ [Plan 02](./02-trading-core-paper.md), context từ [Plan 05](./05-research-ingestion.md) nếu feature đó được dùng.
**Stories:** US-15, US-23, US-26…29.
**Tài liệu:** [AI use cases](../product/use-cases.md#4-chi-tiết-use-case), [training sequence](../product/sequence-diagrams.md#sd-10--trade-review-outcome-label-và-attribution), [glossary AI](../research/glossary-vi.md#e-dữ-liệu-và-chiến-lược-huấn-luyện-ai), [strategy research](../research/strategies-2026.md).

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

1. Chỉ gán label sau khi trade/replay window đủ dữ liệu, fees/funding/fills hoàn tất; provisional label không vào tập train.
2. Replay đúng exit recipe đã gắn vào trade (partial, stop/target, trailing, time/opposing exit), không mặc định đổi sang triple-barrier label khác.
3. Ghi label source/version/reviewer/correction, input event IDs, feature time boundary và source-right eligibility.
4. Dataset manifest pin query/symbol/time range, as-of policy, feature/label version, rights, exclusions, balance class, hash và retention.
5. Tách dataset build khỏi trainer execution; trainer chỉ đọc dataset manifest được duyệt.

**Nghiệm thu:** label sửa tạo version mới; recipe mismatch làm row ineligible; mọi row train truy về dữ liệu có thời điểm khả dụng hợp lệ.

## WP-06D — Trainer và temporal evaluation

1. Bắt đầu bằng baseline CPU có giám sát, deterministic seed và time-series split; benchmark với rule đơn giản/naive baseline.
2. Dùng expanding/rolling walk-forward; purging/embargo khi nhãn hoặc cửa sổ feature chồng lấn; khóa holdout dùng một lần.
3. Điều chỉnh phí, funding, slippage, fill rejection và latency; tính net metrics, drawdown, expectancy, calibration và confidence interval.
4. Lưu toàn bộ trial/parameter search để tính selection bias; không tối ưu threshold trên test cuối.
5. Lưu code/data/config/feature/label/library/seed hashes, model artifact checksum, evaluation report và sample counts.
6. Nêu rõ giới hạn CPCV/PBO/DSR và loại bằng chứng; không dùng một metric để kết luận chắc chắn có lợi nhuận.

**Nghiệm thu:** train có thể tái lập theo manifest trong tolerance; leakage fixture làm evaluation fail; report có so sánh baseline, chi phí, coverage và holdout integrity.

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
