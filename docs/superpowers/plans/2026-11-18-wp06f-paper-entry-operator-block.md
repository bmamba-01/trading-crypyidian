# WP06F — PAPER operator entry block Kế hoạch triển khai

> Dành cho agent triển khai: BẮT BUỘC dùng skill superpowers:subagent-driven-development. Thực hiện sau plan scorer reader-role. Làm tuần tự phần runtime/persistence rồi tới Admin UI vì chúng dùng chung scorer event projection.

**Mục tiêu:** Cho phép owner/risk-admin thêm block PAPER có audit cho đúng một quyết định scorer đã duyệt, không sửa score event hoặc bỏ qua kiểm tra rủi ro.

**Kiến trúc:** Lưu ledger block append-only riêng theo một BotDecision và score event APPROVE/PAPER_GATE. Service đã xác thực dùng workspace L1 entry lock hiện có để tuần tự hóa, kiểm tra scorer target còn active và chưa có OrderIntent, rồi ghi operator block. PAPER runtime kiểm ledger trước reservation/OrderIntent; trang scorer history hiện có hiển thị action hợp lệ và audit state.

**Công nghệ:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL 18 (phiên bản Compose đã kiểm), Alembic, Jinja2, Bootstrap 5.3.8 nội bộ/CSS, pytest và Playwright.

**Thiết kế gốc:** docs/superpowers/specs/2026-11-17-wp06f-paper-entry-operator-block-design.md

## Ràng buộc toàn cục

- Chỉ owner/risk-admin được block; yêu cầu CSRF, TOTP MFA còn hạn theo RECENT_MFA_LIFETIME và reason đã trim dài 1–500 ký tự.
- Chỉ block score event APPROVE/PAPER_GATE còn active và khớp chính xác workspace/bot/decision/hash/candidate.
- Ghi operator event riêng, bất biến; không sửa BotDecision/LearningScoreEvent và không nâng VETO/ABSTAIN.
- Một event chỉ block một BotDecision; không release hoặc pause toàn bot/strategy/workspace.
- Nếu block thắng race thì chặn trước khi ghi reservation/OrderIntent/trade; nếu OrderIntent đã commit, request block đã quá muộn và không hủy intent đó.
- RiskEngine vẫn bắt buộc với các entry khác; protective exit, close/flatten, SHADOW và venue execution không bị ảnh hưởng.
- Giữ nhãn/lỗi/lịch sử lý do bằng tiếng Việt, Bootstrap/Be Vietnam Pro nội bộ và CSP; không script/style nội tuyến, không CDN.
- Yêu cầu xác nhận trước block không hoàn tác, focus bàn phím rõ, feedback loading/success/error, layout responsive, touch target web đủ cỡ và screenshot desktop/mobile.

## Các điểm cần review kỹ

- BotDecision/event/candidate giả hoặc scorer target cũ có thể tạo block; server phải nạp lại và đối chiếu mọi quan hệ.
- Không tin actor, MFA timestamp, workspace hoặc target revision do client gửi.
- Đã có intent hoặc trade thì không block hồi tố hoặc hủy.
- Block và entry đồng thời phải tuần tự hóa theo cùng thứ tự khóa, không để row ghi dở.
- UI phải phân biệt APPROVE gốc của scorer với PAPER block do operator tạo, đồng thời dùng được trên màn nhỏ.

---

### Task 1: Hợp đồng domain, quyền và bản ghi block bất biến

**Tệp:**
- Tạo: src/trading_bot/domain/learning/paper_entry_block.py
- Sửa: src/trading_bot/auth/policy.py
- Tạo: src/trading_bot/adapters/database/models/learning_scorer_paper_entry_block_entities.py
- Tạo: src/trading_bot/adapters/database/repositories/learning_scorer_paper_entry_blocks.py
- Tạo: một migration kế tiếp migration cấp quyền reader-role trong plan scorer history
- Test: tests/unit/test_learning_scorer_paper_entry_block.py
- Test: tests/unit/test_learning_scorer_paper_entry_block_repository.py

