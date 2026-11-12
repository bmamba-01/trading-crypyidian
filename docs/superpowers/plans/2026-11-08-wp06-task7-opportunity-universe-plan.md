# Kế hoạch triển khai WP06 Task7 — proof cơ hội quyết định và candidate

> **Dành cho agent triển khai:** BẮT BUỘC dùng `superpowers:subagent-driven-development` hoặc `superpowers:executing-plans`; thực hiện từng task với RED → GREEN → REFACTOR. Không bắt đầu trước khi dependency vector chuẩn bên dưới đã được hợp nhất và kiểm chứng.

**Mục tiêu:** Ghi nhận bất biến mọi cơ hội quyết định trong phạm vi replay BASE đã khai báo trước, bao gồm cả lượt strategy không phát candidate, rồi tạo proof version mới nối candidate với outcome gốc mà không gán nhãn giả.

**Kiến trúc:** Giữ nguyên proof `base-closed-trade-universe-v1` và report chẩn đoán hiện có. Replay writer sẽ thêm một hàng metadata-only cho mỗi nến quyết định dự kiến, gồm trạng thái đầu vào, kết quả strategy/pipeline, định danh candidate và hash lineage của vector chuẩn; trade outcome đóng sẽ tham chiếu cơ hội entry ban đầu. Candidate builder dựng proof version mới bằng cách đối chiếu opportunity ledger với run/trial/BASE scenario, bundle/manifest, report, child rights hiện hành và outcome links dưới source lock cùng trial lock.

**Công nghệ:** Python hiện có, SQLAlchemy, Alembic, PostgreSQL/RLS, pytest và Docker Compose; không thêm dependency ML hoặc database.

**Thiết kế đã duyệt:** [Task7 scorer PAPER đã hiệu chuẩn](../specs/2026-11-08-wp06-task7-calibrated-scorer-design.md), nhất là hợp đồng vector chuẩn và proof cơ hội ở mục 7C.

## Điều kiện bắt đầu

- Task7C.1 canonical vector phải được triển khai trước. Người thực hiện đọc API cuối cùng của `LearningFeatureVector`/builder, exact per-timeframe lineage và test parity; plan này tiêu thụ đúng API đó, không tự dựng vector/hash hoặc suy nguồn từ `feature_id`.
- Trước khi bắt đầu Task 1, phải đạt C.1.1 PostgreSQL Compose E2E theo gate bên dưới. Unit parity riêng chưa thay thế kiểm chứng trên PostgreSQL.
- Vector builder phải hỗ trợ từ `DecisionSnapshot` chính xác được replay evaluator dùng. Thành phần vector dự kiến gồm composite snapshot ID/hash, schema/hash, canonical values hash, `feature_snapshot_id` theo timeframe, source event/bar digest, as-of/availability, quality/coverage và strategy release/hash theo thiết kế Task7.
- Khi vào implementation phải xác nhận migration graph vẫn có một head và chọn `down_revision` hiện hành. Migration `b2d4f6a8c0e1_wp06_decision_opportunity_universe.py` đã được áp dụng/accepted ở Task2; Task3 nếu cần quyền nguồn phải thêm migration mới nối tiếp head hiện hành, không sửa migration đã phát hành và không tạo nhánh thứ hai.
- Nếu vector chưa mang exact source lineage hoặc parity test chưa đạt, dừng Task7 opportunity implementation; không thay bằng timestamp lookup, hash `DecisionSnapshot` cũ hay payload do caller gửi.

## Ràng buộc toàn cục

- Query version mới: `base-decision-opportunity-universe-v1`; query version cũ và canonical bytes proof v1 giữ nguyên.
- Phạm vi proof ban đầu chỉ là một run/trial `BASE`, một symbol, strategy release/hash, decision timeframe và source window `[evaluation_start, range_end)` được ghim trước replay/test. Không suy rộng sang toàn bot hoặc symbol khác.
- Expected opportunity inventory được dựng từ ordered source-bar identities và evaluation scope đã ghim trong child manifest/bundle; không lấy expected IDs/count từ replay rows, caller hoặc tập observations thực sự nhận được. Vì vậy observation bị thiếu hoàn toàn vẫn tạo gap khi proof đối chiếu inventory. Candidate builder chỉ đọc cột identity metadata cần thiết; không đọc OHLCV/raw payload.
- Mỗi nến quyết định dự kiến phải có đúng một opportunity row. `INPUT_EXCLUDED + NOT_RUN` ghi input/vector readiness bị loại và làm completeness false; không biến input thiếu thành `NO_CANDIDATE`.
- Input hợp lệ nhưng config/pipeline dừng trước strategy được ghi `EVALUATED + NOT_RUN` cùng vector chuẩn và reason code. Đây là trạng thái riêng với `INPUT_EXCLUDED`; không tính là `NO_CANDIDATE`.
- `NO_CANDIDATE` chỉ được ghi khi strategy thật sự được gọi và trả `NoTrade`. Candidate bị planner/risk từ chối vẫn là candidate trong universe.
- Opportunity/proof lưu metadata và hash/ID nguồn; không lưu raw feature vector, label, holdout payload hoặc dữ liệu OHLCV trùng lặp.
- Rights receipt `BACKTEST_RESEARCH` tại thời điểm replay không cấp quyền TRAINING. Proof builder recheck TRAINING cho từng child dưới WP05 source lock và ghi receipt bất biến theo proof; evaluator tiếp tục dùng receipt/gateway hiện có.
- Opportunity ledger, outcome link và proof phải append-only, workspace-scoped, RLS FORCE, exact role grants và retry idempotent. Mọi lỗi lineage, rights, hash, gap hoặc count phải fail-closed.
- Không đổi model artifact v1, scorer mode, candidate completeness gate, registry deployment contract hoặc PAPER runtime side effects.
- Sau khi implement, cập nhật memory/WBS/module/status/glossary theo AGENTS.md; không coi test chưa chạy/skip/flaky là đạt. Không commit nếu unit/E2E liên quan chưa pass.

