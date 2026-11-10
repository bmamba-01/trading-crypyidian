# Phân tích khoảng trống WBS và kế hoạch lát tiếp theo

> **Trạng thái cập nhật 04/11/2026:** Task 1–4 của WP-07C ownership fence đã triển khai và kiểm chứng; báo cáo chi tiết ở `.superpowers/sdd/2026-11-04-next-wbs-gap-analysis/`. Tài liệu phân tích không cấp quyền mở connector, Demo hoặc LIVE.

**Mục tiêu:** chọn phần việc kế tiếp từ bằng chứng code/test thực tế, tránh làm lại nền đã hoàn thành và giữ độc lập với luồng dashboard realtime.

**Kiến trúc:** giữ các boundary PostgreSQL/UoW, quyền DB và PAPER replay hiện có. Lát backend đề xuất trước mắt là giữ khóa job xuyên transaction persist của WP-07C; các bước tiếp theo xử lý ngữ nghĩa risk/replay và nối dữ liệu/evaluator theo phụ thuộc.

**Công nghệ:** phiên bản repo hiện ghi Python `>=3.14,<3.15`, SQLAlchemy `2.1.5`, PostgreSQL image `18.6-bookworm`, pytest/Compose; không đề xuất nâng dependency.

**Thiết kế/căn cứ:** [WP-07 WBS](./2026-11-03-wp07-trader-workbench-wbs.md), [plan jobs/admin](./2026-11-04-wp07c-backtest-jobs-admin.md), [project status](../../implementation/project-status.md), các Plan [03](../../implementation/03-binance-execution.md), [05](../../implementation/05-research-ingestion.md), [06](../../implementation/06-ai-learning.md), [07 vận hành](../../implementation/07-operations-release.md) và [memory](../../implementation/implementation-memory.md).

**Ngày đối chiếu:** 04/11/2026. Chỉ đọc repo và tài liệu chính thức; không chạy lại test hoặc thay DB trong vòng phân tích. Mọi con số pass dưới đây là evidence đã ghi trong memory, không phải kết quả mới. Chỉ tạo tài liệu này, không sửa WBS/status/memory, dashboard hoặc code.

## Báo cáo phân tích sâu theo work package

- [WP-03 Binance execution](./2026-11-04-wp03-deep-gap-analysis.md): ưu tiên offline recovery/rehydration, transaction bridge và risk window trước transport; WP-03E Demo vẫn khóa.
- [WP-05 rights và ingestion](./2026-11-04-wp05-deep-gap-analysis.md): rights control plane/repository có nhưng production operator API và connector chưa có; không có provider S1 được duyệt, không coi quyền policy nhập tay là đã xác minh pháp lý.
- [WP-06 evaluation và learning](./2026-11-04-wp06-deep-gap-analysis.md): phân biệt verifier candidate/completeness còn thiếu với fold outcome verifier đã được kiểm thử; có thể tiếp tục offline trên WP-02 outcomes, chưa mở trainer/model.

**Thứ tự thực thi đã chọn:** hoàn tất fence WP-07C Task 1–3 để outcome/job success nguyên tử; sau đó ưu tiên một lát WP-05A control plane chỉ đọc/ghi policy qua actor có thẩm quyền, không fetch network. Giữ WP-06 candidate/split approval ở fixture-only tới khi universe/zero-or-multi outcome policy được chốt. WP-03 risk windows/restart bridge cần quyết định sản phẩm; Demo/private network vẫn là gate riêng. Thứ tự này không biểu thị các nhánh còn lại đã hoàn tất.

## Ràng buộc chung

- Dùng tiếng Việt cho giải thích/comment/docstring, identifier tiếng Anh; thuật ngữ giao dịch/AI giữ theo glossary hiện hành.
- Không sửa các file dashboard realtime; không gộp chức năng ingestion vào “dashboard live snapshot”. Poll dữ liệu đã lưu không chứng minh có market feed tự động.
- Không tạo lệnh sàn, gửi Telegram thật, tải dataset thật hoặc đưa model vào hoạt động trong slice đề xuất.
- `crypidian_trainer_worker` tiếp tục không được SELECT bundle/manifests/OHLCV; không cấp quyền rộng để tiện composition.
- Migration chạy tuần tự, kiểm lại Alembic head tại thời điểm implement; không hardcode parent migration chỉ theo tài liệu này trong worktree có nhiều luồng.
- Trước commit: unit + E2E liên quan phải đạt, lỗi/skip phải được phân biệt, memory được cập nhật/đọc lại. Vòng phân tích này không stage/commit/push/reset/clean.

## Kết luận ưu tiên

