# WP06 Task7/8 — kế hoạch quan sát shadow và đề xuất drift an toàn

**Ngày:** 2026-11-08 <br>
**Trạng thái:** đang triển khai; chưa nghiệm thu Task7.
**Liên quan:** [thiết kế hoàn tất WP06](../specs/2026-11-06-wp06-completion-design.md), [plan hoàn tất WP06](./2026-11-06-wp06-completion.md), [WBS WP06](./2026-11-01-wp06-learning-wbs.md), [implementation memory](../../implementation/implementation-memory.md).

## Mục tiêu và giới hạn

Tạo một observer workspace-scoped đọc quyết định PAPER đã persist, lưu lineage của challenger và incumbent trên cùng một snapshot, ghi nhận `ABSTAIN` của challenger hiện tại, đo tình trạng coverage/chất lượng dữ liệu, rồi tạo assessment và retrain proposal bất biến khi policy cho phép. Observer không nhận vector hoặc score từ request, không gọi planner/risk/execution, không enqueue job, không đổi registry pointer và không gửi lệnh.

Đây là lát cắt quan sát an toàn, không phải nghiệm thu đầy đủ shadow scoring hay model-performance drift. Trainer hiện cố định artifact `calibrated=false` và `scorer_state=abstain`; `ScoreResult` cấm probability/confidence/uncertainty cho `ABSTAIN`. Feature keys trong artifact là `timeframe:name`, khác contract tên feature của scorer; runtime giữ `DecisionSnapshot`, scorer yêu cầu `FeatureSnapshot`. Không đổi tên/hash feature ngầm, không đưa logistic chưa hiệu chuẩn vào score và không tính `ABSTAIN/ABSTAIN` thành agreement hoặc hiệu năng.

Các phần cần một design/approval khác trước khi được gọi là hoàn tất gồm calibration có phiên bản, mapping feature/schema có kiểm chứng, temporal out-of-sample evaluation của chính artifact, attribution outcome theo đúng snapshot và bridge incident→entry-gate. Trong lúc chưa có, performance/calibration drift phải là `inconclusive`; proposal không được diễn đạt như lệnh huấn luyện đã chạy.

## Trạng thái triển khai (2026-11-08)

- Task7A đã có domain observation, repository/application worker slice, CLI/supervisor/Compose role và migration `d3a5c7e9b1d4`; challenger vẫn chỉ `ABSTAIN`. DB kiểm tra lineage, receipt quyền, logical uniqueness và RLS/immutability. Active pointer được khóa bằng đúng advisory key của registry writer trước khi observer quét.
- Task7B đã có domain, repository và application persistence. App giữ thứ tự source-rights lock → cohort lock; nếu cohort chỉ có quan sát vốn đã không đủ quyền thì lưu `INCONCLUSIVE/RIGHTS_UNVERIFIED`, còn thiếu receipt hoặc fingerprint lệch đối với member từng đủ quyền thì fail-closed. Policy cố định `coverage-v1` (N=3, coverage .8, quality .9); assessment/members/proposal được lưu cùng UoW và retry trả row gốc. Cohort rỗng không tạo row thiếu scope, transaction timestamp cũ hơn member mới nhất fail-closed. Unit domain/repository/application **37 passed** sau bổ sung hai nhánh quyền; Ruff/format/Mypy đạt.
- Snapshot kiểm chứng hiện tại: focused unit **70 passed** trước lần gom nhóm cuối; regression `PYTHONPATH=. uv run pytest tests/unit -q --tb=short` **3056 passed/15.68s**; PostgreSQL Compose `tests/e2e/test_wp06_shadow_drift_persistence.py` **1 passed/15.93s** với `alembic check`, RLS, policy forgery, quyền, lineage fail-closed và downgrade guard.
- Task8C read-only responsive admin đã có Playwright desktop/mobile **1 passed** (4 unit bổ sung; tổng lát UI 5 passed) và screenshot; nhãn thời gian dùng `giờ Việt Nam (UTC+7)`, fixture sample thấp khớp policy. Migration DB tái tính canonical content hash/idempotency và persistence E2E kiểm forged hash/key. Task9 Compose **1 passed/78.43s** chứng minh positive decision → observation → assessment/member → rollback/restart/retry, active-pointer race, current source-rights revocation và DB trigger từ chối observation sai source/fingerprint. Còn mở: opportunity-universe proof bao phủ no-entry; canonical vector parity, scorer v2/calibration/OOS/deployment governance và independent review.

