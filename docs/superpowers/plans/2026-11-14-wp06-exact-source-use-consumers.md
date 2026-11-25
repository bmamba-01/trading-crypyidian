# WP06 Exact Source-use Consumers Implementation Plan

**Đối soát 24/11/2026:** PostgreSQL Compose hiện đã chạy được. Replay TRAINING kiểm deny từng chiều `symbols`, `intervals`, `data_start`, `data_end`, `purpose`, `outputs`, `derived_use`, `retention`; WP05 registry kiểm mismatch `source_id/provider/product/venue/contract` và chặn sửa identity bất biến. Shadow/Drift QA-only E2E xác nhận cohort có lineage QA + decision thiếu source-bar link vẫn giữ đủ member, `INCONCLUSIVE`, không metrics/proposal và retry idempotent (**1 passed/19s**; Drift/source-scope unit **40 passed/1.01s**). PAPER source-scope E2E giữ source advisory lock, xác nhận aggregate gate chờ khóa rồi từ chối QA và không ghi report (**1 passed/20.84s**; performance/source-scope unit **104 passed/0.99s**). Đây là QA denial/lock-order proof, không phải mixed eligible/denied, positive rights hoặc revoke/shrink race với entitlement hợp lệ. Lượt Docker disconnect trước probe không tính pass. Matrix consumer-specific từng chiều scorer/PAPER performance/Shadow/Drift, rights race và PAPER non-QA vẫn mở.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Buộc mọi scorer TEST/PAPER, Shadow và Drift kiểm scope quyền hiện hành trên đúng dữ liệu MarketBar đã lưu trước khi đọc, chấm hoặc ghi kết quả.

**Architecture:** Dựng lineage nến từ `BotDecision` và `FeatureSnapshot` qua ledger bất biến `bot_decision_source_bar`; không nhận source, bar ID hay scope JSON từ caller. SECURITY DEFINER gate đọc lineage authoritative, policy/evidence đang hiệu lực và `SourceLicenseScope`, dựng scope cho operation cố định, kiểm mọi chiều quyền và trả fingerprint metadata-only. Runtime kiểm trước inference và trong transaction ghi decision/event; Shadow/Drift kiểm từng member dưới source lock và persist digest của tập quyền cùng cohort.

