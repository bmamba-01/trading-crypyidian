# Project plan và trạng thái triển khai

**Cập nhật:** 2026-10-24 (Asia/Saigon)  
**Nhánh:** `dev`  
**Phạm vi:** repo bot Binance Futures độc lập; không tính mã Freqtrade upstream là implementation của sản phẩm.

Tài liệu này là bảng theo dõi trạng thái có bằng chứng. Plan thiết kế và acceptance criteria đầy đủ nằm trong [bản đồ implementation](./README.md), [kế hoạch tổng thể](../superpowers/plans/2026-10-21-independent-binance-futures-bot.md) và các plan module liên kết bên dưới.

## Quy ước trạng thái

| Trạng thái | Ý nghĩa |
| --- | --- |
| **Hoàn thành** | Đầu ra trong phạm vi đã có và các kiểm tra được yêu cầu đã chạy đạt. |
| **Hoàn thành phần nền (MVP)** | Có khung hoặc lát cắt nền chạy được; các chức năng sản phẩm còn lại được liệt kê rõ. |
| **Đang triển khai** | Đã có code/tài liệu một phần, còn tiêu chí nghiệm thu trong phạm vi chưa đạt hoặc chưa được kiểm chứng. |
| **Chưa bắt đầu** | Mới có thiết kế/kế hoạch; chưa có implementation của module. |
| **Bị chặn** | Không thể tiếp tục vì phụ thuộc ngoài chưa sẵn sàng. Không có plan nào hiện được đánh dấu trạng thái này. |

Không cộng tỷ lệ phần trăm tổng dự án: các phase có khối lượng khác nhau và chưa có estimate đã duyệt. Trạng thái MVP nền tảng không đồng nghĩa với bot đã có thể tạo quyết định giao dịch hoặc gửi lệnh.

## Tóm tắt điều hành

| Giai đoạn | Trạng thái | Kết quả hiện có | Điều kiện để chuyển giai đoạn |
| --- | --- | --- | --- |
| 0. Nền tảng và database | **Hoàn thành phần nền (MVP)** | Package/config Python; Compose và process boundary; PostgreSQL schema, migration, workspace isolation; owner auth/MFA/RBAC; audit và transactional outbox. Unit và Compose E2E cuối cùng đạt. | Bổ sung readiness nối dependency, quản trị thành viên/invitation/recovery, limiter dùng chung, worker outbox thực tế và kiểm thử bảo mật/tải theo nhu cầu triển khai. |
| 1. Trading core và PAPER | **Hoàn thành WP-02 trong phạm vi PAPER/replay MVP** | H03 Compose lifecycle; active trade pinning; strategy parameters từ root settings; reducer account progression; PP6 hai race order target và RP4 persistence target; final full suite và independent review đạt. | Chưa có mark-to-market; day/rolling risk-window semantics còn cần hoàn thiện. Market dispatcher và Binance execution vẫn disabled; strategy vẫn là hypothesis chưa xác nhận profitability. |
| 2. Binance Demo execution | **Chưa bắt đầu** | Chưa có adapter thực thi, state machine lệnh hoặc Demo contract run. | PAPER contract ổn định; hoàn thành Plan 03 và reconcile/partial-fill/protective-order E2E trên Demo. |
| 3. Admin web và Telegram | **Đang triển khai một phần** | Có API nền auth/bootstrap, RBAC, MFA và risk profile draft trong WP-01D; chưa có admin UI, user/config CRUD, dashboard hay Telegram. | Hoàn thành Plan 04, gồm UI và webhook/command E2E. |
| 4. Research ingestion | **Chưa bắt đầu** | Có backlog/thiết kế nguồn và quyền sử dụng; chưa có registry/fetcher/normalizer/news/attention/trader-opinion pipeline. | Hoàn thành Plan 05 với lineage, quyền dữ liệu và point-in-time tests. |
| 5. AI learning có kiểm soát | **Chưa bắt đầu** | Có design/guardrails; chưa có scorer, dataset, trainer, model registry hay drift loop. | Hoàn thành Plan 06, temporal evaluation và promotion/rollback gates. |
| 6. Operations và release | **Chưa bắt đầu** | Có các kiểm tra nền trong Compose; chưa có observability/incident/restore drills hoặc Demo readiness dossier. | Hoàn thành Plan 07; không bật LIVE nếu thiếu phê duyệt readiness riêng. |