**Giao diện:**
- Thêm Permission.LEARNING_PAPER_ENTRY_BLOCK, chỉ cấp cho OWNER và RISK_ADMIN.
- Domain result bất biến, gồm workspace_id, bot_id, bot_decision_id/hash, candidate_id, score-event/target pointer tuple, actor_user_id, mfa_verified_at, reason, created_at và idempotency_key.
- Repository cung cấp find_by_decision, find_by_idempotency_key và append_block; không update/delete event đã có.
- Database RLS giới hạn mọi read/write theo workspace; trigger/privilege từ chối UPDATE/DELETE.

- [x] Bước 1: Viết test thất bại cho quyền theo role, bản ghi sự kiện bất biến, giới hạn reason sau chuẩn hóa, xung đột idempotency key và hành vi chỉ ghi nối thêm.
- [x] Bước 2: Chạy `uv run pytest tests/unit/test_learning_scorer_paper_entry_block.py tests/unit/test_learning_scorer_paper_entry_block_repository.py -q --tb=short`. Kỳ vọng: test thất bại vì permission, domain event, repository và migration chưa tồn tại.
- [x] Bước 3: Thêm domain types, permission chuyên biệt, ORM mapping, repository và migration áp RLS theo workspace/chỉ ghi nối thêm. Cấp cho scorer-history reader quyền SELECT đúng các cột workspace/decision/event/actor/MFA/reason/time cần hiển thị, kèm RLS theo workspace; không cấp quyền ghi ledger.
- [x] Bước 4: Chạy hai unit test files cùng auth/migration tests liên quan; kỳ vọng đạt.
- [x] Bước 5: Sửa hai finding review về downgrade FORCE RLS và UUID zero; re-review xác nhận cả hai đã xử lý, không có lỗi Critical/Important mới.

### Task 2: Service operator đã xác thực và runtime gate được tuần tự hóa

**Tệp:**
- Tạo: src/trading_bot/application/learning_paper_entry_block.py
- Sửa: src/trading_bot/application/paper_runtime_application.py
- Sửa: src/trading_bot/adapters/database/repositories/learning_scorer_paper_entry_blocks.py
- Tạo: migrations/versions/f6c8e0a2b4da_wp06_paper_entry_block_source.py
- Test: tests/unit/test_learning_paper_entry_block_source_migration.py
- Test: tests/unit/test_learning_paper_entry_block_service.py
- Sửa: tests/unit/test_paper_runtime_application.py
- Hồi quy đường nâng cấp và head Alembic: tests/unit/test_sig0_taker_flow.py, tests/unit/test_wp06_migration_legacy_reconciliation.py
- PostgreSQL E2E cho quyền hàm đọc nguồn: tests/e2e/test_wp06_scorer_paper_entry_block_source_ddl.py

**Giao diện:**
- Service nhận Admin session factory và Principal đã xác thực; block(bot_decision_id, reason, idempotency_key) nạp lại mọi event/target ở server. Vì scorer-history reader phải giữ nguyên allowlist đã duyệt và admin-web không được đọc thêm cột scorer, service gọi một SECURITY DEFINER source reader hẹp, workspace-scoped, chỉ trả tuple score event/decision/target cần cho block. Function thuộc role NOLOGIN/NOBYPASSRLS riêng, chỉ có SELECT cột tối thiểu và không cấp quyền đọc trực tiếp cho admin-web.
- Trước khi ghi, service kiểm quyền OWNER/RISK_ADMIN, TOTP MFA còn hạn, score event APPROVE/PAPER_GATE, target hiện hành khớp và chưa có OrderIntent.
- Request block và PaperBotDecisionCommand dùng cùng workspace L1 lock rồi cùng decision-scoped transaction advisory lock trước khi nạp BotDecision. Không dùng `SELECT FOR UPDATE` vì PostgreSQL yêu cầu thêm UPDATE privilege trên bảng BotDecision; giữ nguyên bảng BotDecision SELECT-only.
- Runtime kiểm block sau khi xác thực BotDecision và trước RiskEngine/reservation/OrderIntent/trade.
- Nếu entry commit trước, service trả conflict báo decision đã submit; không hủy hoặc sửa intent/position hiện có.

