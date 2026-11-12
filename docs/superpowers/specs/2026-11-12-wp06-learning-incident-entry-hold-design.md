# Thiết kế WP06 — Xác nhận entry hold cho learning incident

**Ngày:** 2026-11-12
**Trạng thái:** đặc tả triển khai theo WBS WP06 đã được ủy quyền; người dùng chọn ledger riêng và xác nhận chủ động bằng MFA.
**Phạm vi:** đường từ data-quality incident đã lưu đến entry gate PAPER; không thay đổi market-data source rights hoặc mở active scorer.

## Mục tiêu

Khi WP06 lưu một learning incident, người có thẩm quyền phải chủ động xác nhận bằng MFA gần đây và ghi lý do trước khi hệ thống chặn entry mới. Hệ thống không tự pause hoặc tự mở lại bot chỉ vì assessment thay đổi. Owner/risk-admin có thể xem lịch sử hold/release; mọi entry mới trong scope đang bị hold đều bị từ chối, còn quản lý vị thế, protective exit và các thao tác thoát lệnh tiếp tục chạy.

## Hiện trạng liên quan

- `LearningDriftAssessment` hiện chỉ đo snapshot coverage và chất lượng dữ liệu. `DETECTED` chỉ xuất hiện với `COVERAGE_BELOW_THRESHOLD` hoặc `QUALITY_BELOW_THRESHOLD`; hiệu năng luôn `INCONCLUSIVE` do chưa có outcome attribution. `LearningRetrainProposal` là bản ghi metadata bất biến, không phải training job.
- Scorer deployment và resolver được scope theo `workspace_id`. Runtime PAPER đọc `BotRuntimeControl` và account activity incident trong transaction entry; `EntryGate` chỉ áp cho `NEW_ENTRY`, không cho protective operation.
- Workspace kill switch đã có nhưng là control vận hành chung, không gắn quan hệ dữ liệu tới learning proposal. Chức năng `pause_workspace` hiện có không yêu cầu MFA; release có MFA.

## Quyết định và phương án

1. **Ledger entry hold riêng theo learning proposal — chọn.** Lưu từng lần xác nhận/chấm dứt hold trong ledger append-only, liên kết chính xác proposal/assessment/hash và reviewer. Runtime đọc trạng thái active theo workspace trong transaction entry. Đổi lại cần migration, quyền DB hẹp, application/API/UI và E2E riêng.
2. **Dùng workspace kill switch.** Ít mã hơn và entry đã được chặn ở runtime, nhưng history chỉ ghi control vận hành chung; không có khóa quan hệ tới proposal, không phân biệt hold học máy với sự cố sàn/ops và retry/release khó audit theo từng incident.
3. **Pause từng bot.** Không chọn vì C4 deployment/incident scope là workspace-wide; nhiều thao tác bot có thể thành công một phần hoặc lệch nhau khi có concurrency.

Người dùng chọn phương án 1. Phạm vi một hold là **workspace** vì scorer target hiện được resolve theo workspace; nhiều proposal có thể cùng mở hold, và chỉ khi mọi hold đều được release thì entry mới được tiếp tục.

### Ruling sau khi đối chiếu source

- Observation được tạo cho challenger `EVALUATED` khác model active; assessment ghim challenger ID/revision, không phải active target. Khi registry được kích hoạt, revision còn tăng. Vì vậy không so challenger ID/revision với scorer target. Thay vào đó, HOLD chỉ hợp lệ khi mọi observation thuộc assessment cùng ghim một incumbent registry ID/revision/artifact hash/pointer version; scorer deployment hiện hành phải trỏ cùng registry ID/revision/pointer, còn artifact hash phải khớp `learning_model_registry.artifact_hash` trong DB. `ScorerDeploymentTarget.scorer_artifact_hash` là hash của artifact scorer riêng, không được so hoặc gộp với incumbent model artifact hash. Cohort có incumbent không đồng nhất hoặc target đã đổi không được HOLD; cần assessment mới sau khi target ổn định.
- RELEASE không phụ thuộc target hiện hành; nếu không, deployment đổi có thể làm hold không thể gỡ. Idempotent retry tra event gốc trước khi kiểm tra target/CAS để retry hợp lệ vẫn trả kết quả ban đầu.
- HOLD/RELEASE lấy cùng workspace-settings lock L1 với giao dịch entry trước khi lấy khóa deployment/registry theo thứ tự hiện hành. Như vậy xác nhận hold và entry mới được tuần tự hóa; không thể để entry đọc trạng thái cũ rồi ghi sau khi HOLD đã commit.
- Entry gate chạy trong transaction PAPER sau khi BotDecision/score-event có thể đã được ghi ở dispatcher transaction trước đó. “Không side effect” tại đây nghĩa là không ghi execution decision, order intent, reservation hoặc trade trong transaction entry; không xóa hay phủ nhận scorer audit record đã tồn tại.