**Tình trạng sản phẩm:** hiện là nền tảng chạy local/PAPER, chưa phải trading bot hoàn chỉnh. Không có Binance API key, không gửi lệnh lên Binance và LIVE bị khóa.

## Project plan chi tiết theo work package

| ID | Work package | Trạng thái | Đã triển khai | Còn lại / điều kiện nghiệm thu | Kiểm chứng gần nhất |
| --- | --- | --- | --- | --- | --- |
| WP-01A | Quy ước repo, Python package, dependency và quality tooling | **Hoàn thành** | Python package `trading_bot`, cấu hình PAPER mặc định, lock/dependency inventory/SBOM, settings, health endpoint và quy ước ngôn ngữ/glossary. | Cài mới sạch từ lock trên môi trường khác và giữ inventory cập nhật khi dependency đổi. | Regression hiện tại: 368 unit + 9 E2E pass; Ruff, format, mypy đạt. |
| WP-01B | Compose, process boundary, secrets và image E2E | **Hoàn thành** | Các role Python dùng chung một app image; gateway giữ boundary secret sàn; healthchecks, volume persistence và image tag theo hash build input. Dọn 149 tag E2E mồ côi và 3 image trung gian không còn dùng; không chạy global prune. | Khi Dockerfile COPY thêm tệp, cập nhật danh sách hash input; dọn tag cũ theo tham chiếu container; thiết lập readiness đầy đủ khi worker nghiệp vụ được thêm. | Regression hiện tại: 368 unit + 9 E2E pass; targeted Compose boundary chạy lại 4 pass; Ruff/mypy đạt. |
| WP-01C | Schema, migrations, workspace isolation/RLS, DB roles | **Hoàn thành phần nền (MVP)** | PostgreSQL schema/migrations, numeric/Decimal, DB roles riêng, tenant scope/RLS và migration upgrade/downgrade/re-upgrade. | Kiểm tra quyền chi tiết cho các domain mới khi Plan 02–06 thêm bảng; migration rollout/restore production chưa diễn tập. | E2E suite tích hợp hiện tại 5 passed; memory WP-01C ghi nhận schema, RLS, migration và persistence checks. |
| WP-01D | Authentication, RBAC, MFA và secret boundary | **Hoàn thành phần nền (MVP)** | Owner bootstrap, TOTP, Argon2id, session/CSRF/logout, RBAC ở server, risk profile draft, local AES-GCM MFA secret provider; volume provisioner chạy lặp an toàn. | Quản trị thành viên/invitation, khôi phục MFA, secret sàn lifecycle, rate limiter chia sẻ giữa replica, đầy đủ ma trận permission UI. | Regression hiện tại: 368 unit + 9 E2E pass, gồm auth flow; Ruff/mypy đạt. |
| WP-01E | Unit of Work, audit và transactional outbox | **Hoàn thành phần nền (MVP)** | Transaction/workspace context, audit append-only, event envelope kiểm tra secret, outbox claim/lease/retry/dead-letter và receipt. | Chưa có worker consumer nền/fan-out; cần mở rộng concurrency/fault tests khi consumer nghiệp vụ được xây; exactly-once side effect ngoài DB không được bảo đảm. | Regression hiện tại: 368 unit + 9 E2E pass trên stack tích hợp. |
| WP-02A | Hợp đồng, persistence và Binance public market-data | **Hoàn thành phần nền (MVP)** | Domain contract, quality/as-of, market instrument/bar/funding/OI/raw/cursor/gap persistence và Binance USDⓈ-M read-only adapter. | Cần tiếp tục xác nhận cursor/backfill và coverage ở production; historical OI có giới hạn retention/coverage từ nguồn. | Domain/repository/adapter unit tests; offline ingestion E2E nằm trong final full suite **13 passed / 381.82s**. |
| WP-02B | Formula features và immutable snapshot | **Hoàn thành phần nền (MVP)** | Indicator formulas, regime/quality snapshot với schema/formula/code hash, source lineage, as-of, idempotent persistence, hash verification và DB immutability/RLS. | Cần thêm reference golden datasets và semantic review trước khi xem feature là production-grade. | `tests/unit/test_market_features.py`, `test_feature_snapshot.py`, `test_feature_snapshot_repository.py`; Compose snapshot as-of/hash/immutability probe pass. |
| WP-02C | Chart geometry, strategy registry và baseline hypotheses | **Hoàn thành phần nền (MVP)** | Candidate/NoTrade contracts; H01 trend, H03 breakout, H06 mean-reversion; confirmed geometry, recipe, release/evidence lineage, freshness/no-lookahead gates. H03 runtime áp dụng parameters đã publish. Các release vẫn experimental/unvalidated. | Chưa được tuyên bố về edge/lợi nhuận; các release cần đánh giá point-in-time và sensitivity trước promotion. | Strategy geometry/hypothesis/registry/decision-cycle tests nằm trong full unit **520 passed**. |
| WP-02D | TradePlanner, RiskEngine và atomic reservation | **Hoàn thành phần nền (MVP)** | Decimal plan, cap/exposure/margin/data/config gates; persisted risk decision, reservation expiry/reconcile/idempotency; account locking và kiểm tra aggregate risk/margin ở PostgreSQL. H03 dùng candidate thật tới reservation. | Tiếp tục nối reconcile/order unknown vào runtime worker khi WP-03 adapter được xây. | Compose concurrency E2E và `tests/e2e/test_reservation_expiry.py`: reservation hết hạn được giải phóng, UNKNOWN vẫn bị giữ; migration downgrade/upgrade và `alembic check` pass. |
| WP-02E | PAPER gateway, position monitor, ledger và restart recovery | **Hoàn thành phần nền (MVP)** | Checkpoint schema v2, UoW, recovery/bootstrap; active order/position giữ pinned execution profile và strategy/risk lineage; strategy-only setting publish không làm đổi policy cũ. PP2 focused **17 unit passed**; PP6 target Compose kiểm tra provider/settings mới và pin active state. | `market_dispatch=not_configured`, `exchange_execution=disabled`; chưa có dispatcher tự chạy, venue reconcile, Telegram/operator controls hay multi-workspace worker. | Final full suite **520 unit + 13 E2E**; PP6 target **1 passed / 109.47s**. |
| WP-02F | Replay/backtest và đánh giá leakage/cost | **Hoàn thành phần nền (MVP)** | Deterministic replay, leakage/time split, metrics/uncertainty, persistence; reducer sở hữu realized equity/exposure/reservation progression, provider cấp base state và market/execution facts. RP4 multi-observation Compose và 30 unit regression đạt; hash determinism được xác nhận bằng replay lại không restart worker. | Chưa có mark-to-market; day/rolling risk-window semantics cần hoàn thiện. Funding/spread stress fail-closed; không phải bằng chứng profitability. | RP4 Compose **1 passed / 57.69s**; final full E2E **13 passed / 381.82s**. |
| WP-02G | Root WorkspaceSettings và Admin API | **Hoàn thành phần nền (MVP)** | Versioned root settings dùng chung cho market/features/strategy/risk/PAPER; Admin API draft/validate/publish/rollback, RBAC/CSRF/recent-MFA, immutability, audit/outbox; secret không nằm trong JSON. H03 application service resolve effective settings/profile và ghi version/hash vào lineage. | Giao diện quản trị thuộc WP-04; chưa có Telegram hoặc quản trị nhiều bot. | API unit và Compose auth/settings publish/concurrency/RLS E2E; H03 application E2E. |
| WP-03A–E | Binance USDⓈ-M adapter, order lifecycle, protection và Demo | **Chưa bắt đầu** | Có nguyên tắc cách ly secret và kế hoạch adapter; chưa có order path. | Toàn bộ Plan 03; contract tests + Demo scenarios accepted/rejected/partial/timeout/reconnect, không duplicate và bảo vệ vị thế. | Chưa có test adapter hoặc Binance Demo. |
| WP-04A–E | Admin API/UI, config approval, Telegram và hướng dẫn vận hành | **Đang triển khai một phần** | WP-01D cung cấp một số auth API/policy nền; chưa phải admin management product. | UI/dashboard; CRUD config/user; phê duyệt version; Telegram bind/webhook/command/notifier; accessibility/flow E2E. | Có unit/API auth tests và Compose auth flow; chưa có test admin UI/Telegram. |
| WP-05A–F | Source registry, ingestion, macro/news, attention, trader opinions và quyền dữ liệu | **Chưa bắt đầu** | Stories, use cases, sequence và yêu cầu provenance/rights đã thiết kế. | Toàn bộ Plan 05; nguồn hợp pháp, quota/backoff, normalize/dedup/revisions, as-of, coverage degradation và reviewer correction. | Chưa có test ingestion/research. |
| WP-06A–F | Scorer/abstain, DSL, labels/dataset, trainer, registry, drift/retrain | **Chưa bắt đầu** | Guardrails và plan về bounded learning/approval đã có. | Toàn bộ Plan 06; reproducible temporal evaluation, leakage tests, model registry, paper promotion, rollback và drift review. | Chưa có test AI/model. |
| WP-07A–E | Observability, breaker, backup/restore, security và readiness | **Chưa bắt đầu** | Có Docker healthchecks, audit nền và một số cấu hình an toàn; chưa có quy trình vận hành đủ chuẩn. | Toàn bộ Plan 07; metrics/logs/tracing, incident drills, restore RPO/RTO thực đo, supply-chain/security review, Demo dossier và gate. | Chưa có test incident/restore/release readiness. |