Đề xuất lát triển khai độc lập kế tiếp là **WP-07C: bảo đảm worker chỉ commit outcome khi còn sở hữu job, đồng thời commit trạng thái thành công với outcome**. Đây là phần hoàn thiện acceptance lease/reclaim của plan jobs đã có, không phải viết lại queue, CLI hoặc UI. Nó không đòi quyền dataset thật, chính sách giao dịch mới hoặc WP-03/05/06 hoàn tất.

Sau lát này, ưu tiên nền theo `project-status.md` vẫn là thống nhất **equity trong replay và day/rolling loss window** trước khi nâng claim rủi ro hoặc bật Demo; phải tái sử dụng MTM đã có ở MB-04. Nhánh đưa dữ liệu thực vào Workbench cần WP05A/source approval → ingestion composition WP07C → freshness/readiness WP07E; nhánh AI cần WP06.3 completeness/split approval → WP06.4 composition/report → trainer. Những nhánh này không có thứ tự tổng duy nhất và không phải chờ Binance Demo để làm offline.

## 1. Các câu trạng thái đã lỗi thời cần root tích hợp sau

| Vị trí | Nội dung chưa chính xác nếu đọc như hiện trạng | Bằng chứng thay thế và cách diễn đạt đúng |
| --- | --- | --- |
| WBS WP07C đoạn “Đã triển khai một phần”, “service orchestration” và Acceptance | Còn thiếu run persistence, CLI/job lifecycle, UI và E2E bundle→runner. | `application/backtest_dataset_replay_execution.py`, `workers/backtest_replay_worker.py`, `cli/backtest_replay.py`, `admin/backtest_replay_router.py`; memory 04/11 “durable replay jobs, worker CLI và trang admin”: 141 unit, Compose `test_wp07_rest_backfill_persistence.py` 1 pass, Playwright `test_backtest_admin_ui.py` 1 pass. Chỉ scheduler/backfill ingestion product path, macro/raw retention và dataset thật còn mở; replay job đã có. |
| Plan 06 foundation và WP06D evaluator/adapter | Adapter chưa có tests/unit/PostgreSQL E2E. | `tests/unit/test_learning_fold_evaluation.py`, `tests/e2e/test_learning_fold_outcome_verifier.py` và probe `wp06_fold_outcome_verifier.py`; memory 02/11 “WP06.4 unit và PostgreSQL E2E”/“Responsive Admin…” và WBS WP06.4 ghi 74 unit regression + 1 Compose. Còn thiếu composition/report persistence/completeness và các nhánh lỗi DB riêng; không phải thiếu toàn bộ test. |
| Project status phần tóm tắt phase 4/5 | Chỉ có persistence nền; verifier adapter còn thiếu. | Bảng WP05/WP06 cùng tài liệu cập nhật hơn: rights scanner/consumer chạy trong `workers/research_rights_worker.py`, app lifespan; `adapters/database/learning_fold_outcome_verifier.py` đã có. Còn verifier **của DatasetManifestBuilder** và completeness; không nhầm với verifier **fold outcome** đã có. |
| Project status phần WP04/MB07 và thứ tự P4 | “Outbound Telegram Bot API adapter chưa có”. | `adapters/telegram/bot_api_sender.py`, `tests/unit/test_telegram_signal_delivery.py` và `tests/unit/test_telegram_signal_worker.py`; memory 03/11 producer/subscription flow, Compose fixture receipts. Còn việc gửi thật/ma trận retry/dead-letter đầy đủ và accessibility thủ công; không làm lại sender. |
| Project status “chưa có mark-to-market” | Có thể bị hiểu là toàn repo chưa có MTM. | `domain/risk/equity.py::mark_to_market_equity`, `PaperLedgerEquityBasis`, `adapters/database/repositories/account_equity.py` và workspace SQL có seed + realized + partial-exit PnL + open UPNL − chi phí. `test_account_equity.py`, `test_account_equity_repository.py`, `test_workspace_risk_real_paths.py` kiểm domain/ledger/provenance. Gap cụ thể: `runtime_replay_evaluator.py` còn `initial_equity + realized_pnl`; `paper_account_state.py` công khai realized-only; loss counters cộng dồn chưa mang window semantics. |
| Plan 07 vs WBS WP07 | Cả hai dùng WP-07A…E nhưng là hai phạm vi khác nhau. | Gọi **WB07** cho Trader Workbench trong tài liệu này và **OPS07** cho Plan 07 operations/release. Không tính chart, màu PnL hay worker health là hoàn tất restore/incident/Demo readiness. |

Outbound adapter và lifecycle liên kết chat là hai boundary riêng; evidence gửi tin hiện tại vẫn dùng fixture, không chứng minh đã gửi Telegram thật.