## Chấp thuận

Ngày 12/11/2026, người dùng duyệt triển khai theo đặc tả này, chọn **chỉ target còn active** và **không HOLD lại cùng proposal sau RELEASE**. “Proposal khớp target active” được thực thi qua incumbent registry/revision/artifact/pointer được ghim bởi toàn bộ observation trong assessment; challenger ID của proposal vốn khác incumbent theo hợp đồng observer hiện tại. HOLD bị từ chối nếu assessment cohort không đồng nhất hoặc incumbent target đã đổi.

## Hành vi

### Tạo hold

- Chỉ proposal đã persist, nối tới assessment mới nhất của cùng cohort/policy và `DETECTED` vì quality/coverage mới được hiển thị với nút xác nhận. Nếu assessment mới hơn là `INCONCLUSIVE`/`NO_DRIFT`, proposal cũ không còn nút HOLD. Rights-only warning hoặc performance status không tạo hold qua flow này.
- Khi xác nhận, server xác minh toàn bộ assessment membership ghim cùng incumbent registry ID/revision/artifact hash/pointer version, và scorer deployment hiện hành tham chiếu đúng tuple đó. Proposal challenger không cần bằng ID với incumbent; assessment cũ hoặc pointer đã đổi bị từ chối, cần đánh giá lại theo target hiện hành.
- Chỉ `owner` hoặc `risk_admin`, có `Permission.LEARNING_INCIDENT_MANAGE`, recent MFA trong `RECENT_MFA_LIFETIME`, CSRF hợp lệ, lý do 1–500 ký tự và idempotency key mới được ghi `HOLD`.
- Request phải ghim `proposal_id`, `assessment_id`, `assessment_hash`, incumbent target tuple, `expected_revision`, `expected_deployment_revision`, `expected_active_pointer_version` và reason; server nạp lại mọi quan hệ/hash trong cùng transaction, actor lấy từ authenticated `Principal`.
- Tạo event append-only, tăng revision và ghi audit trong cùng transaction. Cùng idempotency key/cùng payload trả event gốc trước mọi kiểm tra trạng thái hiện hành; key dùng lại với payload khác conflict. CAS lệch trả conflict, không ghi hold.
- Chỉ hành động xác nhận mới tạo trạng thái chặn. Assessment/proposal được phát hiện, retry evaluator hoặc thay đổi score không tự đổi entry state.

### Entry và release

- Runtime đọc hold active theo workspace trong cùng transaction của `NEW_ENTRY`, sau control/fencing checks và trước planner/risk/reservation/write; nếu tồn tại trả reason code ổn định `LEARNING_INCIDENT_ENTRY_HOLD` và không ghi execution decision/order/reservation/trade trong transaction đó. BotDecision/score-event đã được dispatcher ghi trước đó có thể tồn tại và giữ nguyên.
- Gate không áp cho `PROTECTIVE_EXIT` hoặc monitor/reconciliation. Một hold không flatten vị thế, không hủy protective order, không đổi trạng thái account/bot thành DISABLED.
- Hold còn active sau restart và sau khi xuất hiện assessment mới. Không auto-release.
- Release là event `RELEASE` riêng, cùng yêu cầu owner/risk-admin + MFA gần đây + lý do + idempotency + `expected_revision`, nhưng không yêu cầu scorer target cũ vẫn active. Release một proposal không xóa hold của proposal khác. `RELEASE` là trạng thái cuối cho proposal đó; nếu drift tái diễn cần assessment/proposal mới, không append HOLD sau RELEASE. Chủ thể kiểm tra/ghi nhận đã xử lý nguyên nhân trước khi release; hệ thống lưu lý do nhưng không suy đoán remediation từ score.

## Dữ liệu, bảo mật và quyền

- Tạo bảng event metadata-only với workspace, proposal/assessment IDs + exact hashes, state (`HOLD`/`RELEASE`), revision/predecessor, actor membership, MFA verified-at/method, reason, idempotency key, request/correlation ID và created-at. Không sao chép sample, feature, label, payload TEST hoặc source bar.
- Append-only; RLS/FORCE RLS theo workspace; populated downgrade guard. Admin writer/API đọc lịch sử; runtime chỉ cần kiểm tra active-state. Không cấp write cho trainer, evaluator, market-signal worker hoặc venue executor.
- Trigger/unique constraints chặn revision fork, sai proposal lineage, duplicate idempotency hoặc thay đổi/xóa event. Runtime DB access chỉ đủ để đọc boolean/reason code qua workspace-scoped projection/function; không đọc proposal payload.
- Thêm quyền `learning:incident_manage` chỉ cho OWNER và RISK_ADMIN. Không dùng `LEARNING_MODEL_APPROVE` vì hiện quyền đó gồm analyst reviewer và thiếu risk-admin; không dùng bot operator pause permission.
- Trang observation/drift responsive hiển thị trạng thái hold, người thực hiện, thời gian, revision và lý do; không lộ dữ liệu mẫu. Owner/risk-admin được đọc đúng metadata incident qua quyền mới, không nhận ngầm quyền đọc toàn bộ model registry. Form confirm/release dùng CSRF, no-store, idempotency, expected revision; tiếng Việt/Be Vietnam Pro, desktop/mobile.

