# Thiết kế: Tách quyền DB admin-web khỏi role ứng dụng chung

Ngày: 2026-11-24. Trạng thái: người dùng chọn A; WP06.14/CRY-449 triển khai, hồi quy và review độc lập hoàn tất ngày 2026-11-25. Admin-web đã tách khỏi `crypidian_app`; report-reader dùng RLS/current rights; WP07/RT1/ACL/Playwright và full unit đạt. Review không thấy P0/P1; thiếu DML probe đã khép. Hai P2 được theo dõi riêng: column-level report-reader (CRY-450/WP06.15) và positive allowlist admin-safe (CRY-451/WP06.16). WP06 cha vẫn In Progress cùng data-rights/support/eligibility gates.

## Ý định

Đảm bảo login PostgreSQL `crypidian_admin_web` chỉ có quyền tối thiểu phục vụ API quản trị, không kế thừa toàn bộ quyền của `crypidian_app` và không thể truy vấn trực tiếp dữ liệu nguồn nhạy cảm của WP06 như PnL từng trade hay xác suất scorer. Các trang quản trị cần trình bày kết quả PAPER vẫn hoạt động qua một reader/gateway riêng, có kiểm tra workspace, membership và quyền hiện hành.

Phát hiện gốc: PostgreSQL E2E ngày 2026-11-24 xác nhận `crypidian_admin_web` là thành viên của `crypidian_app`; role chung nhận `SELECT` toàn bảng `trade`. Vì vậy `admin_web` đọc được `trade.net_pnl`, dù scorer probabilities đã bị từ chối. Đường aggregate WP06 đang an toàn theo function gateway, nhưng ACL của login admin vẫn vi phạm acceptance trong Task 3.

## Mục tiêu

- Thu hồi membership `crypidian_app` khỏi `crypidian_admin_web`; quyền core admin được cấp qua role không-login có allowlist rõ ràng.
- Login `crypidian_admin_web` không được `SELECT` toàn bảng/`trade.net_pnl`, `learning_score_event.class_probabilities`, hoặc raw signal/decision/intent source fields ngoài allowlist được ghi rõ.
- Chuyển các trang dashboard/báo cáo cần đọc chi tiết PAPER sang credential reader riêng, chỉ dùng trong service/repository tương ứng; credential này không có quyền ghi ledger hay scorer data và RLS phải giới hạn workspace/member/bot.
- Giữ aggregate WP06 qua `SECURITY DEFINER` gateway hiện hữu, kiểm current-rights nguyên tử, không trả row nguồn hoặc một phần kết quả khi bị từ chối.
- Giữ đầy đủ các luồng admin hiện có: xác thực/MFA, quản lý cấu hình và bot, kiểm soát vòng đời PAPER, dashboard/báo cáo, scorer, model card và các trang learning.
- Thêm PostgreSQL Compose E2E xác minh ma trận role, RLS/tenant isolation, các aggregate WP06, cùng hồi quy admin web cần dữ liệu PAPER; giữ Playwright desktop/mobile cho các trang bị ảnh hưởng.

## Ngoài phạm vi

- Không thay đổi quyền dữ liệu nguồn, policy scoring, ngưỡng support, thuật toán drift/performance hoặc entitlement Binance/CoinAPI.
- Không mở `SHADOW`/`PAPER_GATE`, không tạo order, không đổi RiskEngine hay lifecycle bot.
- Không dùng QA fixture để chứng minh quyền non-QA hoặc kết quả PAPER dương tính.
- Không bỏ các màn hình report/trading đã được yêu cầu để đơn giản hóa ACL.

## Điều cấm

- Không để `crypidian_admin_web` tiếp tục là thành viên của `crypidian_app` sau migration.
- Không cấp `SELECT` rộng lên bảng nguồn cho role admin-safe hoặc WP06 aggregate caller.
- Chỉ gắn secret credential reader vào `admin-web` và `db-provisioner` tạm thời để tạo/đặt mật khẩu role; không gắn vào worker/runtime khác, `report-reader`, endpoint tổng quát hoặc request không qua authorization.
- Không làm suy yếu RLS, current-rights recheck, MFA, audit, giới hạn workspace/bot hoặc fail-closed behavior.
- Không ghi password, secret hay token vào source, log, ticket, screenshot hoặc report.

## Phương án

