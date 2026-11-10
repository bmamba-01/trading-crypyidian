# WP06 Task7 — thiết kế scorer PAPER đã hiệu chuẩn

**Ngày:** 2026-11-08

**Trạng thái:** thiết kế; chưa triển khai. Artifact huấn luyện hiện hành vẫn phải `ABSTAIN`.

**Phạm vi:** phần scorer/runtime của Task7, bổ sung cho [kế hoạch observer và drift Task7/8](../plans/2026-11-08-wp06-task7-shadow-drift-plan.md). Kế hoạch observer chỉ đo metadata và phát `ABSTAIN`; nó không thay thế calibration, evaluation hay scorer runtime được thiết kế ở đây.

**Liên quan:** [thiết kế hoàn tất WP06](./2026-11-06-wp06-completion-design.md), [implementation plan WP06](../plans/2026-11-06-wp06-completion.md), [WBS WP06](../plans/2026-11-01-wp06-learning-wbs.md), [AI learning module](../../implementation/06-ai-learning.md), [glossary](../../research/glossary-vi.md).

## Mục tiêu

Cho phép một artifact đã được đánh giá ngoài mẫu và duyệt rõ ràng chấm **candidate do strategy hiện tại tạo ra** trên đúng feature vector point-in-time. Scorer có thể chạy `SHADOW` để ghi nhận dự đoán mà không thay đổi hành vi. `PAPER_GATE` bị khóa khi `candidate_completeness_verified=false`; chỉ được xét sau khi có bằng chứng candidate-universe đầy đủ, đại diện cho đúng phạm vi evaluation, cùng report scorer được chấp nhận cho PAPER gate và approval MFA/CAS riêng. Candidate proof hiện có chỉ bao phủ trade đã đóng của BASE trial nên chưa mở khóa gate.

Score là xác suất lớp kết quả net-PnL đã định nghĩa trong manifest, có điều kiện trên candidate strategy đã chọn; nó không phải kỳ vọng số tiền lãi, tín hiệu tự sinh, quyền đặt lệnh hay bằng chứng hiệu quả tương lai. Strategy chỉ sinh candidate; scorer không tạo candidate; RiskEngine và các runtime gate hiện có vẫn quyết định phần còn lại. Không có trạng thái hay đường LIVE.

## Hiện trạng được xác nhận

- `train_authorized_batch` fit ba logistic one-versus-rest từ train và chỉ lập report diagnostic trên validation. Artifact schema hiện hành khóa `calibrated=false`, `scorer_state="abstain"`; trigger DB cũng yêu cầu các giá trị này. Không được sửa artifact đã lưu hoặc bỏ ràng buộc để biến artifact cũ thành scorer.
- `ScoreRequest` nhận `FeatureSnapshot` một timeframe; `ScoringPolicy.required_features` hiện chỉ chấp nhận tên feature không có `timeframe:name`. Runtime dispatcher dùng `DecisionSnapshot`, có feature key theo timeframe nhưng không có composite schema hash, vector hash, regime/coverage tổng hợp theo hợp đồng scorer.
- `LearningScoringService` chưa được gọi từ `BotEvidenceDispatcher`; registry active pointer hiện là metadata quản trị, chưa được runtime đọc để chấm điểm.
- Replay outcome lưu entry features theo `timeframe:name`, nhưng manifest schema hash được caller cung cấp; hiện chưa có hàm schema chung chứng minh feature order, công thức, tham số và hash giống giữa replay với runtime.
- `LearningEvaluationReport` hiện là báo cáo diagnostic cho replay strategy và `candidate_completeness_verified=false`; nó không đánh giá xác suất của artifact. Một report đó không đủ căn cứ activate scorer.

Các đường dẫn cụ thể gồm `application/learning_cpu_trainer.py`, `application/learning_training_artifact.py`, `domain/learning/scoring.py`, `application/learning_scoring.py`, `application/bot_dispatcher.py`, `application/market_signal_composition.py`, `application/runtime_replay_evaluator.py`, `application/learning_fold_samples.py` và `domain/learning/model_registry.py`.

## Mục tiêu và giới hạn

### Mục tiêu