## Điểm cần soi khi review

1. Nến quyết định bị context provider loại vì input chưa sẵn sàng vẫn phải thành `INPUT_EXCLUDED`; thiếu row hoặc source gap làm proof không complete.
2. Config/pipeline `NoTrade` trước khi gọi strategy không được tính như strategy `NO_CANDIDATE`.
3. Một evaluation call có thể đóng trade cũ rồi tạo candidate mới; outcome phải gắn với opportunity/candidate lúc entry.
4. Rights replay lịch sử không thay TRAINING receipt hiện hành; không disclosure payload khi rights bị revoke/expired.
5. Insert cạnh tranh với seal phải tuần tự hóa trên cùng trial; mọi write sau proof phải bị từ chối, còn retry cùng bytes phải idempotent.

---

## Prerequisite C.1.1 — PostgreSQL runtime/replay feature-vector parity

**Trạng thái 09/11/2026:** gate **Accepted** sau scoped review fix vòng 1; Compose **1 passed/23.79s + 7 regression pass**.

Đây là gate của thiết kế scorer Task7C.1, chạy trước Opportunity Task 1; nó chỉ xác minh hợp đồng snapshot/vector, chưa xác minh `TRAINING` rights và không tạo scorer/report/deployment.

**Files:** tạo `tests/e2e/test_wp06_learning_feature_vector_parity.py` và `tests/e2e/probes/wp06_learning_feature_vector_parity.py`; theo harness Compose đang có, tạo volume mount tests chỉ đọc, project riêng, cleanup project cụ thể và chỉ xóa app image nếu chính test đã build.

**Fixture và đường production:** migrator seed workspace, instrument, `MarketRawReference`, policy/observer hiện hành cho mục đích `BACKTEST_RESEARCH`, ordered `MarketBar` v4 và replay manifest/bundle hai timeframe của một H01 run. Có ít nhất 224 nến 1h và 206 nến 4h để đủ EMA-80/EMA-200 warmup/coverage; mọi bar tham chiếu raw-reference DB không-null. Một logical bar có revision 1 và revision 2, cả hai khả dụng trước cutoff được chọn; manifest/bundle phải ghim đúng record/revision/supersedes/checksum của revision 2. Không tạo LIVE source rows để runtime và replay cùng đọc đúng REST source.

- Runtime probe chạy dưới Compose service/DB role `market-signal-worker`; gọi `PostgresMarketDecisionSnapshotProvider.snapshot_for` với evidence cutoff/freshness cutoff đã cố định, H01 release, position flat và macro trống; dựng vector bằng `build_learning_feature_vector`.
- Replay probe chạy dưới service/DB role `backtest-worker`; nạp bundle thật qua `BacktestDatasetReplayBundleLoader` + `BacktestDatasetReplayLoader` (do đó phải seed quyền `BACKTEST_RESEARCH` hợp lệ), tạo replay observations bằng `build_backtest_dataset_replay_observations`, chọn cùng decision cutoff, position flat và dựng vector bằng cùng builder.
- Hai probe phải trả metadata JSON đã lọc, không feature payload thô ra host; host E2E so sánh snapshot ID/hash, vector/schema hash, ordered feature keys/float64 values/quality/availability, strategy release ID/content hash và mọi per-timeframe lineage: feature snapshot ID, event/bar IDs/digest, schema/formula/code versions/hashes, as-of/freshness/availability, coverage/warmup/quality/regime. Xác nhận record ID/revision 2 có mặt, revision 1 vắng, raw reference bằng UUID nguồn đã seed.
- Mỗi probe dựng vector thành công rồi thử `dataclasses.replace` với raw reference khác nhưng giữ source identity/checksum/snapshot ID, source identity record/revision khác giữ snapshot ID, và feature value khác giữ snapshot ID; cả ba phải bị từ chối với `SNAPSHOT_CONTENT_MISMATCH`. Đây là lỗi builder, chưa gọi scorer `ABSTAIN`.
- Không cần hoặc nhận current `TRAINING` receipt/fingerprint; rights gate chỉ áp dụng cho loader replay `BACKTEST_RESEARCH`. Không lưu/so sánh vector values ở database.

**Acceptance:** `PYTHONPATH=. uv run pytest tests/e2e/test_wp06_learning_feature_vector_parity.py -q -s --tb=short` chạy một Compose E2E pass, không skip; `alembic check` và single-head sạch; app image cleanup chính xác, container/volume project tạm được dọn bởi test. Chỉ sau đó Task1 mới bắt đầu.

---

## Bản đồ file và quyền sở hữu