## Hợp đồng dữ liệu

### 7A — quan sát quyết định cặp

1. Nguồn là quyết định bot/PAPER và shared signal evidence đã ghi trong workspace. Worker nhận workspace cấu hình, không nhận danh sách decision ID hay dữ liệu do caller khai là đầy đủ. Repository phải xác minh lại decision hash, evidence hash, thời điểm và workspace; chỉ tham chiếu đúng snapshot đã persist.
2. Lưu một observation bất biến cho mỗi khóa `(workspace, incumbent decision, challenger registry revision, snapshot hash)`. Metadata tối thiểu: ID nguồn, snapshot ID/hash/schema/as-of, incumbent release ID/hash và kết quả strategy đã persist, challenger registry/artifact ID/hash, active pointer version/revision, rights fingerprint/eligibility nếu áp dụng, thời điểm quan sát, trạng thái challenger (`ABSTAIN`) và mã lý do ổn định (`UNCALIBRATED_SCORE`, `FEATURE_SCHEMA_MISMATCH` hoặc mã thiếu lineage phù hợp).
3. Pair chỉ được tạo khi hai phía thật sự tham chiếu cùng snapshot ID/hash và decision time. Nếu incumbent hoặc snapshot không tồn tại/không thể xác minh thì ghi exclusion metadata riêng hoặc bỏ qua theo policy; không tạo baseline giả.
4. Không sao chép feature vector, sample, label, outcome/holdout payload vào observation. Không đọc `normalized_learning_sample` hay manifest payload. Source-dependent evidence cần rights check hiện hành theo boundary WP05; nếu chưa có boundary phù hợp, fail-closed và để observation ở trạng thái chưa đủ bằng chứng.
5. Registry pointer/revision và approval event được pin vào mỗi row. Rollback không sửa observations cũ; observation mới mang pointer/revision mới. Việc resolver đọc registry và pointer phải ở cùng transaction/snapshot để tránh ghép hai thế hệ.

### 7B — assessment drift dữ liệu và retrain proposal

1. Assessment ghim `policy_version`, khoảng thời gian/cohort, workspace/challenger revision, baseline ID/hash nếu có, ordered membership digest của observation IDs, denominator, exclusion count và các tỷ lệ coverage/chất lượng/abstention có thể tính từ metadata authoritative.
2. `detected` chỉ áp dụng cho ngưỡng coverage/chất lượng dữ liệu đã version hóa. `inconclusive` là bắt buộc khi không có incumbent, snapshot schema không tương thích, cỡ mẫu thấp, thiếu rights hoặc challenger toàn `ABSTAIN`. Không phát `degraded` về lợi nhuận/calibration/performance từ các điều kiện này.
3. Proposal append-only gắn đúng assessment ID/hash, lý do, policy và thời điểm. Proposal không chứa job ID, không tự enqueue trainer, không tự approve/promote hoặc đổi active pointer. Người được ủy quyền phải dùng flow enqueue hiện có để tạo attempt riêng sau khi xem xét.
4. Chống lặp bằng khóa idempotent theo workspace, challenger revision, policy và cohort/membership hash. Retry cùng đầu vào trả cùng record; cùng khóa với payload khác bị conflict.

### Task8 — read-only admin view cho observation/drift

Mở rộng trang learning hiện có bằng phần danh sách observations, coverage assessments và retrain proposals dạng metadata-only. Giao diện tiếng Việt, Be Vietnam Pro, responsive desktop/mobile; phân biệt rõ “chưa đủ bằng chứng”, “thiếu coverage” và “đề xuất chờ người duyệt”. Không hiển thị feature/label/holdout, không có nút auto-train/auto-promote. Nếu thêm thao tác enqueue, dùng riêng form/API hiện hữu có CSRF, MFA, role và idempotency; không gắn nó với nút tạo proposal.

## Phân rã theo TDD

### Task7A.1 — domain observation contract