**Tech Stack:** Python 3.14, SQLAlchemy async, PostgreSQL/Alembic, FastAPI, pytest, Docker Compose, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-13-wp06-source-use-scope-consumers-design.md`

## Global Constraints

- Mọi consumer dựng scope từ persisted lineage; caller không được gửi JSON scope hoặc quyền.
- Source/provider, symbol, interval, range, bar UUID/revision và event identity phải khớp chính xác; thiếu chiều nào thì deny hoặc `INCONCLUSIVE`.
- TRAINING evaluation dùng `training` + `aggregate_report` + `aggregate_reporting`; PAPER/SHADOW inference dùng `paper_demo` + `paper_inference` + `paper_inference`; Drift aggregate dùng `paper_demo` + `aggregate_report` + `aggregate_reporting`.
- Quyền được kiểm lại dưới source advisory lock sát điểm đọc/ghi; receipt lịch sử không thay current recheck.
- Fixture first-party QA không được dùng để eligible deployment; `SHADOW`/`PAPER_GATE` giữ khóa.
- Không trả feature, label, probabilities, trade rows, raw evidence hoặc giấy phép qua rights gateway.
- Migration lịch sử bất biến; migration mới phải giữ sole Alembic head và populated downgrade guard.
- Hướng dẫn, comment, docstring và UI tiếng Việt; identifier/API tiếng Anh.
- Cập nhật implementation memory, WBS/module và glossary nếu phát sinh thuật ngữ mới; đọc lại memory sau khi ghi.

## Review Focus

- BotDecision có nhiều snapshot/timeframe: ledger phải giữ toàn bộ nến theo thứ tự ổn định và không bỏ baseline decision chưa có scorer event; kiểm bằng Compose test cho snapshot nhiều timeframe và scorer/baseline.
- Event ID khớp nhưng MarketBar revision/checksum bị đổi hoặc bar không còn current tại decision time: rights gate phải từ chối; kiểm adversarial old revision, duplicate/missing bar và forged event linkage.
- Policy/evidence bị thu hẹp giữa preflight và atomic write: transaction phải recheck dưới source lock và không ghi BotDecision/score event; kiểm race Compose bằng hai transaction.
- Một member trong Shadow/Drift hoặc PAPER performance cohort thiếu scope, lineage hay vượt retention/range: không ghi rights-eligible result; Drift phải `INCONCLUSIVE`, còn performance gateway từ chối tạo/trả report thay vì loại lặng lẽ member; kiểm mixed-member cohort và decision không có outcome trưởng thành.
- QA, legacy license thiếu scope hash, unknown provider, exact `range_end`, và sai output/derived-use: fail-closed; không mở mode.

## Files and Interfaces

- `src/trading_bot/domain/research/license_scope.py`: canonical serializer/hash cho `SourceUseScope` để đối chiếu Python/SQL.
- `src/trading_bot/adapters/database/models/learning_source_bar_entities.py`: ORM cho lineage nến bất biến theo BotDecision.
- `src/trading_bot/adapters/database/repositories/bot_decision_source_bars.py`: append exact persisted lineage trong transaction tạo decision.
- `migrations/versions/<new>_wp06_decision_source_bars.py`: table/grants/RLS/trigger và current source-use gate cho PAPER/aggregate operations.
- `migrations/versions/<new>_wp06_scorer_runtime_source_scope.py`: preflight + atomic recheck khi scorer runtime consume decision-specific bars.
- `migrations/versions/<new>_wp06_paper_performance_source_scope.py`: kiểm lại quyền hiện hành cho từng PAPER decision và cohort/report aggregate.
- `migrations/versions/<new>_wp06_shadow_drift_source_scope.py`: per-decision Shadow gate, per-member Drift recheck và immutable scope digest, dùng chung exact decision gate.
- `src/trading_bot/adapters/database/repositories/learning_scorer_runtime_targets.py`, `.../learning_scorer_runtime_committer.py`: nối typed bar lineage/recheck, fail-closed.
- `src/trading_bot/adapters/database/repositories/learning_shadow.py`, `learning_drift.py`, `learning_paper_performance.py` và ứng dụng tương ứng: exact member authorization và assessment digest.
- `tests/unit/test_source_license_scope.py`, `tests/unit/test_learning_shadow.py`, `tests/unit/test_learning_drift.py`, `tests/unit/test_learning_scorer_runtime_targets.py`, `tests/unit/test_learning_scorer_runtime_committer.py`: contract/caller behavior.
- `tests/e2e/probes/wp06_paper_performance_source_scope.py`, `tests/e2e/test_wp06_paper_performance_source_scope.py`, `tests/e2e/probes/wp06_shadow_drift_source_scope.py`, `tests/e2e/test_wp06_shadow_drift_source_scope.py`: PostgreSQL ACL/RLS/identity/race/downgrade boundaries.
- `docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md`, `docs/superpowers/plans/2026-11-12-wp06-open-gaps.md`, `docs/implementation/06-ai-learning.md`, `docs/research/glossary-vi.md`, `docs/implementation/implementation-memory.md`: traceability.

## Tasks

### Task 1: Canonical use-scope fingerprint

**Deliverable:** Python can serialize/hash `SourceUseScope` identically to PostgreSQL and rejects malformed dimensions before a gate call.

- [x] Add unit tests for canonical UTC microsecond timestamps, stable sorted dimensions and `scope_sha256` parity fixtures.
- [x] Run the tests and confirm they fail because only license scopes currently have canonical helpers.
- [x] Add `canonical_use_scope_json()` and `use_scope_sha256()` with strict `SourceUseScope` type checking.
- [x] Run `uv run pytest tests/unit/test_source_license_scope.py -q --tb=short` and pass all cases.

### Task 2: Persisted decision-to-MarketBar lineage

**Deliverable:** every BotDecision records all exact MarketBar/event/revision identities used by its feature snapshot, independent of whether a scorer event exists.

- [x] Write unit tests for typed lineage rows, ordering, duplicate rejection and baseline decisions.
- [x] Add an append-only workspace-scoped table with composite FKs and a trigger that proves bars belong to the decision's evidence/snapshot and current revision at decision time.
- [x] Wire the existing decision writer to append lineage atomically from persisted snapshot metadata, not caller-provided bar IDs.
- [x] Add Compose probes for multi-timeframe completeness, cross-workspace denial, event/bar mismatch, old revision, append-only, role grants and populated downgrade guard; unit tests assert rollback when scorer lineage fails.
- [x] Run focused unit tests, `uv run pytest tests/e2e/test_wp07_market_signal_producer.py -q --tb=short`, Ruff, mypy, and Compose-backed Alembic upgrade/check/downgrade guards.

### Task 3: Exact PAPER inference scope gate and scorer runtime

**Deliverable:** scorer runtime can use a target only when every persisted decision bar is covered by current `PAPER_DEMO` license scope, both before inference and inside the atomic BotDecision/score-event transaction.

- [x] Unit và runtime regression xác nhận lineage thiếu/không rõ làm abstain trước khi đọc model; quyền source-bar/committer bị từ chối thì rollback decision và score-event.
- [x] Thêm gate `SECURITY DEFINER` tự dựng `SourceUseScope` có kiểu từ các bar đã persist và kiểm tra policy, evidence digest, license-scope hash hiện hành dưới source lock.
- [x] Nối preflight exact scope và atomic recheck vào runtime target resolver/committer; scope do server dựng.
- [x] Compose kiểm tra lineage nhiều timeframe đã persist, QA/current-rights denial, no-write, resolver và atomic source-bar. Exact coverage/hash được parity-test, không giả quyền non-QA.
- [x] Migration unit khóa predicate scorer cho identity, symbol, interval, date range, purpose, output, derived-use và retention. Đây là regression guard trên SQL migration; không thay PostgreSQL consumer mismatch E2E.
- [x] PostgreSQL Compose scorer kiểm riêng 13 chiều near-miss của license `SourceUseScope`: source ID/provider/product/venue/contract, symbol, interval, hai biên thời gian, purpose, output, derived-use và retention. Mỗi ca bị core gate từ chối và không ghi authorization/receipt/feature snapshot/decision/score-event; policy/evidence là dữ liệu tổng hợp trong DB disposable, được seed để chỉ kiểm nhánh âm tính.
- [x] PostgreSQL scorer gate denial matrix kiểm 13 chiều SourceUseScope; test chỉ dùng synthetic mismatch và không xác nhận positive authorization.
- [ ] Kiểm race thu hẹp/thu hồi giữa preflight với commit khi source rights hợp lệ; positive authorization cần quyền non-QA thật.
- [x] Chạy runtime units, resolver/current-rights và source-bar atomic E2E; D7 gateway chạy Alembic upgrade/check và downgrade/re-upgrade. Source-linked replay, scorer PAPER denial, source-scope, Shadow/Drift và report writer chạy lại cùng D7 head.

#### Follow-up QA-only PostgreSQL gate lock-order — 20/11/2026

- [x] TDD RED xác nhận Compose test không tìm thấy probe trước khi thêm; sau đó dùng PostgreSQL fixture riêng, không có policy/license evidence, gọi core `SECURITY DEFINER` source gate bằng owner role hiện có.
- [x] Giữ đúng source advisory lock ở transaction khác để chứng minh gate thực sự chờ khóa; sau khi nhả khóa, PostgreSQL trả đúng `WP06_SCORER_JOB_SOURCE_QA_DENIED`.
- [x] Xác nhận authorization, feature snapshot, BotDecision và score-event không phát sinh; E2E tự chạy Alembic upgrade/check và dọn Compose resources.
- [x] `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_job_source_scope_qa.py` → **1 passed/21.78s/0 skipped**; scorer source-rights/committer/targets/migration unit → **32 passed/0 skipped**; Ruff, format và diff-check đạt.
- **Giới hạn:** test gọi core gate bằng quyền owner trong DB Compose, không kiểm public wrapper/model artifact; chỉ chứng minh QA denial/no-write và lock-order. Ma trận các chiều scope cùng race thu hẹp/thu hồi sau preflight hợp lệ vẫn cần quyền PAPER non-QA thật.

#### Follow-up QA-only structural lineage mismatch — 20/11/2026

- [x] Compose fixture tạo PENDING job/evidence và source bar first-party QA; staged timeframe `1h` cố ý không khớp timeframe `4h` đã persist. Không seed source policy, license evidence hay rights receipt.
- [x] Giữ source advisory lock QA trong transaction khác, gọi core `SECURITY DEFINER` gate bằng owner role hiện có, xác nhận gate hoàn tất trước khi nhả lock và trả đúng `WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING` thay vì đi tới QA rights branch.
- [x] Xác nhận `pg_blocking_pids` không có blocker của source lock; authorization, rights receipt, feature snapshot, BotDecision và score-event không đổi. E2E tự chạy Alembic upgrade/check và dọn Compose/volume trong `finally`.
- [x] `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_job_source_scope_lineage_mismatch.py` → **1 passed/20.14s/0 skipped**; scorer source-rights/committer/targets/migration unit → **32 passed/1.11s/0 skipped**; Ruff check và format-check cho hai file đạt.
- **Giới hạn:** kiểm tra này chỉ xác nhận SQL từ chối khi timeframe persisted/staged lệch trước source lock; không kiểm policy license dimensions, quyền non-QA, positive eligibility hoặc race thu hẹp/thu hồi sau preflight.

#### Follow-up scorer SourceUseScope database near-miss — 25/11/2026

- Dùng nến/job/evidence thị trường tổng hợp và policy/license-evidence tổng hợp chỉ trong PostgreSQL Compose dùng một lần; không tải dữ liệu Binance, không có license thật, không gọi public authorization path, không bật scorer hay PAPER mode. Các fixture rights được seed để kiểm riêng predicate từ chối của core gate, không phải bằng chứng quyền sử dụng.
- Kiểm từng chiều `source_id`, `provider`, `product`, `venue`, `contract`, `symbols`, `intervals`, `data_start`, `data_end`, `purpose`, `outputs`, `derived_use`, `max_retention_days`; mọi trường khác giữ nguyên ở dạng canonical/hash nhất quán. Mỗi ca trả đúng `WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED`; authorization, rights receipt, feature snapshot, BotDecision và score-event không đổi.
- `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_job_source_scope_qa.py tests/e2e/test_wp06_scorer_source_scope_near_miss.py` → **2 passed/38.50s/0 skipped**. Unit scorer migration/source-license/TRAINING-scope → **77 passed/0.67s/0 skipped**; Ruff, format-check, py_compile, Alembic check trong Compose và cleanup đạt.
- **Còn mở:** quyền non-QA được xác minh, race hai thứ tự khóa khi quyền đang hợp lệ rồi bị thu hẹp/thu hồi, full policy-transition enforcement, scope/mixed-cohort E2E riêng cho PAPER aggregate/Shadow/Drift, TEST support 1.000 tổng/100 mỗi lớp và journey dương tính. CRY-334/WP06 giữ In Progress; `SHADOW`/`PAPER_GATE` khóa.

### Task 4: Kiểm scope chính xác cho aggregate hiệu năng PAPER

**Kết quả:** chẩn đoán hiện có kiểm quyền PAPER có kiểu cho từng decision chính xác và quyền aggregate-report cho toàn cohort trước khi trả hoặc lưu metric. Giữ attribution decision→intent→đúng một PAPER trade đã đóng cùng các ngưỡng đã duyệt; thiếu quyền không trả aggregate/report một phần. `INCONCLUSIVE` chỉ dành cho cohort đã qua kiểm quyền nhưng không đủ mẫu/support/coverage; thiếu quyền làm gateway từ chối.

- [x] Dùng quyền hiện hành theo exact decision-bar lineage và các operation cố định `paper_demo` + `aggregate_report` + `aggregate_reporting`; receipt inference chỉ là audit.
- [x] Aggregate gateway kiểm toàn bộ decision của cả hai cohort dưới source lock trước khi tính metric; thiếu quyền từ chối, không trả member data hoặc persist report.
- [x] PostgreSQL Compose kiểm riêng 13 chiều SourceUseScope mismatch qua fixed PAPER performance decision gate; mọi ca trả deny và không ghi report. Fixture policy/evidence tổng hợp chỉ kiểm nhánh âm tính, không tuyên bố entitlement hoặc aggregate dương tính.
- [x] Gắn membership + current-rights digest vào identity/idempotency và recheck trước khi trả idempotent/recent reports.
- [x] Unit/Compose kiểm exact decision→intent→closed trade attribution, QA/missing-target denial, metadata-only access, no-write denial, digest và current report recheck. Kiểm tra tầng ứng dụng xác nhận khi aggregate bị từ chối, luồng dừng trước khi đọc metadata snapshot, kiểm quyền report hiện hành, đọc payload hoặc ghi report. D7 writer tự chạy aggregate gateway, đối chiếu report/policy/metric/status/digest trước khi ghi; E2E kiểm ACL INSERT của bảng/cột, scope, downgrade và re-upgrade.
- [x] Xác minh và sửa truy vấn PostgreSQL của evaluator trong migration `f4b6d8e0a2c7`. Compose chạy projection baseline và phép tính log-loss từ hàm đã cài, kiểm đúng tập cột được cấp và xác nhận downgrade/re-upgrade. API trả HTTP 409 cho lỗi lineage và sai lệch với kết quả aggregate.
- [x] PostgreSQL fixed decision gate denial matrix kiểm 13 chiều SourceUseScope mismatch riêng cho aggregate consumer; test không tạo report.
- [ ] Hoàn tất policy thu hẹp/rights revoke, race khi cohort/read/write và retry sau policy change. Positive aggregate report E2E chờ quyền non-QA thật. Giữ ngưỡng 30 ngày, 1.000/100, coverage ≥80%, regression >0,01 và `NO_STATE_CHANGE`.
- [x] Không đổi projection/UI trong lát source-use này; Playwright responsive desktop/mobile report đã đạt ở vòng UI trước, screenshot hiện có được giữ.
- [x] QA-only PostgreSQL source-lock ordering: giữ `research-source:{workspace}:CRYPIDIAN_FIRST_PARTY_QA`, gọi aggregate source gate bằng admin principal, xác nhận `pg_blocking_pids` thấy gate chờ; sau khi nhả khóa gate từ chối QA và report count vẫn 0. `test_wp06_paper_performance_source_scope.py` **1 passed/20.84s/0 skipped**. Đây không chứng minh policy shrink/revoke sau một preflight hợp lệ.

#### Follow-up PAPER performance SourceUseScope database near-miss — 25/11/2026

- Tạo decision lineage/nến tổng hợp và workspace riêng cho từng dimension; policy/evidence tổng hợp có canonical scope/hash nhưng chỉ dùng để kiểm denial của `wp06_can_aggregate_paper_performance_decision_source`. Không tạo trade, scorer, current-rights receipt hay report; không có ca trả authorization thành công.
- Kiểm đủ 13 chiều `source_id`, `provider`, `product`, `venue`, `contract`, `symbols`, `intervals`, `data_start`, `data_end`, `purpose`, `outputs`, `derived_use`, `max_retention_days`; mỗi ca gate trả false và report count giữ 0.
- `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_paper_performance_scope_near_miss.py tests/e2e/test_wp06_paper_performance_source_scope.py` → **2 passed/39.73s/0 skipped**; performance/source-license/TRAINING-scope unit → **89 passed/0.73s/0 skipped**. Ruff, format-check, py_compile, Alembic check trong Compose và resource cleanup đạt.
- **Còn mở:** mixed eligible/denied cohort, policy change giữa preflight/read/write, rights race dưới entitlement hợp lệ, report dương tính có license non-QA, scope matrix riêng của Shadow/Drift và TEST support. CRY-334/WP06 tiếp tục In Progress; active modes giữ khóa.

### Task 5: Kiểm quyền chính xác cho từng member Shadow và Drift

**Kết quả:** observer dùng bộ kiểm source-use chính xác dùng chung cho baseline/challenger; Drift ghim digest scope hiện hành cho từng cohort member và thành `INCONCLUSIVE` khi bất kỳ member nào thiếu coverage.

- [x] Unit kiểm exact member lineage, thiếu scope/retention/range, source rights và hành vi baseline decision.
- [x] Dùng exact source-use gate kiểm từng Shadow decision trước khi append; giữ nguyên baseline decision và metadata-only/ABSTAIN.
- [x] Recheck bar của mọi Drift member dưới source lock và persist current-rights digest xác định cùng membership cohort.
- [x] Compose E2E kiểm QA denial, workspace/RLS/ACL, source-scope gate, Drift persistence/digest, admin metadata read và populated downgrade.
- [x] Trên head `a6c8e0b2d4f7`, observer chỉ gọi được cổng Drift hai tham số; cổng tự lấy source IDs, khóa nguồn theo thứ tự rồi mới đọc `clock_timestamp()` của PostgreSQL. Lời gọi cổng ba tham số với timestamp lịch sử và hai hàm PAPER nội bộ đều bị từ chối `42501`; app/runtime không có EXECUTE. Compose cũng xác nhận gate chờ khóa QA rồi trả `NULL`, không dùng fixture để suy ra quyền PAPER.
- [x] Mở rộng QA-only Compose E2E qua `LearningDriftApplication` với cohort có một member QA: assessment/member thật được ghi nhưng `INCONCLUSIVE`, `rights_unverified`, 0 eligible và không có proposal; retry trả duplicate. Fixture chỉ kiểm denial, không phải bằng chứng quyền.
- [x] Thêm Compose E2E gọi `LearningShadowApplication.observe_pending` trên lineage QA và challenger đã đăng ký: writer chỉ lưu observation `SOURCE_RIGHTS_INELIGIBLE` metadata-only, không có rights/source-scope fingerprint hoặc score payload; retry idempotent và các ledger decision/order/score không đổi. Cùng observer session xác nhận decision của writer có đúng một nguồn `CRYPIDIAN_FIRST_PARTY_QA`, exact-scope gate trả `None` trước/sau; domain canonical payload và tập cột ORM có allowlist đầy đủ. Fixture chỉ kiểm denial, không cấp quyền.
- [x] Sửa hash parity cho `source_rights_digest` khác NULL: migration `c6e8a0b2d4f9` dựng JSON theo thứ tự khóa của Python và giữ nguyên cách viết số mũ khi tính SHA-256. Integrity guard dùng quyền có sẵn trên hàm JSON và số thực. Nhánh NULL lịch sử giữ nguyên chuỗi byte.
- [x] Migration unit bảo vệ predicate scope của scorer và Shadow cho đủ identity/symbol/interval/range/purpose/output/derived-use/retention; Drift gọi lại rights gate PAPER performance. Các SQL assertions chỉ là regression guard, không thay E2E PostgreSQL từng consumer.
- [x] Repository thật trả `None` cho decision QA. Ứng dụng trả `None` khi cohort trống, không ghi assessment/member; downgrade bị chặn khi ledger có dữ liệu và downgrade/re-upgrade bảng trống khôi phục contract/ACL.
- [x] Sau sửa review, 149 unit liên quan và lô Compose source-scope, persistence, admin-read đạt trên head `c6e8a0b2d4f9` (4 E2E/133.88s/0 skipped). Các bước downgrade trực tiếp khi ledger có dữ liệu bị từ chối tại đúng revision. Re-review không còn finding chặn; retry xác nhận cùng assessment ID và chỉ một hàng theo đúng identity.
- [x] Full unit **6.076 passed**, regression chéo 8 file WP06 scorer/source-lineage/PAPER/rights/DB **11 passed/613.13s/0 skipped**, Ruff, format (1.408 files), mypy (544 source files), sole Alembic head `c6e8a0b2d4f9` và diff-check đạt.
- [x] Commit `06eee0a` được fast-forward push lên `origin/dev`; CRY-334 vẫn In Progress vì các rights race/non-QA journey chưa có entitlement thật.
- [x] QA-only PostgreSQL cohort retention: một decision lineage QA và một persisted observation có decision thiếu liên kết source-bar được lưu đủ thành viên; cả hai current-rights ineligible, member thiếu lineage có `current_source_ids=[]`, assessment `INCONCLUSIVE` không metrics/proposal và retry idempotent. `test_wp06_shadow_drift_source_scope.py` **1 passed/19.00s**, Drift/source-scope units **40 passed/1.01s**; fixture tạm xóa liên kết append-only chỉ trong DB Compose dùng một lần, không tạo source policy/license/rights.
- [x] PostgreSQL Shadow inference gate và Drift source gate kiểm riêng đủ 13 chiều SourceUseScope mismatch; mọi ca trả `NULL`, không ghi observation/assessment. Fixture tổng hợp chỉ kiểm denial và không thay bằng chứng entitlement.
- [x] Scorer, PAPER aggregate, Shadow và Drift hiện đều có DB denial matrix 13 chiều; batch regression **5 passed/95.37s/0 skipped**, migration/source-license/TRAINING scope unit **95 passed/0.86s/0 skipped**. Đây không kiểm positive eligibility hoặc policy lifecycle.
- [ ] Mixed-source/member, rights revoke/shrink race sau preflight hợp lệ, positive entitlement non-QA và TEST support vẫn mở. QA/synthetic fixture chỉ chứng minh denial; fixture registry Drift vẫn không xác nhận registry approval hoặc quyền non-QA. Fixture DB WP06F chỉ dành kiểm operator path.

### Task 6: WP06 traceability and regression audit

**Deliverable:** WBS accurately marks completed exact-scope consumers and names external evidence required for any positive active deployment.

- [x] Rà lại TRAINING normalization/claim và TEST report gates theo operation map; giữ operation `evaluation` tách biệt với aggregate/report scope. Source-linked replay kiểm parity hash D5; PAPER aggregate dùng đúng `aggregate_report`/`aggregate_reporting` cố định.
- [x] Cập nhật WP06 WBS, open gaps, module, memory và project-status; đọc lại memory, đối chiếu source diff/test output; glossary không phát sinh thuật ngữ mới.
- [x] D7 full unit **4077 passed/0 skipped**, Ruff, format-check **965 files**, mypy **379**, Compose config, sole Alembic head `e2f4a6c8b0d7`, source-linked/scorer/source-scope/Shadow/Drift/writer Compose batch **10 passed/500.80s/0 skipped**, downgrade/re-upgrade writer và diff-check đạt.
- [x] `git fetch origin dev` xác nhận nhánh hiện tại có chung base `5c360a7` với `origin/dev`, ahead 5/behind 0; không còn commit dev mới cần tích hợp. Chạy lại Compose current-head liên quan: Drift **4 passed** và regression chéo WP06 **11 passed/613.13s/0 skipped**. Không sửa projection/UI nên bằng chứng Playwright responsive hiện có vẫn áp dụng.

### Bổ sung bằng chứng replay execute→persist — 19/11/2026

- [x] E2E PostgreSQL thu hồi source rights sau khi replay executor hoàn tất nhưng trước transaction persist; xác nhận recheck từ chối và sáu bảng kết quả/receipt giữ nguyên số dòng. `tests/e2e/test_wp07_rest_backfill_persistence.py`: **3 passed / 0 skipped / 50,97s**; Ruff, format và `git diff --check` đạt.
- Phạm vi này chỉ đóng khoảng trống replay persistence của CRY-334; scorer lock-order, scope near-miss, performance cohort, Shadow/Drift mixed-member race và tích hợp regression vẫn mở. Kết quả chạy thuộc worktree đang mở với migration head `f6c8e0a2b4da`, chưa phải xác nhận regression trên `origin/dev` hiện tại.

### Task 7: Race membership cohort Drift chỉ dùng QA để kiểm rollback

**Phạm vi đã được người dùng chọn:** bổ sung một PostgreSQL E2E cho `LearningDriftApplication` khi tập member đổi trong lúc writer đang chờ khóa nguồn. Chỉ dùng lineage `CRYPIDIAN_FIRST_PARTY_QA` để chứng minh rollback/denial; không giả quyền PAPER non-QA, không tái dùng fixture WP06F, không khẳng định Shadow writer hay registry approval đã được kiểm chứng.

**Cách đồng bộ:** fixture transaction giữ khóa source rồi member-set, cố ý không giữ registry-entry lock để assessor có thể đọc cohort ban đầu rồi chờ khóa source. Việc thêm observation qua fixture là thay đổi metadata chỉ dùng trong bài test; nó kiểm tra defensive membership recheck, không mô phỏng hay xác nhận đường append của Shadow production.

**Tệp:**
- Sửa: `tests/e2e/test_wp06_shadow_drift_source_scope.py`
- Sửa: `tests/e2e/probes/wp06_shadow_drift_source_scope.py`
- Sau kiểm chứng, cập nhật: WBS, `docs/implementation/06-ai-learning.md`, `docs/implementation/project-status.md`, `docs/implementation/implementation-memory.md`.

- [x] Thêm kỳ vọng Compose cho thứ tự xác định: một QA observation đã commit; fixture transaction giữ khóa source rồi khóa member-set theo đúng thứ tự, seed observation QA thứ hai vào cùng registry/revision với `observed_at` trước khi assessor bắt đầu và commit sau khi `pg_blocking_pids` chứng minh assessor đang đợi khóa.
- [x] Gọi `LearningDriftApplication.assess_cohort` thật bằng login observer; xác nhận lần đầu ném `LearningDriftMembershipConflict` và rollback để lại 0 assessment/member/proposal.
- [x] Retry cohort ổn định qua application thật; xác nhận đủ 2 member, 0 eligible, `INCONCLUSIVE`, `rights_unverified`, không có metric khi rights chưa xác minh, không có proposal; lần retry idempotent trả cùng assessment ID và chỉ có một assessment cùng 2 member.
- [x] Chạy `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_shadow_drift_source_scope.py`, Ruff/format cho hai tệp E2E, và `git diff --check`; không skip.
- [x] Ghi đúng giới hạn: đây là denial-only membership rollback proof trên fixture QA, không đóng mixed eligible/denied cohort, current-rights revoke/shrink race hay positive active journey cần quyền PAPER non-QA thật.

Kiểm chứng trên nền `a976c5e29c26`, Alembic head `c6e8a0b2d4f9`. Unit Drift application/repository **20 passed / 0 skipped**, Compose E2E **1 passed / 0 skipped**, Ruff, format và diff-check đạt. CRY-334 giữ In Progress cho các ca chưa có bằng chứng. [Nhật ký kiểm chứng](../../implementation/implementation-memory.md#2026-11-20--cry-334-task-7-rollback-khi-tập-observation-drift-qa-thay-đổi).

### Task 8: Shadow observer writer QA-denial E2E

**Phạm vi:** PostgreSQL Compose chạy `LearningShadowApplication.observe_pending` bằng login observer với một decision có lineage nguồn first-party QA và challenger đã đăng ký. Không cấp quyền dữ liệu, không bật scorer, không dùng fixture WP06F và không thay đổi production/schema.

- [x] Seed đúng các hàng cần thiết; Shadow fixture không tạo trade PAPER ngoài mục tiêu. Helper chia sẻ giữ mặc định cũ cho caller PAPER hiện hữu.
- [x] Gọi writer thật; kiểm observation `SOURCE_RIGHTS_INELIGIBLE`, quyền false, các receipt/fingerprint NULL, canonical/domain và schema ORM không chứa payload score/feature/probability ngoài allowlist; retry không thêm observation.
- [x] Kiểm các ledger decision/order/fill/position/trade/score/scorer không đổi và scorer deployment vẫn vắng mặt hoặc `DISABLED`.
- [x] Chạy Shadow Compose E2E **1 passed/17.15s/0 skipped**, PAPER fixture tương thích **1 passed/19.37s/0 skipped**, Shadow unit **17 passed/0.84s/0 skipped**, Ruff, format và diff-check đạt.
- [x] Re-review độc lập: tuân thủ đặc tả và chất lượng mã **APPROVED**, không còn finding.

**Đối chiếu câu duyệt mới ngày 20/11:** race Drift membership QA-only khi writer chờ source lock đã được triển khai ở Task 7 và kiểm lại trong cùng Compose E2E hiện hành; không thêm test trùng lặp. Phép thử rollback 0 ledger rồi retry đủ hai member `INCONCLUSIVE`, không metrics/proposal, chỉ chứng minh denial bằng QA.

### Follow-up QA-only scorer lineage mismatch theo symbol — 20/11/2026

- Probe giữ ca timeframe mismatch QA và thêm mismatch `MarketBar.symbol=ETHUSDT` trong khi evidence/staged vẫn là `BTCUSDT`. Cả hai yêu cầu exact `WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING` trước source lock, không ghi rights receipt/authorization/snapshot/decision/score-event và không seed policy/license.
- E2E **chưa xác minh**: `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_job_source_scope_lineage_mismatch.py` mắc tại `migrator alembic check` quá timeout subprocess 360 giây; không có stdout/stderr cuối để xác nhận assertion hoặc cleanup inventory. Docker API ping chỉ đọc cũng timeout 3 giây/0 bytes. Không chạy lại khi daemon không phản hồi.
- `uv run python -m py_compile` hai tệp probe/wrapper, Ruff check/format-check và `git diff --check` đạt. Không đánh dấu test hoặc Task 3 hoàn thành.
- Ma trận thuần miền `SourceLicenseScope.covers` hiện có được chạy lại: `uv run pytest -q -rs --tb=short tests/unit/test_source_license_scope.py` → **68 passed/0 skipped**. `uv run pytest --collect-only -q tests/e2e/test_wp06_scorer_job_source_scope_lineage_mismatch.py` → **1 test collected**; đây chỉ là collection, không chứng minh Compose E2E.
- Ca này chỉ thêm structural-lineage QA denial; không hoàn tất license-scope mismatch matrix, rights shrink/revoke giữa preflight/commit hay positive non-QA authorization. CRY-334 vẫn In Progress.

#### Rerun sau khi Docker Compose phục hồi — 21/11/2026

- `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_job_source_scope_lineage_mismatch.py` → **1 passed/25,93s/0 skipped**; xác nhận timeframe và symbol mismatch trả exact denial trước source lock, không ghi quyền/decision, không seed policy/license.
- `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_job_source_scope_qa.py` → **1 passed/20,25s/0 skipped**; QA gate chờ source lock rồi từ chối, không ghi authorization, feature snapshot, decision hoặc score-event. Cả hai bài chạy Alembic upgrade/check và cleanup Compose thành công trên head hiện tại.
- Phạm vi vẫn QA-only: không chứng minh positive non-QA quyền nguồn, full license-scope matrix, race thu hẹp/thu hồi quyền hay mixed-cohort PAPER. CRY-334 tiếp tục In Progress.

#### Rerun kiểm chứng QA denial/lock boundary — 22/11/2026

- `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_paper_performance_source_scope.py tests/e2e/test_wp06_shadow_drift_source_scope.py` → **2 passed/40.20s/0 skipped**. PAPER E2E kiểm evaluator-role isolation, QA/missing-rights denial, không ghi report và writer chờ cohort fence trước khi trade đổi trạng thái; Shadow/Drift E2E kiểm membership change làm application rollback toàn cohort rồi retry ổn định không metrics/proposal.
- `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_job_source_scope_qa.py tests/e2e/test_wp06_scorer_job_source_scope_lineage_mismatch.py` → **2 passed/38.33s/0 skipped**. QA source gate chờ khóa rồi fail closed; mismatch timeframe/symbol bị từ chối trước lock và không ghi rights/decision/score ledger.
- Các ca đều QA-only/negative. Không đóng mixed eligible cohort, near-miss mọi chiều, policy shrink/revoke sau preflight hợp lệ hay quyền PAPER non-QA; CRY-334 giữ In Progress và active modes khóa.

### Follow-up unit SQL contract cho exact PAPER SourceUseScope — 21/11/2026

- Thêm assertion cấu trúc migration `f8e0a2c4d6ba`: source/provider/product/venue/contract; symbol/interval membership; containment hai biên range; purpose/output/derived-use/retention; và canonical hash của use-scope dựng từ dòng quyết định đã kiểm quyền.
- `uv run pytest -q -rs --tb=short tests/unit/test_wp06_paper_performance_source_scope_migration.py` → **18 passed/0 skipped**; Ruff check/format và diff-check đạt. Đây là regression guard cho SQL text, không được tính là behavioral PostgreSQL mismatch/race E2E.
- Vẫn mở ma trận denial trên DB, race policy shrink/revoke, mixed cohort và positive quyền PAPER non-QA; CRY-334 giữ In Progress.

### Follow-up QA-only scorer source-lineage guards — 22/11/2026

- Mở rộng PostgreSQL Compose probe trên workspace QA disposable. Core scorer gate từ chối timeframe và symbol lệch với `WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING`, đồng thời từ chối source provider không nhận diện với `WP06_SCORER_JOB_SOURCE_UNKNOWN_PROVIDER`; các ca chạy trước khi lấy source lock và không ghi authorization, receipt, feature snapshot, decision hoặc score-event.
- Dùng app workspace/bot context hợp lệ để gọi lại trigger `BEFORE INSERT` staging thật sau khi fixture tạo mismatch có chủ đích trong transaction rollback. Revision, checksum, source-event ID, source không nằm trong allowlist và bar bị thiếu đều trả exact `WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING`; không rights/decision writes, không seed policy/license.
- `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_job_source_scope_lineage_mismatch.py` → **1 passed/47.54s/0 skipped**. E2E chạy Alembic upgrade/head/check và xác nhận Compose resources được dọn. Lỗi probe ban đầu do thiếu `app.workspace_id`/`app.bot_id` khiến scope guard chạy trước lineage; sau khi đặt context, bổ sung no-write fields và sửa assertion provider/source, toàn bộ matrix mục tiêu đạt.
- `uv run ruff check src tests migrations scripts` đạt; `uv run ruff format --check src tests migrations scripts` → **1.429 files already formatted**; mypy **548 source files** không lỗi; `git diff --check` đạt.
- **Giới hạn:** chỉ là structural/negative QA proof. Không chứng minh policy license scope đủ mọi chiều, positive non-QA eligibility, rights shrink/revoke sau preflight hợp lệ hay mixed eligible/denied cohort. CRY-334 giữ In Progress; `SHADOW`/`PAPER_GATE` giữ khóa.

### Follow-up v3 TRAINING lineage denial và Shadow authorization clock — 22/11/2026

- Review độc lập phát hiện manifest dataset v3 phụ thuộc nguồn có thể qua `DatasetSourceEligibilityGate` dù chưa có producer lineage exact `SourceUseScope`; các source/policy identifiers đơn lẻ không đủ chứng minh symbol/interval/range/output/derived-use/retention đã được kiểm. Gate hiện từ chối trước load/persist mẫu bằng `source_use_lineage_missing`, giữ source-free path và để bundle v5 dùng `DatabaseReplayTrainingRightsGate` chuyên biệt. Domain/source/fold/replay-rights unit liên quan **42 passed/0 skipped**; normalized-sample PostgreSQL Compose E2E **1 passed/335.38s/0 skipped** trên d2 xác nhận no-write preflight.
- Reviewer thứ hai phát hiện gateway Shadow nhận `checked_at` do caller chọn. Migration `f0a2c4e6b8d3` giữ API tương thích nhưng nguồn quyết định là `clock_timestamp()` DB sau source lock; quyền gọi helper nội bộ bị giới hạn. Migration/ACL unit **28 passed/2 warnings**, Ruff/format và sole Alembic head d3 đạt.
- E2E của normalized sample chưa rerun sau khi cập nhật head guard d3. Shadow E2E không tới probe do Docker image inspect timeout 30 giây. Task9 integrated E2E timeout 360 giây tại `market-signal-worker produce-paper-decision`; không tính pass. Docker socket ping timeout và một E2E khác đang chạy, nên chưa rerun Compose.
- Lựa chọn A của người dùng cho model card/action latency tiếp tục là aggregate từ ledger hiện có theo cohort as-of/current-rights, không thêm ledger. Các E2E/fixture hiện có chỉ phủ QA/negative; vẫn thiếu positive PAPER non-QA, mixed cohort, full near-miss, rights shrink/revoke race và TEST support. CRY-334/WP06 In Progress; active modes khóa.
- Rerun unit trên head d3: source eligibility/receipt/fold/split/replay-rights/Shadow clock/action latency **45 passed/6.52s/0 skipped**; Ruff check/format, diff-check và Alembic sole head đạt. Docker Compose E2E chưa rerun vì daemon timeout kể cả sau khi lượt Playwright khác đã kết thúc.

### Rerun Task9 sau sửa regex/hash canonical và rights-revoke contract — 23/11/2026

- Regression unit migration/domain Drift → **32 passed/0 skipped**. Regex fingerprint sau f-string được kiểm bằng rendered SQL; scoped JSON serialization khớp canonical key order phía Python; nhánh hash legacy không đổi.
- Integrated PostgreSQL Compose `tests/e2e/test_wp06_task9_integrated_learning_journey.py` → **1 passed/115.35s/0 skipped**. Sau revoke, application ghi assessment metadata-only `INCONCLUSIVE`, 0 eligible, không metrics/proposal; populated downgrade dừng đúng guard Shadow/Drift vì ledger đã có dòng.
- Synthetic QA không phải entitlement. Rights non-QA, mixed eligibility, mọi chiều near-miss và shrink/revoke race với quyền hợp lệ vẫn cần bằng chứng thật; CRY-334 giữ In Progress, active modes khóa.

### Cross-check migration head d3 — 23/11/2026

- Shadow/Drift source-scope Compose **1 passed/26.48s/0 skipped**; normalized-fold-sample Compose **1 passed/114.76s/0 skipped**. Cả hai chạy lại sau migration `f0a2c4e6b8d3`, xác nhận QA denial/no-write ở source sample và source authorization path hiện hành.
- Scorer proof-v3 PostgreSQL Compose **1 passed/23.56s/0 skipped**, proof validator/migration units **29 passed/0.78s/0 skipped**, admin Playwright desktop/mobile **2 passed/4.96s/0 skipped**. Các kiểm chứng này không tạo quyền non-QA hoặc làm đủ sample support.

### Recheck scope/mô hình hiển thị và Docker Compose — 24/11/2026

- `uv run pytest -q -rs --tb=short tests/unit/test_source_license_scope.py tests/unit/test_wp06_training_license_scope_migration.py tests/unit/test_learning_scorer_model_card.py tests/unit/test_learning_scorer_model_card_application.py tests/unit/test_learning_scorer_model_card_read_model.py tests/unit/test_learning_scorer_report_read_model.py` → **115 passed/1.14s**.
- `uv run pytest -q -rs --tb=short tests/e2e/test_learning_scorer_model_card_ui.py tests/e2e/test_learning_scorer_admin_ui.py` → **3 passed/6.34s/0 skipped**; desktop/mobile Playwright, model-card screenshot đã xem. Đây không chạy PostgreSQL scope gate.
- Compose `tests/e2e/test_wp06_replay_rights_races.py` dừng ở `alembic upgrade head` timeout 300s trước probe; không tính là behavioral pass. Source scope domain/migration matrix đã pass unit nhưng mọi dimension chưa có đối chiếu đầy đủ qua DB; policy shrink/revoke race và quyền PAPER non-QA còn mở. Itsaplan MCP `localhost:3000` cũng không kết nối; không thay đổi tracker state. Không restart Docker vì cần tránh gián đoạn các container đang chạy.

### Retry sau khi Docker Engine tự hồi phục — 24/11/2026

- `uv run pytest -q -rs --tb=short tests/e2e/test_wp06_replay_rights_races.py` → **1 passed/167.04s/0 skipped**. Chạy thành công consumer-first và writer-first source-lock/revoke race cùng finite-retention denial trên fixture QA; đây là quyền QA tổng hợp, không phải entitlement PAPER non-QA hay full per-dimension DB mismatch matrix.
- Compose cleanup của lần retry đạt. Đã xác nhận project test còn sót `crypidian-wp06-replay-race-055394bfb0` thuộc đúng Compose labels của lần chạy thất bại trước, rồi `docker compose ... down --volumes --remove-orphans` xóa đúng container/network/volume đó. Không restart Docker.
- Itsaplan đã hồi phục; comment **#4469** của CRY-334 ghi unit, Playwright và E2E mới, giữ ticket In Progress. Mixed cohort, full DB scope mismatch, positive PAPER non-QA và scope shrink/revoke trên entitlement hợp lệ vẫn mở.
- Scorer lineage mismatch QA PostgreSQL Compose `tests/e2e/test_wp06_scorer_job_source_scope_lineage_mismatch.py` → **1 passed/46.09s/0 skipped**; timeframe/symbol/provider lineage mismatch và revision/checksum/source-event/source/missing-bar guards đều bị từ chối trước ghi quyền/quyết định.
- Scorer source QA gate Compose `tests/e2e/test_wp06_scorer_job_source_scope_qa.py` → **1 passed/29.76s/0 skipped**, xác nhận current QA denial/lock-order. Hai Compose test tự dọn project; không có production entitlement.
- Đọc activity lại rồi thêm comment **#4470** vào CRY-334 cho hai E2E mới; issue giữ In Progress. Dữ liệu vẫn chưa bao phủ mọi policy dimension trong DB hoặc quyền PAPER non-QA.

### Replay TRAINING scope matrix và source identity guard — 24/11/2026

- PostgreSQL replay-rights E2E mở rộng từ rights race sang near-miss từng chiều `symbols`, `intervals`, `data_start`, `data_end`, `purpose`, `outputs`, `derived_use`, `retention`; mỗi lần thu hẹp được authorize qua candidate-builder nhưng replay từ chối trước ghi training receipt. `purpose` cần dựng policy và scope nhất quán; ca fixture không hợp lệ bị domain guard từ chối trước DB và đã được sửa.
- WP05 source-registry E2E đối chiếu scope identity mismatch cho `source_id`, `provider`, `product`, `venue`, `contract`; đồng thời thử sửa provider/product/venue/contract sau khi identity đã thiết lập và xác nhận DB từ chối. Đây là kiểm soát identity của source catalog, không phải quyền PAPER.
- TDD: probe identity ban đầu thiếu trường kết quả (`KeyError: scope_identity_denials`, 41.63s); replay probe RED trước khi có nhánh test mới (54.36s), sau đó lộ fixture purpose ngoài domain và tham số `allowed_purposes` truyền trùng. Đã sửa test theo domain invariant, cuối cùng E2E replay **1 passed/194.51s/0 skipped** và registry **1 passed/58.78s/0 skipped**.
- Unit source-scope/rights/revision/registry migration → **95 passed/0.76s/0 skipped**. Ruff/format, `py_compile` cho wrapper/probe và `git diff --check` đạt. Model card/action latency theo lựa chọn A vẫn tổng hợp ledger hiện có theo cohort as-of và current-rights, không thêm latency ledger.
- **Còn mở:** scope matrix riêng cho scorer, PAPER performance, Shadow và Drift; mixed eligible/denied cohort; rights shrink/revoke trong race của các consumer khi quyền thực hợp lệ đang được dùng; entitlement PAPER non-QA và TEST support **1.000 tổng/100 mỗi lớp**. Itsaplan comments **#4480–#4482** ghi tiến độ và đính chính số test; CRY-334/WP06 tiếp tục In Progress, `SHADOW`/`PAPER_GATE` giữ khóa.