| File | Trách nhiệm sau triển khai |
| --- | --- |
| `src/trading_bot/application/backtest_decision_opportunities.py` | Contract/canonical metadata cho một opportunity và trace collector; không chứa payload feature. |
| `src/trading_bot/application/backtest.py` | Gọi hook tùy chọn `record_input_excluded(observation, reason_code)` trước khi bỏ qua observation `INPUT_NOT_READY`; evaluator generic không có hook giữ nguyên hành vi. |
| `src/trading_bot/application/paper_decision_cycle.py` | Phân biệt strategy đã được gọi nhưng trả `NoTrade` với lần cycle dừng trước strategy. |
| `src/trading_bot/application/backtest_dataset_replay_context.py` | Phát trace input cho mọi nến quyết định, kể cả input bị loại; gắn event ID/lineage của vector chuẩn. |
| `src/trading_bot/application/runtime_replay_evaluator.py` | Ghi kết quả strategy/planner/risk cho từng opportunity; giữ entry opportunity qua vòng đời intent tới lúc đóng trade. |
| `src/trading_bot/domain/learning/evaluation.py` | Thêm liên kết entry opportunity vào `EvaluationTrade` mà không đổi PnL/label contract hiện tại. |
| `src/trading_bot/adapters/database/models/backtest_opportunity_entities.py` | ORM cho opportunity ledger append-only. |
| `src/trading_bot/adapters/database/models/backtest_outcome_entities.py` | Nullable entry opportunity/candidate reference cho tương thích legacy; reference bắt buộc để eligible trong proof v2. |
| `src/trading_bot/adapters/database/models/candidate_universe_entities.py` | Hỗ trợ song song proof v1/v2 và các count/scope metadata riêng của v2; v1 canonical không đổi. |
| `src/trading_bot/adapters/database/models/__init__.py` | Export model mới theo convention hiện có. |
| `src/trading_bot/adapters/database/repositories/backtest_outcomes.py` | Persist opportunity rows và outcomes trong cùng replay persist transaction; kiểm retry/hash/conflict. |
| `src/trading_bot/adapters/database/repositories/candidate_universe_proofs.py` | Thêm v2 scanner kiểm expected scope/count/IDs/source/outcome links/rights, không nhận counts từ caller. |
| `src/trading_bot/application/candidate_universe_proof.py` | Giữ v1 type/service; thêm v2 type và service method/query version riêng. |
| `src/trading_bot/cli/candidate_proof.py` | Thêm lựa chọn query version cố định; vẫn chỉ nhận workspace/trial UUID và không nhận payload/counts/rights. |
| `migrations/versions/b2d4f6a8c0e1_wp06_decision_opportunity_universe.py` | Tạo table/index/FK/check/RLS/grants/trigger, mở rộng proof version compatibility, populated downgrade guard. |
| `tests/unit/test_backtest_decision_opportunities.py` | Contract, canonicalization, opportunity-state, candidate/outcome-link unit tests. |
| `tests/unit/test_candidate_universe_proof.py` | Regression proof v1 và unit scanner/proof v2. |
| `tests/unit/test_backtest_outcomes_repository.py` | Atomic append, entry-link integrity, retry và conflict. |
| `tests/e2e/probes/wp06_candidate_opportunity_universe.py` | PostgreSQL probe gồm coverage, rights, ACL/RLS, race/seal, migration và failure cases. |
| `tests/e2e/test_wp06_candidate_opportunity_universe.py` | Chạy probe Compose và kiểm output exact. |

### Hợp đồng dữ liệu cố định cho query v1

#### Opportunity row

Một row đại diện cho một source decision bar trong đúng decision timeframe của replay. Natural identity là `(workspace_id, trial_record_id, query_version, event_id)`; `event_id` phải ổn định theo bundle, decision timeframe, source bar, symbol và strategy release. Có unique ordinal theo trial để proof kiểm thứ tự/count.

Các trường tối thiểu:

- scope: `workspace_id`, `run_id`, `trial_record_id`, `scenario_id`, `manifest_hash`, `bundle_hash`, `decision_timeframe`, `symbol`, `strategy_release_id`, `strategy_content_hash`, `evaluation_start`, `range_end`;
- source/snapshot: `event_id`, `decision_ordinal`, `event_at`, `available_at`, `decision_at`, `source_refs_digest`, `feature_snapshot_ids` metadata theo timeframe, `snapshot_id`, `runtime_snapshot_id` nếu cycle dùng runtime state;
- metadata contract: `schema_version=crypidian-replay-decision-opportunity-v2`; `source_event_digests` chứa ordered timeframe/digest pairs, cùng keys/order với `feature_snapshot_ids`; DB và domain tính lại `source_refs_digest` từ đúng map này. Không có raw values. `content_hash` bind schema version + full metadata; idempotency bind schema version/workspace/manifest/scenario/event/query version. Query algorithm version giữ nguyên.
- canonical vector: `feature_schema_hash`, `vector_hash` từ builder đã duyệt; nullable chỉ cho input bị loại, không có feature values;
- kết quả: `input_state` (`EVALUATED`/`INPUT_EXCLUDED`), `strategy_state` (`NO_CANDIDATE`/`CANDIDATE`/`NOT_RUN`), `decision_outcome` (`NO_TRADE`/`PLAN_REJECTED`/`RISK_REJECTED`/`RISK_APPROVED`/`PAPER_INTENT_CREATED`), `reason_code`, `candidate_id` nullable;
- `canonical_opportunity`, `content_hash`, `idempotency_key` tính ở server, `created_at` từ DB.