Plan chi tiết và đầu việc: [Plan 01](./01-foundation-database.md), [Plan 02](./02-trading-core-paper.md), [Plan 03](./03-binance-execution.md), [Plan 04](./04-admin-telegram.md), [Plan 05](./05-research-ingestion.md), [Plan 06](./06-ai-learning.md), [Plan 07](./07-operations-release.md).

## Mở rộng Multi-Bot

Thiết kế và WBS đã được người dùng duyệt ngày 2026-10-24. Các trạng thái dưới đây theo dõi phạm vi Multi-Bot; MB-01 hoàn thành (commit `4871661`).

| WP | Trạng thái | Đã hoàn thành | Còn lại / điều kiện nghiệm thu | Kiểm chứng |
| --- | --- | --- | --- | --- |
| MB-00 | **Hoàn thành** | Contract về root config/overlay, ID/time/Decimal, shared evidence, equity/FX, risk serialization workspace→account, RLS/RBAC boundary, account exclusivity và rollout migration. Reviewer đóng các finding equity double-count, root-cap publish race và bot-scope policy. | Không còn blocker; các contract được đưa thành testable gates cho MB-01/02/04. | Unit `tests/unit -q`: 520 passed; E2E `tests/e2e -q`: 13 passed / 410.59s; `git diff --check` đạt; reviewer không còn high-risk finding. |
| MB-01 | **Hoàn thành** | Bot identity 1:1 account (trigger SECURITY DEFINER tự tạo và đồng bộ name/status), `bot_grant` theo permission, `bot_id` + FK ghép + index trên 13 bảng account-scoped, RLS workspace+bot (runtime cần `app.bot_id`; admin cần membership với role workspace-wide hoặc grant), migration expand/backfill có downgrade guard; repository/UoW ghi bot tường minh. Review độc lập: không P0, các P2 đã sửa. | Commit `4871661` trên `dev`. MB-05 xem lại quy tắc bot mirror trạng thái account; WP-03/MB-08 thêm policy cho gateway/worker khi cần. | Unit `tests/unit -q`: 534 passed; E2E `tests/e2e -q`: 14 passed / 452.08s; Ruff/format/mypy, `alembic heads`/`check`, `git diff --check` đạt. [WBS chi tiết](../superpowers/plans/2026-10-24-mb01-identity-migration-wbs.md). |
| MB-02 | **Hoàn thành** | Versioned bot overlay, resolver root+bot, validate/publish/rollback Admin API; audit/outbox/RBAC/CSRF/MFA và PAPER pin config theo vị thế. | Overlay không nới cap; release khác root còn bị chặn trên đường PAPER cũ cho đến khi risk/runtime fan-out nối đủ ở MB-04/05. | Review độc lập đạt; MB-02 focused E2E đạt; final integration `tests/unit`: 793 passed, `tests/e2e`: 18 passed / 844.69s. [WBS](../superpowers/plans/2026-10-24-mb02-bot-config-wbs.md). |
| MB-03 | **Hoàn thành** | Shared evidence workspace-scoped, provenance/hash/as-of, dedupe và outbox fan-out sang BotDecision/job theo strategy/config riêng; không thể gửi order. | Exclusion reason chưa persist; old PAPER candidate path chưa nối fan-out vào plan/risk; retry giữ `evaluated_at` đã ghim. | Review độc lập MB-03/integration đạt; MB-03 focused E2E đạt; final integration `tests/unit`: 793 passed, `tests/e2e`: 18 passed / 844.69s. [WBS](../superpowers/plans/2026-10-24-mb03-shared-evidence-wbs.md). |
| MB-04 | **WBS được duyệt; sẵn sàng triển khai** | WBS chi tiết đã được duyệt 2026-10-25; phạm vi equity point-in-time, mark-to-market, FX→USDT và reservation risk workspace/account/bot. | Bắt đầu code sau khi MB-01/02/03 + SEC-01/02 integration qua full gate; ưu tiên giới hạn PAPER/fake adapter. | Chưa chạy test implementation. [WBS](../superpowers/plans/2026-10-25-mb04-workspace-risk-wbs.md). |
| MB-05–MB-09 | **Chưa bắt đầu** | Dependencies và acceptance trong [WBS Multi-Bot](../superpowers/plans/2026-10-24-multibot-implementation-plan.md). | Tiếp tục runtime, reports, Admin Web/Telegram, model references và final gates sau khi phụ thuộc trước đạt. | Chưa có test implementation. |
| SEC-01/SEC-02 | **Hoàn thành trong phạm vi hẹp** | Least-privilege DB grants theo process, ACL/RLS matrix và PostgreSQL image 18.6; migration `f4d6b8c0e2a5`. | Không phải security sign-off; security research còn P0/P1 khác cho WP-03/MB-07/MB-09. | Review độc lập đạt; final integration 793 unit + 18 E2E passed. [WBS](../superpowers/plans/2026-10-24-sec01-sec02-db-hardening-wbs.md) · [Security research](../research/security-admin-bot-2026.md). |

