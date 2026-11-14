# WP06 — Quy kết kết quả PAPER cho chẩn đoán hiệu năng

## Ý định

Tạo chẩn đoán PAPER có thể tái lập cho xác suất scorer: nối đúng `BotDecision` đã chấm với ý định PAPER và trade đóng, so Brier/log-loss với TEST baseline đã đóng băng, và chỉ ghi nhận suy giảm khi đủ coverage, số mẫu và class support. Đây là chẩn đoán xác suất trên giao dịch PAPER được chọn; không phải đánh giá lợi nhuận hay bằng chứng promotion.

## Phạm vi được duyệt

- Thêm `OrderIntent.bot_decision_id` nullable và FK ghép `(workspace_id, bot_id, bot_decision_id)` tới `bot_decision`. Mỗi decision có tối đa một intent. Paper runtime lấy ID từ `PaperBotDecisionCommand`, không nhận giá trị từ payload người dùng; truyền nguyên tử tới intent. Bao gồm ID trong đối chiếu idempotency.
- Dữ liệu cũ/luồng `PaperDecisionCommand` giữ `bot_decision_id=NULL`; không backfill JSON. JSON `evidence.bot_decision_id` được giữ làm audit tương thích nhưng không được dùng làm khóa nối. Trigger intent tiếp tục khóa trường attribution bất biến; runtime role vẫn chỉ UPDATE status/filled_quantity.
- Báo cáo chỉ ghép `learning_score_event` → `bot_decision` → `order_intent` → `trade` bằng workspace, bot và UUID có FK; xác minh decision hash/candidate ID, scorer artifact/policy/deployment revision, TEST report ID+hash và vector/feature-schema lineage. Từ chối trùng/mismatch và ghép hơn một trade. Một trade thực sự đóng với net PnL bằng 0 là lớp 0; không có trade, trade chưa đóng hoặc trade đóng sau cutoff không thành nhãn 0.
- Chỉ xét PAPER intent có scorer APPROVE/CANDIDATE và được risk chấp thuận. Các quyết định VETO/ABSTAIN, không vào lệnh, bị risk-gate, dữ liệu legacy không attribution và đối tượng không có đúng một trade đóng không đưa vào metric. Coverage denominator chỉ gồm các intent đủ điều kiện và đã trưởng thành; numerator là intent có đúng một trade đóng không muộn hơn 30 ngày sau thời điểm intent. Báo cáo giải thích rõ selection bias và không suy diễn counterfactual.
- Hai cohort decision không giao nhau, mỗi cohort 30 ngày và chỉ đánh giá sau khi cohort đó đủ 30 ngày trưởng thành: `[as_of−90d, as_of−60d)` và `[as_of−60d, as_of−30d)`. Baseline phải là TEST report persisted cùng target fingerprint. Không trộn bot, workspace, registry/revision, artifact, deployment, policy, report hoặc feature schema.
- Target fingerprint canonical là SHA-256 của tuple cố định: `workspace_id`, `bot_id`, `registry_id`, `registry_revision`, `scorer_artifact_id/hash`, `base_artifact_hash`, `policy_id/hash`, `deployment_event_id/revision`, `evaluation_report_id/hash`, `proof_hash`, `report_scope_hash`, `feature_schema_hash`, `source_rights_fingerprint`. Mỗi báo cáo thuộc đúng một fingerprint; lineage khác được đánh giá thành report khác, không gộp. Cả hai cohort phải có members của cùng fingerprint đó; nếu một cohort thiếu thì report target là `INCONCLUSIVE`.
- Mỗi cohort riêng phải có tối thiểu 1.000 kết quả được quy kết, tối thiểu 100 kết quả cho từng lớp `-1/0/+1`, và coverage trưởng thành ít nhất 80%. Thiếu hoặc không đồng nhất lineage, quyền hiện hành hay baseline thì trạng thái là `INCONCLUSIVE`.
- Chỉ đặt `DEGRADED` khi **cả** ΔBrier và Δlog-loss đều lớn hơn `0.01` ở **cả hai** cohort đủ điều kiện liên tiếp. Mọi trường hợp còn lại là `HEALTHY` hoặc `INCONCLUSIVE` theo dữ liệu/ngưỡng. Đây là lựa chọn conservative để tránh một metric đơn lẻ hoặc một cửa sổ nhiễu gây incident.
- Báo cáo bất biến lưu policy version/hash, target fingerprint, baseline report ID/hash và metric, ranh giới/maturity của hai cohort, counts/class support/coverage, observed metrics, membership digest, status/reasons và thời điểm DB. Admin chỉ đọc summary aggregate theo workspace qua repository/gateway hẹp; không cấp quyền đọc rộng `trade`, `order_intent` hoặc xác suất score-event.
- Giao diện admin tiếng Việt responsive hiển thị baseline và hai cohort, công thức/cửa sổ/maturity, coverage, class support, ΔBrier/Δlog-loss, status và nhãn rõ “PAPER diagnostic — không chứng minh lợi nhuận”. `DEGRADED` chỉ tạo cảnh báo/chẩn đoán; không tự HOLD, đổi trạng thái bot, đề xuất promote hay chặn entry. Cơ chế incident hold MFA hiện có giữ độc lập.