`NOT_RUN` hợp lệ cho cả `INPUT_EXCLUDED` và input `EVALUATED` bị config/pipeline dừng trước strategy. Trường hợp `EVALUATED + NOT_RUN` phải giữ exact snapshot/schema/vector hash; `INPUT_EXCLUDED + NOT_RUN` có thể thiếu vector và luôn làm proof completeness false. `NO_CANDIDATE` chỉ hợp lệ khi `strategy_state=NO_CANDIDATE`, strategy đã được gọi, không có candidate ID và không có entry outcome. `CANDIDATE` yêu cầu stable `candidate_id` từ `StrategyCandidate.candidate_id`; planner/risk rejection không xóa candidate khỏi universe. Dữ liệu `source_refs_digest`/feature IDs phải đến từ canonical vector hoặc source envelope exact, không tạo nearest-time mapping.

#### Candidate ↔ closed outcome

- `BacktestTradeOutcomeRecord` nhận nullable `entry_opportunity_event_id`, `entry_candidate_id` và `entry_vector_hash`; null giữ tương thích outcome legacy. Opportunity có unique FK scope theo workspace/trial/event; trigger xác minh opportunity đó có candidate ID khớp outcome, `entry_decision_snapshot_id`, symbol/time/schema/vector hash nhất quán.
- Cardinality query v1 là **một candidate → zero hoặc một closed outcome**. Unique constraint ngăn link thứ hai; nếu evaluator sau này hỗ trợ nhiều trade từ một candidate thì phải thiết kế query version/cardinality mới, không nới constraint âm thầm.
- Candidate bị risk/planner reject, chưa fill, hoặc còn mở khi replay kết thúc được giữ trong universe nhưng `unlabeled`/exclusion có reason; không gán PnL 0. `NO_CANDIDATE` chỉ xác nhận coverage và không thành training sample.
- Outcome link luôn trỏ về opportunity lúc candidate được entry, không phải snapshot đóng. Intent capture phải giữ `entry_opportunity_event_id` và `entry_candidate_id` song song với entry snapshot/vector provenance.

#### Proof v2

Giữ `candidate_universe_proof` làm ledger versioned để tái dùng FK của `candidate_training_rights`; nới query constraint để cho phép đúng hai hằng version, không sửa/rehash row v1. Các trường count riêng v2 lưu: `opportunity_count`, `candidate_count`, `no_candidate_count`, `not_run_count`, `input_excluded_count`, `eligible_count`, `exclusion_count`, `unlabeled_candidate_count`, `outcome_link_count`, `source_scope_hash`, `completeness_verified` và reason counts trong canonical proof.

Các bất biến v2: `opportunity_count = candidate_count + no_candidate_count + not_run_count`; `input_excluded_count <= not_run_count`; `candidate_count = eligible_count + exclusion_count`; `unlabeled_candidate_count <= exclusion_count`; mọi count không âm; membership digest bao phủ sorted opportunity/event ID, trạng thái, candidate ID, outcome ID/hash và reason. `completeness_verified=true` chỉ khi mỗi expected decision bar có đúng một row, scope/source hashes khớp, không có input-excluded row, mọi `NOT_RUN` có reason hợp lệ, tất cả candidate/no-candidate states hợp lệ và toàn bộ outcomes map được; candidate không có closed outcome vẫn có thể nằm trong universe-complete count nhưng vẫn bị loại khỏi labeled support.

Để giữ compatibility, các cột v2 nullable trên proof v1 phải `NULL`; v2 yêu cầu đủ trường và check constraint riêng. `candidate_count` v1 tiếp tục nghĩa theo contract cũ; v2 nghĩa số strategy candidate row. Không chuyển v1 thành v2 và không cấp eligibility hồi tố cho replay cũ.

---

## Task 1: Opportunity contract và capture mọi decision bar

**Trạng thái 09/11/2026:** review độc lập **Accepted (0 Critical/Important)**; focused **89 passed/5.71s**, related **139 passed/3.21s**, Ruff/format11, mypy6, compile/head/diff-check đạt. Chỉ contract/capture domain/application; bước kế tiếp Opportunity Task2 ledger/persistence **pending/chưa bắt đầu**.

**Files:**

- Tạo `src/trading_bot/application/backtest_decision_opportunities.py`.
- Sửa `src/trading_bot/application/paper_decision_cycle.py`, `src/trading_bot/application/backtest_dataset_replay_context.py`, `src/trading_bot/application/runtime_replay_evaluator.py`, `src/trading_bot/application/backtest.py`, `src/trading_bot/domain/learning/evaluation.py`.
- Test: `tests/unit/test_backtest_decision_opportunities.py`, `tests/unit/test_runtime_replay_evaluator.py`, `tests/unit/test_backtest_dataset_replay_context.py`, `tests/unit/test_backtest_runner.py`, `tests/unit/test_evaluation_trade_entry_lineage.py`.

**Interfaces:**

- Consumes: canonical `LearningFeatureVector`/builder from Task7C.1; `ReplayObservation`, `DecisionCycleResult`, `StrategyCandidate.candidate_id`.
- Produces: immutable `ReplayDecisionOpportunity` and `RuntimePaperReplayRun.opportunities`; closed `EvaluationTrade` may include `entry_opportunity_event_id`/`entry_candidate_id`.