- [x] Bước 1: Viết unit tests thất bại cho quyền/scope/MFA/reason/idempotency, block trước intent, intent commit trước block và đường RiskEngine/protective-exit giữ nguyên.
- [x] Bước 2: Chạy `uv run pytest tests/unit/test_learning_paper_entry_block_service.py tests/unit/test_paper_runtime_application.py -q --tb=short`. Kỳ vọng: test thất bại vì ledger operator và kiểm tra runtime chưa tồn tại.
- [x] Bước 3: Thêm service và kiểm tra repository trong runtime theo đúng thứ tự khóa workspace hiện tại; giữ nguyên scorer decision.
- [x] Bước 4: Chạy unit tests service/runtime và hồi quy incident-hold; kỳ vọng tất cả đạt.
- [x] Bước 5: Hoàn tất review độc lập về yêu cầu và chất lượng.

**Kết quả triển khai:** 338 test tập trung và 5.410 unit test đạt, mỗi lượt có 6 cảnh báo Alembic `path_separator`. PostgreSQL Compose xác nhận ACL/RLS hàm đọc nguồn, upgrade → downgrade về d9 → re-upgrade dA và `alembic check`. Review độc lập đã chấp thuận Task 2 và xác nhận đủ điều kiện chuyển sang Task 3.

### Task 3: Thao tác Admin và lịch sử audit tiếng Việt

**Tệp:**
- Sửa: src/trading_bot/app.py
- Sửa: src/trading_bot/admin/learning_scoring_page_router.py
- Sửa: src/trading_bot/application/learning_paper_entry_block.py để có phép đọc eligibility chỉ phục vụ xem trước, dùng chung predicate current-target với POST.
- Sửa: src/trading_bot/application/learning_scorer_score_event_read_model.py
- Sửa: src/trading_bot/adapters/database/repositories/learning_scorer_score_event_read_model.py
- Sửa: src/trading_bot/domain/learning/scorer_score_event_summary.py
- Sửa: src/trading_bot/templates/admin/learning_scoring.html
- Sửa: src/trading_bot/static/pages/learning.css (stylesheet riêng đã được trang này tải; không sửa token hoặc stylesheet Admin toàn cục).
- Sửa: src/trading_bot/static/learning-scoring.js
- Tạo: src/trading_bot/admin/learning_paper_entry_block_router.py
- Test: tests/unit/test_learning_scorer_admin_page.py
- Test: tests/unit/test_learning_scorer_score_event_read_model.py
- Test: tests/unit/test_learning_paper_entry_block_service.py
- Test: tests/unit/test_learning_paper_entry_block_admin.py
- Test: tests/unit/test_learning_paper_entry_block_app.py
- Hồi quy: tests/unit/test_wp06_scorer_history_reader_app.py và tests/unit/test_paper_runtime_application.py.

**Giao diện:**
- POST action: `/admin/learning/scoring/events/{score_event_id}/paper-block`; chỉ nhận CSRF hợp lệ và Principal đã xác thực.
- Page projection gồm scorer decision gốc, trạng thái operator block và metadata actor/reason/time tối thiểu sau khi đọc theo workspace.
- App composition nối `LearningPaperEntryBlockService` vào `app.state.learning_paper_entry_block_service` bằng session factory Admin hiện tại và đăng ký router trong admin-web; scorer-history reader tiếp tục dùng factory riêng.
- Trang chỉ dùng phép xem trước read-only do service sở hữu sau khi projection xác định score event có thể xét; phép xem trước và POST cùng gọi predicate so khớp active target. POST tiếp tục là nguồn quyết định cuối: service lấy lại workspace L1 → decision lock và kiểm target/OrderIntent trong transaction trước khi ghi.
- Giữ nguyên scorer-history reader ACL allowlist đã duyệt. Metadata audit lấy từ các cột reader đã được cấp ở Task 1; không thêm quyền hoặc cột cho login reader.
- Chỉ hiện action khi PAPER_GATE APPROVE còn active, chưa bị block, có BotDecision link và chưa có OrderIntent.
- Form xác nhận nêu reason và yêu cầu MFA gần đây bằng tiếng Việt; lịch sử phân biệt AI decision với operator block.

