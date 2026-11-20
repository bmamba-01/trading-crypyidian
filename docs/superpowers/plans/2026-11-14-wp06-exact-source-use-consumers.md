# WP06 Exact Source-use Consumers Implementation Plan

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
- [ ] Hoàn tất ma trận mismatch từng chiều scope và race policy thu hẹp/thu hồi giữa preflight với commit; positive authorization cần quyền non-QA thật.
- [x] Chạy runtime units, resolver/current-rights và source-bar atomic E2E; D7 gateway chạy Alembic upgrade/check và downgrade/re-upgrade. Source-linked replay, scorer PAPER denial, source-scope, Shadow/Drift và report writer chạy lại cùng D7 head.

### Task 4: Kiểm scope chính xác cho aggregate hiệu năng PAPER

**Kết quả:** chẩn đoán hiện có kiểm quyền PAPER có kiểu cho từng decision chính xác và quyền aggregate-report cho toàn cohort trước khi trả hoặc lưu metric. Giữ attribution decision→intent→đúng một PAPER trade đã đóng cùng các ngưỡng đã duyệt; thiếu quyền không trả aggregate/report một phần. `INCONCLUSIVE` chỉ dành cho cohort đã qua kiểm quyền nhưng không đủ mẫu/support/coverage; thiếu quyền làm gateway từ chối.

- [x] Dùng quyền hiện hành theo exact decision-bar lineage và các operation cố định `paper_demo` + `aggregate_report` + `aggregate_reporting`; receipt inference chỉ là audit.
- [x] Aggregate gateway kiểm toàn bộ decision của cả hai cohort dưới source lock trước khi tính metric; thiếu quyền từ chối, không trả member data hoặc persist report.
- [x] Gắn membership + current-rights digest vào identity/idempotency và recheck trước khi trả idempotent/recent reports.
- [x] Unit/Compose kiểm exact decision→intent→closed trade attribution, QA/missing-target denial, metadata-only access, no-write denial, digest và current report recheck. D7 writer tự chạy aggregate gateway, đối chiếu report/policy/metric/status/digest trước insert; E2E kiểm cả table/column INSERT ACL, scope, downgrade và re-upgrade.
- [x] Xác minh và sửa truy vấn PostgreSQL của evaluator trong migration `f4b6d8e0a2c7`. Compose chạy projection baseline và phép tính log-loss từ hàm đã cài, kiểm đúng tập cột được cấp và xác nhận downgrade/re-upgrade. API trả HTTP 409 cho lỗi lineage và sai lệch với kết quả aggregate.
- [ ] Hoàn tất matrix policy thu hẹp/rights revoke, race khi cohort/read/write, retry sau policy change và toàn bộ scope mismatch; positive aggregate report E2E chờ quyền non-QA thật. Giữ ngưỡng 30 ngày, 1.000/100, coverage ≥80%, regression >0,01 và `NO_STATE_CHANGE`.
- [x] Không đổi projection/UI trong lát source-use này; Playwright responsive desktop/mobile report đã đạt ở vòng UI trước, screenshot hiện có được giữ.

### Task 5: Kiểm quyền chính xác cho từng member Shadow và Drift

**Kết quả:** observer dùng bộ kiểm source-use chính xác dùng chung cho baseline/challenger; Drift ghim digest scope hiện hành cho từng cohort member và thành `INCONCLUSIVE` khi bất kỳ member nào thiếu coverage.

- [x] Unit kiểm exact member lineage, thiếu scope/retention/range, source rights và hành vi baseline decision.
- [x] Dùng exact source-use gate kiểm từng Shadow decision trước khi append; giữ nguyên baseline decision và metadata-only/ABSTAIN.
- [x] Recheck bar của mọi Drift member dưới source lock và persist current-rights digest xác định cùng membership cohort.
- [x] Compose E2E kiểm QA denial, workspace/RLS/ACL, source-scope gate, Drift persistence/digest, admin metadata read và populated downgrade.
- [x] Trên head `a6c8e0b2d4f7`, observer chỉ gọi được cổng Drift hai tham số; cổng tự lấy source IDs, khóa nguồn theo thứ tự rồi mới đọc `clock_timestamp()` của PostgreSQL. Lời gọi cổng ba tham số với timestamp lịch sử và hai hàm PAPER nội bộ đều bị từ chối `42501`; app/runtime không có EXECUTE. Compose cũng xác nhận gate chờ khóa QA rồi trả `NULL`, không dùng fixture để suy ra quyền PAPER.
- [x] Mở rộng QA-only Compose E2E qua `LearningDriftApplication` với cohort có một member QA: assessment/member thật được ghi nhưng `INCONCLUSIVE`, `rights_unverified`, 0 eligible và không có proposal; retry trả duplicate. Fixture chỉ kiểm denial, không phải bằng chứng quyền.
- [x] Sửa hash parity cho `source_rights_digest` khác NULL: migration `c6e8a0b2d4f9` dựng JSON theo thứ tự khóa của Python và giữ nguyên cách viết số mũ khi tính SHA-256. Integrity guard dùng quyền có sẵn trên hàm JSON và số thực. Nhánh NULL lịch sử giữ nguyên chuỗi byte.
- [x] Repository thật trả `None` cho decision QA. Ứng dụng trả `None` khi cohort trống, không ghi assessment/member; downgrade bị chặn khi ledger có dữ liệu và downgrade/re-upgrade bảng trống khôi phục contract/ACL.
- [x] Sau sửa review, 149 unit liên quan và lô Compose source-scope, persistence, admin-read đạt trên head `c6e8a0b2d4f9` (4 E2E/133.88s/0 skipped). Các bước downgrade trực tiếp khi ledger có dữ liệu bị từ chối tại đúng revision. Re-review không còn finding chặn; retry xác nhận cùng assessment ID và chỉ một hàng theo đúng identity.
- [x] Full unit **6.076 passed**, regression chéo 8 file WP06 scorer/source-lineage/PAPER/rights/DB **11 passed/613.13s/0 skipped**, Ruff, format (1.408 files), mypy (544 source files), sole Alembic head `c6e8a0b2d4f9` và diff-check đạt.
- [x] Commit `06eee0a` được fast-forward push lên `origin/dev`; CRY-334 vẫn In Progress vì các rights race/non-QA journey chưa có entitlement thật.
- [ ] Mixed-source/member và rights revoke/shrink race vẫn cần quyền PAPER non-QA thật; QA fixture chỉ chứng minh denial. Fixture registry Drift được seed ở chế độ replica, nên test không tuyên bố kiểm registry approval hoặc Shadow production writer. Fixture DB WP06F chỉ dành kiểm operator path.

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