- [x] **Bước 1: Viết unit test đỏ** cho một row mỗi nến nguồn, input not-ready thành `INPUT_EXCLUDED`, strategy `NoTrade` thành `NO_CANDIDATE`, config/pipeline stop thành `NOT_RUN`, candidate bị risk reject vẫn có candidate ID, và outcome đóng ở bar sau giữ entry event/candidate ban đầu. Thêm test `BacktestRunner` gọi hook evaluator tùy chọn với observation + reason `INPUT_NOT_READY` trước khi bỏ qua; evaluator generic không có hook vẫn được bỏ qua như trước.
- [x] **Bước 2: Chạy test để xác nhận đỏ:** `PYTHONPATH=. uv run pytest tests/unit/test_backtest_decision_opportunities.py tests/unit/test_runtime_replay_evaluator.py tests/unit/test_backtest_dataset_replay_context.py tests/unit/test_backtest_runner.py tests/unit/test_evaluation_trade_entry_lineage.py -q --tb=short`; kỳ vọng thiếu contract/trace assertions, chưa có migration.
- [x] **Bước 3: Thêm contract** `ReplayDecisionOpportunity` canonical và trường `strategy_invoked` (hoặc tín hiệu tương đương đã test) vào `DecisionCycleResult`, để phân biệt `NoTrade` do strategy với chặn trước strategy.
- [x] **Bước 4: Tạo trace input trước readiness filter**: `BacktestRunner` gọi hook evaluator `record_input_excluded(observation, reason_code)` nếu evaluator hỗ trợ, rồi giữ nguyên hành vi bỏ qua evaluator cho `INPUT_NOT_READY`; `RuntimeReplayEvaluator` ghi opportunity `INPUT_EXCLUDED` với reason code ổn định. Generic evaluator không có hook không đổi hành vi. Với input hợp lệ, `BacktestDatasetReplayContextProvider` tạo trace và dựng vector bằng builder canonical đúng `DecisionSnapshot` được cycle sử dụng.
- [x] **Bước 5: Ghi decision state sau `prepare_published`** trong `RuntimeReplayEvaluator`; candidate ID tồn tại cả khi planner/risk reject. Lưu entry opportunity/candidate theo intent và gắn đúng fields vào `EvaluationTrade` khi position đóng.
- [x] **Bước 6: Chạy focused unit** bằng lệnh ở Bước 2; kỳ vọng các state và stable IDs khớp, old PnL/outcome behavior không đổi, hook không làm đổi evaluator generic.

## Task 2: Append-only ledger, outcome FK và role boundary

**Trạng thái 09/11/2026:** scoped review độc lập **Accepted (0 Critical/Important/Minor)**. Final focused + related **228 passed/12.92s**, PostgreSQL Compose schema/ACL **1 passed/27.59s**, Ruff check/format **11 files**, mypy **8 source files**, compileall/Alembic single-head/diff-check đạt. Task2 hoàn tất; Task3 có thể bắt đầu.

**Files:**

- Tạo migration `migrations/versions/b2d4f6a8c0e1_wp06_decision_opportunity_universe.py` sau khi kiểm tra current Alembic head.
- Tạo `src/trading_bot/adapters/database/models/backtest_opportunity_entities.py`; sửa `models/backtest_outcome_entities.py`, `models/candidate_universe_entities.py`, `models/__init__.py`.
- Sửa `src/trading_bot/adapters/database/repositories/backtest_outcomes.py`.
- Test: `tests/unit/test_backtest_opportunity_persistence.py`, `tests/unit/test_backtest_outcomes_repository.py`, `tests/unit/test_database_models.py`.

**Interfaces:**

- Consumes: `RuntimePaperReplayRun.opportunities` và outcomes có entry-opportunity linkage từ Task1.
- Produces: `BacktestDecisionOpportunityRecord`, atomic opportunity/outcome persistence, immutable v2 proof columns/version constraints; replay writer là producer duy nhất.

- [x] **Bước 1: Viết unit/repository tests đỏ** cho atomic append opportunity+outcomes, idempotent retry cùng canonical bytes, cùng event ID khác payload phải conflict, outcome FK phải chỉ về candidate entry cùng workspace/trial.
- [x] **Bước 2: Chạy tests đỏ** với `PYTHONPATH=. uv run pytest tests/unit/test_backtest_opportunity_persistence.py tests/unit/test_backtest_outcomes_repository.py -q --tb=short`.
- [x] **Bước 3: Thêm model/migration** cho opportunity table: composite workspace/run/trial/scope FKs, unique event ID + ordinal, checks cho state/candidate/reason/hash, canonical bytes, RLS + FORCE, immutable trigger, index theo workspace/trial/ordinal. Cấm ghi scenario không phải BASE trong query version này.
- [x] **Bước 4: Cấp quyền tối thiểu**: replay writer chỉ SELECT/INSERT metadata cần retry và INSERT rows; candidate builder SELECT đúng metadata opportunity/outcome/proof; trainer/admin/runtime không có feature/source payload mới. Revoke PUBLIC, không cấp role inheritance mới.
- [x] **Bước 5: Thêm outcome entry link** nullable cho legacy; FK/check/trigger yêu cầu link khớp candidate, entry snapshot, schema/vector hash khi link được cung cấp. Mọi outcome thuộc v2 trial phải truy vết được; không gán link theo bar đóng.
- [x] **Bước 6: Nâng proof v2 có tương thích**: expand `query_version` allowlist, thêm count/hash/completeness columns; DB checks phân biệt v1 fields null và v2 required/equations; giữ nguyên v1 CHECK/canonical semantics trên row legacy.
- [x] **Bước 7: Chạy focused unit** và `PYTHONPATH=. uv run pytest tests/unit/test_database_models.py -q --tb=short`; kỳ vọng migration/model contracts đồng nhất và outcome legacy vẫn có thể đọc.