## Trạng thái Product backlog

| User Story | Phạm vi | Trạng thái | Ghi chú triển khai |
| --- | --- | --- | --- |
| US-01 | Workspace và phân tách mode | **Một phần** | Workspace/owner bootstrap và PAPER default có nền; UI/config vận hành chưa có. |
| US-02 | Binance USDⓈ-M connection | **Chưa bắt đầu** | Chưa có adapter/credential lifecycle/exchange contract. |
| US-03 | Quản lý, rotate, revoke secrets | **Một phần nền** | SecretProvider và MFA secret local; chưa quản lý Binance key. |
| US-04 | Users, roles, MFA | **Một phần** | Owner auth/TOTP/RBAC API; chưa member CRUD/invite/recovery/UI. |
| US-05 | Telegram identity và command permissions | **Chưa bắt đầu** | Chưa có Telegram integration. |
| US-06 | Draft và phát hành config version | **Chưa bắt đầu** | Chưa có configuration workflow/admin UI. |
| US-07 | Risk limits, pause/resume | **Một phần** | API tạo risk profile draft có permission gate; chưa có RiskEngine/entry control. |
| US-08 | Watchlist/symbol eligibility | **Một phần nền** | Có Instrument contract cơ bản; chưa có watchlist version, eligibility policy hoặc UI/API. |
| US-09 | Source registry/rights | **Chưa bắt đầu** | Mới có thiết kế. |
| US-10 | Market data point-in-time | **Hoàn thành phần nền (MVP)** | Có Binance USDⓈ-M public read adapter, OHLCV/funding/OI persistence, cursor/gap, as-of/quality và offline ingestion E2E; collector/backfill coverage production còn mở. |
| US-11 | News/macro catalyst | **Chưa bắt đầu** | Mới có thiết kế. |
| US-12 | Trending/attention score | **Chưa bắt đầu** | Mới có thiết kế. |
| US-13 | Trader opinion/review | **Chưa bắt đầu** | Mới có thiết kế; chưa thu thập/đánh giá trader. |
| US-14 | Strategy/chart candidate | **Hoàn thành phần nền (MVP)** | H01/H03/H06, recipes/registry, chart geometry, confirmation/freshness/no-lookahead và runtime candidate path có coverage; chưa có edge/profitability claim. |
| US-15 | AI score/veto/abstain | **Chưa bắt đầu** | Mới có guardrails/plan, chưa có scorer. |
| US-16 | Trade plan/hard risk gate | **Hoàn thành phần nền (MVP)** | TradePlanner/RiskEngine, hard caps, data/config/account freshness checks, persisted decision/reservation, atomic aggregate risk/margin và expiry có unit/Compose coverage. Day/rolling risk-window semantics còn cần hoàn thiện. |
| US-17 | Idempotent entry/reconcile | **Một phần — PAPER** | Paper intent có idempotency/retry/conflict và persisted lifecycle; exchange order/reconcile chưa có, thuộc WP-03. |
| US-18 | Partial fill/protective stop | **Một phần — PAPER** | Simulator có partial fill/cancel remainder/protective exit; venue order management chưa có. |
| US-19 | Position monitoring/exit recipe | **Hoàn thành phần nền (MVP) — PAPER** | Monitor xử lý stop/target/trailing/time/opposing signal và H03 Compose đi từ entry tới exit/closed trade; chưa áp dụng Binance venue. |
| US-20 | Startup/reconnect reconcile | **Chưa bắt đầu** | Chưa có Binance order/position adapter. |
| US-21 | Telegram notification/operator command | **Chưa bắt đầu** | Chưa có Telegram integration. |
| US-22 | Trade/decision/order history | **Hoàn thành phần nền (MVP)** | PAPER intent/fill/position/cost/trade ledger và decision/risk lineage được persist; admin history UI chưa có. |
| US-23 | Manual review/decision quality | **Chưa bắt đầu** | Chưa có research/review workflow. |
| US-24 | Health, breaker, incident, audit | **Một phần nền** | Healthcheck/audit primitives; chưa có breaker, incident runbook/drill, observability đầy đủ. |
| US-25 | Dashboard/performance attribution | **Một phần — dữ liệu nền** | Có PAPER closed-trade report và replay/backtest report/outcome persistence; dashboard và attribution sản phẩm thuộc WP-04. |
| US-26 | Hypothesis DSL | **Chưa bắt đầu** | Chưa có DSL. |
| US-27 | Temporal/walk-forward training | **Chưa bắt đầu** | Chưa có trainer/evaluator. |
| US-28 | Promotion/rollback approvals | **Chưa bắt đầu** | Chưa có model registry/release workflow. |
| US-29 | Drift/retraining trigger | **Chưa bắt đầu** | Chưa có monitoring/training loop. |