## Ngoài phạm vi

- Tự động pause khi phát hiện incident; tự auto-release; dừng workspace bằng kill switch; flatten vị thế; hủy exit/protective orders.
- Dùng `coverage-v1`/`coverage-v2-30d` history, QA fixture, source không có quyền TRAINING, diagnostic/inconclusive report hoặc performance drift làm điều kiện kích hoạt.
- Thay ngưỡng `wp06-scorer-test-v1`, bật SHADOW/PAPER_GATE, thay strategy/risk, gửi lệnh venue hoặc Telegram cảnh báo.
- Coi `LearningRetrainProposal` là kết luận về PnL/profitability hay tuyên bố incident đã được khắc phục sau một assessment `NO_DRIFT`.

## Nghiệm thu

1. Unit/HTTP tests: chỉ OWNER/RISK_ADMIN + MFA + reason + CSRF + exact proposal/hash + idempotency/CAS được xác nhận; role khác, MFA cũ/tương lai, stale hash/revision, payload injection và retry conflict không ghi event.
2. PostgreSQL Compose: RLS/workspace isolation, grants, immutable event, trigger/sequence, duplicate-key behavior, stale active-target rejection, release-terminal constraint, competing hold/release CAS, rollback atomically với audit và populated downgrade guard.
3. Runtime E2E: hold active chặn `NEW_ENTRY` với reason ổn định và zero write side effects; hold của workspace khác không tác động; protective exit/monitor vẫn chạy; hold sống qua restart; release một proposal không bỏ hold thứ hai.
4. Playwright desktop 1280px và mobile 390px: proposal `DETECTED` có action; inconclusive/no-drift không có action; confirm/release yêu cầu reason/MFA; history chính xác, không tràn ngang/không lộ payload mẫu; tạo và kiểm trực quan screenshot.
5. E2E dùng dữ liệu tổng hợp cô lập chỉ để kiểm ledger/runtime boundary; không ghi nhận fixture đó là source-rights receipt, report đủ điều kiện hay bằng chứng deployment. Feature này không đóng blocker positive active scorer; WP06 chỉ đóng khi các tiêu chí dữ liệu/licensing, active scorer, performance drift và integrated review độc lập đều đạt.

## Các lựa chọn để builder hoàn thiện

- Tên migration/table/helper/router theo naming convention repo, giữ state-machine invariants trên đây.
- Giao diện gắn vào trang observation/drift đang có; không tạo trang dashboard thứ hai.
- Khi proposal cũ đã `HOLD`/`RELEASE`, UI biểu diễn bằng event hiện hành; không sửa/xóa lịch sử.

## Kết quả đối chiếu implementation

- Assessment chỉ ghim challenger ID/revision; registry activation làm revision tăng. Yêu cầu challenger trùng active scorer target khiến không có proposal nào đi qua HOLD. Ruling đã sửa: ghim incumbent target từ mọi observation trong membership và so khớp với target hiện hành; cohort không đồng nhất hoặc target stale bị từ chối.
- Risk-admin hiện không có `LEARNING_MODEL_READ`; quyền mới cho phép xem metadata incident cần thiết mà không cấp model read chung.
- Gate ở PAPER runtime không ngăn BotDecision/score-event đã ghi ở dispatcher transaction trước đó. Acceptance chỉ yêu cầu không side effect thuộc transaction execution entry.
- `RELEASE` cuối cho proposal và independent khỏi target là các quyết định kỹ thuật bảo thủ để không tái sử dụng incident cũ và không tạo hold không thể tháo. Tất cả được ghim bằng unit/Compose/Playwright tests.

## Nguồn nội bộ đã đọc

- [`domain/learning/drift.py`](../../../src/trading_bot/domain/learning/drift.py) — chỉ quality/coverage mới tạo proposal; performance luôn `INCONCLUSIVE`.
- [`application/learning_drift.py`](../../../src/trading_bot/application/learning_drift.py) và [`repositories/learning_drift.py`](../../../src/trading_bot/adapters/database/repositories/learning_drift.py) — persistence/projection scope.
- [`domain/execution/runtime_control.py`](../../../src/trading_bot/domain/execution/runtime_control.py) và [`paper_runtime_application.py`](../../../src/trading_bot/application/paper_runtime_application.py) — New Entry gate, protective-operation boundary.
- [`policy.py`](../../../src/trading_bot/auth/policy.py) — role/permission model.