## 2. Bảng các work package còn mở

Các đường dẫn `application/`, `domain/`, `adapters/`, `workers/` dưới đây đều nằm trong `src/trading_bot/`.

| WP | Đã có trong code/test | Khoảng trống thực | Phụ thuộc/điều kiện nghiệm thu tiếp |
| --- | --- | --- | --- |
| WP01C/D/E | UoW/RLS/roles, auth/MFA/RBAC, transactional outbox; `test_db_role_least_privilege.py`, auth/outbox suites. | Invitation/MFA recovery, shared limiter, consumer theo nghiệp vụ, restore/migration drill. Outbox framework không đồng nghĩa mọi consumer đã có. | Mỗi domain thêm bảng/worker cần test bằng role thật; OPS07 kiểm vận hành rộng. |
| WP02/MB04 risk + replay | `domain/risk/equity.py`, `repositories/account_equity.py`, `workspace_risk.py`; reducer replay đã quản lý realized/risk reservation. `test_account_equity.py`, `test_workspace_risk_real_paths.py`, `test_runtime_replay_evaluator.py`. | Replay equity chưa gồm UPNL; day/rolling loss counters thiếu duration/reset/timezone/aggregation được pin. PAPER account read-model realized-only là hợp đồng riêng. | Chốt policy rồi tái sử dụng định giá đã có; test partial exits/costs, as-of/freshness, reset/window boundaries. Chặn Demo và metrics claim tương ứng; không chặn chart hiện có. |
| WP03A | `binance_demo_rest.py`, parser regular/Algo, failure taxonomy, target host/account reference; 166 parser/adapter/submission unit ghi trong memory. | Production transport/secret lifecycle, signed readiness/clock/account permissions; xác minh private Demo capability. | Audit Binance hiện hành → fake transport boundary → read-only Demo evidence trước order. Không cần WP05/06. |
| WP03B | `domain/execution/venue_orders.py`, `application/venue_order_reconciliation.py`, `adapters/database/venue_reconciliation_ledger.py`, durable ledger; `test_wp03_venue_order_ledger.py`. | Rehydrate proof sau restart; verifier venue có thẩm quyền; bridge ghi exposure rồi transfer/release reservation cùng transaction; worker composition. | Giữ UNKNOWN; test restart/timeout-after-accept/partial + no double risk release bằng PostgreSQL role. Không “xóa UNKNOWN” để bật Demo. |
| WP03C/D/E | Parser stream/algo offline và account-activity guard có thể tái sử dụng. | User-stream supervisor, stop/partial protection, startup/reconnect reconcile, readiness runbook, prospective Demo. | WP03A/B + risk-window policy → protection → reconcile → Demo run. Demo credentials và quyền account thật là blocker của network stage, không blocker offline code. |
| WP04/MB07, MB08 | Authenticated admin/CSRF/owner controls, Telegram link lifecycle, responsive browser flow. | Manual screen-reader review, natural partial-fill integration; MB08 worker/research/learning scopes chưa hoàn tất. | Không đòi viết UI lại. Partial-fill phụ thuộc execution producer; MB08 phụ thuộc contracts thật của WP05/06. |
| WP05A/B | `application/source_registry.py`, policy/quota persistence, Principal permissions; `test_wp05_source_registry.py`. | HTTP/composition và một source connector S1 được duyệt, quota/fetch runtime. | Chọn purpose/provider/evidence/retention/approver; dùng fixture để phát triển, không giả grant thật. |
| WP05C/G | Metadata revision/takedown + access envelope; scanner/rights consumer đã chạy. `test_wp05_source_items.py`, `test_wp05_rights_change_outbox.py`, `test_wp05_expiry_scanner.py`, `test_wp06_rights_invalidation.py`. | Blob/artifact erasure, propagation tới mọi consumer và grant thật. Event lịch sử trước G2 không được tự suy backfill. | Có storage contract mới mở body/excerpt; đồng bộ rights gate vẫn chặn khi scanner trễ. Không thêm worker rights thứ hai. |
| WP05D/E/F/H | Chỉ contract/plan cho macro/news/attention/opinion/as-of context. | Parser/extractor, source context provider và evidence integration. | WP05A/B/C + nguồn cụ thể; macro point-in-time mới giúp H06; coverage thiếu phải unknown. |
| WP06.1/.2 | Scorer/abstain preflight, bounded DSL/hypothesis + unit. | Actual model adapter/runtime composition và reviewed experiment execution. | Không nối model vào planner trước manifest/evaluation/approval; không network/model requirement cho offline contracts. |
| WP06.3 | DatasetManifestBuilder verifier port, immutable manifest v1/v2/v3, source reverse refs; `test_learning_dataset_application.py`, `test_wp06_dataset_manifest.py`. | Authoritative dataset-sample verifier khác fold verifier, query completeness, zero/multi-trade sample representation, split approval/holdout access. | WP02 persisted feature/outcome; WP05 rights khi source-dependent. Caller tự chọn list sample không chứng minh completeness. |
| WP06.4 | Fold selector/interval purge, DatabaseFoldOutcomeVerifier read-only snapshot, report object/hash; `test_learning_fold_evaluation.py`, `test_learning_fold_outcome_verifier.py`. | Composition UoW + gate + report persistence, trial-family/fold accounting; report aggregate chỉ link không verify metrics/hash; một sample chỉ một persisted trade. | Có thể làm diagnostic fixture slice trước trainer, nhưng không gọi đó là dataset đủ điều kiện train/promotion khi .3 chưa đạt. Không đọc holdout ngoài fold để dễ tính aggregate. |
| WP06.5–.9 | Chưa có trainer/registry/shadow/drift pipeline sản phẩm; G1/G2 rights foundation đã có. | Reproducible trainer/artifact, approval registry, shadow/PAPER rollback, drift/retrain proposal, integration. | .3/.4 → .5 → .6 → .7 → .8; quyền data/holdout/reviewer gates giữ xuyên chuỗi. |
| WB07A | Chart/report marker/panel/list 50 trade, provenance, responsive + Playwright hover. | Các lựa chọn/chart coverage còn lại phải kiểm theo trường thật; live dashboard đang do luồng riêng sở hữu. | Không coi tooltip hoặc polling là ingest/venue data; không đụng files dashboard trong plan này. |
| WB07B | Producer composition, opt-in/filter API/HTML, Bot API sender/delivery worker, claim-time receipt filter. | Automatic market-feed, delivery history, quiet hours/cooldown/daily cap, fault matrix retry/dead-letter. | WP05 rights + market feed deployment cho nguồn thật; sender fixture/fault coverage có thể độc lập. |
| WB07C | REST paginator/cursor atomic, rights gate, manifest/bundle persistence, observations/context/runner, execution service, durable jobs/CLI/MFA/admin. `test_wp07_rest_backfill_persistence.py`, `test_backtest_outcomes_persistence.py`, `test_backtest_admin_ui.py`. | Persist ownership race; backfill ingestion chưa có production caller, scheduler, raw reference/retention, macro, initial non-flat state, fill/funding fidelity, granted real dataset. | Khóa persist trước mở nhiều worker; source approval → ingestion worker → manifest/bundle → replay đã có. Không nhầm replay job với backfill job: `BinanceRestBackfillIngestionService` chỉ được gọi từ E2E probe hiện tại. |
| WB07D | Report v2 + realized calendar/metrics/quote partition và màu ngữ nghĩa. | CSV/filter/range comparison, journal/tag audit, MFE/MAE/MTM/market context. | CSV có thể dựa report đã có; MTM analytics phải chờ semantics/valuation đúng; journal storage riêng khỏi immutable trade ledger. |
| WB07E | Một số role có worker health/tick, đặc biệt rights maintenance. | Cross-worker freshness/coverage/rights/queue lags + reason-coded readiness/runbook. | Có deployment contract cho producer/sender/ingestion trước đo; không đủ chỉ DB ping. |
| OPS07A–E | SBOM/lock/basic Compose checks và một số health nền. | Correlation/metrics, circuit-breaker drills, encrypted backup/restore/RPO-RTO, egress/security review, Demo readiness dossier. | Có thể chuẩn bị restore/observability độc lập, nhưng readiness tổng cần các module và Demo evidence. Chưa được tính “hoàn thành” từ WB07. |