- **Trước implementation:** xác nhận exact source columns và hash reconstruction của `BotDecisionRecord`, shared signal evidence, `FeatureSnapshotRecord`, registry entry, approval event và pointer; xác nhận source-rights gateway mà observer được phép gọi. Không thiết kế dựa trên payload do request gửi.
- **RED:** `tests/unit/test_learning_shadow.py` kiểm workspace/registry lineage, pair bắt buộc cùng snapshot ID/hash/time, pointer revision pinning, immutable canonical hash, ABSTAIN không có score, lý do mismatch, và reject mọi field order/execution.
- **GREEN:** thêm `src/trading_bot/domain/learning/shadow.py` với typed `ShadowObservation`, `IncumbentReference`, `ChallengerReference` và fixed fail-closed result. Không gọi `score_candidate` để tạo probability.
- **Gate:** schema hiện tại không biểu diễn được cùng snapshot hoặc persisted incumbent result thì dừng và ghi rõ missing contract; không tạo adapter đổi feature.

### Task7A.2 — repository, migration, observer service

- **Migration:** thêm `learning_shadow_observation` append-only, FK `(workspace_id, decision_id)` và `(workspace_id, registry_id)`, hash/schema/time/status constraints, unique idempotency key, RLS + FORCE RLS, workspace policies, trigger UPDATE/DELETE deny và populated downgrade guard. Cấp observer role chỉ quyền đọc đúng cột lineage/metadata, ghi observation; cấm đọc payload feature/label/holdout và cấm ghi registry, training job, risk/order.
- **RED:** repository tests về canonical/hash verification, cross-tenant, duplicate/conflict, rollback atomicity, stale pointer, revoked rights và column grants. Migration test kiểm đúng role/policy/trigger/constraint/downgrade.
- **GREEN:** `src/trading_bot/adapters/database/repositories/learning_shadow.py` xác minh nguồn trong cùng workspace transaction; `src/trading_bot/application/learning_shadow.py` resolve pointer + artifact metadata + incumbent/snapshot lineage rồi append row nguyên tử. Artifact không `calibrated=true` hoặc schema không khớp luôn sinh ABSTAIN có reason.
- **Worker/CLI:** `src/trading_bot/workers/learning_shadow_observer.py` và `src/trading_bot/cli/learning_shadow.py` nhận duy nhất workspace cấu hình, đọc incremental theo watermark/ID ổn định, idempotent khi restart. Compose role/service riêng (`crypidian_learning_observer`) không dùng secret trainer/admin. Không gắn vào đường tạo order.
- **RED/E2E:** Compose test chạy một persisted PAPER decision qua observer, kiểm snapshot pointer pinning, duplicate retry, restart không nhân đôi, cross-workspace denial, revoked-rights fail-closed, direct payload SELECT bị từ chối và INSERT vào order/reservation/pointer/training job bị từ chối.

### Task7B — drift assessment và proposal

- **RED:** `tests/unit/test_learning_drift.py` kiểm policy versioning, window/cohort, membership digest, low-N=`inconclusive`, thiếu incumbent/schema/rights, all-ABSTAIN không cho kết luận performance, coverage threshold chỉ dùng denominator authoritative, proposal idempotency/conflict và cấm enqueue/promote/order.
- **GREEN:** thêm `src/trading_bot/domain/learning/drift.py`, `src/trading_bot/application/learning_drift.py`, repository `learning_drift.py` và migration cho `learning_drift_assessment`, `learning_retrain_proposal`. Cả hai workspace-scoped, append-only, RLS/immutability/downgrade guard; proposal FK exact assessment hash.
- **Quyền:** observer chỉ INSERT assessment/proposal và đọc observation metadata cần thiết; admin chỉ SELECT metadata. Proposal không được cấp quyền trainer, registry write, active pointer hoặc entry gate.
- **Compose E2E:** lấy nhiều observations có schema/coverage khác nhau, xác minh threshold data-quality đúng policy, low-N/insufficient baseline thành inconclusive, membership digest tái lập, proposal bám exact assessment, restart/idempotency, cross-workspace, ACL/immutability, không tạo training attempt và không thay pointer.

### Task8C — trang quản trị metadata-only