## Không thuộc phạm vi

- Không tạo nhãn cho no-entry/veto/abstain/risk-denied; không đo profit expectancy, Sharpe hay tuyên bố chiến lược có lợi nhuận.
- Không tự động dừng bot/entry, tạo hold, rollback, retrain, registry proposal hoặc thay scorer active pointer.
- Không thay các policy calibration/activation đã được ghim cho C3/C4; TEST baseline chỉ được tham chiếu, không sửa.
- Không mở `SHADOW`/`PAPER_GATE` hoặc gọi Binance.

## Các cách đã cân nhắc và quyết định

1. Quy kết bằng `candidate_id` hoặc gộp trade theo PnL: bị loại vì không chứng minh một quyết định scorer cụ thể đã tạo đúng intent; gộp có thể che double-fill/duplicate.
2. Giữ `INCONCLUSIVE` tới khi đủ report production: an toàn nhưng chưa cung cấp chẩn đoán hữu ích cho PAPER đang chạy.
3. **Được chọn:** nullable typed FK và exact scorer-event/decision/intent/trade join; report aggregate-only PAPER, so với frozen TEST baseline, chỉ diagnostic và fail-closed khi thiếu bằng chứng.

## Ràng buộc và tiêu chí chấp nhận

- Dùng số học xác định và cùng định nghĩa multiclass cho `-1/0/+1`; xác suất phải hữu hạn, nằm trong miền và tổng bằng 1; clipping log-loss phải được định nghĩa trong policy hash.
- Kết quả phải idempotent theo target, policy, as-of/window và membership digest; quyền bị thu hồi hoặc target đổi trong lúc đọc sẽ không persist report eligible.
- PostgreSQL security boundary aggregate-only, RLS FORCE, append-only và downgrade guard populated. Không nới grants trực tiếp cho admin.
- Test TDD cho exact FK/idempotency/legacy NULL, mismatch/no-trade/open/late/multiple/zero-PnL, boundaries, class support/coverage, one-vs-both-metric regression, consecutive windows, deployment mixing, baseline mismatch, rights race, workspace isolation, no side effects và replay/idempotency. Compose E2E theo luồng PaperBotDecision thật qua đóng trade và aggregate report. Nếu đổi UI, Playwright desktop 1280×800 + mobile 390×844, kiểm tiếng Việt, không tràn ngang, screenshot mới.

## Nguồn kiểm tra

- Gneiting & Raftery, *Strictly Proper Scoring Rules, Prediction, and Estimation*, kiểm tra 2026-11-13: https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf — proper scoring rules đánh giá dự báo xác suất; không tự chứng minh lợi nhuận.
- Brier (1950), *Verification of Forecasts Expressed in Terms of Probability*, kiểm tra 2026-11-13: https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml.
- scikit-learn calibration guide, kiểm tra 2026-11-13: https://scikit-learn.org/stable/modules/calibration — Brier và log-loss là proper scoring rules; Brier đồng thời phản ánh reliability, resolution và uncertainty, nên không được diễn giải như calibration-only hoặc trading-return metric.