Acceptance criteria đầy đủ được giữ tại [Product backlog](../product/user-stories.md); trạng thái implementation phải cập nhật ở đây và link tới test cụ thể khi story được hoàn tất.

## Test và quality gate

### Bằng chứng kiểm thử hiện tại

Các kết quả dưới đây được chạy trên working tree ngày 2026-10-24. E2E dùng Docker Compose thật trên Docker Desktop Linux engine 29.9.2 với `DOCKER_CONTEXT=desktop-linux`; đây vẫn là kiểm thử local, không gửi lệnh lên Binance.

| Loại | Lệnh/kết quả | Phạm vi chứng minh | Trạng thái |
| --- | --- | --- | --- |
| Unit | `.venv/Scripts/python.exe -m pytest tests/unit -q` → **520 passed** | Final full suite sau PP2/RP4/PP6 changes. | **Đạt** |
| Target E2E | RP4 target → **1 passed / 57.69 giây**; PP6 target → **1 passed / 109.47 giây** | PostgreSQL reducer/persistence lifecycle; settings publish/decision races theo cả hai thứ tự; PP2 pinned provider/profile. Replay hash determinism được xác nhận bằng replay lặp, không restart worker. | **Đạt** |
| Full E2E | `$env:DOCKER_CONTEXT='desktop-linux'; .venv/Scripts/python.exe -m pytest tests/e2e -q` → **13 passed trong 381.82 giây** | Final Compose suite; gồm migration check/downgrade/upgrade, runtime/replay boundaries và teardown. Xóa một stale E2E project; giữ lại PostgreSQL dev container. | **Đạt** |
| Ruff lint | `.venv/Scripts/ruff.exe check src tests scripts migrations` → đạt | Lint toàn bộ source, tests, scripts và migrations. | **Đạt** |
| Ruff format | `.venv/Scripts/ruff.exe format --check src tests scripts migrations` → **227 files already formatted** | Format toàn bộ source, test, script và migration. | **Đạt** |
| Mypy | `.venv/Scripts/mypy.exe src` → **94 source files**, không lỗi | Kiểu tĩnh toàn bộ package ứng dụng. | **Đạt** |
| Diff hygiene | `git diff --check` | Không có whitespace error; Git có cảnh báo quy đổi LF/CRLF trên Windows. | **Đạt** |
| Alembic | `.venv/Scripts/python.exe -m alembic heads` → một head `b2e7a9d4c631`; full Compose suite chạy `alembic check`, downgrade base rồi upgrade head. | Migration tree hợp lệ trên PostgreSQL Compose thật. | **Đạt** |
| Compose image reuse | Một shared hash-content image cho các process Python; tag E2E phụ thuộc build input, không phụ thuộc project/run ID. | Final teardown xóa một stale E2E project; giữ lại PostgreSQL dev container, không chạy global prune. | **Đạt** |
| PAPER H03 vertical slice + replay/report | H03 app-lifespan lifecycle; pinned strategy/execution profile; reducer-owned multi-observation account state; PP6 two-order race và RP4 Compose target đạt. | Không có autonomous market dispatch, Binance order, mark-to-market, đầy đủ day/rolling risk-window semantics, funding event sensitivity hoặc profitability claim. Hash determinism không kiểm tra restart worker. | **WP-02 hoàn thành trong phạm vi MVP; giới hạn được giữ rõ** |