- **RED:** Playwright API/UI tests desktop 1280px và mobile 390px kiểm tiếng Việt + font, no horizontal overflow, keyboard/focus, CSRF/no-store/role, projection không có payload, labels trạng thái và không có auto-enqueue/promote action.
- **GREEN:** bổ sung read-only route/service/schemas/template/static dưới `/admin/learning/observations` hoặc section tương ứng trong trang learning; dữ liệu phân trang/có giới hạn, workspace từ Principal, không truyền workspace/actor qua body/query tin cậy.
- **E2E:** screenshot desktop/mobile lưu trong `tests/e2e/evidence/`; kiểm hiển thị warning rõ “challenger đang ABSTAIN — chưa đánh giá hiệu năng”. Không thay đổi UI cũ ngoài phần navigation cần thiết.

## Task9 — hành trình tích hợp và review

**Kiểm chứng 08/11/2026:** `tests/e2e/test_wp06_task9_integrated_learning_journey.py` → **1 passed/63.50s**; real candidate proof/split approval/trainer/evaluator/registry/PAPER decision/observer/drift assessment/member/rollback/restart/idempotency và no-execution invariants đều có trong cùng journey. Shadow drift persistence riêng **1 passed/19.46s**. Còn rights revocation/pointer-race evidence và independent security review; scorer v2 không thuộc observer-only completion.

1. Compose đi từ actual candidate proof → reviewer MFA approve → builder seal → trainer job/artifact → evaluator report → registry approval/pointer → persisted PAPER decision → observer pair → assessment/proposal → pointer rollback → observation kế tiếp ghim revision mới → restart/duplicate retry.
2. Dùng đúng artifact do trainer sinh nhưng assert bất biến `calibrated=false`, `scorer_state=abstain`; không dùng fake calibrated response. Kiểm proposal không tạo job, không thay pointer và không tác động planner/risk/order bằng cách so cùng input/decision state trước/sau khi observer được bật.
3. Thêm role matrix: observer không đọc normalized payload/holdout, không ghi order/risk reservation/registry pointer/training jobs; admin đọc metadata tối thiểu; trainer/evaluator không có quyền observer write; cross-workspace trả 0/deny.
4. Fault injection: lỗi trước commit không tạo nửa observation/assessment; crash sau commit rồi retry trả cùng record; pointer rollback cạnh tranh với observer không tạo pair ghép revision; rights revocation trong lúc read giữ source lock hoặc fail closed.
5. Independent review kiểm WBS, migration graph single-head/Alembic check, secret boundaries, no execution capability, accessibility và mọi kết luận drift có bằng chứng.

## Tiêu chí hoàn tất và phần tiếp tục mở

**Có thể đóng Task7A/7B ở phạm vi observer** khi unit + PostgreSQL Compose E2E thực chứng immutable paired lineage, ABSTAIN fail-closed, coverage/data-quality assessment authoritative, low-N inconclusive, retrain proposal append-only, restart/idempotency/ACL đúng, và observer không đổi bất cứ quyết định execution nào. Task8C đóng khi Playwright desktop/mobile, tiếng Việt, responsive, no payload và screenshots đạt.

**Không được đánh dấu WP06 Task7 hoặc WP06 hoàn tất toàn bộ** sau các tiêu chí trên. Còn mở: calibration/evaluation cho chính artifact, feature schema adapter được kiểm chứng, scorer có thể phát score hợp lệ, performance/realized-outcome drift, attribution/false-positive analysis và incident-to-entry-gate bridge. Các phần này cần design riêng, test temporal holdout/right boundaries và review thay đổi runtime; không nới contract chỉ để tạo tín hiệu demo.

Trước khi đóng WP06, Task9 còn phải có journey Compose trên cùng lineage, UI screenshot nếu UI đổi, independent review đã xử lý finding, WBS/module/project-status/glossary/memory được đồng bộ, đọc lại memory và chạy đầy đủ unit/E2E liên quan. Không commit cho tới khi các cổng test hiện hành của dự án đạt.

## Phụ thuộc

`WP06.1` feature/decision lineage + persisted PAPER decisions → `WP06.6` registry approval/pointer → `WP06.7A` observer → `WP06.7B` data-quality drift → `WP06.8C` UI metadata → `WP06.9` integrated verification. Task3 split approval, Task4/5 trainer lifecycle và Task3G/H/I rights gates là tiền đề dữ liệu. Task8A/B training UI và registry UI đã có; plan này chỉ thêm phần quan sát/drift, không thay thế chúng.