1. **A — Tách admin-web khỏi app role và chia read path theo trách nhiệm (đã chọn ngày 2026-11-24).** Bỏ membership chung; role admin-safe chỉ nhận quyền được allowlist; report/dashboard chuyển qua credential reader read-only có RLS chặt; WP06 summary tiếp tục qua gateway aggregate. Ưu điểm: đáp ứng tiêu chí đã duyệt và giữ các màn hình admin. Chi phí: cần map quyền và hồi quy rộng cho auth, bot, report, control và learning.
2. **B — Chỉ thêm credential aggregate riêng cho WP06.** Giữ quyền hiện tại của `admin_web` qua `crypidian_app`; acceptance về việc admin login không đọc raw trade phải đổi và tách thành một WBS bảo mật khác. Không chọn vì còn nguyên vi phạm đã được E2E tái hiện.
3. **C — Thu hồi `trade` khỏi role chung nhưng giữ membership admin-web.** Hẹp hơn, nhưng không tách được các quyền khác mà admin đang kế thừa, và migration mới có thể âm thầm mở lại quyền. Không chọn vì không khớp phạm vi người dùng vừa duyệt.

## Ranh giới kiến trúc được chọn

- Tạo role nhóm `crypidian_admin_web_safe` không đăng nhập, không superuser, không `BYPASSRLS`; `crypidian_admin_web` là login member của role này và không member `crypidian_app`.
- Role safe giữ các quyền ứng dụng admin không thuộc dữ liệu nguồn nhạy cảm. Migration phải xác định quyền tường minh; không sao chép `SELECT` raw trên `trade`, `order_intent`, `bot_decision`, `shared_signal_evidence`, hoặc cột xác suất. Các cột khóa/trạng thái cần cho lifecycle/control chỉ được cấp hẹp sau khi truy vấn cụ thể và kiểm thử chứng minh cần thiết.
- Tạo login `crypidian_admin_report_reader` read-only, không `BYPASSRLS`, secret local riêng; role chỉ được gắn vào admin-web read service. Reader chỉ nhận các cột cần cho dashboard/report đã được liệt kê trong plan. Mỗi query phải thiết lập `app.workspace_id`, `app.membership_id`, `app.bot_id` và để PostgreSQL kiểm tra membership đang ACTIVE cùng role/bot-grant hiện hành trong chính transaction reader; kiểm tra authorization ở application connection chỉ là lớp bổ sung, không thay thế RLS/current-rights check. Không dùng role hiện tại `crypidian_report_reader` nguyên trạng vì role đó có tập quyền đọc/ghi rộng hơn cho Run Report/AI Analyst.
- `admin-web` tạo session factory riêng cho reader; `BotReportingService`, `LiveMonitorService` và các adapter report cần row-level PAPER data chuyển sang factory này. Các thao tác control/auth/config vẫn dùng `crypidian_admin_web_safe`; reader chỉ đọc.
- `AnalystReviewService` và phần đọc AI proposal cũng dùng reader: `analyst_run`/`run_report` được giới hạn theo membership ACTIVE và `workspace:read` hiện hành; `proposal_backtest` được giới hạn theo bot grant `bot:report:read`. Chỉ cấp cột mà trang danh sách/chi tiết dùng, không cấp `analyst_run.raw_stdout`. Việc tạo draft từ proposal phải đọc qua reader rồi kiểm lại `REPORT_READ` và `CONFIG_WRITE` trên admin connection trước khi ghi.
- Operational health dùng function aggregate cố định `wp06_admin_operational_telemetry_snapshot`, không dùng report reader vì `resume_breaker` phải giữ truy vấn risk projection và append breaker/audit trong cùng transaction. Function là `SECURITY DEFINER` có `search_path` cố định, row security bật, owner `crypidian_admin_telemetry_reader` NOLOGIN/NOBYPASSRLS; current membership/role phải khớp route operation (`WORKSPACE_READ` + `bot:read` cho health; `RUNTIME_RESUME` cho resume). Function chỉ trả `OperationalTelemetry`/`LossWindowSnapshot` aggregates đã allowlist, không trả trade IDs, từng lệnh PnL hoặc raw order rows. Safe login chỉ được EXECUTE function và không thể SELECT các bảng/cột nguồn; Python tiếp tục áp dụng domain Circuit Breaker hiện hữu. Cách này phải có parity test với phép tính domain và DB scope gate.
- Giữ ranh giới process role: `TelegramTradeResultWorker` được khởi chạy dưới `signal-worker`, tiếp tục dùng `crypidian_signal_worker` và ACL hiện hành; không gắn reader secret vào container này. Secret mới chỉ mount vào `admin-web` và `db-provisioner` provisioning tạm thời. Telegram send path được regression-test riêng để bảo đảm việc tách admin-web không làm hỏng delivery.
- `LearningPaperPerformanceService` và `ScorerModelCardReadModel` tiếp tục dùng aggregate/read gateways. SQL aggregate/writer chỉ cho phép đúng principal/operation; reader không được execute function mutating hoặc đọc probabilities.
- Các đường đọc nhạy cảm khác phải được chốt rõ trong inventory trước khi thu quyền: `RiskAppetiteService` nhận projection equity và provenance digest không chứa trade/fill IDs; `Flatten` nhận kết quả đối soát/preview hẹp thay vì các raw ledger rows; `AnalystReviewService` kiểm quyền workspace/member hiện hành và phạm vi bot trước khi trả payload; `SignalOutcomeReviewService` chỉ đọc cột projection UI cần. Không cho các đường này fallback sang admin-safe raw SELECT.
- Luồng điều khiển cần kiểm tra trạng thái ledger trước mutation cũng không được giữ raw `SELECT`: `BotLifecycleGuard.ensure_flat_for_disable` dùng projection hẹp đếm vị thế/lệnh/reservation của đúng bot trong cùng transaction sau khóa workspace. Projection DB tự xác minh login admin, membership owner ACTIVE và workspace/bot hiện hành; kết quả chỉ trả tồn tại và ba số đếm, không trả ID hay dữ liệu trade. Denial phải được đổi thành lỗi quyền/không tìm thấy an toàn và được audit như trước; không thay đổi thứ tự khóa hoặc tính atomic của disable.
- Mutation admin tiếp tục ghi `OutboxEvent` nhưng chỉ được `INSERT` trong workspace đang gắn trên transaction. ORM dùng `INSERT ... RETURNING` cho hai cột thời điểm `available_at` và `created_at`, vì vậy role safe chỉ nhận `SELECT` hai cột này kèm policy `SELECT` giới hạn cùng workspace; không cấp đọc cột khác, `UPDATE` hoặc `DELETE` outbox. RLS `WITH CHECK` là bắt buộc cho allowlist insert này.
- Secret reader phải được thêm vào `secret_inventory.py` và test inventory/rotation; reader engine mới phải được dispose cả khi wiring hoặc startup thất bại trước khi lifespan yield.
- Các migration sau ranh giới này phải khai báo rõ grant cho admin-safe group. Thêm kiểm tra migration/unit để bắt grant mới vào `crypidian_app` mà quên đánh giá tác động tới admin-web.