### Điều kiện trước commit

1. Chạy unit suite đầy đủ và E2E liên quan trên working tree hiện tại; không tính test skip, chưa chạy hoặc lỗi/flaky chưa điều tra là đạt.
2. Chạy Ruff/format/mypy, kiểm tra lock/build khi dependency/package bị ảnh hưởng; chạy Compose config và `git diff --check` khi có thay đổi liên quan.
3. Với schema/auth/outbox, E2E phải đi qua PostgreSQL/Compose thật; test cấu hình tĩnh không thay thế DB-backed E2E.
4. Ghi command, số pass/fail/skip, thời lượng, lỗi và cách xử lý vào [implementation memory](./implementation-memory.md); đọc lại mục vừa ghi trước commit.
5. Chưa commit thay đổi nếu cổng E2E liên quan chưa đạt. Repo hiện còn thay đổi working tree; trạng thái commit được rà soát riêng trước khi chốt một vòng.

## Thứ tự công việc tiếp theo

| Ưu tiên | Công việc | Lý do/phụ thuộc | Hoàn tất khi |
| --- | --- | --- | --- |
| P1 | Hoàn thiện mark-to-market và risk-window semantics | Reducer quản lý realized equity/exposure/reservation từ PAPER event; unrealized mark-to-market và định nghĩa day/rolling loss windows còn mở. | WBS riêng cho valuation vị thế đang mở và semantics reset/rolling/window timezone trước khi dùng làm risk/performance claim mở rộng. |
| P4 | Hoàn thiện WP-04A–E admin web/Telegram song song theo contract đã ổn định | Hiện chỉ có một phần API nền; vận hành an toàn cần UI và operator control. | Role/API/UI/Telegram E2E pass; mọi thay đổi có audit và duplicate update idempotent. |
| P5 | WP-03A–E Binance Demo execution | Chỉ sau khi paper contract và risk gate ổn định. | Demo contract/reconnect/partial-fill/protective-order scenarios pass; không có route tự sang LIVE. |
| P6 | WP-05A–F nguồn/nghiên cứu, rồi WP-06A–F AI | AI cần dữ liệu có quyền, provenance và label point-in-time. | Ingestion rights/as-of tests; temporal evaluation/leakage/promotion/rollback gates pass. |
| P7 | WP-07A–E vận hành và readiness | Cần hệ thống đủ module để diễn tập khôi phục và release. | Restore/incident/security evidence; Demo readiness được phê duyệt; LIVE vẫn khóa nếu thiếu gate riêng. |

## Các điểm cần giữ rõ khi cập nhật

- **PAPER:** mô phỏng nội bộ, không gửi lệnh sàn. **Demo:** môi trường thử nghiệm của Binance; chưa phải Live.
- Auth/risk profile draft là nền quản trị; chưa được mô tả thành RiskEngine hay trade planner.
- Outbox receipt bảo đảm chống nhận trùng trong DB; không tự bảo đảm side effect bên ngoài chạy đúng một lần.
- AI chỉ có quyền tạo/chấm candidate theo hợp đồng; không truy cập secret sàn, không vượt hard risk gate và không tự promote LIVE.
- Các story hoàn thành cần có liên kết test và evidence. Khi trạng thái code hoặc test thay đổi, cập nhật đồng thời bảng này và implementation memory.