1. Chỉ hiệu chuẩn bằng train/validation được cấp theo fold; test/holdout được đọc một lần qua gateway có receipt dành riêng cho scorer evaluation.
2. Có một hợp đồng vector duy nhất cho feature schema, thứ tự, availability, chất lượng và hash được dùng khi tạo sample replay lẫn chấm runtime.
3. Sinh artifact scoring version mới, bất biến, ghim artifact train gốc, calibration, feature schema và chính sách scoring; artifact train v1 tiếp tục diagnostic-only.
4. Tách rõ `DISABLED`, `SHADOW` và `PAPER_GATE`. Mặc định không chạy scorer hay chỉ `ABSTAIN`; mọi lỗi, thiếu quyền, pointer/schema mismatch, OOD hoặc stale input đều fail-closed.
5. Ghi report ngoài mẫu metadata-only và score event append-only cùng lineage quyết định, model, policy, pointer/deployment revision, snapshot và rights fingerprint hiện hành.
6. Tạo deployment governance event bất biến riêng cho scorer; mỗi lần bật/đổi mode ghim chính xác artifact v2, scorer report, policy hash, registry/pointer revision, actor, MFA evidence và CAS expectation. Registry diagnostic Task6 không phải bằng chứng deployment scorer.
7. Yêu cầu người có quyền và MFA bật PAPER gate qua CAS; rollback tăng pointer version, giữ lịch sử score, và làm deployment cũ hết hiệu lực cho tới khi được duyệt lại.
7. Chứng minh bằng PostgreSQL Compose E2E rằng shadow không làm thay đổi strategy decision, plan, risk reservation, PAPER intent, order hoặc trade.

### Không thuộc phạm vi

- Không tạo signal/candidate khi strategy không tạo candidate; không thay thế strategy hiện hữu.
- Không học online, tự thay threshold, tự approve, tự promote, tự rollback hoặc tự enqueue trainer.
- Không gán lợi nhuận kỳ vọng cho xác suất dấu PnL; không dùng score để vượt RiskEngine, workspace/account caps, incident, kill/pause hoặc readiness gate.
- Không xóa, update hoặc diễn giải lại artifact v1, manifest, report hay score event đã persist.
- Không mở Binance private API, exchange credential, LIVE order hoặc execution capability cho trainer/evaluator/scorer.
- Không coi tập outcome trade đã đóng là đầy đủ mọi quyết định. Đánh giá chỉ nói về candidate đủ provenance trong manifest; không chứng minh recall trên no-entry hoặc mọi cơ hội thị trường.

## Các hướng đã cân nhắc

| Hướng | Quyết định | Lý do |
| --- | --- | --- |
| Đánh dấu artifact v1 `calibrated=true` rồi gọi scorer hiện có | Loại | Artifact v1 không lưu calibrator; trainer/evaluator chưa cho thấy xác suất ngoài mẫu. Việc đổi cờ sẽ làm sai lịch sử và vô hiệu hóa fail-closed contract. |
| Gửi feature/vector tới dịch vụ ML bên ngoài | Loại | Thêm network, credential, latency và data-disclosure boundary mới; không cần cho baseline CPU hiện tại. |
| Artifact calibration v2 + vector contract chung + holdout evaluation một lần + shadow mặc định + PAPER gate có MFA/CAS | Chọn | Giữ lại artifact diagnostic, sử dụng pipeline/receipt hiện có, cho phép kiểm parity và không để scorer tác động execution cho tới khi được bật riêng. |