## 3. Thứ tự phụ thuộc và thứ tự làm đề xuất

| Mức | Việc | Lý do thứ tự và phần có thể làm ngay |
| --- | --- | --- |
| 0 | Đồng bộ nhận thức bằng evidence mới; giữ dashboard realtime độc lập. | Các câu stale có thể làm lặp lại nhiều ngày công. Tài liệu này không chỉnh status hộ root. |
| 1 | WB07C khóa quyền persist + kết quả job atomic, phạm vi task bên dưới. | Dependency đều hiện hữu; đóng giới hạn được memory công khai; tăng độ tin cậy pipeline đã có trước scheduler/multi-worker. Không chờ provider/risk decision. |
| 2A | WP02 replay valuation alignment + day/rolling risk policy. | Ưu tiên P1 hiện hữu; cần policy decision trước coding thay risk. Reuse MB04 MTM, giữ report realized semantics/version. Là dependency của WP03 Demo và WB07D metric mới. |
| 2B | WP05A authenticated registry workflow → WB07C bounded ingestion orchestration. | Không có production callsite ingestion dù replay đã chạy; cần source approval cho chạy thật. Có thể implement fixture composition trước grant; giữ connector disabled. Sau đó WB07E freshness/coverage và WB07B auto feed. |
| 2C | WP06.3 authoritative sample/completeness/split → WP06.4 evaluator composition và report bất biến. | Tận dụng persisted backtest outcomes, không cần WP03. Report contract có thể thử fixture ngay; trainer phải chờ cả .3/.4. |
| 2D | WP03A.2 + B restart/risk bridge offline. | Có thể độc lập WP05/06; không mở private orders. Sau đó C protection + D reconcile, chờ 2A để E Demo. |
| 3 | WB07B delivery policies, WB07D CSV/report filters, WP04 manual accessibility. | Không phải dependency kỹ thuật của backend ở mức 1; thực hiện theo nhu cầu sản phẩm. Dashboard realtime riêng không bao gồm các feature này mặc nhiên. |
| 4 | WP05 macro/news/opinion, WP06 trainer/registry/shadow/drift, OPS07 full readiness. | Chỉ sau nguồn/manifest/evaluation/runtime tương ứng. Không dùng một checklist chung để mở tất cả. |