**Refinement đã được controller đồng ý:** metadata schema opportunity v2 thêm ordered `source_event_digests` để kiểm key agreement/recompute nguồn; snapshot IDs/digest pairs chỉ là hash metadata. Sau review, schema được nâng lên v3 với `feature_values_hash` tính từ snapshot/time/schema và exact feature values, lưu bất biến trên opportunity; outcome không thể thay feature values rồi tự tính lại hash mà vẫn giữ vector link cũ. Entry vector hash được chụp từ exact opportunity, giữ trong EvaluationTrade/intent provenance và canonical outcome/SQL column; links absent giữ nguyên legacy bytes. DB trigger dựng canonical JSON chính xác, phân biệt cả whitespace và số nguyên với số float theo contract Python. Execution port forward opportunities trong cùng UoW là wiring cần thiết của persistence. Single migration không tạo role/inheritance hoặc payload grant mới; source/proof scanner/CLI/races ngoài scope.

## Task 3: Proof builder v2 và TRAINING rights recheck

**Trạng thái 09/11/2026:** source/unit/static đã qua independent scoped review Accepted (0 Critical/Important/Minor) sau fix1; PostgreSQL Compose Task4 đã qua independent review Accepted (0 Critical/Important/Minor), final1 passed/29.85s; Opportunity Task2 đã được scoped review Accepted (0 Critical/Important/Minor) với unit 228 passed và PostgreSQL Compose 1 passed.

**Files:**

- Sửa `src/trading_bot/application/candidate_universe_proof.py`, `src/trading_bot/adapters/database/models/candidate_universe_entities.py`, `src/trading_bot/adapters/database/repositories/candidate_universe_proofs.py`, `src/trading_bot/cli/candidate_proof.py`.
- Bổ sung identity-only helper trong `src/trading_bot/application/decision_snapshot_builder.py` và `src/trading_bot/application/backtest_dataset_manifest.py`; dùng chung public metadata-only decision-event helper trong `src/trading_bot/application/backtest_dataset_replay_observations.py`; tạo migration sau `b2d4f6a8c0e1` cấp column-level SELECT tối thiểu trên `market_bar` cho `crypidian_candidate_builder`.
- Test: `tests/unit/test_candidate_universe_proof.py`, `tests/unit/test_replay_training_rights.py`, `tests/unit/test_replay_writer_migration.py`; Task4 Compose E2E phải kiểm privilege metadata columns, từ chối OHLCV và workspace RLS.

**Interfaces:**

- Consumes: v2 opportunity/outcome rows, replay run/job `TRAINING_BASE` lineage, BASE report/scenario, bundle + v4 child manifests, `DatabaseReplayTrainingRightsGate`.
- Produces: `CandidateDecisionOpportunityProof` và `CandidateDecisionOpportunityProofResult`; method `CandidateUniverseProofService.build_decision_opportunity(workspace_id, trial_record_id)`; proof receipt mỗi child qua `candidate_training_rights`.

- [x] **Bước 1: Viết unit tests đỏ** cho scope mismatch, không BASE, training intent không predeclared, child manifest thiếu pinned identities, expected bar count lệch ledger, event/ordinal duplicate/missing, candidate/no-candidate state mismatch, outcome orphan/entry snapshot mismatch và retry/conflict.
- [x] **Bước 2: Chạy proof/rights unit tests**; kỳ vọng v2 query version chưa được hỗ trợ, v1 builder vẫn xanh và chưa đổi semantics.
- [x] **Bước 3: Tách scanner v1/v2**: `build()`/CLI default giữ closed-trade v1; method v2 dùng version cố định `base-decision-opportunity-universe-v1` và chỉ nhận workspace/trial IDs. Không nhận count, exclusions, feature hashes hoặc candidate list từ caller.
- [x] **Bước 4: Đối chiếu scope BASE** với `TRAINING_BASE` job request canonical, đúng run/trial/manifest/report/BASE scenario, `bundle_hash`, strategy ID/hash, symbol, decision timeframe, evaluation window và feature schema. Dựng expected event IDs/ordinal từ ordered, identity-pinned source bars trong bundle child; không tính expected universe từ số row opportunity đã ghi. Proof này chỉ chứng minh BASE scope; tuyệt đối không gắn fold/TEST membership hoặc xem là proof TEST.
- [x] **Bước 5: Recheck rights dưới đúng thứ tự lock**: source advisory lock → current TRAINING rights cho mọi child → lock parent trial → scan → verify lease trước commit/return. Ghi receipt append-only theo child vào proof ID; BACKTEST_RESEARCH run receipt không được coi là TRAINING receipt.
- [x] **Bước 6: Dựng digest/counts** từ row set DB đã đọc lại; đối chiếu chính xác tập event ID và ordinal với expected inventory từ manifest-pinned source bars (không chỉ so count), mọi event/ordinal duy nhất và link outcomes chính xác. `INPUT_EXCLUDED`/source gaps/missing expected event làm `completeness_verified=false`; không bịa no-candidate. Candidate không closed outcome được đếm là unlabeled, không làm giả label.
- [x] **Bước 7: Thêm CLI selector enum** `--universe closed-trades-v1|decision-opportunities-v1`, mặc định `closed-trades-v1`; không nhận arbitrary query string hoặc credential/count. Chỉ in proof metadata.
- [x] **Bước 8: Chạy focused unit**; xác nhận v1 bytes/IDs/retry không đổi, v2 fail-closed đúng các trường hợp đỏ.