- [x] Bước 1: Viết app-composition, page/service/router/read-model tests thất bại cho preview, eligibility, audit, CSRF, reason/confirmation, role, workspace, app wiring và trạng thái UI.
- [x] Bước 2: Chạy sáu tệp focused bằng pytest. RED **14 failed, 65 passed/2,62s**, không có lỗi collection. `task-3-red.log` giữ kết quả; `task-3-intermediate-failure.md` ghi assertion trace cũ và sửa.
- [x] Bước 3: Wire service/router trong `app.py`; thêm route, form xác nhận và lịch sử responsive theo Bootstrap 5.3.8 hiện có cùng `design-system/crypidian-admin/pages/admin-shell-glass.md`. Task 2 service chỉ được mở rộng bằng phép preview read-only dùng chung predicate; POST semantics/lock order được giữ nguyên.
- [x] Bước 4: Focused GREEN **80 passed/3,22s**; regression service/runtime Task 2 **67 passed/2,05s**; Ruff, Ruff format, mypy 7 module production, compileall và `git diff --check` đạt. Không thêm E2E trong Task 3.
- [x] Bước 5: Independent review Task 3 hoàn tất; không có finding Critical/Important và không yêu cầu sửa đổi. Task 4 vẫn phải cung cấp race PostgreSQL và Playwright desktop/mobile.

**Kết quả triển khai và review độc lập:** Task 3 được review chấp thuận, không có finding Critical/Important và không yêu cầu sửa đổi. Report/diff/snapshot ở `.superpowers/sdd/2026-11-18-wp06f-paper-entry-operator-block/task-3-report.md` và `task-3-review.diff`. Chưa chạy E2E; Task 4 Playwright flow mở `/admin/learning/scoring` bằng owner hoặc risk-admin có TOTP mới, dùng PAPER_GATE fixture active với APPROVE đã link BotDecision còn active và chưa có block/OrderIntent; mở nút “Chặn entry PAPER” bằng bàn phím, nhập reason, tick xác nhận rồi submit; xác minh loading, lỗi/CSRF và `Idempotency-Key` UUID hoặc success; reload thấy decision AI APPROVE gốc vẫn nguyên và audit operator actor/reason/time riêng. Lặp tại viewport 390 px, kiểm focus/keyboard và không tràn ngang. Các ca không đủ role/MFA, target cũ, block sẵn, hoặc intent sẵn phải không hiện action.

### Task 4: Chứng minh xử lý race PostgreSQL và Playwright desktop/mobile

**Ranh giới kiểm thử đã chốt:** deployment/current-rights production resolver hiện fail-closed với dữ liệu Compose hiện có. Người dùng duyệt fixture Compose disposable để kiểm operator path: phần setup có thể tạm bypass trigger/FK chỉ để chèn các hàng lineage tối thiểu cho liên kết `learning_score_event`, sau đó phải khôi phục enforcement trước khi gọi production service/runtime. Không dùng các hàng tổng hợp hay seam này để tuyên bố production eligibility PAPER_GATE. Resolver seam chỉ tồn tại trong process E2E, không thêm setting/config và không sửa production policy. Khi service/runtime chạy, PostgreSQL, source reader/ACL/RLS, block service, transaction, workspace L1 → BotDecision advisory lock, ledger append-only, PaperRuntime/RiskEngine/reservation/OrderIntent/trade và router phải là đường thật. Giữ riêng E2E production resolver fail-closed.

**Tệp:**
- Tạo: tests/e2e/test_wp06_scorer_paper_entry_block.py
- Tạo: tests/e2e/probes/wp06_scorer_paper_entry_block.py
- Tạo: tests/e2e/test_learning_scorer_paper_entry_block_admin_ui.py
- Tạo: tests/e2e/evidence/wp06-paper-block-desktop.png
- Tạo: tests/e2e/evidence/wp06-paper-block-mobile.png

**Giao diện:**
- PostgreSQL E2E dùng hai transaction và điểm đồng bộ xác định cho cả hai thứ tự: block lấy L1 trước; entry commit trước khi block lấy L1.
- Kiểm tra block thắng thì có audit row nhưng không có reservation/OrderIntent/trade mới; entry thắng thì giữ nguyên entry đã có và block request conflict vì quá muộn.
- Playwright kiểm desktop và mobile 390px, bàn phím/focus/confirmation, validation reason, feedback success/error, CSRF và `Idempotency-Key` UUID hợp lệ, audit actor/time và không tràn ngang.