Scheduler không phải dependency của một replay job do owner tạo: loop worker + `--once` đã tồn tại. Có thể giữ scheduler ngoài scope cho tới khi có yêu cầu lịch chạy.

## 4. Deep dive WB07C: khóa job xuyên transaction persist

### Bằng chứng và failure cần đóng

- `BacktestDatasetReplayExecutionService.execute()` mở persist UoW, revalidate rights rồi gọi `persist_guard()` không có tham số UoW.
- Closure `verify_persist_lease()` trong `workers/backtest_replay_worker.py` gọi `_update_progress()`, và phương thức này mở **read-role UoW khác**. Guard trả về sau commit của transaction đó; không có job-row lock nằm trong transaction outcome.
- `tests/unit/test_backtest_dataset_replay_execution.py::test_execution_checks_worker_lease_inside_persist_transaction` kiểm thứ tự callback, không chứng minh cùng connection/transaction. Probe `wp07_backfill_persistence.py` kiểm stale generation không update progress sau reclaim; chưa pause worker sau guard rồi đối đầu outcome commit.
- `finish(SUCCEEDED)` cũng nằm ở UoW riêng sau outcome commit. Crash giữa hai bước để job RUNNING dù run đã lưu; retry idempotent giảm trùng dữ liệu nhưng chưa bảo đảm quyền sở hữu tại commit.
- Plan jobs “Review Focus” yêu cầu hai worker không xử lý đồng thời và restart/reclaim an toàn; memory 04/11 ghi rõ long persist transaction chưa được khóa. Lát này đảm bảo **không có hai owner commit outcome cho cùng job**. Không tuyên bố ngăn tuyệt đối hai CPU executor chạy chồng khi worker bị pause và lease đã hết; lease không thể chứng minh điều đó.

### Mục tiêu và ngoài phạm vi

**Mục tiêu:** worker phải khóa hàng job bằng chính session persist trước append; kiểm workspace, owner, generation, RUNNING, cancel và lease bằng DB clock. Giữ khóa đến commit/rollback; ghi SUCCEEDED/result IDs cùng transaction với mọi outcome/rights receipt. Khi mất lease trước khóa, ghi 0 outcome; khi crash/rollback, job không thành công và có thể reclaim sau lease.

**Ngoài phạm vi:** đổi strategy/risk/valuation, raw data ingestion, scheduler, UI/polling, provider grants, Telegram/live/Demo, đổi nghĩa outcome hash, backfill receipt cho legacy runs, chống người đã chiếm DB credential tự đặt workspace GUC. Direct execution service không chạy từ queue vẫn có mode riêng tường minh; job worker không được bỏ qua guard.

### Quyết định kỹ thuật đề xuất