**Ruling 09/11/2026 sau preflight Task3:** manifest v4 hiện chỉ lưu aggregate `bar_identity_hash`; candidate-builder không có đường authoritative để dựng expected event inventory, còn replay-worker có quyền full SELECT market bars. Không dùng quyền full của worker và không tạo OHLCV giả để băm. Task3 được phép thêm migration column-level SELECT cho candidate-builder trên đúng `market_bar` identity fields (`workspace_id`, record ID, symbol/timeframe/source, logical key/revision/supersedes/checksum, `raw_reference_id`, open/close time, availability, closed flag), giữ workspace RLS hiện hành; cấm SELECT giá/volume/raw content và không cấp quyền cho role khác. Thêm hàm canonical identity-only để tính lại child `bar_identity_hash`/event IDs từ metadata authoritative; không lặp serializer hoặc tin `opportunity_count`. Kiểm tra quyền/denial/RLS thật trong Task4 PostgreSQL Compose. Task3 proof chỉ là BASE; fold/test membership chưa tồn tại trên replay job request và thuộc proof TEST riêng của Task7C.3. Không đổi học/training intent hoặc khẳng định BASE proof đủ cho PAPER gate.

**Bằng chứng unit/static Task3:** RED22fail/67pass/15.61s do thiếu scanner v2/CLI; helper+grant RED2/0.53s; scenario-key RED1/1.42s rồi sửa đối chiếu BASE authoritative. Final related13 modules **277 passed/36.06s**, Ruff/format9, mypy6, compile/head/diff-check đạt; sole head `c4e6a8b0d2f3`. Các checkbox trên chỉ ghi implementation và unit/static, không đóng gate Task3 hoặc WP06. Task4 cần PostgreSQL thật cho migration/cột SELECT/OHLCV denial/RLS/seal/races; Task5 cần cập nhật các E2E head assertions cũ. Không chạy E2E hoặc stage/commit trong Task3. Chi tiết command/output/baseline caveat ở `task-3-implementer-report.md` trong SDD workspace.

**Review fix1 — 09/11/2026:** finding Important: source gap của bất kỳ child làm scanner cũ bỏ qua unknown event/ordinal, còn expected IDs co theo source rows còn đọc được. Đã RED riêng event ngoài grid (**1fail/1.28s**) và wrong ordinal khi child4h gap (**1fail/1.14s**), rồi sửa expected event/ordinal từ full immutable decision-child time grid, luôn reject unknown event/sai ordinal. Thiếu source ở đúng timestamp chỉ cho phép grid-bound diagnostic incomplete, không xác minh source times/digests tự khai; guard evaluation count/start alignment và missing-source giữa/cuối grid có unit. Final related13 **277 passed/36.06s**, Ruff/format9, mypy6, compile/head/diff-check đạt. Independent scoped re-review **Accepted (0 Critical/Important/Minor)**; Task3 source review đã đóng. Task4 PostgreSQL ACL/RLS/migration/seal/race E2E vẫn bắt buộc trước khi chấp nhận database boundary hoặc WP06.

## Task 4: Seal fence, race behavior và PostgreSQL Compose E2E

**Trạng thái 09/11/2026:** Task4 đã qua independent scoped re-review **Accepted (0 Critical/Important/Minor)**. Final PostgreSQL Compose E2E **1 passed/29.85s**, sau follow-up append-only DELETE coverage; helper **7 passed/0.17s**, static/Alembic đạt. Fence migration Task2 hiện tại đã bảo vệ opportunity/outcome cùng trial lock và proof seal nên Task4 không cần sửa migration; test/probe là hai file mới.

**Files:**

- Kiểm tra fence trong migration hiện hành; migration Task2 `b2d4f6a8c0e1_wp06_decision_opportunity_universe.py` giữ bất biến.
- Tạo `tests/e2e/probes/wp06_candidate_opportunity_universe.py` và `tests/e2e/test_wp06_candidate_opportunity_universe.py`.

**Interfaces:**

- Consumes: replay writer append, proof builder v2, current TRAINING receipt, migration/roles từ Tasks 1–3.
- Produces: DB-level evidence rằng đủ universe mới complete; partial/malformed/unlicensed/concurrent universe luôn fail closed.

- [x] **Bước 1: Viết Compose probe đỏ** với bundle schema-v4 nhỏ, nhiều decision bars và strategy fixture deterministic cho: no-candidate, risk-rejected candidate, candidate đóng trade ở event sau, candidate còn mở/không có closed outcome, cùng một input-excluded case.
- [x] **Bước 2: Chạy test để xác nhận đỏ:** `PYTHONPATH=. uv run pytest tests/e2e/test_wp06_candidate_opportunity_universe.py -q -s --tb=short`; khi chưa có migration/probe assertions mới phải fail rõ, không skip.
- [x] **Bước 3: Mở rộng cùng trial fence** cho opportunity insert và outcome-link insert: mọi writer khóa cùng `backtest_trial FOR UPDATE`, check proof v1/v2 seal trong transaction. Proof builder giữ đúng source-lock → trial-lock order. Nếu v1 proof đã seal outcome thì không được ghi thêm; nếu v2 proof đã seal universe thì không được append/đổi opportunity/candidate/link.
- [x] **Bước 4: Kiểm test positive**: exact expected decision-bar count = opportunity count; stable candidate IDs/hash; no-entry chỉ coverage; risk-rejected vẫn candidate; close event link về entry opportunity cũ; candidate no outcome là unlabeled; canonical proof/counts/membership digest ổn định.
- [x] **Bước 5: Kiểm test negative**: missing/extra/duplicate ordinal/event, altered source identity/revision/timeframe, vector/schema hash mismatch, wrong strategy/bundle/run/BASE, no-candidate có candidate ID, candidate row thiếu, outcome link sai entry/tenant/candidate, post-seal insert, stale/revoked TRAINING rights, direct payload SELECT, cross-workspace access, wrong role insert và populated downgrade phải bị từ chối.
- [x] **Bước 6: Kiểm race hai thứ tự** bằng backend PID + `pg_blocking_pids`: writer giữ trial lock trước proof thì proof chờ và scan đầy đủ; proof commit trước thì writer bị seal từ chối. Không dùng sleep làm bằng chứng lock.
- [x] **Bước 7: Kiểm migration lifecycle**: upgrade/check/head; downgrade khi bảng rỗng; populated downgrade guard từ chối; upgrade lại/check. Chỉ probe test tự quản lý DB Compose mới chạy migration; không chạy tay ngoài test.
- [x] **Bước 8: Chạy Compose E2E** bằng lệnh ở Bước 2; kỳ vọng một test pass, không skip, `alembic check` sạch và exact response assertions.