- [x] Bước 1: Viết Compose/PostgreSQL race case trước khi mở rộng probe; chèn FK lineage tối thiểu trong disposable fixture với enforcement tạm dừng riêng trong setup, rồi khôi phục trigger/FK trước khi gọi actual service/runtime. Dùng resolver seam chỉ trong process E2E; không tuyên bố eligibility production. Đây là verification của behavior Task 1–3 đã implement; không ép RED giả. Nếu behavior lần đầu GREEN, ghi chính xác.
- [x] Bước 2: Chạy `uv run pytest tests/e2e/test_wp06_scorer_paper_entry_block.py tests/e2e/test_learning_scorer_paper_entry_block_admin_ui.py -q --tb=short`. Phân loại riêng behavior result với lỗi thiếu probe/fixture/bootstrap; chỉ sửa production khi một test tái hiện lỗi thật.
- [x] Bước 3: Thêm barrier PostgreSQL xác định trên workspace L1 và giữ decision advisory lock thật; giữ source reader, block service, audit và PAPER runtime thật. Chạy resolver-denial production riêng. UI dùng `create_app(AppSettings(app_env="test"))` với Playwright, actual scoring/block routers và service/read-model facades chỉ khi route/UI-only harness cần; module docstring phải nêu rõ facade không chứng minh DB eligibility/audit persistence. Ưu tiên POST thật qua service/DB với identity owner/risk-admin + TOTP. Kiểm CSRF/UUID `Idempotency-Key`, success/error, role/MFA/SHADOW/VETO/stale/block/intent, layout 390px; chụp rồi xem trực quan screenshots. Ghi mọi giới hạn auth/facade.
- [x] Bước 4: Chạy race, Playwright và scorer/incident-hold E2E hồi quy hiện có; kỳ vọng các test đã chọn đạt, không bỏ qua. Báo riêng giới hạn: seam không chứng minh current-rights production resolver cấp target active PAPER_GATE.

**Kết quả Task 4 tại thời điểm 18/11:** fixture chỉ tồn tại trong PostgreSQL Compose disposable; trigger/FK được khôi phục và xác nhận trước khi service/runtime. Race hai thứ tự `uv run pytest tests/e2e/test_wp06_scorer_paper_entry_block.py -q --tb=short` **1 passed/25.78s**; production resolver denial riêng `uv run pytest tests/e2e/test_wp06_scorer_runtime_target_resolver.py -q --tb=short` **1 passed/22.04s**. Operator Playwright **1 passed/4.11s**, nhóm scorer/incident-hold/operator UI **6 passed/15.54s**, flatten regression **2 passed/105.65s**. Screenshot desktop 1366px và mobile 390px đã được xem trực quan. UI test dùng router thật nhưng service/read-model facades; không chứng minh persistence hoặc eligibility DB. Bước 4 khi đó còn mở vì hai Compose regression incident-hold dừng trước probe do PostgreSQL fresh-volume unhealthy; kết quả rerun 19/11 ở bên dưới.

**Task 4 regression rerun 19/11:** fixture E2E bật PostgreSQL `tmpfs` chỉ theo lựa chọn từng test. Incident-hold runtime/admin Compose `2 passed/42.42s`; race operator `1 passed/24.73s`; current-rights resolver fail-closed `1 passed/19.19s`; nhóm scorer/incident-hold/operator Playwright `6 passed/19.26s`; scorer deployment PostgreSQL E2E `1 passed/35.57s`. Batch kết hợp tất cả các lượt trên trừ flatten `uv run pytest ... -q --tb=short` **11 passed/117.57s**. Test deployment đầu tiên phát hiện stale assertion `f6c8e0a2b4d8 (head)`; migration graph đang có duy nhất head `f6c8e0a2b4da`; đã rà 36 kỳ vọng graph-head dA trên 20 E2E paths: tracked diff có 34 thay thế d8→dA trên 19 files; hai kiểm tra còn lại nằm trong E2E legacy reconciliation untracked. `alembic heads` xác nhận graph head, còn `alembic current` xác nhận revision DB đã áp dụng. Test deployment đạt sau khi sửa. Resolver denial vẫn test riêng, không nhận synthetic operator target/rights. Bước 4 đã đạt.