1. Khóa row job ở trainer persist session qua một capability DB hẹp. Không dùng khóa trên read-role connection song song: connection đó chết có thể nhả khóa trước outcome transaction.
2. Dùng hai function PostgreSQL schema-qualified: `begin_backtest_job_persist(workspace_id uuid, job_id uuid, worker_id text, generation integer) returns void` và `complete_backtest_job_persist(workspace_id uuid, job_id uuid, worker_id text, generation integer, result_run_ids uuid[]) returns void`. Tên là đề xuất mới, chưa có trong repo.
3. Function begin kiểm GUC workspace khớp tham số; SELECT FOR UPDATE đúng job, rồi DB-clock lease validity, owner/generation/status/cancel; invalid thì raise reason code ổn định. Complete kiểm cùng identity/status; đặt terminal, progress 100, result IDs và clear lease trong cùng transaction. Sau begin thành công, lease hết theo wall-clock không cho worker khác reclaim vì row còn khóa; không tự đánh fail sau khi đã có khóa chỉ do persist kéo dài.
4. Owner function riêng NOLOGIN/NOSUPERUSER/NOBYPASSRLS, không role membership; cấp SELECT các cột cần và UPDATE cột trạng thái cần, RLS workspace, `search_path=pg_catalog, pg_temp`, revoke PUBLIC EXECUTE. Trainer chỉ EXECUTE hai function, không SELECT job payload/market data. Kiểm lại trigger `guard_backtest_replay_job_update` chấp nhận cập nhật hạn chế. Function là capability cho application đáng tin cậy, không tuyên bố chống trainer credential độc hại giả tuple identity.
5. Trong job path: lock → revalidate current rights → append children/receipts → complete job → commit. Replay vẫn chạy ngoài DB transaction. Nếu một child lỗi, rollback cả complete/job/outcomes.
6. Dừng và await monitor heartbeat trước vào persist lock; monitor dùng session khác mà chờ row lock có thể tự gây deadlock/liveness failure. Không gọi `_update_progress` hoặc `_finish(SUCCEEDED)` bằng read-role session bên trong/sau success transaction. Failure trước begin/rollback vẫn dùng conditional finish hiện hữu; stale owner không sửa terminal.
7. Giữ khóa theo thứ tự job trước rights/outcome; không lấy job lock từ đường rights worker. Test timeout/cancel để không giữ transaction vô hạn; timeout thực tế cần chọn theo measured fixture/load, không tăng tùy tiện để né lỗi.

### Task 1 — Capability PostgreSQL và repository persist

**Files:** tạo `src/trading_bot/adapters/database/repositories/backtest_job_persistence.py`; tạo migration additive `migrations/versions/<revision>_backtest_job_persist_fence.py` theo head khi thực thi; test mới `tests/unit/test_backtest_job_persistence.py`, `tests/e2e/probes/wp07_backtest_persist_fence.py`, `tests/e2e/test_wp07_backtest_persist_fence.py`.

**Interface:** `BacktestJobPersistenceRepository(session: AsyncSession)`; `async begin(*, workspace_id: UUID, job_id: UUID, worker_id: str, generation: int) -> None`; `async complete(*, workspace_id: UUID, job_id: UUID, worker_id: str, generation: int, result_run_ids: tuple[UUID, ...]) -> None`. Cả hai không tự commit, dùng session caller. Session sai workspace hoặc token hết hạn bị từ chối.


**Điều chỉnh từ probe PostgreSQL 04/11/2026:** migration legacy `e18f35a7c902` đang cấp trainer SELECT bốn bảng `market_instrument`, `market_bar`, `market_funding`, `market_open_interest`; probe role thật phát hiện giả định “trainer không đọc market” trước đây chưa đúng. Migration Task 1 thu hồi chính bốn grant này để trainer chỉ persist và worker đọc riêng tiếp tục dùng `crypidian_backtest_worker`; downgrade khôi phục đúng bốn SELECT legacy. Cập nhật matrix least-privilege tương ứng, kiểm denial cả bốn bằng trainer thật và kiểm restore/revoke qua downgrade/re-upgrade. Không mở quyền đọc payload/bundle/manifest hoặc nới privilege nào.

- [x] RED/GREEN unit: cùng session, parameter binding, begin/complete order, result UUID validation; tuple rỗng bị từ chối.
- [x] PostgreSQL roles thật/two tenants: thiếu hoặc sai workspace/owner/generation/lease/cancel không ghi; complete chỉ hợp lệ sau begin trong cùng transaction.
- [x] Migration + repository: owner/function grants/RLS/search_path tối thiểu; downgrade giữ dữ liệu và khôi phục bốn SELECT market legacy, re-upgrade thu hồi lại.
- [x] Compose GREEN: trainer gọi capability nhưng không SELECT job payload/manifest/bundle/market hoặc direct UPDATE; app/replay/admin/PUBLIC bị chặn; denial kiểm bằng login role thật.

### Task 2 — Execution service dùng cùng persist UoW

**Files:** sửa `application/backtest_dataset_replay_execution.py`; tạo `application/backtest_job_persistence.py`; sửa `tests/unit/test_backtest_dataset_replay_execution.py`.