## Task 5: Regression và traceability

**Kết quả ghi nhận 12/11/2026:** full-unit regression **3558 passed/106.27s**, trong đó sáu module Task5 chạy trực tiếp **210 passed/36.89s, 0 skipped**. Đồng bộ 14 current-head assertions trên 8 E2E files và 2 probes từ `b2e4a6c8d0f2` lên `c3f5a7b9d1e3`; suite 10 E2E files → **11 passed/587.26s, 0 skipped**. Compose bổ sung chạy populated legacy drift, Task9 integrated và paper-boundary, đều pass; Compose ENV override đủ 14 policy values cũng pass. Ruff toàn bộ `src`/`tests`, format **767 files**, mypy hai source modules, Alembic sole head `c3f5a7b9d1e3`, DB-backed `alembic check` và diff-check đạt. BASE proof vẫn chỉ là BASE, không đủ điều kiện TEST/PAPER_GATE.

- [x] **Bước 1: Unit regression**: lệnh sáu module Task5 chạy trực tiếp → **210 passed/36.89s**, không skip; full unit đồng thời đạt **3558 passed/106.27s**.
- [x] **Bước 2: Compose regression**: `tests/e2e/test_wp06_normalized_fold_samples.py`, `test_wp06_opportunity_persistence_schema.py`, `test_wp06_model_registry.py`, `test_wp06_scorer_deployment.py`, `test_wp06_candidate_universe_proof.py`, `test_wp06_candidate_opportunity_universe.py`, `test_learning_fold_outcome_verifier.py`, `test_wp06_split_approval.py`, `test_wp06_source_pinned_replay_persistence.py`, `test_backtest_outcomes_persistence.py` → **11 passed/587.26s**, không skip. Drift/legacy migration + Task9 integrated → **3 passed/135.72s**; paper boundary → **1 passed/82.58s**.
- [x] **Bước 3: Static/DB checks**: `ruff check src tests` đạt; `ruff format --check src tests` → **767 files already formatted**; mypy hai source modules đạt; `uv run alembic heads` chỉ `c3f5a7b9d1e3 (head)`; Compose E2E chạy DB-backed `alembic check`; `git diff --check` đạt.
- [x] **Bước 4: Cập nhật memory và docs** ghi chính xác regression/output và giới hạn còn mở; memory follow-up đã được đọc lại và đối chiếu test/diff. BASE-only/PAPER_GATE no-go được giữ nguyên; không tuyên bố WP06/Task7C hoàn tất chỉ vì proof coverage qua.

## Điều kiện nghiệm thu và no-go gates

- Pass của plan này chỉ chứng minh được persisted decision-opportunity coverage trong declared replay scope. Nó chưa chứng minh sample/outcome support đủ đại diện, calibration, schema parity với runtime scorer (ngoài dependency Task7C.1), OOS score report, false-veto/retention policy, deployment MFA/CAS hoặc Task9 security review.
- BASE opportunity proof của plan này là bằng chứng cho đúng BASE scope; nó không đủ điều kiện cho scorer report hoặc `PAPER_GATE` trên TEST. Scorer gate sau này phải có proof version riêng hoặc chứng cứ tương đương bao phủ chính xác expected TEST opportunity universe và fold/test membership đã predeclare, với identity-pinned source bars, rights hiện hành và report cùng scope.
- `candidate_completeness_verified` chỉ được tạo từ v2 proof exact scope. Missing/extra decision bars, bất kỳ `INPUT_EXCLUDED`, source/strategy/snapshot mismatch, unlinked candidate/outcome hoặc query scope chọn sau khi mở test giữ cờ false.
- Outcome no-entry hoặc candidate chưa đóng không được biến thành class `0`, net PnL `0`, trade row hay label giả. Report phải nêu riêng candidate unlabeled/no-entry counts.
- Artifact v1 tiếp tục `calibrated=false`, `scorer_state=abstain`; scoring runtime giữ `ABSTAIN`. Không nối registry pointer vào runtime scorer trong slice này.
- `PAPER_GATE` tiếp tục đóng; proof v2 một mình không được đổi deployment mode, không được xem `candidate_completeness_verified=true` là đủ approval, không tạo order/reservation/PAPER intent/LIVE capability.
- Chỉ đánh giá gate khi sau này đạt đủ scorer v2 canonical vector parity, calibration, sealed-test/OOS report, current rights, pre-registered policy metrics, exact proof/report scope và approval deployment riêng theo thiết kế Task7.