### Task 5: Kiểm thử hồi quy toàn diện và bằng chứng đóng WP06F

**Tệp:**
- Cập nhật: docs/implementation/06-ai-learning.md
- Cập nhật: docs/superpowers/plans/2026-11-01-wp06-learning-wbs.md
- Cập nhật: docs/implementation/project-status.md
- Cập nhật: docs/research/glossary-vi.md chỉ khi xuất hiện thuật ngữ mới
- Cập nhật: docs/implementation/implementation-memory.md

- [x] Bước 1: Chạy `uv run pytest tests/unit -q --tb=short` để xác nhận toàn bộ unit suite; bare `uv run pytest -q` hiện thu thập nhầm script trong `tests/e2e/probes/` cần biến do E2E cha truyền. Chạy các E2E liên quan tường minh ở bước kế tiếp, không bỏ qua test đã chọn.
- [x] Bước 2: Chạy toàn bộ Compose E2E/Playwright bị ảnh hưởng gồm scorer deployment, operator block, incident entry hold, scorer UI và report/settings; kỳ vọng tất cả đạt, không bỏ qua test.
- [x] Bước 3: Chạy Alembic check với Compose DB, Ruff, mypy và `git diff --check`; điều tra mọi lỗi.
- [ ] Bước 4: Cập nhật và đọc lại memory/WBS, đồng bộ Itsaplan khi MCP hoạt động, yêu cầu review code độc lập rồi tích hợp, commit và push fast-forward lên `dev` sau khi mọi cổng cuối đạt.

**Kết quả Task 5 tại thời điểm 18/11:** full unit `uv run pytest tests/unit -q --tb=short` **5.432 passed/184.78s**, có 6 cảnh báo deprecation Alembic `path_separator`. Alembic Compose check đã đạt trong race E2E; Ruff, format, mypy và diff-check đạt cho các module/file liên quan. Regression `uv run pytest tests/e2e/test_wp06_learning_incident_entry_hold_runtime.py tests/e2e/test_wp06_learning_incident_entry_hold_admin.py -q --tb=short` **2 failed/949.09s** trước khi probe được gọi: cả hai Postgres fresh volume đều unhealthy (`pg_isready: no response`, log initdb dừng tại `running bootstrap script`). PostgreSQL 18.6 direct-run không volume healthy; direct-run có volume mới timeout sau 30 giây ở `docker run` cả khi chỉ dùng password ENV và khi thêm secret bind tạm, nên lỗi không chỉ do secret file. Kết quả khoanh vùng về fresh container/volume startup trên Docker Desktop nhưng chưa xác định root cause. Hai container/volume chẩn đoán đã được dọn đúng đối tượng; inventory `wp06f-postgres` lọc rỗng. Docker `/_ping` và `/info` còn hoạt động nhưng list container toàn cục timeout. Không có bằng chứng lỗi production; không đổi code/health timeout hoặc restart Docker dùng chung. Chưa chạy scorer-deployment và report/settings trong đợt này. Task 5 chưa hoàn tất.

### Cập nhật 2026-11-19 — Compose fixture `tmpfs` và Docker Desktop dừng