**Interface mới:** immutable `BacktestJobPersistToken(workspace_id: UUID, job_id: UUID, worker_id: str, generation: int)`; protocol `ReplayJobPersistencePort.begin(token) -> Awaitable[None]`, `.complete(token, result_run_ids: tuple[UUID, ...]) -> Awaitable[None]`; `JobPersistenceFactory = Callable[[ReplayExecutionUnitOfWork], ReplayJobPersistencePort]`. `execute(..., job_token: BacktestJobPersistToken | None = None)` chỉ nhận token khi constructor có factory. Bỏ callback `persist_guard` cho queued path và cập nhật toàn bộ callsite/test của callback cũ.

- [x] RED/GREEN `test_job_fence_uses_outcome_session_and_precedes_rights_check`: begin/rights/append/complete cùng persist UoW theo thứ tự.
- [x] RED/GREEN stale owner: begin bị từ chối thì không append/complete; caller rollback.
- [x] RED/GREEN child failure: rollback outcome/job completion; retry cùng run IDs idempotent.
- [x] RED/GREEN token factory/workspace validation; đường direct không token giữ regression.
- [x] Compose service→DB: outcome/receipt/job SUCCEEDED cùng transaction; lỗi/cancel/race không để lại ghi một phần.

### Task 3 — Worker chuyển quyền từ heartbeat sang persist lock

**Files:** sửa `workers/backtest_replay_worker.py`; thêm `tests/unit/test_backtest_replay_worker.py` nếu chưa có tại lúc thực thi, hoặc mở rộng file đó; mở rộng probe task 1.

**Consumes:** token/factory task 2, repository task 1, claim hiện có. **Produces:** worker `_process` queued path luôn truyền token; phase persist dừng/await monitor rồi để execution service khóa/complete bằng persist UoW. Kết quả service commit là evidence success; không gọi finish success transaction thứ hai.

- [x] RED/GREEN unit: monitor được await dừng trước begin; không có session heartbeat/progress thứ hai trong lúc persist; failure cleanup không che lỗi gốc.
- [x] RED/GREEN lease/reclaim: lease cũ bị DB từ chối trước append và không làm đổi generation mới.
- [x] PostgreSQL barrier race: A giữ persist lock, B không reclaim; A commit thì job SUCCEEDED không bị reclaim.
- [x] PostgreSQL race ngược: B reclaim generation mới trước begin của A; A bị chặn, B hoàn tất.
- [x] PostgreSQL rollback/failure: lỗi giữa child rows rollback toàn bộ; retry generation mới hoàn tất với run IDs idempotent.
- [x] PostgreSQL cancel/terminal: cancel trước begin tạo 0 outcome; sau persist không đổi SUCCEEDED/receipt.

### Task 4 — Hồi quy và handoff evidence

**Files:** tests task 1–3; ở vòng implementation root mới cập nhật memory/status/WBS. Không chạm dashboard realtime.

- [x] Unit full regression hiện tại: **2669 passed**; focused fence/execution/worker regression cũng đạt.
- [x] Compose fencing E2E **1 passed / 21.91s**; backfill worker **1 passed / 15.82s**; outcome persistence **1 passed / 27.76s**; DB role least-privilege **1 passed / 32.05s**.
- [x] Migration upgrade/check, downgrade/re-upgrade, một Alembic head, Ruff/format/mypy và `git diff --check` đạt; commands/results ghi ở task reports và implementation memory.
- [x] Cập nhật memory/status/WBS và đọc đối chiếu sau ghi. WP-07C vẫn còn scheduler, market-data rights/source và macro context; không tuyên bố toàn bộ WP07C hoàn tất.

## 5. Rủi ro và kiểm chứng chính thức

Các nguồn PostgreSQL sau được mở và kiểm tra **04/11/2026**, theo major 18 mà Compose đang pin. Không đổi API/library version trong vòng phân tích.