## Ràng buộc và tiêu chí nghiệm thu

- PostgreSQL chỉ có một Alembic head; migration downgrade/re-upgrade bảo toàn role grants, RLS và function ownership.
- Database probe phải chứng minh: `pg_has_role('crypidian_admin_web','crypidian_app','USAGE') = false`; `admin_web` bị từ chối khi select `trade.net_pnl` và `learning_score_event.class_probabilities`; không đọc được raw intent/decision/evidence ngoài allowlist; reader được từ chối INSERT/UPDATE/DELETE và chỉ đọc đúng workspace/member/bot; workspace khác trả 0 hàng; thiếu/thu hồi quyền làm report/gateway fail-closed.
- E2E API/UI phải chứng minh các trang bot report, live monitor/dashboard, settings/lifecycle, scorer history, model card và PAPER report vẫn hoạt động theo quyền của principal; người không có quyền không đọc được qua reader. Không dùng dữ liệu QA để tuyên bố positive source entitlement.
- Playwright chạy lại desktop 1280×800 và mobile 390×844 cho dashboard/report/model-card/operator pages liên quan; không tràn ngang, tiếng Việt đầy đủ, ảnh chụp mới được kiểm tra.
- Unit, PostgreSQL Compose E2E và Playwright liên quan đều đạt 0 skip trước commit; cập nhật plan/WBS, `project-status.md`, module, glossary nếu có thuật ngữ mới, và implementation-memory rồi đọc lại memory.

## Giới hạn đã biết