Phương pháp calibration được đề xuất cho v2 là **temperature scaling** trên ba logit one-versus-rest, chuyển thành phân phối lớp bằng softmax `softmax(z / T)`. Nhiệt độ dương `T` được fit chỉ trên validation bằng cách tối thiểu hóa multiclass negative log-likelihood trong miền `[T_min, T_max]` do policy version hóa quy định. Nghiên cứu gốc đề xuất temperature scaling cho logits đa lớp; áp dụng từ neural-network logits sang logistic one-versus-rest của Crypidian là một giả thuyết cần được kiểm chứng bằng validation và test riêng, không phải đảm bảo hiệu quả. Nếu optimizer không hội tụ, nhiệt độ nằm ngoài biên policy, validation thiếu support hoặc tiêu chí calibration không đạt, không phát artifact có thể activate và scorer giữ `ABSTAIN`. Tham khảo [Guo et al., ICML 2017](https://proceedings.mlr.press/v70/guo17a.html).

## Hợp đồng feature vector chung

Thêm domain contract `LearningFeatureVector` (tên module dự kiến `domain/learning/features.py`) và builder duy nhất dùng bởi cả replay và runtime. Không đổi ngầm tên key hay ép `DecisionSnapshot` thành `FeatureSnapshot` giả.

Vector canonical gồm tối thiểu:

- `workspace_id`, `symbol`, `decision_at`, composite `DecisionSnapshot.snapshot_id` và `snapshot_hash`;
- `schema_version`, `feature_schema_hash`, danh sách feature canonical có key `timeframe:name`, numeric value hữu hạn, quality và `available_at`;
- các `feature_snapshot_id` theo timeframe, event/bar provenance digest, `coverage`, `freshness`, `warmup`/quality summary và regime theo quy tắc version hóa;
- source-rights eligibility/fingerprint được tính lại bằng current-rights gate; quyền không được caller tự khai;
- `strategy_release_id/hash` được lưu riêng với schema hash, để model lineage không bị ghép nhầm với tên strategy.

Quy tắc:

1. Khóa feature duy nhất, sắp xếp canonical, đúng schema đã ghim trong manifest/artifact; không nhận vector hoặc schema hash do request/API tự khai.
2. `feature_schema_hash` được tính từ canonical schema/version, ordered keys, timeframe, đơn vị/kiểu, công thức, tham số và phiên bản builder. `snapshot_hash` bao gồm workspace/symbol/time, schema hash, values, quality/availability và provenance digest; không bao gồm wall-clock không có nghĩa nghiệp vụ.
3. Missing, duplicate, non-finite, stale/future, warmup chưa đủ, quality không READY, insufficient coverage, schema mismatch hoặc thiếu provenance tạo reason `ABSTAIN`; không tự điền 0, đổi timeframe hoặc bỏ feature để khớp artifact.
4. Runtime builder nhận đúng instance `DecisionSnapshot` do `PostgresMarketDecisionSnapshotProvider` tạo và dispatcher đã dùng để đánh giá candidate. Provider phải giữ trong snapshot envelope lineage chính xác cho từng timeframe: `feature_snapshot_id`, source event/bar IDs và digest, as-of/availability, quality/coverage. Nếu các metadata này chưa có trên `DecisionSnapshot`, implementation phải bổ sung typed immutable lineage vào snapshot/provider; không được suy ra bằng cách cắt chuỗi `feature_id` hoặc truy vấn lại.
5. Vector được tạo đồng bộ từ chính `DecisionSnapshot` đó. Composite `snapshot_id`, per-timeframe IDs, `decision_time` và giá trị `FeatureObservation` phải khớp tuyệt đối; không dựng vector mới bằng query theo symbol/time gần đúng, “latest”, hoặc timestamp gần nhất. Không trộn `BotDecision.feature_snapshot_id` (evidence snapshot) với composite `DecisionSnapshot.snapshot_id`; score event lưu hai loại lineage ở các trường riêng.
6. Replay entry provenance và runtime builder gọi cùng serializer/schema fingerprint trên cùng loại snapshot envelope. Mỗi tham chiếu timeframe được khóa theo khóa composite có symbol, timeframe và exact provider snapshot/source IDs, không chỉ timestamp. Thiếu ID, duplicate/mismatch, hoặc provider cần nearest-time lookup thì tạo `ABSTAIN`.
7. Test parity cấp domain và Compose dùng cùng chuỗi bar/event phải xác nhận exact decision snapshot ID, per-timeframe snapshot IDs, source event/bar IDs, key list, schema hash, vector hash và từng value; sai một đầu vào hoặc ghép nhầm timeframe phải từ chối parity.
8. `ScoringPolicy` v2 và `ScoreRequest` v2 nhận typed vector này. Không giữ validator cũ cho `FeatureSnapshot` như lối tắt để gọi model bằng key thiếu timeframe.

## Calibration và artifact scoring v2

### Ranh giới dữ liệu

- Fit scaler và logistic coefficients chỉ từ `TRAIN` như hiện tại.
- Fit temperature và đánh giá điều kiện calibration chỉ từ `VALIDATION` thuộc đúng fold/approval/manifest. Validation là dữ liệu đã dùng để calibration, không được báo như test cuối.
- Dự đoán ngoài mẫu cuối cùng chỉ trên `TEST` của đúng fold qua evaluator gateway một lần. Gateway kiểm workspace, membership, outcome/hash, fold role, current rights và source lock; ghi receipt trước khi disclose bất kỳ payload test nào. Scorer không có quyền đọc normalized sample/assignment hay holdout trực tiếp.
- Test payload chỉ nằm trong evaluator process trong lúc tính score/report; report trả ra gồm metadata aggregate, hash và support, không gồm sample ID/value/label/trade row. Không expose bytes test cho trainer, admin UI, API response hoặc scorer runtime.
- Threshold/policy/method/schema phải được cố định và hash trước receipt test. Không chọn method, threshold, feature, cutoff hay policy từ test result; muốn thử lại cần manifest/holdout mới chưa bị tiêu thụ.

### Scoring artifact

Tạo một bảng/artifact schema mới, ví dụ `LearningScoringArtifactRecord`, liên kết bất biến đến:

- `base_training_artifact_id/hash` (artifact v1 đã xác minh), attempt, workspace, manifest/fold/approval hash và feature schema v2;
- `calibrator_method/version`, `temperature`, lớp theo thứ tự `(-1, 0, 1)`, số mẫu validation và per-class support, membership digest, validation summary hash;
- `scoring_policy_version/hash`, trainer/calibrator code/dependency fingerprint, artifact canonical JSON/hash;
- trạng thái `ready_for_evaluation` hoặc reason fail-closed. Artifact không có nhãn/vector/sample/holdout payload.

Không ghi đè cột `calibrated` hoặc `scorer_state` trong `LearningTrainingArtifactRecord`. Artifact v1 vẫn `calibrated=false`; chỉ artifact scoring v2 có report ngoài mẫu phù hợp và human approval mới có thể gắn với deployment.

Artifact v2 trả phân phối ba lớp đã calibration. `probability` trong `ScoreResult` được định nghĩa là xác suất class `+1` — outcome có net PnL dương sau chi phí — có điều kiện trên candidate strategy hiện tại. `confidence` là xác suất lớp lớn nhất; `uncertainty = 1 - confidence`. Không diễn giải `probability` là expected return hoặc bảo đảm trade lời. Threshold hành động là policy đã duyệt trước test; policy thiếu/không khớp artifact nghĩa là `ABSTAIN`.

### Evaluation report ngoài mẫu

Thêm report kind riêng cho scorer (không dùng report diagnostic strategy cũ làm chứng cứ model). Report hash pin scoring artifact, temperature/calibrator version, policy hash, feature schema, manifest/fold/test membership digest, evaluator version, rights receipt và ngày database clock.

Tối thiểu report aggregate:

- số test sample và support từng lớp; coverage/abstain, score availability và các exclusion counts;
- multiclass log loss, Brier score, reliability bins/calibration error có ghi cách tính và cỡ mỗi bin; so sánh với baseline xác suất train prior và identity/no-calibration;
- confusion matrix/false-positive/false-negative theo lớp;
- số candidate được giữ/chặn theo policy đã pre-register và aggregate net PnL/fees/funding/slippage/max drawdown của tập test đã đóng, đối chiếu baseline không lọc; không chỉ nêu win-rate;
- caveat `candidate_completeness_verified=false`: đây là outcome replay đã đóng đủ provenance, không phải toàn bộ candidate universe/no-entry.

Gate evaluation chỉ chấp nhận report nếu policy-bound minimum sample/per-class support, max calibration error, Brier/log-loss guard, minimum coverage và candidate-retention/false-veto constraints đạt. Report scorer có trường trạng thái rõ ràng: `ACCEPTED_SHADOW_ONLY` cho phép quan sát SHADOW khi metric calibration đạt nhưng candidate universe chưa được xác minh đầy đủ; `ACCEPTED_PAPER_GATE_ELIGIBLE` chỉ được ghi khi cùng phạm vi test có candidate-universe proof đầy đủ, đại diện và được ghim bằng ID/hash. `INCONCLUSIVE` hoặc thiếu bằng chứng không cho phép PAPER_GATE. Candidate proof hiện có chứng minh các closed trade outcome của một BASE trial; nó không chứng minh toàn bộ strategy decision opportunities/candidates, no-entry hay candidate không tạo trade, nên không thể tự nâng report lên `ACCEPTED_PAPER_GATE_ELIGIBLE`.

Candidate-universe proof cho PAPER phải dùng định nghĩa/phạm vi đã version hóa trước khi mở test. Proof phải liệt kê đủ các decision opportunity trong phạm vi replay, trạng thái candidate/no-candidate và mọi candidate do strategy phát ra bằng ID ổn định; đối soát tổng số opportunity/candidate với các bản ghi được đưa vào, loại trừ có reason code, và link outcome theo hợp đồng one-to-zero/one-to-many đã khai báo. Opportunity `no-candidate` chỉ dùng để xác nhận coverage, không được gán outcome giả; xác suất/retention/false-veto chỉ tính trên candidate universe đã chứng minh. Proof có source window, strategy release/hash, timeframe, manifest/fold/test membership, row counts, gap/exclusion summary và canonical hash. Bất kỳ gap, count mismatch, outcome/candidate không truy vết được hoặc phạm vi bị chọn sau khi xem test đều đặt cờ completeness về false. Chỉ closed-trade proof hoặc chỉ kiểm tra các row đã persist không đạt tiêu chí này.

Các metric candidate retention/false-veto được tính trên toàn bộ unit candidate đã định nghĩa trong proof, không chỉ trade đã đóng. Report vẫn phải nêu riêng giới hạn về no-entry và mọi phần universe không nằm trong declared scope; không được tuyên bố đại diện cho phạm vi rộng hơn proof.

## Runtime scoring, deployment và rollback

### Chế độ

| Mode | Hành vi |
| --- | --- |
| `DISABLED` | Không gọi scorer; ghi metadata disabled nếu cần. Đây là mode mặc định khi chưa có deployment hợp lệ. |
| `SHADOW` | Chấm candidate bằng pointer/artifact/policy đã pin, persist `ScoreResult` metadata và lý do. Kết quả không đổi `BotDecision`, candidate, planner, risk, reservation, PAPER intent, order hoặc trade. Lỗi tạo `ABSTAIN`. |
| `PAPER_GATE` | Mặc định không đủ điều kiện khi `candidate_completeness_verified=false`. Chỉ bật nếu deployment event ghim report `ACCEPTED_PAPER_GATE_ELIGIBLE`, candidate-universe proof đúng phạm vi, rights hiện hành, policy/artifact đã review và CAS/MFA riêng đạt. Sau đó candidate chỉ đi tiếp nếu `ScoreDecision.APPROVE`; `VETO` hoặc `ABSTAIN` trở thành quyết định chặn entry có reason lineage. Candidate đã được strategy tạo; scorer không tự tạo candidate. Candidate APPROVE vẫn phải qua runtime gates, planner và RiskEngine bình thường. |

Không có mode LIVE. `PAPER_GATE` không thể đặt lệnh; nó chỉ quyết định candidate có được đưa tới pipeline PAPER đã tồn tại hay không. Không có policy, stale pointer, calibration/evaluation/report hash mismatch, schema mismatch, rights bị thu hồi, model timeout/failure, OOD hoặc dữ liệu thiếu đều fail-closed: persist `ABSTAIN`; ở `SHADOW` không ảnh hưởng hành vi, còn ở `PAPER_GATE` chặn candidate đó.

Ánh xạ status lưu trong decision ledger: khi shadow, giữ `BotDecision` của strategy nguyên trạng và ghi score riêng; khi gate, scorer `VETO` đổi candidate thành `BotDecisionStatus.VETO` với reason code ổn định `LEARNING_MODEL_VETO`; scorer `ABSTAIN` đổi thành `BotDecisionStatus.ABSTAIN` với reason code `LEARNING_SCORER_ABSTAIN`; `APPROVE` giữ `CANDIDATE` và tiếp tục pipeline hiện có. Score event ghim candidate ID/hash trước khi status được đổi, để giải thích được candidate nào bị chặn.

### Score event và consistency

Thêm append-only `learning_score_event` (hoặc tên tương đương) với FK workspace-scoped tới `bot_decision`, registry/scoring artifact, scoring evaluation report, scorer deployment event, active pointer/deployment revision, feature schema/vector hash và policy. Row ghim `incumbent_decision_id/hash/status` cùng `candidate_id/hash` của strategy hiện tại để chứng minh hai phía dùng cùng snapshot; lưu riêng composite `decision_snapshot_id` và map timeframe → exact `feature_snapshot_id`/source-event digest. Candidate digest phải được tính server-side trước khi gate có thể đổi status. Ghi score decision/reason code, probability/confidence/uncertainty nếu hợp lệ, model latency và rights fingerprint; không lưu vector, raw source, label hoặc holdout.

Score event và BotDecision phải commit nguyên tử cùng transaction của decision job. Khóa/đọc active pointer và scoring deployment trong cùng workspace transaction; event ghi chính xác pointer version đọc được. Retry cùng decision + pointer + artifact + vector trả record cũ; cùng idempotency key nhưng payload/hash khác báo conflict. Nếu pointer đổi giữa resolve và commit thì retry/fail-closed; không lưu score gắn hai revision.

### Approval/CAS

- Model chỉ vào registry theo flow hiện hữu; `EVALUATED` không có nghĩa đã chạy scorer.
- Task6 `learning_model_registry_event`/`learning_model_active_pointer` giữ registry lineage và report diagnostic; các record đó không đủ quyền hoặc bằng chứng để activate scorer. Thêm `learning_scoring_deployment_event` append-only (và current deployment projection nếu cần) thuộc scorer, không sửa lịch sử Task6. Mỗi event lưu workspace, deployment ID/revision, mode, `registry_id/revision`, `active_pointer_id/version`, scoring artifact v2 ID/hash, scorer-specific report ID/hash/state, policy ID/version/hash, candidate proof ID/hash/completeness, actor membership, MFA verified-at/method, expected pointer/deployment revisions, reason, idempotency key, predecessor event/hash và created-at.
- Mọi lần bật hoặc đổi mode tạo deployment event mới; client không gửi actor/workspace. Server revalidate trong cùng transaction: actor membership/role còn active, MFA theo freshness policy, report/artifact/policy/proof hash chính xác, rights eligibility và expected registry/pointer/deployment revisions. CAS stale thì từ chối, không ghi mode mới. Event/pointer revision được pin vào mọi score event.
- `SHADOW` và `PAPER_GATE` là hai quyết định governance khác nhau. SHADOW yêu cầu deployment event và scorer-specific report `ACCEPTED_SHADOW_ONLY` hoặc `ACCEPTED_PAPER_GATE_ELIGIBLE`; report `INCONCLUSIVE` không được chấm điểm. Bật `PAPER_GATE` cần role model approval + recent MFA, report chính xác `ACCEPTED_PAPER_GATE_ELIGIBLE`, `candidate_completeness_verified=true`, proof khớp phạm vi/report, policy hash đã review và expected registry/pointer/deployment revision chính xác. Giá trị `false`, `null`, thiếu proof hoặc proof closed-trade-only luôn từ chối gate, dù mọi calibration metric còn lại đạt.
- Runtime chỉ dùng deployment nếu bound `active_pointer_version`, registry revision, scoring artifact/report/policy hashes trùng dữ liệu hiện hành. Sai/mismatch nghĩa là `DISABLED`/`ABSTAIN`, không suy đoán mode.
- Rollback dùng CAS qua registry repository hiện có, tăng active pointer version và append audit event. Deployment cũ không còn khớp revision nên scorer gate bị vô hiệu tới khi reviewer tạo deployment mới; score event/report cũ giữ nguyên.
- Incident `block new entries` là mutation riêng qua control plane hiện hữu có actor/reason/MFA. Scorer chỉ phát evidence/alert; không tự pause bot, cancel order hay đóng vị thế.

## Database, quyền và migration

Mọi bảng mới có workspace FK, composite FK chống ghép tenant, RLS + FORCE RLS, policy fail-closed, immutable trigger, unique idempotency/content hashes, index theo workspace/time/fold và populated downgrade guard. Không cấp quyền UPDATE/DELETE cho event/artifact/report/receipt.

Role tối thiểu:

- **trainer:** SELECT đúng batch TRAIN/VALIDATION qua gateway; INSERT artifact v1 và scoring artifact v2 theo attempt; không đọc TEST trực tiếp, sửa registry/pointer/policy, risk/order.
- **scorer runtime:** SELECT đúng cột metadata/artifact cần chấm và deployment/pointer hiện hành; INSERT score event; không đọc normalized payload/label/holdout, không ghi registry/pointer/training job/risk/order. Nếu role được kế thừa bởi decision worker, inherited grants phải được kiểm đúng.
- **evaluator:** chỉ tiêu thụ test qua one-shot receipt/gateway và ghi scorer report/receipt metadata; không ghi artifact, registry, score event, pointer hoặc order.
- **admin-web:** SELECT metadata report/artifact/score/deployment; mutations chỉ qua API Principal + CSRF + MFA + expected version/idempotency, không SELECT vector/label/test payload.
- **candidate-builder/app/trainer/evaluator:** không được kế thừa scorer deployment mutation hoặc active pointer write.

Migration đặt grants/revokes ngay trong cùng transaction tạo đối tượng; role tests kiểm column privileges riêng, table privileges, membership inheritance, `has_column_privilege`, RLS/FORCE, cross-workspace, direct INSERT/UPDATE/DELETE và downgrade populated. Có thể thêm bảng `learning_scoring_artifact`, `learning_scoring_evaluation_report`, `learning_scoring_deployment_event`, current deployment projection và `learning_score_event`; không sửa nội dung artifact/report v1 hoặc biến report diagnostic Task6 thành report scorer. Nếu cần liên kết registry, migration giữ tương thích record diagnostic cũ; trigger/API chỉ cho `PAPER_GATE` khi event mới thỏa các điều kiện scorer-specific ở trên.

## UI/CLI và quan sát

- Bổ sung phần scorer vào admin learning hiện tại, tiếng Việt, Be Vietnam Pro và responsive desktop/mobile. Hiển thị mode, pointer/revision, artifact/report/policy hash viết gọn, thời điểm, health/coverage/abstain/calibration metrics và caveat về closed-trade sample.
- Với mode `SHADOW`, gắn nhãn rõ “chỉ quan sát — không tác động lệnh”. Cho phép xem score event metadata/paging, không có feature vector, label/test row.
- Chỉ role reviewer/owner gần MFA thấy form chuyển sang `PAPER_GATE`; form yêu cầu expected pointer version, policy/report summary, reason và idempotency. CSRF/no-store; backend xác nhận lại toàn bộ hashes/rights/state.
- Rollback confirmation nói rõ scorer quay về version trước và mode cũ hết hiệu lực. Không có nút auto-train/promote/flip-LIVE.
- CLI/operator output chỉ IDs/hash/status/reason codes đã lọc; không dump artifact coefficient payload hay source features.

## Phân rã kiểm thử trước triển khai

### 7C.1 — vector contract và parity

- RED: unit cho canonical keys/hash, duplicate/missing/non-finite/future/stale/warmup/coverage, schema mismatch; replay/runtime cùng bars và exact snapshot lineage phải ra đúng một vector; nearest-time lookup, thiếu per-timeframe ID, đổi timeframe hoặc feature-order phải từ chối parity.
- GREEN: thêm `LearningFeatureVector` và builder dùng chung trong replay entry capture và `PostgresMarketDecisionSnapshotProvider`; provider giữ typed lineage trên `DecisionSnapshot` hoặc envelope bất biến đi cùng chính instance đó, scorer contract v2 nhận typed vector.
- Compose: seed cùng bars, chạy provider replay/runtime, so composite snapshot ID, từng timeframe snapshot ID, source event/bar IDs, vector/schema/content hash; sửa một event/bar/feature làm mismatch và `ABSTAIN`.

### 7C.2 — calibration artifact v2

- RED: trainer chỉ nhận batch typed authorized; v2 chỉ dùng train để fit logistic, validation để fit temperature; thay label test không được đổi coefficients, temperature, artifact hash hay validation report. Thiếu class/support, NaN/infinity, temperature biên, optimizer failure và policy absent giữ artifact `ABSTAIN`/ineligible.
- GREEN: tạo scoring artifact mới gắn exact v1 base hash, feature schema, fold/approval, validation membership digest, calibration method/params/policy hash; verify canonical JSON/checksum.
- Unit/DB: v1 canonical bytes/hash bất biến; v2 update/delete denied; retry idempotent; same key/different payload conflict.

### 7C.3 — sealed test evaluation và registry approval

- RED: evaluator chỉ đọc role TEST cho exact artifact/fold/manifest, source-rights gate chạy trong shared lock; receipt commit trước disclosure; retry, rollback, crash và concurrent consume đúng one-shot. Test payload không rò ra report/API/UI.
- GREEN: scorer evaluator dùng v2 transform, persist aggregate OOS report và candidate-filter counterfactual; report hash revalidate trước registry attach/approval.
- Compose: no direct TEST select qua trainer/scorer/admin; cross-workspace denied; rights revoke/current policy mismatch fail-closed; report diagnostic cũ không thể activate scorer.

### 7C.4 — runtime shadow và PAPER gate

- RED: pointer/registry/deployment/policy/artifact/report/proof mismatch, completeness false, stale/future vector, rights denial, timeout/unavailable/OOD và duplicate conflict đều fail-closed; completeness false vẫn cho SHADOW nhưng API từ chối PAPER_GATE ngay cả khi các metric pass; shadow score không được chạm `PaperRuntimeApplicationService` side effects.
- GREEN: resolver/app adapter chấm vector từ chính `DecisionSnapshot` candidate, persist score event và BotDecision atomic; shadow bỏ qua mọi score effect. Thêm API Governance `set_scoring_mode(expected_pointer_version, expected_registry_revision, expected_deployment_revision, mode, actor_from_Principal, MFA, reason, idempotency)` ghi event scorer append-only; `PAPER_GATE` chỉ propagate candidate khi `APPROVE` và report/proof eligibility vẫn hiện hành.
- Compose: một candidate thật qua shadow, so trước/sau không đổi decision/plan/reservation/intent/order/trade; completeness=false cho phép SHADOW nhưng gate bị từ chối. Sau khi seed proof đại diện và report scorer ACCEPTED đúng scope, bật gate qua HTTP+MFA+CSRF/CAS; kiểm stale CAS và diagnostic Task6 report bị từ chối, VETO/ABSTAIN chặn, APPROVE vẫn vào risk gate; rollback CAS làm deployment cũ không hợp lệ, event cũ vẫn còn.

### 7C.5 — admin/mobile và independent review

- RED: Playwright desktop 1280px/mobile 390px cho localization, Be Vietnam Pro, overflow, keyboard/focus, CSRF/MFA, no payload/no-store, mode labels, no mutation cho ordinary viewer.
- GREEN: read model/report/score listing và approval controls trong trang learning responsive; screenshot ở `tests/e2e/evidence/`.
- Review độc lập soi leakage, source rights, `ABSTAIN` semantics, pointer races, zero execution capability và report selection bias; xử lý mọi finding trước Task9.

## Tiêu chí nghiệm thu

Task7 scorer chỉ có thể đóng khi tất cả điều kiện sau cùng đạt:

1. Replay/runtime feature vector exact-parity được kiểm trên bars/event thật của PostgreSQL; vector runtime được dựng từ đúng `DecisionSnapshot` đã đánh giá candidate, ghim composite snapshot ID và exact timeframe/source IDs, không có truy vấn gần đúng hay mapping ngầm.
2. Artifact v1 vẫn bất biến, uncalibrated/abstain; artifact v2 có temperature fit chỉ từ validation, exact data/rights/membership lineage, canonical hash và unit/DB immutable checks.
3. Test/holdout evaluation có receipt one-shot, report scorer-specific, current rights, sample/class support và calibration/performance gates đã pre-register. Report closed-trade-only có thể `ACCEPTED_SHADOW_ONLY` nhưng luôn `candidate_completeness_verified=false` và không được PAPER_GATE.
4. Chỉ report `ACCEPTED_PAPER_GATE_ELIGIBLE` có candidate-universe proof đầy đủ, đại diện, cùng declared test scope, ID/hash khớp mới đủ điều kiện gate; completeness false/null/thiếu proof luôn bị từ chối dù metric đạt.
5. Deployment event scorer bất biến ghim artifact v2/report scorer ACCEPTED/policy hash/registry revision/active pointer version/proof hash/actor/MFA/CAS; Task6 diagnostic report hoặc pointer metadata không thể thay thế event/evidence đó. Runtime pin đúng revisions; rollback làm gate deployment cũ hết hiệu lực.
6. Shadow production Compose E2E chứng minh zero side effects execution; PAPER_GATE chỉ có thể bật sau approval riêng, mọi candidate APPROVE vẫn qua RiskEngine, VETO/ABSTAIN chặn candidate; không có execution call trong scorer.
7. Role/RLS/cross-workspace/column grants, immutability/idempotency/restart/rollback/rights-revocation tests đạt; Playwright desktop/mobile đạt nếu UI thay đổi; screenshot được lưu.
8. Unit, Compose PostgreSQL, relevant full regression, Ruff, format, mypy, Alembic single head/check và `git diff --check` đạt; WBS, module, status, glossary và memory đồng bộ/đọc lại.

Passing một synthetic calibration fixture chỉ xác nhận implementation; không chứng minh model có edge trên Binance hoặc tương lai. Nếu sample/test không đạt policy thì chức năng vẫn chạy `ABSTAIN`, không được hạ ngưỡng để ép promotion. Task9 full journey/security review vẫn là điều kiện để đóng toàn bộ WP06.

## Phụ thuộc và giới hạn còn lại

Thứ tự: Task3 approved split + immutable normalized fold samples → Task5 diagnostic artifact v1 → Task7C vector/calibration/test evaluation + candidate-universe proof → Task6 registry approval/pointer extension và scorer deployment ledger → Task7 runtime SHADOW/PAPER gate → Task7B coverage/drift/proposal → Task8C admin UI → Task9 integrated Compose/security review. WP05 current-rights lock phải còn giữ tới receipt/evaluation commit. Feature/training sample hiện chỉ đại diện trade outcome đã đóng của candidate được strategy chọn; nó không xác nhận universe đầy đủ, không học no-entry opportunities và không chứng minh tính đại diện. Vì vậy chỉ SHADOW được phép khi chưa có proof; `PAPER_GATE` luôn bị khóa cho tới khi proof và report scorer đủ điều kiện. Khi chưa đủ data rights, sample size, test support hoặc calibrated report, scorer phải tiếp tục `ABSTAIN`.

Không thêm dependency ML. Không sửa các migration lịch sử. Việc triển khai phải đọc lại API/framework/database docs hiện hành nếu thay đổi contract/thư viện, ghi URL/ngày vào implementation memory, và thực hiện theo TDD từng lát.