| Vấn đề | Nguồn chính thức và ảnh hưởng |
| --- | --- |
| Row lock tồn tại đến cuối transaction; deadlock/liveness | [PostgreSQL 18 Explicit Locking](https://www.postgresql.org/docs/18/explicit-locking.html). Thiết kế suy ra: phải lấy lock bằng persist transaction; thứ tự khóa thống nhất, không gọi heartbeat session khác trong transaction đó. |
| SKIP LOCKED và SELECT FOR UPDATE cần quyền phù hợp | [PostgreSQL 18 SELECT](https://www.postgresql.org/docs/18/sql-select.html), [GRANT](https://www.postgresql.org/docs/18/sql-grant.html). Không giả rằng SELECT-only trainer tự khóa được job; cần capability hẹp và test catalog/real role. |
| RLS/owner bypass, SECURITY DEFINER | [Row Security](https://www.postgresql.org/docs/18/ddl-rowsecurity.html), [CREATE FUNCTION](https://www.postgresql.org/docs/18/sql-createfunction.html). Function owner tối thiểu, NOLOGIN/NOBYPASSRLS, workspace check và RLS; revoke PUBLIC, cố định search_path. Không mở SELECT payload cho trainer. |
| Dataset grant thật | Trước 2B: kiểm lại [Binance Dataset Terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md) và thỏa thuận áp dụng cho account/provider/purpose. Vòng này **không tái xác minh điều khoản**; sử dụng trạng thái deny hiện có của repo. Technical endpoint docs không cấp quyền training/product/redistribution. |
| Binance host/schema/rate/algo/Demo support | Trước WP03 transport hoặc network stage: kiểm [USDⓈ-M General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info), [Change Log](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log) và endpoint cụ thể. Vòng này không dùng lại audit cũ như chứng nhận Demo hiện hành. |
| Telegram delivery policy | Trước mở rộng sender network: [Bot API](https://core.telegram.org/bots/api), [FAQ limits](https://core.telegram.org/bots/faq). Tách retry có side effect chưa rõ khỏi lỗi chắc chắn; không claim external exactly-once từ receipt DB. Chưa tái kiểm trong vòng này. |
| Tải lớn/persist transaction dài | Đo count child/trade và thời gian locks với fixture nhiều observations trước đặt timeout; test rollback lúc hết timeout. Không giữ transaction trong CPU replay. |
| Rights đổi đồng thời persist | Giữ revalidation hiện hữu sau job lock; race policy-revoke với outcome commit cần test và một quyết định serialization riêng nếu yêu cầu deny đồng thời tuyệt đối. Job lock không tự khóa rights row, không quảng cáo xử lý race ngoài scope. |

## 6. Những câu hỏi thực sự chặn implementation

**Lát WB07C ở mức 1:** chưa thấy thông tin từ người dùng bắt buộc thiếu; có thể triển khai fixture/offline theo thiết kế trên sau root review. Cần xác minh bằng DB probe rằng role/function/trigger/RLS tương thích; đây là việc kỹ thuật task 1, không phải lý do hỏi lại người dùng. Chốt migration parent theo actual head là coordination của root.

**Các nhánh sau có quyết định ngoài code cần chốt:**

1. Risk-window: day theo UTC hay timezone cấu hình, rolling duration, loss là tổng phần âm hay net PnL, cost/funding ghi ở thời điểm nào, partial exit có tính trước trade close không, equity basis tại reset/now và behavior khi lịch sử chưa đủ. Đây chặn thay risk gate/Demo, không chặn sửa persist ownership.
2. Dataset/provider: nguồn S1 nào, scope/purpose thực tế, ai có thẩm quyền approve và evidence grant nào còn hiệu lực; quota/retention có thể dùng. Thiếu trả disabled; không cần hỏi để viết fixture composition.
3. WP06 split/holdout: ai approve split/query universe, quyền và lần truy cập holdout được khóa thế nào, sample zero/multi-trade biểu diễn ra sao. Thiếu không được nghiệm thu trainer/promotion; không cản unit verifier/fold diagnostic.
4. Demo stage: account/key Demo chuyên dụng, allowed symbols/caps và người duyệt readiness. Chỉ hỏi khi offline gate đã đủ và chuẩn bị network run cụ thể; không yêu cầu credential trong chat.

Không có câu hỏi chặn về palette, dashboard polling, framework mới, scheduler hay lựa chọn AI model cho slice trước mắt.

## 7. Tự rà soát và trạng thái tài liệu

- [x] Đọc AGENTS, skill dự án, memory gần nhất, project status, WBS WP07 và modules 03/05/06/07; đối chiếu callsites/tests.
- [x] Phân biệt contract, persistence, composition, fixture E2E và network evidence; ghi nguồn cũ chưa tái kiểm.
- [x] Không đề xuất làm lại MTM MB04, rights maintenance worker, Telegram sender hoặc replay CLI/admin.
- [x] Map slice đề xuất về acceptance/giới hạn WB07C jobs; không nhầm với OPS07.
- [x] Task có file/interface/unit/Compose acceptance; năm failure chính được bao phủ: stale generation, pause trong persist, rollback giữa children, cancel race, role/tenant misuse.
- [x] Chỉ tạo tài liệu này; không triển khai, không test runtime, không sửa memory/status/WBS gốc hoặc file dashboard.