- Báo cáo UI có thể hiển thị thông tin giao dịch PAPER đã được authorize; ranh giới ở đây bảo vệ credential ứng dụng admin chung và ngăn role đó truy cập trực tiếp dữ liệu nguồn. Reader riêng vẫn phải bị giới hạn theo đúng principal và mục đích.
- TEST dataset hiện có 9 mẫu (3/class), thấp hơn policy 1.000 tổng/100 mỗi class. PAPER rights non-QA và positive eligible cohort chưa có; các điều kiện này vẫn khóa active modes và không được thay bằng fixture QA.
- Cần xác minh mọi luồng admin đang dùng trực tiếp session factory chung trước khi chốt allowlist. Nếu một luồng không thể chuyển qua reader/gateway mà vẫn giữ chức năng, phải bổ sung interface/probe vào plan thay vì cấp lại quyền tổng quát.

## Kết quả builder review

- Reviewer hỏi reader authorization có thể chỉ dựa vào application connection hay phải giữ active-membership/bot-grant enforcement trong DB. Thiết kế chốt DB-enforced: query reader tự kiểm tra membership ACTIVE, owner/risk-admin hoặc bot-grant hiện hành trong transaction reader; app authorization chỉ là lớp bổ sung. Quyết định này giữ đúng ranh giới tenant dù quyền thay đổi sau bước authorize ban đầu.

## Tài liệu và bằng chứng hiện có

- Acceptance PAPER performance: `docs/superpowers/plans/2026-11-13-wp06-paper-outcome-attribution-performance.md`, Task 3, yêu cầu admin không được SELECT raw score probabilities/trades.
- Model card/action latency: `docs/superpowers/plans/2026-11-21-wp06f-model-card-action-latency.md`, Task 2–3, aggregate-only và không cấp quyền source trực tiếp cho admin/history reader.
- Existing role design: `migrations/versions/12e554aace5d_cry421_report_reader_role.py`, `migrations/versions/f0a2c4e6b8d2_wp06_scorer_action_latency.py`, `scripts/provision_database_roles.py`, `compose.yaml`.
- PostgreSQL Compose E2E ngày 2026-11-24: `crypidian_admin_web` có membership `crypidian_app`; `SELECT net_pnl FROM public.trade` thành công; `SELECT class_probabilities FROM public.learning_score_event` bị PostgreSQL từ chối. Aggregate report cross-workspace denial vẫn đạt; reader role chưa tồn tại.
- Bổ sung khi rà plan ngày 2026-11-24: `rg -l 'crypidian_admin_web' migrations/versions` cho thấy 97 file migration có nhắc login này. Đây là số dấu vết source, không phải số ACL đang có hiệu lực; trong đó có grant tường minh vào login và policy RLS gọi tên login. Vì vậy migration phải đối chiếu catalog PostgreSQL hiện hành (trực tiếp/kế thừa, function/sequence, default ACL, policy và ownership), chỉ chuyển allowlist đã xác minh sang safe group, và test downgrade/upgrade theo snapshot. Không thể chỉ bỏ membership `crypidian_app` rồi kết luận đã tách quyền.
- PostgreSQL 17 docs, kiểm tra ngày 2026-11-24: membership có thể kế thừa object privilege và cần được gỡ ở đúng role membership; `SELECT` có thể cấp ở mức cột sau khi thu hồi quyền toàn bảng. Nguồn: https://www.postgresql.org/docs/17/role-membership.html · https://www.postgresql.org/docs/17/sql-grant.html · https://www.postgresql.org/docs/17/ddl-priv.html.
- PostgreSQL 18 docs, kiểm tra ngày 2026-11-24 để khớp Compose PostgreSQL 18.6: membership có option độc lập `INHERIT`/`SET`/`ADMIN`, nên grant nhóm `admin-safe` phải đặt tường minh `INHERIT TRUE, SET FALSE, ADMIN FALSE`; không suy từ role attribute. Nguồn: https://www.postgresql.org/docs/18/role-membership.html · https://www.postgresql.org/docs/18/sql-grant.html.
- Compose DB cô lập theo migration head worktree ngày 2026-11-24 cho thấy `crypidian_admin_web` có 157 ACL relation trực tiếp, 109 ACL theo cột, 29 function grants trực tiếp và membership duy nhất vào `crypidian_app`; `trade`/`order_intent` được kế thừa, còn `bot_decision`, `bot_decision_job`, `shared_signal_evidence`, `run_report` và ledger venue có SELECT trực tiếp. Đây là catalog của Compose head `f0a2c4e6b8d3`. DB review đang chạy ở worktree khác báo revision `b7d2e4f8c1a3`, không có trong graph hiện tại; không dùng snapshot đó để chứng minh migration của nhánh này.