- Thêm `postgres_tmpfs=True` cho Compose fixture của incident-hold, operator race và current-rights resolver E2E. Mặc định helper vẫn gắn named volume; `compose.yaml` sản phẩm không bị sửa. PostgreSQL `tmpfs` không đặt mode để dùng quyền thư mục mặc định của Docker.
- TDD: unit mới ban đầu **1 failed** đúng vì helper chưa nhận tùy chọn; sau implement `uv run pytest tests/unit/test_wp06_learning_feature_vector_parity_harness.py -q --tb=short` **3 passed**. `docker compose config --format json` xác nhận service PostgreSQL có `tmpfs: [/var/lib/postgresql]` và không có persistent mount.
- Full unit rerun `uv run pytest tests/unit -q --tb=short` **5.433 passed/179.49s**, 6 cảnh báo Alembic `path_separator`. Ruff check, format-check và diff-check cho 6 tệp thay đổi đạt.
- `uv run pytest tests/e2e/test_wp06_learning_incident_entry_hold_runtime.py -q --tb=short` **1 passed/21.66s** với fixture `tmpfs`. Lượt kế `test_wp06_learning_incident_entry_hold_admin.py` dừng ở `compose up postgres` trước probe: Docker API trả HTTP 500 rồi “Docker Desktop is unable to start”; `docker compose down` trong cleanup cũng thất bại. Process Docker Desktop không còn hiện trong process list và `docker info` không kết nối được. Vì daemon tắt, cleanup/inventory của đúng project này hiện chưa xác minh được. Không khởi động lại Desktop dùng chung. Race operator, production resolver denial và các Compose regression khác chưa chạy lại sau thay đổi fixture.
- Task 4 regression step và Task 5 step 2 tiếp tục mở. Đây là lỗi hạ tầng Docker ở lượt admin, chưa phải assertion ứng dụng; không kết luận fixture mới đã giải quyết ổn định cho toàn bộ E2E.

### Cập nhật 2026-11-19 — Docker phục hồi và regression CRY-440 đạt

- Docker Desktop tự phản hồi lại; kiểm kê theo đúng Compose project của lượt lỗi cho thấy một container `Exited (127)` và một network còn sót, không có volume. Chỉ container/network đúng nhãn project được xóa; kiểm kê lại container/volume/network đều rỗng.
- Chạy lại incident-hold admin **1 passed/20.65s**; incident-hold runtime/admin chung **2 passed/42.42s**; operator race **1 passed/24.73s**; current-rights resolver denial **1 passed/19.19s**; scorer/incident/operator Playwright **6 passed/19.26s**; scorer deployment **1 passed/35.57s** sau khi cập nhật migration head assertions. Batch regression chọn **11 passed/117.57s**; report/settings **104 passed/1.29s**.
- Scorer deployment trước đó đã fail do test còn khóa Alembic head d8; actual graph hiện duy nhất có head dA. Đã rà 36 kỳ vọng graph-head dA trên 20 E2E paths: tracked diff có 34 thay thế d8→dA trên 19 tracked files; hai kiểm tra khác nằm trong E2E legacy reconciliation untracked. `alembic heads` kiểm graph head, `alembic current` kiểm revision DB đã áp dụng. Giữ nguyên các assertion revision lịch sử không gắn `(head)`.
- Task 4 bước 4 và Task 5 bước 2 đạt. Review độc lập không có Critical/Important finding; góp ý minor về graph head so với DB current đã cập nhật trong plan/memory. Task 5 bước 4 còn mở cho integration/commit vì worktree/index hiện chứa thay đổi hỗn hợp; không stage/commit/push trong lượt này.

## Thứ tự

Thực hiện sau plan scorer reader-role. Plan này sửa chung scorer history page/runtime entry flow, nên không chạy đồng thời với CRY-439.


#### Rerun 19/11/2026 theo ranh giới fixture A

- Người dùng chốt fixture PostgreSQL Compose dùng riêng cho operator path; production current-rights resolver vẫn chạy denial E2E riêng.
- Rerun operator race Compose: uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_paper_entry_block.py → 1 passed/21.71s/0 skipped. Disposable DB seed lineage tổng hợp chỉ ở setup, phục hồi trigger/FK trước service/runtime; test xác nhận cả hai lock order, audit riêng và không có side effect entry khi block thắng. Kết quả không chứng minh active scorer eligibility hoặc license non-QA.
- Rerun resolver thật: uv run pytest -q -rs --tb=short tests/e2e/test_wp06_scorer_runtime_target_resolver.py → 1 passed/16.10s/0 skipped, fail-closed riêng với target không đủ quyền.
- Rerun UI: uv run pytest -q -rs --tb=short tests/e2e/test_learning_scorer_paper_entry_block_admin_ui.py → 1 passed/3.68s/0 skipped. Ảnh desktop 1366×900 và mobile 390×844 đã tạo và xem: desktop scroll nằm trong vùng bảng, mobile chuyển thành card, trang không tràn ngang. UI harness dùng route thật và service/read-model facades; audit DB được chứng minh bởi Compose test.
- Ruff check, format-check và git diff --check cho ba file E2E liên quan đạt. Không đổi production code/migration trong lượt rerun. CRY-440 vẫn In Progress do các file implementation/test WP06F còn ở worktree cũ có thay đổi hỗn hợp; chưa tích hợp/commit trên head dev hiện hành.

### Cập nhật cuối 19/11/2026 — kiểm chứng tích hợp và UI

- Trên nhánh codex/wp06f-operator-dev từ origin/dev ad6583f7c2d1, full unit 5.993 passed/447.75s; batch Compose operator/schema/scorer/incident/UI 21 passed/160.09s; source-linked replay/backfill 7 passed/393.17s. Sau khi ảnh desktop lộ cột action bị cắt, thêm assertion về biên hiển thị, giảm min-width bảng desktop từ 1100px xuống 1060px và giữ cuộn ngang ở 1280px; Playwright scorer/operator UI chạy lại 3 passed/6.49s. Ảnh cuối desktop 1366×900 và mobile 390×844 đã được tạo lại và xem trực quan.
- Ruff check/format (1352 files), mypy (533 source files), node --check, git diff --check đạt. Sau khi ghép với CRY-421, sole Alembic head là f6c8e0a2b4db; lệnh alembic check chạy trong Compose scorer deployment E2E đã đạt trước nhánh CRY-421. Gọi trực tiếp từ host không kết nối được DNS postgres vì không có service Compose đang chạy, nên không khởi chạy DB phát triển có volume chỉ để lặp lại phép kiểm. Unit UI hồi quy 39 passed/4.85s.
- Đã đọc lại docs và đối chiếu số liệu với đầu ra; cập nhật Itsaplan, commit và push fast-forward là các bước cuối còn lại. Chỉ đóng CRY-440 sau khi push thành công; WP06/CRY-334 vẫn mở cho scope matrix còn thiếu và quyền dữ liệu non-QA.

### Tích hợp cuối 19/11/2026 sau đồng bộ `origin/dev`

- Hợp nhất `origin/dev` tại `9f3ff727c819` (CRY-421 report-reader, CRY-256 replay MTM) với WP06F; thêm merge migration `f6c8e0a2b4db` để giữ một Alembic head.
- Full unit **6.006 passed/440.34s**; Compose/PostgreSQL batch operator, resolver, DDL, deployment, DB roles, source-linked replay, report-reader và backfill **24 passed/496.73s/0 skipped**. Focused rerun các lỗi integration ban đầu **5 passed/238.25s**; Playwright admin **1 passed/4.60s**, screenshots desktop/mobile được kiểm tra trực quan.
- TDD phát hiện report canonical `realized_only` bị ghi thêm field mặc định mới trong khi `ReplayManifest.content_hash` cố ý giữ hash cũ; repository nay bỏ key mặc định, nhưng vẫn lưu `mark_to_market` khi được chọn. Source-linked training E2E đạt sau sửa. Fixture MFA unit tạo timestamp mới cho từng principal, không thay đổi lifetime/chính sách MFA. Probe least-privilege chấp nhận definer role operator đã cấp; test Alembic current nhận marker `(mergepoint)`.
- Build E2E ban đầu timeout do `uv sync` lặp bytecode compilation ở layer cuối; lần sync cuối tắt compilation trùng lặp, dependencies vẫn được precompile ở layer trước. Compose operator E2E sau đó đạt.
- CSS bổ sung ghi đè cụ thể cho `th.num` của scorer history; Playwright assertion xác nhận `white-space: normal` và header không tràn. Ảnh cuối ở `tests/e2e/evidence/wp06-paper-block-desktop.png` và `...mobile.png`.
- Ruff check/format (`1.377 files`), mypy (**533 source files**), JS syntax, `git diff --check`, `alembic heads` (`f6c8e0a2b4db (head)`) và Compose `alembic check` đạt. Task 5 test gates đã đạt; readback/tracker hoàn tất trước commit. WP06F chỉ chuyển Done sau push; CRY-334 và WP06 tổng thể vẫn mở.
